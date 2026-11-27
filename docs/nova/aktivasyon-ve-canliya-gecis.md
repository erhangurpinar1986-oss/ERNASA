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

## 2. Canlıya geçiş öncesi kontrol listesi

1. **Üretim yedeği (kurucu, 2 dk):**
   - dashboard.render.com › Databases › **ernasa-db** › sol menü **Recovery** sayfasını açın.
   - Point-in-time recovery penceresini ve son yedeğin tarih/saatini kontrol edin. Ekran görüntüsünü ya da tarih/saati bildirin.
   - Plan PITR sunmuyorsa aynı ekrandaki **Export** ile bir dışa aktarım başlatın ve tamamlandığını bildirin.
   - Bu doğrulama bu oturumdan yapılamıyor: Render bağlantısında yedekleme aracı yok.
2. **Birleşik testler:** NOVA #81 ile SECURITY #79 ve #80'in en güncel commit'leri `integration/nova-security-20261127` dalında
   test edilir: yerel testler ve GitHub Actions PG18. Sonuçlar PR #81'e yazılır.
3. **Merge sırası:** #79 → #80 → #81 (tümü draft; kurucu “Ready for review” ve merge yapar ya da yetki verir).
4. **Dağıtım:** Render › ERNASA › **Manual Deploy › Deploy latest commit**. Başlangıçta yalnızca ek `nova_*` tabloları
   oluşturulur. Mevcut tablolar değişmez.
5. **Canlı doğrulama (salt okunur):**
   - `ernasa_control.nova_settings` kaydında `kill_switch=1` olmalı.
   - Mevcut tablo satır sayıları dağıtımdan önceki ve sonraki sorgularda aynı olmalı.
   - `/control/nova` yalnızca kurucu oturumuyla açılmalı; oturumsuz istek giriş sayfasına yönlenmeli.
   - `/control/nova/hook/tick` gizli anahtar olmadan 403 dönmeli.
   - Ana sayfa ve HR FLOW sayfası güncel görünmeli.
6. **Geri dönüş:** Render › ERNASA › Deploys bölümünde önceki canlı dağıtım `dep-dc4niobl550s73a19po0` (commit `fbde788`) için
   **Rollback**. NOVA tabloları ekleme niteliğinde olduğundan yerinde kalır ve eski sürüm onları kullanmaz. Tablo silmek gerekmez.
   Ret listesi korunmalıdır.

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
