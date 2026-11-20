# ERNASA Eğitim Platformu — Temel mimari (18 Kasım 2026)

## Ürün sınırı
- İK uygulamasından bağımsız bir kurumsal SaaS ürünüdür. Ayrı veritabanı / servis ve ayrı erişim politikası önerilir.
- `education/schema.sql` yalnızca **taslak**tır. Mevcut ERNASA üretim veritabanında çalıştırılmadı; `backend/main.py` içine import edilmedi; Render'a deploy edilmedi.
- GitHub ana dalındaki İK tablolarını, mevcut aday kayıtlarını, CV arşivini, mülakatları ve raporları değiştirmemek temel ilkedir.

## Mevcut repo denetimi
18 Kasım 2026 itibarıyla `main` dalında canlı LMS API, kurs/sınav/ilerleme modülü ve eğitim veritabanı tablosu bulunmadı. Kurumsal sektörel eğitim tanıtım sayfası bir LMS değildir.

## İlk sürüm modülleri
1. Kurum, kullanıcı ve yetki yönetimi: owner/admin/instructor/learner/auditor.
2. Kurs ve içerik: taslak, sürümlü yayımlama, ders sırası, zorunlu içerik.
3. Atama ve ilerleme: kurum-çalışan kayıtları, deadline, ders yüzde ilerlemesi.
4. Sınav: soru havuzu, seçenekler, cevap anahtarı sadece sunucu, deneme limiti, insan incelemesi.
5. Rapor: kurs tamamlama, sınav başarı/başarısızlık, çalışan-kurs bazlı rapor ve denetim izi.
6. Kişiselleştirme: öğretmen onayından sonra uygulanabilen öneri planı.
7. Sertifika: tamamlanan eğitimler için izlenebilir, iptal edilebilir belge numarası.
8. Paket hakları: backend tarafında doğrulanan lisans ve özellik kontrolleri.

## Kilitli fiyatlandırma (kullanıcı onayı olmadan değiştirilmez)
| Paket | İç referans | Türkiye kampanya TL / ay (KDV hariç) |
|---|---:|---:|
| Starter | 75 USD | 3.750 TL |
| Growth | 150 USD | 7.500 TL |
| Business | 300 USD | 15.000 TL |
| PRO | 500 USD | 25.000 TL |

Türkiye kampanya hesap kuru 1 USD = 50 TL; Türkiye'de yapılan anlaşmalar için son tarih 31 Ocak 2027. Türkiye sözleşme/fatura/tahsilat işlemleri TL olarak yürütülür. Bu fiyatlar *dokümantasyon referansıdır*; ödeme ve abonelik kodu bu commit kapsamında eklenmedi.

İçerik üretimi tek seferlik konu başına KDV hariç: mevcut firma dokümanından standart eğitim 750 TL; sıfırdan özgün konu + sınav 1.750 TL; ileri teknik / mesleki uygulama senaryosu 3.500 TL; özel video/animasyon/uzman onaylı içerik özel teklif. Kişiselleştirilmiş öğrenme planı ek hizmeti aktif öğrenci başına aylık 30 TL.

## Güvenlik ve yayınlama kapıları
- Her API isteği `tenant_id` ve doğrulanmış kullanıcının rolüyle yetkilendirilmeli; kullanıcıdan gelen tenant ID tek başına güvenilir değildir.
- Her yabancı anahtar kurum kapsamını korumalı; farklı kurumun kayıtlarına erişim/ilişki kurma engellenmeli.
- Learner API yanıtlarında `edu_questions.answer_key` ve diğer katılımcı verileri yer almamalı.
- Puanlandırma sunucuda; manuel inceleme ve otomatik öneriler insan yetki sınırlarıyla uygulanmalı.
- Soru ve ders içerikleri sürümlü ve yayımlandıktan sonra değişmez olmalı; mevcut denemeler geriye dönük bozulmamalı.
- DB üzerinde Postgres RLS + API rol politikası, kapsamlı çapraz-tenant testleri, indeks ve migration incelemesi yapılmadan üretime geçilmemeli.
- Erişim/öğrenci kotası ve satın alma durumu sadece backend'de doğrulanmalı.
- KVKK kapsamında açık amaç, saklama süresi, erişim kaydı ve kurum bazlı veri ayrımı tasarlanmalı.

## Planlanan API (henüz uygulanmadı)
- `GET /api/education/me`, `GET /api/education/courses`, `POST /api/education/courses`
- `POST /api/education/courses/{id}/publish`
- `POST /api/education/enrollments`, `GET /api/education/my-learning`
- `PUT /api/education/enrollments/{id}/lessons/{lesson_id}/progress`
- `POST /api/education/assessments/{id}/attempts`, `POST /api/education/attempts/{id}/submit`
- `GET /api/education/reports/completion`, `GET /api/education/reports/exams`
- `POST /api/education/learning-plans/{id}/approve`

## Test kabul kriterleri
- Kurum A kendi verisini okur, B verisini hiçbir rolde okuyamaz/yazamaz.
- Öğrenci cevap anahtarını sorgulayamaz; sınav deneme limiti aşılmaz.
- Zamanında ve tekrar gönderilen ilerleme yazımı kayıpsız ve idempotenttir.
- Eğitimi tamamlayan çalışan için ilerleme, sınav ve sertifika tutarlıdır.
- Otomatik öğrenme önerileri insan onayı olmadan kişiye atanmaz.
- Ana `main`/Render İK dağıtımı ve veri sayıları değişmez.

**Mevcut durum:** Veritabanı taslağı hazır; API, kullanıcı arayüzü, entegrasyon, testler ve yayınlama bu aşamada tamamlanmadı.
