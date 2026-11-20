"""Candidate-confirmed scheduling for newly approved FLOW applications."""
import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import logging
import secrets
from zoneinfo import ZoneInfo

from fastapi import HTTPException

import ats
import ats_workflow
import control_integration as control
import hr_flow_contacts
import recruitment
from database import get_connection
from services import invite_email

LOCAL_ZONE = ZoneInfo("Europe/Istanbul")


def enqueue(cursor, actor, permission):
    if not actor.portal or "ai" not in actor.products:
        return
    app_id = permission["application_id"]
    app = ats.application(cursor,actor,app_id)
    if app["identity_conflict_email"] or not app["email"]:
        raise HTTPException(409,"Uygunluk e-postasından önce adayın e-posta adresini doğrulayın.")
    hr_flow_contacts.stage_availability_message(cursor, actor.company_id, app_id)
    cursor.execute("""INSERT INTO flow_booking_jobs
        (application_id,company_id,document_id,manager_id,access_version,slug,origin,status,updated_at)
        VALUES(?,?,?,?,?,?,?,'pending_email',?) ON CONFLICT(application_id) DO NOTHING""",
        (app_id, actor.company_id, permission["document_id"], actor.user_id,
         actor.access_version, actor.portal.slug, actor.portal.origin, ats.now()))


def _load_token(cursor, token, slug):
    cursor.execute("""SELECT * FROM flow_booking_jobs
        WHERE token_hash=? AND slug=? AND expires_at>?""",
        (sha256(token.encode()).hexdigest(), slug, ats.now()))
    row = cursor.fetchone()
    if not row or row["status"] not in ("waiting", "booked", "dispatching", "finished"):
        raise HTTPException(404, "Uygunluk bağlantısı geçersiz veya süresi dolmuş.")
    return dict(row)


def booking(token, slug):
    with closing(get_connection()) as db:
        return _load_token(db.cursor(), token, slug)


def _actor(job):
    portal = control.Portal(job["slug"], job["origin"])
    identity = control.control_call(portal, "session", id=str(job["manager_id"]))
    if (identity.get("company_id") != job["company_id"]
            or identity.get("version") != job["access_version"]
            or identity.get("role") not in recruitment.MANAGERS
            or identity.get("must_change_password")
            or not {"ats", "ai"}.issubset(identity.get("products", ()))):
        raise HTTPException(403, "Onaylayan yöneticinin yetkisi değişmiş.")
    return control.Actor(job["company_id"], identity["role"], portal, job["manager_id"],
                         job["access_version"], identity.get("company_name"),
                         products=tuple(identity["products"]),
                         interview_quota=identity.get("interview_quota"),
                         month_bounds=tuple(identity.get("month_bounds", ())))


def confirm(token, slug, local_start):
    job = booking(token, slug)
    if job["status"] != "waiting":
        return
    try:
        start = datetime.strptime(local_start, "%Y-%m-%dT%H:%M").replace(tzinfo=LOCAL_ZONE)
    except ValueError:
        raise HTTPException(422, "Geçerli bir gün ve saat seçin.") from None
    remaining = start.astimezone(timezone.utc) - datetime.now(timezone.utc)
    if not timedelta(minutes=15) <= remaining <= timedelta(days=30):
        raise HTTPException(422, "En az 15 dakika sonrası ve önümüzdeki 30 gün içinde bir saat seçin.")
    actor = _actor(job)
    tenant, app_id = job["company_id"], job["application_id"]
    with recruitment.write(tenant) as cursor:
        job = _load_token(cursor, token, slug)
        if job["status"] != "waiting":
            return
        hr_flow_contacts.require_fresh_approval(cursor, tenant, app_id, job["document_id"])
        fields = ats_workflow.Schedule(
            interviewer_id=job["manager_id"], starts_at=start.isoformat(),
            ends_at=(start + timedelta(minutes=30)).isoformat(), kind="video",
            notes="Aday tarafından ERNASA HR FLOW üzerinden seçildi.")
        schedule = ats_workflow.save_schedule(actor, app_id, fields, active_cursor=cursor)
        confirmed = start.astimezone(timezone.utc).isoformat()
        stamp = ats.now()
        cursor.execute("""INSERT INTO flow_candidate_appointment
            (application_id,company_id,proposed_at,confirmed_at,confirmed_by,confirmed_on,updated_at)
            VALUES(?,?,?,?,'candidate',?,?) ON CONFLICT(application_id) DO UPDATE SET
            proposed_at=excluded.proposed_at,confirmed_at=excluded.confirmed_at,
            confirmed_by=excluded.confirmed_by,confirmed_on=excluded.confirmed_on,
            updated_at=excluded.updated_at""", (app_id,tenant,confirmed,confirmed,stamp,stamp))
        cursor.execute("""UPDATE flow_booking_jobs SET status='booked',schedule_id=?,updated_at=?
            WHERE company_id=? AND application_id=?""", (schedule["id"],stamp,tenant,app_id))


def process_one(create_case, send_availability=invite_email.send_availability):
    with closing(get_connection()) as db:
        cursor = db.cursor()
        cursor.execute("""SELECT * FROM flow_booking_jobs WHERE status IN ('pending_email','booked')
            ORDER BY updated_at LIMIT 1""")
        job = cursor.fetchone()
    if not job:
        return False
    tenant, app_id = job["company_id"], job["application_id"]
    sending = job["status"] == "pending_email"
    claim = "sending" if sending else "dispatching"
    with recruitment.write(tenant) as cursor:
        cursor.execute("""UPDATE flow_booking_jobs SET status=?,updated_at=?
            WHERE company_id=? AND application_id=? AND status=?""",
            (claim,ats.now(),tenant,app_id,job["status"]))
        if cursor.rowcount != 1:
            return False
    try:
        actor = _actor(job)
        if sending:
            token = secrets.token_urlsafe(32)
            expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
            with recruitment.write(tenant) as cursor:
                hr_flow_contacts.require_fresh_approval(cursor, tenant, app_id, job["document_id"])
                app = ats.application(cursor, actor, app_id)
                invite_email.validate_recipient(app["email"])
                cursor.execute("""UPDATE flow_booking_jobs SET token_hash=?,expires_at=?
                    WHERE company_id=? AND application_id=?""",
                    (sha256(token.encode()).hexdigest(), expires, tenant,app_id))
            provider_id = send_availability(app, actor.portal.origin + "/mulakat-uygunluk/" + token,
                                            idempotency_key="ernasa-availability-" + app_id)
            with recruitment.write(tenant) as cursor:
                cursor.execute("""UPDATE flow_contact_outbox SET status='provider_accepted',
                    provider_message_id=?,updated_at=? WHERE company_id=? AND application_id=?
                    AND purpose='availability_request'""", (provider_id,ats.now(),tenant,app_id))
                cursor.execute("""UPDATE flow_booking_jobs SET status='waiting',updated_at=?
                    WHERE company_id=? AND application_id=?""", (ats.now(),tenant,app_id))
        else:
            case = create_case(actor, app_id, job["document_id"])
            if case["status"] != "interview_invited":
                raise RuntimeError("FLOW interview invitation requires review")
            with recruitment.write(tenant) as cursor:
                cursor.execute("""UPDATE flow_booking_jobs SET status='finished',updated_at=?
                    WHERE company_id=? AND application_id=?""", (ats.now(),tenant,app_id))
                cursor.execute("""UPDATE selin_intake_queue SET status='assigned',case_id=?
                    WHERE company_id=? AND application_id=? AND status='pending_review'""",
                    (case["id"],tenant,app_id))
    except Exception as error:
        error_code = type(error).__name__
        logging.getLogger(__name__).warning("FLOW booking requires review (%s)",error_code)
        with recruitment.write(tenant) as cursor:
            cursor.execute("""UPDATE flow_booking_jobs SET status='failed',error_code=?,updated_at=?
                WHERE company_id=? AND application_id=?""", (error_code,ats.now(),tenant,app_id))
            if sending:
                cursor.execute("""UPDATE flow_contact_outbox SET status='failed',updated_at=?
                    WHERE company_id=? AND application_id=? AND purpose='availability_request'""",
                    (ats.now(),tenant,app_id))
    return True


async def worker(create_case, stop):
    while not stop.is_set():
        try:
            if await asyncio.to_thread(process_one, create_case):
                continue
        except Exception as error:
            logging.getLogger(__name__).warning("FLOW booking worker unavailable (%s)",type(error).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except TimeoutError:
            pass
