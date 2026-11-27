# NOVA Veri Havuzu — Araştırma Yöntemi (v2, 2026-11-27)

Durum dosyaları (bu klasör, `nova-veri-havuzu` dalı):
- `havuz.json` — tüm kayıtlar (seviye, segment, kanıt, sonraki adım, mevzuat kontrolleri).
- `kaynak-gunlugu.json` — açılan her URL, tarih ve sonuç. Aynı kaynak gereksiz yere tekrar açılmaz.
- `ret-listesi.json` — iletişim istemeyen firmalar/alan adları. Bu listedekiler A olamaz.
- `disa-aktarim/` — CONTROL › NOVA › Veri havuzu › Toplu içe aktar'a yapıştırılacak dosyalar (yalnızca A).

## Seviyeler
- **A — satışa açık:** firma doğrulandı + resmî sitede kurumsal e-posta veya sabit hat görüldü + mevzuat kontrolleri (aşağıda) tamam.
- **B — iletişim eksik:** firma ve güncel işe alım ihtiyacı (ilan sayfası açıldı) doğrulandı; kurumsal iletişim henüz doğrulanmadı.
- **C — araştırılıyor:** aday tespit edildi (liste/arama sonucu/eski ilan); güncel ilan veya firma doğrulaması eksik.
Eksik bilgili kayıt silinmez; seviyesi ve `next_step` alanı güncellenir. Yalnızca ilan sayfası açılmış ve 45 günden eskiyse B → C'ye düşer.

## Segment
- `KOBI` (≤500 çalışan veya bilinmiyor) — öncelikli.
- `KURUMSAL` (500+ çalışan, holding/borsa/çok uluslu) — elenmez, ayrı tutulur; KOBİ işleri bittikten sonra kalan bütçeyle doğrulanır. Dışa aktarımda yalnızca A olanlar, `sector` sonuna " (Kurumsal)" eklenerek yer alır.

## Mevzuat kontrolleri (A için hepsi true)
`kurumsal_kanal` (info@/ik@/kariyer@/santral; 05xx, kişisel ad.soyad@, ücretsiz posta YOK) · `alan_adi_eslesmesi` · `kisisel_veri_yok` (kişi adı, cep no yok) · `ticari_unvan` (firma adı görünür, gizli ilan değil) · `ret_listesi_kontrolu`.
Not: e-posta gönderiminden önce gelen kutusunda ret/tekrar kontrolü kullanıcının veya NOVA satış görevinin sorumluluğundadır.

## Web erişim bütçesi
- Çalıştırma başına en fazla **35 WebFetch** (saatlik limit paylaşılıyor). Ajanlara açık bütçe verilir (ör. 2 ajan × 15).
- Sıra: (1) B kayıtların resmî site İletişim sayfası (A'ya terfi için en ucuz adım: kayıt başına 1–2 fetch), (2) C kayıtların güncel ilanı, (3) yeni aday keşfi (WebSearch; arama sonucu kanıt sayılmaz), (4) KURUMSAL.
- `kaynak-gunlugu.json`'da 14 gün içinde `ok` olan URL tekrar açılmaz; o kaynaktan kaydedilmiş bilgi kullanılır.
- Limit hatası alınırsa: dur, durumu kaydet, `send_later` ile 65 dk sonra devam iste (günde en fazla 2 devam).

## İlerleme
Hedef: **100 A kaydı**. Raporda A / B / C / Kurumsal sayıları ayrı yazılır; B ve C hedefe sayılmaz.
