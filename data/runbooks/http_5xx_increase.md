# HTTP 5xx Artışı (HTTP_500, HTTP_502)

## Belirtiler
- HTTP_500 veya HTTP_502 hata kodlarında artış (5xx server error).
- Belirli bir serviste veya tüm servislerde hata oranının yükselmesi.

## Olası Nedenler
- Yeni sürüm (deployment) kaynaklı uygulama hatası.
- Bağımlı servisin (veritabanı, cache) erişilemez olması.
- HTTP_502 için: ara katmandaki (load balancer, gateway) upstream bağlantı problemi.

## İnceleme Adımları
- 5xx artışının başladığı zamanı son deployment saatiyle karşılaştırın.
- Etkilenen servisin uygulama loglarında stack trace ve exception örneklerini inceleyin.
- Bağımlı servislerin (veritabanı, cache) sağlık kontrollerini kontrol edin.
- Deployment ile korelasyon güçlüyse rollback kararını on-call ekip lideriyle verin.
