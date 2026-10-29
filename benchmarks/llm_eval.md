# Gerçek LLM ile değerlendirme

Ölçüm zamanı: 2026-10-29 10:28 UTC · Ortam: Python 3.14.7 · Windows 11 · ollama/qwen3.5:4b

Yöntem: 10 senaryo etiketli talebiyle, 3 senaryo ayrıca 2 farklı ifadeyle; her mimaride gerçek LLM ile uçtan uca. "Deterministik ile aynı seviye": aynı talebin aynı mimarideki LLM'siz sonucu. "LLM akışı yönetti": tek agent'ta akış yedek moda düşmeden bitti; çoklu agent'ta LLM agent seçimi geçerli yapılandırılmış çıktı üretti. LLM bulgularındaki sayılar araç çıktılarıyla doğrulanır; ifadelerin anlamı otomatik doğrulanmaz.

> Sentetik veri ve tek bir yerel model (qwen3.5:4b) ile küçük bir örneklemdir; üretim performansını göstermez.

## Özet

| Mimari | Çalıştırma | Deterministik ile aynı seviye | Etiketle uyumlu (beklenen tespitler + seviye) | Kanıt tamam (rapor tamamlandı / kanıt yetersiz doğru) | LLM akışı yönetti | LLM bulgusu kabul / red | Uydurulan kapsam yakalandı | LLM hata olayı | Ort. araç çağrısı | Medyan / maks. süre (sn) |
|---|---|---|---|---|---|---|---|---|---|---|
| Tek agent (LangGraph) | 16 | 16/16 (%100) | 10/10 (%100) | 16/16 (%100) | 7/16 (%44) | 124 / 1 | 0 | 0 | 7.8 | 72.35 / 91.0 |
| Çoklu agent | 16 | 16/16 (%100) | 10/10 (%100) | 16/16 (%100) | 16/16 (%100) | 95 / 0 | 0 | 0 | 8.6 | 17.15 / 24.2 |

Kapsam dışı talep ("Bana bir şiir yaz") reddedildi — Tek agent (LangGraph): evet
Kapsam dışı talep ("Bana bir şiir yaz") reddedildi — Çoklu agent: evet

## Çalıştırmalar

| Mimari | Senaryo | Talep | Deterministik | LLM | Etiket | LLM yönetti | Araç | Bulgu (kabul/red) | Süre (sn) |
|---|---|---|---|---|---|---|---|---|---|
| Tek agent (LangGraph) | normal_baseline | etiketli | LOW | LOW | ✓ | ✓ | 4 | 4/0 | 42.3 |
| Tek agent (LangGraph) | response_code_spike | etiketli | MEDIUM | MEDIUM | ✓ | ✗ | 10 | 9/0 | 84.0 |
| Tek agent (LangGraph) | bank_timeout | etiketli | HIGH | HIGH | ✓ | ✗ | 10 | 12/0 | 64.7 |
| Tek agent (LangGraph) | latency_then_failures | etiketli | HIGH | HIGH | ✓ | ✗ | 14 | 11/1 | 70.5 |
| Tek agent (LangGraph) | reversal_anomaly | etiketli | MEDIUM | MEDIUM | ✓ | ✗ | 9 | 8/0 | 68.9 |
| Tek agent (LangGraph) | version_regression | etiketli | MEDIUM | MEDIUM | ✓ | ✗ | 10 | 10/0 | 86.2 |
| Tek agent (LangGraph) | low_volume | etiketli | INDETERMINATE | INDETERMINATE | ✓ | ✓ | 3 | 6/0 | 52.5 |
| Tek agent (LangGraph) | normal_fluctuation | etiketli | LOW | LOW | ✓ | ✓ | 3 | 3/0 | 43.9 |
| Tek agent (LangGraph) | volume_drop | etiketli | MEDIUM | MEDIUM | ✓ | ✓ | 6 | 4/0 | 75.9 |
| Tek agent (LangGraph) | data_quality_gap | etiketli | LOW | LOW | ✓ | ✓ | 4 | 5/0 | 39.9 |
| Tek agent (LangGraph) | normal_baseline | alternatif | LOW | LOW | - | ✓ | 4 | 6/0 | 57.6 |
| Tek agent (LangGraph) | normal_baseline | alternatif | LOW | LOW | - | ✓ | 5 | 6/0 | 91.0 |
| Tek agent (LangGraph) | bank_timeout | alternatif | HIGH | HIGH | - | ✗ | 12 | 12/0 | 74.2 |
| Tek agent (LangGraph) | bank_timeout | alternatif | HIGH | HIGH | - | ✗ | 10 | 12/0 | 77.3 |
| Tek agent (LangGraph) | reversal_anomaly | alternatif | MEDIUM | MEDIUM | - | ✗ | 11 | 8/0 | 86.5 |
| Tek agent (LangGraph) | reversal_anomaly | alternatif | MEDIUM | MEDIUM | - | ✗ | 9 | 8/0 | 77.0 |
| Çoklu agent | normal_baseline | etiketli | LOW | LOW | ✓ | ✓ | 5 | 6/0 | 13.7 |
| Çoklu agent | response_code_spike | etiketli | MEDIUM | MEDIUM | ✓ | ✓ | 8 | 6/0 | 23.1 |
| Çoklu agent | bank_timeout | etiketli | HIGH | HIGH | ✓ | ✓ | 13 | 6/0 | 20.3 |
| Çoklu agent | latency_then_failures | etiketli | HIGH | HIGH | ✓ | ✓ | 15 | 6/0 | 22.3 |
| Çoklu agent | reversal_anomaly | etiketli | MEDIUM | MEDIUM | ✓ | ✓ | 11 | 6/0 | 16.7 |
| Çoklu agent | version_regression | etiketli | MEDIUM | MEDIUM | ✓ | ✓ | 11 | 6/0 | 24.2 |
| Çoklu agent | low_volume | etiketli | INDETERMINATE | INDETERMINATE | ✓ | ✓ | 3 | 6/0 | 15.2 |
| Çoklu agent | normal_fluctuation | etiketli | LOW | LOW | ✓ | ✓ | 2 | 6/0 | 12.9 |
| Çoklu agent | volume_drop | etiketli | MEDIUM | MEDIUM | ✓ | ✓ | 7 | 5/0 | 15.3 |
| Çoklu agent | data_quality_gap | etiketli | LOW | LOW | ✓ | ✓ | 5 | 6/0 | 11.2 |
| Çoklu agent | normal_baseline | alternatif | LOW | LOW | - | ✓ | 5 | 6/0 | 16.6 |
| Çoklu agent | normal_baseline | alternatif | LOW | LOW | - | ✓ | 5 | 6/0 | 14.5 |
| Çoklu agent | bank_timeout | alternatif | HIGH | HIGH | - | ✓ | 13 | 6/0 | 19.2 |
| Çoklu agent | bank_timeout | alternatif | HIGH | HIGH | - | ✓ | 13 | 6/0 | 20.0 |
| Çoklu agent | reversal_anomaly | alternatif | MEDIUM | MEDIUM | - | ✓ | 11 | 6/0 | 17.6 |
| Çoklu agent | reversal_anomaly | alternatif | MEDIUM | MEDIUM | - | ✓ | 11 | 6/0 | 19.4 |
