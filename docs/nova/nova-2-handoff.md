# NOVA 2.0 — ERNASA AI SALES CENTER · Devir, araştırma ve aktivasyon dosyası

Tarih: 27 Kasım 2026 · Dal: `feature/nova-2-sales-agent-20261127` · Başlangıç main: `fbde788` (PR #78)

> Bu belge bir hukuki görüş değildir. Mevzuat bölümü resmî kaynaklardan derlenmiştir. Gerçek firmalara gönderim veya
> arama başlamadan önce bir avukatın kontrol etmesi önerilir.

## 1. Ne yapıldı / ne yapılmadı

| Bileşen | Durum |
|---|---|
| CONTROL › **NOVA AI SALES CENTER** (`/control/nova`): metrikler, veri havuzu, e-posta onay kuyruğu, telefon, ürün bilgi merkezi, ayarlar/limitler, günlük rapor, **acil durdurma** | Kodlandı, test edildi |
| ERNASA DATA POOL: içe aktarma, mükerrer kayıt engeli (alan adı / vergi no / unvan+il), kanıta dayalı puan, CSV dışa aktarma | Kodlandı, test edildi |
| Satış aşamaları (16 aşama), zaman damgalı ve silinemez geçmiş (`nova_events`) | Kodlandı |
| Kalıcı ve tüm kanallarda geçerli ret listesi (`nova_suppressions`, DB trigger'ı ile silinemez) | Kodlandı |
| E-posta: taslak → kurucu onayı → gönderim (mevcut `services/invite_email.py` göndericisiyle), günlük limit, çalışma saati, İYS kapısı, yasaklı iddia filtresi, ret satırı | Kodlandı. **Gerçek gönderim yapılmadı** |
| Gelen kutusu: salt okunur IMAP (EXAMINE + BODY.PEEK), yanıt sınıflandırma (ilgi/demo/soru/ret/otomatik yanıt/teslim edilemedi) | Kodlandı. **Gelen kutusuna bağlanılmadı** (şifre yok) |
| Sesli AI arama: Vapi adaptörü, Türkçe ilk cümle + yapay zekâ kimliği + ticari unvan, onaylı bilgi tabanı, maliyet rezervasyonu, webhook ile sonuç/özet/maliyet kaydı | Kodlandı. **Gerçek arama yapılmadı** (aşağıda “Engeller”) |
| Sales Hub: firmayı temsilciye atama → Sales Hub müşterisi (tax/domain kimliği); mevcut sahip ve geçmiş korunur; komisyon verisi yazılmaz | Kodlandı, test edildi |
| Bulut zamanlayıcı: GitHub Actions `nova-tick.yml` (hafta içi 09:15–17:45 TRT, 30 dk), Bearer secret | Kodlandı. **Secret'lar girilene kadar pasif** |
| Veri havuzu | **8 doğrulanmış firma** (hedef: 100). Dosya: `nova-data-pool-20261127.json` (PR'a eklenmedi, ayrıca iletildi) |
| PostgreSQL migration | `migrations/nova` ayrı aile. PG16 üzerinde iki kez çalıştırıldı, trigger'lar doğrulandı. **Üretimde çalıştırılmadı** |
| Canlıya geçiş | **Yapılmadı.** PR merge ve Render dağıtımı için kurucu onayı gerekir |

## 2. Güvenlik ve SECURITY ile çakışma önlemleri

- Bütün dış kanallar **kapalı başlar**: `kill_switch=1`, `running=0`, `auto_email=0`, `auto_call=0`,
  `email_policy_approved=0`, `call_policy_approved=0`, `require_iys=1`.
- Otomatik modlar, ilgili politika onayı olmadan açılamaz. Acil durdurma açıkken NOVA başlatılamaz.
- Panel yalnızca CONTROL **kurucu** oturumuyla açılır. Her POST'ta CSRF ve Origin kontrolü yapılır. Hook'lar paylaşılan gizli anahtarla korunur (`hmac.compare_digest`).
  CSV dışa aktarmada formül enjeksiyonuna karşı koruma vardır. Sağlayıcı hata gövdeleri loglanmaz.
- Ortak dosyalarda yalnızca ekleme yapıldı: `control/app.py` (+7: route, menü, SQLite şeması),
  `control/render_gateway.py` (+16: `ensure_nova_schema`, iki hook için başlık iletimi). Mevcut hiçbir güvenlik kontrolü
  değiştirilmedi veya gevşetilmedi.
- Migration numaraları: NOVA, `control_postgresql/NNN` sırasını **kullanmaz**. Tablolar `nova_*` önekli, advisory lock anahtarı
  ayrı (`47623`).
- **Canlı öncesi zorunlu adım:** SECURITY dalı hazır olduğunda iki dal birlikte bir entegrasyon dalında birleştirilip
  `nova-tests.yml`, `sales-hub-tests.yml` ve SECURITY testleri birlikte koşturulmalı. Bu oturumda uzak depoda SECURITY
  adlı bir dal görülmedi.

## 3. Sesli AI sağlayıcı karşılaştırması (Kasım 2026, kamuya açık fiyatlar)

| Sağlayıcı | Dakika ücreti (yayımlanan) | Sabit ücret | Türkçe | TR numarası / SIP | Not |
|---|---|---|---|---|---|
| **Vapi** | Platform $0,05/dk + STT/LLM/TTS maliyet fiyatından; toplam ≈ $0,08–0,13/dk | Yok (kullandıkça öde) | Azure `tr-TR` sesleri, Deepgram `tr` | BYO SIP trunk; EU API bölgesi var | Çağrı başına geçici asistan API ile kurulabiliyor → NOVA'nın firma bağlamı + onaylı bilgi tabanı her çağrıya gömülür |
| **Retell AI** | Altyapı $0,055/dk + ses $0,015/dk'dan + LLM; toplam $0,07–0,31/dk | Yok, $10 deneme kredisi | Türkçe yalnızca “tek dil” modunda güvenilir (topluluk) | SIP trunk | Güçlü alternatif |
| ElevenLabs Agents | $0,08/dk (Business), aşım $0,08 | Plan $6–$990/ay | 90+ dil | Twilio/SIP | Plan ücreti var |
| Bland AI | Start $0,14/dk; Build $0,12/dk + $299/ay | Build/Scale'de var | — | Taşıyıcı ayrıca | Sabit ücret nedeniyle önerilmedi |
| OpenAI Realtime (gpt-realtime-2.1-mini) | Ses girdi $10 / çıktı $20 (1M token) → yaklaşık $0,02/dk | Yok | İyi | SIP destekli | En ucuz ama kendi çağrı orkestrasyonumuzu yazmayı gerektirir; ikinci aşama |
| Twilio (taşıyıcı) | TR sabit $0,0701/dk, TR mobil $0,2875/dk | Numara $1,15/ay'dan | — | — | **Risk:** yurt dışından +90 arayan numarasıyla gelen trafik TR operatörlerince filtrelenebiliyor |

**Öneri:** Test için **Vapi + Türkiye'de lisanslı bir operatörün SIP hattı** (ör. Netgsm 0850/0212 numarası, Vapi'ye
“BYO SIP trunk” olarak bağlanır). Gerekçe: aylık sabit ücret yok, ücretsiz başlangıç, Türkçe ses/tanıma seçenekleri,
EU veri bölgesi ve kodla tam uyumlu webhook. Türk operatörünün SIP dakika ücreti ve yurt dışı IP'den SIP kaydına izin
verip vermediği **teklifle doğrulanmalı** (fiyatı kamuya açık bulunamadı; uydurulmadı).

## 4. Maliyet tablosu (tahmin, KDV ayrı)

Varsayımlar: Vapi $0,05 + STT ≈$0,01 + LLM ≈$0,006 + TTS ≈$0,008 = **AI ≈ $0,074/dk**. Taşıyıcı için referans olarak
Twilio TR sabit hat $0,0701/dk alındı (TR SIP teklifi gelince değiştirilmeli). Kur 1 USD ≈ 50,5 TL (19 Kasım 2026
piyasa). Yurt dışından alınan hizmette sorumlu sıfatıyla %20 KDV (indirilebilir).

| Aylık dakika | Vapi platformu | STT | LLM | TTS | Telefon (ref.) | Toplam USD | Toplam TL | KDV dahil TL |
|---|---|---|---|---|---|---|---|---|
| 100 | 5,00 | 1,00 | 0,60 | 0,80 | 7,01 | 14,41 | ≈ 728 | ≈ 873 |
| 500 | 25,00 | 5,00 | 3,00 | 4,00 | 35,05 | 72,05 | ≈ 3.639 | ≈ 4.366 |
| 1.000 | 50,00 | 10,00 | 6,00 | 8,00 | 70,10 | 144,10 | ≈ 7.277 | ≈ 8.732 |
| 5.000 | 250,00 | 50,00 | 30,00 | 40,00 | 350,50 | 720,50 | ≈ 36.385 | ≈ 43.662 |
| 10.000 | 500,00 | 100,00 | 60,00 | 80,00 | 701,00 | 1.441,00 | ≈ 72.771 | ≈ 87.325 |

Ek kalemler: TR numara aylık ücreti (teklif), cep telefonu aramaları (Twilio referansı $0,2875/dk), Vapi ek eşzamanlı hat
$10/hat/ay (10 hat dahil), e-posta (mevcut Brevo/Resend planı). NOVA, gerçek maliyeti webhook'tan alır. Sağlayıcı maliyet
bildirmezse `call_rate_minor_per_minute × süre` ile tahmin yazar ve bunu “tahmin” olarak işaretler. Günlük ve aylık harcama
limiti her aramadan önce rezervasyonla kontrol edilir. Varsayılan limitler 200 TL/gün ve 3.000 TL/ay olup CONTROL'den değiştirilebilir.

## 5. Mevzuat kontrol listesi (sisteme işlenen kurallar)

- **6563 sayılı Kanun / Ticari İletişim ve Ticari Elektronik İletiler Yönetmeliği:** Tacir veya esnaf alıcılara önceden onay
  zorunlu değildir (m.6/3). Ancak gönderimden önce adresin İYS'ye kaydedilmesi ve ret durumunun İYS'den kontrol edilmesi
  gerekir (m.6/6). → NOVA'da `require_iys=1` ve firma bazında “İYS temiz” işareti olmadan gerçek gönderim veya arama yapılmaz.
  ERNASA'nın İYS'ye hizmet sağlayıcı olarak kaydı **yapılmalı**.
- Ret imkânı her iletide aynı kanaldan, kolay ve ücretsiz sunulur. Bunun için her e-postaya “LİSTEDEN ÇIKAR” satırı otomatik eklenir. Telefonda “beni
  aramayın” talebi kaydedilir ve numara tüm kanallarda engellenir. Ret bildirimleri **3 iş günü** içinde İYS'ye bildirilmelidir:
  bu adım şimdilik **manuel** yapılıyor; İYS API entegrasyonu sonraki iş.
- Sesli aramada ticari unvan söylenir (`ERNASA_NOVA_TRADE_NAME`). NOVA ilk cümlede yapay zekâ asistanı olduğunu belirtir.
- Kamuya açık kurumsal iletişim bilgisi tek başına sınırsız iletişim izni sayılmaz. Bu nedenle limitler, ret listesi ve İYS kontrolü uygulanır.
  Firmanın yazılı “arayabilirsiniz” kabulü kaydedilir.
- **KVKK m.9 (2024 değişikliği):** Vapi, Deepgram ve OpenAI yurt dışında çalışır. Gerçek kişilerle görüşme başlamadan Kurul'un
  standart sözleşmesi (veri sorumlusu → veri işleyen) imzalanmalı ve imzadan sonraki **5 iş günü** içinde Kurum'a bildirilmelidir.
- Kayıt: Ses kaydı varsayılan olarak **kapalı**. Tam görüşme metni, yalnızca sağlayıcı bilgilendirmenin kabul edildiğini teyit ederse
  90 gün saklanır (`ERNASA_NOVA_TRANSCRIPT_DAYS`). Diğer durumlarda yalnızca özet tutulur.
- Kişisel veri: Cep telefonu, ad.soyad@ biçimli adresler, ücretsiz e-posta servisleri ve kişisel LinkedIn profilleri kod düzeyinde
  reddedilir. Yalnızca mesleki unvan saklanır.
- **Doğrulanmadı:** Otomatik arama sistemlerine ilişkin BTK'nın ayrıntılı düzenlemesi bu oturumda araştırılamadı. Gerçek firma
  aramalarından önce hukuki kontrol gerekir.

## 6. Aktivasyon sırası ve gereken bilgiler

1. **Kurucu onayı:** PR'ı inceleyip merge. Render `ERNASA` servisi manuel dağıtım (autoDeploy kapalı). Dağıtım öncesinde DB yedeği
   alınmalı (`docs/hr-flow-live-release.md`). Başlangıçta `ensure_nova_schema` yalnızca ek tablolar oluşturur.
2. Render ortam değişkenleri:
   - `ERNASA_NOVA_TICK_SECRET` (≥32 karakter) ve aynı değerle GitHub secret'ları `NOVA_TICK_SECRET` ile `NOVA_CONTROL_ORIGIN`.
   - Gelen kutusu (salt okunur): `ERNASA_NOVA_IMAP_USER=iletisim@ernasa.com`, `ERNASA_NOVA_IMAP_PASSWORD` (PrivateEmail'de **ayrı**
     bir uygulama şifresi).
   - Test e-postası: `ERNASA_NOVA_TEST_EMAILS=<kurucunun test adresi>`.
   - Sesli arama: `ERNASA_NOVA_VAPI_API_KEY`, `ERNASA_NOVA_VAPI_PHONE_NUMBER_ID`, `ERNASA_NOVA_VAPI_WEBHOOK_SECRET` (≥24),
     isteğe bağlı `ERNASA_NOVA_VAPI_API_BASE=https://api.eu.vapi.ai`, `ERNASA_NOVA_USD_TRY`.
3. **İlk gerçek test araması:** CONTROL › NOVA › Telefon › kendi numaranızı “EVET” ile yetkilendirin → test araması planlayın →
   Ayarlar'da acil durdurmayı kapatıp NOVA'yı başlatın → “Aramayı başlat”. Sonuç, özet ve maliyet webhook ile kaydedilir.
   Bağlantı kurulamazsa arama “failed/no_answer” olarak kalır ve tamamlanmış gösterilmez.
4. Gerçek firmalara geçiş: İYS kaydı, KVKK standart sözleşmesi ve hukuki kontrol → CONTROL'de e-posta/arama politikası onayı →
   önce manuel onaylı mod (günde en çok 10 e-posta, 5 arama, 5 dk).

## 7. Yönetim onayı bekleyenler

- PR merge ve Render dağıtımı (üretim).
- Vapi hesabı ve TR SIP numarası (ücretli olabilir). Bunlar satın alınmadı.
- İYS kaydı, KVKK standart sözleşmesi ve hukuki görüş.
- PrivateEmail uygulama şifresi oluşturulması.
- Günlük 20 firma veri havuzu araştırması için yeni zamanlanmış görev (`docs/nova/data-pool-routine-prompt.md`). Mevcut
  “Nova Müşteri Araştırması” görevi değiştirilmedi.

## 8. Ticari ürüne dönüştürme (sonraki aşama)

`nova_*` tablolarına `tenant_id` eklenmesi (firma bazlı izolasyon) ve CONTROL firma kaydıyla ilişkilendirme gerekiyor. Diğer kalemler:
firma başına bilgi tabanı ve ses/numara ayarı, `nova_spend` üzerinden kullanım bazlı faturalama (mevcut Shopier/billing ile),
WhatsApp Business Platform ve web canlı destek kanalı adaptörleri (`nova_providers` arayüzüyle), CRM dışa aktarma API'si,
çoklu dil. Önce ERNASA içi kullanımda doğrulama yapılacak.

## 9. Testler

- `tests/test_nova.py`: 32 senaryo (şema, mükerrer kayıt, kişisel veri reddi, ret listesinin kalıcılığı, limitler, İYS, acil durdurma,
  arama akışı, webhook, Sales Hub ataması, HTTP/CSRF/yetki). Yerelde 32/32 geçti.
- `tests/test_nova_postgres.py`: Gerçek PG'de başlangıç migration'ı ve iş kurallarını kontrol eder (CI'da `nova-tests.yml`).
- Mevcut CONTROL/Sales Hub/billing seti dal ve main üzerinde aynı sonucu verdi (193/193).

## 10. Güncelleme — 27 Kasım 2026 akşamı

**Birleşik test (NOVA #81 + SECURITY #79 + #80):** `integration/nova-security-20261127` dalında yapıldı.
- Tek çakışma `control/app.py` import satırındaydı; iki taraf da korundu.
- Yerelde 263 test geçti, 1 test atlandı (PG DSN yok). Bu set NOVA, giriş kısıtlama, auth yapı taşları, Sales Hub ve billing testlerini kapsıyor. CONTROL smoke testi de geçti.
- GitHub Actions'ta **PostgreSQL 18** ile `nova-tests.yml` başarılı.
- İlk PG18 koşusu gerçek bir hata buldu: `sum()` Decimal döndürüyordu. Düzeltildi.

**Vapi ücretsiz test:**
- Yeni hesaba 10 $ başlangıç kredisi verilir ve kart istenmez.
- Ücretsiz Vapi numaraları yalnızca ABD'yi arayabilir. Türkiye araması için TR operatöründen BYO SIP hattı gerekir.
- Bakiye 0 $ olunca ve otomatik yükleme kapalıysa hesap dondurulur, yani beklenmedik harcama olmaz. Otomatik yükleme açılmamalıdır.

**Netgsm:**
- Abonelik belgeleri doğrulanarak hizmet açılır.
- Kesin belge listesi ve SIP dakika ücreti kamuya açık kaynakta bulunamadı. Satış ekibinden yazılı teklif alınmalı. Sorulacaklar:
  1. 0850/0212 numara ücreti
  2. Sabit ve mobil dakika ücreti
  3. Yurt dışı IP'den SIP kaydı veya trunk desteği
  4. TLS 5061 desteği
  5. Taahhüt ve kurulum ücreti
  6. Gerekli belgeler

**OpenAI Realtime/Live + TR SIP (doğrudan altyapı):**
- Teknik olarak mümkün. Gelen aramalar SIP trunk ile `sip.api.openai.com`'a bağlanabiliyor; AB veri yerleşimi için `sip-eu` uç noktası var.
- Giden SIP araması organizasyon bazında etkinleştirilmeli ve Live API üzerinden yapılıyor.
- GPT-Live dakika ücreti 0,05 $; arka plandaki model kullanımı ayrıca faturalanıyor. Bu yüzden AI katmanındaki tasarruf Vapi'ye göre sınırlı (~%20).
- Ek mühendislik gerektiriyor: webhook imzası, oturum yönetimi, kendi özet/maliyet hesabı.
- Öneri: İlk test Vapi ile yapılsın. Hacim aylık ~5.000 dakikayı geçerse `nova_providers` içine OpenAI adaptörü eklensin (arayüz hazır).

**Veri havuzu:**
- 8 doğrulanmış firma hazır.
- KOBİ odaklı yeni zamanlanmış görev oluşturuldu: hafta içi 11:13, 20 firma.
- Bu akşam 3 ek çalıştırma başlatıldı (perakende, otel/sağlık, hizmet).
- Lojistik ve üretim çalıştırmaları izin sistemi tarafından engellendi; bu sektörler Pzt/Salı planlı çalıştırmalarla tamamlanacak.
- Geçmiş NOVA raporları için `scripts/nova_report_to_pool.py` hazır. Rapor dosyaları bu oturumdan erişilemiyor, kurucunun eklemesi gerekiyor.

**Canlıya geçiş öncesi açık kabul koşulları:**
1. Üretim DB yedeğinin doğrulanması: Render › ernasa-db › Recovery/Backups.
2. Veri havuzunun 100 firmaya ulaşması.
3. Ortam değişkenleri (bkz. `eposta-baglanti-adimlari.md`).
4. SECURITY PR'larının önce veya birlikte merge edilmesi.
