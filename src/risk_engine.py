"""
Açıklanabilir risk skoru ve ondan ayrı tutulan veri güven düzeyi.

- Risk skoru: anomalinin operasyonel etkisi (ne kadar ciddi?).
- Güven düzeyi: bulguların dayandığı verinin yeterliliği (ne kadar emin olabiliriz?).
Bu ikisi birbirine karıştırılmaz; yüksek risk düşük güvenle birlikte raporlanabilir.

UYARI: Ağırlıklar ve eşikler demo amaçlıdır; üretim ortamı için kalibre edilmemiştir.
"""

from src.config import MIN_SAMPLES
from src.schemas import RiskAssessment, RiskReason

ERROR_DELTA_RULES = [(5.0, 25), (2.0, 12)]
ABS_ERROR_RATE_THRESHOLD, ABS_ERROR_RATE_POINTS = 0.10, 10
EXCESS_FAILED_RULES = [(100, 20), (25, 10)]
VOLUME_DROP_RULES = [(50.0, 30), (30.0, 20)]
LATENCY_RULES = [(200.0, 20), (50.0, 10)]
DURATION_RULES = [(20, 10), (10, 5)]
AFFECTED_SERVICE_RULES = [(2, 10), (1, 5)]

HIGH_THRESHOLD = 60
MEDIUM_THRESHOLD = 30
HIGH_CONFIDENCE_SAMPLES = 300
HIGH_CONFIDENCE_BUCKETS = 6

NOTE = "Risk ağırlıkları ve eşikleri demo amaçlıdır; üretim için kalibre edilmemiştir."
CONFIDENCE_LEVELS = ["LOW", "MEDIUM", "HIGH"]


def _points(value: float | None, rules: list[tuple[float, int]]) -> tuple[int, float | None]:
    if value is None:
        return 0, None
    for threshold, points in rules:
        if value >= threshold:
            return points, threshold
    return 0, None


def level_for(score: int) -> str:
    if score >= HIGH_THRESHOLD:
        return "HIGH"
    if score >= MEDIUM_THRESHOLD:
        return "MEDIUM"
    return "LOW"


def assess_confidence(comparison: dict, analysis: dict | None, tool_failures: int, missing_evidence: list[str]) -> tuple[str, list[str]]:
    cur, base = comparison["current"], comparison["baseline"]
    reasons = []
    buckets = len(analysis["buckets"]) if analysis else 0
    if cur["total_attempts"] >= HIGH_CONFIDENCE_SAMPLES and base["total_attempts"] >= HIGH_CONFIDENCE_SAMPLES and buckets >= HIGH_CONFIDENCE_BUCKETS:
        level = 2
        reasons.append(f"Örneklem yeterli (güncel {cur['total_attempts']}, baseline {base['total_attempts']}, {buckets} zaman dilimi).")
    else:
        level = 1
        reasons.append(f"Örneklem sınırlı (güncel {cur['total_attempts']}, baseline {base['total_attempts']}, {buckets} zaman dilimi).")
    if tool_failures:
        level -= 1
        reasons.append(f"{tool_failures} araç çağrısı başarısız oldu.")
    if missing_evidence:
        level -= 1
        reasons.append(f"Eksik kanıt: {', '.join(missing_evidence)}.")
    if analysis and any(p["insufficient_data"] for p in analysis["patterns"]):
        level -= 1
        reasons.append("Bazı paternler veri yetersizliği nedeniyle değerlendirilemedi.")
    return CONFIDENCE_LEVELS[max(0, level)], reasons


def assess_risk(comparison: dict | None, analysis: dict | None, tool_failures: int = 0, missing_evidence: list[str] | None = None) -> RiskAssessment:
    missing_evidence = missing_evidence or []
    if comparison is None:
        return RiskAssessment(level="INDETERMINATE", score=None, reasons=[], confidence="LOW",
                              confidence_reasons=["Karşılaştırma verisi yok."], note=NOTE)
    cur, base = comparison["current"], comparison["baseline"]
    if cur["total_attempts"] < MIN_SAMPLES or base["total_attempts"] < MIN_SAMPLES:
        return RiskAssessment(
            level="INDETERMINATE", score=None, reasons=[], confidence="LOW",
            confidence_reasons=[f"Güvenilir sınıf için her pencerede en az {MIN_SAMPLES} işlem gerekir "
                                f"(güncel {cur['total_attempts']}, baseline {base['total_attempts']})."],
            note=NOTE,
        )

    detected = {p["type"]: p for p in (analysis or {}).get("patterns", []) if p["detected"]}
    reasons: list[RiskReason] = []

    def add(factor: str, value: str, points: int, explanation: str) -> None:
        if points:
            reasons.append(RiskReason(factor=factor, value=value, points=points, explanation=explanation))

    if "ERROR_RATE_SPIKE" in detected or "SERVICE_ERROR_SPIKE" in detected:
        delta = comparison["error_rate_delta_pp"]
        pts, thr = _points(delta, ERROR_DELTA_RULES)
        add("Hata oranı (baseline farkı)", f"{delta:+.2f} puan", pts, f"Baseline'a göre artış {thr} puan eşiğini aşıyor.")
        if cur["error_rate"] is not None and cur["error_rate"] >= ABS_ERROR_RATE_THRESHOLD:
            add("Mutlak hata oranı", f"%{cur['error_rate'] * 100:.2f}", ABS_ERROR_RATE_POINTS,
                f"Hata oranı %{ABS_ERROR_RATE_THRESHOLD * 100:.0f} üzerinde.")
        excess = comparison["excess_failed"]
        pts, thr = _points(excess, EXCESS_FAILED_RULES)
        add("Etkilenen işlem hacmi", f"~{excess} fazladan başarısız işlem", pts,
            f"Baseline oranına göre beklenenden en az {thr} fazla başarısız işlem.")

    if "VOLUME_ANOMALY" in detected and (comparison["volume_change_pct"] or 0) < 0:
        drop = -comparison["volume_change_pct"]
        pts, thr = _points(drop, VOLUME_DROP_RULES)
        add("İşlem hacmi düşüşü", f"-%{drop:.1f}", pts, f"Dakika başı hacim %{thr:.0f}'den fazla düştü; kayıp ödeme riski.")

    latency_changes = [p["statistic"] for p in (analysis or {}).get("patterns", []) if p["type"] == "SERVICE_LATENCY_INCREASE" and p["detected"]]
    if latency_changes:
        top = max(latency_changes)
        pts, thr = _points(top, LATENCY_RULES)
        add("Gecikme artışı", f"+%{top:.1f} (servis p95)", pts, f"Servis p95 gecikmesi %{thr:.0f}'den fazla arttı.")

    if detected and analysis:
        duration = analysis["duration_min"]
        pts, thr = _points(duration, DURATION_RULES)
        suffix = " ve devam ediyor" if analysis["ongoing"] else ""
        add("Anomali süresi", f"{duration} dk{suffix}", pts, f"Anomali en az {thr} dakikadır sürüyor.")
        affected = len(analysis["affected_services"])
        pts, thr = _points(affected, AFFECTED_SERVICE_RULES)
        add("Etkilenen servis sayısı", str(affected), pts, f"{affected} serviste servis bazlı anomali var.")

    score = min(100, sum(r.points for r in reasons))
    confidence, confidence_reasons = assess_confidence(comparison, analysis, tool_failures, missing_evidence)
    return RiskAssessment(level=level_for(score), score=score, reasons=reasons, confidence=confidence,
                          confidence_reasons=confidence_reasons, note=NOTE)
