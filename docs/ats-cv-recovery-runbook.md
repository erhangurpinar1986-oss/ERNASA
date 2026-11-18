# ATS CV kurtarma — üretim öncesi güvenli kontrol (18.11.2026)

**Kapsam:** Bu çalışma yalnızca ATS CV dosya sürekliliği ve mevcut belgenin aynı ID ile kurtarılmasıdır. Adayları, mülakatları, değerlendirmeleri veya eğitimleri sıfırlamayın.

## DUR: dağıtım öncesi
- Render servisini yeniden başlatmayın, redeploy yapmayın, servis diskini değiştirmeyin.
- Render servisinin Filesystem/Disk ayarındaki **kalıcı disk** durumunu ve `ERNASA_IK_UPLOAD_DIRECTORY` değişkeninin gerçek mount yolunu doğrulayın.
- Aynı hizmetin mevcut çalışma örneğine güvenli shell erişimi varsa, **yalnızca okuma** yapan `python scripts/audit_ats_documents.py` çalıştırın. Bu dosya henüz canlıda yoksa önce çıktısını canlıya kod deploy etmeden aktararak çalıştırın. **Yedek alınana kadar GitHub PR birleştirmesi yapmayın.**
- Hem PostgreSQL veritabanının hem mevcut `uploads` klasörünün ayrı, şifrelenmiş, erişimi sınırlandırılmış yedekleri doğrulanmalı. Yedekten bir dosya ve bir veritabanı geri yükleme örneği çalıştırılmadan yedeği güvenilir kabul etmeyin.
- Eski `ats_documents` kayıtlarında dosya bulunmayanları otomatik silmeyin; arşivden, doğrulanmış yedekten veya aynen aynı orijinal dosyadan geri kazanmayı deneyin.

## Yeni eklenen tablo
`ats_document_blobs` mevcut tablolara dokunmadan eklenir. `document_id`, `company_id`, `application_id`, `digest`, `content_base64` içerir. Tablo uygulamanın mevcut veritabanında tutulur, bu nedenle veritabanı kapasitesini ve yedek boyutunu izleyin. 10 MB PDF'nin Base64 hâli yaklaşık 13,3 MB olabilir; uzun vadeli çözüm şifrelenmiş ve erişim kontrollü nesne depolamadır.

## Kontrollü dağıtım
1. Önce yedek/geri yükleme doğrulaması ve kaynak diskte bulunan CV'lerin sağlamlığı.
2. PR testleri ve kod incelemesi; ardından kontrollü dağıtım ve sağlık kontrolü.
3. Önce sentetik CV yükleyin, aynı başvurudan açın, eşleşme analizi yapın.
4. Tekrar dağıtım öncesinde yeni yüklenen dosyanın arşive yazıldığını doğrulayın.
5. Eskiden kayıtlı ve fiziksel dosyası kayıp CV'leri **aynı mevcut başvuruya**, yetkili İK hesabıyla yeniden yükleyin. Tamamen aynı içerik SHA-256 ile eşleşirse mevcut belge ID'si korunur.
6. Firma izolasyonu ve eski aday/mülakat bağlantıları bozulmadığını doğrulayın.

## Kesin yasaklar
- `TRUNCATE`, `DROP`, `DELETE` ile veri temizliği yapmayın.
- CV dosyalarını GitHub'a veya halka açık dizine yüklemeyin.
- Canlıdaki veri içeriklerini, adları veya özel bilgileri test kayıtlarına/loglara kopyalamayın.
- Yedeksiz bir şema veya disk değişikliği yapmayın.

## Salt-okunur denetim raporunun alanları
- `total`: veritabanındaki CV belge kaydı adedi
- `file_present`: diskte mevcut CV adedi
- `legacy_unarchived_file`: diskte var ancak yeni arşiv tablosunda olmayan CV adedi
- `missing_disk_recoverable_from_archive`: fiziksel dosyası kayıp, arşivde mevcut belge adedi
- `missing_disk_and_archive`: hem disk hem arşivde bulunmayan dosya kaydı adedi

**Not:** Bu denetim yalnızca hangi dosyaların var olduğunu sayar; bir dosyanın içeriğinin veya yedeklenmiş hâlinin gerçekten okunabilir olduğunu tek başına garanti etmez.
