"""
Açıklanabilir, kural tabanlı patern ve anomali tespiti.

Her patern; hangi metriğe, hangi kurala ve hangi eşiğe göre tespit edildiğini
ve kaç örneğe dayandığını birlikte döner. Makine öğrenmesi kullanılmaz.

UYARI: Eşikler demo amaçlıdır; üretim için kalibre edilmemiştir.
"""

from src.config import KNOWN_ERROR_CODES, MIN_SAMPLES
from src.metrics import mean_std, pct_change, two_proportion_z
from src.validation import safe_rate

# Hata oranı sıçraması
SPIKE_MIN_DELTA_PP = 2.0
SPIKE_MIN_RATIO = 2.0
SPIKE_MIN_Z = 3.0
SPIKE_MIN_FAILED = 5

# Servis bazlı kurallar
SERVICE_MIN_SAMPLES = 20
SERVICE_SPIKE_MIN_DELTA_PP = 3.0
SERVICE_LATENCY_MIN_CHANGE_PCT = 50.0

# Hacim
VOLUME_DROP_PCT = -30.0
VOLUME_SPIKE_PCT = 50.0
VOLUME_MIN_Z = 3.0
VOLUME_MIN_BASELINE_BUCKETS = 6

# Hata kodu yoğunlaşması
CODE_MIN_SHARE = 0.5
CODE_MIN_COUNT = 10
CODE_MIN_RATE_RATIO = 3.0

# Zaman dilimi (bucket) bazlı kurallar
BUCKET_MIN_SAMPLES = 10
BUCKET_ERROR_RATIO = 2.0
BUCKET_ERROR_MIN_DELTA = 0.03
BUCKET_LATENCY_RATIO = 1.5
BUCKET_VOLUME_LOW_RATIO = 0.6
COOCCURRENCE_MIN_BUCKETS = 2
SUDDEN_ONSET_RATIO = 0.8

THRESHOLD_NOTE = "Patern eşikleri demo amaçlıdır; üretim için kalibre edilmemiştir."


def _pattern(type_: str, detected: bool, metric: str, current, baseline, rule: str, sample_size: int, **extra) -> dict:
    return {
        "type": type_,
        "detected": detected,
        "subject": extra.pop("subject", None),
        "metric": metric,
        "current": current,
        "baseline": baseline,
        "rule": rule,
        "sample_size": sample_size,
        "insufficient_data": extra.pop("insufficient_data", False),
        "statistic": extra.pop("statistic", None),
        "evidence": extra.pop("evidence", ""),
    }


def _pct(rate: float | None) -> str:
    return "-" if rate is None else f"%{rate * 100:.2f}"


def error_rate_spike(cur: dict, base: dict) -> dict:
    rule = f"Δ ≥ {SPIKE_MIN_DELTA_PP} puan, oran ≥ {SPIKE_MIN_RATIO}x, z ≥ {SPIKE_MIN_Z}, başarısız ≥ {SPIKE_MIN_FAILED}"
    if cur["total_attempts"] < MIN_SAMPLES or base["total_attempts"] < MIN_SAMPLES:
        return _pattern("ERROR_RATE_SPIKE", False, "error_rate", cur["error_rate"], base["error_rate"], rule,
                        cur["total_attempts"], insufficient_data=True, evidence="Örneklem yetersiz; değerlendirilmedi.")
    z = two_proportion_z(cur["failed"], cur["total_attempts"], base["failed"], base["total_attempts"])
    delta_pp = (cur["error_rate"] - base["error_rate"]) * 100
    ratio_ok = base["error_rate"] == 0 or cur["error_rate"] / base["error_rate"] >= SPIKE_MIN_RATIO
    detected = (
        delta_pp >= SPIKE_MIN_DELTA_PP and ratio_ok and (z or 0) >= SPIKE_MIN_Z and cur["failed"] >= SPIKE_MIN_FAILED
    )
    return _pattern(
        "ERROR_RATE_SPIKE", detected, "error_rate", cur["error_rate"], base["error_rate"], rule, cur["total_attempts"],
        statistic=z,
        evidence=f"Hata oranı {_pct(base['error_rate'])} → {_pct(cur['error_rate'])} ({delta_pp:+.2f} puan, z={z})",
    )


def volume_anomaly(cur_buckets: list[dict], base_buckets: list[dict], cur_minutes: float, base_minutes: float,
                   cur_total: int, base_total: int) -> dict:
    rule = f"dakika başı hacim değişimi ≤ {VOLUME_DROP_PCT:.0f}% veya ≥ +{VOLUME_SPIKE_PCT:.0f}% ve |z| ≥ {VOLUME_MIN_Z}"
    cur_rate = cur_total / cur_minutes if cur_minutes else 0
    base_rate = base_total / base_minutes if base_minutes else 0
    change = pct_change(cur_rate, base_rate)
    if base_total < MIN_SAMPLES or len(base_buckets) < VOLUME_MIN_BASELINE_BUCKETS or not cur_buckets:
        return _pattern("VOLUME_ANOMALY", False, "volume_per_min", round(cur_rate, 2), round(base_rate, 2), rule,
                        cur_total, insufficient_data=True, evidence="Baseline hacmi yetersiz; değerlendirilmedi.")
    base_mean, base_std = mean_std([b["total"] for b in base_buckets])
    cur_mean, _ = mean_std([b["total"] for b in cur_buckets])
    se = base_std / len(cur_buckets) ** 0.5 if base_std else 0
    z = round((cur_mean - base_mean) / se, 2) if se else None
    direction = "düşüş" if (change or 0) < 0 else "artış"
    detected = change is not None and (change <= VOLUME_DROP_PCT or change >= VOLUME_SPIKE_PCT) and abs(z or 0) >= VOLUME_MIN_Z
    return _pattern(
        "VOLUME_ANOMALY", detected, "volume_per_min", round(cur_rate, 2), round(base_rate, 2), rule, cur_total,
        subject=direction if detected else None, statistic=z,
        evidence=f"Dakika başı işlem {base_rate:.1f} → {cur_rate:.1f} ({change:+.1f}%, z={z})" if change is not None else "",
    )


def code_concentration(cur_codes: list[dict], base_codes: list[dict], cur: dict, base: dict) -> dict:
    rule = f"tek kodun payı ≥ %{CODE_MIN_SHARE * 100:.0f}, adet ≥ {CODE_MIN_COUNT}, trafiğe oranı baseline'ın ≥ {CODE_MIN_RATE_RATIO}x'i"
    if not cur_codes or not cur["failed"]:
        return _pattern("ERROR_CODE_CONCENTRATION", False, "error_code_share", None, None, rule, cur["failed"],
                        evidence="Başarısız işlem yok.")
    top = cur_codes[0]
    share = safe_rate(top["count"], cur["failed"]) or 0
    base_count = next((c["count"] for c in base_codes if c["key"] == top["key"]), 0)
    cur_code_rate = safe_rate(top["count"], cur["total_attempts"]) or 0
    base_code_rate = safe_rate(base_count, base["total_attempts"]) or 0
    rate_ratio = round(cur_code_rate / base_code_rate, 2) if base_code_rate else None
    rate_ok = base_code_rate == 0 or cur_code_rate / base_code_rate >= CODE_MIN_RATE_RATIO
    detected = share >= CODE_MIN_SHARE and top["count"] >= CODE_MIN_COUNT and rate_ok
    return _pattern(
        "ERROR_CODE_CONCENTRATION", detected, "error_code_share", share, safe_rate(base_count, base["failed"]), rule,
        cur["failed"], subject=top["key"], statistic=rate_ratio,
        evidence=(f"{top['key']}: {top['count']} adet, başarısızların %{share * 100:.1f}'i; "
                  f"baseline'da {base_count} adet"),
    )


def unknown_codes(cur_codes: list[dict]) -> dict:
    unknown = [c for c in cur_codes if c["key"] not in KNOWN_ERROR_CODES]
    total = sum(c["count"] for c in unknown)
    return _pattern(
        "UNKNOWN_ERROR_CODE", bool(unknown), "error_code", [c["key"] for c in unknown], None,
        "hata kodu kataloğunda olmayan kod ≥ 1", total,
        subject=", ".join(c["key"] for c in unknown) or None,
        evidence=", ".join(f"{c['key']} ({c['count']})" for c in unknown) or "Bilinmeyen kod yok.",
    )


def service_signals(cur_services: list[dict], base_services: list[dict]) -> tuple[list[dict], list[dict]]:
    """Servis bazlı gecikme ve hata artışı paternleri ile servis karşılaştırma tablosu."""
    base_by_key = {s["key"]: s for s in base_services}
    patterns, table = [], []
    for svc in cur_services:
        base = base_by_key.get(svc["key"])
        cur_rate = safe_rate(svc["failed"], svc["total"])
        base_rate = safe_rate(base["failed"], base["total"]) if base else None
        p95_change = pct_change(svc["p95_latency_ms"], base["p95_latency_ms"]) if base else None
        row = {
            "service": svc["key"], "total": svc["total"], "failed": svc["failed"],
            "error_rate": cur_rate, "baseline_error_rate": base_rate,
            "p95_latency_ms": svc["p95_latency_ms"], "baseline_p95_latency_ms": base["p95_latency_ms"] if base else None,
            "p95_change_pct": p95_change,
        }
        table.append(row)
        enough = svc["total"] >= SERVICE_MIN_SAMPLES and base and base["total"] >= SERVICE_MIN_SAMPLES
        if not enough:
            continue
        if p95_change is not None and p95_change >= SERVICE_LATENCY_MIN_CHANGE_PCT:
            patterns.append(_pattern(
                "SERVICE_LATENCY_INCREASE", True, "p95_latency_ms", svc["p95_latency_ms"], base["p95_latency_ms"],
                f"servis p95 artışı ≥ %{SERVICE_LATENCY_MIN_CHANGE_PCT:.0f} (n ≥ {SERVICE_MIN_SAMPLES})", svc["total"],
                subject=svc["key"], statistic=p95_change,
                evidence=f"{svc['key']} p95 {base['p95_latency_ms']} → {svc['p95_latency_ms']} ms ({p95_change:+.1f}%)",
            ))
        z = two_proportion_z(svc["failed"], svc["total"], base["failed"], base["total"])
        delta_pp = ((cur_rate or 0) - (base_rate or 0)) * 100
        ratio_ok = not base_rate or (cur_rate or 0) / base_rate >= SPIKE_MIN_RATIO
        if delta_pp >= SERVICE_SPIKE_MIN_DELTA_PP and ratio_ok and (z or 0) >= SPIKE_MIN_Z and svc["failed"] >= SPIKE_MIN_FAILED:
            patterns.append(_pattern(
                "SERVICE_ERROR_SPIKE", True, "error_rate", cur_rate, base_rate,
                f"servis hata oranı Δ ≥ {SERVICE_SPIKE_MIN_DELTA_PP} puan, z ≥ {SPIKE_MIN_Z}", svc["total"],
                subject=svc["key"], statistic=z,
                evidence=f"{svc['key']} hata oranı {_pct(base_rate)} → {_pct(cur_rate)} ({delta_pp:+.2f} puan, z={z})",
            ))
    return patterns, table


def bucket_flags(cur_buckets: list[dict], base: dict, base_bucket_mean: float) -> list[dict]:
    base_rate = base["error_rate"] or 0
    base_p95 = base["p95_latency_ms"]
    flags = []
    for b in cur_buckets:
        rate = safe_rate(b["failed"], b["total"])
        enough = b["total"] >= BUCKET_MIN_SAMPLES
        err = enough and rate is not None and rate >= max(base_rate * BUCKET_ERROR_RATIO, base_rate + BUCKET_ERROR_MIN_DELTA)
        lat = enough and base_p95 and b["p95_latency_ms"] is not None and b["p95_latency_ms"] >= base_p95 * BUCKET_LATENCY_RATIO
        vol = base_bucket_mean > 0 and b["total"] <= base_bucket_mean * BUCKET_VOLUME_LOW_RATIO
        flags.append({**b, "error_rate": rate, "error_anomaly": bool(err), "latency_anomaly": bool(lat), "volume_anomaly": bool(vol)})
    return flags


def cooccurrence(flags: list[dict]) -> dict:
    both = [f["bucket_start"] for f in flags if f["error_anomaly"] and f["latency_anomaly"]]
    detected = len(both) >= COOCCURRENCE_MIN_BUCKETS
    return _pattern(
        "ERROR_LATENCY_COOCCURRENCE", detected, "bucket_overlap", len(both), None,
        f"hata ve p95 anomalisinin aynı 5 dk dilimde görülmesi ≥ {COOCCURRENCE_MIN_BUCKETS} dilim",
        len(flags), evidence=(f"Örtüşen dilimler: {', '.join(t[11:16] for t in both)}. Bu bir zaman korelasyonudur, "
                              "nedensellik kanıtı değildir.") if both else "Örtüşen dilim yok.",
    )


def timeline(flags: list[dict], interval_min: int) -> dict:
    """En son kesintisiz anomali dizisinin başlangıcı, süresi ve şekli."""
    anomalous = [f["error_anomaly"] or f["latency_anomaly"] or f["volume_anomaly"] for f in flags]
    runs, start = [], None
    for i, flag in enumerate(anomalous + [False]):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            runs.append((start, i))
            start = None
    if not runs:
        return {"onset_time": None, "duration_min": 0, "ongoing": False, "onset_shape": None, "anomalous_buckets": []}
    lo, hi = max(runs, key=lambda r: (r[1] - r[0], r[0]))
    run = flags[lo:hi]
    metric = "error_rate" if any(f["error_anomaly"] for f in run) else "p95_latency_ms" if any(f["latency_anomaly"] for f in run) else "total"
    values = [f[metric] or 0 for f in run]
    # İlk iki dilimde zirveye (hacimde dibe) yaklaşıldıysa ani, değilse kademeli kabul edilir
    if metric == "total":
        shape = "sudden" if min(values[:2]) <= min(values) / SUDDEN_ONSET_RATIO else "gradual"
    else:
        shape = "sudden" if max(values[:2]) >= max(values) * SUDDEN_ONSET_RATIO else "gradual"
    return {
        "onset_time": run[0]["bucket_start"],
        "duration_min": (hi - lo) * interval_min,
        "ongoing": hi == len(flags),
        "onset_shape": shape,
        "anomalous_buckets": [f["bucket_start"] for f in run],
    }


def analyze_patterns(*, cur: dict, base: dict, cur_buckets: list[dict], base_buckets: list[dict],
                     cur_services: list[dict], base_services: list[dict], cur_codes: list[dict],
                     base_codes: list[dict], cur_minutes: float, base_minutes: float, interval_min: int) -> dict:
    base_bucket_mean, _ = mean_std([b["total"] for b in base_buckets])
    service_patterns, service_table = service_signals(cur_services, base_services)
    flags = bucket_flags(cur_buckets, base, base_bucket_mean)
    patterns = [
        error_rate_spike(cur, base),
        volume_anomaly(cur_buckets, base_buckets, cur_minutes, base_minutes, cur["total_attempts"], base["total_attempts"]),
        code_concentration(cur_codes, base_codes, cur, base),
        unknown_codes(cur_codes),
        *service_patterns,
        cooccurrence(flags),
    ]
    detected = [p for p in patterns if p["detected"]]
    affected = sorted({p["subject"] for p in detected if p["type"].startswith("SERVICE_")})
    # Tek tek dilimlerdeki dalgalanma, pencere düzeyinde patern yoksa anomali sayılmaz
    events = timeline(flags, interval_min) if detected else timeline([], interval_min)
    return {
        "patterns": patterns,
        "detected_types": sorted({p["type"] for p in detected}),
        "affected_services": affected,
        "services": service_table,
        "buckets": flags,
        **events,
        "threshold_note": THRESHOLD_NOTE,
    }
