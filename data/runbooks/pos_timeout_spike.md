# POS Timeout Artışı (68 / 91)

## Belirtiler
- POS kanalında 68 (yanıt zamanında alınamadı) veya 91 (issuer/switch erişilemiyor) yanıt kodlarında ani artış.
- Timeout'a düşen işlemlerde işlem süresinin istemci timeout süresine (~10 sn) yığılması.
- Timeout sonrası terminallerin otomatik reversal göndermesi; reversal oranının timeout'larla birlikte artması.
- Sorunun tek bir bankaya (issuer) veya tek bir servise izole görünmesi.

## Olası Nedenler
- Issuer (banka) tarafında yavaşlama veya kesinti.
- Banka bağlantı hattı, VPN veya HSM tarafında sorun.
- Switch/gateway bağlantı havuzunun dolması.
- Son değişiklik sonrası timeout veya yönlendirme konfigürasyonu.

## İnceleme Adımları
- Timeout artışının bankaya, servise veya terminal grubuna izole olup olmadığını segment kırılımıyla doğrulayın.
- Etkilenen bankanın durum sayfasını ve bankayla iletişim kanalını kontrol edin; olay numarası isteyin.
- Etkilenen segmentte p95 işlem süresini baseline ile karşılaştırın; gecikmenin timeout'lardan önce başlayıp başlamadığına bakın.
- Timeout sonrası reversal'ların gönderildiğini ve reversal'sız timeout işlemi kalmadığını kontrol edin (mutabakat).
- Etki sürüyorsa incident açın ve ödeme on-call ekibini ile banka ilişkileri ekibini bilgilendirin.

## Kaçınılması Gerekenler
- Timeout süresini doğrulama yapmadan artırmayın; terminal kuyruklarını büyütebilir.
- Tek bir bankadaki sorun için tüm trafiği yeniden yönlendirmeyin.
