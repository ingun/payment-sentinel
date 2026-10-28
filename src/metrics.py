"""
Deterministik metrik hesapları. LLM'e hiçbir sayısal hesap yaptırılmaz.
Girdi olarak veri kaynağının döndürdüğü özet sözlükleri kullanılır.
"""

import math

from src.validation import has_sufficient_samples, safe_rate


def pct_change(current: float | None, baseline: float | None) -> float | None:
    """Yüzde değişim; baseline sıfır veya yoksa None döner."""
    if current is None or baseline is None or baseline == 0:
        return None
    return round((current - baseline) / baseline * 100, 1)


def metrics_from_summary(summary: dict) -> dict:
    total, failed = summary["total"], summary["failed"]
    return {
        "total_attempts": total,
        "successful": total - failed,
        "failed": failed,
        "error_rate": safe_rate(failed, total),
        "avg_latency_ms": summary["avg_latency_ms"],
        "p95_latency_ms": summary["p95_latency_ms"],
        "sufficient_data": has_sufficient_samples(total),
    }


def compare_metrics(current: dict, baseline: dict, current_minutes: float, baseline_minutes: float) -> dict:
    cur_rate, base_rate = current["error_rate"], baseline["error_rate"]
    delta = round((cur_rate - base_rate) * 100, 2) if cur_rate is not None and base_rate is not None else None
    ratio = round(cur_rate / base_rate, 2) if cur_rate is not None and base_rate else None
    cur_per_min = current["total_attempts"] / current_minutes if current_minutes else None
    base_per_min = baseline["total_attempts"] / baseline_minutes if baseline_minutes else None
    return {
        "error_rate_delta_pp": delta,
        "error_rate_ratio": ratio,
        "p95_change_pct": pct_change(current["p95_latency_ms"], baseline["p95_latency_ms"]),
        "avg_latency_change_pct": pct_change(current["avg_latency_ms"], baseline["avg_latency_ms"]),
        "volume_per_min_current": round(cur_per_min, 2) if cur_per_min is not None else None,
        "volume_per_min_baseline": round(base_per_min, 2) if base_per_min is not None else None,
        "volume_change_pct": pct_change(cur_per_min, base_per_min),
        "excess_failed": excess_failures(current, baseline),
    }


def excess_failures(current: dict, baseline: dict) -> int:
    """Baseline hata oranına göre beklenenden fazla başarısız işlem sayısı (etkilenen işlem tahmini)."""
    if baseline["error_rate"] is None or current["total_attempts"] == 0:
        return 0
    expected = baseline["error_rate"] * current["total_attempts"]
    return max(0, round(current["failed"] - expected))


def two_proportion_z(fail_a: int, n_a: int, fail_b: int, n_b: int) -> float | None:
    """İki oran arasındaki farkın z istatistiği (a: güncel, b: baseline)."""
    if not n_a or not n_b:
        return None
    pooled = (fail_a + fail_b) / (n_a + n_b)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n_a + 1 / n_b))
    if se == 0:
        return None
    return round((fail_a / n_a - fail_b / n_b) / se, 2)


def mean_std(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return mean, math.sqrt(var)
