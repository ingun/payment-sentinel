"""
Deterministik metrik hesapları. LLM'e hiçbir sayısal hesap yaptırılmaz.
Girdi olarak veri deposunun döndürdüğü istatistik sözlükleri kullanılır.
"""

import math
from statistics import NormalDist

from src.validation import safe_rate


def pct_change(current: float | None, baseline: float | None) -> float | None:
    """Yüzde değişim; baseline sıfır veya yoksa None döner."""
    if current is None or baseline is None or baseline == 0:
        return None
    return round((current - baseline) / baseline * 100, 1)


def with_rates(stats: dict) -> dict:
    """Depo istatistiğine oranları ekler (paydalar birincil işlem sayısıdır)."""
    total = stats["total"]
    return {
        **stats,
        "approval_rate": safe_rate(stats["approved"], total),
        "decline_rate": safe_rate(stats["declined"], total),
        "technical_rate": safe_rate(stats["technical"], total),
        "timeout_rate": safe_rate(stats["timeouts"], total),
        "reversal_ratio": safe_rate(stats["reversals"], total),
    }


def rate_delta_pp(current: float | None, baseline: float | None) -> float | None:
    if current is None or baseline is None:
        return None
    return round((current - baseline) * 100, 2)


def excess_count(cur_count: int, cur_total: int, base_count: int, base_total: int) -> int:
    """Baseline oranına göre beklenenden fazla olay sayısı (etkilenen işlem tahmini)."""
    if not base_total or not cur_total:
        return 0
    return max(0, round(cur_count - base_count / base_total * cur_total))


def two_proportion_z(fail_a: int, n_a: int, fail_b: int, n_b: int) -> float | None:
    """İki oran arasındaki farkın z istatistiği (a: güncel, b: baseline)."""
    if not n_a or not n_b:
        return None
    pooled = (fail_a + fail_b) / (n_a + n_b)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n_a + 1 / n_b))
    if se == 0:
        return None
    return round((fail_a / n_a - fail_b / n_b) / se, 2)


def bonferroni_z(alpha: float, tests: int) -> float:
    """k test için Bonferroni düzeltmeli tek taraflı z eşiği."""
    return round(NormalDist().inv_cdf(1 - alpha / max(1, tests)), 2)


def mean_std(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return mean, math.sqrt(var)


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(x: list[float], y: list[float]) -> float | None:
    """Spearman sıra korelasyonu; varyans yoksa None."""
    if len(x) != len(y) or len(x) < 3:
        return None
    rx, ry = _ranks(x), _ranks(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if not sx or not sy:
        return None
    return round(cov / (sx * sy), 3)
