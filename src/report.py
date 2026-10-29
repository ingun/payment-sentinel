"""
Doğrulanmış araç sonuçlarından kanıta dayalı inceleme raporu üretir.

Rapordaki her sayı bir araç çıktısından kopyalanır ve her önemli gözlem bir araç adımına
("adım N: araç") bağlanır. Doğrulanmış gerçekler, doğrulanmamış hipotezler ve doğrulanmış
nedenler ayrı listelerde tutulur. LLM'in yazdığı bulgular sayı ve nedensellik iddiası açısından
süzüldükten sonra eklenir.
"""

import re
from datetime import datetime, timezone

from src.incident_matcher import DISCLAIMER as INCIDENT_DISCLAIMER
from src.policy import detected
from src.redaction import redact_text
from src.risk_engine import assess_risk
from src.schemas import Fact, Hypothesis, InvestigationReport, Recommendation
from src.tools import detections_of
from src.validation import collect_allowed_numbers, find_baseline, find_coverage, find_result, find_unverified_numbers, scope_numbers

DISCLAIMER = (
    "SENTETİK VERİ: Bu rapor sentetik ödeme olayları üzerinde otomatik üretilmiştir; üretim ölçümü değildir. "
    "Gerçekler araç çıktılarından ölçülmüş değerlerdir; hipotezler doğrulanmamıştır. Korelasyon ve zamansal "
    "öncelik nedensellik kanıtı değildir. Bu sistem salt okunurdur; hiçbir işlem, iade, reversal veya engelleme yapmaz."
)

CAUSAL_CLAIMS = re.compile(
    r"root cause (is )?confirmed|confirmed root cause|kök neden (doğrulandı|kesinleşti|budur)|kesin (kök )?neden|"
    r"kesinlikle .*neden|neden(i)? kesin(likle)?",
    re.IGNORECASE,
)

DETECTOR_LABELS = {
    "TECHNICAL_FAILURE_SPIKE": "Teknik hata oranı artışı",
    "DECLINE_RATE_SPIKE": "Red (decline) oranı artışı",
    "TIMEOUT_RATE_SPIKE": "Timeout oranı artışı",
    "LATENCY_INCREASE": "İşlem süresi (p95) artışı",
    "VOLUME_ANOMALY": "Hacim anomalisi",
    "RESPONSE_CODE_CONCENTRATION": "Yanıt kodu yoğunlaşması",
    "SEGMENT_ISOLATION": "Segmente izole sapma",
    "LATENCY_TIMEOUT_CORRELATION": "Gecikme-hata korelasyonu",
    "REVERSAL_ANOMALY": "Reversal anomalisi",
    "SERVICE_VERSION_DEGRADATION": "Servis sürümü farkı",
}
DIMENSION_LABELS = {"bank_id": "Banka", "merchant_id": "Üye işyeri", "service_name": "Servis",
                    "service_version": "Servis sürümü", "payment_method": "Ödeme yöntemi", "channel": "Kanal",
                    "terminal_id": "Terminal", "transaction_type": "İşlem tipi"}


def _pct(rate: float | None) -> str:
    return "-" if rate is None else f"%{rate * 100:.2f}"


def _num(value, unit: str = "") -> str:
    return "-" if value is None else f"{value}{unit}"


def _hhmm(iso: str | None) -> str:
    return iso[11:16] if iso else "-"


# ---------- Bölüm oluşturucular ----------


class _Facts:
    def __init__(self) -> None:
        self.items: list[Fact] = []
        self.by_key: dict[str, str] = {}

    def add(self, text: str, step: dict, key: str | None = None) -> str:
        fid = f"G{len(self.items) + 1}"
        where = f"{step['ref']} · {step['agent']}" if step.get("ref") else f"adım {step['step']}"
        self.items.append(Fact(id=fid, text=text, source=f"{where}: {step['tool']}"))
        if key:
            self.by_key.setdefault(key, fid)
        return fid

    def refs(self, *keys: str) -> list[str]:
        return [self.by_key[k] for k in keys if k in self.by_key]


def _steps(results: list[dict], tool: str) -> list[dict]:
    return [r for r in results if r["tool"] == tool and r.get("status") == "ok"]


def build_facts(results: list[dict], scope: dict) -> _Facts:
    facts = _Facts()
    base_step = next((r for r in reversed(_steps(results, "compare_with_baseline")) if r["output"] is find_baseline(results, scope)), None)
    cov_out = find_coverage(results, scope)
    cov_step = next((r for r in _steps(results, "check_data_coverage") if r["output"] is cov_out), None)
    if cov_step:
        c = cov_step["output"]
        facts.add(f"İncelenen pencerede {c['primary_transactions']} birincil ödeme işlemi ({c['events']} olay) var; "
                  f"kanıt kalitesi: {c['evidence_quality']}.", cov_step, "coverage")
    if base_step:
        b = base_step["output"]
        cur, base = b["current"], b["baseline"]
        facts.add(f"Teknik hata oranı {_pct(base['technical_rate'])} → {_pct(cur['technical_rate'])}; red oranı "
                  f"{_pct(base['decline_rate'])} → {_pct(cur['decline_rate'])}; {cur['total']} işlem.", base_step, "baseline")
        facts.add(f"p95 işlem süresi {_num(base['p95_ms'])} → {_num(cur['p95_ms'])} ms (timeout'lar hariç).", base_step, "latency_base")
    for step in results:
        if step.get("status") != "ok":
            continue
        for d in detections_of(step["tool"], step["output"]):
            if d["detected"] and d["claim_type"] != "hypothesis":
                key = f"{d['detector']}:{d['subject']}"
                if key not in facts.by_key:
                    facts.add(f"{DETECTOR_LABELS.get(d['detector'], d['detector'])}: {d['evidence']}", step, key)
                facts.by_key.setdefault(d["detector"], facts.by_key[key])
    rev_step = next(iter(_steps(results, "analyze_reversals")[-1:]), None)
    if rev_step:
        cats = rev_step["output"]["current"]["categories"]
        facts.add(f"Reversal sınıfları: meşru iptal {cats['void_after_approval']}, hata/timeout sonrası koruyucu "
                  f"{cats['after_failure']}, onay sonrası otomatik {cats['auto_after_approval']}, orphan {cats['orphan']}; "
                  f"reversal'sız POS timeout {rev_step['output']['current']['unreversed_timeouts']}.", rev_step, "reversal_classes")
    tx_step = next(iter(_steps(results, "search_transactions")[-1:]), None)
    if tx_step and tx_step["output"]["total_matches"]:
        ids = ", ".join(s["transaction_id"] for s in tx_step["output"]["samples"][:3])
        facts.add(f"Filtreye uyan {tx_step['output']['total_matches']} işlem var; örnek (maskelenmiş): {ids}.", tx_step, "samples")
    return facts


def build_hypotheses(results: list[dict], facts: _Facts) -> list[Hypothesis]:
    found = detected(results)
    names = {d["detector"] for d in found}
    hyps: list[Hypothesis] = []

    def add(text: str, *keys: str) -> None:
        refs = facts.refs(*keys)
        if refs:
            hyps.append(Hypothesis(text=text, evidence_refs=refs))

    iso = {d["scope"].get(k): k for d in found if d["detector"] == "SEGMENT_ISOLATION" for k in d["scope"]
           if k in ("bank_id", "merchant_id", "service_name") and d["subject"] == d["scope"].get(k)}
    isolated = {dim: seg for seg, dim in iso.items()}
    codes = sorted({d["subject"] for d in found if d["detector"] == "RESPONSE_CODE_CONCENTRATION"})
    if "bank_id" in isolated and names & {"TECHNICAL_FAILURE_SPIKE", "TIMEOUT_RATE_SPIKE"}:
        add(f"{isolated['bank_id']} issuer tarafında veya banka bağlantısında bir sorun olabilir: teknik hatalar bu bankada "
            f"yoğunlaşıyor ({', '.join(codes) or 'kod yok'}).", "SEGMENT_ISOLATION", "RESPONSE_CODE_CONCENTRATION", "TIMEOUT_RATE_SPIKE")
    if "bank_id" in isolated and "DECLINE_RATE_SPIKE" in names:
        add(f"{isolated['bank_id']} tarafında bir risk/fraud kuralı veya issuer davranış değişikliği olabilir; red artışı bu "
            "bankaya izole görünüyor.", "SEGMENT_ISOLATION", "DECLINE_RATE_SPIKE", "RESPONSE_CODE_CONCENTRATION")
    corr = next((d for d in found if d["detector"] == "LATENCY_TIMEOUT_CORRELATION"), None)
    if corr and (corr["extra"].get("lead_minutes") or 0) > 0:
        add(f"Gecikme artışı timeout'lara katkıda bulunuyor olabilir: gecikme {corr['extra']['lead_minutes']} dk önce başladı. "
            "Bu zamansal öncelik ve korelasyondur, nedensellik kanıtı değildir.", "LATENCY_TIMEOUT_CORRELATION", "LATENCY_INCREASE")
    if "service_name" in isolated and "SERVICE_VERSION_DEGRADATION" not in names:
        add(f"{isolated['service_name']} servisinde kaynak, bağımlılık veya bağlantı havuzu sorunu olabilir; sürüm farkı bulunmadı.",
            "SEGMENT_ISOLATION", "LATENCY_INCREASE", "TECHNICAL_FAILURE_SPIKE")
    version = next((d for d in found if d["detector"] == "SERVICE_VERSION_DEGRADATION"), None)
    if version:
        step = next(r for r in results if r["tool"] == "compare_service_versions" and r.get("status") == "ok"
                    and r["output"]["detection"] is version)
        fid = facts.add(f"Sürüm karşılaştırması: {version['evidence']}", step, "version")
        dep = version["extra"].get("deployment") or {}
        hyps.append(Hypothesis(
            text=(f"{version['scope'].get('service_name')} {version['subject']} sürümü artan hatalarla ilişkili olabilir"
                  + (f" (sürüm ilk kez {_hhmm(dep.get('first_seen'))} UTC'de görüldü)" if dep.get("first_seen") else "")
                  + ". Değişiklik kayıtlarıyla doğrulanmalıdır."),
            evidence_refs=[fid] + facts.refs("SEGMENT_ISOLATION")))
    if "REVERSAL_ANOMALY" in names:
        where = f" {isolated['merchant_id']} üye işyerinde" if "merchant_id" in isolated else ""
        add(f"Terminaller{where} onay yanıtını alamıyor olabilir (ör. terminal yazılımı veya ağ); onaylı işlemler otomatik "
            "geri alınıyor ve orphan reversal'lar oluşuyor.", "REVERSAL_ANOMALY", "reversal_classes", "SEGMENT_ISOLATION")
    similar = find_result(results, "search_similar_incidents")
    if similar and similar["matches"]:
        m = similar["matches"][0]
        known = "; ".join(m["established_factors"]) or "doğrulanmış faktör yok"
        add(f"Belirtiler geçmiş olay {m['incident_id']} ile benzer (benzerlik {m['similarity']}). O olayda doğrulanan faktör: "
            f"{known}. Bu olayda aynı faktörün varlığı doğrulanmadı.", "baseline")
    return hyps


def build_recommendations(results: list[dict], facts: _Facts, insufficient: bool) -> list[Recommendation]:
    recs: list[Recommendation] = []
    first = [facts.items[0].id] if facts.items else []
    if insufficient:
        recs.append(Recommendation(text="Örneklem yetersiz: pencereyi genişletin veya daha fazla veri birikmesini bekleyip incelemeyi tekrarlayın.",
                                   source="-", evidence_refs=facts.refs("coverage") or first))
        return recs
    found = detected(results)
    if not found:
        return [Recommendation(text="Eşik üzerinde anomali yok; olağan izlemeye devam edin.", source="-", evidence_refs=facts.refs("baseline") or first)]
    runbooks = find_result(results, "search_runbooks")
    for proc in (runbooks or {}).get("procedures", []):
        for step in proc["steps"][:4]:
            recs.append(Recommendation(text=step, source=f"{proc['source']} › {proc['section'].split(' › ')[-1]}"))
    similar = find_result(results, "search_similar_incidents")
    if similar and similar["matches"]:
        m = similar["matches"][0]
        recs.append(Recommendation(
            text=f"Geçmiş olay {m['incident_id']} çözüm adımlarının uygulanabilirliğini değerlendirin: {'; '.join(m['resolution_steps'])}",
            source=f"incidents.json › {m['incident_id']}"))
    reversal = find_result(results, "analyze_reversals")
    if reversal and reversal["reconciliation_risk"]["level"] != "LOW":
        recs.append(Recommendation(text="Reversal'sız timeout ve orphan reversal listesini mutabakat ekibine iletin.",
                                   source="-", evidence_refs=facts.refs("reversal_classes")))
    if not (runbooks or {}).get("found"):
        recs.append(Recommendation(text="İlgili runbook bulunamadı; bulguları kanıtlarıyla birlikte ödeme on-call ekibine eskale edin.",
                                   source="-", evidence_refs=first))
    return recs


def key_metrics(baseline: dict | None) -> list[dict]:
    if not baseline:
        return []
    cur, base = baseline["current"], baseline["baseline"]
    vol = baseline["volume"]
    rows = [{"metric": "İşlem (birincil)", "key": "total", "current": str(cur["total"]), "baseline": str(base["total"]),
             "change": "pencere uzunlukları farklı"},
            {"metric": "İşlem / dk", "key": "per_min", "current": _num(vol["current_per_min"]),
             "baseline": _num(vol["baseline_per_min"]), "change": "-" if vol["change_pct"] is None else f"{vol['change_pct']:+}%"}]
    for label, key, kind in [("Onay oranı", "approval_rate", "rate"),
                             ("Red oranı", "decline_rate", "rate"), ("Teknik hata oranı", "technical_rate", "rate"),
                             ("Timeout oranı", "timeout_rate", "rate"), ("Reversal oranı", "reversal_ratio", "rate"),
                             ("p50 işlem süresi (ms)", "p50_ms", "ms"), ("p95 işlem süresi (ms)", "p95_ms", "ms"),
                             ("p99 işlem süresi (ms)", "p99_ms", "ms")]:
        c, b = cur.get(key), base.get(key)
        fmt = _pct if kind == "rate" else _num
        if kind == "rate":
            change = "-" if c is None or b is None else f"{(c - b) * 100:+.2f} puan"
        else:
            change = "-" if c is None or not b else f"{(c - b) / b * 100:+.1f}%"
        rows.append({"metric": label, "key": key, "current": fmt(c), "baseline": fmt(b), "change": change})
    return rows


def validate_agent_findings(findings: list[str], results: list[dict], scope: dict) -> tuple[list[str], list[str]]:
    """LLM bulgularını sayısal tutarlılık ve nedensellik iddiası açısından süzer."""
    allowed = collect_allowed_numbers(results, extra=scope_numbers(scope))
    accepted, rejected = [], []
    for finding in findings:
        finding = redact_text(finding)
        bad = find_unverified_numbers(finding, allowed)
        if bad:
            rejected.append(f"{finding} (doğrulanmayan sayı: {', '.join(bad)})")
        elif CAUSAL_CLAIMS.search(finding):
            rejected.append(f"{finding} (kanıtlanmamış kesin neden iddiası)")
        else:
            accepted.append(finding)
    return accepted, rejected


def check_report(report: InvestigationReport, results: list[dict]) -> list[str]:
    """Rapor tutarlılığı: metrikler araç çıktısıyla aynı, gerçeklerde uydurma sayı yok, öneri/hipotezler kaynaklı."""
    issues = []
    baseline = find_baseline(results, report.scope)
    if baseline and key_metrics(baseline) != report.key_metrics:
        issues.append("Rapordaki temel metrikler araç çıktısıyla uyuşmuyor")
    allowed = collect_allowed_numbers(results, extra=scope_numbers(report.scope))
    for fact in report.facts:
        bad = find_unverified_numbers(fact.text, allowed)
        if bad:
            issues.append(f"{fact.id} gerçeğinde doğrulanmayan sayı: {', '.join(bad)}")
    fact_ids = {f.id for f in report.facts}
    for rec in report.recommendations:
        if not ((rec.source and rec.source != "-") or any(r in fact_ids for r in rec.evidence_refs)):
            issues.append(f"Kaynaksız öneri: {rec.text[:60]}")
    for hyp in report.hypotheses:
        if not any(r in fact_ids for r in hyp.evidence_refs):
            issues.append(f"Kanıtsız hipotez: {hyp.text[:60]}")
    if report.risk.score is not None and report.risk.score != sum(f.points for f in report.risk.factors) and report.risk.score != 100:
        issues.append("Risk skoru faktör puanlarının toplamına eşit değil")
    return issues


# ---------- Ana fonksiyon ----------


def build_report(*, question: str, scope: dict, results: list[dict], mode: str, data_source: str, assumptions: list[str],
                 limitations: list[str], missing: list[str], validation_log: list[dict],
                 agent_findings: list[str] | None = None) -> InvestigationReport:
    baseline = find_baseline(results, scope)
    coverage = find_coverage(results, scope)
    tool_failures = sum(1 for r in results if r.get("error_type") in {"tool", "transient"})
    risk = assess_risk(results, scope, tool_failures=tool_failures, missing=missing)
    # Baseline hiç alınamadıysa sonuç "kanıt yetersiz" değil "eksik inceleme"dir
    insufficient = baseline is not None and risk.severity == "INDETERMINATE" and risk.evidence_quality == "insufficient"
    facts = build_facts(results, scope)
    hypotheses = [] if insufficient else build_hypotheses(results, facts)
    findings = (find_result(results, "submit_findings") or {}).get("accepted_findings", []) + list(agent_findings or [])
    accepted, rejected = validate_agent_findings(findings, results, scope)

    all_detections = [d for r in results if r.get("status") == "ok" for d in detections_of(r["tool"], r["output"])]
    uniq: dict[tuple, dict] = {}
    for d in all_detections:
        uniq.setdefault((d["detector"], d["subject"], tuple(sorted(d["scope"].items())), d["detected"]), d)
    detections = [d for d in uniq.values() if d["detected"]]
    evaluated = [d for d in uniq.values() if not d["detected"]]

    affected = []
    for out in [r["output"] for r in results if r["tool"] == "breakdown_by_dimension" and r.get("status") == "ok"] + \
               [r["output"]["isolation"] for r in results if r["tool"] == "analyze_response_codes" and r.get("status") == "ok" and r["output"].get("isolation")]:
        affected.append({"dimension": out["dimension"], "metric": out["metric"], "isolated": out["isolated"],
                         "segment": out["isolated_segment"], "verdict": out["verdict"],
                         "top_segments": [{k: s[k] for k in ("segment", "n", "rate", "baseline_rate", "rest_rate", "z_vs_rest", "excess")}
                                          for s in out["segments"][:5]]})
    codes = []
    for r in results:
        if r["tool"] == "analyze_response_codes" and r.get("status") == "ok":
            codes = [c for c in r["output"]["codes"] if c["unusual"]] or codes
    latency_out = find_result(results, "analyze_latency_timeouts")
    latency_findings = []
    if latency_out:
        latency_findings.append(f"p95 {_num(latency_out['baseline_p95_ms'])} → {_num(latency_out['current_p95_ms'])} ms; timeout oranı "
                                f"{_pct(latency_out['baseline_timeout_rate'])} → {_pct(latency_out['current_timeout_rate'])} "
                                f"(kapsam: {', '.join(f'{k}={v}' for k, v in latency_out['filters'].items()) or 'tüm trafik'}).")
        latency_findings.append(f"Spearman rho={latency_out['rho']} ({latency_out['buckets_used']} dilim). {latency_out['order']}")
        latency_findings.append("Korelasyon ve zamansal öncelik nedensellik kanıtı değildir.")
    reversal_out = find_result(results, "analyze_reversals")
    reversal_findings = []
    if reversal_out:
        reversal_findings.append(reversal_out["detection"]["evidence"])
        rr = reversal_out["reconciliation_risk"]
        reversal_findings.append(f"Mutabakat riski göstergesi: {rr['level']}. " + " ".join(rr["indicators"]))
    version_findings = [o["detection"]["evidence"] for o in [r["output"] for r in results if r["tool"] == "compare_service_versions" and r.get("status") == "ok"]]
    similar = find_result(results, "search_similar_incidents")
    runbooks = find_result(results, "search_runbooks")

    limitations = list(limitations)
    if insufficient:
        limitations.append("Kanıt yetersiz: örneklem veya veri kapsamı güçlü bir sonuç için yeterli değil; anomali iddia edilmedi.")
    if rejected:
        limitations.append(f"Agent'ın {len(rejected)} bulgusu doğrulamadan geçemediği için rapora alınmadı.")
    if missing:
        limitations.append(f"Eksik kanıt: {', '.join(missing)}. İlgili bölümler eksik olabilir.")
    if tool_failures:
        limitations.append(f"{tool_failures} araç çağrısı başarısız oldu; ayrıntılar işlem izlerinde.")
    limitations += [f"Kanıt sınırlaması: {x}" for x in risk.evidence_limitations]
    limitations += ["Veriler sentetiktir; eşikler ve risk ağırlıkları demo amaçlıdır, kalibre edilmemiştir.",
                    "Parasal etki hesaplanmadı: veri setinde güvenilir tutar alanı yok.",
                    "Korelasyon nedensellik değildir; hiçbir neden bu otomatik incelemeyle doğrulanmamıştır."]

    status = "insufficient_evidence" if insufficient else ("incomplete" if (missing or baseline is None) else "complete")
    summary = _executive_summary(risk, detections, affected, insufficient, status)
    report = InvestigationReport(
        title=f"Ödeme Operasyonu İnceleme Raporu: {_hhmm(scope['current_start'])}–{_hhmm(scope['current_end'])} UTC",
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        question=question or "(boş talep)", status=status, mode=mode, data_source=data_source,
        data_label=(coverage or {}).get("data_label", "synthetic"), scope=scope,
        data_coverage={
            "primary_transactions": (coverage or {}).get("primary_transactions"),
            "events": (coverage or {}).get("events"), "coverage_ratio": (coverage or {}).get("coverage_ratio"),
            "d1_available": (coverage or {}).get("d1_available"), "evidence_quality": risk.evidence_quality,
            "baseline_transactions": baseline["baseline"]["total"] if baseline else None,
            "tool_calls": len(results), "tool_failures": tool_failures,
        },
        assumptions=assumptions, executive_summary=summary, key_metrics=key_metrics(baseline),
        detections=detections, evaluated=evaluated, affected_scope=affected, response_codes=codes,
        latency_findings=latency_findings, reversal_findings=reversal_findings, version_findings=version_findings,
        similar_incidents=(similar or {}).get("matches", []),
        similar_incidents_note=((similar or {}).get("message", "Geçmiş olay karşılaştırması yapılmadı.") + " " + INCIDENT_DISCLAIMER),
        runbook_sources=[f"{h['source']} › {h['section']}" for h in (runbooks or {}).get("hits", [])],
        risk=risk, recommendations=build_recommendations(results, facts, insufficient), facts=facts.items,
        hypotheses=hypotheses, confirmed_causes=[], agent_findings=accepted, limitations=list(dict.fromkeys(limitations)),
        tool_steps=[{"step": r.get("step"), "ref": r.get("ref"), "agent": r.get("agent"), "tool": r["tool"], "status": r.get("status"),
                     "summary": r.get("summary") or r.get("error", "")} for r in results],
        validation=list(validation_log), disclaimer=DISCLAIMER,
    )
    issues = check_report(report, results)
    report.validation.append({"check": "Rapor sayıları araç çıktılarıyla tutarlı; öneri ve hipotezler kaynaklı", "passed": not issues, "details": issues})
    for item in rejected:
        report.validation.append({"check": "Agent bulgusu reddedildi", "passed": False, "details": [item]})
    return report


def _executive_summary(risk, detections: list[dict], affected: list[dict], insufficient: bool, status: str) -> list[str]:
    if insufficient:
        return ["Kanıt yetersiz: incelenen pencerede güvenilir bir sonuç için yeterli işlem yok. Anomali iddia edilmedi; "
                "pencereyi genişletmek veya daha fazla veri beklemek önerilir."]
    lines = [f"Risk seviyesi {risk.severity}" + (f" (skor {risk.score}/100)" if risk.score is not None else "")
             + f"; kanıt kalitesi {risk.evidence_quality}, güven {risk.confidence}."]
    if not detections:
        lines.append("Baseline'a göre eşik üzerinde bir sapma tespit edilmedi.")
    order = ["TECHNICAL_FAILURE_SPIKE", "DECLINE_RATE_SPIKE", "TIMEOUT_RATE_SPIKE", "REVERSAL_ANOMALY", "LATENCY_INCREASE",
             "SERVICE_VERSION_DEGRADATION", "VOLUME_ANOMALY"]
    shown: set[str] = set()
    for d in sorted((d for d in detections if d["detector"] in order), key=lambda d: order.index(d["detector"])):
        if d["detector"] in shown or len(shown) >= 3:
            continue
        shown.add(d["detector"])
        lines.append(f"{DETECTOR_LABELS[d['detector']]}: {d['evidence']}")
    for a in affected:
        if a["isolated"]:
            lines.append(f"Etkilenen kapsam: {a['verdict']}")
    if risk.affected_transactions:
        lines.append(f"Etkilenen işlem (baseline'a göre fazladan): ~{risk.affected_transactions}. Parasal etki belirlenemedi.")
    if status == "incomplete":
        lines.append("İnceleme eksik: bazı kanıtlar alınamadı (bkz. sınırlamalar).")
    return lines


# ---------- Markdown ----------


def to_markdown(report: InvestigationReport) -> str:
    s = report.scope
    filters = ", ".join(f"{k}={v}" for k, v in s.get("filters", {}).items()) or "yok (tüm trafik)"
    lines = [
        f"# {report.title}", "",
        f"> **SENTETİK VERİ** · Durum: **{ {'complete': 'tamamlandı', 'incomplete': 'eksik', 'insufficient_evidence': 'kanıt yetersiz'}[report.status] }** · "
        f"Mod: {report.mode} · Oluşturulma: {report.generated_at}", "",
        "## 1. İnceleme sorusu ve zaman penceresi",
        f"- Soru: _{report.question}_",
        f"- İncelenen dönem: {s['current_start']} – {s['current_end']} ({s['window_minutes']} dk)",
        f"- Baseline: {s['baseline_start']} – {s['baseline_end']} ({s['baseline_minutes']} dk); hacim için dünün aynı penceresi",
        f"- Kapsam filtreleri: {filters}",
    ]
    lines += [f"- Varsayım: {a}" for a in report.assumptions]
    c = report.data_coverage
    lines += ["", "## 2. Veri kaynağı ve kapsamı",
              f"- Veri kaynağı: {report.data_source} (etiket: {report.data_label})",
              f"- Birincil işlem: {_num(c['primary_transactions'])} (olay: {_num(c['events'])}); baseline: {_num(c['baseline_transactions'])}",
              f"- Zaman kapsamı: {_pct(c['coverage_ratio'])}; D-1 verisi: {'var' if c['d1_available'] else 'yok'}; kanıt kalitesi: **{c['evidence_quality']}**",
              f"- Araç çağrısı: {c['tool_calls']} (başarısız: {c['tool_failures']})"]
    lines += ["", "## 3. Yönetici özeti"] + [f"- {x}" for x in report.executive_summary]
    lines += ["", "## 4. Temel metrikler ve baseline karşılaştırması"]
    if report.key_metrics:
        lines += ["| Metrik | Güncel | Baseline | Değişim |", "|---|---|---|---|"]
        lines += [f"| {m['metric']} | {m['current']} | {m['baseline']} | {m['change']} |" for m in report.key_metrics]
    else:
        lines.append("- Metrikler hesaplanamadı.")
    lines += ["", "## 5. Tespit edilen paternler"]
    for d in report.detections:
        where = ", ".join(f"{k}={v}" for k, v in d.scope.items()) or "tüm trafik"
        lines.append(f"- **{DETECTOR_LABELS.get(d.detector, d.detector)}**{f' ({d.subject})' if d.subject else ''} [kapsam: {where}]: {d.evidence}")
        lines.append(f"  - Gözlenen {d.observed} · baseline {d.baseline} · mutlak sapma {d.abs_deviation} · göreli {_num(d.rel_deviation_pct, '%')} · "
                     f"n={d.sample_size.get('current')}/{d.sample_size.get('baseline')} · kanıt: {d.evidence_quality} · tür: {d.claim_type}")
        lines.append(f"  - Yöntem: {d.method}")
        if d.limitations:
            lines.append(f"  - Sınırlamalar: {' '.join(d.limitations)}")
    if not report.detections:
        lines.append("- Eşik üzerinde patern tespit edilmedi.")
    insufficient = [d for d in report.evaluated if d.insufficient_data]
    lines += [f"- {DETECTOR_LABELS.get(d.detector, d.detector)}: veri yetersiz, değerlendirilemedi ({' '.join(d.limitations)})" for d in insufficient]
    lines += ["", "## 6. Etkilenen bankalar, üye işyerleri, servisler"]
    for a in report.affected_scope:
        lines.append(f"- {DIMENSION_LABELS.get(a['dimension'], a['dimension'])} kırılımı ({a['metric']}): {a['verdict']}")
    if not report.affected_scope:
        lines.append("- Segment kırılımı yapılmadı (gerekli sinyal yok).")
    if report.risk.affected_scope:
        lines.append(f"- İzole kapsam: {', '.join(f'{k}={v}' for k, v in report.risk.affected_scope.items())}")
    lines += ["", "## 7. İlgili yanıt kodları"]
    lines += [f"- {x['code']} ({x['description']}, {x['status']}): {x['count']} işlem, oran {_pct(x['baseline_rate'])} → {_pct(x['rate'])} (z={x['z']})"
              for x in report.response_codes] or ["- Olağandışı yanıt kodu yok."]
    lines += ["", "## 8. Gecikme, timeout, reversal ve sürüm bulguları"]
    lines += [f"- {x}" for x in report.latency_findings + report.reversal_findings + report.version_findings] or ["- İlgili analiz gerekmedi."]
    lines += ["", "## 9. Geçmiş incident eşleşmeleri"]
    for m in report.similar_incidents:
        lines.append(f"- **{m['incident_id']}** (benzerlik {m['similarity']}, {m['match_type']}): {m['summary']}")
        lines.append(f"  - Neden benzer: {'; '.join(m['why_similar']) or '-'} · Farklar: {'; '.join(m['differences']) or '-'}")
        lines.append(f"  - O olayda doğrulanan faktörler: {'; '.join(m['established_factors']) or 'yok'}; doğrulanmamış: {'; '.join(m['unconfirmed_factors']) or 'yok'}")
    if not report.similar_incidents:
        lines.append("- Eşleşme yok.")
    lines.append(f"- _{report.similar_incidents_note}_")
    lines += ["", "## 10. İlgili runbook'lar"] + ([f"- {x}" for x in report.runbook_sources] or ["- Runbook araması yapılmadı veya eşleşme yok."])
    r = report.risk
    lines += ["", "## 11. Risk değerlendirmesi ve gerekçesi",
              f"- **Seviye: {r.severity}**" + (f" (skor {r.score}/100)" if r.score is not None else "") + f" · kanıt kalitesi {r.evidence_quality} · güven {r.confidence}"]
    lines += [f"  - {f.factor}: {f.value} → +{f.points} ({f.explanation})" for f in r.factors]
    if r.capped:
        lines.append(f"  - {r.capped}")
    lines += [f"  - Güven: {x}" for x in r.confidence_reasons]
    lines += [f"- Etkilenen işlem: ~{r.affected_transactions}", f"- Parasal etki: {r.monetary_impact}", f"- Formül: {r.formula}", f"- _{r.note}_"]
    lines += ["", "## 12. Önerilen sonraki adımlar"]
    lines += [f"{i}. {rec.text} _(kaynak: {rec.source if rec.source != '-' else ', '.join(rec.evidence_refs)})_" for i, rec in enumerate(report.recommendations, 1)]
    lines += ["", "## 13. Doğrulanmış gerçekler ve doğrulanmamış hipotezler", "**Gerçekler (ölçülmüş):**"]
    lines += [f"- **{f.id}**: {f.text} _({f.source})_" for f in report.facts] or ["- Yok."]
    lines += ["", "**Hipotezler (doğrulanmadı):**"]
    lines += [f"- {h.text} _(dayanak: {', '.join(h.evidence_refs)})_" for h in report.hypotheses] or ["- Yok."]
    lines += ["", "**Doğrulanmış nedenler:**", "- " + ("; ".join(report.confirmed_causes) or "Yok. Neden bu otomatik incelemeyle doğrulanamaz; insan incelemesi gerekir.")]
    if report.agent_findings:
        lines += ["", "**Agent bulgu özeti** _(sayılar araç çıktılarıyla doğrulandı; LLM ifadelerinin anlamı otomatik doğrulanamaz)_:"] + [f"- {x}" for x in report.agent_findings]
    lines += ["", "## 14. Veri sınırlamaları ve eksik kanıt"] + [f"- {x}" for x in report.limitations]
    if report.agent_runs:
        orch = report.orchestration
        lines += ["", "## Ek: Çoklu agent yürütmesi", f"- Tur sayısı: {orch.get('rounds')}"]
        lines += [f"- **{a['agent']}** (tur {a['round']}) · {a['status']} · {a['elapsed_ms']} ms · {a['tool_calls']} araç · "
                  f"kanıt: {', '.join(a['evidence_refs']) or '-'} · {a['summary']}" for a in report.agent_runs]
        lines += [f"- Çalıştırılmadı: {s['agent']} ({s['reason']})" for s in orch.get("skipped", [])]
        lines += [f"- Çelişki ({c['kind']}, {'çözüldü' if c['resolved'] else 'çözülmedi'}): {c['description']}" for c in report.conflicts]
        lines += [f"- Reddedilen iddia ({x['agent']}): {x['claim'][:120]} — {x['reason']}" for x in orch.get("rejected_claims", [])]
    lines += ["", "## Ek: Araç adımları"] + [f"{t.get('ref') or t['step']}. `{t['tool']}`{' · ' + t['agent'] if t.get('agent') else ''} · "
                                              f"{t['status']} · {t['summary']}" for t in report.tool_steps]
    lines += ["", "## Ek: Doğrulama durumu"]
    lines += [f"- {'✅' if v['passed'] else '⚠️'} {v['check']}" + (f": {'; '.join(v['details'])}" if v.get("details") else "") for v in report.validation]
    lines += ["", f"> {report.disclaimer}"]
    return redact_text("\n".join(lines))
