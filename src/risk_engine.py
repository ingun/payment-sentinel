"""
Deterministik etki ve risk değerlendirmesi (LLM'den bağımsız).

- Skor (0-100): tespit edilen sinyallerin operasyonel etkisine göre puan toplamı. Formül ve eşikler
  src/analytics/thresholds.py içindeki RiskThresholds ile yapılandırılır.
- Kanıt kalitesi seviyeyi sınırlar: `limited` → en fazla MEDIUM, `insufficient` → INDETERMINATE.
- Güven düzeyi ayrıca raporlanır (araç hataları ve eksik kanıt düşürür); skorla karıştırılmaz.
- Anomali skoru parasal kayıp değildir. Veride tutar alanı olmadığı için parasal etki hesaplanmaz.

LLM skoru göremez, değiştiremez; yalnızca rapordaki gerekçeyi açıklayabilir.
"""

from src.analytics.quality import weakest
from src.analytics.thresholds import RiskThresholds, default_thresholds
from src.policy import detected
from src.schemas import RiskAssessment, RiskFactor
from src.validation import find_all, find_baseline, find_coverage, find_result

MONETARY_NOTE = ("Belirlenemedi: veri setinde güvenilir tutar alanı ve iş kuralı yok. Etki yalnızca işlem sayısı "
                 "olarak raporlanır; anomali skoru parasal kayıp değildir.")
NOTE = "Risk ağırlıkları ve eşikleri demo amaçlıdır; üretim için kalibre edilmemiştir."
CONFIDENCE_LEVELS = ["LOW", "MEDIUM", "HIGH"]


def formula(rt: RiskThresholds) -> str:
    def fmt(rules):
        return " / ".join(f"≥{t}: +{p}" for t, p in rules)

    return (f"skor = min(100, Σ puan). Teknik hata Δ puan [{fmt(rt.technical_delta_rules)}]; red Δ puan "
            f"[{fmt(rt.decline_delta_rules)}]; etkilenen işlem [{fmt(rt.affected_tx_rules)}]; p95 değişim % "
            f"[{fmt(rt.latency_rules)}]; süre dk [{fmt(rt.duration_rules)}]; teknik reversal [{fmt(rt.technical_reversal_rules)}]; "
            f"mutabakat riski göstergesi [{', '.join(f'{k}: +{v}' for k, v in rt.reconciliation_points.items())}]; "
            f"hacim düşüşü % [{fmt(rt.volume_drop_rules)}]; izole segment içi teknik hata oranı [{fmt(rt.segment_rate_rules)}]; "
            f"izole kapsam +{rt.isolated_scope_points}, yaygın kapsam +{rt.broad_scope_points}. "
            f"Seviye: ≥{rt.high_threshold} HIGH, ≥{rt.medium_threshold} MEDIUM, aksi LOW.")


def _points(value: float | None, rules: list[tuple[float, int]]) -> tuple[int, float | None]:
    if value is None:
        return 0, None
    for threshold, points in rules:
        if value >= threshold:
            return points, threshold
    return 0, None


def level_for(score: int, rt: RiskThresholds) -> str:
    if score >= rt.high_threshold:
        return "HIGH"
    if score >= rt.medium_threshold:
        return "MEDIUM"
    return "LOW"


def affected_scope(results: list[dict]) -> tuple[dict, str | None]:
    """İzole segmentler ve kapsam tipi ('isolated' | 'broad' | None)."""
    scope: dict[str, str] = {}
    broad = False
    for out in find_all(results, "breakdown_by_dimension") + [o["isolation"] for o in find_all(results, "analyze_response_codes") if o.get("isolation")]:
        if out["isolated"]:
            scope.setdefault(out["dimension"], out["isolated_segment"])
        elif len(out.get("significant", [])) > 1:
            broad = True
    for out in find_all(results, "compare_service_versions"):
        if out["detection"]["detected"]:
            scope.setdefault("service_version", f"{out['service']} {out['detection']['subject']}")
    kind = "isolated" if scope else ("broad" if broad else None)
    return scope, kind


def assess_risk(results: list[dict], scope: dict, tool_failures: int = 0, missing: list[str] | None = None,
                rt: RiskThresholds | None = None) -> RiskAssessment:
    rt = rt or default_thresholds().risk
    missing = missing or []

    baseline = find_baseline(results, scope)
    coverage = find_coverage(results, scope)
    base_kw = {"formula": formula(rt), "note": NOTE, "monetary_impact": MONETARY_NOTE}
    if baseline is None:
        return RiskAssessment(severity="INDETERMINATE", score=None, factors=[], affected_scope={}, affected_transactions=0,
                              evidence_quality="insufficient", evidence_limitations=["Baseline karşılaştırması yok."],
                              confidence="LOW", confidence_reasons=["Karşılaştırma verisi alınamadı."], **base_kw)

    tech = next(d for d in baseline["detections"] if d["detector"] == "TECHNICAL_FAILURE_SPIKE")
    quality = weakest(tech["evidence_quality"], coverage["evidence_quality"] if coverage else tech["evidence_quality"])
    limitations = list(dict.fromkeys(tech["limitations"] + (coverage["issues"] if coverage else [])))
    found = detected(results)
    names = {d["detector"] for d in found}
    seg_scope, scope_kind = affected_scope(results)

    if quality == "insufficient":
        return RiskAssessment(severity="INDETERMINATE", score=None, factors=[], affected_scope=seg_scope,
                              affected_transactions=0, evidence_quality=quality,
                              evidence_limitations=limitations or ["Kanıt yetersiz."], confidence="LOW",
                              confidence_reasons=["Örneklem veya veri kapsamı güçlü bir sonuç için yetersiz."], **base_kw)

    factors: list[RiskFactor] = []

    def add(factor: str, value: str, points: int, explanation: str) -> None:
        if points:
            factors.append(RiskFactor(factor=factor, value=value, points=points, explanation=explanation))

    deltas, excess = baseline["deltas"], baseline["excess"]
    affected = 0
    if "TECHNICAL_FAILURE_SPIKE" in names or "TIMEOUT_RATE_SPIKE" in names:
        pts, thr = _points(deltas["technical_pp"], rt.technical_delta_rules)
        add("Teknik hata oranı farkı", f"{deltas['technical_pp']:+.2f} puan", pts, f"Baseline'a göre artış {thr} puan eşiğini aşıyor.")
        affected += excess["technical"]
    if "DECLINE_RATE_SPIKE" in names:
        pts, thr = _points(deltas["decline_pp"], rt.decline_delta_rules)
        add("Red oranı farkı", f"{deltas['decline_pp']:+.2f} puan", pts, f"Red oranı baseline'a göre {thr} puandan fazla arttı.")
        affected += excess["declined"]
    # İnceleme kapsamıyla eşleşen reversal analizi tercih edilir; yalnızca daha dar (takip) analiz varsa o kullanılır.
    # Böylece skor, agent'ların çalışma sırasına bağlı olmaz.
    reversal = next((o for o in reversed(find_all(results, "analyze_reversals"))
                     if o.get("filters", {}) == scope.get("filters", {})), None) or find_result(results, "analyze_reversals")
    if reversal and reversal["detection"]["detected"]:
        technical = reversal["detection"]["extra"]["technical"]
        pts, thr = _points(technical, rt.technical_reversal_rules)
        add("Teknik reversal", f"{technical} adet", pts, f"Onay sonrası otomatik veya orphan reversal sayısı {thr} eşiğini aşıyor.")
        affected += technical
    if reversal:
        rr = reversal["reconciliation_risk"]
        add("Mutabakat riski göstergesi", rr["level"], rt.reconciliation_points.get(rr["level"], 0),
            "Onay sonrası otomatik reversal, orphan reversal ve reversal'sız timeout sayılarından türetilir.")
    volume = next((d for d in found if d["detector"] == "VOLUME_ANOMALY" and (d["rel_deviation_pct"] or 0) < 0), None)
    if volume:
        drop = -volume["rel_deviation_pct"]
        pts, thr = _points(drop, rt.volume_drop_rules)
        add("Hacim düşüşü", f"-%{drop:.1f}", pts, f"Hacim referansa göre %{thr:.0f}'den fazla düştü.")
        affected += volume["extra"].get("missing_transactions", 0)  # işlenmeyen işlem tahmini (referansa göre)
    if affected:
        pts, thr = _points(affected, rt.affected_tx_rules)
        add("Etkilenen işlem", f"~{affected} işlem", pts,
            f"Baseline'a göre beklenenden en az {thr} fazla hatalı, reddedilmiş, geri alınmış veya işlenmemiş işlem.")
    if "LATENCY_INCREASE" in names:
        change = max((d["statistic"] or 0) for d in found if d["detector"] == "LATENCY_INCREASE")
        pts, thr = _points(change, rt.latency_rules)
        add("Gecikme artışı", f"p95 +%{change:.1f}", pts, f"p95 işlem süresi %{thr:.0f}'den fazla arttı.")
    durations = [t["duration_min"] for t in baseline.get("timeline", {}).values()]
    if found and durations:
        duration = max(durations)
        pts, thr = _points(duration, rt.duration_rules)
        add("Süre", f"{duration} dk", pts, f"Anomali en az {thr} dakikadır sürüyor.")
    segment_rates = [(o["isolated_segment"], o["segments"][0]["rate"]) for o in find_all(results, "breakdown_by_dimension")
                     if o["isolated"] and o["metric"] == "technical_rate" and o["segments"] and o["segments"][0]["segment"] == o["isolated_segment"]]
    if segment_rates:
        segment, rate = max(segment_rates, key=lambda x: x[1] or 0)
        pts, thr = _points(rate, rt.segment_rate_rules)
        add("Segment içi teknik hata oranı", f"{segment}: %{(rate or 0) * 100:.2f}", pts,
            f"Etkilenen segmentte işlemlerin en az %{(thr or 0) * 100:.0f}'i teknik hatayla sonuçlanıyor.")
    if found and scope_kind == "isolated":
        add("Etkilenen kapsam", ", ".join(f"{k}={v}" for k, v in seg_scope.items()), rt.isolated_scope_points,
            "Etki belirli bir segmentle sınırlı.")
    elif found and scope_kind == "broad":
        add("Etkilenen kapsam", "birden fazla segment", rt.broad_scope_points, "Etki birden fazla segmente yayılmış.")

    score = min(100, sum(f.points for f in factors))
    severity = level_for(score, rt)
    capped = None
    if quality == "limited" and severity == "HIGH":
        severity, capped = "MEDIUM", "Kanıt kalitesi sınırlı olduğu için seviye MEDIUM ile sınırlandı."

    conf = 2 if quality == "sufficient" else 1
    reasons = [f"Kanıt kalitesi: {quality}."]
    if tool_failures:
        conf -= 1
        reasons.append(f"{tool_failures} araç çağrısı başarısız oldu.")
    if missing:
        conf -= 1
        reasons.append(f"Eksik kanıt: {', '.join(missing)}.")
    return RiskAssessment(
        severity=severity, score=score, factors=factors, affected_scope=seg_scope, affected_transactions=affected,
        evidence_quality=quality, evidence_limitations=limitations, confidence=CONFIDENCE_LEVELS[max(0, conf)],
        confidence_reasons=reasons, capped=capped, **base_kw,
    )
