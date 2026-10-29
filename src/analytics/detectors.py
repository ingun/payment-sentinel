"""
Ödeme operasyonuna özgü deterministik dedektörler (LLM kullanılmaz).

Her dedektör aynı yapıda bir tespit (Detection) sözlüğü döner: metrik, gözlenen ve baseline değer,
mutlak ve göreli sapma, zaman penceresi, kapsam, örneklem, kanıt kalitesi, yöntem ve sınırlamalar.
Girdiler veri deposunun toplulaştırılmış istatistikleridir (ham kayıt değil).

UYARI: Eşikler demo amaçlıdır; üretim için kalibre edilmemiştir.
"""

from dataclasses import dataclass, field

from src.analytics.quality import assess_evidence
from src.analytics.thresholds import DetectionThresholds
from src.domain import describe_code, status_for
from src.metrics import bonferroni_z, excess_count, mean_std, pct_change, rate_delta_pp, spearman, two_proportion_z
from src.validation import safe_rate

CORRELATION_NOTE = "Korelasyon ve zamansal öncelik nedensellik kanıtı değildir."
HYPOTHESIS_NOTE = "Sürüm/deployment ilişkisi bir hipotezdir; değişiklik kayıtlarıyla doğrulanmalıdır."


@dataclass
class Ctx:
    window: dict
    baseline_window: dict | None = None
    scope: dict = field(default_factory=dict)


def _pct(rate: float | None) -> str:
    return "-" if rate is None else f"%{rate * 100:.2f}"


def detection(detector: str, ctx: Ctx, *, detected: bool, metric: str, observed, baseline, sample_current: int,
              sample_baseline: int | None, quality: str, method: str, evidence: str, unit: str = "",
              statistic: float | None = None, subject: str | None = None, limitations: list[str] | None = None,
              claim_type: str = "observation", scope_extra: dict | None = None, extra: dict | None = None) -> dict:
    abs_dev = rel_dev = None
    if isinstance(observed, (int, float)) and isinstance(baseline, (int, float)):
        abs_dev = round(observed - baseline, 4)
        rel_dev = pct_change(observed, baseline)
    return {
        "detector": detector,
        "detected": bool(detected) and quality != "insufficient",
        "subject": subject,
        "metric": metric,
        "unit": unit,
        "observed": observed,
        "baseline": baseline,
        "abs_deviation": abs_dev,
        "rel_deviation_pct": rel_dev,
        "window": ctx.window,
        "baseline_window": ctx.baseline_window,
        "scope": {**ctx.scope, **(scope_extra or {})},
        "sample_size": {"current": sample_current, "baseline": sample_baseline},
        "evidence_quality": quality,
        "insufficient_data": quality == "insufficient",
        "statistic": statistic,
        "method": method,
        "limitations": list(limitations or []),
        "claim_type": claim_type,
        "evidence": evidence,
        "extra": extra or {},
    }


# ---------- A. Oran sıçramaları (teknik hata, red, timeout) ----------

RATE_SPECS = {
    # dedektör: (sayı alanı, oran alanı, etiket)
    "TECHNICAL_FAILURE_SPIKE": ("technical", "technical_rate", "Teknik hata oranı (error+timeout)"),
    "DECLINE_RATE_SPIKE": ("declined", "decline_rate", "Red (decline) oranı"),
    "TIMEOUT_RATE_SPIKE": ("timeouts", "timeout_rate", "Timeout oranı"),
}


def rate_spike(detector: str, cur: dict, base: dict, ctx: Ctx, th: DetectionThresholds) -> dict:
    count_key, rate_key, label = RATE_SPECS[detector]
    if detector == "TECHNICAL_FAILURE_SPIKE":
        min_delta, min_ratio, min_excess = th.technical_min_delta_pp, th.technical_min_ratio, th.technical_min_excess
    elif detector == "DECLINE_RATE_SPIKE":
        min_delta, min_ratio, min_excess = th.decline_min_delta_pp, th.decline_min_ratio, th.technical_min_excess
    else:
        min_delta, min_ratio, min_excess = th.timeout_min_delta_pp, th.timeout_min_ratio, th.timeout_min_excess
    quality, reasons = assess_evidence(cur["total"], base["total"], th)
    z = two_proportion_z(cur[count_key], cur["total"], base[count_key], base["total"])
    delta = rate_delta_pp(cur[rate_key], base[rate_key])
    ratio_ok = not base[rate_key] or (cur[rate_key] or 0) / base[rate_key] >= min_ratio
    excess = excess_count(cur[count_key], cur["total"], base[count_key], base["total"])
    detected = delta is not None and delta >= min_delta and ratio_ok and (z or 0) >= th.min_z and excess >= min_excess
    rule = f"Δ ≥ {min_delta} puan, oran ≥ {min_ratio}x, z ≥ {th.min_z}, fazladan ≥ {min_excess}"
    return detection(
        detector, ctx, detected=detected, metric=rate_key, observed=cur[rate_key], baseline=base[rate_key],
        sample_current=cur["total"], sample_baseline=base["total"], quality=quality, statistic=z, unit="oran",
        method=f"İki oran z-testi (güncel vs baseline). Kural: {rule}",
        limitations=reasons,
        evidence=(f"{label} {_pct(base[rate_key])} → {_pct(cur[rate_key])} ({delta:+.2f} puan, z={z}); "
                  f"{cur[count_key]} / {cur['total']} işlem, baseline'a göre ~{excess} fazladan")
        if delta is not None else f"{label} hesaplanamadı (işlem yok).",
        extra={"excess": excess, "delta_pp": delta, "count": cur[count_key]},
    )


def onset_duration(buckets: list[dict], base: dict, detector: str, th: DetectionThresholds, interval_min: int) -> dict:
    """Oran sıçramasının başladığı dilim ve pencere sonuna kadarki süre (sürekli eşik aşımı)."""
    count_key, rate_key, _ = RATE_SPECS[detector]
    min_delta = {"TECHNICAL_FAILURE_SPIKE": th.technical_min_delta_pp, "DECLINE_RATE_SPIKE": th.decline_min_delta_pp,
                 "TIMEOUT_RATE_SPIKE": th.timeout_min_delta_pp}[detector] / 100
    series = [safe_rate(b[count_key], b["total"]) if b["total"] >= 10 else None for b in buckets]
    i = _onset(series, (base[rate_key] or 0) + min_delta, th.onset_sustain_buckets)
    if i is None:
        return {"onset": None, "duration_min": 0}
    return {"onset": buckets[i]["bucket_start"], "duration_min": (len(buckets) - i) * interval_min}


def latency_increase(cur: dict, base: dict, ctx: Ctx, th: DetectionThresholds) -> dict:
    quality, reasons = assess_evidence(cur["total"] - cur["timeouts"], base["total"] - base["timeouts"], th)
    change = pct_change(cur["p95_ms"], base["p95_ms"])
    detected = change is not None and change >= th.latency_min_change_pct
    return detection(
        "LATENCY_INCREASE", ctx, detected=detected, metric="p95_ms", observed=cur["p95_ms"], baseline=base["p95_ms"],
        sample_current=cur["total"] - cur["timeouts"], sample_baseline=base["total"] - base["timeouts"],
        quality=quality, statistic=change, unit="ms",
        method=f"p95 işlem süresi değişimi ≥ %{th.latency_min_change_pct:.0f} (timeout'lar hariç)",
        limitations=reasons + ["Yüzdelik farkı için anlamlılık testi yapılmaz; örneklem kalitesine dayanır."],
        evidence=(f"p95 işlem süresi {base['p95_ms']} → {cur['p95_ms']} ms ({change:+.1f}%); "
                  f"p50 {base['p50_ms']} → {cur['p50_ms']} ms") if change is not None else "Gecikme karşılaştırılamadı.",
        extra={"p50_ms": cur["p50_ms"], "p99_ms": cur["p99_ms"], "baseline_p50_ms": base["p50_ms"]},
    )


# ---------- F. Hacim (gün içi sezonsallık için D-1 aynı pencere) ----------


def volume_anomaly(cur_buckets: list[dict], ref_buckets: list[dict], ctx: Ctx, th: DetectionThresholds,
                   method: str) -> dict:
    cur_total = sum(b["total"] for b in cur_buckets)
    ref_total = sum(b["total"] for b in ref_buckets)
    cur_mean, _ = mean_std([b["total"] for b in cur_buckets])
    ref_mean, ref_std = mean_std([b["total"] for b in ref_buckets])
    change = pct_change(cur_mean, ref_mean)
    quality, reasons = assess_evidence(cur_total, ref_total, th)
    limitations = list(reasons)
    if method == "trailing":
        limitations.append("D-1 aynı pencere verisi yok; önceki pencereyle karşılaştırıldı, gün içi sezonsallık hesaba katılmadı.")
    if len(ref_buckets) < th.volume_min_buckets or not cur_buckets:
        quality = "insufficient"
        limitations.append("Hacim karşılaştırması için yeterli zaman dilimi yok.")
    se = ref_std / len(cur_buckets) ** 0.5 if ref_std and cur_buckets else 0
    z = round((cur_mean - ref_mean) / se, 2) if se else None
    detected = change is not None and (change <= th.volume_drop_pct or change >= th.volume_spike_pct) and abs(z or 0) >= th.volume_min_z
    direction = "düşüş" if (change or 0) < 0 else "artış"
    # Etki tahmini: referans dilim ortalamasına göre işlenmemiş işlem sayısı (yalnızca düşüşte)
    missing = max(0, round(ref_mean * len(cur_buckets) - cur_total)) if (change or 0) < 0 else 0
    label = "dünün aynı penceresi" if method == "day_over_day" else "önceki pencere"
    return detection(
        "VOLUME_ANOMALY", ctx, detected=detected, metric="tx_per_bucket", observed=round(cur_mean, 2),
        baseline=round(ref_mean, 2), sample_current=cur_total, sample_baseline=ref_total, quality=quality,
        statistic=z, subject=direction if detected else None, unit="işlem/dilim",
        method=f"Dilim başı hacim, referans: {label}. Kural: değişim ≤ {th.volume_drop_pct:.0f}% veya ≥ +{th.volume_spike_pct:.0f}% ve |z| ≥ {th.volume_min_z}",
        limitations=limitations,
        evidence=(f"Dilim başı işlem {ref_mean:.1f} ({label}) → {cur_mean:.1f} ({change:+.1f}%, z={z})"
                  + (f"; referansa göre tahmini ~{missing} işlem eksik" if detected and missing else "")
                  if change is not None else "Hacim karşılaştırılamadı."),
        extra={"reference": method, "change_pct": change, "missing_transactions": missing},
    )


# ---------- B. Yanıt kodu yoğunlaşması ----------


def response_codes(cur_codes: list[dict], base_codes: list[dict], cur_total: int, base_total: int, ctx: Ctx,
                   th: DetectionThresholds) -> tuple[list[dict], list[dict]]:
    """Kod tablosu ve her olağandışı kod için bir tespit döner (onay kodu 00 hariç)."""
    base_by = {c["key"]: c["count"] for c in base_codes}
    quality, reasons = assess_evidence(cur_total, base_total, th)
    rows, detections = [], []
    for c in cur_codes:
        code = c["key"]
        if code == "00":
            continue
        base_count = base_by.get(code, 0)
        rate, base_rate = safe_rate(c["count"], cur_total), safe_rate(base_count, base_total)
        ratio = round(rate / base_rate, 2) if rate is not None and base_rate else None
        z = two_proportion_z(c["count"], cur_total, base_count, base_total)
        delta = rate_delta_pp(rate, base_rate) or 0.0
        unusual = (c["count"] >= th.code_min_count and (z or 0) >= th.min_z
                   and (ratio is None or ratio >= th.code_min_rate_ratio or delta >= th.decline_min_delta_pp))
        row = {"code": code, "description": describe_code(code), "status": status_for(code), "count": c["count"],
               "rate": rate, "baseline_count": base_count, "baseline_rate": base_rate, "rate_ratio": ratio, "z": z,
               "unusual": bool(unusual and quality != "insufficient")}
        rows.append(row)
        if unusual:
            detections.append(detection(
                "RESPONSE_CODE_CONCENTRATION", ctx, detected=True, metric=f"rate:{code}", observed=rate,
                baseline=base_rate, sample_current=cur_total, sample_baseline=base_total, quality=quality, statistic=z,
                subject=code, unit="oran", scope_extra={"response_code": code}, limitations=reasons,
                method=(f"Kod oranı baseline'a göre (iki oran z-testi). Kural: adet ≥ {th.code_min_count}, z ≥ {th.min_z}, "
                        f"oran ≥ {th.code_min_rate_ratio}x veya Δ ≥ {th.decline_min_delta_pp} puan"),
                evidence=(f"{code} ({describe_code(code)}): {c['count']} işlem, oran {_pct(base_rate)} → {_pct(rate)} "
                          f"(baseline'da {base_count}; z={z})"),
                extra={"status": status_for(code), "rate_ratio": ratio},
            ))
    return rows, detections


# ---------- Segment izolasyonu (etkilenen / etkilenmeyen karşılaştırması) ----------


def isolate(dimension: str, metric: str, cur_groups: list[dict], base_groups: list[dict], ctx: Ctx,
            th: DetectionThresholds, count_key: str, denominator_key: str = "total") -> dict:
    """Her segmenti hem kendi baseline'ı hem de aynı penceredeki diğer segmentlerle karşılaştırır.

    "X'e izole" sonucu yalnızca tek bir segment Bonferroni düzeltmeli eşiği iki karşılaştırmada da geçer
    ve fazladan olayların en az `segment_min_excess_share` kadarını taşırsa verilir.
    """
    base_by = {g["key"]: g for g in base_groups}
    total_count = sum(g[count_key] for g in cur_groups)
    total_n = sum(g[denominator_key] for g in cur_groups)
    tested = [g for g in cur_groups if g[denominator_key] >= th.segment_min_samples]
    z_crit = bonferroni_z(th.segment_alpha, len(tested) or 1)
    rows = []
    for g in cur_groups:
        b = base_by.get(g["key"], {count_key: 0, denominator_key: 0})
        n, k = g[denominator_key], g[count_key]
        rate, base_rate = safe_rate(k, n), safe_rate(b[count_key], b[denominator_key])
        rest_k, rest_n = total_count - k, total_n - n
        z_rest = two_proportion_z(k, n, rest_k, rest_n)
        z_own = two_proportion_z(k, n, b[count_key], b[denominator_key])
        excess = excess_count(k, n, b[count_key], b[denominator_key]) if b[denominator_key] else 0
        significant = (n >= th.segment_min_samples and (z_rest or 0) >= z_crit and (z_own or 0) >= z_crit)
        rows.append({"segment": g["key"], "n": n, "count": k, "rate": rate, "baseline_n": b[denominator_key],
                     "baseline_rate": base_rate, "rest_rate": safe_rate(rest_k, rest_n), "z_vs_rest": z_rest,
                     "z_vs_baseline": z_own, "excess": excess, "significant": significant,
                     "p95_ms": g.get("p95_ms"), "baseline_p95_ms": base_by.get(g["key"], {}).get("p95_ms")})
    rows.sort(key=lambda r: (-r["excess"], -(r["count"]), r["segment"]))
    total_excess = sum(r["excess"] for r in rows)
    significant = [r for r in rows if r["significant"]]
    top = significant[0] if significant else None
    share = round(top["excess"] / total_excess, 3) if top and total_excess else None
    isolated = len(significant) == 1 and share is not None and share >= th.segment_min_excess_share
    quality, reasons = assess_evidence(total_n, sum(g[denominator_key] for g in base_groups), th)
    if isolated:
        verdict = (f"{dimension}={top['segment']}: oran {_pct(top['baseline_rate'])} → {_pct(top['rate'])} "
                   f"(diğer segmentler {_pct(top['rest_rate'])}; z={top['z_vs_rest']}); fazladan olayların "
                   f"%{share * 100:.0f}'i bu segmentte")
    elif len(significant) > 1:
        verdict = f"Sapma {len(significant)} segmente yayılmış: {', '.join(r['segment'] for r in significant)}"
    else:
        verdict = f"{dimension} boyutunda eşiği geçen tek bir segment yok (z eşiği {z_crit}, {len(tested)} segment test edildi)"
    det = detection(
        "SEGMENT_ISOLATION", ctx, detected=isolated, metric=f"{metric}@{dimension}",
        observed=top["rate"] if top else None, baseline=top["rest_rate"] if top else None,
        sample_current=top["n"] if top else total_n, sample_baseline=top["baseline_n"] if top else None,
        quality=quality, statistic=top["z_vs_rest"] if top else None, subject=top["segment"] if isolated else None,
        unit="oran", scope_extra={dimension: top["segment"]} if isolated else {}, limitations=reasons,
        method=(f"Etkilenen segment vs diğer segmentler ve kendi baseline'ı; Bonferroni düzeltmeli z ≥ {z_crit} "
                f"({len(tested)} test), fazladan payı ≥ %{th.segment_min_excess_share * 100:.0f}"),
        evidence=verdict,
        extra={"dimension": dimension, "excess_share": share, "significant_segments": [r["segment"] for r in significant]},
    )
    return {"dimension": dimension, "metric": metric, "z_threshold": z_crit, "segments_tested": len(tested),
            "segments": rows, "significant": [r["segment"] for r in significant], "isolated": isolated,
            "isolated_segment": top["segment"] if isolated else None, "excess_share": share,
            "total_excess": total_excess, "verdict": verdict, "detection": det}


# ---------- C. Gecikme ve timeout korelasyonu ----------


def _onset(values: list[float | None], threshold: float, sustain: int) -> int | None:
    run = 0
    for i, v in enumerate(values):
        run = run + 1 if v is not None and v >= threshold else 0
        if run >= sustain:
            return i - sustain + 1
    return None


def latency_timeout_correlation(buckets: list[dict], base: dict, ctx: Ctx, th: DetectionThresholds,
                                interval_min: int) -> dict:
    usable = [b for b in buckets if b["total"] >= 10]
    p95 = [b["p95_ms"] for b in usable]
    failure = [safe_rate(b["timeouts"] + b["errors"], b["total"]) for b in usable]
    pairs = [(x, y) for x, y in zip(p95, failure) if x is not None and y is not None]
    rho = spearman([p[0] for p in pairs], [p[1] for p in pairs]) if len(pairs) >= 3 else None
    base_p95 = base["p95_ms"] or 0
    base_fail = base["technical_rate"] or 0
    lat_series = [b["p95_ms"] for b in buckets]
    fail_series = [safe_rate(b["timeouts"] + b["errors"], b["total"]) if b["total"] >= 10 else None for b in buckets]
    lat_i = _onset(lat_series, base_p95 * th.onset_latency_ratio, th.onset_sustain_buckets) if base_p95 else None
    fail_i = _onset(fail_series, base_fail + th.onset_failure_min_delta, th.onset_sustain_buckets)
    lat_onset = buckets[lat_i]["bucket_start"] if lat_i is not None else None
    fail_onset = buckets[fail_i]["bucket_start"] if fail_i is not None else None
    lead = (fail_i - lat_i) * interval_min if lat_i is not None and fail_i is not None else None
    both = [b["bucket_start"] for b, lv, fv in zip(buckets, lat_series, fail_series)
            if lv is not None and fv is not None and base_p95 and lv >= base_p95 * th.onset_latency_ratio
            and fv >= base_fail + th.onset_failure_min_delta]
    quality = "sufficient" if len(pairs) >= th.correlation_min_buckets else ("limited" if len(pairs) >= 4 else "insufficient")
    limitations = [CORRELATION_NOTE]
    if quality != "sufficient":
        limitations.append(f"Korelasyon için {len(pairs)} yeterli dilim var (en az {th.correlation_min_buckets} önerilir).")
    detected = rho is not None and rho >= th.correlation_min_rho and (len(both) >= 2 or (lat_i is not None and fail_i is not None))
    if lead is None:
        order = "Gecikme ve hata başlangıcı birlikte belirlenemedi."
    elif lead > 0:
        order = f"Gecikme artışı ({lat_onset[11:16]}) hata artışından ({fail_onset[11:16]}) {lead} dk önce başladı."
    elif lead == 0:
        order = f"Gecikme ve hata artışı aynı dilimde ({lat_onset[11:16]}) başladı."
    else:
        order = f"Hata artışı ({fail_onset[11:16]}) gecikme artışından ({lat_onset[11:16]}) {-lead} dk önce başladı."
    return {
        "rho": rho, "buckets_used": len(pairs), "latency_onset": lat_onset, "failure_onset": fail_onset,
        "lead_minutes": lead, "cooccurring_buckets": both, "order": order,
        "detection": detection(
            "LATENCY_TIMEOUT_CORRELATION", ctx, detected=detected, metric="spearman(p95, teknik hata oranı)",
            observed=rho, baseline=None, sample_current=len(pairs), sample_baseline=None, quality=quality,
            statistic=rho, unit="rho", claim_type="correlation", limitations=limitations,
            method=(f"{interval_min} dk dilimlerde Spearman sıra korelasyonu (rho ≥ {th.correlation_min_rho}) ve "
                    f"başlangıç zamanı karşılaştırması (p95 ≥ baseline × {th.onset_latency_ratio}, hata ≥ baseline + "
                    f"{th.onset_failure_min_delta * 100:.0f} puan, {th.onset_sustain_buckets} dilim sürekli)"),
            evidence=f"Spearman rho={rho} ({len(pairs)} dilim); {order} {CORRELATION_NOTE}",
            extra={"lead_minutes": lead, "latency_onset": lat_onset, "failure_onset": fail_onset},
        ),
    }


# ---------- D. Reversal ----------


def reversal_anomaly(cur_link: dict, base_link: dict, cur_total: int, base_total: int, ctx: Ctx,
                     th: DetectionThresholds) -> dict:
    cc, bc = cur_link["categories"], base_link["categories"]
    cur_tech = cc["auto_after_approval"] + cc["orphan"]
    base_tech = bc["auto_after_approval"] + bc["orphan"]
    rate, base_rate = safe_rate(cur_tech, cur_total), safe_rate(base_tech, base_total)
    z = two_proportion_z(cur_tech, cur_total, base_tech, base_total)
    ratio_ok = not base_rate or (rate or 0) / base_rate >= th.reversal_min_ratio
    quality, reasons = assess_evidence(cur_total, base_total, th)
    limitations = list(reasons)
    if cur_link.get("truncated"):
        limitations.append("Reversal bağlantı sorgusu üst sınıra takıldı; sınıflandırma kısmi.")
    limitations.append("Orijinali pencere/lookback dışında kalan reversal'lar orphan görünebilir.")
    detected = cur_tech >= th.reversal_min_technical and ratio_ok and (z or 0) >= th.min_z
    total_ratio, base_total_ratio = safe_rate(cur_link["total_reversals"], cur_total), safe_rate(base_link["total_reversals"], base_total)
    explained = cc["after_failure"]
    return detection(
        "REVERSAL_ANOMALY", ctx, detected=detected, metric="technical_reversal_ratio", observed=rate, baseline=base_rate,
        sample_current=cur_total, sample_baseline=base_total, quality=quality, statistic=z, unit="oran",
        limitations=limitations,
        method=(f"Reversal'lar correlation_id ile orijinal işleme bağlanır: onaydan ≤ {th.auto_reversal_max_s} sn sonra "
                f"(otomatik) ve orijinalsiz (orphan) reversal'lar teknik sayılır; meşru iptaller ve hata/timeout sonrası "
                f"koruyucu reversal'lar ayrı raporlanır. Kural: teknik ≥ {th.reversal_min_technical}, oran ≥ "
                f"{th.reversal_min_ratio}x, z ≥ {th.min_z}"),
        evidence=(f"Açıklanamayan teknik reversal {base_tech} → {cur_tech} (oran {_pct(base_rate)} → {_pct(rate)}, z={z}); "
                  f"toplam reversal oranı {_pct(base_total_ratio)} → {_pct(total_ratio)}; hata/timeout sonrası koruyucu "
                  f"reversal {explained}, meşru iptal {cc['void_after_approval']}"),
        extra={"technical": cur_tech, "baseline_technical": base_tech, "categories": cc, "baseline_categories": bc},
    )


def reconciliation_risk(link: dict) -> dict:
    cats = link["categories"]
    indicators = []
    if cats["auto_after_approval"]:
        indicators.append(f"{cats['auto_after_approval']} onaylı işlem saniyeler içinde otomatik geri alındı (terminal yanıtı alamamış olabilir).")
    if cats["orphan"]:
        indicators.append(f"{cats['orphan']} reversal'ın orijinal işlemi bulunamadı (orphan).")
    if link["unreversed_timeouts"]:
        indicators.append(f"{link['unreversed_timeouts']} POS timeout işleminin reversal kaydı yok (durumu belirsiz işlem).")
    if cats.get("unlinkable"):
        indicators.append(f"{cats['unlinkable']} reversal'ın correlation_id alanı boş; orijinal işleme bağlanamadı (veri kalitesi).")
    if cats["unexpected"]:
        indicators.append(f"{cats['unexpected']} reversal beklenmeyen bir orijinal duruma bağlı.")
    score = cats["auto_after_approval"] + cats["orphan"] + link["unreversed_timeouts"] * 2
    level = "HIGH" if score >= 30 else "MEDIUM" if score >= 10 else "LOW"
    return {"level": level, "indicators": indicators,
            "note": "Mutabakat riski göstergesidir; tutar verisi olmadığı için parasal etki hesaplanmaz."}


# ---------- E. Servis sürümü ----------


def version_comparison(service: str, versions: list[dict], deployments: list[dict], ctx: Ctx,
                       th: DetectionThresholds) -> dict:
    total_n = sum(v["total"] for v in versions)
    total_k = sum(v["technical"] for v in versions)
    tested = [v for v in versions if v["total"] >= th.version_min_samples]
    z_crit = bonferroni_z(th.segment_alpha, len(tested) or 1)
    rows = []
    for v in versions:
        rest_n, rest_k = total_n - v["total"], total_k - v["technical"]
        rate, rest_rate = safe_rate(v["technical"], v["total"]), safe_rate(rest_k, rest_n)
        z = two_proportion_z(v["technical"], v["total"], rest_k, rest_n)
        ratio = round(rate / rest_rate, 2) if rate is not None and rest_rate else None
        degraded = (v["total"] >= th.version_min_samples and (z or 0) >= z_crit
                    and (ratio is None or ratio >= th.version_min_ratio) and len(versions) > 1)
        rows.append({"version": v["key"], "n": v["total"], "technical": v["technical"], "technical_rate": rate,
                     "others_rate": rest_rate, "rate_ratio": ratio, "z": z, "p95_ms": v["p95_ms"], "degraded": degraded})
    worst = max((r for r in rows if r["degraded"]), key=lambda r: r["z"] or 0, default=None)
    quality, reasons = assess_evidence(min((v["total"] for v in versions), default=0), total_n, th) if len(versions) > 1 \
        else ("insufficient", ["Pencerede tek sürüm var; sürüm karşılaştırması yapılamaz."])
    deploy = next((d for d in deployments if worst and d["version"] == worst["version"]), None)
    if worst:
        evidence = (f"{service} {worst['version']}: teknik hata {_pct(worst['technical_rate'])} vs diğer sürümler "
                    f"{_pct(worst['others_rate'])} (z={worst['z']}, n={worst['n']})"
                    + (f"; sürüm ilk kez {deploy['first_seen'][11:16]} UTC'de görüldü" if deploy and deploy.get("first_seen") else ""))
    elif len(versions) <= 1:
        evidence = f"{service} için pencerede tek sürüm görüldü ({versions[0]['key'] if versions else '-'})."
    else:
        evidence = f"{service} sürümleri arasında eşiği geçen fark yok (z eşiği {z_crit})."
    return {
        "service": service, "versions": rows, "deployments": deployments, "z_threshold": z_crit,
        "detection": detection(
            "SERVICE_VERSION_DEGRADATION", ctx, detected=worst is not None, metric="technical_rate@service_version",
            observed=worst["technical_rate"] if worst else None, baseline=worst["others_rate"] if worst else None,
            sample_current=worst["n"] if worst else total_n, sample_baseline=None, quality=quality,
            statistic=worst["z"] if worst else None, subject=worst["version"] if worst else None, unit="oran",
            scope_extra={"service_name": service, **({"service_version": worst["version"]} if worst else {})},
            claim_type="hypothesis", limitations=reasons + [HYPOTHESIS_NOTE],
            method=(f"Aynı penceredeki sürümlerin eşzamanlı karşılaştırması (zaman etkisini dengeler); Bonferroni "
                    f"z ≥ {z_crit}, oran ≥ {th.version_min_ratio}x, n ≥ {th.version_min_samples}. Deployment zamanı = "
                    "sürümün veride ilk görüldüğü an (yaklaşık)."),
            evidence=evidence,
            extra={"deployment": deploy},
        ),
    }
