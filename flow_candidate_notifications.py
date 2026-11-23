"""Deterministic, send-free notification planning for ERNASA HR FLOW.

This component never sends emails, evaluates suitability, or takes hiring decisions.
Callers must perform company-scoped lookups, persist unique event IDs in a DB
outbox, and send only through the existing authorized provider worker.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256

EVENTS = {
    "application_received": ("Başvurunuz Alındı", "Başvurunuzu başarıyla aldık. Başvurunuz ilgili pozisyonun değerlendirme sürecine dâhil edilmiştir. Sonraki aşamalara ilişkin gelişmeleri sizinle paylaşacağız."),
    "screening_completed": ("Başvurunuzun Ön İncelemesi Tamamlandı", "Özgeçmişinizin ön incelemesi tamamlandı. Başvurunuz değerlendirme sürecinde ilerlemektedir. Bu bilgilendirme bir işe alım veya ret kararı anlamına gelmez."),
    "cv_unreadable": ("Başvurunuz İçin CV Dosyası Hakkında Bilgilendirme", "Özgeçmişinizin PDF dosyası sistemimizde teknik olarak okunamadı. Bu durum başvurunuzun reddedildiği anlamına gelmez. Mülakat aşamasının sağlıklı ilerleyebilmesi için metni seçilebilir bir PDF veya Word kopyasını iletisim@ernasa.com adresine başvurunuzdaki adınız ve pozisyonunuzla birlikte iletmenizi rica ederiz. Başvurunuz inceleme aşamasında korunmaktadır."),
    "availability_invitation": ("Mülakat İçin Görüşme Zamanı Seçimi", "Başvurunuz kapsamında mülakat aşamasına ilişkin uygun görüşme zamanını seçmeniz beklenmektedir. Gün ve saat seçim bağlantınız ayrı bir e-postada paylaşılır."),
    "appointment_confirmed": ("Mülakat Randevunuz Kaydedildi", "Seçtiğiniz mülakat tarihi ve saati sistemimize kaydedilmiştir. Mülakata erişim bilgileriniz ilgili aşamada ayrıca paylaşılacaktır."),
    "interview_link_ready": ("Çevrim İçi Mülakat Erişim Bilgileri", "Çevrim içi mülakatınıza ilişkin erişim bilgileri hazırlandı. Katılım bağlantınızı içeren ayrı davet e-postasını kontrol edebilirsiniz."),
    "interview_completed": ("Mülakatınız Tamamlandı", "Mülakatınız başarıyla tamamlandı. Değerlendirme raporunuz yetkili İK ekibinin incelemesine sunulacaktır. Nihai karar henüz verilmemiştir."),
    "awaiting_hr_update": ("Başvurunuz Hakkında Bilgilendirme", "Başvurunuzun değerlendirme süreci devam etmektedir. Sonuçla ilgili kesin bir karar oluştuğunda sizi bilgilendireceğiz. Gösterdiğiniz sabır için teşekkür ederiz."),
    "hr_approved": ("Başvurunuzda Yeni Aşama", "Başvurunuzun bir sonraki aşamaya ilerlemesi uygun görülmüştür. Bu bilgilendirme tek başına kesin iş teklifi veya işe alım onayı değildir. İlgili ayrıntılar ayrıca paylaşılacaktır."),
    "hr_rejected": ("Başvuru Sonucunuz Hakkında", "Başvurunuza ayırdığınız zaman ve ERNASA'ya gösterdiğiniz ilgi için teşekkür ederiz. Yapılan değerlendirme sonucunda bu pozisyon için sürecinizi ilerletemiyoruz. Gelecek başvurularınızda başarılar dileriz."),
    "second_interview": ("İkinci Görüşme Süreciniz", "Başvurunuz için ikinci bir görüşme yapılması planlanmaktadır. Görüşmenin takvimi ve katılım bilgileri kesinleştiğinde ayrıca iletilecektir."),
    "training_assigned": ("Eğitim Süreciniz Hakkında", "Başvurunuz kapsamında bir eğitim aşaması tanımlanmıştır. Eğitim bağlantısı ve katılım koşulları ayrı bir davet mesajında paylaşılacaktır."),
}
TERMINAL = {"hr_approved", "hr_rejected"}

@dataclass(frozen=True)
class Notice:
    event: str
    dedupe_key: str
    subject: str
    message: str


def notice_for(*, company_id: str, application_id: str, event: str, occurrence: str) -> Notice:
    """Plan a notification for a verified real event; no I/O or delivery claim.

    occurrence: stable event ID or ISO week for periodic status notices.
    Do not use PII as the occurrence identifier.
    """
    if not all(isinstance(x, str) and x.strip() for x in (company_id, application_id, event, occurrence)):
        raise ValueError("Şirket, başvuru, olay ve olay kimliği zorunludur.")
    if event not in EVENTS:
        raise ValueError("Bilinmeyen aday bildirim olayı.")
    key_material = "\x1f".join((company_id, application_id, event, occurrence))
    subject, message = EVENTS[event]
    return Notice(event, sha256(key_material.encode("utf-8")).hexdigest(), subject, message)


def status_update_due(*, now: datetime, last_notice_at: datetime, decision_final: bool,
                      days: int = 7) -> bool:
    """A final decision suspends pending updates; requires timezone-aware dates."""
    if not 1 <= days <= 90:
        raise ValueError("Bildirim aralığı 1–90 gün olmalıdır.")
    if now.tzinfo is None or last_notice_at.tzinfo is None:
        raise ValueError("Saat bilgisi zaman dilimli olmalıdır.")
    return not decision_final and now >= last_notice_at + timedelta(days=days)


def hr_reminder_due(*, now: datetime, awaiting_since: datetime,
                    decision_final: bool, days: int = 14) -> bool:
    return status_update_due(now=now, last_notice_at=awaiting_since,
                             decision_final=decision_final, days=days)
