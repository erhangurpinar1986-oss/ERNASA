"""ERNASA HR FLOW: communication channel policy and auditable preparation.

Recruitment messages are prepared, not claimed as delivered.  LinkedIn requires
a specifically authorized provider; WhatsApp is reserved for a later, consented
Business Platform integration.  No browser automation or scraped inboxes.
"""
import re
import json
import hashlib
from datetime import timedelta

from fastapi import HTTPException
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


def may_create_interview_link(company_id, application_id, document_id=None):
    """Single non-mutating gate for ALL SELIN interview-creation paths.

    A score is evidence for human review, never permission to interview.
    An actual manager approval, unchanged CV/job criteria and an explicit
    candidate-confirmed future appointment are each required.
    """
    if not company_id or not application_id or not document_id:
        return False
    with closing(get_connection()) as db:
        c=db.cursor()
        c.execute("""SELECT p.status,p.document_id,p.decided_by,p.decided_at,
                   s.analysis_state,s.match_score,s.criteria_digest,s.cv_digest,
                   a.position_id,a.closure,pr.fields,dg.digest,
                   ap.confirmed_at,ap.confirmed_on,ap.confirmed_by
            FROM flow_interview_permissions p
            JOIN ats_applications a ON a.id=p.application_id
                AND a.company_id=p.company_id
            JOIN flow_screening_results s ON s.application_id=p.application_id
                AND s.company_id=p.company_id AND s.document_id=p.document_id
                AND s.position_id=a.position_id
            JOIN ats_document_digests dg ON dg.company_id=p.company_id
                AND dg.application_id=p.application_id
                AND dg.document_id=p.document_id
            LEFT JOIN ats_position_profiles pr ON pr.position_id=a.position_id
                AND pr.company_id=a.company_id
            LEFT JOIN flow_candidate_appointment ap ON
                ap.application_id=p.application_id AND ap.company_id=p.company_id
            WHERE p.company_id=? AND p.application_id=? AND p.document_id=?""",
            (company_id,application_id,document_id))
        row=c.fetchone()
    if not row or row["status"]!="approved" or not row["decided_by"] or not row["decided_at"]:
        return False
    if row["closure"] or row["analysis_state"]!="analyzed" or row["match_score"] is None:
        return False
    if row["digest"]!=row["cv_digest"]:
        return False
    try:
        criteria=json.loads(row["fields"] or "{}")
        current=str(criteria.get("matching_criteria") or "")
        if not current or hashlib.sha256(current.encode("utf-8")).hexdigest()!=row["criteria_digest"]:
            return False
        if row["confirmed_by"]!="candidate" or not row["confirmed_on"]:
            return False
        from datetime import datetime,timezone
        appointment=datetime.fromisoformat(row["confirmed_at"])
        if appointment.tzinfo is None:
            return False
        remaining=appointment.astimezone(timezone.utc)-datetime.now(timezone.utc)
        return timedelta(0)<remaining<=timedelta(days=90)
    except (ValueError,TypeError,AttributeError,KeyError):
        return False


def require_interview_ready(company_id,application_id,document_id):
    """Fail closed before an interview token is minted or email attempted."""
    if not may_create_interview_link(company_id,application_id,document_id):
        raise HTTPException(
            409,"Mülakat için güncel ATS analizi, İK yönetici onayı ve "
                "adayın teyit ettiği ileri tarihli görüşme saati gereklidir.")
