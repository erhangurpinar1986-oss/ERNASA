# NOVA — iletisim@ernasa.com bağlantısı: kurucunun yapacağı adımlar (ekran ekran)

Şifre veya API anahtarı bu sohbete ya da GitHub'a yazılmaz. Hepsi yalnızca Render ortam değişkenlerine girilir.

## A. Giden e-posta (mevcut altyapı, yeni hesap gerekmez)

NOVA, aday davetlerinde kullanılan göndericiyi (`services/invite_email.py`) kullanır.
1. **dashboard.render.com** › **ERNASA** servisi › sol menü **Environment** sayfasını açın.
2. Şu iki değişkenin bulunduğunu kontrol edin:
   - `ERNASA_EMAIL_PROVIDER` (`brevo` veya `resend`);
   - ilgili anahtar `ERNASA_BREVO_API_KEY` veya `ERNASA_RESEND_API_KEY`.
   Değerleri kopyalamayın, yalnızca varlıklarını kontrol edin.
3. `ERNASA_NOVA_TEST_EMAILS` adlı değişkeni ekleyin. Değeri, kendi test adresiniz olsun (virgülle birden çok adres yazılabilir).
   Politika onayı verilmeden NOVA yalnızca bu adreslere gönderim yapar.
4. Alan adı itibarı için **Brevo** › Senders, Domains & Dedicated IPs › Domains › `ernasa.com` yolunu izleyin. Resend kullanıyorsanız **Resend** › Domains.
   Bu ekranda SPF, DKIM ve DMARC satırlarının **Verified** göründüğünü kontrol edin. DMARC yoksa Namecheap › Domain List › ernasa.com ›
   Advanced DNS bölümüne şu kaydı ekleyin: `TXT  _dmarc  v=DMARC1; p=none; rua=mailto:iletisim@ernasa.com`. İzleme modunda başlayıp
   2–4 hafta sonra `p=quarantine` değerine geçin.

## B. Gelen kutusu: salt okunur okuyucu

NOVA kutuyu yalnızca okur. Kod, IMAP'i salt okunur modda açar; silme, taşıma ve "okundu" işaretleme yapmaz.
İki seçenek var. **Seçenek 1** en az yetkiyi verdiği için önerilir.

**Seçenek 1: ayrı okuma kutusu (önerilen).** NOVA, iletisim@ şifresini hiç görmez ve o adresten gönderim yapamaz.
1. **Namecheap** › Private Email › yönetim paneli › **yeni posta kutusu** oluşturun: `nova-okuyucu@ernasa.com`.
   Ek kutu, paketinize göre ücretli olabilir. Ücret çıkarsa bu adımı onayınız olmadan yapmayın.
2. **privateemail.com** webmail'de `iletisim@ernasa.com` ile oturum açın › **Settings** › **Filters / Forwarding**.
   "Gelen her iletinin kopyasını `nova-okuyucu@ernasa.com` adresine ilet, kopyayı kutuda tut" kuralını ekleyin.
3. Render › ERNASA › **Environment** sayfasına şu değişkenleri ekleyin:
   - `ERNASA_NOVA_IMAP_USER = nova-okuyucu@ernasa.com`
   - `ERNASA_NOVA_IMAP_PASSWORD = (bu yeni kutunun şifresi)`
   - `ERNASA_NOVA_IMAP_HOST = mail.privateemail.com`

**Seçenek 2: doğrudan iletisim@ ile okuma.** Ek ücret yoktur, ancak şifre gönderim yetkisi de taşır.
- Render'a `ERNASA_NOVA_IMAP_USER = iletisim@ernasa.com` ve kutunun şifresini girin.
- Şifreyi daha önce başka yerde kullandıysanız önce Namecheap'ten yenileyin.

## C. Zamanlayıcı (bilgisayarınız kapalıyken çalışması için)

1. 32 karakterden uzun rastgele bir değer üretin (ör. bir parola yöneticisinde).
2. Render › ERNASA › Environment › `ERNASA_NOVA_TICK_SECRET = (değer)`.
3. **github.com/erhangurpinar1986-oss/ERNASA** › Settings › Secrets and variables › Actions › **New repository secret** ile iki secret ekleyin:
   - `NOVA_TICK_SECRET`: Render'daki ile aynı değer.
   - `NOVA_CONTROL_ORIGIN`: CONTROL'ün açıldığı adres (ör. `https://ernasa.com`).
4. Actions › **ERNASA NOVA scheduler tick** › **Run workflow** ile bir kez elle çalıştırın. Günlükte `HTTP 200` görünmelidir.

## D. Kontrol

CONTROL › **NOVA AI SALES CENTER** › Genel bakış › **Harici servis hazırlığı** tablosunda "Giden e-posta", "Gelen kutusu" ve
"Zamanlayıcı" satırları **Hazır** olmalıdır. Ardından CONTROL › NOVA › E-posta sekmesinde şu testi yapın:
1. Sentetik test firmasına taslak hazırlayın ve onaylayın.
2. Ayarlar'da acil durdurmayı kapatıp NOVA'yı başlatın.
3. "Gönderimi çalıştır" düğmesine basın.
4. Test adresinize gelen iletiye "demo istiyoruz" yazarak yanıt verin.
5. Bir sonraki tick'te yanıtın "Gelen yanıtlar" bölümünde **demo** olarak sınıflandığını kontrol edin.
