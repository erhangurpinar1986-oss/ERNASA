# HR FLOW canlı düzeltme yayını

1. Üretimdeki Render uygulama commit’ini ve PostgreSQL örneğini kaydet. Render’dan geri yüklenebilir yedek al ve geri yükleme noktasını kaydet. Destekleniyorsa yedeği ayrı örneğe geri yükleyerek doğrula.
2. İzole PostgreSQL ve sentetik CV ile farklı firma, aynı e-posta, belge sürümü, randevu, e-posta ve PDF akışlarını doğrula. Üretim bağlantısı ve gerçek gönderim anahtarı kullanma.
3. Aday ve CV tablolarını yerinde koru. Yeni tabloları eklemeli geçişle oluştur. Dağıtım öncesi ve sonrası salt okunur sorgularla başvuru, CV, mülakat ve yanıt kayıtlarını karşılaştır.
4. Render’da test edilen commit’in canlı olduğunu doğrula. Firma portalında yetkili oturumla Aday Havuzu’nu ve mevcut ekranları kontrol et. Gerçek adaylara test iletisi gönderme.
5. Uygulama hatasında önce önceki Render sürümüne dön. Eklenen tabloları koru. Yedeği ayrı bir örneğe geri yükle ve farkları incele. Üretim veritabanının üzerine geri yükleme, dağıtım sonrası yazıları etkileyebileceği için ayrıca onay gerektirir.
