"""
Yapılandırılabilir analitik ve risk eşikleri.

Varsayılanlar burada belgelenir; config/thresholds.toml dosyasındaki [detection] ve [risk]
tablolarıyla değiştirilebilir (dosya yoksa varsayılanlar kullanılır).

UYARI: Eşikler sentetik demo için seçilmiştir; üretim için kalibre edilmemiştir.
"""

import tomllib
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

from src.config import THRESHOLDS_PATH


class DetectionThresholds(BaseModel):
    # Kanıt kalitesi (birincil işlem sayısı)
    min_samples_sufficient: int = 200
    min_samples_limited: int = 50
    min_coverage_ratio: float = 0.8
    max_null_rate: float = 0.02

    # A. Teknik hata oranı (error + timeout)
    technical_min_delta_pp: float = 1.0
    technical_min_ratio: float = 2.0
    min_z: float = 3.0
    technical_min_excess: int = 10

    # Red (decline) oranı
    decline_min_delta_pp: float = 2.0
    decline_min_ratio: float = 1.3

    # Timeout oranı
    timeout_min_delta_pp: float = 0.5
    timeout_min_ratio: float = 2.0
    timeout_min_excess: int = 8

    # Gecikme
    latency_min_change_pct: float = 50.0

    # B. Yanıt kodu yoğunlaşması
    code_min_count: int = 15
    code_min_rate_ratio: float = 3.0

    # Segment izolasyonu (Bonferroni düzeltmeli iki taraflı alfa)
    segment_alpha: float = 0.001
    segment_min_samples: int = 30
    segment_min_excess_share: float = 0.6

    # C. Gecikme-timeout korelasyonu
    correlation_min_buckets: int = 8
    correlation_min_rho: float = 0.6
    onset_latency_ratio: float = 1.5
    onset_failure_min_delta: float = 0.02
    onset_sustain_buckets: int = 2

    # D. Reversal
    auto_reversal_max_s: int = 60
    reversal_min_technical: int = 10
    reversal_min_ratio: float = 3.0

    # E. Servis sürümü
    version_min_samples: int = 50
    version_min_ratio: float = 2.0

    # F. Hacim
    volume_drop_pct: float = -30.0
    volume_spike_pct: float = 50.0
    volume_min_z: float = 3.0
    volume_min_buckets: int = 6


class RiskThresholds(BaseModel):
    # (eşik, puan) çiftleri büyükten küçüğe değerlendirilir
    technical_delta_rules: list[tuple[float, int]] = [(4.0, 25), (2.0, 15), (1.0, 8)]
    decline_delta_rules: list[tuple[float, int]] = [(8.0, 20), (2.5, 15)]
    affected_tx_rules: list[tuple[float, int]] = [(150, 20), (40, 12), (15, 5)]
    latency_rules: list[tuple[float, int]] = [(200.0, 15), (50.0, 8)]
    duration_rules: list[tuple[float, int]] = [(30, 10), (15, 5)]
    technical_reversal_rules: list[tuple[float, int]] = [(30, 20), (10, 12)]
    reconciliation_points: dict[str, int] = {"HIGH": 10, "MEDIUM": 5, "LOW": 0}
    segment_rate_rules: list[tuple[float, int]] = [(0.10, 10), (0.05, 5)]  # izole segment içindeki teknik hata oranı
    volume_drop_rules: list[tuple[float, int]] = [(50.0, 25), (30.0, 15)]
    isolated_scope_points: int = 5
    broad_scope_points: int = 10
    high_threshold: int = 60
    medium_threshold: int = 30


class Thresholds(BaseModel):
    detection: DetectionThresholds = DetectionThresholds()
    risk: RiskThresholds = RiskThresholds()


def load_thresholds(path: Path = THRESHOLDS_PATH) -> Thresholds:
    if not path.exists():
        return Thresholds()
    with path.open("rb") as f:
        data = tomllib.load(f)
    return Thresholds.model_validate(data)


@lru_cache(maxsize=1)
def default_thresholds() -> Thresholds:
    return load_thresholds()
