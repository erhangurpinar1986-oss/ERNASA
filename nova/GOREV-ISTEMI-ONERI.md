# Günlük görev için önerilen yeni istem (onay bekliyor)

Zamanlanmış çalıştırma içinden görev istemi değiştirilemediği için bu metin burada bekliyor. Kullanıcı canlı sohbette onaylayınca "NOVA Veri Havuzu Araştırması" görevine uygulanacak.

Özet değişiklikler:
1. Her çalıştırma önce `nova-veri-havuzu` dalındaki `nova/` durumunu yükler, kaldığı yerden devam eder; sonunda commit + push eder.
2. A/B/C seviyeleri (YONTEM.md). Eksik kayıt silinmez; yalnızca A dışa aktarılır.
3. 500+ çalışanlılar `KURUMSAL` segmentinde saklanır; KOBİ işlerinden kalan bütçeyle doğrulanır.
4. Çalıştırma başına en fazla 35 WebFetch; 14 gün içinde açılmış URL tekrar açılmaz; limit hatasında 65 dk sonra devam (günde en fazla 2).
5. Sıra: B→A terfi, C→B terfi, yeni keşif (günün sektörü), kurumsal.
6. Rapor: A sayısı / 100 hedef; B ve C ayrı yazılır, hedefe sayılmaz.
7. Bildirim yalnızca yeni A kaydı çıktığında veya durum yüklenemediğinde/push edilemediğinde.
