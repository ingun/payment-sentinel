# LLM agent değerlendirmesi

Ölçüm zamanı: 2026-10-28 22:52 UTC

Ortam: Python 3.14.7 · Windows 11 · ollama/qwen3.5:4b

Yöntem: 4 senaryo × 3 farklı talep, gerçek LLM ile uçtan uca. Beklenen risk seviyesi, aynı talebin deterministik moddaki sonucudur. "LLM sona kadar yönetti": akış yedek deterministik moda düşmeden bitti. "Bulgu özeti": LLM'in yazdığı bulgular; reddedilenler araç çıktısında olmayan sayı veya kesin neden iddiası içerir ve rapora girmez.

## Özet

| Senaryo | Çalıştırma | Doğru risk seviyesi | Zorunlu kanıt tamam | LLM sona kadar yönetti | Bulgu özeti yazıldı | Kabul / reddedilen bulgu | Uydurulan kapsam yakalandı | Ort. araç çağrısı | Medyan süre (sn) |
|---|---|---|---|---|---|---|---|---|---|
| normal | 3 | 3/3 (%100) | 3/3 (%100) | 3/3 (%100) | 3/3 (%100) | 19 / 0 | 1 | 7.3 | 79.0 |
| error_spike | 3 | 3/3 (%100) | 3/3 (%100) | 2/3 (%67) | 3/3 (%100) | 19 / 0 | 1 | 7 | 98.6 |
| latency_degradation | 3 | 3/3 (%100) | 3/3 (%100) | 2/3 (%67) | 3/3 (%100) | 19 / 0 | 1 | 6.7 | 87.2 |
| volume_drop | 3 | 3/3 (%100) | 3/3 (%100) | 3/3 (%100) | 3/3 (%100) | 18 / 0 | 1 | 7.3 | 86.5 |
| **Toplam** | 12 | 12/12 (%100) | 12/12 (%100) | 10/12 (%83) | 12/12 (%100) | 75 / 0 | 4 | 7.1 | 87.1 |

Kapsam dışı talep ("Bana bir şiir yaz") reddedildi: 3/3 (%100)

## Çalıştırmalar

| Senaryo | Talep | Beklenen | LLM | Araç | Bulgu (kabul/red) | Süre (sn) |
|---|---|---|---|---|---|---|
| normal | Son 30 dakikadaki ödeme başarısızlıklarını ar… | LOW | LOW | 9 | 6/0 | 77.3 |
| normal | Son 30 dakikadaki ödeme hatalarını araştır ve… | LOW | LOW | 7 | 7/0 | 90.7 |
| normal | Ödemelerde bir sorun var gibi, kontrol eder m… | LOW | LOW | 6 | 6/0 | 79.0 |
| error_spike | Son 30 dakikadaki ödeme başarısızlıklarını ar… | HIGH | HIGH | 7 | 6/0 | 75.8 |
| error_spike | Son 30 dakikadaki ödeme hatalarını araştır ve… | HIGH | HIGH | 8 | 7/0 | 98.6 |
| error_spike | Ödemelerde bir sorun var gibi, kontrol eder m… | HIGH | HIGH | 6 | 6/0 | 98.7 |
| latency_degradation | Son 30 dakikadaki ödeme başarısızlıklarını ar… | MEDIUM | MEDIUM | 7 | 6/0 | 87.0 |
| latency_degradation | Son 30 dakikadaki ödeme hatalarını araştır ve… | MEDIUM | MEDIUM | 6 | 6/0 | 87.2 |
| latency_degradation | Ödemelerde bir sorun var gibi, kontrol eder m… | MEDIUM | MEDIUM | 7 | 7/0 | 92.7 |
| volume_drop | Son 30 dakikadaki ödeme başarısızlıklarını ar… | MEDIUM | MEDIUM | 8 | 6/0 | 84.0 |
| volume_drop | Son 30 dakikadaki ödeme hatalarını araştır ve… | MEDIUM | MEDIUM | 8 | 6/0 | 96.8 |
| volume_drop | Ödemelerde bir sorun var gibi, kontrol eder m… | MEDIUM | MEDIUM | 6 | 6/0 | 86.5 |
