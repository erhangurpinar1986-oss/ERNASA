"""Durable tenant-scoped CV upload jobs."""
import asyncio
from base64 import b64decode, b64encode
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import logging
from pathlib import Path

from fastapi import HTTPException

import ats
import control_integration as control
from database import get_connection
from recruitment import write


def enqueue(actor, application_id, filename, content):
    name = Path(filename).name[:200]
    ats.document_media(name, content)
    digest = sha256(content).hexdigest()
    stamp = ats.now()
    with write(actor.company_id) as cursor:
        ats.application(cursor,actor,application_id)
        scope = (actor.company_id,application_id,digest)
        cursor.execute("""SELECT 1 FROM ats_document_digests
            WHERE company_id=? AND application_id=? AND digest=?""",scope)
        if cursor.fetchone():
            raise HTTPException(409,"Bu dosya bu başvuruya zaten yüklenmiş.")
        cursor.execute("""SELECT 1 FROM ats_upload_jobs
            WHERE company_id=? AND application_id=? AND content_hash=? AND status='pending'""",scope)
        if cursor.fetchone():
            raise HTTPException(409,"Bu dosya zaten yükleme kuyruğunda.")
        cursor.execute("SELECT COUNT(*) AS n FROM ats_upload_jobs WHERE company_id=? AND status='pending'",
                       (actor.company_id,))
        if cursor.fetchone()["n"] >= 20:
            raise HTTPException(429,"Firma yükleme kuyruğu dolu. Bekleyen dosyalar işlendiğinde yeniden deneyin.")
        job_id = ats.uid()
        cursor.execute("""INSERT INTO ats_upload_jobs VALUES (?,?,?,?,?,?,?,?,?,'pending',NULL,?,?)""",
            (job_id,actor.company_id,application_id,actor.user_id,actor.portal.slug,actor.access_version,
             name,digest,b64encode(content).decode("ascii"),stamp,stamp))
    return {"id":job_id,"status":"pending"}


def jobs(actor, application_id):
    with closing(get_connection()) as db:
        cursor = db.cursor()
        ats.application(cursor,actor,application_id)
        cursor.execute("""SELECT id,filename,status,error FROM ats_upload_jobs
            WHERE company_id=? AND application_id=? AND user_id=?
            ORDER BY created_at DESC LIMIT 40""",(actor.company_id,application_id,actor.user_id))
        return [dict(row) for row in cursor.fetchall()]


def update(row, error, retry=False):
    with write(row["company_id"]) as cursor:
        if retry:
            next_attempt = (datetime.now(timezone.utc)+timedelta(seconds=8)).isoformat()
            cursor.execute("""UPDATE ats_upload_jobs SET available_at=?,error=?
                WHERE id=? AND company_id=? AND status='pending'""",
                (next_attempt,error,row["id"],row["company_id"]))
        else:
            cursor.execute("""UPDATE ats_upload_jobs SET status='failed',payload=NULL,error=?
                WHERE id=? AND company_id=? AND status='pending'""",(error,row["id"],row["company_id"]))


def process_one(root):
    with closing(get_connection()) as db:
        cursor = db.cursor()
        cursor.execute("""SELECT * FROM ats_upload_jobs WHERE status='pending' AND available_at<=?
            ORDER BY available_at,created_at,id LIMIT 1""",(ats.now(),))
        row = cursor.fetchone()
    if row is None:
        return False
    portal = control.Portal(row["slug"],"")
    try:
        identity = control.control_call(portal,"session",id=str(row["user_id"]))
        if (identity.get("company_id") != row["company_id"] or identity.get("version") != row["access_version"]
                or identity.get("role") not in control.STAFF_ROLES or identity.get("must_change_password")
                or "ats" not in identity.get("products",())):
            raise HTTPException(403,"Yükleme yetkisi değişti. Yeniden giriş yapıp tekrar deneyin.")
        actor = control.Actor(company_id=row["company_id"],role=identity["role"],portal=portal,user_id=row["user_id"])
        ats.upload_document(root,actor,row["application_id"],row["filename"],b64decode(row["payload"]),job_id=row["id"])
    except HTTPException as error:
        update(row,error.detail,retry=error.status_code>=500)
    except OSError:
        update(row,"Dosya deposuna geçici olarak erişilemiyor.",retry=True)
    return True


async def worker(root, stop):
    while not stop.is_set():
        try:
            if await asyncio.to_thread(process_one,root):
                continue
        except Exception:
            logging.getLogger(__name__).exception("ATS CV job processing failed")
        try:
            await asyncio.wait_for(stop.wait(),timeout=0.5)
        except TimeoutError:
            pass
