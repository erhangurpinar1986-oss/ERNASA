"""ERNASA HR FLOW · İK Karar Merkezi.

Mevcut ATS, HR FLOW, Akademi V9 ve takvim kayıtları üzerinde çalışan ince bir okuma
ve karar katmanıdır. Aynı iş için ikinci bir ATS, randevu, e-posta veya eğitim sistemi
kurmaz:

* Onayla        -> mevcut ATS aşama geçişi (ats_workflow.move, 'offer')
* Reddet        -> mevcut olumsuz kapanış (closure='negative') + gerekçe ats_notes'a
* İkinci görüşme-> mevcut ATS aşaması 'interview' + isteğe bağlı ats_schedules kaydı
* Eğitim ata    -> mevcut Akademi V9 ataması (academy_assignments.record_assignment)

Eklenen iki tablo yalnızca mevcut sistemde karşılığı olmayan kayıtlar içindir:
* flow_hr_decisions      : kararın türü, gerekçesi, karar veren ve zamanı
                           (ats_stage_events gerekçe/karar türü taşımaz; eğitim
                           atamasının başvuruyla bağı başka yerde tutulmaz)
* flow_candidate_events  : adayın kendi yaptığı işlemler (uygunluk bağlantısını açma,
                           saat seçme, mülakat bağlantısını açma, başlatma, tamamlama).
                           recruitment_audit bir İK kullanıcısı zorunlu tutar; aday
                           olaylarının zaman damgası başka hiçbir tabloda yoktur.

Kurallar:
* Karar yalnızca firma yöneticisi (supervisor / hr_manager) tarafından verilir.
* Yapay zekâ veya otomasyon karar vermez; puanlar yalnızca karar destek verisidir.
* Bu modül e-posta GÖNDERMEZ. Eğitim daveti mevcut "davet gönder" ucu ile ayrıca
  ve açıkça gönderilir.
* Gerçekleşmemiş bir işlem tamamlanmış gösterilmez: her adım kaynak kaydıyla döner.
"""
import hashlib
import json
import logging
import re
from contextlib import closing
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field

import academy_assignments
import ats
import ats_workflow
from database import get_connection
from recruitment import MANAGERS, record, write

LOG = logging.getLogger(__name__)

CANDIDATE_EVENTS = (
    "availability_link_opened",  # adayın uygunluk (saat seçme) bağlantısını açması
    "appointment_selected",      # adayın görüşme saatini seçmesi
    "interview_link_opened",     # adayın mülakat bağlantısını açması
    "interview_started",         # adayın mülakatı başlatması
    "interview_completed",       # son cevabın kaydedilmesiyle mülakatın tamamlanması
)
DECISIONS = ("approve", "reject", "second_interview", "assign_training")
DECISION_LABELS = {"approve": "Onaylandı", "reject": "Reddedildi",
                   "second_interview": "İkinci görüşme", "assign_training": "Eğitim atandı"}
DECIDABLE_STATES = ("awaiting_hr", "second_interview", "training_assigned")

STATE_LABELS = {
    "application": "Başvuru alındı",
    "ats_done": "ATS ön değerlendirmesi tamamlandı",
    "permission_pending": "Mülakat izni yönetici onayı bekliyor",
    "permission_declined": "Mülakat izni verilmedi",
    "availability_pending": "Uygunluk e-postası hazırlanıyor",
    "availability_sent": "Adaydan saat seçmesi bekleniyor",
    "appointment_selected": "Aday görüşme saatini seçti",
    "interview_invited": "Mülakat bağlantısı oluşturuldu",
    "interview_opened": "Aday mülakat bağlantısını açtı",
    "interview_in_progress": "Mülakat devam ediyor",
    "report_pending": "Mülakat tamamlandı · rapor hazırlanıyor",
    "awaiting_hr": "İK kararı bekliyor",
    "second_interview": "İkinci görüşme",
    "training_assigned": "Eğitim atandı",
    "approved": "Onaylandı · teklif aşaması",
    "hired": "İşe alındı",
    "rejected": "Reddedildi",
    "closed": "Başvuru kapatıldı",
}
CLOSURE_LABELS = {"negative": "Olumsuz", "withdrawn": "Aday çekildi", "position_closed": "Pozisyon kapandı"}


def now():
    return datetime.now(timezone.utc).isoformat()


def initialize_schema():
    """Yalnızca ekleme yapar. Tablo tanımları ats_schema.DDL içindedir; böylece
    database.initialize_database() çağıran her ortamda (test, araç, uygulama) bulunur."""
    import ats_schema
    with closing(get_connection()) as db:
        c = db.cursor()
        for statement in ats_schema.DDL:
            if "flow_candidate_events" in statement or "flow_hr_decisions" in statement:
                c.execute(statement)
        db.commit()


def subject_ref(value):
    """Token gibi erişim değerleri saklanmaz; yalnızca kısa bir SHA-256 özeti tutulur."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------- aday olayları

def record_candidate_event(cursor, company_id, application_id, subject, event):
    """Açık işlem içinde, savepoint ile en iyi çabayla kaydeder.

    Kayıt başarısız olsa bile adayın mülakat veya randevu işlemi bozulmaz; yalnızca
    olay görünmez (ekranda 'kaydı yok' olarak gösterilir, tamamlandı denmez).
    """
    if company_id is None or event not in CANDIDATE_EVENTS or not subject:
        return False
    try:
        cursor.execute("SAVEPOINT flow_candidate_event")
    except Exception as error:  # savepoint desteklenmiyorsa olay atlanır
        LOG.warning("HR FLOW candidate event skipped (%s)", type(error).__name__)
        return False
    try:
        cursor.execute("""INSERT INTO flow_candidate_events
            (id,company_id,application_id,subject,event,occurred_at) VALUES(?,?,?,?,?,?)
            ON CONFLICT(company_id,subject,event) DO NOTHING""",
            (uuid4().hex, company_id, application_id, subject_ref(subject), event, now()))
        created = cursor.rowcount == 1
        cursor.execute("RELEASE SAVEPOINT flow_candidate_event")
        return created
    except Exception as error:
        LOG.warning("HR FLOW candidate event not recorded (%s)", type(error).__name__)
        try:
            cursor.execute("ROLLBACK TO SAVEPOINT flow_candidate_event")
            cursor.execute("RELEASE SAVEPOINT flow_candidate_event")
        except Exception:
            pass
        return False


def record_candidate_event_now(company_id, application_id, subject, event):
    """Açık bir işlem olmayan uçlar için (ör. uygunluk sayfasının açılması)."""
    if company_id is None:
        return False
    try:
        with closing(get_connection()) as db:
            created = record_candidate_event(db.cursor(), company_id, application_id, subject, event)
            db.commit()
            return created
    except Exception as error:
        LOG.warning("HR FLOW candidate event unavailable (%s)", type(error).__name__)
        return False


def interview_event(cursor, token, company_id, event):
    """Mülakat belirtecinden başvuruyu bulup olayı kaydeder (yalnızca firma portalı)."""
    if company_id is None:
        return False
    try:
        cursor.execute("SELECT application_id FROM interview_links WHERE token=? AND company_id=?",
                       (token, company_id))
        row = cursor.fetchone()
    except Exception:
        return False
    return record_candidate_event(cursor, company_id, row["application_id"] if row else None, token, event)


# --------------------------------------------------------------------- okuma modeli

_SECTION = re.compile(r"^\s*([A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜ /]{3,}):\s*$")


def report_sections(text):
    """AI raporunu mevcut başlıklarına göre böler; metni değiştirmez veya yorumlamaz."""
    sections, current = {}, None
    for line in (text or "").splitlines():
        match = _SECTION.match(line)
        if match:
            current = match.group(1).strip()
            sections[current] = []
            continue
        if current and line.strip():
            sections[current].append(re.sub(r"^[-•*\s]+", "", line).strip())
    return sections


def report_score(text):
    """Rapordaki 'Genel Pozisyon Uyumu' yüzdesini okur; yoksa None (uydurulmaz)."""
    match = re.search(r"Genel Pozisyon Uyumu[^\n\d]{0,20}%?\s*(\d{1,3})", text or "")
    if not match:
        return None
    value = int(match.group(1))
    return value if 0 <= value <= 100 else None


def _in_clause(values):
    return ",".join("?" for _ in values)


def _latest_screening(cursor, tenant, app_ids):
    if not app_ids:
        return {}
    cursor.execute(f"""SELECT s.application_id,s.analysis_state,s.match_score,s.coverage,s.analyzed_at,
            s.document_id,s.details_json
        FROM flow_screening_results s
        JOIN ats_applications a ON a.id=s.application_id AND a.company_id=s.company_id
            AND a.position_id=s.position_id
        WHERE s.company_id=? AND s.application_id IN ({_in_clause(app_ids)})
          AND s.document_id=(SELECT cv.id FROM ats_documents cv
                WHERE cv.company_id=s.company_id AND cv.application_id=s.application_id
                ORDER BY cv.created_at DESC,cv.id DESC LIMIT 1)""", (tenant, *app_ids))
    return {row["application_id"]: dict(row) for row in cursor.fetchall()}


def _latest_interviews(cursor, tenant, app_ids):
    if not app_ids:
        return {}
    cursor.execute(f"""SELECT i.token,i.application_id,i.status,i.created_at,i.expires_at,
            r.created_at AS report_at, r.job_fit_analysis
        FROM interview_links i LEFT JOIN interview_reports r ON r.token=i.token
        WHERE i.company_id=? AND i.application_id IN ({_in_clause(app_ids)})
        ORDER BY i.created_at DESC""", (tenant, *app_ids))
    latest = {}
    for row in cursor.fetchall():
        latest.setdefault(row["application_id"], dict(row))
    return latest


def _by_application(cursor, sql, tenant, app_ids):
    if not app_ids:
        return {}
    cursor.execute(sql.format(ids=_in_clause(app_ids)), (tenant, *app_ids))
    return {row["application_id"]: dict(row) for row in cursor.fetchall()}


def _latest_decisions(cursor, tenant, app_ids):
    if not app_ids:
        return {}
    cursor.execute(f"""SELECT * FROM flow_hr_decisions
        WHERE company_id=? AND application_id IN ({_in_clause(app_ids)})
        ORDER BY decided_at DESC""", (tenant, *app_ids))
    latest = {}
    for row in cursor.fetchall():
        latest.setdefault(row["application_id"], dict(row))
    return latest


def derive_state(app, screening, permission, booking, appointment, outbox, interview, events, decision):
    """Durumu yalnızca var olan kayıtlardan türetir."""
    if app["closure"]:
        return "rejected" if app["closure"] == "negative" else "closed"
    if app["stage"] == "hired":
        return "hired"
    if app["stage"] == "offer":
        return "approved"
    interview_done = bool(interview and interview["status"] == "completed")
    report_ready = bool(interview_done and interview.get("job_fit_analysis"))
    if decision and decision["decision"] == "second_interview":
        return "second_interview"
    if decision and decision["decision"] == "assign_training":
        return "training_assigned"
    if report_ready:
        return "awaiting_hr"
    if interview_done:
        return "report_pending"
    if interview:
        if interview["status"] == "started":
            return "interview_in_progress"
        if interview["status"] == "opened":
            return "interview_opened"
        return "interview_invited"
    if appointment and appointment.get("confirmed_by") == "candidate" and appointment.get("confirmed_on"):
        return "appointment_selected"
    if booking and booking.get("status") in ("waiting", "booked", "dispatching"):
        return "availability_sent"
    if permission:
        if permission["status"] == "pending":
            return "permission_pending"
        if permission["status"] == "declined":
            return "permission_declined"
        return "availability_pending"
    if screening:
        return "ats_done"
    return "application"


def _collect(cursor, actor, app_rows):
    tenant = actor.company_id
    ids = [row["id"] for row in app_rows]
    screenings = _latest_screening(cursor, tenant, ids)
    interviews = _latest_interviews(cursor, tenant, ids)
    permissions = _by_application(cursor, """SELECT application_id,status,requested_at,decided_at,decided_by,decision_note
        FROM flow_interview_permissions WHERE company_id=? AND application_id IN ({ids})""", tenant, ids)
    bookings = _by_application(cursor, """SELECT application_id,status,expires_at,updated_at,error_code
        FROM flow_booking_jobs WHERE company_id=? AND application_id IN ({ids})""", tenant, ids)
    appointments = _by_application(cursor, """SELECT application_id,proposed_at,confirmed_at,confirmed_by,
        confirmed_on FROM flow_candidate_appointment WHERE company_id=? AND application_id IN ({ids})""", tenant, ids)
    outbox = _by_application(cursor, """SELECT application_id,status,provider_message_id,updated_at
        FROM flow_contact_outbox WHERE company_id=? AND purpose='availability_request'
        AND application_id IN ({ids})""", tenant, ids)
    decisions = _latest_decisions(cursor, tenant, ids)
    return screenings, interviews, permissions, bookings, appointments, outbox, decisions


def state_for(cursor, actor, app_id):
    """Tek başvuru için durum (aday takip sayfası); yalnızca var olan kayıtlardan türetilir."""
    cursor.execute("""SELECT a.id,a.stage,a.closure,a.revision FROM ats_applications a
        WHERE a.company_id=? AND a.id=?""", (actor.company_id, app_id))
    app = cursor.fetchone()
    if not app:
        raise HTTPException(404, "Başvuru bulunamadı.")
    app = dict(app)
    screenings, interviews, permissions, bookings, appointments, outbox, decisions = _collect(cursor, actor, [app])
    return derive_state(app, screenings.get(app_id), permissions.get(app_id), bookings.get(app_id),
                        appointments.get(app_id), outbox.get(app_id), interviews.get(app_id), None,
                        decisions.get(app_id))


NOTICE_LABELS = {"prepared": "Hazırlandı (gönderilmedi)", "sending": "Gönderiliyor",
                 "provider_accepted": "Sağlayıcı kabul etti", "delivered": "Teslim edildi",
                 "retry": "Yeniden denenecek", "failed": "Başarısız", "unconfirmed": "Sonuç doğrulanamadı (İK kontrolü)",
                 "cancelled": "İptal edildi (gerek kalmadı)"}
DECISION_NOTICE = {"approve": "hr_approved", "reject": "hr_rejected",
                   "second_interview": "second_interview", "assign_training": "training_assigned"}


def _notices(cursor, tenant, app_id):
    try:
        cursor.execute("SAVEPOINT flow_notice_read")
        cursor.execute("""SELECT id,event,status,attempts,provider_message_id,error_code,created_at,updated_at
            FROM flow_candidate_notices WHERE company_id=? AND application_id=? ORDER BY created_at""",
            (tenant, app_id))
        rows = [dict(r) for r in cursor.fetchall()]
        cursor.execute("RELEASE SAVEPOINT flow_notice_read")
    except Exception:
        cursor.execute("ROLLBACK TO SAVEPOINT flow_notice_read")
        return []
    import flow_candidate_notifications as planner
    return [{"event": r["event"], "title": planner.EVENTS.get(r["event"], (r["event"],))[0],
             "status": r["status"], "status_label": NOTICE_LABELS.get(r["status"], r["status"]),
             "attempts": r["attempts"], "has_provider_id": bool(r["provider_message_id"]),
             "error_code": r["error_code"], "created_at": r["created_at"], "updated_at": r["updated_at"]}
            for r in rows]


def list_center(actor, limit=300):
    """Sol panel: aday listesi, durum ve iki ayrı puan (birleştirilmez)."""
    staff, params = ats.author_clause(actor)
    with closing(get_connection()) as db:
        cursor = db.cursor()
        cursor.execute(f"""SELECT a.id,a.stage,a.closure,a.revision,a.created_at,a.changed_at,a.position_id,
                c.name,COALESCE(c.email,e.reported_email) AS email,p.name AS position
            FROM ats_applications a
            JOIN ats_candidates c ON c.id=a.candidate_id AND c.company_id=a.company_id
            LEFT JOIN ats_candidate_email_conflicts e ON e.candidate_id=c.id AND e.company_id=c.company_id
            JOIN company_positions p ON p.id=a.position_id AND p.company_id=a.company_id
            WHERE a.company_id=? {staff}
            ORDER BY a.changed_at DESC,a.id LIMIT ?""", (actor.company_id, *params, limit))
        apps = [dict(row) for row in cursor.fetchall()]
        screenings, interviews, permissions, bookings, appointments, outbox, decisions = _collect(cursor, actor, apps)
    items, counts = [], {key: 0 for key in STATE_LABELS}
    for app in apps:
        app_id = app["id"]
        interview = interviews.get(app_id)
        state = derive_state(app, screenings.get(app_id), permissions.get(app_id), bookings.get(app_id),
                             appointments.get(app_id), outbox.get(app_id), interview, None, decisions.get(app_id))
        counts[state] += 1
        screening = screenings.get(app_id)
        items.append({
            "application_id": app_id, "name": app["name"], "position": app["position"],
            "email": app["email"], "stage": app["stage"], "closure": app["closure"],
            "state": state, "state_label": STATE_LABELS[state],
            "ats_score": screening["match_score"] if screening else None,
            "ats_coverage": screening["coverage"] if screening else None,
            "interview_score": report_score(interview.get("job_fit_analysis")) if interview else None,
            "changed_at": app["changed_at"],
        })
    return {"candidates": items, "counts": counts,
            "states": [{"code": key, "label": label} for key, label in STATE_LABELS.items()],
            "can_decide": actor.role in MANAGERS}


def _step(code, title, state, detail, at=None, source=None):
    """state: done | waiting | missing | failed | not_applicable"""
    return {"code": code, "title": title, "state": state, "detail": detail, "at": at, "source": source}


def _timeline(app, documents, screening, permission, booking, appointment, outbox, interview,
              email, events, answers, assessment, decisions):
    ev = {row["event"]: row["occurred_at"] for row in events}
    steps = [_step("application", "Başvuru alındı", "done", "ATS başvuru kaydı.", app["created_at"], "ats_applications")]
    steps.append(_step("cv", "CV yüklendi", "done" if documents else "missing",
                       f"{len(documents)} belge kayıtlı." if documents else "Başvuruya bağlı CV kaydı yok.",
                       documents[-1]["created_at"] if documents else None, "ats_documents" if documents else None))
    if screening:
        steps.append(_step("ats", "ATS ön değerlendirmesi", "done",
                           {"analyzed": "Analiz tamamlandı.", "review_required": "Analiz yapıldı; kanıt yetersiz, İK incelemesi gerekli.",
                            "criteria_missing": "Pozisyon kriteri tanımlı olmadığı için puan üretilmedi."}.get(
                               screening["analysis_state"], screening["analysis_state"]),
                           screening["analyzed_at"], "flow_screening_results"))
    else:
        steps.append(_step("ats", "ATS ön değerlendirmesi", "waiting", "ATS ön değerlendirme kaydı yok."))
    if permission:
        state = {"approved": "done", "declined": "failed"}.get(permission["status"], "waiting")
        detail = {"approved": "Yönetici mülakat iznini onayladı.", "declined": "Yönetici mülakat izni vermedi.",
                  "pending": "Mülakat izni yönetici onayı bekliyor."}[permission["status"]]
        automatic = str(permission.get("decision_note") or "").startswith("Otomatik akış")
        if automatic:
            detail = "Pozisyonda otomatik süreç açık: aday bazında İK onayı istenmedi (ayarı açan yönetici adına kaydedildi)."
        steps.append(_step("permission", "Mülakat daveti izni (otomatik süreç)" if automatic else "Mülakat izni (İK yönetici onayı)",
                           state, detail,
                           permission["decided_at"] or permission["requested_at"], "flow_interview_permissions"))
    else:
        steps.append(_step("permission", "Mülakat izni (İK yönetici onayı)", "missing", "Mülakat izni talebi kaydı yok."))
    if outbox:
        accepted = outbox["status"] in ("provider_accepted", "delivered") and outbox["provider_message_id"]
        steps.append(_step("availability_email", "Uygunluk e-postası",
                           "done" if accepted else "failed" if outbox["status"] == "failed" else "waiting",
                           "E-posta servisi isteği kabul etti; adaya teslim edildiği ayrıca kanıtlanmadı." if accepted
                           else "E-posta gönderilemedi." if outbox["status"] == "failed"
                           else "E-posta hazırlandı; servis kabul kaydı yok, gönderildi denemez.",
                           outbox["updated_at"], "flow_contact_outbox"))
    else:
        steps.append(_step("availability_email", "Uygunluk e-postası", "missing", "Uygunluk e-postası kaydı yok."))
    steps.append(_step("availability_link_opened", "Aday uygunluk bağlantısını açtı",
                       "done" if ev.get("availability_link_opened") else "missing",
                       "Adayın bağlantıyı ilk açtığı an kaydedildi." if ev.get("availability_link_opened")
                       else "Açılma kaydı yok." + (" (Bu kayıt sistemi güncellemeden önce tutulmuyordu.)" if appointment else ""),
                       ev.get("availability_link_opened"), "flow_candidate_events" if ev.get("availability_link_opened") else None))
    picked = bool(appointment and appointment.get("confirmed_by") == "candidate" and appointment.get("confirmed_on"))
    steps.append(_step("appointment_selected", "Aday görüşme saatini seçti", "done" if picked else "missing",
                       ("Aday saati kendisi seçti: " + str(appointment["confirmed_at"])) if picked
                       else "Adayın seçtiği bir saat kaydı yok. İK tarafından girilen takvim notu aday seçimi sayılmaz.",
                       appointment["confirmed_on"] if picked else None, "flow_candidate_appointment" if picked else None))
    if interview:
        steps.append(_step("interview_link", "Mülakat bağlantısı oluşturuldu", "done",
                           "Bağlantı oluşturuldu. Son durum: " + str(interview["status"]), interview["created_at"], "interview_links"))
        sent = bool(email and email["status"] == "sent" and email["provider_message_id"])
        steps.append(_step("interview_email", "Mülakat daveti e-postası",
                           "done" if sent else "failed" if email and email["status"] == "failed" else "missing" if not email else "waiting",
                           "E-posta servisi kabul etti; teslim ayrıca kanıtlanmadı." if sent
                           else "Davet e-postası kaydı yok." if not email else "Servis kabul kaydı yok; gönderildi denemez.",
                           (email["sent_at"] or email["initiated_at"]) if email else None,
                           "ik_invitation_email_sends" if email else None))
        status = interview["status"]
        reached_open = status in ("opened", "started", "completed")
        reached_start = status in ("started", "completed")
        for code, title, reached, fallback in (
            ("interview_link_opened", "Aday mülakat bağlantısını açtı", reached_open, None),
            ("interview_started", "Aday mülakatı başlattı", reached_start,
             answers[0]["created_at"] if answers else None),
        ):
            if ev.get(code):
                steps.append(_step(code, title, "done", "Olay zamanı kaydedildi.", ev[code], "flow_candidate_events"))
            elif reached:
                detail = "Mülakat durumu bu adımın gerçekleştiğini gösteriyor; olay zamanı kaydı yok (güncelleme öncesi kayıt)."
                if fallback:
                    detail += " İlk cevabın kayıt zamanı gösteriliyor."
                steps.append(_step(code, title, "done", detail, fallback, "interview_links"))
            else:
                steps.append(_step(code, title, "missing", "Bu adım için kayıt yok."))
        completed = status == "completed"
        last_answer = answers[-1]["created_at"] if answers else None
        steps.append(_step("interview_completed", "Mülakat tamamlandı", "done" if completed else "missing",
                           f"{len(answers)} cevap kayıtlı." if completed else f"{len(answers)} cevap kayıtlı; mülakat tamamlanmadı.",
                           ev.get("interview_completed") or (last_answer if completed else None),
                           "flow_candidate_events" if ev.get("interview_completed") else "interview_answers" if completed else None))
        has_report = bool(interview.get("job_fit_analysis"))
        steps.append(_step("report", "AI mülakat raporu",
                           "done" if has_report else "failed" if assessment in ("failed", "insufficient_evidence") else "waiting" if completed else "missing",
                           "Rapor kaydı bulundu." if has_report else "Rapor değerlendirme durumu: " + str(assessment),
                           interview.get("report_at"), "interview_reports" if has_report else None))
    else:
        for code, title in (("interview_link", "Mülakat bağlantısı oluşturuldu"), ("interview_email", "Mülakat daveti e-postası"),
                            ("interview_link_opened", "Aday mülakat bağlantısını açtı"), ("interview_started", "Aday mülakatı başlattı"),
                            ("interview_completed", "Mülakat tamamlandı"), ("report", "AI mülakat raporu")):
            steps.append(_step(code, title, "missing", "Bu adım için kayıt yok."))
    for item in decisions:
        steps.append(_step("decision", "İK kararı: " + DECISION_LABELS[item["decision"]], "done",
                           item["reason"] or "Gerekçe girilmedi.", item["decided_at"], "flow_hr_decisions"))
    return steps


def _training_for(cursor, tenant, token_hashes):
    if not token_hashes:
        return []
    cursor.execute(f"""SELECT a.token_hash,a.program_id,a.modules_json,a.created_at,i.expires_at,i.revoked,
            p.state_json,p.updated_at AS progress_at,v.opened_at,v.started_at,v.last_activity_at
        FROM ernasa_training_assignments a
        JOIN ernasa_training_invites i ON i.token_hash=a.token_hash
        LEFT JOIN ernasa_training_v9_progress p ON p.token_hash=a.token_hash
        LEFT JOIN ernasa_training_v9_activity v ON v.token_hash=a.token_hash
        WHERE a.company_id=? AND a.token_hash IN ({_in_clause(token_hashes)})
        ORDER BY a.created_at""", (tenant, *token_hashes))
    result = []
    for row in cursor.fetchall():
        modules = json.loads(row["modules_json"])
        state = json.loads(row["state_json"]) if row["state_json"] else {}
        done = state.get("done", {})
        result.append({
            "program_id": row["program_id"],
            "modules": [{"id": m, "title": academy_assignments.MODULE_TITLES.get(m, m), "done": done.get(m) is True}
                        for m in modules],
            "completed_lessons": sum(done.get(m) is True for m in modules), "total_lessons": len(modules),
            "assigned_at": row["created_at"], "expires_at": row["expires_at"], "revoked": bool(row["revoked"]),
            "opened_at": row["opened_at"], "started_at": row["started_at"],
            "last_activity_at": row["last_activity_at"] or row["progress_at"],
            "notice": "İlerleme katılımcının kendi işaretlemesidir; doğrulanmış sınav başarısı değildir.",
        })
    return result


def detail(actor, app_id):
    """Orta ve sağ panel: birleşik aday dosyası (yalnızca okuma)."""
    tenant = actor.company_id
    with closing(get_connection()) as db:
        cursor = db.cursor()
        app = ats.application(cursor, actor, app_id)  # firma ve personel kapsamı burada uygulanır
        cursor.execute("""SELECT id,filename,media_type,size_bytes,created_at FROM ats_documents
            WHERE company_id=? AND application_id=? ORDER BY created_at""", (tenant, app_id))
        documents = [dict(row) for row in cursor.fetchall()]
        screenings, interviews, permissions, bookings, appointments, outbox, _ = _collect(cursor, actor, [app])
        screening = screenings.get(app_id)
        interview = interviews.get(app_id)
        permission, booking = permissions.get(app_id), bookings.get(app_id)
        appointment, availability = appointments.get(app_id), outbox.get(app_id)
        email, answers, assessment = None, [], None
        if interview:
            cursor.execute("""SELECT status,initiated_at,sent_at,provider_message_id FROM ik_invitation_email_sends
                WHERE token=? AND company_id=?""", (interview["token"], tenant))
            row = cursor.fetchone()
            email = dict(row) if row else None
            cursor.execute("""SELECT question_number,question,answer,created_at FROM interview_answers
                WHERE token=? ORDER BY question_number,id""", (interview["token"],))
            unique = {}
            for row in cursor.fetchall():
                unique.setdefault(row["question_number"], dict(row))
            answers = list(unique.values())
            cursor.execute("SELECT status FROM ik_interview_assessments WHERE token=? AND company_id=?",
                           (interview["token"], tenant))
            row = cursor.fetchone()
            assessment = row["status"] if row else None
        cursor.execute("""SELECT event,occurred_at FROM flow_candidate_events
            WHERE company_id=? AND application_id=? ORDER BY occurred_at""", (tenant, app_id))
        events = [dict(row) for row in cursor.fetchall()]
        cursor.execute("""SELECT id,decision,reason,decided_by,decided_at,schedule_id,training_token_hash
            FROM flow_hr_decisions WHERE company_id=? AND application_id=? ORDER BY decided_at""", (tenant, app_id))
        decisions = [dict(row) for row in cursor.fetchall()]
        cursor.execute("""SELECT previous,next,closure,user_id,occurred_at FROM ats_stage_events
            WHERE company_id=? AND application_id=? ORDER BY occurred_at""", (tenant, app_id))
        stage_events = [dict(row) for row in cursor.fetchall()]
        cursor.execute("""SELECT note,user_id,created_at FROM ats_notes
            WHERE company_id=? AND application_id=? ORDER BY created_at""", (tenant, app_id))
        notes = [dict(row) for row in cursor.fetchall()]
        trainings = _training_for(cursor, tenant, [d["training_token_hash"] for d in decisions if d["training_token_hash"]])
        cursor.execute("SELECT id,status FROM selin_cases WHERE company_id=? AND application_id=?", (tenant, app_id))
        case = cursor.fetchone()
        candidate_notices = _notices(cursor, tenant, app_id)
        cursor.execute("""SELECT pa.enabled FROM flow_position_automation pa WHERE pa.company_id=?
            AND pa.position_id=?""", (tenant, app["position_id"]))
        automation = cursor.fetchone()
        cursor.execute("""SELECT reason_code,created_at FROM flow_automation_reviews WHERE company_id=?
            AND application_id=? ORDER BY created_at""", (tenant, app_id))
        reviews = [dict(r) for r in cursor.fetchall()]
        cursor.execute("""SELECT kind,message,status,created_at FROM flow_candidate_requests WHERE company_id=?
            AND application_id=? ORDER BY created_at""", (tenant, app_id))
        requests = [dict(r) for r in cursor.fetchall()]
    schedules = ats_workflow.list_schedules(actor, app_id)
    latest = decisions[-1] if decisions else None
    state = derive_state(app, screening, permission, booking, appointment, availability, interview, events, latest)
    report_text = interview.get("job_fit_analysis") if interview else None
    sections = report_sections(report_text)
    ats_details = json.loads(screening["details_json"]) if screening and screening.get("details_json") else {}
    can_decide = actor.role in MANAGERS and state in DECIDABLE_STATES
    return {
        "application": {key: app.get(key) for key in ("id", "name", "email", "phone", "position", "position_id",
                                                       "stage", "closure", "revision", "created_at", "changed_at", "source")},
        "state": state, "state_label": STATE_LABELS[state],
        "closure_label": CLOSURE_LABELS.get(app["closure"]),
        "scores": {
            "ats": screening["match_score"] if screening else None,
            "ats_coverage": screening["coverage"] if screening else None,
            "interview": report_score(report_text),
            "note": "ATS puanı ve mülakat puanı ayrı kaynaklardan gelir ve birleştirilmez. "
                    "Puanlar karar destek verisidir; sistem hiçbir adayı puana göre reddetmez.",
        },
        "documents": documents,
        "ats": {"state": screening["analysis_state"] if screening else None,
                "analyzed_at": screening["analyzed_at"] if screening else None,
                "document_id": screening["document_id"] if screening else None,
                "match_score": ats_details.get("match_score"), "coverage": ats_details.get("evidence_coverage"),
                "requirements": ats_details.get("requirements", []), "reason": ats_details.get("reason"),
                "source": ats_details.get("source")} if screening else None,
        "report": {"text": report_text, "created_at": interview.get("report_at") if interview else None,
                   "strengths": sections.get("GÜÇLÜ YÖNLER", []),
                   "development": sections.get("GELİŞTİRİLMESİ GEREKEN ALANLAR", []),
                   "attention": sections.get("DİKKAT EDİLMESİ GEREKEN NOKTALAR", []),
                   "summary": sections.get("İK DEĞERLENDİRME ÖZETİ", []),
                   "assessment_status": assessment} if interview else None,
        "interview": {"status": interview["status"], "created_at": interview["created_at"],
                      "expires_at": interview["expires_at"], "reference": subject_ref(interview["token"])} if interview else None,
        "answers": answers,
        "timeline": _timeline(app, documents, screening, permission, booking, appointment, availability, interview,
                              email, events, answers, assessment, decisions),
        "schedules": schedules,
        "stage_events": stage_events, "notes": notes,
        "decisions": [{key: d[key] for key in ("id", "decision", "reason", "decided_by", "decided_at", "schedule_id")}
                      | {"label": DECISION_LABELS[d["decision"]]} for d in decisions],
        "last_decision_id": latest["id"] if latest else "",
        "trainings": trainings,
        "case_id": case["id"] if case else None,
        "can_decide": can_decide,
        "decision_help": None if can_decide else (
            "Karar yalnızca firma yöneticisi tarafından verilir." if actor.role not in MANAGERS else
            "Karar için tamamlanmış mülakat ve AI raporu gerekir (şu an: " + STATE_LABELS[state] + ")."),
        "candidate_notices": candidate_notices,
        "automation": bool(automation and automation["enabled"]),
        "reviews": reviews, "requests": requests,
        "notice": "Kararınız kaydedildiğinde adaya ilgili bilgilendirme e-postası kuyruğa alınır ve mevcut "
                  "e-posta altyapısıyla gönderilir. Ret gerekçesi adaya iletilmez; yalnızca dosyada saklanır. "
                  "Eğitim daveti mevcut Eğitim Atama gönderimiyle ayrıca gönderilir.",
    }


# --------------------------------------------------------------------- karar

class SecondInterviewPlan(BaseModel):
    interviewer_id: int = Field(gt=0)
    starts_at: str = Field(max_length=40)
    ends_at: str = Field(max_length=40)
    kind: Literal["video", "onsite", "phone"]
    notes: str = Field(default="", max_length=3000)


class TrainingChoice(BaseModel):
    program_id: str = Field(min_length=2, max_length=40)
    module_ids: list[str] = Field(min_length=1, max_length=53)
    days: int = Field(default=14, ge=1, le=30)


class Decision(BaseModel):
    decision: Literal["approve", "reject", "second_interview", "assign_training"]
    reason: str = Field(default="", max_length=2000)
    expected_revision: int = Field(ge=0)
    expected_last_decision_id: str = Field(default="", max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=100)
    schedule: SecondInterviewPlan | None = None
    training: TrainingChoice | None = None


def _replay(cursor, tenant, key):
    cursor.execute("SELECT * FROM flow_hr_decisions WHERE company_id=? AND idempotency_key=?", (tenant, key))
    row = cursor.fetchone()
    return dict(row) if row else None


def decide(actor, app_id, fields: Decision):
    """Yetkili İK yöneticisinin kararı. Tek işlemde, mevcut modüllerle uygulanır."""
    if actor.company_id is None or actor.role not in MANAGERS:
        raise HTTPException(403, "Nihai karar yalnızca firma yöneticisi tarafından verilebilir.")
    reason = fields.reason.strip()
    if fields.decision == "reject" and len(reason) < 3:
        raise HTTPException(422, "Ret kararı için gerekçe zorunludur.")
    if fields.decision == "assign_training" and fields.training is None:
        raise HTTPException(422, "Eğitim programı ve dersleri seçilmelidir.")
    tenant = actor.company_id
    training_link = None
    with write(tenant) as cursor:
        existing = _replay(cursor, tenant, fields.idempotency_key)
        if existing:
            if existing["application_id"] != app_id or existing["decision"] != fields.decision:
                raise HTTPException(409, "Bu işlem anahtarı başka bir karar için kullanılmış.")
            return {"decision": _public(existing), "replayed": True, "training_link": None}
        app = ats.application(cursor, actor, app_id)
        interview = _latest_interviews(cursor, tenant, [app_id]).get(app_id)
        latest = _latest_decisions(cursor, tenant, [app_id]).get(app_id)
        state = derive_state(app, None, None, None, None, None, interview, None, latest)
        if state not in DECIDABLE_STATES:
            raise HTTPException(409, "Karar için tamamlanmış mülakat ve AI raporu gerekir (şu an: "
                                + STATE_LABELS[state] + ").")
        if (latest["id"] if latest else "") != fields.expected_last_decision_id or app["revision"] != fields.expected_revision:
            raise HTTPException(409, "Aday dosyası bu sırada güncellendi. Ekranı yenileyip yeniden değerlendirin.")
        revision = app["revision"]
        schedule_id = training_hash = None
        if fields.decision == "approve":
            ats_workflow.move(actor, app_id, ats_workflow.Move(stage="offer", revision=revision), active_cursor=cursor)
        elif fields.decision == "reject":
            ats_workflow.move(actor, app_id, ats_workflow.Move(stage=app["stage"], revision=revision, closure="negative"),
                              active_cursor=cursor)
        elif fields.decision == "second_interview":
            if app["stage"] != "interview":
                ats_workflow.move(actor, app_id, ats_workflow.Move(stage="interview", revision=revision), active_cursor=cursor)
            if fields.schedule:
                plan = fields.schedule
                schedule_id = ats_workflow.save_schedule(actor, app_id, ats_workflow.Schedule(
                    interviewer_id=plan.interviewer_id, starts_at=plan.starts_at, ends_at=plan.ends_at,
                    kind=plan.kind, notes=("İkinci görüşme · İK Karar Merkezi. " + plan.notes).strip()),
                    active_cursor=cursor)["id"]
        elif fields.decision == "assign_training":
            if not app["email"]:
                raise HTTPException(422, "Eğitim ataması için adayın doğrulanmış e-postası gerekir.")
            choice = fields.training
            result = academy_assignments.record_assignment(cursor, tenant, actor.user_id, academy_assignments.AssignmentRequest(
                recipient=app["name"], email=app["email"], program_id=choice.program_id,
                module_ids=choice.module_ids, days=choice.days))
            training_hash, training_link = result["token_hash"], result["link"]
        if reason:
            cursor.execute("INSERT INTO ats_notes VALUES (?,?,?,?,?,?)",
                           (uuid4().hex, tenant, app_id, actor.user_id,
                            ("İK kararı (" + DECISION_LABELS[fields.decision] + "): " + reason)[:4000], now()))
        decision_id = uuid4().hex
        cursor.execute("""INSERT INTO flow_hr_decisions
            (id,company_id,application_id,previous_decision_id,decision,reason,decided_by,decided_at,
             idempotency_key,interview_token_ref,schedule_id,training_token_hash)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (decision_id, tenant, app_id, fields.expected_last_decision_id, fields.decision, reason,
             actor.user_id, now(), fields.idempotency_key,
             subject_ref(interview["token"]) if interview else None, schedule_id, training_hash))
        record(cursor, actor, "hr_decision_" + fields.decision, app_id)
        import hr_flow_automation
        queued = hr_flow_automation.stage_notice(cursor, tenant, app_id, DECISION_NOTICE[fields.decision],
                                                 decision_id, require_automation=False)
        cursor.execute("SELECT * FROM flow_hr_decisions WHERE id=?", (decision_id,))
        saved = dict(cursor.fetchone())
    return {"decision": _public(saved), "replayed": False, "training_link": training_link,
            "notice": "Karar kaydedildi. Aday bilgilendirme e-postası kuyruğa alındı; gönderim durumu "
                      "Bildirimler sekmesinde izlenir." if queued else "Karar kaydedildi."}


def _public(row):
    return {"id": row["id"], "decision": row["decision"], "label": DECISION_LABELS[row["decision"]],
            "reason": row["reason"], "decided_by": row["decided_by"], "decided_at": row["decided_at"],
            "schedule_id": row["schedule_id"], "training_assigned": bool(row["training_token_hash"])}
