"""ERNASA HR FLOW: communication channel policy and auditable preparation.

Recruitment messages are prepared, not claimed as delivered.  LinkedIn requires
a specifically authorized provider; WhatsApp is reserved for a later, consented
Business Platform integration.  No browser automation or scraped inboxes.
"""
import re
from contextlib import closing
from datetime import datetime, timezone
from uuid import uuid4

from database import get_connection

_CHANNELS=("email","linkedin","whatsapp")
_PURPOSES=("availability_request","interview_invitation","followup","training_invitation")
_STATES=("prepared","connector_required","waiting_confirmation","provider_accepted",
         "delivered","failed","cancelled")


def now():
    return datetime.now(timezone.utc).isoformat()


def initialize_schema():
    with closing(get_connection()) as db:
        cur=db.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS flow_candidate_contact_preferences (
            application_id TEXT PRIMARY KEY REFERENCES ats_applications(id),
            company_id BIGINT NOT NULL,
            linkedin_profile TEXT,
            linkedin_authorized INTEGER NOT NULL DEFAULT 0,
            whatsapp_number TEXT,
            whatsapp_opt_in_at TEXT,
            updated_at TEXT NOT NULL
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS flow_contact_outbox (
            id TEXT PRIMARY KEY,
            company_id BIGINT NOT NULL,
            application_id TEXT NOT NULL REFERENCES ats_applications(id),
            purpose TEXT NOT NULL CHECK(purpose IN
                ('availability_request','interview_invitation','followup','training_invitation')),
            channel TEXT NOT NULL CHECK(channel IN ('email','linkedin','whatsapp')),
            status TEXT NOT NULL CHECK(status IN
                ('prepared','connector_required','waiting_confirmation','provider_accepted',
                 'delivered','failed','cancelled')),
            provider_message_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(company_id,application_id,purpose)
        )""")
        cur.execute("""CREATE INDEX IF NOT EXISTS flow_outbox_company_status
            ON flow_contact_outbox(company_id,status,created_at)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS flow_candidate_appointment (
            application_id TEXT PRIMARY KEY REFERENCES ats_applications(id),
            company_id BIGINT NOT NULL,
            proposed_at TEXT,
            confirmed_at TEXT,
            confirmed_by TEXT,
            confirmed_on TEXT,
            updated_at TEXT NOT NULL
        )""")
        db.commit()


def stage_availability_message(cur, company_id, application_id):
    """Called once *inside* the ATS application transaction.

    A prepared record does not imply a real message was sent.
    LinkedIn cannot be selected based on having a public profile alone.
    """
    cur.execute("""SELECT c.email FROM ats_applications a
        JOIN ats_candidates c ON c.id=a.candidate_id AND c.company_id=a.company_id
        WHERE a.id=? AND a.company_id=?""",(application_id,company_id))
    row=cur.fetchone()
    if not row:
        raise ValueError("ATS application not found in company")
    cur.execute("""INSERT INTO flow_candidate_contact_preferences
        (application_id,company_id,updated_at) VALUES(?,?,?)
        ON CONFLICT(application_id) DO NOTHING""",
        (application_id,company_id,now()))
    if row["email"] and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",row["email"]):
        # Only draft; CV precheck and contact authorization are separate steps.
        cur.execute("""INSERT INTO flow_contact_outbox
            (id,company_id,application_id,purpose,channel,status,
             provider_message_id,created_at,updated_at)
            VALUES(?,?,?,'availability_request','email','prepared',NULL,?,?)
            ON CONFLICT(company_id,application_id,purpose) DO NOTHING""",
            (uuid4().hex,company_id,application_id,now(),now()))


def contact_status(company_id, application_id):
    with closing(get_connection()) as db:
        cur=db.cursor()
        cur.execute("""SELECT purpose,channel,status,provider_message_id,created_at,updated_at
            FROM flow_contact_outbox WHERE company_id=? AND application_id=?
            ORDER BY created_at""",(company_id,application_id))
        messages=[dict(x) for x in cur.fetchall()]
        cur.execute("""SELECT proposed_at,confirmed_at,confirmed_by,confirmed_on
            FROM flow_candidate_appointment WHERE company_id=? AND application_id=?""",
            (company_id,application_id))
        booking=cur.fetchone()
    return {
        "messages":messages,
        "appointment":dict(booking) if booking else None,
        "linkedin":{"status":"integration_required",
                    "note":"Resmî yetkilendirilmiş mesajlaşma entegrasyonu olmadan otomatik DM gönderilmez."},
        "whatsapp":{"status":"future_opt_in",
                    "note":"Business Platform bağlantısı ve uygun iletişim izni olmadan mesaj gönderilmez."},
        "notice":"Hazırlandı, sağlayıcı kabul etti ve teslim edildi ayrı durumlardır."
    }


def may_create_interview_link(company_id, application_id):
    """Guard for the future scheduler-integrated invite stage."""
    with closing(get_connection()) as db:
        row=db.cursor().execute("""SELECT confirmed_at,confirmed_on
            FROM flow_candidate_appointment
            WHERE company_id=? AND application_id=?""",
            (company_id,application_id)).fetchone()
    return bool(row and row["confirmed_at"] and row["confirmed_on"])
