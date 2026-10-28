# Gateway Timeout Artışı (GATEWAY_504)

## Belirtiler
- GATEWAY_504 hata kodunda ani artış (gateway timeout).
- acquirer-gateway veya api-gateway üzerinde p95 latency yükselmesi.
- Timeout'a düşen işlemlerde latency değerlerinin istemci timeout süresine (~5000 ms) yığılması.

## Olası Nedenler
- Upstream acquirer (banka) tarafında yavaşlama.
- Bağlantı havuzunun (connection pool) dolması.
- Son deployment sonrası konfigürasyon değişikliği.

## İnceleme Adımları
- acquirer-gateway p95 latency grafiğini son 2 saat için baseline ile karşılaştırın.
- Timeout artışının başladığı zamanı deployment ve konfigürasyon değişiklik kayıtlarıyla eşleştirin.
- Connection pool kullanımını ve bekleyen istek kuyruğunu kontrol edin.
- Acquirer (banka) durum sayfasını ve iletişim kanalını kontrol edin.
- Etki %5 hata oranını aşıyorsa incident açıp on-call ödeme ekibini çağırın.

## Kaçınılması Gerekenler
- Timeout süresini doğrulama yapmadan artırmayın; kuyruğu büyütebilir.
