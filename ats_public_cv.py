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
        db.commit()


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
    # Return IDs to trusted server caller only; HTTP route must not expose them.
    return {"application_id":app_id,"document_id":doc_id,"company_id":tenant,
            "source":source}
