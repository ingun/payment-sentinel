# Bankaya Özgü Red (Decline) Artışı

## Belirtiler
- 05 (do not honor), 51, 57 gibi iş reddi kodlarında belirli bir bankada artış.
- Teknik hata oranı normalken red oranının yükselmesi.
- Red artışının bir banka, kart tipi veya üye işyeri grubunda yoğunlaşması.

## Olası Nedenler
- Issuer tarafında risk/fraud kuralı değişikliği.
- Issuer'ın belirli üye işyeri kategorisini (MCC) veya kanalı engellemesi.
- Kart programı veya BIN aralığında sorun.
- Issuer sisteminin teknik sorunu iş reddi kodu olarak döndürmesi (05 bazen genel amaçlı kullanılır).

## İnceleme Adımları
- Red artışının tek bir bankaya izole olup olmadığını banka kırılımıyla doğrulayın.
- Artan red kodunun dağılımını üye işyeri, ödeme yöntemi ve kanal bazında inceleyin.
- Etkilenen bankanın son kural veya sistem değişikliği olup olmadığını banka ilişkileri üzerinden sorun.
- Red edilen örnek işlemleri (maskelenmiş) bankayla paylaşarak issuer tarafındaki gerekçeyi isteyin.
- Üye işyerlerine etki yüksekse müşteri destek ve üye işyeri ekiplerini bilgilendirin.

## Kaçınılması Gerekenler
- İş reddini teknik hata gibi otomatik yeniden denemeyin; tekrar denemeler ek red ve fraud sinyali üretebilir.
