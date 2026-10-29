# Benchmark sonuçları (sentetik bellek içi depo)

Ölçüm zamanı: 2026-10-29 07:52 UTC

Ortam: Python 3.14.7 · pandas 3.1.0 · numpy 2.5.4 · Windows 11 · Intel64 Family 6 Model 154 Stepping 3, GenuineIntel

Yöntem: 1 ısınma + 5 tekrar, `time.perf_counter` medyanı; veri bellekte, 24 saate yayılmış. Elasticsearch ölçülmedi.

| Olay | Yükleme+sıralama (ms) | summary 60 dk (ms) | histogram 24 sa / 5 dk (ms) | group_by bank 24 sa (ms) | reversal_linkage 60 dk (ms) | compare_with_baseline 60 vs 120 dk (ms) |
|---|---|---|---|---|---|---|
| 100,000 | 5.0 | 2.26 | 427.42 | 42.38 | 10.49 | 87.92 |
| 1,000,000 | 41.8 | 9.73 | 764.41 | 382.37 | 42.31 | 138.3 |
