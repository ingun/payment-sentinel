# Yüksek İşlem Süresi (Latency)

## Belirtiler
- p95 / p99 işlem süresinde baseline'a göre belirgin artış.
- Gecikmenin bir servis, banka veya kanal üzerinde yoğunlaşması.
- Gecikme artışını izleyen timeout (68) artışı.

## Olası Nedenler
- Bağımlı servis veya issuer tarafında yavaşlama.
- Veritabanı, kuyruk veya bağlantı havuzu doygunluğu.
- Kaynak (CPU/bellek) yetersizliği veya ölçeklendirme sorunu.
- Son deployment sonrası performans gerilemesi.

## İnceleme Adımları
- Gecikmenin hangi servis veya segmentte başladığını kırılımlarla belirleyin.
- p95 artışının timeout artışından önce mi sonra mı başladığını zaman dilimleriyle karşılaştırın.
- Servisin kaynak kullanımı, bağlantı havuzu ve kuyruk derinliği metriklerini kontrol edin.
- Aynı zaman aralığındaki deployment ve konfigürasyon değişikliklerini listeleyin.
- Gecikme timeout'a dönüşüyorsa POS timeout runbook'unu uygulayın.

## Kaçınılması Gerekenler
- Gecikme ile hata artışının aynı anda görülmesini tek başına nedensellik kanıtı saymayın.
