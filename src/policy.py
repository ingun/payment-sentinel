"""
Kanıta dayalı inceleme politikası: bir sonraki aracı ara sonuçlara göre seçer.

Sabit bir araç sırası yoktur. Örneğin teknik hata artışı varsa önce banka kırılımı yapılır;
sorun bir bankaya izole değilse servis kırılımına geçilir; izole bir servis varsa sürümler
karşılaştırılır; timeout varsa gecikme korelasyonu ve reversal'lar incelenir. Kanıt yetersizse
inceleme erken sonlandırılır. Her karar izlerde gösterilen kısa bir gerekçe taşır.

Politika LLM modunda da ipucu olarak kullanılır ve verifier'ın "zorunlu kanıt" listesini üretir.
"""

import re
from dataclasses import dataclass

from src.tools import detections_of
from src.validation import find_baseline, find_coverage, find_result

REVERSAL_HINTS = re.compile(r"reversal|iptal|geri al|mutabakat|reconcil|settlement", re.IGNORECASE)
DEPLOY_HINTS = re.compile(r"deploy|sürüm|surum|version|release|canary", re.IGNORECASE)


@dataclass
class Action:
    tool: str
    args: dict
    reason: str


def _norm(filters: dict | None) -> dict:
    return {k: v for k, v in (filters or {}).items() if v not in (None, "")}


def _attempted(results: list[dict], tool: str, **where) -> bool:
    """Araç bu argümanlarla daha önce çağrıldı mı (başarılı ya da kalıcı hata)?"""
    for r in results:
        if r["tool"] != tool or r.get("error_type") in ("transient", "invalid_args"):
            continue
        args = r.get("args") or {}
        if all((_norm(args.get(k)) if k == "filters" else args.get(k)) == v for k, v in where.items()):
            return True
    return False


def _failed(results: list[dict], tool: str) -> bool:
    return any(r["tool"] == tool and r.get("error_type") == "tool" for r in results)


def detected(results: list[dict]) -> list[dict]:
    found = []
    for r in results:
        if r.get("status") == "ok":
            found += [d for d in detections_of(r["tool"], r["output"]) if d["detected"]]
    return found


def signals(baseline: dict | None) -> dict[str, bool]:
    names = {d["detector"] for d in (baseline or {}).get("detections", []) if d["detected"]}
    deltas = (baseline or {}).get("deltas", {})
    cur = (baseline or {}).get("current", {})
    return {
        "technical": "TECHNICAL_FAILURE_SPIKE" in names,
        "timeout": "TIMEOUT_RATE_SPIKE" in names,
        "decline": "DECLINE_RATE_SPIKE" in names,
        "latency": "LATENCY_INCREASE" in names,
        "volume": "VOLUME_ANOMALY" in names,
        "reversal": (deltas.get("reversal_ratio_change_pct") or 0) >= 50 and cur.get("reversals", 0) >= 15,
    }


def focus_filters(scope: dict, results: list[dict]) -> dict:
    """Kapsam filtreleri + izole bulunan segmentler (ör. bank_id=BANK_B)."""
    focus = dict(scope.get("filters", {}))
    for r in results:
        if r.get("status") != "ok":
            continue
        out = r["output"]
        iso = out if r["tool"] == "breakdown_by_dimension" else out.get("isolation") if r["tool"] == "analyze_response_codes" else None
        if iso and iso.get("isolated") and iso["dimension"] in ("bank_id", "merchant_id", "service_name"):
            focus.setdefault(iso["dimension"], iso["isolated_segment"])
    return focus


def runbook_query(found: list[dict]) -> str:
    names = {d["detector"] for d in found}
    words: list[str] = []
    if names & {"TIMEOUT_RATE_SPIKE", "TECHNICAL_FAILURE_SPIKE"}:
        words.append("POS timeout issuer banka")
    if "DECLINE_RATE_SPIKE" in names:
        words.append("red decline issuer banka")
    if "LATENCY_INCREASE" in names:
        words.append("gecikme latency p95")
    if "REVERSAL_ANOMALY" in names:
        words.append("reversal orphan mutabakat terminal")
    if "SERVICE_VERSION_DEGRADATION" in names:
        words.append("deployment sürüm canary")
    words += sorted({d["subject"] for d in found if d["detector"] == "RESPONSE_CODE_CONCENTRATION" and d["subject"]})
    return " ".join(words) or "ödeme anomalisi"


def deterministic_findings(found: list[dict]) -> list[str]:
    order = ["TECHNICAL_FAILURE_SPIKE", "TIMEOUT_RATE_SPIKE", "DECLINE_RATE_SPIKE", "SEGMENT_ISOLATION",
             "RESPONSE_CODE_CONCENTRATION", "LATENCY_INCREASE", "LATENCY_TIMEOUT_CORRELATION", "REVERSAL_ANOMALY",
             "SERVICE_VERSION_DEGRADATION", "VOLUME_ANOMALY"]
    seen, lines = set(), []
    for d in sorted(found, key=lambda d: order.index(d["detector"]) if d["detector"] in order else 99):
        key = (d["detector"], d["subject"])
        if key not in seen:
            seen.add(key)
            lines.append(d["evidence"])
    return lines[:6] or ["Eşik üzerinde bir anomali tespit edilmedi."]


def next_action(scope: dict, results: list[dict]) -> Action | None:
    cw = {"start": scope["current_start"], "end": scope["current_end"]}
    bw = {"start": scope["baseline_start"], "end": scope["baseline_end"]}
    filters = _norm(scope.get("filters"))
    request = scope.get("request", "")

    def pair(**extra) -> dict:
        return {"current_window": cw, "baseline_window": bw, **extra}

    # 1-2. Veri kapsamı ve baseline karşılaştırması her incelemenin temelidir
    if not _attempted(results, "check_data_coverage"):
        return Action("check_data_coverage", {"window": cw, "filters": filters},
                      "Önce pencerenin veri kapsamı ve örneklem yeterliliği kontrol ediliyor.")
    if not _attempted(results, "compare_with_baseline", filters=filters):
        return Action("compare_with_baseline", pair(filters=filters),
                      f"Güncel dönem {scope['baseline_minutes']} dk baseline ve dünün aynı penceresiyle karşılaştırılıyor.")

    coverage = find_coverage(results, scope)
    baseline = find_baseline(results, scope)
    found = detected(results)
    if coverage and coverage["evidence_quality"] == "insufficient":
        return _finish(results, found, "Veri kapsamı/örneklem yetersiz; güçlü iddia üretmeden incelemeyi sonlandırıyorum.")
    if baseline is None:
        return _finish(results, found, "Baseline karşılaştırması alınamadı; inceleme eksik olarak raporlanacak.")

    sig = signals(baseline)
    focus = focus_filters(scope, results)

    # 3. Teknik hata / timeout: önce banka, izole değilse servis kırılımı
    if sig["technical"] or sig["timeout"]:
        for dim in ("bank_id", "service_name"):
            if dim in filters:
                continue  # kullanıcı bu boyutu zaten filtreledi
            if dim in focus:
                break  # bu boyutta izole segment bulundu; daha geniş kırılım gerekmez
            if _failed(results, "breakdown_by_dimension"):
                if not _attempted(results, "analyze_response_codes", filters=filters, dimension=dim):
                    return Action("analyze_response_codes", pair(filters=filters, dimension=dim),
                                  f"breakdown_by_dimension kalıcı hata verdi; alternatif olarak yanıt kodu üzerinden {dim} kırılımı yapılıyor.")
                continue
            if not _attempted(results, "breakdown_by_dimension", dimension=dim, metric="technical_rate", filters=filters):
                prev = "Sorun tek bir bankaya izole değil; " if dim == "service_name" and "bank_id" not in filters else ""
                return Action("breakdown_by_dimension", pair(filters=filters, dimension=dim, metric="technical_rate"),
                              f"{prev}teknik hata artışının {dim} boyutunda tek segmente izole olup olmadığı test ediliyor.")
            break_out = find_result(results, "breakdown_by_dimension", {"dimension": dim, "metric": "technical_rate"})
            if break_out and break_out["isolated"]:
                break
        focus = focus_filters(scope, results)
        if not _attempted(results, "analyze_response_codes", filters=focus):
            seg = ", ".join(f"{k}={v}" for k, v in focus.items() if k not in filters) or "tüm trafik"
            return Action("analyze_response_codes", pair(filters=focus),
                          f"Etkilenen kapsamda ({seg}) hangi yanıt kodlarının arttığı inceleniyor.")

    # 4. Red artışı: yanıt kodları ve banka yoğunlaşması
    if sig["decline"] and not sig["technical"] and not _attempted(results, "analyze_response_codes", filters=filters, dimension="bank_id"):
        return Action("analyze_response_codes", pair(filters=filters, dimension="bank_id"),
                      "Red oranı arttı; artan red kodu ve bankaya yoğunlaşıp yoğunlaşmadığı inceleniyor.")
    focus = focus_filters(scope, results)

    # 5. Gecikme / timeout korelasyonu etkilenen kapsamda
    if (sig["timeout"] or sig["latency"]) and not _attempted(results, "analyze_latency_timeouts", filters=focus):
        return Action("analyze_latency_timeouts", pair(filters=focus),
                      "Timeout/gecikme sinyali var; aynı kapsamda gecikmenin hatalarla birlikte ve önce artıp artmadığı ölçülüyor.")

    # 6. İzole servis veya deployment sorusu: sürüm karşılaştırması
    service = focus.get("service_name")
    if service and (sig["technical"] or DEPLOY_HINTS.search(request)) and not _attempted(results, "compare_service_versions", service_name=service):
        version_filters = {k: v for k, v in focus.items() if k not in ("service_name", "service_version")}
        return Action("compare_service_versions", {"window": cw, "service_name": service, "filters": version_filters},
                      f"Sorun {service} servisinde yoğunlaşıyor; aynı penceredeki sürümler ve deployment zamanı karşılaştırılıyor.")

    # 7. Reversal: sinyal, timeout (koruyucu reversal'lar) veya talep
    if (sig["reversal"] or sig["timeout"] or REVERSAL_HINTS.search(request)) and not _attempted(results, "analyze_reversals", filters=focus):
        if _attempted(results, "analyze_reversals"):
            seg = ", ".join(f"{k}={v}" for k, v in focus.items() if k not in filters)
            return Action("analyze_reversals", pair(filters=focus),
                          f"Teknik reversal'lar {seg} segmentinde yoğunlaşıyor; sınıflandırma bu segment için ayrıca yapılıyor.")
        why = "reversal oranı arttı" if sig["reversal"] else "timeout'lar reversal üretir" if sig["timeout"] else "talep reversal/mutabakat soruyor"
        return Action("analyze_reversals", pair(filters=focus), f"Reversal'lar sınıflandırılıyor ({why}); mutabakat riski değerlendiriliyor.")
    reversal = find_result(results, "analyze_reversals")
    if (reversal and reversal["detection"]["detected"] and "merchant_id" not in focus
            and not _attempted(results, "breakdown_by_dimension", dimension="merchant_id", metric="reversal_ratio", filters=filters)):
        return Action("breakdown_by_dimension", pair(filters=filters, dimension="merchant_id", metric="reversal_ratio"),
                      "Teknik reversal artışı var; artışın üye işyerine izole olup olmadığı test ediliyor.")

    # 8. Anomali varsa örnek işlemler, geçmiş olaylar ve runbook'lar
    found = detected(results)
    focus = focus_filters(scope, results)
    if found:
        names = {d["detector"] for d in found}
        if not _attempted(results, "search_transactions"):
            status = "timeout" if "TIMEOUT_RATE_SPIKE" in names else "error" if "TECHNICAL_FAILURE_SPIKE" in names \
                else "declined" if "DECLINE_RATE_SPIKE" in names else None
            extra = {"transaction_type": "reversal"} if status is None and "REVERSAL_ANOMALY" in names else {}
            return Action("search_transactions", {"window": cw, "filters": {**focus, **extra}, "transaction_status": status, "limit": 5},
                          "Tespiti somutlaştırmak için etkilenen kapsamdan maskelenmiş örnek işlemler alınıyor.")
        if not _attempted(results, "search_similar_incidents"):
            return Action("search_similar_incidents", pair(filters=filters),
                          "Anomali imzası geçmiş incident kayıtlarıyla karşılaştırılıyor.")
        if not _attempted(results, "search_runbooks"):
            query = runbook_query(found)
            return Action("search_runbooks", {"query": query}, f"İlgili operasyon runbook'u aranıyor: '{query}'.")
    return _finish(results, found, "Gerekli kanıtlar toplandı; bulgular rapora gönderiliyor." if found
                   else "Eşik üzerinde anomali yok; ek araştırma gerekmiyor, sonuç raporlanıyor.")


def _finish(results: list[dict], found: list[dict], reason: str) -> Action | None:
    if _attempted(results, "submit_findings"):
        return None
    return Action("submit_findings", {"findings": deterministic_findings(found)}, reason)


def required_evidence(scope: dict, results: list[dict]) -> list[str]:
    """Rapordan önce bulunması gereken araç sonuçları (verifier kullanır)."""
    missing = []
    if find_baseline(results, scope) is None:
        missing.append("compare_with_baseline")
        return missing
    coverage = find_coverage(results, scope)
    if coverage and coverage["evidence_quality"] == "insufficient":
        return missing
    if detected(results):
        for tool in ("search_similar_incidents", "search_runbooks"):
            if find_result(results, tool) is None:
                missing.append(tool)
    return missing
