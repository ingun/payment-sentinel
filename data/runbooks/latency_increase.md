# Latency (Gecikme) Yükselmesi

## Belirtiler
- Bir serviste ortalama ve p95 latency değerlerinin baseline'a göre belirgin artması.
- Gecikme artışına eşlik eden timeout (GATEWAY_504) ve 502 hataları.

## Olası Nedenler
- Kaynak tüketimi (CPU, bellek) veya thread havuzu doygunluğu.
- Upstream bağımlılıkta yavaşlama.
- Trafik hacminde ani artış.

## İnceleme Adımları
- Gecikmenin tek bir servise mi yoksa tüm akışa mı yayıldığını servis bazlı p95 ile belirleyin.
- Trafik hacmini baseline ile karşılaştırın; hacim değişmeden gecikme arttıysa kapasite dışı nedenleri araştırın.
- Servisin CPU, bellek ve thread havuzu metriklerini kontrol edin.
- Gerekirse on-call onayıyla geçici ölçeklendirme (scale-out) uygulayın.

## Not
- Gecikme artışı ile hata artışının aynı zamanda görülmesi korelasyondur; nedensellik ayrıca doğrulanmalıdır.
