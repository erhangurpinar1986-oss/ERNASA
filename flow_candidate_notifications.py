"""Deterministic, send-free notification planning for ERNASA HR FLOW.

This component never sends emails, evaluates suitability, or takes hiring decisions.
Callers must perform company-scoped lookups, persist unique event IDs in a DB
outbox, and send only through the existing authorized provider worker.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256

EVENTS = {
    "application_received": ("Başvurunuz alındı", "Başvurunuz bize ulaştı. Sürecinizi takip etmeye devam edeceğiz."),
    "screening_completed": ("Başvurunuz değerlendiriliyor", "Ön inceleme tamamlandı. Bu bir işe alım veya ret kararı değildir."),
    "availability_invitation": ("Mülakat gün ve saat seçimi", "Mülakat için size sunulan uygun zamanlardan birini seçebilirsiniz."),
    "appointment_confirmed": ("Mülakat randevunuz", "Seçtiğiniz mülakat randevusu kaydedildi."),
    "interview_link_ready": ("Mülakat bağlantınız hazır", "Mülakat bağlantınız ve katılım bilgileri hazır."),
    "interview_completed": ("Mülakatınız tamamlandı", "Mülakatınız tamamlandı. Başvurunuz İK değerlendirmesini bekliyor."),
    "awaiting_hr_update": ("Başvurunuz hakkında güncelleme", "Başvurunuz hâlâ değerlendirme aşamasında. Henüz nihai karar verilmedi."),
    "hr_approved": ("Başvurunuzda yeni aşama", "Başvurunuz bir sonraki aşamaya ilerledi. Bu mesaj kesin işe alım teklifi değildir."),
    "hr_rejected": ("Başvurunuzun sonucu", "Başvurunuz değerlendirilmiş ve bu pozisyon için olumsuz sonuçlanmıştır."),
    "second_interview": ("İkinci görüşme daveti", "Başvurunuz için ikinci görüşme aşaması planlanmaktadır."),
    "training_assigned": ("Eğitim ataması", "Başvurunuz kapsamında size bir eğitim atanmıştır. Katılım bilgileri ayrıca iletilecektir."),
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
