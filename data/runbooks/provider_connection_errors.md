# Payment Provider Bağlantı Hataları (PROVIDER_TIMEOUT, CONNECTION_RESET)

## Belirtiler
- provider-connector servisinde PROVIDER_TIMEOUT veya CONNECTION_RESET artışı.
- Hataların tek bir ödeme sağlayıcısına (provider) yönelik çağrılarda yoğunlaşması.
- Diğer servislerin hata oranı normal seyrederken yalnızca provider-connector'ın etkilenmesi.

## Olası Nedenler
- Ödeme sağlayıcısında kesinti veya performans düşüşü.
- TLS sertifikası, DNS veya ağ (network) problemi.
- Sağlayıcı tarafında rate limit uygulanması.

## İnceleme Adımları
- Hataların hangi provider endpoint'ine gittiğini log örneklerinden doğrulayın.
- Sağlayıcının status sayfasını ve duyurularını kontrol edin.
- DNS çözümleme ve TLS el sıkışma sürelerini kontrol edin.
- Alternatif sağlayıcıya yönlendirme (failover) mümkünse on-call onayıyla değerlendirin.
- Sağlayıcıya, örnek transaction ID'leri ve zaman aralığıyla destek kaydı açın.

## Kaçınılması Gerekenler
- Başarısız ödemeleri sağlayıcı durumunu doğrulamadan otomatik tekrar denemeyin; çift tahsilat riski vardır.
