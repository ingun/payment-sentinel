# Payment Sentinel: Ödeme Operasyonu İnceleme Agent'ı

Payment Sentinel, ödeme operasyon ekiplerinin olağandışı işlem davranışını incelemesine yardım eden, **salt okunur** bir agentic AI uygulamasıdır. Bir orkestratör; anomali tespiti, ödeme akışı, geçmiş olay ve risk uzmanı agent'ları ara bulgulara göre (gerektiğinde paralel) yönetir. Uygulama sırasıyla şunları yapar:

1. Anomaliyi deterministik istatistikle tespit eder.
2. Sorunun banka, üye işyeri, servis veya sürüm düzeyinde izole olup olmadığını ara sonuçlara göre seçtiği araçlarla araştırır.
3. Geçmiş incident kayıtlarını ve runbook'ları getirir.
4. Doğrulanmış gerçekleri, doğrulanmamış hipotezleri ve doğrulanmış nedenleri ayrı tutan kanıta dayalı bir rapor üretir.

> **Tüm veriler sentetiktir.** Gerçek kart, müşteri veya üye işyeri verisi kullanılmaz. Gösterilen sayılar üretim ölçümü değildir. Sistem hiçbir işlem, iade, reversal veya engelleme yapmaz; yalnızca analiz eder ve öneride bulunur.

![Genel görünüm: BANK_B timeout senaryosu](screenshots/01_bank_timeout_overview.png)

---

## İçindekiler
1. [Problem](#1-problem)
2. [Neden tek bir LLM çağrısı yetmez?](#2-neden-tek-bir-llm-çağrısı-yetmez)
3. [Çoklu agent mimarisi](#3-çoklu-agent-mimarisi)
4. [Tek agent (LangGraph) mimarisi](#4-tek-agent-langgraph-mimarisi)
5. [Veri modeli ve sentetik senaryolar](#5-veri-modeli-ve-sentetik-senaryolar)
6. [Deterministik tespit](#6-deterministik-tespit)
7. [Araçlar ve kanıta dayalı inceleme politikası](#7-araçlar-ve-kanıta-dayalı-inceleme-politikası)
8. [Geçmiş incident eşleştirme](#8-geçmiş-incident-eşleştirme)
9. [Runbook araması](#9-runbook-araması)
10. [Risk ve etki değerlendirmesi](#10-risk-ve-etki-değerlendirmesi)
11. [Rapor](#11-rapor)
12. [Veri erişimi ve Elasticsearch](#12-veri-erişimi-ve-elasticsearch)
13. [Güvenlik ve dayanıklılık](#13-güvenlik-ve-dayanıklılık)
14. [Teknolojiler ve seçim gerekçeleri](#14-teknolojiler-ve-seçim-gerekçeleri)
15. [Kurulum ve çalıştırma](#15-kurulum-ve-çalıştırma)
16. [Testler ve değerlendirme](#16-testler-ve-değerlendirme)
17. [Sınırlamalar](#17-sınırlamalar)
18. [Ekran görüntüleri](#18-ekran-görüntüleri)
19. [Proje yapısı](#19-proje-yapısı)

Ayrıntılı mimari: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

---

## 1. Problem

Bir POS hata artışında on-call mühendisinin dakikalar içinde şu sorulara cevap vermesi gerekir:

- Artış gerçek mi, yoksa normal dalgalanma mı?
- Sorun tek bir bankaya, üye işyerine, servise veya sürüme mi izole?
- Timeout'lar gecikme artışıyla aynı zamanda ve kapsamda mı görülüyor? Hangisi önce başladı?
- Reversal dağılımı değişti mi; mutabakat riski var mı?
- Geçmişte benzer belirtili bir olay yaşandı mı?
- Sırada ne incelenmeli?

Payment Sentinel bu triage sürecini uçtan uca yürütür, ancak **kök neden iddia etmez**. Korelasyon ve benzer geçmiş olaylar yalnızca hipotez olarak raporlanır.

## 2. Neden tek bir LLM çağrısı yetmez?

| İhtiyaç | Tek LLM çağrısı | Payment Sentinel |
|---|---|---|
| Binlerce/milyonlarca ödeme olayı | Bağlama sığmaz, maliyetli | Veri kaynağında toplulaştırma; LLM yalnızca özet görür |
| Doğru oranlar, z-testi, Bonferroni | LLM hesapta hata yapar, sayı uydurabilir | Tüm metrikler Python'da deterministik hesaplanır |
| Sorgunun sonuca göre değişmesi ("banka izole değil → servise bak → sürümü karşılaştır") | Tek atış, geri bildirim yok | Ara bulgulara göre araç seçen çok adımlı akış |
| Farklı uzmanlık alanları (istatistik, ödeme akışı, geçmiş olay, risk) | Tek prompt hepsini aynı anda yapmaya çalışır | Ayrı sorumluluklu uzman agent'lar, paralel yürütme |
| Kurum bilgisi (runbook, geçmiş olaylar) | Modelde yok | Kaynaklı arama araçları |
| Eksik veri / araç hatası | Fark edilmez | Kanıt kalitesi, verifier, takip turu, eksik inceleme bildirimi |
| Halüsinasyon | Kontrol yok | Her iddia bir kanıt referansına (E1, E2…) bağlı olmalı; desteksiz ve kesin neden iddiaları reddedilir |

Bu problem birden çok kaynaktan veri toplamayı, ara sonuçlara göre karar vermeyi ve doğrulamayı gerektirir; bu yüzden agentic bir akışa ihtiyaç duyar.

## 3. Çoklu agent mimarisi

Varsayılan mimaridir (arayüzde **Mimari: Çoklu agent**). Her agent'ın ayrı sorumluluğu, kendi araç izin listesi, Pydantic ile doğrulanan yapılandırılmış çıktısı ve testleri vardır (`src/multiagent/`).

```mermaid
flowchart TB
    U([Talep + kapsam]) --> O[Orchestrator<br/>kapsam, plan, agent seçimi,<br/>bütçe, tur sınırı]
    O -->|dalga 1| AD[Anomaly Detection<br/>oranlar, kodlar, hacim, gecikme,<br/>segment izolasyonu, sürüm]
    O -.->|dalga 1, reversal sorusu: paralel| PF
    AD -->|ara bulgu| O
    O -->|dalga 2, paralel| PF[Payment Flow<br/>reversal sınıflama, correlation,<br/>timeout, mutabakat]
    O -->|dalga 2, paralel| HI[Historical Incident<br/>geçmiş olaylar + runbook'lar]
    PF --> O
    HI --> O
    O -->|dalga 3| IR[Impact & Risk<br/>deterministik risk hesabı]
    IR --> O
    O -->|takip önerisi / çelişki / geçici hata| FU[Hedefli takip turu<br/>en fazla max_rounds]
    FU --> O
    O --> S[Evidence Synthesizer<br/>referans doğrulama, tekilleştirme,<br/>çelişkiler, gerçek/hipotez ayrımı]
    S --> R([Tek, izlenebilir rapor])
    AD & PF & HI -.->|ToolRunner: izin listesi,<br/>bütçe, timeout, retry| T[[12 salt okunur araç]]
    T -.-> EV[(EvidenceStore<br/>E1, E2, …)]
    IR -.-> EV
    S -.-> EV
```

| Agent | Sorumluluk | Araçlar |
|---|---|---|
| **Orchestrator** | Kapsamı kurar (tek agent ile aynı kurallar), agent seçer, bağımsız agent'ları `asyncio` ile paralel çalıştırır, ara bulgulara göre takip turu açar, başarısız agent'ları yönetir, bütçe ve tur sınırını uygular | — |
| **Anomaly Detection** | Teknik hata/red/timeout/gecikme/hacim; banka → servis kırılımı; etkilenen kapsamda yanıt kodları, gecikme-timeout ilişkisi ve sürüm karşılaştırması | `check_data_coverage`, `compare_with_baseline`, `breakdown_by_dimension`, `analyze_response_codes`, `analyze_latency_timeouts`, `compare_service_versions` |
| **Payment Flow** | correlation_id ile reversal sınıflama (her reversal hata değildir), timeout sonrası koruyucu reversal, reversal'sız timeout, üye işyeri izolasyonu, eksik ilişki verisi | `check_data_coverage`, `analyze_reversals`, `breakdown_by_dimension`, `search_transactions` |
| **Historical Incident** | Geçmiş olay eşleşmesi ve gerekçesi, güvenilir eşleşme yoksa açıkça "yok", runbook bölümleri (içerik güvenilmeyen veri) | `search_similar_incidents`, `search_runbooks` |
| **Impact & Risk** | Paylaşılan kanıt üzerinde deterministik risk, etkilenen işlem, kapsam, süre, kanıt kalitesi; parasal kayıp üretmez | (veri sorgusu yok; tekrarlanabilir hesap `assess_impact`) |
| **Evidence Synthesizer** | Kanıt referanslarını doğrular, desteksiz ve kesin neden iddialarını reddeder, bulguları tekilleştirir, çelişkileri kaydeder ve gerekirse hedefli doğrulama ister | — |

**İletişim sözleşmesi** (`src/multiagent/contracts.py`):
- **Ortak bağlam** `InvestigationContext`: `investigation_id`, `user_question`, `time_range`, `filters`, `data_source`, `baseline_definition`, `shared_evidence_references`, `execution_budget`.
- **Uzman çıktısı** `AgentResult` şu alanları taşır:
  - `agent_name`
  - `status` (`success | partial | insufficient_data | failed`)
  - `summary`, `findings`, `supporting_metrics`, `evidence_references`, `hypotheses`, `assertions`
  - `limitations`, `recommended_follow_up`, `errors`
- **Bulgu kuralı:** her bulgu ve hipotez en az bir `E<n>` referansı taşımak zorundadır.

**Gerçeğin kaynağı agent mesajları değil, `EvidenceStore`'daki araç sonuçlarıdır:**
- Birden fazla agent'ın aynı iddiayı tekrarlaması kanıt sayılmaz.
- Var olmayan bir referansa dayanan bulgu reddedilir.

**Yürütme stratejisi:**
1. Talep doğrulanır, pencere ve baseline kurulur.
2. Dalga 1: anomali tespiti. Soru reversal, iade veya mutabakat ile ilgiliyse ödeme akışı agent'ı da aynı dalgada **paralel** çalışır.
3. Dalga 2: ara bulgulara göre ödeme akışı (timeout/reversal sinyali) ve geçmiş olay (anomali varsa) agent'ları **paralel** çalışır. Gereksiz agent'lar gerekçesiyle atlanır.
4. Dalga 3: etki ve risk hesaplanır.
5. Orkestratör kanıtı değerlendirir. Agent takip önerileri, çözülebilir çelişkiler ve geçici hatalar için **hedefli takip turu** açar. Örnek: ödeme akışı teknik reversal'ların M-007'de yoğunlaştığını bulur, orkestratör anomali agent'ını yalnızca M-007 kapsamında yeniden çalıştırır.
6. Synthesizer tek raporu üretir.

**Yapılandırılabilir sınırlar** (`ExecutionBudget`, arayüzde "Yürütme bütçesi"):

| Sınır | Varsayılan |
|---|---|
| `max_rounds` | 2 |
| `max_tool_calls` | 30 |
| `tool_timeout_s` | 10 |
| `agent_timeout_s` | 30 |
| `retries` | 1 |
| `max_parallel` | 3 |

**Hata yönetimi:**
- Başarısız bir agent diğer agent'ların bulgularını geçersiz kılmaz.
- Rapor `incomplete` olur ve eksik analizi açıkça yazar.
- Bir agent'ın çıktısı şemaya uymazsa veya başka bir agent adına sonuç döndürürse o agent başarısız sayılır.
- İzin listesi dışındaki bir aracı çağıran agent engellenir.

**Çelişkiler:**

| Tür | Ne zaman | Sonuç |
|---|---|---|
| İzolasyon | Aynı boyut/metrik için farklı segment iddiası | Hedefli doğrulama turu ister; çözülünce kaydedilir |
| Tespit | Aynı kapsamda farklı karar | Kaydedilir |
| Kanıt kalitesi | Yetersiz örneklemde bulgu raporlanması | Kaydedilir |
| Geçmiş olay kapsamı | Benzer belirti, farklı segment | Kaydedilir |

**Neden yeni bir framework değil?** Projede zaten LangGraph var. Ancak uzmanlar arasındaki görev dağıtımı, paralel dalgalar ve takip turları açık ve test edilebilir olsun diye `asyncio` + Pydantic ile küçük, şeffaf bir orkestrasyon katmanı yazıldı. Model istemcisi ve araçlar enjekte edilir; testler gerçek API çağrısı yapmaz.

**LLM'in rolü (opsiyonel):**
- İlk dalgaya agent önerir. Bu öneri doğrulanır; geçersizse deterministik plan kullanılır.
- Bulgu özeti yazar. Özetteki uydurma sayılar reddedilir.

Uzman agent'ların kararları deterministiktir; her önemsiz işlem için LLM çağrılmaz.

## 4. Tek agent (LangGraph) mimarisi

Alternatif mimaridir (arayüzde **Mimari: Tek agent (LangGraph)**). Tek bir agent, planlayıcı → araç seçici ⇄ executor → verifier döngüsüyle aynı araçları kullanır. İki mimari aynı kapsam kurallarını (`agent.resolve_scope`), araçları, risk motorunu ve rapor üreticisini paylaşır; bu yüzden sonuçları doğrudan karşılaştırılabilir (bkz. Bölüm 16).

```mermaid
flowchart LR
    U([Talep + kapsam]) --> P[parse_request<br/>zaman, banka, kanal, takip sorusu]
    P --> PL[planner]
    PL --> I[investigator<br/>LLM veya politika<br/>sonraki aracı seçer]
    I -->|araç çağrısı| E[executor<br/>izin listesi, Pydantic,<br/>timeout, sınırlı retry,<br/>çıktı şeması]
    E --> I
    I --> V[verifier<br/>zorunlu kanıt]
    V -->|eksik, sınırlı tekrar| I
    V --> S[summarizer<br/>LLM bulgu özeti]
    S --> R[reporter<br/>risk + 14 bölümlü rapor<br/>+ tutarlılık kontrolü]
    E -.-> T[[12 salt okunur araç]]
    T -.-> PR[(PaymentEventRepository<br/>synthetic / Elasticsearch)]
    T -.-> IR[(HistoricalIncidentRepository)]
    T -.-> RR[(RunbookRepository + BM25)]
    T -.-> A[analytics/<br/>deterministik dedektörler]
```

| Katman | Modül |
|---|---|
| Alan kataloğu | `src/domain.py`: yanıt kodları (ISO-8583 tarzı), alanlar, bankalar, servisler |
| Veri erişimi | `src/repositories/`: `PaymentEventRepository`, `HistoricalIncidentRepository`, `RunbookRepository` |
| Analitik | `src/analytics/`: dedektörler, kanıt kalitesi, yapılandırılabilir eşikler (`config/thresholds.toml`) |
| Araçlar | `src/tools.py`: 12 araç, Pydantic argüman/çıktı şemaları (`src/schemas.py`) |
| Orkestrasyon | `src/agent.py` (LangGraph) + `src/policy.py` (kanıta dayalı sonraki adım) |
| Bilgi | `src/incident_matcher.py`, `src/retriever.py` |
| Risk ve rapor | `src/risk_engine.py`, `src/report.py` |
| Güvenlik | `src/redaction.py`, `src/validation.py` |
| Arayüz | `app.py` (Streamlit) |

**LLM'in görevleri:** talebi yorumlamak, plan yazmak, araç seçmek ve bulgu özeti yazmak.

**LLM'in görevi olmayanlar:** sayı hesaplamak, anomali tespit etmek, risk puanı vermek, neden doğrulamak.

LLM yoksa veya hata verirse aynı araçlar deterministik politikayla çalışır. LLM modunda da politikanın önerisi `[politika önerisi]` mesajı olarak verilir.

## 5. Veri modeli ve sentetik senaryolar

Olay şeması: `transaction_id, timestamp, merchant_id, bank_id, terminal_id, payment_method, transaction_type (authorization|sale|reversal|refund), transaction_status (approved|declined|error|timeout), response_code, processing_time_ms, service_name, service_version, channel (POS|ECOM), correlation_id, data_label="synthetic"`.

**Gerçekçi ilişkiler:**
- Durum yanıt kodundan türetilir:
  - `00` onay
  - `05/14/51/54/55/57` iş reddi
  - `91/96` teknik hata
  - `68` timeout
- Timeout'ların işlem süresi istemci timeout süresine (~10 sn) yığılır.
- Reversal'lar orijinal işlemin `correlation_id`'sini taşır:
  - POS timeout'larının çoğu koruyucu reversal alır.
  - Onaylı satışların küçük bir kısmı meşru olarak iptal edilir (void).
  - Az sayıda orphan reversal vardır.
- Kanal servisi belirler: POS → `pos-gateway`; e-ticaret kart → `payment-orchestrator`; cüzdan → `ecom-gateway`.
- Banka başına farklı gecikme profili vardır ve sabah trafik rampası uygulanır.

**Zaman yapısı:**
- Veri iki gün üretilir: D-1 ve D, ikisi de 06:00-12:00 UTC.
- D-1 her senaryoda normaldir ve gün içi sezonsallık için "dünün aynı penceresi" referansıdır.
- Seed sabittir (`SEED=42`).
- Veri bellekte üretilir ve repoya eklenmez. İncelemek için: `python -m src.data_generator --export` (çıktı `data/generated/`).

| Senaryo | İçerik | Beklenen (etiket) |
|---|---|---|
| `normal_baseline` | Normal trafik | Tespit yok, LOW |
| `response_code_spike` | 11:10'dan sonra BANK_C'de 05 reddi | Red artışı, 05 yoğunlaşması, BANK_C izole |
| `bank_timeout` | 11:15'ten sonra yalnızca BANK_B'de 68/91 ve gecikme | Teknik hata + timeout, BANK_B izole, 68, gecikme, korelasyon; HIGH |
| `latency_then_failures` | pos-gateway gecikmesi 11:00'de, timeout'lar 11:20'de | Gecikme, timeout, pos-gateway izole, korelasyon (gecikme önce) |
| `reversal_anomaly` | M-007'de onay sonrası otomatik reversal ve orphan'lar | Reversal anomalisi, M-007 izole |
| `version_regression` | payment-orchestrator 2.4.0 canary'de 96 (10:45 deploy) | Servis izole, 96, sürüm farkı (hipotez) |
| `low_volume` | Çok düşük hacim, birkaç hata yüksek oran gibi görünür | **Kanıt yetersiz**, INDETERMINATE |
| `normal_fluctuation` | Daha yüksek sabah zirvesi, aşırı dağılım, kısa gürültü | Yüksek alarm yok |
| `volume_drop` | 11:10'dan sonra hacim ~%60 düşer, hata oranı normal | Hacim anomalisi (dünün aynı penceresine göre), tahmini işlenmeyen işlem; MEDIUM |
| `data_quality_gap` | 11:20'den sonra olayların bir kısmında `bank_id`/sürüm/`correlation_id` boş; 11:35-11:50 arası hiç kayıt yok | Anomali iddia edilmez; kanıt kalitesi **limited**, eksik kapsam ve boş alanlar raporlanır |

Etiketler `src/data_generator.py` içindeki `SCENARIO_LABELS`'tadır ve değerlendirmede kullanılır.

## 6. Deterministik tespit

Her tespit aynı alanları döner:
- Metrik, gözlenen ve baseline değer
- Mutlak ve göreli sapma
- Pencere, baseline penceresi ve kapsam
- Örneklem (güncel/baseline)
- Kanıt kalitesi (`sufficient/limited/insufficient`)
- İstatistik, yöntem ve kural
- Sınırlamalar
- İddia türü (`observation/correlation/hypothesis`)

Oranların paydası birincil işlemlerdir (authorization + sale). Gecikme yüzdelikleri timeout'lar hariç hesaplanır.

| Dedektör | Yöntem (varsayılan eşikler) |
|---|---|
| A. `TECHNICAL_FAILURE_SPIKE` | error + timeout oranı; iki oran z-testi. Δ ≥ 1 puan, ≥ 2x, z ≥ 3, fazladan ≥ 10 |
| `DECLINE_RATE_SPIKE` | İş reddi oranı ayrı izlenir (teknik hatayla karıştırılmaz). Δ ≥ 2 puan, ≥ 1.3x, z ≥ 3 |
| `TIMEOUT_RATE_SPIKE` | Δ ≥ 0.5 puan, ≥ 2x, z ≥ 3, fazladan ≥ 8 |
| B. `RESPONSE_CODE_CONCENTRATION` | Kod bazında oran: adet ≥ 15, z ≥ 3, ≥ 3x veya Δ ≥ 2 puan; ardından boyut kırılımı |
| `SEGMENT_ISOLATION` | Her segment hem **diğer segmentlerle (etkilenen/etkilenmeyen)** hem kendi baseline'ıyla karşılaştırılır. **Bonferroni düzeltmeli** z (α=0.001/k). Tek segment hem anlamlı olmalı hem fazladan olayların ≥ %60'ını taşımalı |
| C. `LATENCY_TIMEOUT_CORRELATION` | 5 dk dilimlerde Spearman rho ≥ 0.6. Gecikme ve hata başlangıçları karşılaştırılır (hangisi kaç dk önce). Her zaman "korelasyon, nedensellik değil" |
| `LATENCY_INCREASE` | p95 değişimi ≥ %50 (timeout'lar hariç) |
| D. `REVERSAL_ANOMALY` | Reversal'lar `correlation_id` ile bağlanır ve sınıflanır: meşru iptal, hata/timeout sonrası koruyucu, onaydan ≤ 60 sn sonra otomatik, orphan ve correlation_id boş olduğu için **bağlanamayan** (veri kalitesi; anomali sayılmaz). Yalnızca **açıklanamayan teknik** reversal'lar (otomatik + orphan) sayılır. Mutabakat riski göstergesi ayrıca üretilir |
| E. `SERVICE_VERSION_DEGRADATION` | Aynı penceredeki sürümlerin eşzamanlı karşılaştırması (zaman etkisini dengeler). Deployment zamanı = sürümün ilk görüldüğü an. Her zaman **hipotez** |
| F. `VOLUME_ANOMALY` | Referans dünün aynı penceresidir (sezonsallık). D-1 yoksa önceki pencere kullanılır ve bu sınırlama olarak yazılır. ≤ -%30 / ≥ +%50 ve \|z\| ≥ 3 |
| G. Kanıt kalitesi | n < 50 → `insufficient`: tespit **asla** `detected=True` olmaz. n < 200, eksik zaman kapsamı (veri sınırları ve yeterli hacimde boş 5 dk dilimler) veya anahtar alanlarda boş değer oranı > %2 → `limited` |

Eşikler `src/analytics/thresholds.py` içinde belgelenmiştir ve `config/thresholds.toml` ile değiştirilir. Arayüzde iş kuralı yoktur.

## 7. Araçlar ve kanıta dayalı inceleme politikası

| Araç | Görev |
|---|---|
| `check_data_coverage` | Kayıt sayısı, zaman kapsamı, D-1 varlığı, alan boş değer oranları, kanıt kalitesi |
| `get_payment_metrics` | Hacim, onay/red/teknik/timeout oranları, p50/p95/p99 |
| `compare_with_baseline` | A + F dedektörleri, dakika başı hacim, başlangıç ve süre |
| `breakdown_by_dimension` | Banka, üye işyeri, servis, sürüm, ödeme yöntemi, kanal veya terminal kırılımı + izolasyon testi |
| `analyze_response_codes` | B dedektörü ve istenirse en olağandışı kodun boyut kırılımı |
| `analyze_latency_timeouts` | C dedektörü (etkilenen kapsamda) |
| `analyze_reversals` | D dedektörü ve mutabakat riski |
| `compare_service_versions` | E dedektörü |
| `search_transactions` | Sayfalı, en fazla 50, **maskelenmiş** örnek kayıt |
| `search_similar_incidents` | İmzayı deterministik araç çıktılarından kurar ve geçmiş olaylarla karşılaştırır |
| `search_runbooks` | BM25, `doküman › bölüm` kaynaklı |
| `submit_findings` | Bulgu özeti (rapordan önce doğrulanır) |

`src/policy.py` sonraki aracı **ara sonuçlara göre** seçer:

1. Kapsam yetersizse inceleme erken biter.
2. Teknik hata veya timeout artışı varsa banka kırılımı yapılır.
3. Banka izole değilse servis kırılımı yapılır. İzole servis varsa sürümler karşılaştırılır.
4. Red artışı varsa yanıt kodu + banka yoğunlaşması incelenir.
5. Timeout veya gecikme varsa **etkilenen kapsamda** korelasyon analizi yapılır.
6. Reversal sinyali, timeout veya talepte reversal geçiyorsa sınıflandırma yapılır. Gerekirse üye işyeri kırılımı ve segment içi tekrar eklenir.
7. Anomali varsa örnek işlemler, geçmiş olaylar ve runbook'lar getirilir.

Her kararın gerekçesi izlerde görünür, örneğin: "Sorun tek bir bankaya izole değil; teknik hata artışının service_name boyutunda…". Modelin iç muhakemesi gösterilmez.

Deterministik modda örnek yollar (seed 42):

| Senaryo | Araç yolu |
|---|---|
| `normal_baseline`, `low_volume` | coverage → baseline → submit |
| `response_code_spike` | coverage → baseline → **yanıt kodları (banka)** → işlemler → incident → runbook → submit |
| `bank_timeout` | coverage → baseline → **banka kırılımı (BANK_B izole)** → kodlar (BANK_B) → gecikme/timeout (BANK_B) → reversal → … |
| `latency_then_failures` | … → banka kırılımı (izole değil) → **servis kırılımı (pos-gateway)** → kodlar → gecikme/timeout → **sürüm** → reversal → … |
| `version_regression` | … → banka → servis (payment-orchestrator) → kodlar → **sürüm (2.4.0)** → … |
| `reversal_anomaly` | coverage → baseline → **reversal** → **üye işyeri kırılımı (M-007)** → reversal (M-007) → … |

**Sınırlar:**
- `MAX_TOOL_CALLS=16`
- Verifier tekrarı en fazla 2
- Geçici hatada araç başına 1 tekrar
- Araç timeout'u 10 sn
- LangGraph `recursion_limit=60`

## 8. Geçmiş incident eşleştirme

`data/incidents.json` içinde 10 sentetik olay vardır. Her kayıt kapsam, metrikler, yanıt kodları, `contributing_factors[{factor, established}]`, çözüm adımları ve sonuç içerir.

Benzerlik puanı yapısal özelliklerle metin benzerliğinin ağırlıklı birleşimidir:

| Sinyal | Ağırlık |
|---|---|
| Tespit tipleri (Jaccard) | 0.30 |
| Etkilenen kapsam (boyut + değer) | 0.20 |
| Yanıt kodları (Jaccard) | 0.20 |
| Metrik büyüklükleri (log ölçekli yakınlık) | 0.10 |
| Metin benzerliği (BM25, belirti özeti ↔ olay özeti) | 0.20 |

- Eşik 0.55'tir. En az bir ortak tespit tipi gerekir; yoksa eşleşme **uydurulmaz**.
- Her eşleşmede "neden benzer" ve "farklar" listeleri döner.
- Eşleşme `similar_symptom` olarak etiketlenir. Geçmiş kayıttaki doğrulanmış faktörler **o olay** için doğrulanmıştır, bu olay için değil.

## 9. Runbook araması

- `data/runbooks/` altındaki dokümanlar: POS timeout, bankaya özgü red artışı, reversal anomalisi, yüksek gecikme, deployment incelemesi, veri kalitesi ve mutabakat.
- Dokümanlar Markdown başlıklarına göre bölünür ve BM25 ile aranır. Model indirme gerekmez ve sonuçlar deterministiktir.
- Her öneri `doküman › bölüm` kaynağı taşır.
- Doküman ve log içeriği **güvenilmeyen veri**dir:
  - Talimat satırları ayıklanır ve işaretlenir. `data_quality_reconciliation.md` dosyasına test için bilerek bir enjeksiyon satırı konmuştur.
  - Sistem prompt'u bu içeriklerdeki talimatlara uyulmamasını söyler.
  - Araç izinleri değişmez.

## 10. Risk ve etki değerlendirmesi

`src/risk_engine.py` LLM'den bağımsızdır. Skor, tespit edilen sinyallerin puan toplamıdır (`min(100, Σ)`):

| Faktör | Puan (varsayılan) |
|---|---|
| Teknik hata Δ puan | ≥4: +25 · ≥2: +15 · ≥1: +8 |
| Red Δ puan | ≥8: +20 · ≥2.5: +15 |
| Etkilenen işlem (fazladan teknik/red + teknik reversal + hacim düşüşünde tahmini işlenmeyen işlem) | ≥150: +20 · ≥40: +12 · ≥15: +5 |
| p95 değişimi % | ≥200: +15 · ≥50: +8 |
| Süre (dk) | ≥30: +10 · ≥15: +5 |
| Teknik reversal | ≥30: +20 · ≥10: +12 |
| Mutabakat riski göstergesi | HIGH: +10 · MEDIUM: +5 |
| Hacim düşüşü % | ≥50: +25 · ≥30: +15 |
| İzole segment içi teknik hata oranı | ≥%10: +10 · ≥%5: +5 |
| Kapsam | izole: +5 · yaygın: +10 |

**Seviye:**
- ≥ 60 HIGH, ≥ 30 MEDIUM, aksi LOW.
- Kanıt kalitesi `limited` ise seviye en fazla MEDIUM olur.
- Kanıt kalitesi `insufficient` ise seviye `INDETERMINATE` olur ve skor üretilmez.

**Güven ayrı raporlanır.** Araç hatası veya eksik kanıt güveni düşürür ama skoru değiştirmez.

**Parasal etki hesaplanmaz.** Veride güvenilir tutar alanı ve iş kuralı yoktur; etki yalnızca işlem sayısıyla verilir. Ağırlıklar demo amaçlıdır ve `RiskThresholds` ile değiştirilebilir.

## 11. Rapor

Her inceleme 14 bölümlü bir rapor üretir (Markdown + JSON indirme):

1. Soru ve zaman penceresi
2. Veri kaynağı ve kapsamı
3. Yönetici özeti
4. Temel metrikler ve baseline
5. Tespit edilen paternler (tüm alanlarıyla)
6. Etkilenen banka, üye işyeri ve servisler
7. Yanıt kodları
8. Gecikme, timeout, reversal ve sürüm bulguları
9. Geçmiş incident'lar
10. Runbook'lar
11. Risk ve gerekçesi (formül dahil)
12. Önerilen adımlar (kaynaklı)
13. **Doğrulanmış gerçekler (G1, G2… her biri `adım N: araç`) / doğrulanmamış hipotezler (dayanak gerçek kimlikleriyle) / doğrulanmış nedenler (her zaman "yok")**
14. Sınırlamalar ve eksik kanıt

Rapor durumu `complete`, `incomplete` (kanıt alınamadı) veya `insufficient_evidence` olur.

**Tutarlılık kontrolü:**
- Temel metrikler araç çıktısıyla birebir karşılaştırılır.
- Gerçeklerdeki her sayı araç çıktılarında bulunmalıdır.
- Her öneri ve hipotez bir kaynağa dayanmalıdır.
- LLM bulgularında uydurma sayı veya "kök neden doğrulandı" türü iddia varsa bulgu reddedilir.

## 12. Veri erişimi ve Elasticsearch

`PaymentEventRepository` yalnızca toplulaştırılmış sonuç döner: `summary`, `histogram`, `group_by`, `top_terms`, `reversal_linkage`, `field_quality`, `bounds`. Ham kayıt yalnızca `sample` ile alınır (sayfalı, ≤ 50, maskelenmiş).

Taban sınıf şu sınırları uygular:
- Pencere en fazla 24 saat
- En fazla 500 zaman dilimi
- En fazla 20 grup
- Alan izin listesi
- Filtre değerleri `^[A-Za-z0-9_.-]{1,64}$` desenine uymalı

**LLM'e hiçbir zaman ham olay kümesi gönderilmez.**

- **Sentetik depo (varsayılan):** pandas, zaman sıralı veride `searchsorted` ile pencere keser.
- **Elasticsearch/OpenSearch adaptörü** (`src/repositories/elasticsearch.py`):
  - Sunucu tarafında `range/term` filtreleri, `filter` ve `percentiles` agregasyonları, `date_histogram` ve `missing` kullanılır.
  - Sayfalama `search_after` iledir.
  - Her sorguda sunucu `timeout` ve istemci `request_timeout` vardır.
  - `reversal_linkage` iki adımlı ve `MAX_REVERSAL_LINKS` ile sınırlıdır; sınır aşılırsa `truncated` işaretlenir.
  - Bağlantı hatası ve sunucu timeout'u `DataSourceUnavailable`, beklenmeyen yanıt `MalformedResponseError` olur.

> ⚠️ Elasticsearch adaptörü **canlı bir kümede doğrulanmamıştır**. Query DSL ve yanıt ayrıştırma sahte istemciyle test edilir. Kullanmak için `pip install -r requirements-es.txt` ve `.env` içinde `DATA_SOURCE=elasticsearch`, `ES_URL`, `ES_INDEX` gerekir. Beklenen mapping: `timestamp` (date), kimlik/kategori alanları (keyword), `processing_time_ms` (numeric).

## 13. Güvenlik ve dayanıklılık

- Gizli bilgiler `.env` dosyasında tutulur ve git'e eklenmez. `.env.example` yalnızca yer tutucu içerir.
- **Salt okunur:** araçlar arasında yazma, iade, reversal veya engelleme yoktur. Bu bir testle doğrulanır. Agent'ın shell/SQL yetkisi yoktur.
- Araç argümanları Pydantic ile doğrulanır (`extra="forbid"` filtreler, Literal boyutlar). Güvenli sorgu yalnızca izinli alan ve değer desenleriyle kurulur.
- Maskeleme (`src/redaction.py`):
  - PAN benzeri rakam dizileri ve e-postalar maskelenir.
  - `terminal_id`'nin yalnızca son 2 karakteri görünür.
  - Log'lara `RedactingFilter` uygulanır.
- Hata yönetimi:

| Durum | Davranış |
|---|---|
| Geçersiz zaman, filtre, boyut | `invalid_args`; LLM'e düzeltme ipucuyla döner |
| Geçici hata veya timeout | 1 tekrar |
| Kalıcı araç hatası | Alternatif araç (ör. `breakdown_by_dimension` → `analyze_response_codes` boyut kırılımı) veya rapor `incomplete` olur ve güven düşer |
| Şema dışı araç çıktısı | Reddedilir ve kalıcı hata sayılır |
| LLM erişilemez / hata | Deterministik politika devam eder; rapor ve arayüzde belirtilir |
| LLM talepte olmayan banka/servis uydurur | Yok sayılır ve izlerde gösterilir |
| Kapsam dışı talep | İnceleme yapılmaz |
| Belirsiz talep ("bir sorun var gibi") | Varsayılan 60 dk pencere + 120 dk baseline; varsayım raporda yazılır |
| Uzman agent başarısız / zaman aşımı / geçersiz çıktı | Diğer agent'ların bulguları korunur; rapor `incomplete`, eksik analiz açıkça yazılır |

## 14. Teknolojiler ve seçim gerekçeleri

| Teknoloji | Neden |
|---|---|
| **Python 3.12+** (3.14.7 ile test edildi) | Veri analizi ve LLM ekosistemi; `tomllib`, modern tip sözdizimi |
| **asyncio + Pydantic (çoklu agent)** | Paralel uzman agent'lar, timeout ve bütçe kontrolü; ağır bir framework eklemeden açık, test edilebilir görev dağıtımı |
| **LangGraph** (tek agent) | `StateGraph`, koşullu kenarlar, adım limiti ve checkpointer (hafıza) hazır |
| **LangChain Core** | `bind_tools` ve `with_structured_output` ile sağlayıcıdan bağımsız araç çağırma |
| **Ollama + qwen3.5:4b** (varsayılan) / **Gemini** (opsiyonel) | Yerel ve ücretsiz çalışma; daha güçlü araç seçimi için Gemini `.env` ile seçilebilir |
| **pandas / numpy** | Sentetik depoda toplulaştırma, yüzdelikler, zaman dilimleri |
| **Pydantic v2** | Araç argüman/çıktı şemaları, agent iletişim sözleşmesi, rapor modeli |
| **rank-bm25** | Runbook ve olay metninde kod/banka/servis adlarıyla deterministik, kaynaklı arama; model indirme gerektirmez |
| **Streamlit** | Hızlı, okunaklı arayüz; canlı ilerleme (`st.status`), sekmeler, indirme |
| **Elasticsearch istemcisi** (opsiyonel) | Büyük veride sunucu tarafı agregasyon için adaptör (canlı kümede doğrulanmadı) |
| **pytest** | Sahte LLM ve sahte agent'larla deterministik testler |
| **Playwright** (yalnızca geliştirme) | Ekran görüntülerini çalışan uygulamadan almak |

## 15. Kurulum ve çalıştırma

**Gereksinimler:** Python 3.12+ (3.14.7 ile test edildi). LLM modu için isteğe bağlı olarak [Ollama](https://ollama.com) veya Gemini API anahtarı.

```bash
git clone https://github.com/ingun/payment-sentinel.git
cd payment-sentinel
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env          # macOS/Linux: cp .env.example .env
streamlit run app.py
```

`http://localhost:8501` adresinde bir senaryo seçip **İncelemeyi başlat**'a basın. Deterministik mod için kenar çubuğunda LLM sağlayıcıyı `none` yapın veya `.env` içinde `LLM_PROVIDER=none` ayarlayın. LLM modu için `ollama pull qwen3.5:4b` çalıştırın.

> **Windows Smart App Control / uygulama denetimi:** Bu politika `uuid_utils` paketinin DLL'ini (langchain_core ve langsmith kullanır) engelleyebilir; hata `DLL load failed while importing _uuid_utils` olur. Proje bu durumda otomatik olarak saf Python bir UUIDv7 yedeğine geçer (`src/_uuid_compat.py`) ve log'a bir uyarı yazar; analitik sonuçları etkilenmez. `pytest.ini`, kullanılmayan ve aynı DLL'i erkenden yükleyen langsmith pytest eklentisini devre dışı bırakır.

Mimari kenar çubuğundan seçilir: **Çoklu agent** (varsayılan) veya **Tek agent (LangGraph)**. Çoklu agent bütçesi (tur, araç çağrısı, paralellik, agent zaman aşımı) "Yürütme bütçesi" bölümünden değiştirilir. Python'dan çalıştırmak için:

```python
from src.multiagent import Orchestrator
from src.repositories import SyntheticPaymentRepository
from src.retriever import RunbookRetriever
from src.tools import PaymentToolkit

out = Orchestrator(PaymentToolkit(SyntheticPaymentRepository.from_scenario("bank_timeout"), RunbookRetriever())).run(
    "Son 1 saatteki POS başarısızlıklarındaki artışı araştır.")
print(out["report_markdown"])
```

**3 dakikalık demo:**
1. `bank_timeout` (çoklu agent): **Agent'lar** sekmesinde anomali agent'ı BANK_B'yi izole eder. Ardından ödeme akışı ve geçmiş olay agent'ları **eşzamanlı** çalışır. Birleştirilmiş bulgular E1, E2… referanslarıyla listelenir.
2. `reversal_anomaly` (çoklu agent): ödeme akışı sorusu olduğu için anomali ve ödeme akışı agent'ları ilk dalgada paralel çalışır. Ödeme akışı M-007'yi bulur ve orkestratör **takip turu 2**'de anomali agent'ını yalnızca M-007 için çalıştırır. Geçmiş olay farklı üye işyerinde olduğu için çelişki olarak kaydedilir.
3. `latency_then_failures`: banka izole değil → servis → gecikmenin timeout'lardan önce başladığı (korelasyon uyarısıyla).
4. `low_volume`: "Kanıt yetersiz", INDETERMINATE; ödeme akışı ve geçmiş olay agent'ları gerekçesiyle atlanır.
5. Hata simülasyonu `search_runbooks: kalıcı hata`: rapor `incomplete`, risk aynı, güven LOW.
6. Aynı oturumda "Şimdi sadece BANK_B'ye bak": takip sorusu önceki kapsamı kullanır.

## 16. Testler ve değerlendirme

```bash
python -m pytest -q
python -m pytest --cov=src --cov=app --cov-branch   # kapsam (pip install -r requirements-dev.txt)
python -m scripts.evaluate      # → benchmarks/eval_results.md
python -m scripts.benchmark     # → benchmarks/results.md
python -m scripts.llm_eval      # gerçek LLM (Ollama + qwen3.5:4b) ile iki mimari → benchmarks/llm_eval.md
```

Son çalıştırma: **169 test geçti** (~55 sn, gerçek LLM çağrısı yok; LLM sahte istemciyle test edilir, gerçek LLM ayrıca `llm_eval` ile ölçülür). Satır + dal kapsamı **%94** (`src/` ve `app.py`; arayüz %87).

| Dosya | Kapsam |
|---|---|
| `test_data.py` | Üretici determinizmi, şema, correlation bağları, filtre doğrulama, depo sınırları, sayfalama ve maskeleme, reversal sınıflama |
| `test_detectors.py` | Her dedektör, Bonferroni izolasyon, korelasyon (gecikme önce), reversal ayrımı, sürüm hipotezi, hacim (D-1 / trailing), **normal senaryolarda kayan pencerelerde alarm yok**, yetersiz örneklem, eşik yapılandırması |
| `test_tools.py` | Geçersiz girdiler (ters/24 sa üstü zaman, enjeksiyon değeri, bilinmeyen alan/boyut/araç, limit), salt okunur araç kümesi, hata enjeksiyonu, imzanın araç çıktılarından kurulması |
| `test_knowledge.py` | Beklenen incident eşleşmeleri ve gerekçeleri, eşleşme uydurulmaması, runbook kaynakları, enjeksiyon ayıklama |
| `test_risk_report.py` | Senaryo seviyeleri, skor = faktör toplamı, monotonluk, `limited` sınırı, güvenin skordan ayrı olması, 14 bölüm, rapor-araç tutarlılığı ve tahrifatın yakalanması, LLM bulgu doğrulaması, maskeleme |
| `test_agent.py` | **Senaryoya göre farklı araç yolları**, karar gerekçeleri, geçici/yavaş/kalıcı/şema dışı araç hataları, alternatif araç, LLM erişilemez, LLM'in uydurduğu kapsam, eksik kanıtın tamamlanması, araç çağrısı sınırı, "bu sabah" ayrıştırma, takip sorusu |
| `test_elasticsearch.py` | DSL, `search_after`, ayrıştırma, erişilemeyen/zaman aşımı/bozuk yanıt, sınırlı reversal bağlantısı (sahte istemci) |
| `test_multiagent.py` | Soruya ve ara bulgulara göre görev dağıtımı, **paralel yürütme** (zaman damgası örtüşmesi ve duvar saati), **ara bulguyla tetiklenen takip turu**, çelişki → hedefli doğrulama, agent zaman aşımı ve kısmi hata (diğer bulgular korunur), geçersiz yapılandırılmış çıktı, izin listesi dışı araç, araç/tur bütçesi, desteksiz ve kesin neden iddialarının reddi (agent'lar aynı şeyi söylese de), sentez-kanıt tutarlılığı, sahte LLM seçimi, iki senaryolu uçtan uca test |
| `test_scenarios.py` | **Senaryo matrisi:** 10 senaryonun her biri iki mimaride etiketlerle (beklenen tespitler, etiket dışı tespit yok, seviye, kanıt yetersizliği, kanıt kalitesi) karşılaştırılır; iki mimarinin aynı riski üretmesi; veri kalitesi (boş alan, veri boşluğu, bağlanamayan reversal) ve hacim düşüşü uçtan uca |
| `test_app.py` | Streamlit arayüzü (AppTest, başsız): çoklu ve tek agent, takip turunda doğru kapsamlı KPI'lar, kanıt yetersizliği, araç hatasının gösterilmesi |
| `test_infra.py` | LLM sağlayıcı seçimi (anahtar yok, Ollama erişilemez, model yok; ağ çağrısı yok), depo fabrikası, JSONL yükleme, log maskeleme filtresi, saat aralığı ve pencere kenar durumları |
| `test_uuid_compat.py` | Engellenen `uuid_utils` için yedek UUIDv7 üreticisi |

**Etiketli değerlendirme** (`python -m scripts.evaluate`, 10 senaryo × 3 seed, deterministik mod, her iki mimari; tam tablo `benchmarks/eval_results.md`):

| Ölçüt | Tek agent (LangGraph) | Çoklu agent |
|---|---|---|
| Tespit precision / recall | 1.000 / 0.985 | 1.000 / 0.985 |
| Doğru risk seviyesi | 29/30 | 29/30 (ikisinde de `bank_timeout` seed 123: MEDIUM, beklenen HIGH) |
| Normal senaryolarda tespit içeren inceleme | 0/6 | 0/6 |
| Kayan 60 dk pencerelerde alarm (normal senaryolar) | 0/66 | (aynı dedektörler) |
| Yetersiz kanıt doğru işaretlendi | 3/3 (yanlış: 0/27) | 3/3 |
| Beklenen kanıt kalitesi (`data_quality_gap`: limited) | 3/3 | (senaryo matrisi testinde doğrulanır) |
| Hata enjeksiyonlu çalıştırmada rapor üretildi | 9/9 (3'ü `incomplete`) | 6/6 (4'ü `incomplete`) |
| Rapor-araç (ve risk agent'ı) tutarlılık kontrolü | 39/39 | 36/36 |
| Paralel agent dalgası içeren inceleme | — | 12/30 (`bank_timeout`, `latency_then_failures`, `reversal_anomaly`, `data_quality_gap`) |
| Takip turu açılan inceleme | — | 3/30 (`reversal_anomaly`) |
| Ort. araç çağrısı | 7.4 | 6.1 |
| Süre medyan / p95 (inceleme başına) | 381 / 502 ms | 405 / 577 ms |

İki mimari aynı deterministik araçları ve risk motorunu kullandığı için tespit ve risk sonuçları aynıdır. Çoklu agent mimarisinin farkı, görevlerin ayrı sorumluluklu agent'lara dağıtılması, bağımsız işlerin paralel yürütülması, ara bulgulara göre hedefli takip turu ve çelişki kaydıdır.

Kaçırılan tek tespit `latency_then_failures` senaryosunun bir seed'inde gecikme-hata korelasyonudur (recall 0.83). Değerlendirme etiketleri üreticinin enjekte ettiği etkileri yansıtır. `acceptable` listesi gerçek ama zorunlu olmayan etkileri (ör. `bank_timeout` içindeki 91 kodu) yanlış pozitif saymaz.

> Bu sonuçlar sentetik senaryolarda ve bu senaryolar için seçilmiş eşiklerle ölçülmüştür; üretim performansını göstermez.

**Gerçek LLM ile değerlendirme** (`python -m scripts.llm_eval`, Ollama + `qwen3.5:4b`, RTX 3050 Ti 4 GB; tam tablo `benchmarks/llm_eval.md`).

Kapsam:
- 10 senaryo etiketli talebiyle çalıştırıldı.
- 3 senaryo ayrıca iki farklı ifadeyle ("…araştır ve normal dönemle karşılaştır", belirsiz "bir sorun var gibi") çalıştırıldı.
- Her mimaride 16 çalıştırma yapıldı.

| Ölçüt | Tek agent (LangGraph) | Çoklu agent |
|---|---|---|
| Aynı talebin LLM'siz sonucuyla aynı risk seviyesi | 16/16 | 16/16 |
| Etiketle uyumlu (beklenen tespitler + seviye) | 10/10 | 10/10 |
| Rapor tamamlandı / kanıt yetersizliği doğru | 16/16 | 16/16 |
| LLM akışı yönetti | 7/16 | 16/16 (LLM agent seçimi geçerli) |
| LLM bulgusu kabul / red | 124 / 1 | 95 / 0 |
| Kapsam dışı talep ("Bana bir şiir yaz") reddedildi | evet | evet |
| Medyan / maks. süre | 72 / 91 sn | 17 / 24 sn |

**Gözlemler:**
- **Tek agent'ta Qwen akışı her zaman sonuna kadar götürmüyor.** Anomali içeren senaryolarda araçları doğru sırayla seçiyor (banka kırılımı → yanıt kodları → gecikme → reversal), ama geçmiş olay ve runbook aramasını yapmadan "bitti" diyor. Verifier'ın somut araç çağrısı içeren hatırlatmasını da her zaman izlemiyor.
  - Bu durumda eksik kanıtı deterministik politika tamamlıyor ve rapor modu "LLM + deterministik tamamlama" olarak yazılıyor. Sonuç seviyesi etkilenmedi.
  - Bu, küçük yerel modelin bir sınırı. Mimari bunu özellikle bu yüzden tolere edecek şekilde tasarlandı.
- **Çoklu agent'ta LLM yalnızca agent seçimi ve özet yazar.** Bu yüzden ~4 kat daha hızlı ve LLM'in akışı yarıda bırakması söz konusu değil.
- **Reddedilen 1 bulgu, araç çıktısında olmayan bir sayı içeriyordu.**
- **LLM ifadelerinin anlamı otomatik doğrulanmıyor.** Değerlendirme sırasında Qwen'in bir sayıyı yanlış etikete bağladığı ("4.61 puanlık z-skor") görüldü. Prompt bu yüzden sıkılaştırıldı ve rapor bu bulguları "sayılar doğrulandı, anlam doğrulanamaz" notuyla gösteriyor.

**Performans** (`benchmarks/results.md`, sentetik bellek içi depo, 1 ısınma + 5 tekrar medyanı):

| Olay | summary 60 dk | histogram 24 sa | group_by banka 24 sa | reversal_linkage 60 dk | compare_with_baseline |
|---|---|---|---|---|---|
| 100.000 | 2.3 ms | 427 ms | 42 ms | 10 ms | 88 ms |
| 1.000.000 | 9.7 ms | 764 ms | 382 ms | 42 ms | 138 ms |

Elasticsearch ölçülmedi.

## 17. Sınırlamalar

- **Veri ve eşikler:** Veriler sentetiktir. Eşikler ve risk ağırlıkları bu senaryolar için seçilmiştir ve kalibre edilmemiştir.
- **Nedensellik:**
  - Korelasyon ve zamansal öncelik nedensellik değildir.
  - Sürüm/deployment ilişkisi hipotezdir.
  - Kök neden hiçbir zaman otomatik doğrulanmaz.
- **Parasal etki:** Tutar alanı olmadığı için parasal etki hesaplanmaz.
- **Reversal bağlantısı:** Lookback penceresi (30 dk) dışında kalan orijinaller orphan görünebilir.
- **Deployment zamanı:** Sürümün veride ilk görüldüğü andan tahmin edilir; değişiklik kaydı entegrasyonu yoktur.
- **Elasticsearch adaptörü:** Canlı kümede doğrulanmamıştır.
- **Hafıza:** Bellek içidir (`InMemorySaver` / oturum geçmişi); uygulama yeniden başlatılınca silinir.
- **Çoklu agent:**
  - Uzman agent'ların kararları deterministik kurallardır.
  - LLM yalnızca ilk dalga agent seçimine ve bulgu özetine katılır.
  - Takip turu sayısı ve tetikleyiciler (agent önerisi, çözülebilir çelişki, geçici hata) sınırlıdır.
  - Çelişki türleri önceden tanımlı dört kuralla sınırlıdır.
- **Küçük yerel model (qwen3.5:4b):**
  - Tek agent modunda ölçülen 16 çalıştırmanın 9'unda akışı geçmiş olay ve runbook aramasından önce bıraktı; eksik kanıt deterministik olarak tamamlandı.
  - Araç argümanlarını hatalı gönderebilir; executor bunu `invalid_args` olarak döndürür.
  - Bulgu özetinde sayıları yanlış metrik adına bağlayabilir; sayılar doğrulanır, anlam doğrulanamaz.
  - Tek agent incelemesi ~1-1,5 dk sürer (yerel GPU'ya bağlı).

## 18. Ekran görüntüleri

Hepsi çalışan uygulamadan `python -m scripts.screenshots --llm` ile alınmıştır (Playwright + Edge). İlk üçü gerçek LLM modundadır (Ollama + qwen3.5:4b); diğerleri tekrarlanabilirlik için deterministik moddadır.

| | |
|---|---|
| ![LLM çoklu agent](screenshots/00_llm_multi_agent.png) **Gerçek LLM (qwen3.5:4b): LLM agent seçimi, paralel ilk dalga, takip turu** | ![LLM tek agent](screenshots/00b_llm_single_agent_trace.png) **Gerçek LLM: tek agent'ta LLM'in araç seçimleri ve verifier'ın tamamlaması** |
| ![LLM bulgular](screenshots/00c_llm_findings.png) **Gerçek LLM bulgu özeti (sayılar doğrulanmış)** | |
| ![Paralel](screenshots/14_multi_agent_parallel.png) **Çoklu agent: plan, paralel dalga, agent durumları, kanıt referansları** | ![Takip](screenshots/15_multi_agent_follow_up.png) **Çoklu agent: ara bulguyla tetiklenen takip turu ve çelişki** |
| ![Tek agent](screenshots/16_single_agent_trace.png) **Tek agent (LangGraph): araç yolu** | |
| ![İzler](screenshots/02_bank_timeout_trace.png) **Araç yolu ve karar gerekçeleri** | ![Segmentler](screenshots/03_bank_timeout_segments.png) **Banka izolasyonu (etkilenen/etkilenmeyen)** |
| ![Gerçekler](screenshots/04_facts_vs_hypotheses.png) **Gerçek / hipotez / neden ayrımı** | ![Incident](screenshots/05_similar_incidents.png) **Geçmiş olay eşleşmesi ve gerekçesi** |
| ![Runbook](screenshots/06_runbooks.png) **Kaynaklı runbook adımları** | ![Gecikme](screenshots/07_latency_then_failures.png) **Gecikme önce, timeout sonra** |
| ![Sürüm](screenshots/08_version_regression.png) **Sürüm karşılaştırması (hipotez)** | ![Reversal](screenshots/09_reversal_anomaly.png) **Reversal sınıflama ve mutabakat riski** |
| ![Yetersiz](screenshots/10_low_volume_insufficient.png) **Kanıt yetersiz** | ![Normal](screenshots/11_normal_fluctuation_no_alarm.png) **Normal dalgalanma: alarm yok** |
| ![Hata](screenshots/12_tool_failure_incomplete.png) **Araç hatası: eksik inceleme** | ![Risk](screenshots/13_risk_explanation.png) **Risk gerekçesi ve formül** |

## 19. Proje yapısı

```
payment-sentinel/
├── app.py                       # Streamlit arayüzü
├── config/thresholds.toml       # analitik ve risk eşikleri
├── src/
│   ├── domain.py                # yanıt kodları, alanlar, kataloglar
│   ├── data_generator.py        # 10 etiketli sentetik senaryo (D-1 + D)
│   ├── repositories/            # PaymentEvent/HistoricalIncident/Runbook depoları; synthetic + Elasticsearch
│   ├── analytics/               # dedektörler, kanıt kalitesi, eşikler
│   ├── metrics.py               # z-testi, Bonferroni, Spearman, fazladan olay
│   ├── tools.py                 # 12 salt okunur araç
│   ├── multiagent/              # contracts.py, runtime.py (EvidenceStore, ToolRunner), agents.py,
│   │                            # orchestrator.py, synthesizer.py
│   ├── policy.py                # kanıta dayalı sonraki adım seçimi (tek agent)
│   ├── agent.py                 # LangGraph orkestrasyonu (tek agent) + ortak kapsam kuralları
│   ├── incident_matcher.py      # yapısal + BM25 benzerlik
│   ├── retriever.py             # runbook BM25, enjeksiyon ayıklama
│   ├── risk_engine.py           # deterministik risk
│   ├── report.py                # 14 bölümlü rapor + tutarlılık kontrolü
│   ├── redaction.py             # maskeleme
│   ├── validation.py, schemas.py, llm.py, config.py
├── data/incidents.json, data/runbooks/*.md
├── scripts/                     # evaluate.py, benchmark.py, llm_eval.py, screenshots.py
├── benchmarks/                  # eval_results.md, results.md
├── docs/ARCHITECTURE.md
└── tests/                       # 169 test
```
