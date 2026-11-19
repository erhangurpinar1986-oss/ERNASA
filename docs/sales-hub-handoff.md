# ERNASA SALES HUB — GELİŞTİRME DEVİR VE YEDEK DOSYASI

Tarih: 19 Kasım 2026. Çalışma dalı: `feature/ernasa-sales-hub`.
Başlangıç main: `cab9d937cbfd47e63777ef4594c8370f6d71c949`.

## Yetki ve teslim sınırı

Kullanıcı kurumsal tasarımı onayladı. Son öncelik: kritik güvenlik/finans,
TL girişi, engelleyici mobil sorunlar, testler, commit, feature dalı ve PR.
Yeni modül/görsel kapsam genişletilmeyecek. `main` push, PR merge ve Render/canlı
dağıtım için ayrıca açık kullanıcı onayı gerekir. Bunlar yapılmadı.

## Tamamlanan uygulama

- `control/sales.py`: aynı CONTROL veri katmanında müşteri/fırsat/not-görev,
  sürümlü teklif ve yönetim kararları, tahsilat, komisyon, düzeltme ve audit.
- `control/sales_http.py`: mevcut CONTROL kurucu oturumu ile `/control/satis`,
  temsilciye ayrı onaylı hesap/oturum ile `/control/sales`, aynı kuralları çağıran
  `/control/sales/api`. Akademi/İK erişimi satış yetkisi vermiyor.
- Backend'de firma + sahip filtreleri, her istekte aktif hesap/firma kontrolü,
  origin/CSRF, hashed oturum, scrypt parola, kurucu dışı finans kararı engeli.
- Yeni temsilci hesabı pasif açılıyor; etkinleştirme CONTROL kararı.
- Vergi numarası/alan adı ile mükerrer müşteri ve sahiplik talebi; talepte diğer
  temsilcinin müşterisi açığa çıkmıyor. Geçmişi olan müşteri otomatik devredilmiyor.
- Resmi fiyat/indirim CONTROL'den; onaysız teklif resmi belgeye dönüşmüyor.
- İlk yeni müşteri/ürün aboneliği %25, aynı abonelik devamı %5; yükseltme/ek ürün
  %0. İlk ürün uygunluğu müşteri geçmişi üzerinden sınırlandırılıyor.
- Doğrulanmış tahsilat yoksa hak ediş yok. Özel projede otomatik oran yok;
  yazılı CONTROL kararı + maliyet/kâr analizi ile tutar tanımlanıyor.
- Tahsilat referansı ve abonelik dönemi tekil; sağlayıcı maliyeti matrahtan ayrı.
- Ödeme işareti ayrı onaya ve İstanbul takviminde izleyen ayın 1–15 gününe bağlı.
  Gerçek para transferi veya e-posta gönderimi yapılmıyor.
- İade/iptal bekletmesi, sonraki tahakkuk engeli ve gerekçeli tam ters kayıt.
  Audit/karar/düzeltme geçmişi DB trigger'larıyla update/delete'e kapalı.
- UI tutarları TL metni kabul ediyor (`1.234,56`, `1234,56`, `1234.56`).
  Decimal ile tam sayı kuruşa çevriliyor. İkiden fazla ondalık, float, negatif,
  bilimsel gösterim ve TL+kuruş birlikte gönderimi reddediliyor.
- `design-system.css`: mevcut CONTROL CSS'si ortak bileşenlere taşındı; gerçek
  ERNASA logosu ve onaylı tema korundu. İK/Akademi sayfaları yeniden tasarlanmadı.
- `sales-menu.js`: mobil menü seçimde ve geçmişten dönüldüğünde kapanıyor.

## Migration / rollback

SQLite `control.app.init` yalnız eklemeli satış DDL ve koruma trigger'larını uygular.
PostgreSQL için (operatör, açık canlı onayından sonra) mevcut CONTROL şemasında
tek transaction içinde `SET LOCAL search_path=ernasa_control,pg_catalog`, ardından
`migrations/sales/001_sales.up.sql` ve
`migrations/control_postgresql/003_sales_hub.up.sql` uygulanmalı.
Mevcut CONTROL 001/002 migration'larını üretimde yeniden çalıştırmayın.
SQLite korumaları `migrations/sales/002_sqlite_guards.up.sql` dosyasındadır.
Geri dönüş: önceki uygulama sürümüne dön; satış tablolarını ve mali/audit geçmişini
silme. Üretimde hiçbir migration çalıştırılmadı.

## İzole test çalıştırma

Bu Windows ortamında mevcut Python:
`../ERNASA_Git_Yayin_20261116/.venv/Scripts/python.exe`.
Yeni bağımlılıklar yalnız `.sales-test-state/test-deps` içine PyPI'dan kuruldu.
SSL doğrulaması korunarak pip `--use-feature=truststore` ile Windows kök
sertifikaları kullanıldı. Mevcut venv/proje gereksinimleri değiştirilmedi.

PowerShell:

```powershell
$env:PYTHONPATH = '.;.sales-test-state\test-deps'
& '..\ERNASA_Git_Yayin_20261116\.venv\Scripts\python.exe' scripts/verify_sales_offline.py
& '..\ERNASA_Git_Yayin_20261116\.venv\Scripts\python.exe' scripts/verify_sales_postgres.py
node --check control/sales-menu.js
```

Offline çalıştırıcı üretim env değişkenlerini temizler, dış socket bağlantılarını
engeller; DB/dosyaları `.sales-test-state/offline-<uuid>` içinde tutar.
PostgreSQL çalıştırıcı sabit `127.0.0.1:55439`, `sales_test_hub`, `sales_test`
kullanıcısı ve çalışma klasöründeki `pgdata` dizinini doğrular. Mevcut DB'yi silmez,
sıfırlamaz; her test turu yeni sentetik kayıt ekler. İlk kurulum PostgreSQL 18.7
ile yapıldı. Windows sandbox socketpair sorununda bu testler için kullanıcı açık
izin verdi; gerekirse aynı izinli yürütme kullanılmalı.

## Test sonuçları

19 Kasım 2026 son Codex çalışma raporu (KULLANICI TARAFINDAN AKTARILAN,
bu devir kaydı güncellemesinde yeniden çalıştırılmamış sonuçlar):
- Tam çevrimdışı regresyon: **125 başarılı, 0 başarısız, 1 atlanan**.
- Atlanan PostgreSQL senaryosu çevrimdışı koşuda DSN verilmemesinden kaynaklanıyor;
  ayrı ve doğrulanmış yerel `sales_test_hub` PostgreSQL koşusu **1 başarılı**.
- Gerçek OCR entegrasyon testi dış ağ kapalıyken başarılı olarak bildirildi.
- PR #35 açıklamasındaki Sales Hub finans/yetki/TL test sonucu: 57 başarılı.
- GitHub üzerinde ilk Sales Hub commit'ine bağlı CI iş akışları başarılı görünüyor:
  ERNASA Sales Hub isolation and finance, ERNASA Academy voice upload checks,
  ERNASA B2B Interview Template Tests.
Bu bilgiler çalışan yerel oturumun raporlarıdır. GitHub'daki son kodla aynı
snapshot'ın test edildiği, ayrı ortam ve CI üzerinden yeniden doğrulanmalıdır.
Testi geçirmek için hata/skip gizlenmesi veya test kapsamının azaltılması yasaktır.

ÖNEMLİ TESLİM SINIRI: Codex kullanım limiti dolmadan hemen önce
"Edited 8 files +257/-9" olarak bir yerel değişiklik grubu bildirildi.
Bu grubun tamamının GitHub'a push edildiği doğrulanamadı. Yerel
`git status`, `git diff`, `git log` kontrol edilmeden "eksiksiz yedeklendi"
denmemeli; çalışma klasörü silinmemeli. Kullanıcının son raporuna göre
kesin test sayıları 125/0/1'dir; eski 105/1/1 sonucu güncel değildir.

## Tarayıcı / ekran dosyaları

Önceki tam inceleme: çalışma klasörünün üst dizininde
`ERNASA_SALES_HUB_ARAYUZ_INCELEME_20261119.zip` (27 ekran ve rapor).
Masaüstü 1440×1000, mobil 390×844. Müşteri → fırsat → taslak → gönder → CONTROL
onay/revizyon → tahsilat → 250 TL (%25) hak ediş → yönetim onayı doğrulandı.
Erişim reddi ve tahsilat öncesi boş ekranlar da kaydedildi.
Son TL/mobil değişikliklerinin tarayıcı kontrolü ayrıca kaydedilecek.

Önizleme: `scripts/preview_sales.py`, yalnız `127.0.0.1:18099`, her çalıştırmada
yeni sentetik SQLite. Gerçek hesap, veri veya dış servis kullanılmaz.

## Bilinen sınırlar / sonraki oturum

- Kullanım hakkı önceliği nedeniyle kısa okunabilir kayıt numaraları, UTC'den
  yerel tarih/saat sunumu, CONTROL sekmeleri ve ayrıntılı boş liste metinleri
  sonraki oturuma bırakıldı. Kimlikler/UTC kayıtları değiştirilmedi.
- MVP yalnız TRY ve tam dönem tahsilatı; kısmi tahsilat/iadeler, dosya ekleri,
  taslak düzenleme ve geçmişli müşteri devri genişletilmedi.
- İade/iptal ters kaydı tam hak ediş tutarı içindir; sözleşme belirsizliklerini
  otomatik oran/mahsup uydurarak çözmeyin.
- Fuzzy müşteri eşleştirme yok; vergi no/alan adı güçlü anahtarları kullanılıyor.
- Trigger koruması uygulama/normal SQL düzeyindedir; DB sahibi/superuser
  trigger'ı kaldırabilir. Üretim DB rolleri ayrıca gözden geçirilmeli.
- Hedef `sales.ernasa.com` için DNS/TLS/deploy yapılandırılmadı. Korunan route
  `/control/sales` üzerinden çalışılıyor.

## Git / PR

GitHub'da doğrulanan çalışma dalı: `feature/ernasa-sales-hub`.
Kod ve devir dosyasıyla ilk gönderilmiş Sales Hub commit:
`6d41b9ddc74c4fbc4e7693aa896725154ea6b869`.
Açık PR: https://github.com/erhangurpinar1986-oss/ERNASA/pull/35
Hedef dal: `main`. PR henüz birleştirilmedi; üretim dağıtımı yapılmadı.
Bu devir kaydı güncellemesi ayrıca bir feature-dalı commit'i olarak yapılabilir;
güncel head SHA sonradan değişirse GitHub PR head'inden doğrulanmalıdır.
Özellikle Codex'in yerelde bırakmış olabileceği son 8 dosyayı `git status`
ve `git diff` ile kontrol edin; yerel değişiklik varsa PR'a ayrıca gönderin.
İlk bakılacak kaynak dosyalar: `control/app.py`, `control/sales-menu.js`,
`control/sales_http.py` ve Codex'in diğer son değiştirdiği dosyalar.
Yalnız feature dalına push; PR merge, `main` push, Render/DNS ve canlı
migration için açık kullanıcı onayı gerekir. Tamamlanan işleri baştan yapmayın.


## Amaç, mimari ve dosya envanteri

Amaç: İK, aday/mülakat ve Akademi V9 akışlarını koruyarak tek satış çekirdeği ve
iki yetkili arayüz sağlamak. FastAPI `backend/main.py`, CONTROL'u
`control/render_gateway.py` ile `/control` altında proxy'ler. CONTROL HTTP sunucusu
`control/app.py` içinde; SQLite veya `ernasa_control` PostgreSQL şeması kullanılır.
Satış iki panelde aynı CONTROL tablolarını/servislerini kullanır. İK'nın
`database.py`, `control_integration.py`, Akademi ve e-posta akışları değiştirilmedi.

Değişen/eklenen proje dosyalarının tam listesi:

| Dosya | Görev |
|---|---|
| `.gitignore` | Yerel sentetik test verileri ve bağımlılıkları hariç tutar |
| `ats_matching.py` | Gerçek OCR için yalnız PyPI wheel içindeki yerel ONNX modelleri; dış model indirmesi yok |
| `control/app.py` | Menü, satış route, ortak CSS/menü JS ve eklemeli SQLite şema bağlantısı |
| `control/control.css` | Ortak tasarımı import eder; satış durum rozetleri |
| `control/design-system.css` | Mevcut CONTROL görünümünü koruyan ortak token/bileşen CSS |
| `control/sales-menu.js` | Mobil menüyü seçimde/pageshow'da kapatır |
| `control/sales.py` | Satış veri erişimi, rol/owner kontrolü, iş/finans kuralları |
| `control/sales_http.py` | Auth, form/JSON API, TL dönüşümü, iki Türkçe arayüz |
| `migrations/sales/001_sales.up.sql` | Ortak eklemeli satış tablo/index DDL |
| `migrations/sales/002_sqlite_guards.up.sql` | SQLite append-only trigger korumaları |
| `migrations/control_postgresql/003_sales_hub.up.sql` | PG audit/karar/düzeltme update/delete/truncate koruması |
| `scripts/preview_sales.py` | Her açılışta yeni sentetik localhost önizlemesi |
| `scripts/verify_sales_offline.py` | Dış ağı engelleyen, üretim env temizleyen tam regresyon |
| `scripts/verify_sales_postgres.py` | Sabit doğrulanmış yerel cluster testi; DB reset yok |
| `tests/test_sales_hub.py` | SQLite, gerçek HTTP oturumu/CSRF/IDOR ve finans/TL testleri |
| `tests/test_sales_postgres.py` | PG migration, izolasyon, finans ve audit senaryoları |
| `tests/test_ats_cv_match_integration.py` | Gerçek OCR testinin Windows/Linux font uyumluluğu |
| `.github/workflows/sales-hub-tests.yml` | PR'da sentetik SQLite/PG + mevcut regresyonlar |
| `docs/sales-hub-plan.md` | Keşif, etki, plan ve rollback |
| `docs/sales-hub-test-dependencies.txt` | İzole PyPI test paketlerinin kesin sürümleri |
| `docs/sales-hub-handoff.md` | Bu teknik devir ve devam kaydı |

## Endpoint ve erişim matrisi

Dış URL'ler mevcut gateway ile `/control` öneki alır:

| Endpoint | Yetki / davranış |
|---|---|
| `GET /control/satis` | Yalnız mevcut aktif CONTROL founder oturumu |
| `GET,POST /control/sales/login` | Pasif/olmayan hesap reddedilir; giriş CSRF + rate limit |
| `GET,POST /control/sales` | Temsilci kendi kayıtları; founder tüm satış kayıtları |
| `GET /control/sales/api` | Aynı rol/firma/sahip filtreli snapshot + CSRF |
| `POST /control/sales/api` | JSON action; Origin ve X-CSRF-Token zorunlu |
| `GET /control/sales/api/record?table=...&id=...` | Kendi customer/opportunity/activity/proposal; başkası 404 |
| `GET /control/sales/official?id=...` | Kendi, yalnız approved teklif; founder tüm approved |
| `POST /control/sales/logout` | Oturumu iptal eder; CSRF gerekli |

Temsilci action'ları: customer, opportunity, activity, proposal, submit, password.
Founder action'ları: account_create, account_active, classify, resolve_claim,
decide, collection, project_commission, commission_action, hold, reverse.
Kimlik istemciden alınmaz; sunucunun doğruladığı session'dan türetilir.
JSON minor alanları korunur. HTML formu `_tl` alanını gönderir ve backend
`Decimal` ile `_minor` tamsayısına çevirir. Para limiti 10^12 kuruş.

## Eklenen veritabanı tabloları

`sales_accounts`, `sales_sessions`, `sales_customers`, `sales_customer_identities`,
`sales_claims`, `sales_opportunities`, `sales_activities`, `sales_proposals`,
`sales_decisions`, `sales_subscriptions`, `sales_collections`, `sales_commissions`,
`sales_adjustments`, `sales_audit`. Owner/firma FK'leri ve benzersiz kimlik,
abonelik/dönem, tahsilat referansı, hak ediş/tahsilat index/koşulları DDL'dedir.
Mevcut users rol CHECK'i, İK ve Akademi tabloları değiştirilmedi.

## Yerel ortamı tekrar açma

1. `git status`, `git branch --show-current`, `git log -3 --oneline` ve bu belgeyi oku.
2. Dizin: `C:/Users/Naz/Downloads/ERNASA_NEXUS12_V13_SATIS_YEREL_8098/ernasa-sales-hub`.
3. Yukarıdaki PowerShell PYTHONPATH'i ayarla; test paketleri git'e dahil değildir.
   Başka makinede `docs/sales-hub-test-dependencies.txt` ile izole klasöre kur;
   PyPI dışında paket indirme, SSL kontrolünü kapatma.
4. `scripts/preview_sales.py` çalıştır; `http://127.0.0.1:18099/control/sales/login`
   ve `/control/giris` kullan. Yalnız sentetik örnek hesaplar script içindedir;
   gerçek hesap/parola/token kullanma. Durdurmak için bu sürece Ctrl+C gönder.
5. Offline tam testi ve ayrı yerel PG testini çalıştır. Yerel PG ikilileri üst
   dizindeki `postgresql-18.7-x64-portable/pgsql/bin`; cluster `.sales-test-state/pgdata`.
   Var olan test verisini silme. Başka bir port/cluster'a kendiliğinden bağlanma.

## Canlıya geçişe engeller ve ilk 5 devam işi

1. Bu belgedeki kesin test/PR bilgisini kontrol et; varsa açık test/CI hatasını düzelt.
2. TL/mobile son tarayıcı kanıtlarını kontrol et; güvenlik ve finans regresyonunu koru.
3. Kullanıcının ertelenen kısa kayıt no, yerel tarih, CONTROL sekme ve boş mesaj
   taleplerini ayrıca planla; mevcut UUID/UTC değerlerini değiştirme.
4. Üretim DB yedeği, migration kuru koşusu, least-privilege rol ve rollback planını
   operatörle incele. Supersuser'ın trigger korumasını aşabildiğini hesaba kat.
5. Kullanıcıdan ayrı canlı onayı gelmeden merge, deploy, DNS/TLS ve gerçek hesap
   etkinleştirmesi yapma. İK, Akademi V9 (53 ders), aday/mülakat daveti ve e-posta
   regresyonlarını her entegrasyon turunda koru.

OCR artık wheel'in model dosyalarını kullanır; paket sürümüne bağlı model/bellek
profilini düşük kaynaklı Render staging ortamında ayrıca ölçmek gerekir. Üretim
model davranışı sentetik gerçek OCR testiyle doğrulanacak; test ağ koruması açık.
Bu sürüm son üretim onayı veya eksiksiz ürün lansmanı değildir.
