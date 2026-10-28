# Benchmark sonuçları

Ölçüm zamanı: 2026-10-28 20:36 UTC

Ortam: Python 3.14.7 · pandas 3.1.0 · numpy 2.5.4 · Windows 11 · Intel64 Family 6 Model 154 Stepping 3, GenuineIntel

Yöntem: 1 ısınma + 5 tekrar, `time.perf_counter` medyanı; veri bellekte, 24 saate yayılmış, %3 hata oranı.

```
    Kayıt  Yükleme+sıralama (ms)  summary 30 dk (ms)  histogram 24 sa / 5 dk (ms)  group_by service 24 sa (ms)  analyze_error_patterns 30 dk vs 120 dk (ms)
  100,000                    4.1                0.21                        38.57                        10.12                                        11.77
1,000,000                   24.8                0.37                        69.78                        99.89                                        26.84
3,000,000                   71.7                0.66                       134.53                       333.16                                        58.26
```
