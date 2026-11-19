# ERNASA Sales Hub — keşif ve uygulama sınırı

19 Kasım 2026. Temel: GitHub main `cab9d937cbfd47e63777ef4594c8370f6d71c949`.
Dal: `feature/ernasa-sales-hub`.

## Mevcut mimari ve etki

İncelenen dosyalar: `backend/main.py`, `control/app.py`, `control/ik_bridge.py`,
`control/postgres.py`, `control/render_gateway.py`, `control_integration.py`,
`database.py`, `academy_assignments.py`, `tests/control_smoke.py`, Akademi testleri,
`migrations/control_postgresql/001_schema_draft.up.sql` ve `002_session_rules.up.sql`.
FastAPI, CONTROL HTTP servisini aynı origin altında `/control` üzerinden sunuyor.
CONTROL kendi firma/kullanıcı/oturum verilerini SQLite veya `ernasa_control` PostgreSQL
şemasında tutuyor. İK ve Akademi ayrı uygulama tabloları kullanıyor.

Mevcut CONTROL kullanıcı rol CHECK koşulunu değiştirmeden ayrı `sales_accounts`
ve `sales_sessions` eklenecek. Kurucu mevcut CONTROL oturumuyla yönetir; satış
temsilcisi ayrı, kurucu tarafından açılan hesapla girer. Akademi ve İK oturumu satış
yetkisi vermez. İki arayüz aynı CONTROL veritabanındaki satış servislerini kullanır.
Mevcut origin kontrolü, scrypt parola özeti, hashed oturumlar, CSRF ve seri yazma
kilidi yeniden kullanılacak. Kayıtlarda firma ve sahip filtresi birlikte uygulanacak.

## Değişiklik planı

- `control/sales.py`: tek iş kuralı/veri katmanı; müşteri, fırsat, not/görev,
  sürümlü teklif, karar geçmişi, abonelik, doğrulanmış tahsilat, komisyon ve audit.
- `control/sales_http.py`: aynı servisleri çağıran Türkçe CONTROL/temsilci ekranı
  ve oturum korumalı JSON API. Orijinal CONTROL logosu ve CSS kullanılacak.
- `control/app.py`: yalnız menü, migration ve satış route bağlantısı.
- Eklemeli SQLite/PostgreSQL migration dosyaları; mevcut tablolar değişmez.
- `tests/test_sales_hub.py`: gerçek HTTP oturum, CSRF, rol/firma/sahip izolasyonu,
  karar sürümü, tekrar tahsilat ve tam sayı kuruş hesap sınırları.

Müşteri sınıflandırması ve resmi fiyat yalnız kurucudan gelir. Temsilci yeni müşteri
iddiasıyla %25 üretemez. Teklif onayı, doğrulanmış tahsilat ve CONTROL hak ediş onayı
ayrı adımlardır. Özel projede otomatik oran yoktur. İade/iptal ödemeyi durdurur;
ters hak ediş yalnız gerekçeli kurucu kararıyla kaydedilir. Sağlayıcı maliyeti ayrı tutulur.

## Doğrulama sırası ve geri dönüş

1. Geçici SQLite üzerinde migration tekrar uygulanabilirliği ve veri koruması.
2. Servis/API: iki temsilci, ikinci firma, yetkisiz kullanıcı, CSRF ve hatalı geçişler.
3. CONTROL karar kuyruğu ve temsilci kayıt/karar/finans ekranları.
4. %25/%5/%0, özel proje, mükerrer dönem, ödeme penceresi, iade kilidi testleri.
5. Mevcut CONTROL, İK/Akademi ve e-posta testleri; dış servisler mock/kapalı.
6. Varsa geçici PostgreSQL üzerinde aynı servis senaryoları; yoksa eksik raporlanır.

Üretim migration/deploy yapılmaz. Onay sonrası operatör yedek alır, migration'ı
transaction içinde uygular. Geri dönüşte önce önceki uygulama sürümüne dönülür;
sales tabloları silinmez, mali kayıtlar ve audit saklanır. Gerçek veriyle test,
e-posta, DNS veya ödeme sağlayıcısı işlemi yapılmaz. PR birleştirme ayrı onay gerektirir.

## 19 Kasım son kapsam kararı
Kurumsal tasarım kullanıcı tarafından onaylandı. Kullanım kotası nedeniyle yeni modül yok; TL girişi, mobil kapanış, OCR offline bağımlılıkları, güvenli commit/push/PR ve teknik devir önceliklendirildi. Kesin durum ve ertelenen talepler `sales-hub-handoff.md` dosyasındadır.
