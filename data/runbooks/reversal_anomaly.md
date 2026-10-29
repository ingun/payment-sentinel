# Reversal Anomalisi

## Belirtiler
- Reversal oranında baseline'a göre artış.
- Onaylanmış işlemlerin saniyeler içinde otomatik geri alınması (terminal yanıtı alamamış olabilir).
- Orijinal işlemi bulunamayan (orphan) reversal kayıtları.
- Artışın belirli üye işyeri veya terminal grubunda yoğunlaşması.

## Olası Nedenler
- Terminal yazılımı veya ağ sorunu nedeniyle yanıtın terminale ulaşmaması.
- Terminal ile gateway arasındaki timeout ayarlarının uyumsuzluğu.
- Mükerrer reversal gönderimi veya correlation bilgisinin kaybolması.
- Meşru iptallerdeki (void) operasyonel artış: ör. kampanya, kasa sorunu.

## İnceleme Adımları
- Reversal'ları orijinal işleme bağlayıp sınıflandırın: meşru iptal, hata/timeout sonrası koruyucu reversal, onay sonrası otomatik reversal ve orphan.
- Teknik reversal'ların üye işyeri, terminal ve banka kırılımını çıkarın.
- Etkilenen terminallerin yazılım sürümünü ve son güncelleme zamanını terminal yönetim sisteminden kontrol edin.
- Onaylı ama geri alınmış işlemler için issuer tarafında da reversal'ın işlendiğini doğrulayın (çift çekim veya askıda bakiye riski).
- Orphan reversal'lar için mutabakat ekibine kayıt açın.

## Kaçınılması Gerekenler
- Her reversal'ı hata kabul etmeyin; meşru iptaller ayrı değerlendirilmelidir.
- Reversal'ları manuel olarak yeniden göndermeyin; mükerrer geri alma oluşabilir.
