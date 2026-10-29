# Değerlendirme sonuçları (sentetik)

Ölçüm zamanı: 2026-10-29 10:05 UTC · Ortam: Python 3.14.7 · Windows 11 · Intel64 Family 6 Model 154 Stepping 3, GenuineIntel · Seed'ler: 42, 7, 123 · Mod: deterministik (LLM yok); önce tek agent (LangGraph), ardından çoklu agent mimarisi

> Bu sonuçlar `python -m scripts.evaluate` çalıştırmasından otomatik üretilmiştir. Veriler ve etiketler sentetiktir; eşikler bu senaryolar için seçilmiştir. Sonuçlar üretim performansını göstermez.

## Tek agent (LangGraph) · özet

| Ölçüt | Sonuç |
|---|---|
| Çalıştırma (senaryo × seed) | 30 |
| Toplam precision (tespit) | 1.000 |
| Toplam recall (tespit) | 0.985 |
| Doğru risk seviyesi | 29/30 (%96.7) |
| Normal senaryolarda tespit içeren inceleme (FP oranı) | 0/6 (%0.0) |
| Normal senaryolarda HIGH seviye | 0/6 (%0.0) |
| Kayan 60 dk pencerelerde alarm (normal) | 0/66 (%0.0) |
| Yetersiz kanıt doğru işaretlendi (low_volume) | 3/3 (%100.0) |
| Yetersiz kanıt yanlış işaretlendi (diğer senaryolar) | 0/27 (%0.0) |
| Beklenen kanıt kalitesi (data_quality_gap: limited) | 3/3 (%100.0) |
| İnceleme tamamlama (hata enjeksiyonsuz) | 30/30 (%100.0) |
| Rapor üretildi (hata enjeksiyonlu) | 9/9 (%100.0) |
| Tamamlandı / eksik olarak işaretlendi (hata enjeksiyonlu) | 6 / 3 |
| Rapor-araç tutarlılık kontrolü geçti | 39/39 (%100.0) |
| Farklı araç yolu sayısı (seed 1, 10 senaryo) | 8 |
| Ort. / maks. araç çağrısı | 7.4 / 12 |
| Süre medyan / p95 (ms, inceleme başına) | 403 / 576 |

## Dedektör bazında

| Dedektör | TP | FP | FN | Precision | Recall |
|---|---|---|---|---|---|
| DECLINE_RATE_SPIKE | 3 | 0 | 0 | 1.00 | 1.00 |
| LATENCY_INCREASE | 6 | 0 | 0 | 1.00 | 1.00 |
| LATENCY_TIMEOUT_CORRELATION | 5 | 0 | 1 | 1.00 | 0.83 |
| RESPONSE_CODE_CONCENTRATION | 12 | 0 | 0 | 1.00 | 1.00 |
| REVERSAL_ANOMALY | 3 | 0 | 0 | 1.00 | 1.00 |
| SEGMENT_ISOLATION | 15 | 0 | 0 | 1.00 | 1.00 |
| SERVICE_VERSION_DEGRADATION | 3 | 0 | 0 | 1.00 | 1.00 |
| TECHNICAL_FAILURE_SPIKE | 9 | 0 | 0 | 1.00 | 1.00 |
| TIMEOUT_RATE_SPIKE | 6 | 0 | 0 | 1.00 | 1.00 |
| VOLUME_ANOMALY | 3 | 0 | 0 | 1.00 | 1.00 |

## Senaryo bazında

| Senaryo | Seviye (seed'ler) | Beklenen seviye | Recall | FP | Ort. araç çağrısı | Yol (seed 1) |
|---|---|---|---|---|---|---|
| normal_baseline | LOW, LOW, LOW | LOW | - | 0 | 3 | check data coverage → compare with baseline → submit findings |
| response_code_spike | MEDIUM, MEDIUM, MEDIUM | MEDIUM/HIGH | 9/9 (%100.0) | 0 | 7 | check data coverage → compare with baseline → analyze response codes → search transactions → search similar incidents → search runbooks → submit findings |
| bank_timeout | HIGH, HIGH, MEDIUM | HIGH | 18/18 (%100.0) | 0 | 10 | check data coverage → compare with baseline → breakdown by dimension → analyze response codes → analyze latency timeouts → analyze reversals → search transactions → search similar incidents → search runbooks → submit findings |
| latency_then_failures | HIGH, HIGH, HIGH | MEDIUM/HIGH | 17/18 (%94.4) | 0 | 12 | check data coverage → compare with baseline → breakdown by dimension → breakdown by dimension → analyze response codes → analyze latency timeouts → compare service versions → analyze reversals → search transactions → search similar incidents → search runbooks → submit findings |
| reversal_anomaly | MEDIUM, MEDIUM, MEDIUM | MEDIUM/HIGH | 6/6 (%100.0) | 0 | 9 | check data coverage → compare with baseline → analyze reversals → breakdown by dimension → analyze reversals → search transactions → search similar incidents → search runbooks → submit findings |
| version_regression | MEDIUM, MEDIUM, MEDIUM | MEDIUM/HIGH | 12/12 (%100.0) | 0 | 10 | check data coverage → compare with baseline → breakdown by dimension → breakdown by dimension → analyze response codes → compare service versions → search transactions → search similar incidents → search runbooks → submit findings |
| low_volume | INDETERMINATE, INDETERMINATE, INDETERMINATE | INDETERMINATE | - | 0 | 3 | check data coverage → compare with baseline → submit findings |
| normal_fluctuation | LOW, LOW, LOW | LOW/MEDIUM | - | 0 | 3 | check data coverage → compare with baseline → submit findings |
| volume_drop | MEDIUM, MEDIUM, MEDIUM | MEDIUM/HIGH | 3/3 (%100.0) | 0 | 6 | check data coverage → compare with baseline → search transactions → search similar incidents → search runbooks → submit findings |
| data_quality_gap | LOW, LOW, LOW | LOW/MEDIUM | - | 0 | 4 | check data coverage → compare with baseline → analyze reversals → submit findings |

## Etiket dışı tespitler (yanlış pozitif sayılanlar)

- Yok.

## Hata enjeksiyonlu çalıştırmalar

| Senaryo | Hata | Durum | Seviye | Araç çağrısı |
|---|---|---|---|---|
| bank_timeout | breakdown_by_dimension=error | complete | HIGH | 11 |
| bank_timeout | search_runbooks=error | incomplete | HIGH | 10 |
| bank_timeout | analyze_latency_timeouts=timeout_once | complete | HIGH | 10 |
| version_regression | breakdown_by_dimension=error | complete | MEDIUM | 11 |
| version_regression | search_runbooks=error | incomplete | MEDIUM | 10 |
| version_regression | analyze_latency_timeouts=timeout_once | complete | MEDIUM | 10 |
| reversal_anomaly | breakdown_by_dimension=error | complete | MEDIUM | 8 |
| reversal_anomaly | search_runbooks=error | incomplete | MEDIUM | 9 |
| reversal_anomaly | analyze_latency_timeouts=timeout_once | complete | MEDIUM | 9 |

## Çoklu agent mimarisi

| Ölçüt | Sonuç |
|---|---|
| Çalıştırma (senaryo × seed) | 30 |
| Tespit precision / recall | 1.000 / 0.985 |
| Doğru risk seviyesi | 29/30 (%96.7) |
| Normal senaryolarda tespit içeren inceleme | 0/6 (%0.0) |
| Yetersiz kanıt doğru işaretlendi | 3/3 (%100.0) |
| Paralel agent dalgası içeren inceleme | 12/30 (%40.0) |
| Takip turu açılan inceleme | 3/30 (%10.0) |
| Rapor + risk agent'ı tutarlılık kontrolü | 36/36 (%100.0) |
| Hata enjeksiyonunda rapor üretildi / eksik işaretlendi | 6/6 / 4 |
| Ort. araç çağrısı | 6.1 |
| Süre medyan / p95 (ms) | 421 / 646 |

| Senaryo | Seviye (seed'ler) | Agent'lar (seed 1, tur) | Paralel | Takip turu |
|---|---|---|---|---|
| normal_baseline | LOW, LOW, LOW | anomaly_detection@1, impact_risk@1 | hayır | hayır |
| response_code_spike | MEDIUM, MEDIUM, MEDIUM | anomaly_detection@1, historical_incident@1, impact_risk@1 | hayır | hayır |
| bank_timeout | HIGH, HIGH, MEDIUM | anomaly_detection@1, payment_flow@1, historical_incident@1, impact_risk@1 | evet | hayır |
| latency_then_failures | HIGH, HIGH, HIGH | anomaly_detection@1, payment_flow@1, historical_incident@1, impact_risk@1 | evet | hayır |
| reversal_anomaly | MEDIUM, MEDIUM, MEDIUM | anomaly_detection@1, payment_flow@1, historical_incident@1, impact_risk@1, anomaly_detection@2, impact_risk@2 | evet | evet |
| version_regression | MEDIUM, MEDIUM, MEDIUM | anomaly_detection@1, historical_incident@1, impact_risk@1 | hayır | hayır |
| low_volume | INDETERMINATE, INDETERMINATE, INDETERMINATE | anomaly_detection@1, impact_risk@1 | hayır | hayır |
| normal_fluctuation | LOW, LOW, LOW | anomaly_detection@1, impact_risk@1 | hayır | hayır |
| volume_drop | MEDIUM, MEDIUM, MEDIUM | anomaly_detection@1, historical_incident@1, impact_risk@1 | hayır | hayır |
| data_quality_gap | LOW, LOW, LOW | anomaly_detection@1, payment_flow@1, impact_risk@1 | evet | hayır |

| Senaryo | Hata | Durum | Seviye | Başarısız agent |
|---|---|---|---|---|
| bank_timeout | breakdown_by_dimension=error | incomplete | HIGH | 0 |
| bank_timeout | search_runbooks=error | incomplete | HIGH | 0 |
| bank_timeout | analyze_latency_timeouts=timeout_once | complete | HIGH | 0 |
| reversal_anomaly | breakdown_by_dimension=error | incomplete | MEDIUM | 0 |
| reversal_anomaly | search_runbooks=error | incomplete | MEDIUM | 0 |
| reversal_anomaly | analyze_latency_timeouts=timeout_once | complete | MEDIUM | 0 |
