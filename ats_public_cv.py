"""ERNASA HR: public CV submission through a published ATS job link.

Only a resolved, published tenant job is accepted. Documents and consent are
persisted in one tenant-locked transaction. No outbound mail or automated
hiring decision is made in the public request.
"""
import hashlib
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException

import ats
import hr_flow_contacts
from database import get_connection
from recruitment import write

SOURCES = frozenset({"direct","linkedin","indeed","kariyer_net","social","other"})
MAX_PER_VISITOR_HOUR = 8
MAX_PER_TENANT_HOUR = 250


def initialize_schema():
    with __import__("contextlib").closing(get_connection()) as db:
        c=db.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS ats_application_sources (
            application_id TEXT PRIMARY KEY REFERENCES ats_applications(id),
            company_id BIGINT NOT NULL, job_id TEXT NOT NULL,
            source TEXT NOT NULL, recorded_at TEXT NOT NULL
        )""")
        c.execute("""CREATE INDEX IF NOT EXISTS ats_application_sources_tenant
            ON ats_application_sources(company_id,source,recorded_at)""")
        c.execute("""CREATE TABLE IF NOT EXISTS ats_public_cv_attempts (
            id TEXT PRIMARY KEY, company_id BIGINT NOT NULL,
            visitor_hash TEXT NOT NULL, created_at TEXT NOT NULL
        )""")
        c.execute("""CREATE INDEX IF NOT EXISTS ats_public_cv_limit
            ON ats_public_cv_attempts(company_id,created_at,visitor_hash)""")
        c.execute("""CREATE TABLE IF NOT EXISTS selin_intake_queue (
            application_id TEXT PRIMARY KEY REFERENCES ats_applications(id),
            company_id BIGINT NOT NULL, job_id TEXT NOT NULL,
            document_id TEXT NOT NULL, source TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN
                ('pending_review','processing','assigned','needs_attention','skipped')),
            case_id TEXT, error_code TEXT, reviewed_by BIGINT, reviewed_at TEXT,
            received_at TEXT NOT NULL
        )""")
        c.execute("""CREATE INDEX IF NOT EXISTS selin_intake_queue_scope
            ON selin_intake_queue(company_id,status,received_at)""")
        db.commit()


def list_intake(company_id):
    """Trusted manager-only caller; joins source, position, candidate and job."""
    from contextlib import closing
    with closing(get_connection()) as db:
        c=db.cursor()
        c.execute("""SELECT q.application_id,q.document_id,q.job_id,q.source,q.status,
            q.case_id,q.error_code,q.received_at,q.reviewed_at,c.name,c.email,
            p.name AS position,j.title AS job_title
            FROM selin_intake_queue q
            JOIN ats_applications a ON a.id=q.application_id AND a.company_id=q.company_id
            JOIN ats_candidates c ON c.id=a.candidate_id AND c.company_id=q.company_id
            JOIN company_positions p ON p.id=a.position_id AND p.company_id=q.company_id
            JOIN ats_jobs j ON j.id=q.job_id AND j.company_id=q.company_id
            WHERE q.company_id=? ORDER BY q.received_at DESC LIMIT 200""",
            (company_id,))
        return [dict(row) for row in c.fetchall()]


def decide_intake(actor, application_id, action, root, create_interview, send_interview):
    """Manager action, no public account can trigger an interview or message."""
    import recruitment
    import selin_workflow
    if actor.company_id is None or actor.role not in recruitment.MANAGERS:
        raise HTTPException(403,"Firma İK yöneticisi yetkisi gerekiyor.")
    if not {"ats","ai"}.issubset(set(actor.products)):
        raise HTTPException(403,"ATS ve AI İK yetkileri gerekiyor.")
    if action not in ("start","skip"):
        raise HTTPException(422,"Geçersiz işlem.")
    with write(actor.company_id) as c:
        c.execute("""SELECT document_id,status FROM selin_intake_queue
            WHERE company_id=? AND application_id=?""",(actor.company_id,application_id))
        row=c.fetchone()
        if not row:
            raise HTTPException(404,"SELİN başvuru kuyruğunda kayıt bulunamadı.")
        if row["status"]!="pending_review":
            raise HTTPException(409,"Başvuru daha önce işleme alınmış; yeniden başlatılmaz.")
        if action=="start" and not hr_flow_contacts.may_create_interview_link(actor.company_id,application_id):
            # Never mint an invite before candidate appointment confirmation.
            raise HTTPException(409,"Adayın görüşme zamanı teyit edilmeden mülakat başlatılamaz.")
        next_status="processing" if action=="start" else "skipped"
        c.execute("""UPDATE selin_intake_queue
            SET status=?,reviewed_by=?,reviewed_at=?,error_code=NULL
            WHERE company_id=? AND application_id=? AND status='pending_review'""",
            (next_status,actor.user_id,datetime.now(timezone.utc).isoformat(),
             actor.company_id,application_id))
        if c.rowcount!=1:
            raise HTTPException(409,"Başvuru aynı anda işleme alınıyor.")
        document_id=row["document_id"]
    if action=="skip":
        # Skip means no automated workflow; ATS application is NOT rejected.
        return {"application_id":application_id,"status":"skipped",
                "message":"Başvuru ATS'de korundu; SELİN otomasyonu başlatılmadı."}
    try:
        case=selin_workflow.begin(actor,application_id,document_id,root,
                                  create_interview,send_interview)
        with write(actor.company_id) as c:
            c.execute("""UPDATE selin_intake_queue SET status='assigned',case_id=?
                WHERE company_id=? AND application_id=? AND status='processing'""",
                (case["id"],actor.company_id,application_id))
        return {"application_id":application_id,"status":"assigned",
                "case_id":case["id"],"case_status":case["status"],
                "message":"SELİN aday dosyası oluşturuldu. E-posta durumu aday dosyasından kontrol edilmelidir."}
    except Exception as error:
        # Unknown outbound-mail outcomes are NEVER retried automatically.
        with write(actor.company_id) as c:
            c.execute("""UPDATE selin_intake_queue
                SET status='needs_attention',error_code=?
                WHERE company_id=? AND application_id=? AND status='processing'""",
                (type(error).__name__[:80],actor.company_id,application_id))
        raise


def enabled():
    return os.getenv("ERNASA_PUBLIC_CV_ENABLED") == "1"


def _rate_limit(c, tenant, client_ip, now):
    secret=os.getenv("ERNASA_PUBLIC_CV_RATE_SECRET") or os.getenv("IK_SESSION_SECRET","")
    if len(secret)<32 or not client_ip:
        raise HTTPException(503,"Başvuru koruması yapılandırılmamış.")
    visitor=hashlib.sha256((secret+"|"+client_ip).encode()).hexdigest()
    one_hour=(now-timedelta(hours=1)).isoformat()
    c.execute("DELETE FROM ats_public_cv_attempts WHERE created_at<?",
              ((now-timedelta(days=1)).isoformat(),))
    c.execute("""SELECT COUNT(*) AS n FROM ats_public_cv_attempts
        WHERE company_id=? AND visitor_hash=? AND created_at>=?""",
        (tenant,visitor,one_hour))
    if c.fetchone()["n"]>=MAX_PER_VISITOR_HOUR:
        raise HTTPException(429,"Çok fazla başvuru denemesi. Daha sonra tekrar deneyin.")
    c.execute("""SELECT COUNT(*) AS n FROM ats_public_cv_attempts
        WHERE company_id=? AND created_at>=?""",(tenant,one_hour))
    if c.fetchone()["n"]>=MAX_PER_TENANT_HOUR:
        raise HTTPException(429,"Başvuru kapasitesi dolu. Daha sonra tekrar deneyin.")
    c.execute("INSERT INTO ats_public_cv_attempts VALUES(?,?,?,?)",
              (uuid4().hex,tenant,visitor,now.isoformat()))


def submit(tenant, job_id, fields, filename, content, source, client_ip):
    if not enabled():
        raise HTTPException(404,"CV başvurusu etkin değil.")
    if source not in SOURCES:
        raise HTTPException(422,"Geçersiz başvuru kaynağı.")
    # Must validate before opening a write transaction or storing candidate PII.
    name=ats.document_name(filename)
    media=ats.document_media(name,content)
    now=datetime.now(timezone.utc)
    with write(tenant) as c:
        _rate_limit(c,tenant,client_ip,now)
        job=ats.public_job(c,tenant,job_id)
        if not fields.acknowledge or fields.notice_version!=job["notice_version"]:
            raise HTTPException(400,"Güncel aday aydınlatma metnini okuyup onaylayın.")
        applicant=ats.record_application(
            c,tenant,job["position_id"],fields,None,
            "İlan / "+source,job_id=job_id
        )
        app_id=applicant["id"]
        doc_id=ats.uid()
        actor=SimpleNamespace(company_id=tenant,user_id=0)
        ats.record_document(c,actor,app_id,doc_id,name,media,content)
        c.execute("INSERT INTO ats_notices VALUES(?,?,?,?,?)",
                  (app_id,tenant,job["notice"],job["notice_version"],now.isoformat()))
        c.execute("INSERT INTO ats_application_sources VALUES(?,?,?,?,?)",
                  (app_id,tenant,job_id,source,now.isoformat()))
        # Same transaction as application, consent and immutable CV archive:
        # public requests NEVER initiate e-mail or create interview decisions.
        c.execute("""INSERT INTO selin_intake_queue
            (application_id,company_id,job_id,document_id,source,status,received_at)
            VALUES(?,?,?,?,?,'pending_review',?)""",
            (app_id,tenant,job_id,doc_id,source,now.isoformat()))
        # Prepare the availability-contact record but NEVER send an email in the public request.
        hr_flow_contacts.stage_availability_message(c,tenant,app_id)
    # Return IDs to trusted server caller only; HTTP route must not expose them.
    return {"application_id":app_id,"document_id":doc_id,"company_id":tenant,
            "source":source}
