"""ERNASA HR FLOW: pozisyon bazında tam otomatik süreç (Issue #65).

İK yöneticisi bir pozisyonda otomasyonu bir kez etkinleştirir. Sonra:
başvuru → ATS → (aday bazında İK onayı istenmeden) uygunluk e-postası → aday
saat seçimi → 8 saatlik mülakat penceresi → AI rapor → İK Karar Merkezi.

Bu modül yeni bir ATS, takvim veya e-posta altyapısı kurmaz:
- Onay kaydı mevcut ``flow_interview_permissions`` tablosuna, otomasyonu
  açan yönetici adına ve açıklamalı olarak yazılır; ``require_fresh_approval``
  ve booking worker'ın yönetici yetki doğrulaması aynen çalışır.
- Takvim ``ats_workflow.save_schedule``; mülakat penceresi
  ``portal_interviews.availability`` (seçilen saat + 8 saat, sunucu tarafı).
- Aday e-postaları PR #66 ``flow_candidate_notifications`` planlayıcısı ve
  ``services.invite_email`` ile gönderilir. Kuyruk kaydı gönderim sayılmaz.
- Yapay zekâ işe alım/ret kararı vermez; otomatik ret yoktur.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

import ats
import ats_schema
import ats_workflow
import control_integration as control
import flow_candidate_notifications as notices
import recruitment
from database import get_connection
from recruitment import write

LOCAL_ZONE = ZoneInfo("Europe/Istanbul")
WINDOW = timedelta(hours=8)  # Kesin kural: erişim seçilen saatte açılır, 8 saat sonra kapanır.
LOG = logging.getLogger(__name__)
PROCEED_STATES = ("analyzed",)
NOTICE_STATES = ("prepared", "sending", "provider_accepted", "delivered", "retry",
                 "failed", "unconfirmed", "cancelled")
REQUEST_KINDS = {"access": "Kişisel verilerime erişim (KVKK)", "deletion": "Verilerimin silinmesi (KVKK)",
                 "correction": "Düzeltme", "objection": "İtiraz",
                 "technical_issue": "Teknik sorun / alternatif katılım"}
# Erişim ve silme mevcut ATS KVKK talep akışına (ats_privacy_requests) yazılır; İK mevcut ekrandan karar verir.
PRIVACY_KINDS = {"access": "export", "deletion": "erase"}

# PR #66 olay kataloğuna, bu modülün gerçek olaylarla tetiklediği üç olay eklenir.
notices.EVENTS.setdefault("interview_reminder", (
    "Mülakat hatırlatması", "Mülakat erişim pencereniz yaklaşıyor."))
notices.EVENTS.setdefault("appointment_rescheduled", (
    "Mülakat randevunuz güncellendi", "Randevunuz yeni seçtiğiniz saate taşındı. Önceki erişim penceresi geçersizdir."))
notices.EVENTS.setdefault("appointment_cancelled", (
    "Mülakat randevunuz iptal edildi", "Randevunuz iptal edildi. Bağlantınız geçerli olduğu sürece yeni bir saat seçebilirsiniz."))

DDL = ats_schema.FLOW_AUTOMATION_DDL
REVIEW_LABELS = {
    "cv_unreadable": "CV teknik olarak incelenemedi",
    "criteria_missing": "Pozisyon değerlendirme kriteri yok",
    "email_invalid": "Aday e-posta adresi geçersiz",
    "identity_conflict": "Aday kimliği / e-posta çakışması",
    "automation_error": "Otomatik akış teknik hatası",
}


def now_utc():
    return datetime.now(timezone.utc)


def stamp(value=None):
    return (value or now_utc()).isoformat()


def initialize_schema():
    with closing(get_connection()) as db:
        cursor = db.cursor()
        for statement in DDL:
            cursor.execute(statement)
        db.commit()


# ---------------------------------------------------------------- ayarlar
class AutomationConfig(BaseModel):
    enabled: bool
    slot_weekdays: list[int] = Field(default=[1, 2, 3, 4, 5], min_length=1, max_length=7)
    slot_start: str = Field(default="09:00", pattern=r"^\d{2}:\d{2}$")
    slot_end: str = Field(default="17:00", pattern=r"^\d{2}:\d{2}$")
    slot_minutes: int = Field(default=30, ge=15, le=240)
    notice_days: int = Field(default=7, ge=1, le=90)
    hr_reminder_days: int = Field(default=14, ge=1, le=90)

    @field_validator("slot_weekdays")
    @classmethod
    def _weekdays(cls, value):
        if any(day not in range(1, 8) for day in value):
            raise ValueError("Gün numaraları 1 (Pazartesi) ile 7 (Pazar) arasında olmalıdır.")
        return sorted(set(value))


def _config_out(row):
    if not row:
        return {"enabled": False, **AutomationConfig(enabled=False).model_dump()}
    return {"enabled": bool(row["enabled"]), "slot_weekdays": [int(x) for x in row["slot_weekdays"].split(",")],
            "slot_start": row["slot_start"], "slot_end": row["slot_end"],
            "slot_minutes": row["slot_minutes"], "notice_days": row["notice_days"],
            "hr_reminder_days": row["hr_reminder_days"], "manager_id": row["manager_id"],
            "updated_at": row["updated_at"]}


def get_config(actor, position_id):
    with closing(get_connection()) as db:
        cursor = db.cursor()
        import hr_flow_screening
        hr_flow_screening._job_criteria(cursor, actor.company_id, position_id, actor)
        cursor.execute("SELECT * FROM flow_position_automation WHERE company_id=? AND position_id=?",
                       (actor.company_id, position_id))
        return _config_out(cursor.fetchone())


def configure(actor, position_id, body: AutomationConfig):
    if actor.role not in recruitment.MANAGERS:
        raise HTTPException(403, "Otomatik süreci yalnızca İK yöneticisi açabilir.")
    if not {"ats", "ai"}.issubset(actor.products or ()) or not actor.portal or not actor.user_id:
        raise HTTPException(403, "Otomatik süreç için ATS ve AI Mülakat abonelikleri gerekir.")
    start, end = time.fromisoformat(body.slot_start), time.fromisoformat(body.slot_end)
    if not start < end:
        raise HTTPException(422, "Randevu başlangıç saati bitişten önce olmalıdır.")
    import hr_flow_screening
    with write(actor.company_id) as cursor:
        _, criteria, _ = hr_flow_screening._job_criteria(cursor, actor.company_id, position_id, actor)
        if body.enabled and not criteria.strip():
            raise HTTPException(409, "Otomatik süreç için önce pozisyonun değerlendirme kriterlerini tanımlayın.")
        cursor.execute("""INSERT INTO flow_position_automation
            (company_id,position_id,enabled,manager_id,access_version,slug,origin,slot_weekdays,
             slot_start,slot_end,slot_minutes,notice_days,hr_reminder_days,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(company_id,position_id) DO UPDATE SET enabled=excluded.enabled,
              manager_id=excluded.manager_id,access_version=excluded.access_version,
              slug=excluded.slug,origin=excluded.origin,slot_weekdays=excluded.slot_weekdays,
              slot_start=excluded.slot_start,slot_end=excluded.slot_end,
              slot_minutes=excluded.slot_minutes,notice_days=excluded.notice_days,
              hr_reminder_days=excluded.hr_reminder_days,updated_at=excluded.updated_at""",
            (actor.company_id, position_id, int(body.enabled), actor.user_id, actor.access_version,
             actor.portal.slug, actor.portal.origin, ",".join(map(str, body.slot_weekdays)),
             body.slot_start, body.slot_end, body.slot_minutes, body.notice_days,
             body.hr_reminder_days, stamp()))
        recruitment.record(cursor, actor, "flow_automation_" + ("enabled" if body.enabled else "disabled"),
                           position_id)
        cursor.execute("SELECT * FROM flow_position_automation WHERE company_id=? AND position_id=?",
                       (actor.company_id, position_id))
        result = _config_out(cursor.fetchone())
        # Replay only recent analyzed applications when a manager explicitly enables
        # this position's automation. Existing permissions/deliveries are preserved.
        # Unreadable/review-required documents must never be silently passed.
        pending = []
        if result["enabled"]:
            cursor.execute("""SELECT a.id AS application_id, s.document_id
                FROM ats_applications a
                JOIN flow_screening_results s ON s.company_id=a.company_id
                    AND s.application_id=a.id AND s.position_id=a.position_id
                LEFT JOIN flow_interview_permissions p ON p.company_id=a.company_id
                    AND p.application_id=a.id
                WHERE a.company_id=? AND a.position_id=?
                    AND a.closure IS NULL AND s.analysis_state='analyzed'
                    AND a.created_at >= ? AND p.id IS NULL
                ORDER BY a.created_at DESC LIMIT 20""",
                (actor.company_id, position_id,
                 stamp(now_utc() - timedelta(days=7))))
            pending = [(row["application_id"], row["document_id"]) for row in cursor.fetchall()]
    # Do not call on_screened() inside the configuration transaction: it opens
    # its own tenant-scoped transaction and enqueues a unique invitation job.
    for application_id, document_id in pending:
        try:
            on_screened(actor.company_id, application_id, document_id)
        except Exception as error:
            LOG.warning("FLOW automatic backlog replay needs review (%s)", type(error).__name__)
            try:
                with write(actor.company_id) as review_cursor:
                    _review(review_cursor, actor.company_id, application_id, "automation_error")
            except Exception:
                pass
    return result


def automation_for_application(cursor, company_id, application_id):
    cursor.execute("""SELECT pa.* FROM ats_applications a
        JOIN flow_position_automation pa ON pa.company_id=a.company_id
            AND pa.position_id=a.position_id AND pa.enabled=1
        WHERE a.company_id=? AND a.id=?""", (company_id, application_id))
    row = cursor.fetchone()
    return dict(row) if row else None


def _system_actor(config):
    """Otomasyonu açan yönetici adına sınırlı iç aktör (yetki booking worker'da yeniden doğrulanır)."""
    return control.Actor(config["company_id"], "hr_manager", control.Portal(config["slug"], config["origin"]),
                         config["manager_id"], config["access_version"], products=("ats", "ai"))


def _review(cursor, company_id, application_id, reason):
    cursor.execute("""INSERT INTO flow_automation_reviews(company_id,application_id,reason_code,created_at)
        VALUES(?,?,?,?) ON CONFLICT(company_id,application_id,reason_code) DO NOTHING""",
        (company_id, application_id, reason, stamp()))


# ---------------------------------------------------------------- ATS sonrası otomatik geçiş
def on_screened(company_id, application_id, document_id):
    """ATS sonucu kaydedildikten sonra çağrılır. Aday bazında İK onayı istemez."""
    import hr_flow_booking
    import hr_flow_screening
    with write(company_id) as cursor:
        config = automation_for_application(cursor, company_id, application_id)
        if not config:
            return "manual"
        cursor.execute("""SELECT a.closure,c.email,e.reported_email AS conflict FROM ats_applications a
            JOIN ats_candidates c ON c.id=a.candidate_id AND c.company_id=a.company_id
            LEFT JOIN ats_candidate_email_conflicts e ON e.candidate_id=c.id AND e.company_id=c.company_id
            WHERE a.company_id=? AND a.id=?""", (company_id, application_id))
        app = cursor.fetchone()
        if not app or app["closure"]:
            return "closed"
        result = hr_flow_screening._result_row(cursor, company_id, application_id, document_id)
        if not result:
            return "not_screened"
        stage_notice(cursor, company_id, application_id, "screening_completed", document_id)
        state = result["analysis_state"]
        if state not in PROCEED_STATES:
            _review(cursor, company_id, application_id,
                    "criteria_missing" if state == "criteria_missing" else "cv_unreadable")
            # An unreadable document is a technical exception, never a low score
            # or an automatic rejection. Notify once per original document.
            try:
                details = json.loads(result["details_json"] or "{}")
            except (ValueError, TypeError):
                details = {}
            if (state == "review_required" and details.get("text_extraction_status") == "unreadable"
                    and not app["conflict"]):
                stage_notice(cursor, company_id, application_id, "cv_unreadable", document_id)
            return "review"
        if app["conflict"]:
            _review(cursor, company_id, application_id, "identity_conflict")
            return "review"
        try:
            from services import invite_email
            invite_email.validate_recipient(app["email"] or "")
        except ValueError:
            _review(cursor, company_id, application_id, "email_invalid")
            return "review"
        if hr_flow_screening.interview_permission(cursor, company_id, application_id):
            return "existing"  # geçmiş izin/mülakat kayıtları korunur
        permission = {"id": uuid4().hex, "application_id": application_id, "document_id": document_id}
        when = stamp()
        cursor.execute("""INSERT INTO flow_interview_permissions
            (id,company_id,application_id,document_id,requested_by,requested_at,status,
             decided_by,decided_at,decision_note) VALUES(?,?,?,?,?,?,'approved',?,?,?)""",
            (permission["id"], company_id, application_id, document_id, config["manager_id"], when,
             config["manager_id"], when, "Otomatik akış: pozisyon otomasyonu (aday bazında İK onayı gerekmez)"))
        hr_flow_booking.enqueue(cursor, _system_actor(config), permission)
    return "invited"


# ---------------------------------------------------------------- randevu saatleri ve pencere
def window_for(start):
    start = start if start.tzinfo else start.replace(tzinfo=LOCAL_ZONE)
    return start, start + WINDOW


def window_text(start):
    begin, end = window_for(start)
    local_b, local_e = begin.astimezone(LOCAL_ZONE), end.astimezone(LOCAL_ZONE)
    return (f"Mülakat bağlantısı erişim başlangıcı: {local_b:%d.%m.%Y %H:%M}\n"
            f"Mülakat bağlantısı erişim bitişi: {local_e:%d.%m.%Y %H:%M} (Türkiye saati, 8 saat)")


def _taken(cursor, config):
    cursor.execute("""SELECT starts_at FROM ats_schedules WHERE company_id=? AND interviewer_id=?
        AND status='planned'""", (config["company_id"], config["manager_id"]))
    taken = set()
    for row in cursor.fetchall():
        try:
            taken.add(datetime.fromisoformat(row["starts_at"]).astimezone(LOCAL_ZONE).strftime("%Y-%m-%dT%H:%M"))
        except (TypeError, ValueError):
            continue
    return taken


def offered_slots(cursor, config, *, now=None, days=14, keep=None):
    now = now or now_utc()
    weekdays = {int(x) for x in str(config["slot_weekdays"]).split(",")}
    begin, end = time.fromisoformat(config["slot_start"]), time.fromisoformat(config["slot_end"])
    step = timedelta(minutes=int(config["slot_minutes"]))
    taken = _taken(cursor, config) - ({keep} if keep else set())
    today = now.astimezone(LOCAL_ZONE).date()
    slots = []
    for offset in range(days + 1):
        day = today + timedelta(days=offset)
        if day.isoweekday() not in weekdays:
            continue
        cursor_at = datetime.combine(day, begin, LOCAL_ZONE)
        while cursor_at.time() < end and cursor_at.date() == day:
            if cursor_at - now >= timedelta(minutes=15):
                key = cursor_at.strftime("%Y-%m-%dT%H:%M")
                if key not in taken:
                    slots.append(key)
            cursor_at += step
    return slots


def _parse_local(value):
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M").replace(tzinfo=LOCAL_ZONE)
    except (TypeError, ValueError):
        raise HTTPException(422, "Geçerli bir gün ve saat seçin.") from None


def require_offered(cursor, company_id, application_id, local_start, *, keep=None):
    config = automation_for_application(cursor, company_id, application_id)
    if config and local_start not in offered_slots(cursor, config, keep=keep):
        raise HTTPException(409, "Seçtiğiniz saat artık uygun değil. Lütfen listeden başka bir saat seçin.")
    return config


def booking_view(token, slug):
    """Aday sayfası için güvenli özet: sunulan saatler, mevcut pencere, değişiklik hakkı."""
    import hr_flow_booking
    with closing(get_connection()) as db:
        cursor = db.cursor()
        job = hr_flow_booking._load_token(cursor, token, slug)
        config = automation_for_application(cursor, job["company_id"], job["application_id"])
        current, link_state = None, None
        if job["schedule_id"]:
            cursor.execute("SELECT starts_at,status FROM ats_schedules WHERE company_id=? AND id=?",
                           (job["company_id"], job["schedule_id"]))
            row = cursor.fetchone()
            if row and row["status"] == "planned":
                current = datetime.fromisoformat(row["starts_at"])
        cursor.execute("""SELECT status FROM interview_links WHERE company_id=? AND application_id=?
            ORDER BY created_at DESC LIMIT 1""", (job["company_id"], job["application_id"]))
        link = cursor.fetchone()
        link_state = link["status"] if link else None
        keep = current.astimezone(LOCAL_ZONE).strftime("%Y-%m-%dT%H:%M") if current else None
        slots = offered_slots(cursor, config) if config else []
    changeable = (job["status"] in ("waiting", "booked", "finished")
                  and link_state not in ("started", "completed", "expired"))
    return {"job": job, "automated": bool(config), "slots": slots, "current": current,
            "changeable": changeable, "link_state": link_state, "keep": keep}


def _move_link(cursor, company_id, application_id, old_schedule, new_schedule, start):
    """Aynı mülakat jetonunun penceresini yeni saate taşır; eski pencere geçersizleşir."""
    if old_schedule:
        cursor.execute("""UPDATE flow_scheduled_interviews SET schedule_id=?
            WHERE company_id=? AND schedule_id=?""", (new_schedule, company_id, old_schedule))
    cursor.execute("""UPDATE interview_links SET expires_at=? WHERE company_id=? AND application_id=?
        AND status IN ('waiting','created','opened') AND token IN
        (SELECT token FROM flow_scheduled_interviews WHERE company_id=? AND schedule_id=?)""",
        (window_for(start)[1].isoformat(), company_id, application_id, company_id, new_schedule))
    cursor.execute("""SELECT COUNT(*) AS n FROM flow_scheduled_interviews WHERE company_id=? AND schedule_id=?""",
                   (company_id, new_schedule))
    return cursor.fetchone()["n"] > 0


def _cancel_schedule(cursor, company_id, schedule_id):
    cursor.execute("""UPDATE ats_schedules SET status='cancelled',revision=revision+1
        WHERE company_id=? AND id=? AND status='planned'""", (company_id, schedule_id))


def change(token, slug, local_start=None, *, cancel=False):
    """Aday randevusunu değiştirir veya iptal eder (mülakat başlamadıysa)."""
    import hr_flow_booking
    view = booking_view(token, slug)
    job = view["job"]
    if not view["automated"]:  # otomasyonsuz pozisyon: mevcut davranış aynen korunur
        return None if cancel else hr_flow_booking.confirm(token, slug, local_start)
    if job["status"] == "waiting" and not cancel:
        return hr_flow_booking.confirm(token, slug, local_start)
    if not cancel and local_start == view.get("keep"):
        return {"status": "unchanged"}
    if not view["changeable"] or job["status"] not in ("booked", "finished"):
        raise HTTPException(409, "Mülakat başladığı, tamamlandığı veya işlem sürdüğü için randevu değiştirilemez.")
    start = None if cancel else _parse_local(local_start)
    actor = hr_flow_booking._actor(job)
    tenant, app_id = job["company_id"], job["application_id"]
    with recruitment.write(tenant) as cursor:
        job = hr_flow_booking._load_token(cursor, token, slug)
        if job["status"] not in ("booked", "finished"):
            raise HTTPException(409, "Randevu durumu değişti; sayfayı yenileyin.")
        cursor.execute("""SELECT status FROM interview_links WHERE company_id=? AND application_id=?
            AND status IN ('started','completed') LIMIT 1""", (tenant, app_id))
        if cursor.fetchone():
            raise HTTPException(409, "Mülakat başladığı için randevu değiştirilemez.")
        old = job["schedule_id"]
        if old:
            _cancel_schedule(cursor, tenant, old)
        if cancel:
            cursor.execute("""UPDATE flow_booking_jobs SET status='waiting',schedule_id=NULL,updated_at=?
                WHERE company_id=? AND application_id=?""", (stamp(), tenant, app_id))
            cursor.execute("""UPDATE flow_candidate_appointment SET confirmed_at=NULL,confirmed_on=NULL,
                updated_at=? WHERE company_id=? AND application_id=?""", (stamp(), tenant, app_id))
            stage_notice(cursor, tenant, app_id, "appointment_cancelled", (old or "none") + ":" + stamp())
            return {"status": "cancelled"}
        require_offered(cursor, tenant, app_id, local_start)
        schedule = ats_workflow.save_schedule(actor, app_id, ats_workflow.Schedule(
            interviewer_id=job["manager_id"], starts_at=start.isoformat(),
            ends_at=(start + timedelta(minutes=30)).isoformat(), kind="video",
            notes="Aday tarafından ERNASA HR FLOW üzerinden yeniden seçildi."), active_cursor=cursor)
        _move_link(cursor, tenant, app_id, old, schedule["id"], start)
        confirmed = start.astimezone(timezone.utc).isoformat()
        cursor.execute("""UPDATE flow_candidate_appointment SET proposed_at=?,confirmed_at=?,
            confirmed_by='candidate',confirmed_on=?,updated_at=? WHERE company_id=? AND application_id=?""",
            (confirmed, confirmed, stamp(), stamp(), tenant, app_id))
        cursor.execute("""UPDATE flow_booking_jobs SET schedule_id=?,updated_at=?
            WHERE company_id=? AND application_id=?""", (schedule["id"], stamp(), tenant, app_id))
        stage_notice(cursor, tenant, app_id, "appointment_rescheduled", schedule["id"],
                     {"window": window_text(start)})
    return {"status": "rescheduled", "starts_at": start.isoformat(), "ends_at": window_for(start)[1].isoformat()}


def after_confirm(cursor, company_id, application_id, schedule_id, start):
    """İlk saat seçimi (veya iptal sonrası yeni seçim) sonrası: onay e-postası ve varsa jetonu taşıma."""
    moved = _move_link(cursor, company_id, application_id, None, schedule_id, start)
    if not moved:
        cursor.execute("""SELECT f.token FROM flow_scheduled_interviews f JOIN interview_links i
            ON i.token=f.token AND i.company_id=f.company_id
            WHERE f.company_id=? AND i.application_id=? AND i.status IN ('waiting','created','opened')""",
            (company_id, application_id))
        row = cursor.fetchone()
        if row:  # iptal sonrası yeniden seçim: mevcut mülakat jetonu yeni pencereye taşınır
            cursor.execute("UPDATE flow_scheduled_interviews SET schedule_id=? WHERE company_id=? AND token=?",
                           (schedule_id, company_id, row["token"]))
            _move_link(cursor, company_id, application_id, None, schedule_id, start)
            moved = True
    stage_notice(cursor, company_id, application_id, "appointment_confirmed", schedule_id,
                 {"window": window_text(start)})
    return moved


# ---------------------------------------------------------------- aday bildirimleri (outbox)
def _tracking_key():
    key = os.getenv("ERNASA_FLOW_TRACKING_KEY", "")
    return key.encode() if len(key) >= 32 else None


def tracking_token(company_id, application_id):
    key = _tracking_key()
    if not key:
        return None
    digest = hmac.new(key, f"{company_id}:{application_id}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def ensure_tracking(cursor, company_id, application_id):
    token = tracking_token(company_id, application_id)
    if token:
        cursor.execute("""INSERT INTO flow_candidate_tracking(token_hash,company_id,application_id,created_at)
            VALUES(?,?,?,?) ON CONFLICT(company_id,application_id) DO NOTHING""",
            (hashlib.sha256(token.encode()).hexdigest(), company_id, application_id, stamp()))
    return token


def stage_notice(cursor, company_id, application_id, event, occurrence, details=None, *, require_automation=True):
    """Gerçek bir olay için kuyruk kaydı hazırlar. Kayıt gönderim değildir; tekrar kaydı engellenir."""
    if require_automation and not automation_for_application(cursor, company_id, application_id):
        return None
    notice = notices.notice_for(company_id=str(company_id), application_id=application_id,
                                event=event, occurrence=str(occurrence))
    cursor.execute("""INSERT INTO flow_candidate_notices
        (id,company_id,application_id,event,status,attempts,next_attempt_at,details_json,created_at,updated_at)
        VALUES(?,?,?,?,'prepared',0,?,?,?,?) ON CONFLICT(id) DO NOTHING""",
        (notice.dedupe_key, company_id, application_id, event, stamp(),
         json.dumps(details or {}, ensure_ascii=False), stamp(), stamp()))
    return notice.dedupe_key


def _final_decision(cursor, company_id, application_id):
    cursor.execute("""SELECT 1 FROM flow_hr_decisions WHERE company_id=? AND application_id=?
        AND decision IN ('approve','reject') LIMIT 1""", (company_id, application_id))
    if cursor.fetchone():
        return True
    cursor.execute("SELECT closure,stage FROM ats_applications WHERE company_id=? AND id=?",
                   (company_id, application_id))
    row = cursor.fetchone()
    return bool(row and (row["closure"] or row["stage"] in ("offer", "hired")))


def compose(cursor, row):
    cursor.execute("""SELECT c.name,c.email,p.name AS position FROM ats_applications a
        JOIN ats_candidates c ON c.id=a.candidate_id AND c.company_id=a.company_id
        JOIN company_positions p ON p.id=a.position_id AND p.company_id=a.company_id
        WHERE a.company_id=? AND a.id=?""", (row["company_id"], row["application_id"]))
    app = cursor.fetchone()
    if not app:
        raise LookupError("application_missing")
    subject, message = notices.EVENTS[row["event"]]
    details = json.loads(row["details_json"] or "{}")
    lines = [f"Merhaba {str(app['name'] or 'Aday')[:120]},", "",
             f"{str(app['position'] or 'Pozisyon')[:160]} başvurunuz hakkında: {message}"]
    if details.get("window"):
        lines += ["", details["window"],
                  "Bu 8 saatlik süre içinde size uygun herhangi bir anda mülakatınızı başlatabilirsiniz."]
    if details.get("reason_hidden") is False and details.get("reason"):
        lines += ["", details["reason"]]
    token = ensure_tracking(cursor, row["company_id"], row["application_id"])
    cursor.execute("SELECT origin FROM flow_position_automation pa JOIN ats_applications a "
                   "ON a.position_id=pa.position_id AND a.company_id=pa.company_id WHERE a.company_id=? AND a.id=?",
                   (row["company_id"], row["application_id"]))
    origin = cursor.fetchone()
    if token and origin:
        lines += ["", "Başvurunuzun durumunu buradan takip edebilirsiniz:",
                  origin["origin"].rstrip("/") + "/basvuru-takip/" + token]
    if details.get("survey_token") and origin:
        from urllib.parse import urlencode
        lines += ["", "İsteğe bağlı aday deneyimi anketi (değerlendirmenizi etkilemez):",
                  origin["origin"].rstrip("/") + "/aday-deneyimi?" + urlencode(
                      {"source": "mulakat", "token": details["survey_token"]})]
    lines += ["", "Bu ileti otomatik gönderilmiştir. İşe alım kararlarını yapay zekâ vermez; "
                  "nihai değerlendirme yetkili İK personeline aittir.", "ERNASA HR FLOW"]
    return app["email"], "ERNASA · " + subject, "\n".join(lines)


def process_notice(send=None):
    """Bir bildirimi gönderir. Sağlayıcı kabulü 'provider_accepted'tır; teslim ayrıca doğrulanmalıdır."""
    from services import invite_email
    send = send or invite_email.send_notice
    with closing(get_connection()) as db:
        cursor = db.cursor()
        stale = stamp(now_utc() - timedelta(minutes=10))
        cursor.execute("""UPDATE flow_candidate_notices SET status='unconfirmed',error_code='send_unconfirmed',
            updated_at=? WHERE status='sending' AND updated_at<?""", (stamp(), stale))
        db.commit()
        cursor.execute("""SELECT * FROM flow_candidate_notices WHERE status IN ('prepared','retry')
            AND next_attempt_at<=? ORDER BY created_at LIMIT 1""", (stamp(),))
        row = cursor.fetchone()
    if not row:
        return False
    row = dict(row)
    tenant = row["company_id"]
    with write(tenant) as cursor:
        cursor.execute("""UPDATE flow_candidate_notices SET status='sending',updated_at=?
            WHERE id=? AND status=?""", (stamp(), row["id"], row["status"]))
        if cursor.rowcount != 1:
            return True
        if row["event"] in ("awaiting_hr_update", "interview_reminder") and _final_decision(
                cursor, tenant, row["application_id"]):
            cursor.execute("UPDATE flow_candidate_notices SET status='cancelled',updated_at=? WHERE id=?",
                           (stamp(), row["id"]))
            return True
        try:
            recipient, subject, body = compose(cursor, row)
            recipient = invite_email.validate_recipient(recipient or "")
        except (LookupError, ValueError):
            cursor.execute("""UPDATE flow_candidate_notices SET status='failed',error_code='invalid_recipient',
                updated_at=? WHERE id=?""", (stamp(), row["id"]))
            return True
    try:
        provider_id = send(recipient, subject, body, idempotency_key="ernasa-notice-" + row["id"][:48])
    except Exception as error:  # sağlayıcı hatası: kontrollü yeniden deneme
        attempts = row["attempts"] + 1
        status = "retry" if attempts < 4 else "failed"
        LOG.warning("FLOW candidate notice deferred (%s)", type(error).__name__)
        with write(tenant) as cursor:
            cursor.execute("""UPDATE flow_candidate_notices SET status=?,attempts=?,next_attempt_at=?,
                error_code=?,updated_at=? WHERE id=?""",
                (status, attempts, stamp(now_utc() + timedelta(minutes=2 ** attempts)),
                 type(error).__name__[:60], stamp(), row["id"]))
        return True
    with write(tenant) as cursor:
        cursor.execute("""UPDATE flow_candidate_notices SET status='provider_accepted',attempts=attempts+1,
            provider_message_id=?,error_code=NULL,updated_at=? WHERE id=?""",
            (str(provider_id)[:200], stamp(), row["id"]))
    return True


def sweep(now=None):
    """Periyodik: mülakat hatırlatması ve uzun beklemede durum bilgilendirmesi (yalnız gerçek durumlardan)."""
    now = now or now_utc()
    staged = 0
    with closing(get_connection()) as db:
        cursor = db.cursor()
        cursor.execute("""SELECT ap.company_id,ap.application_id,ap.confirmed_at FROM flow_candidate_appointment ap
            JOIN ats_applications a ON a.id=ap.application_id AND a.company_id=ap.company_id
            JOIN flow_position_automation pa ON pa.company_id=a.company_id AND pa.position_id=a.position_id
                AND pa.enabled=1
            WHERE ap.confirmed_at IS NOT NULL AND ap.confirmed_by='candidate'""")
        reminders = [dict(r) for r in cursor.fetchall()]
        cursor.execute("""SELECT i.company_id,i.application_id,MAX(i.created_at) AS since,pa.notice_days
            FROM interview_links i JOIN ats_applications a ON a.id=i.application_id AND a.company_id=i.company_id
            JOIN flow_position_automation pa ON pa.company_id=a.company_id AND pa.position_id=a.position_id
                AND pa.enabled=1
            WHERE i.status='completed' GROUP BY i.company_id,i.application_id,pa.notice_days""")
        waiting = [dict(r) for r in cursor.fetchall()]
        # Include candidates who applied before automation was enabled.
        # Only genuine PDF extraction failures qualify; never use score/coverage.
        # Closed applications and already-invited candidates are excluded.
        cursor.execute("""SELECT a.company_id,a.id AS application_id,s.document_id
            FROM ats_applications a
            JOIN flow_position_automation pa ON pa.company_id=a.company_id
                AND pa.position_id=a.position_id AND pa.enabled=1
            JOIN flow_screening_results s ON s.company_id=a.company_id
                AND s.application_id=a.id AND s.position_id=a.position_id
            LEFT JOIN flow_interview_permissions p ON p.company_id=a.company_id
                AND p.application_id=a.id
            LEFT JOIN ats_candidate_email_conflicts e ON e.company_id=a.company_id
                AND e.candidate_id=a.candidate_id
            WHERE a.closure IS NULL AND p.id IS NULL AND e.candidate_id IS NULL
                AND a.created_at >= ?
                AND s.analysis_state='review_required'
                AND s.details_json LIKE '%"unreadable"%'
            ORDER BY a.created_at DESC LIMIT 30""",
            (stamp(now - timedelta(days=7)),))
        unreadable = [dict(r) for r in cursor.fetchall()]
    for item in unreadable:
        with write(item["company_id"]) as cursor:
            if not _final_decision(cursor, item["company_id"], item["application_id"]):
                if stage_notice(cursor, item["company_id"], item["application_id"],
                                "cv_unreadable", item["document_id"]):
                    staged += 1
    for item in reminders:
        try:
            start = datetime.fromisoformat(item["confirmed_at"])
        except (TypeError, ValueError):
            continue
        if timedelta(0) < start - now <= timedelta(hours=24):
            with write(item["company_id"]) as cursor:
                cursor.execute("""SELECT 1 FROM interview_links WHERE company_id=? AND application_id=?
                    AND status IN ('started','completed')""", (item["company_id"], item["application_id"]))
                if not cursor.fetchone():
                    if stage_notice(cursor, item["company_id"], item["application_id"], "interview_reminder",
                                    item["confirmed_at"], {"window": window_text(start)}):
                        staged += 1
    for item in waiting:
        with write(item["company_id"]) as cursor:
            if _final_decision(cursor, item["company_id"], item["application_id"]):
                continue
            cursor.execute("""SELECT MAX(created_at) AS last FROM flow_candidate_notices
                WHERE company_id=? AND application_id=?""", (item["company_id"], item["application_id"]))
            last = cursor.fetchone()["last"] or item["since"]
            if notices.status_update_due(now=now, last_notice_at=datetime.fromisoformat(last),
                                         decision_final=False, days=int(item["notice_days"])):
                iso = now.isocalendar()
                stage_notice(cursor, item["company_id"], item["application_id"], "awaiting_hr_update",
                             f"{iso[0]}-W{iso[1]:02d}")
                staged += 1
    return staged


async def worker(stop):
    last_sweep = 0.0
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        try:
            if loop.time() - last_sweep > 60:
                last_sweep = loop.time()
                await asyncio.to_thread(sweep)
            if await asyncio.to_thread(process_notice):
                continue
        except Exception as error:
            LOG.warning("FLOW notice worker unavailable (%s)", type(error).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except TimeoutError:
            pass


# ---------------------------------------------------------------- aday takip ve talepler
CANDIDATE_LABELS = {
    "application": "Başvurunuz alındı", "ats_done": "Başvurunuz ön incelemeden geçti",
    "availability_pending": "Mülakat daveti hazırlanıyor", "availability_sent": "Mülakat saati seçmeniz bekleniyor",
    "appointment_selected": "Mülakat randevunuz kaydedildi", "invited": "Mülakat bağlantınız gönderildi",
    "interview_opened": "Mülakat bağlantınız açıldı", "interview_started": "Mülakatınız devam ediyor",
    "report_pending": "Mülakatınız tamamlandı, değerlendirme hazırlanıyor",
    "awaiting_hr": "Başvurunuz İK değerlendirmesinde", "second_interview": "İkinci görüşme aşaması",
    "training_assigned": "Eğitim aşaması", "approved": "Başvurunuz bir sonraki aşamaya ilerledi",
    "rejected": "Başvurunuz bu pozisyon için olumsuz sonuçlandı", "closed": "Başvuru süreci kapandı",
}


def _tracking_row(cursor, token):
    if not isinstance(token, str) or len(token) != 43:
        raise HTTPException(404, "Takip bağlantısı geçersiz.")
    cursor.execute("SELECT company_id,application_id FROM flow_candidate_tracking WHERE token_hash=?",
                   (hashlib.sha256(token.encode()).hexdigest(),))
    row = cursor.fetchone()
    if not row:
        raise HTTPException(404, "Takip bağlantısı geçersiz.")
    return row["company_id"], row["application_id"]


def tracking(token):
    import hr_flow_decisions
    with closing(get_connection()) as db:
        cursor = db.cursor()
        tenant, app_id = _tracking_row(cursor, token)
        actor = SimpleNamespace(company_id=tenant, role="hr_manager", user_id=None)
        state = hr_flow_decisions.state_for(cursor, actor, app_id)
        cursor.execute("""SELECT p.name AS position FROM ats_applications a JOIN company_positions p
            ON p.id=a.position_id AND p.company_id=a.company_id WHERE a.company_id=? AND a.id=?""",
            (tenant, app_id))
        position = cursor.fetchone()["position"]
        cursor.execute("""SELECT confirmed_at FROM flow_candidate_appointment WHERE company_id=?
            AND application_id=? AND confirmed_by='candidate'""", (tenant, app_id))
        appointment = cursor.fetchone()
        cursor.execute("""SELECT event,status,created_at FROM flow_candidate_notices WHERE company_id=?
            AND application_id=? ORDER BY created_at""", (tenant, app_id))
        sent = [dict(r) for r in cursor.fetchall()]
        cursor.execute("""SELECT kind,status,created_at FROM flow_candidate_requests WHERE company_id=?
            AND application_id=? ORDER BY created_at""", (tenant, app_id))
        requests = [dict(r) for r in cursor.fetchall()]
        cursor.execute("""SELECT kind,status,created_at FROM ats_privacy_requests WHERE company_id=?
            AND application_id=? ORDER BY created_at""", (tenant, app_id))
        requests += [{"kind": {"export": "access", "erase": "deletion"}.get(r["kind"], r["kind"]),
                      "status": r["status"], "created_at": r["created_at"]} for r in cursor.fetchall()]
    window = None
    if appointment and appointment["confirmed_at"]:
        begin, end = window_for(datetime.fromisoformat(appointment["confirmed_at"]))
        window = {"starts_at": begin.isoformat(), "ends_at": end.isoformat()}
    return {"position": position, "state": state, "label": CANDIDATE_LABELS.get(state, "Başvurunuz işlemde"),
            "window": window, "requests": requests,
            "messages": [{"event": m["event"], "title": notices.EVENTS.get(m["event"], ("",))[0],
                          "sent": m["status"] in ("provider_accepted", "delivered"), "status": m["status"],
                          "created_at": m["created_at"]} for m in sent]}


class CandidateRequest(BaseModel):
    kind: str
    message: str = Field(default="", max_length=1000)


def submit_request(token, body: CandidateRequest):
    if body.kind not in REQUEST_KINDS:
        raise HTTPException(422, "Geçersiz talep türü.")
    with closing(get_connection()) as db:
        tenant, app_id = _tracking_row(db.cursor(), token)
    with write(tenant) as cursor:
        if body.kind in PRIVACY_KINDS:
            kind = PRIVACY_KINDS[body.kind]
            cursor.execute("""SELECT id FROM ats_privacy_requests WHERE company_id=? AND application_id=?
                AND kind=? AND status='requested'""", (tenant, app_id, kind))
            if not cursor.fetchone():
                cursor.execute("INSERT INTO ats_privacy_requests VALUES (?,?,?,?,?,?,?)",
                               (uuid4().hex, tenant, app_id, 0, kind, "requested", stamp()))
            return {"status": "requested", "notice": "KVKK talebiniz kayda alındı ve İK'ya iletildi."}
        cursor.execute("""SELECT COUNT(*) AS n FROM flow_candidate_requests WHERE company_id=? AND application_id=?
            AND status='open'""", (tenant, app_id))
        if cursor.fetchone()["n"] >= 5:
            raise HTTPException(429, "Açık talep sınırına ulaşıldı. İK yanıtını bekleyin.")
        cursor.execute("""INSERT INTO flow_candidate_requests(id,company_id,application_id,kind,message,status,created_at)
            VALUES(?,?,?,?,?,'open',?)""", (uuid4().hex, tenant, app_id, body.kind, body.message.strip(), stamp()))
    return {"status": "open", "notice": "Talebiniz İK'ya iletildi. Kayıt alındı; yanıt e-postayla iletilir."}


# ---------------------------------------------------------------- raporlar
def reports(actor):
    import hr_flow_decisions
    center = hr_flow_decisions.list_center(actor)
    tenant = actor.company_id
    with closing(get_connection()) as db:
        cursor = db.cursor()
        cursor.execute("""SELECT status,COUNT(*) AS n FROM flow_candidate_notices WHERE company_id=?
            GROUP BY status""", (tenant,))
        notice_counts = {r["status"]: r["n"] for r in cursor.fetchall()}
        cursor.execute("""SELECT r.application_id,r.reason_code,r.created_at FROM flow_automation_reviews r
            WHERE r.company_id=? ORDER BY r.created_at DESC LIMIT 100""", (tenant,))
        reviews = [{**dict(r), "label": REVIEW_LABELS.get(r["reason_code"], r["reason_code"])}
                   for r in cursor.fetchall()]
        cursor.execute("""SELECT id,application_id,kind,message,status,created_at FROM flow_candidate_requests
            WHERE company_id=? ORDER BY created_at DESC LIMIT 100""", (tenant,))
        requests = [{**dict(r), "label": REQUEST_KINDS[r["kind"]]} for r in cursor.fetchall()]
        cursor.execute("""SELECT i.application_id,MAX(i.created_at) AS since,MAX(pa.hr_reminder_days) AS days
            FROM interview_links i JOIN ats_applications a ON a.id=i.application_id AND a.company_id=i.company_id
            LEFT JOIN flow_position_automation pa ON pa.company_id=a.company_id AND pa.position_id=a.position_id
            WHERE i.company_id=? AND i.status='completed' GROUP BY i.application_id""", (tenant,))
        completed = {r["application_id"]: r for r in cursor.fetchall()}
    awaiting = [c for c in center["candidates"] if c["state"] == "awaiting_hr"]
    overdue, waits = [], []
    now = now_utc()
    for item in awaiting:
        info = completed.get(item["application_id"])
        if not info:
            continue
        waited = now - datetime.fromisoformat(info["since"])
        waits.append(waited.total_seconds() / 86400)
        if waited >= timedelta(days=int(info["days"] or 14)):
            overdue.append({"application_id": item["application_id"], "name": item["name"],
                            "days": round(waited.total_seconds() / 86400, 1)})
    states = center["counts"]
    return {
        "applications": len(center["candidates"]), "states": states,
        "interviews_completed": len(completed),
        "average_hr_wait_days": round(sum(waits) / len(waits), 1) if waits else None,
        "hr_overdue": overdue, "notices": notice_counts, "reviews": reviews, "requests": requests,
        "note": "Sağlayıcı kabulü teslim anlamına gelmez; teslim durumu sağlayıcı geri bildirimiyle doğrulanır.",
    }
