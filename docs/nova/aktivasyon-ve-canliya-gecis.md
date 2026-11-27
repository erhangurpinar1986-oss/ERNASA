# NOVA 2.0 — Aktivasyon adımları ve canlıya geçiş planı

Kurucu kararları (27.11.2026 19:44):
- 100 firma hedefi canlıya geçiş için zorunlu koşul **değildir**; veri havuzu canlıda büyümeye devam eder.
- Dış iletişim aktivasyonları NOVA'nın güvenli bileşenlerinin canlıya alınmasını engellemez. Dış iletişim, aktivasyon ve hukuki
  kontroller tamamlanana kadar kapalı kalır. Kod tarafında bu varsayılandır: `kill_switch=1`, politika onayları 0, `require_iys=1`.
- Ücretli satın alma ve otomatik bakiye yükleme yapılmaz.

## 1. Canlıya alınacak kısım ve kapalı kalacak kısım

| Bileşen | Canlıda | Not |
|---|---|---|
| CONTROL › NOVA AI SALES CENTER paneli, veri havuzu, içe aktarma, CSV, ret listesi, aşamalar, raporlar | Açık | Yalnızca kurucu oturumuyla |
| Ürün bilgi merkezi ve toplu yönetim onayı | Açık | |
| Sales Hub ataması | Açık | Komisyon kuralları değişmez |
| E-posta gönderimi | **Kapalı** | Politika onayı + İYS + `ERNASA_NOVA_TEST_EMAILS` gerekir |
| Gelen kutusu okuma | **Kapalı** | IMAP değişkenleri girilene kadar |
| Sesli arama | **Kapalı** | Vapi + TR SIP değişkenleri girilene kadar |
| Zamanlayıcı | **Pasif** | GitHub secret'ları girilene kadar |
| ernasa.com HR FLOW tanıtım metni | Güncellendi | Satıştaki ve kodla doğrulanan özellikler; “geliştirme aşamasında” ibaresi HR FLOW'dan kaldırıldı |

## 2. Sıralı canlıya geçiş planı (güncel: 27.11.2026 21:10, SECURITY koordinasyonu)

Güncel durum: #79 main'e birleştirildi (`b117c06`). #80 ve #82, #83 içinde yer aldığından **ayrı ayrı birleştirilmez**. Bu PR'ları
kapatma kararı SECURITY ekibinindir. NOVA #81 güncel main ile birleştirildi (`83f8d52`).

**Birleşik test (geçerli olan):** `integration/nova81-security83-20261127` @ `b14f026` = main `b117c06` + NOVA `83f8d52` + SECURITY #83 `7df61af`.
- Yerel: 291 test geçti, 3 test atlandı (PG). CONTROL smoke testi geçti.
- GitHub Actions PostgreSQL 18: `nova-tests.yml` ✅ ve `sales-hub-tests.yml` ✅. İkincisi #83'ün PG testlerini de çalıştırır.
- Ortak noktalar kontrol edildi:
  - 2FA: kayıtlı hesapta ikinci adım geçilmeden oturum oluşmuyor, bu yüzden NOVA paneline yarım oturumla girilemiyor.
  - Menü: NOVA "Satış ve İş Geliştirme" grubunda, Security Center "Güvenlik ve Sistem" grubunda; tekrar eden kayıt yok.
  - Advisory lock anahtarları ayrı: CONTROL 47621, billing 47622, NOVA 47623/47624, SECURITY 47630.
  - Şemalar: iki taraf da yalnızca `CREATE TABLE IF NOT EXISTS` kullanıyor; mevcut tablolara DROP/ALTER yok.
- Önceki #79+#80 birleşik testi geçersiz sayılır ve kullanılmaz.

**Aşama 0 — Yedek ve geri yüklenebilirlik (kurucu; dağıtım bu aşama tamamlanmadan yapılmaz)**
1. dashboard.render.com › Databases › **ernasa-db** › **Recovery** › **Export** › *Create export*. Tamamlanınca dosyayı indirin
   (`.dir.tar.gz`). Bu işlem ücretsizdir ve yeni hizmet açmaz.
2. Geri yükleme testi ücretsiz yapılmalı ve üretime dokunmamalı. Yeni ücretli veritabanı açılmaz:
   - Kendi bilgisayarınızda geçici PostgreSQL ile:
     `tar xzf <dosya>` → `createdb ernasa_restore_test` → `pg_restore --no-owner -d ernasa_restore_test <klasör>`.
   - Ardından şu sorgu çalıştırılır ve sonuç aşağıdaki referansla karşılaştırılır:
     `SELECT count(*) FROM ernasa_control.users; … sales_accounts; … subscriptions; public.ats_candidates; public.ats_applications;`
3. Referans (27.11 üretim, salt okunur):
   - ernasa_control: users 2, companies 1, subscriptions 1, sales_accounts 3, sales_audit 14, audit 50.
   - public: ats_candidates 28, ats_applications 28, interview_answers 178.
4. Sayılar tutarsa yedek "geri yüklenebilir" olarak işaretlenir. Test veritabanı kişisel veri içerdiği için iş bitince silinir
   (`dropdb ernasa_restore_test`) ve dosya güvenli bir yerde saklanır.

**Aşama 1 — SECURITY #83 (SECURITY ekibi yürütür)**
- Önce #83 merge edilir. Ardından Render › ERNASA › Manual Deploy yapılır ve SECURITY ekibinin doğrulaması tamamlanır:
  giriş, 2FA kapalı hesapla giriş, Turnstile/SMS bayrakları kapalı.
- NOVA bu aşamada dağıtılmaz. Sorun çıkarsa SECURITY rollback yapar; NOVA'nın bundan etkilenecek bir parçası yoktur.

**Aşama 2 — NOVA #81**
1. #81 dalı, #83'ü içeren yeni main ile birleştirilir. CI (PG18) yeniden çalıştırılır ve yeşil olmalıdır.
2. Dağıtım öncesinde salt okunur satır sayıları alınır: müşteri, aday, abonelik, ödeme, komisyon.
3. Merge yapılır, Render › ERNASA › Manual Deploy çalıştırılır. Başlangıçta yalnızca `nova_*` tabloları oluşur.
4. Dağıtım sonrası doğrulama:
   - Satır sayıları değişmemiş olmalı.
   - `nova_settings` kaydında `kill_switch=1, running=0, auto_email=0, auto_call=0` olmalı.
   - `/control/nova` yalnızca kurucu oturumuyla açılmalı.
   - Hook'lar gizli anahtar olmadan 403 dönmeli.
   - Panel sekmeleri, veri havuzu içe aktarma (8 firma) ve ürün bilgi merkezi çalışmalı.
   - HR FLOW tanıtım sayfası güncel görünmeli.
5. Kapalı kalanlar:
   - E-posta gönderimi ve gelen kutusu: Render'a IMAP veya NOVA değişkeni girilmez.
   - Telefon: Vapi değişkenleri girilmez.
   - Zamanlayıcı: GitHub secret'ları girilmez.
   - Otomatik dış iletişim: politika onayları 0 kalır.
6. Geri dönüş: Render rollback ile Aşama 1 dağıtımına dönülür. NOVA tabloları ekleme niteliğindedir ve yerinde kalır.

## 3. E-posta — ek maliyetsiz seçenek (öncelikli)

Mevcut `iletisim@ernasa.com` kutusu doğrudan kullanılır. Ek posta kutusu açılmaz, ek ücret çıkmaz.
- **Gönderim:** Mevcut Brevo/Resend anahtarı kullanılır. NOVA ayrı bir gönderim kanalı açmaz.
- **Okuma:** Render › ERNASA › Environment sayfasına şunları ekleyin:
  - `ERNASA_NOVA_IMAP_HOST = mail.privateemail.com`
  - `ERNASA_NOVA_IMAP_USER = iletisim@ernasa.com`
  - `ERNASA_NOVA_IMAP_PASSWORD = (kutunun şifresi)`
- Şifre yalnızca Render'da durur; sohbete veya GitHub'a yazılmaz.
- NOVA kutuyu yalnızca **salt okunur** açar (IMAP EXAMINE + BODY.PEEK). Silme, taşıma ve okundu işaretleme yapmaz. Kod SMTP gönderimi için bu şifreyi kullanmaz.
- Risk ve önlem: Şifre teknik olarak gönderim yetkisi de taşır. Şifreyi daha önce başka yerde kullandıysanız Namecheap › Private Email ›
  kutu ayarlarından yenileyin. Render erişimi yalnızca kurucuda olmalı. İleride ek kutu açılırsa (ücretli olabilir) okuma o kutuya
  taşınabilir.
- DMARC kaydı (ücretsiz): Namecheap › Domain List › ernasa.com › Advanced DNS bölümüne
  `TXT _dmarc  v=DMARC1; p=none; rua=mailto:iletisim@ernasa.com` kaydını ekleyin. SPF ve DKIM'in **Verified** olduğunu Brevo/Resend Domains
  ekranından kontrol edin.

## 4. Vapi ücretsiz test hesabı (ücretsiz, kart girmeden)

1. **dashboard.vapi.ai** › Sign up. Kurumsal e-postanızla kaydolun. Yeni hesaplara 10 $ başlangıç kredisi tanımlanır ve kart istenmez.
2. **Billing** sayfasında **Auto-reload** seçeneğinin **kapalı** olduğunu kontrol edin. Ödeme yöntemi eklemeyin. Bakiye 0 olunca hesap
   dondurulur ve ek ücret oluşmaz.
3. Organization ayarlarında bölge olarak mümkünse **EU** seçin (`ERNASA_NOVA_VAPI_API_BASE=https://api.eu.vapi.ai`).
4. **API Keys** › Private key oluşturun ve Render'a `ERNASA_NOVA_VAPI_API_KEY` olarak girin. Anahtarı sohbete yazmayın.
5. Render'a `ERNASA_NOVA_VAPI_WEBHOOK_SECRET` olarak en az 24 karakterlik rastgele bir değer girin. Asistan, çağrı başına NOVA
   tarafından oluşturulur; Vapi panelinde asistan kurmak gerekmez.
6. Ücretsiz Vapi numaraları yalnızca ABD numaralarını arayabilir. Türkiye'deki kurucu numarasını aramak için 5. bölümdeki
   TR SIP hattı gerekir. Hat gelince **Phone Numbers › Import › BYO SIP Trunk** ekranında Netgsm bilgileri girilir. Oluşan
   numara kimliği Render'a `ERNASA_NOVA_VAPI_PHONE_NUMBER_ID` olarak yazılır.

## 5. Netgsm SIP hattı — başvuru öncesi (satın alma yok)

Netgsm'den **yazılı teklif** isteyin (netgsm.com.tr › Kurumsal › Sabit Telefon/0850 › teklif formu veya müşteri temsilcisi).
Teklifte şunlar sorulmalı:
1. 0850 veya şehir kodlu (0212/0216) numaranın aylık ücreti ve tahsis/kurulum ücreti; taahhüt süresi.
2. Sabit ve mobil dakika ücretleri (KDV ve ÖİV dahil/hariç ayrı).
3. **Yurt dışındaki bir bulut platformundan** (Vapi, AB/ABD) SIP kaydı veya trunk bağlantısı yapılmasına izin verilip verilmediği; IP
   kısıtlaması; TLS (5061) ve SRTP desteği; eşzamanlı çağrı sayısı.
4. Arayan numara (CLI) olarak tahsis edilen numaranın gösterilmesi.
5. Gerekli belgeler. Genellikle şirket için vergi levhası, imza sirküleri, ticaret sicil gazetesi ve yetkili kimliği istenir.
   Kesin listeyi Netgsm verecek; bu oturumda kamuya açık bir liste bulunamadı.
6. Otomatik arama veya yapay zekâ ile ticari arama için operatörün kullanım koşulları ve BTK kapsamındaki yükümlülükler.

Teklif gelince toplam maliyet tablosundaki telefon satırı (Twilio referansı) gerçek fiyatla güncellenir. Satın alma kararı kurucuya aittir.

## 6. Dış iletişimi açmadan önce (hukuki)

- İYS'ye hizmet sağlayıcı kaydı (ERNASA unvanıyla).
- KVKK yurt dışı aktarım standart sözleşmesi (Vapi, Deepgram, OpenAI için veri sorumlusu → veri işleyen), imzadan sonraki 5 iş günü içinde
  Kurum'a bildirim.
- BTK ve operatör koşulları için hukuki görüş.
- Sonra CONTROL › NOVA › Ayarlar: `email_policy_approved` / `call_policy_approved` → manuel onaylı mod.
