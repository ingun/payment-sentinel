"""
G. Kanıt kalitesi: örneklem büyüklüğü, baseline kapsamı ve veri kalitesi.

`insufficient` kalitedeki bir dedektör hiçbir zaman `detected=True` dönmez; güçlü iddia üretilmez.
"""

from src.analytics.thresholds import DetectionThresholds

QUALITY_ORDER = ["insufficient", "limited", "sufficient"]


def assess_evidence(cur_n: int, base_n: int, th: DetectionThresholds, coverage_ratio: float | None = None,
                    max_null_rate: float | None = None) -> tuple[str, list[str]]:
    reasons: list[str] = []
    level = 2
    smallest = min(cur_n, base_n)
    if smallest < th.min_samples_limited:
        level = 0
        reasons.append(f"Örneklem çok küçük (güncel {cur_n}, baseline {base_n}; en az {th.min_samples_limited} gerekir).")
    elif smallest < th.min_samples_sufficient:
        level = 1
        reasons.append(f"Örneklem sınırlı (güncel {cur_n}, baseline {base_n}; güçlü sonuç için {th.min_samples_sufficient}).")
    if coverage_ratio is not None and coverage_ratio < th.min_coverage_ratio:
        level = min(level, 0 if coverage_ratio < 0.5 else 1)
        reasons.append(f"Zaman kapsamı eksik (pencerenin %{coverage_ratio * 100:.0f}'i veriyle kapsanıyor).")
    if max_null_rate is not None and max_null_rate > th.max_null_rate:
        level = min(level, 1)
        reasons.append(f"Anahtar alanlarda boş değer oranı yüksek (en fazla %{max_null_rate * 100:.1f}).")
    return QUALITY_ORDER[level], reasons


def weakest(*levels: str) -> str:
    return min(levels, key=QUALITY_ORDER.index) if levels else "insufficient"
