# İşlem Hacmi Düşüşü (Volume Drop)

## Belirtiler
- Dakika başı ödeme girişimi (işlem hacmi) baseline'a göre belirgin düşüş gösterir.
- Hata oranı normal kalabilir; istekler ödeme servislerine hiç ulaşmıyor olabilir.

## Olası Nedenler
- İstemci (mobil/web) tarafında hatalı sürüm veya endpoint değişikliği.
- DNS, CDN veya yük dengeleyici (load balancer) yönlendirme problemi.
- Üye işyeri entegrasyonunda kesinti.
- Beklenen iş kaynaklı düşüş (kampanya bitişi, gece saatleri); teknik olmayan nedenler elenmelidir.

## İnceleme Adımları
- Düşüşün tüm ödeme yöntemlerinde ve kanallarda mı yoksa tek bir kaynakta mı olduğunu kontrol edin.
- api-gateway erişim loglarında gelen istek sayısını ve 4xx oranını kontrol edin.
- Son istemci sürümü, DNS ve CDN değişikliklerini düşüşün başladığı zamanla karşılaştırın.
- Büyük üye işyerlerinin trafiğini ayrı ayrı inceleyin.
- Takvim etkisini elemek için geçen haftanın aynı saatiyle karşılaştırın.
