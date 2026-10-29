# Servis Deployment İncelemesi

## Belirtiler
- Yeni bir servis sürümünün görülmeye başladığı zamandan sonra hata oranında artış.
- Aynı penceredeki sürümler arasında teknik hata veya 96 (sistem arızası) kodunda fark.
- Canary sürümünün trafiğin bir kısmında farklı davranması.

## Olası Nedenler
- Yeni sürümde kod veya konfigürasyon hatası.
- Sürüme özgü bağımlılık veya şema uyumsuzluğu.
- Deployment ile eş zamanlı başka bir altyapı değişikliği.

## İnceleme Adımları
- Sürümleri aynı zaman penceresinde karşılaştırın; yalnızca önce/sonra karşılaştırmasına dayanmayın.
- Yeni sürümün ilk görüldüğü zamanı değişiklik kayıtları (CAB/CD pipeline) ile eşleştirin.
- Yeni sürümde artan yanıt kodlarının örnek işlemlerini ve uygulama loglarını inceleyin.
- Canary trafiğinin payını ve hangi segmentlere yönlendirildiğini doğrulayın.
- Geri alma (rollback) kararını değişiklik sahibi ekip ve on-call ile birlikte verin.

## Kaçınılması Gerekenler
- Sürüm ile hata arasındaki ilişkiyi doğrulamadan kök neden olarak duyurmayın.
- Otomatik araçlarla rollback yapmayın; bu inceleme aracı salt okunurdur.
