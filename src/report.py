"""
Doğrulanmış araç sonuçlarından kanıta dayalı inceleme raporu üretir.

Rapordaki tüm sayılar araç çıktılarından kopyalanır; LLM'in yazdığı bulgular ise
sayısal tutarlılık ve nedensellik iddiası açısından kontrol edildikten sonra eklenir.
Gözlem, olası açıklama ve doğrulanmış kök neden ayrı alanlarda tutulur.
"""

import re
from datetime import datetime, timezone

from src.incident_matcher import DISCLAIMER as INCIDENT_DISCLAIMER
from src.risk_engine import assess_risk
from src.schemas import (
    CodeItem,
    ComparisonResult,
    Deviation,
    Explanation,
    IncidentReport,
    MetricsResult,
    Observation,
    Pattern,
    Recommendation,
)
from src.validation import (
    check_recommendations_supported,
    check_report_consistency,
    collect_allowed_numbers,
    find_result,
    find_unverified_numbers,
)

DISCLAIMER = (
    "Bu rapor sentetik veri üzerinde otomatik üretilmiştir. Gözlemler ölçülmüş değerlerdir; olası açıklamalar "
    "hipotezdir. Zaman korelasyonu nedensellik kanıtı değildir ve kök neden insan incelemesiyle doğrulanmalıdır."
)

CAUSAL_CLAIMS = re.compile(
    r"root cause (is )?confirmed|kök neden (doğrulandı|kesinleşti|budur)|kesin (kök )?neden|kesinlikle .*neden",
    re.IGNORECASE,
)

PATTERN_LABELS = {
    "ERROR_RATE_SPIKE": "Hata oranında ani yükseliş",
    "VOLUME_ANOMALY": "İşlem hacminde olağandışı değişim",
    "ERROR_CODE_CONCENTRATION": "Hata kodu yoğunlaşması",
    "UNKNOWN_ERROR_CODE": "Bilinmeyen hata kodu",
    "SERVICE_LATENCY_INCREASE": "Serviste gecikme artışı",
    "SERVICE_ERROR_SPIKE": "Serviste hata artışı",
    "ERROR_LATENCY_COOCCURRENCE": "Hata ve gecikme artışının aynı zaman diliminde görülmesi",
}


def _pct(rate: float | None) -> str:
    return "-" if rate is None else f"%{rate * 100:.2f}"


def _num(value: float | None, unit: str = "") -> str:
    return "-" if value is None else f"{value}{unit}"


def _signed(value: float | None, unit: str = "") -> str:
    return "-" if value is None else f"{value:+}{unit}"


def _hhmm(iso: str | None) -> str:
    return iso[11:16] if iso else "-"


# ---------- Bölüm oluşturucular ----------


def build_observations(comparison: dict | None, analysis: dict | None, logs: dict | None) -> list[Observation]:
    obs: list[Observation] = []

    def add(text: str, evidence: str) -> str:
        oid = f"O{len(obs) + 1}"
        obs.append(Observation(id=oid, text=text, evidence=evidence))
        return oid

    if comparison:
        cur, base = comparison["current"], comparison["baseline"]
        add(
            f"Hata oranı baseline'da {_pct(base['error_rate'])} iken incelenen dönemde {_pct(cur['error_rate'])} "
            f"({_signed(comparison['error_rate_delta_pp'])} puan); {cur['failed']} başarısız / {cur['total_attempts']} işlem.",
            "compare_time_windows",
        )
        add(
            f"Dakika başı işlem hacmi {_num(comparison['volume_per_min_baseline'])} → {_num(comparison['volume_per_min_current'])} "
            f"({_signed(comparison['volume_change_pct'], '%')}).",
            "compare_time_windows",
        )
        add(
            f"p95 gecikme {_num(base['p95_latency_ms'])} → {_num(cur['p95_latency_ms'])} ms ({_signed(comparison['p95_change_pct'], '%')}).",
            "compare_time_windows",
        )
    if analysis:
        for p in analysis["patterns"]:
            if p["detected"]:
                add(f"{PATTERN_LABELS.get(p['type'], p['type'])}: {p['evidence']}", f"analyze_error_patterns:{p['type']}")
        if analysis["onset_time"]:
            state = "devam ediyor" if analysis["ongoing"] else "sona erdi"
            shape = {"sudden": "ani", "gradual": "kademeli"}.get(analysis["onset_shape"], "-")
            add(
                f"Anomali {_hhmm(analysis['onset_time'])} UTC diliminde başladı, {analysis['duration_min']} dk sürdü ve {state}; "
                f"başlangıç şekli: {shape}.",
                "analyze_error_patterns:timeline",
            )
    if logs and logs["total_matches"]:
        ids = ", ".join(s["transaction_id"] for s in logs["samples"][:3])
        add(f"Filtreye uyan {logs['total_matches']} log kaydı var; örnek işlemler: {ids}.", "search_payment_logs")
    return obs


def _refs(observations: list[Observation], *keys: str) -> list[str]:
    return [o.id for o in observations if any(k in o.evidence for k in keys)]


def build_explanations(analysis: dict | None, similar: dict | None, observations: list[Observation]) -> list[Explanation]:
    if not analysis or not analysis["detected_types"]:
        return []
    detected = {p["type"]: p for p in analysis["patterns"] if p["detected"]}
    top_code = analysis["top_error_codes"][0]["key"] if analysis["top_error_codes"] else None
    services = analysis["affected_services"]
    explanations: list[Explanation] = []

    def add(text: str, keys: tuple[str, ...], confidence: str | None = None) -> None:
        refs = _refs(observations, *keys)
        if refs:
            level = confidence or ("medium" if len(refs) >= 2 else "low")
            explanations.append(Explanation(text=text, evidence_refs=refs, confidence=level))

    if top_code in {"PROVIDER_TIMEOUT", "CONNECTION_RESET"} and "ERROR_CODE_CONCENTRATION" in detected:
        add(
            f"Ödeme sağlayıcısı bağlantısında bir bozulma olabilir: hatalar {top_code} kodunda ve "
            f"{', '.join(services) or 'tek serviste'} yoğunlaşıyor.",
            ("ERROR_CODE_CONCENTRATION", "SERVICE_ERROR_SPIKE"),
        )
    if top_code in {"GATEWAY_504", "HTTP_502"} and "SERVICE_LATENCY_INCREASE" in detected:
        add(
            "Upstream (acquirer) tarafındaki yavaşlama timeout hatalarına yol açıyor olabilir; gecikme ve hata artışı aynı "
            "zaman dilimlerinde görülüyor, ancak bu bir korelasyondur.",
            ("SERVICE_LATENCY_INCREASE", "ERROR_LATENCY_COOCCURRENCE", "ERROR_CODE_CONCENTRATION"),
        )
    if top_code == "HTTP_500" and "ERROR_CODE_CONCENTRATION" in detected:
        add("Uygulama veya deployment kaynaklı bir sunucu hatası olabilir.", ("ERROR_CODE_CONCENTRATION",))
    if "VOLUME_ANOMALY" in detected and detected["VOLUME_ANOMALY"]["subject"] == "düşüş" and "ERROR_RATE_SPIKE" not in detected:
        add(
            "Hata oranı artmadan hacim düştüğü için isteklerin ödeme servislerine hiç ulaşmıyor olması olası "
            "(istemci, DNS/CDN veya yönlendirme). İş kaynaklı bir düşüş de elenmelidir.",
            ("VOLUME_ANOMALY",),
        )
    if "UNKNOWN_ERROR_CODE" in detected:
        add("Sağlayıcı dokümante edilmemiş yeni bir hata kodu üretiyor olabilir.", ("UNKNOWN_ERROR_CODE",))
    if similar and similar["matches"]:
        m = similar["matches"][0]
        status = "doğrulanmış" if m["cause_status"] == "verified" else "doğrulanmamış"
        add(
            f"Benzer geçmiş olay {m['incident_id']} (benzerlik {m['similarity']}) {status} neden olarak "
            f"'{m['cause']}' ile ilişkilendirilmişti. Mevcut olayda aynı nedenin bulunduğu doğrulanmadı.",
            ("analyze_error_patterns",),
            confidence="low",
        )
    if not explanations:
        add("Paternler tespit edildi ancak kural tabanlı bir açıklama eşleşmedi; manuel inceleme gerekir.", ("analyze_error_patterns",))
    return explanations


def build_recommendations(analysis: dict | None, runbooks: dict | None, similar: dict | None,
                          observations: list[Observation]) -> list[Recommendation]:
    first_obs = [observations[0].id] if observations else []
    if not analysis or not analysis["detected_types"]:
        return [Recommendation(text="Belirgin bir anomali tespit edilmedi; olağan izlemeye devam edin.", source="-", evidence_refs=first_obs)]
    recs: list[Recommendation] = []
    for proc in (runbooks or {}).get("procedures", []):
        for step in proc["steps"][:4]:
            recs.append(Recommendation(text=step, source=f"{proc['source']} › {proc['section'].split(' › ')[-1]}"))
    if similar and similar["matches"]:
        m = similar["matches"][0]
        recs.append(Recommendation(
            text=f"Geçmiş olay {m['incident_id']} çözümünü uygulanabilirlik açısından değerlendirin: {m['resolution']}",
            source=f"incidents.json › {m['incident_id']}",
        ))
    if not (runbooks or {}).get("found"):
        recs.append(Recommendation(
            text="İlgili runbook bulunamadı; bulguları kanıtlarıyla birlikte on-call ödeme ekibine eskale edin.",
            source="-", evidence_refs=first_obs,
        ))
    return recs


def build_deviations(comparison: dict | None) -> list[Deviation]:
    if not comparison:
        return []
    cur, base = comparison["current"], comparison["baseline"]
    return [
        Deviation(metric="Hata oranı", current=_pct(cur["error_rate"]), baseline=_pct(base["error_rate"]),
                  change=f"{_signed(comparison['error_rate_delta_pp'])} puan"),
        Deviation(metric="Dakika başı işlem", current=_num(comparison["volume_per_min_current"]),
                  baseline=_num(comparison["volume_per_min_baseline"]), change=_signed(comparison["volume_change_pct"], "%")),
        Deviation(metric="Ortalama gecikme (ms)", current=_num(cur["avg_latency_ms"]), baseline=_num(base["avg_latency_ms"]),
                  change=_signed(comparison["avg_latency_change_pct"], "%")),
        Deviation(metric="p95 gecikme (ms)", current=_num(cur["p95_latency_ms"]), baseline=_num(base["p95_latency_ms"]),
                  change=_signed(comparison["p95_change_pct"], "%")),
        Deviation(metric="Fazladan başarısız işlem", current=str(comparison["excess_failed"]), baseline="0", change="-"),
    ]


def validate_agent_findings(findings: list[str], results: list[dict], scope: dict) -> tuple[list[str], list[str]]:
    """LLM bulgularını sayısal tutarlılık ve nedensellik iddiası açısından süzer."""
    allowed = collect_allowed_numbers(results, extra=[float(scope.get("window_minutes", 0)), float(scope.get("baseline_minutes", 0))])
    accepted, rejected = [], []
    for finding in findings:
        bad = find_unverified_numbers(finding, allowed)
        if bad:
            rejected.append(f"{finding} (doğrulanmayan sayı: {', '.join(bad)})")
        elif CAUSAL_CLAIMS.search(finding):
            rejected.append(f"{finding} (kanıtlanmamış kesin neden iddiası)")
        else:
            accepted.append(finding)
    return accepted, rejected


# ---------- Ana fonksiyon ----------


def build_report(*, scope: dict, results: list[dict], mode: str, data_source: str, assumptions: list[str],
                 limitations: list[str], missing: list[str], validation_log: list[dict],
                 agent_findings: list[str] | None = None) -> IncidentReport:
    comparison = find_result(results, "compare_time_windows", scope)
    analysis = find_result(results, "analyze_error_patterns", scope)
    similar = find_result(results, "search_similar_incidents", scope)
    runbooks = find_result(results, "search_runbooks", scope)
    logs = find_result(results, "search_payment_logs", scope)
    report_call = find_result(results, "generate_incident_report", scope)
    tool_failures = sum(1 for r in results if r.get("error_type") in {"tool", "transient"})

    risk = assess_risk(comparison, analysis, tool_failures=tool_failures, missing_evidence=missing)
    observations = build_observations(comparison, analysis, logs)
    findings = (report_call or {}).get("accepted_findings", []) + list(agent_findings or [])
    accepted, rejected = validate_agent_findings(findings, results, scope)

    limitations = list(limitations)
    if rejected:
        limitations.append(f"Agent'ın {len(rejected)} bulgusu doğrulamadan geçemediği için rapora alınmadı.")
    if missing:
        limitations.append(f"Eksik kanıt: {', '.join(missing)}. İlgili bölümler eksik olabilir.")
    if tool_failures:
        limitations.append(f"{tool_failures} araç çağrısı başarısız oldu; ayrıntılar işlem izlerinde.")
    limitations.append("Veriler sentetiktir; eşikler ve risk ağırlıkları demo amaçlıdır.")
    limitations.append("Zaman korelasyonu nedensellik kanıtı değildir; kök neden doğrulanmamıştır.")

    detected = [Pattern(**p) for p in (analysis or {}).get("patterns", []) if p["detected"]]
    evaluated = [Pattern(**p) for p in (analysis or {}).get("patterns", []) if not p["detected"]]
    affected_services = [s for s in (analysis or {}).get("services", []) if s["service"] in (analysis or {}).get("affected_services", [])]

    report = IncidentReport(
        title=f"Ödeme Anomali İnceleme Raporu: {_hhmm(scope['current_start'])}–{_hhmm(scope['current_end'])} UTC",
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        mode=mode,
        data_source=data_source,
        scope=scope,
        data_coverage={
            "current_records": comparison["current"]["total_attempts"] if comparison else None,
            "baseline_records": comparison["baseline"]["total_attempts"] if comparison else None,
            "buckets": len(analysis["buckets"]) if analysis else 0,
            "tool_calls": len(results),
            "tool_failures": tool_failures,
        },
        assumptions=assumptions,
        risk=risk,
        current=MetricsResult(**comparison["current"]) if comparison else None,
        baseline=MetricsResult(**comparison["baseline"]) if comparison else None,
        comparison=ComparisonResult(**comparison) if comparison else None,
        detected_patterns=detected,
        evaluated_patterns=evaluated,
        deviations=build_deviations(comparison),
        affected_services=affected_services,
        affected_error_codes=[CodeItem(**c) for c in (analysis or {}).get("top_error_codes", [])],
        similar_incidents=(similar or {}).get("matches", []),
        similar_incidents_note=(similar or {}).get("message", "Geçmiş olay karşılaştırması yapılmadı.") + " " + INCIDENT_DISCLAIMER,
        observations=observations,
        possible_explanations=build_explanations(analysis, similar, observations),
        confirmed_root_cause=None,
        recommended_steps=build_recommendations(analysis, runbooks, similar, observations),
        agent_findings=accepted,
        runbook_sources=[f"{h['source']} › {h['section']}" for h in (runbooks or {}).get("hits", [])],
        limitations=limitations,
        validation=list(validation_log),
        disclaimer=DISCLAIMER,
    )

    issues = check_report_consistency(report, results) + check_recommendations_supported(report)
    report.validation.append({"check": "Rapor sayıları araç çıktılarıyla tutarlı ve öneriler kaynaklı", "passed": not issues, "details": issues})
    for item in rejected:
        report.validation.append({"check": "Agent bulgusu reddedildi", "passed": False, "details": [item]})
    return report


# ---------- Markdown ----------


def to_markdown(report: IncidentReport) -> str:
    s = report.scope
    lines = [
        f"# {report.title}",
        "",
        f"_Oluşturulma: {report.generated_at} · Mod: {report.mode} · Veri kaynağı: {report.data_source}_",
        "",
        "## 1. İncelenen dönem ve veri kapsamı",
        f"- İncelenen dönem: {s['current_start']} – {s['current_end']} ({s['window_minutes']} dk)",
        f"- Baseline: {s['baseline_start']} – {s['baseline_end']} ({s['baseline_minutes']} dk)",
        f"- Servis filtresi: {s.get('service') or 'tümü'}",
        f"- Kayıt sayısı: güncel {report.data_coverage['current_records']}, baseline {report.data_coverage['baseline_records']}; "
        f"{report.data_coverage['buckets']} adet 5 dk dilim; {report.data_coverage['tool_calls']} araç çağrısı",
    ]
    lines += [f"- Varsayım: {a}" for a in report.assumptions]

    lines += ["", "## 2. İşlem hacmi ve temel metrikler"]
    if report.current and report.baseline:
        c, b = report.current, report.baseline
        lines += [
            "| Metrik | Güncel | Baseline |", "|---|---|---|",
            f"| Toplam işlem | {c.total_attempts} | {b.total_attempts} |",
            f"| Başarılı / başarısız | {c.successful} / {c.failed} | {b.successful} / {b.failed} |",
            f"| Hata oranı | {_pct(c.error_rate)} | {_pct(b.error_rate)} |",
            f"| Ortalama gecikme (ms) | {_num(c.avg_latency_ms)} | {_num(b.avg_latency_ms)} |",
            f"| p95 gecikme (ms) | {_num(c.p95_latency_ms)} | {_num(b.p95_latency_ms)} |",
        ]
    else:
        lines.append("- Metrikler hesaplanamadı.")

    lines += ["", "## 3. Tespit edilen paternler"]
    lines += [f"- **{PATTERN_LABELS.get(p.type, p.type)}**: {p.evidence} _(kural: {p.rule}; n={p.sample_size})_" for p in report.detected_patterns] or ["- Patern tespit edilmedi."]
    insufficient = [p for p in report.evaluated_patterns if p.insufficient_data]
    lines += [f"- {PATTERN_LABELS.get(p.type, p.type)}: veri yetersiz, değerlendirilemedi." for p in insufficient]

    lines += ["", "## 4. Normal döneme göre sapmalar", "| Metrik | Güncel | Baseline | Değişim |", "|---|---|---|---|"]
    lines += [f"| {d.metric} | {d.current} | {d.baseline} | {d.change} |" for d in report.deviations]

    lines += ["", "## 5. Etkilenen servisler ve hata kodları"]
    lines += [
        f"- {sv['service']}: hata oranı {_pct(sv['baseline_error_rate'])} → {_pct(sv['error_rate'])}, "
        f"p95 {_num(sv['baseline_p95_latency_ms'])} → {_num(sv['p95_latency_ms'])} ms"
        for sv in report.affected_services
    ] or ["- Servis bazlı anomali yok."]
    lines += [f"- {c.key}: {c.count} adet (başarısızların {_pct(c.share)}'i; baseline'da {c.baseline_count})" for c in report.affected_error_codes]

    lines += ["", "## 6. Benzer geçmiş incident kayıtları"]
    lines += [
        f"- **{m['incident_id']}** (benzerlik {m['similarity']}): {m['service']}, neden: {m['cause']} "
        f"[{'doğrulanmış' if m['cause_status'] == 'verified' else 'doğrulanmamış'}]; çözüm: {m['resolution']}"
        for m in report.similar_incidents
    ] or ["- Eşleşme yok."]
    lines.append(f"- _{report.similar_incidents_note}_")

    r = report.risk
    lines += ["", "## 7. Risk seviyesi ve gerekçesi",
              f"- **Risk: {r.level}**" + (f" (skor {r.score}/100)" if r.score is not None else ""),
              f"- **Veri güven düzeyi: {r.confidence}** (risk skorundan ayrı değerlendirilir)"]
    lines += [f"  - {x.factor}: {x.value} → +{x.points} ({x.explanation})" for x in r.reasons]
    lines += [f"  - Güven: {x}" for x in r.confidence_reasons]
    lines.append(f"- _{r.note}_")

    lines += ["", "## 8. Gözlemler (ölçülmüş)"]
    lines += [f"- **{o.id}**: {o.text} _(kaynak: {o.evidence})_" for o in report.observations]
    lines += ["", "## 9. Olası açıklamalar (hipotez, doğrulanmadı)"]
    lines += [f"- {e.text} _(dayanak: {', '.join(e.evidence_refs)}; güven: {e.confidence})_" for e in report.possible_explanations] or ["- Yok."]
    lines += ["", "## 10. Doğrulanmış kök neden", f"- {report.confirmed_root_cause or 'Yok. Kök neden bu otomatik incelemeyle doğrulanamaz.'}"]

    lines += ["", "## 11. İlgili runbook kaynakları"]
    lines += [f"- {src}" for src in report.runbook_sources] or ["- İlgili runbook bulunamadı."]
    lines += ["", "## 12. Önerilen inceleme adımları"]
    lines += [f"{i}. {rec.text} _(kaynak: {rec.source if rec.source != '-' else ', '.join(rec.evidence_refs)})_" for i, rec in enumerate(report.recommended_steps, 1)]

    if report.agent_findings:
        lines += ["", "## 13. Agent bulgu özeti (doğrulamadan geçti)"]
        lines += [f"- {f}" for f in report.agent_findings]

    lines += ["", "## Veri eksikleri ve analiz sınırlamaları"]
    lines += [f"- {x}" for x in report.limitations]
    lines += ["", "## Doğrulama durumu"]
    lines += [f"- {'✅' if v['passed'] else '⚠️'} {v['check']}" + (f": {'; '.join(v['details'])}" if v.get("details") else "") for v in report.validation]
    lines += ["", f"> {report.disclaimer}"]
    return "\n".join(lines)
