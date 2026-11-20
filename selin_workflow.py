"""SELİN: tenant-scoped, opt-in recruitment-to-training case automation.

A started case is permission to automate invitations and assigned training, NOT
a hiring decision. All writes are additive to existing ERNASA ATS and Academy.
No scheduler or outbound action runs unless separately enabled at deployment.
"""
import base64
import hashlib
import json
import logging
import os
import secrets
from contextlib import closing
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from reportlab.platypus import Spacer

import academy_assignments
import ats
import ats_matching
import recruitment
import recruitment_pdf
from database import get_connection

LOG = logging.getLogger(__name__)
STATES = ("created", "interview_created", "invitation_sending", "interview_invited",
          "processing_report", "training_sending", "training_invited",
          "training_in_progress", "evaluation_ready", "needs_attention")
DEFAULT_MODULES = ("ernasa", "hakkaniyet", "analiz", "demo", "olcme")
RECOMMENDATION_MAP = {
    "avcilik": "avcilik", "crm": "crm", "müzakere": "muzakere",
    "kurumsal": "kurumsal", "almanya": "uluslararasi",
    "metrik": "metrik", "demo": "demo", "kesif": "kesif",
}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def initialize_schema():
    """Additive only; neither drops nor rewrites existing production tables."""
    with closing(get_connection()) as db:
        c = db.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS selin_cases (
            id TEXT PRIMARY KEY, company_id BIGINT NOT NULL,
            application_id TEXT NOT NULL, document_id TEXT NOT NULL,
            authorized_by BIGINT NOT NULL, training_days INTEGER NOT NULL,
            interview_token TEXT, training_token_hash TEXT,
            status TEXT NOT NULL, cv_summary TEXT NOT NULL,
            error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(company_id,application_id)
        )""")
        c.execute("""CREATE INDEX IF NOT EXISTS selin_cases_tenant_status
            ON selin_cases(company_id,status)""")
        c.execute("""CREATE TABLE IF NOT EXISTS selin_report_archives (
            case_id TEXT PRIMARY KEY REFERENCES selin_cases(id) ON DELETE CASCADE,
            company_id BIGINT NOT NULL, interview_token TEXT NOT NULL,
            digest TEXT NOT NULL, pdf_base64 TEXT NOT NULL, archived_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS selin_automation_settings (
            company_id BIGINT PRIMARY KEY,
            enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
            configured_by BIGINT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS selin_events (
            id TEXT PRIMARY KEY, company_id BIGINT NOT NULL,
            case_id TEXT NOT NULL REFERENCES selin_cases(id) ON DELETE CASCADE,
            event TEXT NOT NULL, created_at TEXT NOT NULL
        )""")
        db.commit()


def auto_enabled(actor):
    """Opt-in gate: environment plus per-company manager authorization."""
    if not actor.company_id or actor.role not in recruitment.MANAGERS:
        return False
    if not {"ats", "ai"}.issubset(set(actor.products)):
        return False
    if os.getenv("ERNASA_SELIN_AUTOSTART_ENABLED") != "1":
        return False
    if os.getenv("ERNASA_SELIN_SEND_ENABLED") != "1":
        return False
    with closing(get_connection()) as db:
        row=db.cursor().execute(
            "SELECT enabled FROM selin_automation_settings WHERE company_id=?",
            (actor.company_id,)).fetchone()
    return bool(row and row["enabled"])


def automation_settings(actor):
    if actor.company_id is None or actor.role not in recruitment.MANAGERS:
        raise HTTPException(403, "İK yöneticisi yetkisi gerekli.")
    with closing(get_connection()) as db:
        row=db.cursor().execute(
            "SELECT enabled,configured_by,updated_at FROM selin_automation_settings WHERE company_id=?",
            (actor.company_id,)).fetchone()
    return {"enabled":bool(row and row["enabled"]),
            "ready":os.getenv("ERNASA_SELIN_AUTOSTART_ENABLED")=="1"
                    and os.getenv("ERNASA_SELIN_SEND_ENABLED")=="1"
                    and os.getenv("ERNASA_SELIN_WORKER_ENABLED")=="1",
            "last_updated":row["updated_at"] if row else None}


def configure_automation(actor, enabled):
    if actor.company_id is None or actor.role not in recruitment.MANAGERS:
        raise HTTPException(403, "İK yöneticisi yetkisi gerekli.")
    if not {"ats","ai"}.issubset(set(actor.products)):
        raise HTTPException(403, "ATS ve AI İK gerekir.")
    if type(enabled) is not bool:
        raise HTTPException(422, "Açık etkinleştirme kararı gerekli.")
    with closing(get_connection()) as db:
        c=db.cursor()
        c.execute("""INSERT INTO selin_automation_settings
            (company_id,enabled,configured_by,updated_at) VALUES(?,?,?,?)
            ON CONFLICT(company_id) DO UPDATE SET enabled=excluded.enabled,
                configured_by=excluded.configured_by,updated_at=excluded.updated_at""",
            (actor.company_id,int(enabled),actor.user_id,stamp()))
        db.commit()
    return automation_settings(actor)


def auto_start(actor, app_id, document_id, cv_root, create_interview, send_interview):
    """Best-effort hook called ONLY after a successful, committed CV upload.

    CV upload is never rolled back because an unrelated automation step fails.
    A failed case is visible in the SELİN 'needs_attention' queue.
    """
    if not auto_enabled(actor):
        return None
    try:
        return begin(actor,app_id,document_id,cv_root,create_interview,send_interview)
    except Exception as error:
        LOG.warning("SELİN auto-start paused: %s",type(error).__name__)
        return None


def _event(c, case_id, tenant, action):
    c.execute("INSERT INTO selin_events VALUES(?,?,?,?,?)",
              (uuid4().hex, tenant, case_id, action, stamp()))


def _case(c, tenant, case_id):
    c.execute("SELECT * FROM selin_cases WHERE company_id=? AND id=?",(tenant,case_id))
    result=c.fetchone()
    if result is None:
        raise HTTPException(404, "SELİN aday dosyası bulunamadı.")
    return dict(result)


def get_case(tenant, case_id):
    with closing(get_connection()) as db:
        return _case(db.cursor(), tenant, case_id)


def list_cases(tenant):
    with closing(get_connection()) as db:
        c=db.cursor()
        c.execute("""SELECT id,application_id,document_id,status,created_at,updated_at,
            error_code,interview_token,training_token_hash
            FROM selin_cases WHERE company_id=? ORDER BY created_at DESC LIMIT 100""",(tenant,))
        return [dict(row) for row in c.fetchall()]


def _transition(case_id, tenant, from_state, to_state, *, interview=None, training_hash=None, error=None):
    if to_state not in STATES:
        raise ValueError("Unknown state")
    with closing(get_connection()) as db:
        c=db.cursor()
        c.execute("""UPDATE selin_cases SET status=?, interview_token=COALESCE(?,interview_token),
            training_token_hash=COALESCE(?,training_token_hash),error_code=?,updated_at=?
            WHERE id=? AND company_id=? AND status=?""",
            (to_state,interview,training_hash,error,stamp(),case_id,tenant,from_state))
        changed=c.rowcount==1
        if changed:
            _event(c,case_id,tenant,to_state)
        db.commit()
    return changed


def begin(actor, application_id, document_id, cv_root, create_interview, send_interview):
    """Authenticated HR manager starts ONE case; no automated CV rejection."""
    if actor.company_id is None or actor.role not in recruitment.MANAGERS:
        raise HTTPException(403,"İK yönetici yetkisi gerekli.")
    if not {"ats","ai"}.issubset(set(actor.products)):
        raise HTTPException(403,"ATS ve AI İK yetkisi gerekli.")
    with closing(get_connection()) as db:
        c=db.cursor()
        app=ats.application(c,actor,application_id)
        if app["closure"]:
            raise HTTPException(409,"Kapatılmış başvuru için işlem başlatılamaz.")
        c.execute("SELECT id FROM selin_cases WHERE company_id=? AND application_id=?",
                  (actor.company_id,application_id))
        existing=c.fetchone()
        if existing:
            return get_case(actor.company_id,existing["id"])
    # Tenant-scoped source lookup: never infer a qualification or penalize missing CV evidence.
    match=ats_matching.evidence_match(cv_root,actor,application_id,document_id)
    cv_text=ats_matching.source_text(cv_root,actor,application_id,document_id)
    if not cv_text.strip():
        raise HTTPException(422,"CV okunamadı; manuel doğrulama gerekiyor.")
    summary={"match_score":match.get("match_score"),
             "evidence_coverage":match.get("evidence_coverage"),
             "human_review_required":True,
             "notes":match.get("reason",""),
             "source":match.get("source","")}
    case_id=uuid4().hex
    with closing(get_connection()) as db:
        c=db.cursor()
        try:
            c.execute("""INSERT INTO selin_cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (case_id,actor.company_id,application_id,document_id,actor.user_id,14,
                       None,None,"created",json.dumps(summary,ensure_ascii=False),
                       None,stamp(),stamp()))
            _event(c,case_id,actor.company_id,"created")
            db.commit()
        except Exception:
            db.rollback()
            raise
    # Existing ERNASA interview engine supplies questions, link TTL and audit.
    try:
        result=create_interview({"cv_text":cv_text},actor,application_id,
                                cv_document_id=document_id)
        token=result["interview_id"]
        if not _transition(case_id,actor.company_id,"created","interview_created",interview=token):
            raise RuntimeError("SELİN interview state changed unexpectedly")
        if os.getenv("ERNASA_SELIN_SEND_ENABLED") != "1":
            return get_case(actor.company_id,case_id)
        if not _transition(case_id,actor.company_id,"interview_created","invitation_sending"):
            raise RuntimeError("SELİN invitation claim failed")
        send_interview(token,actor)
        _transition(case_id,actor.company_id,"invitation_sending","interview_invited")
    except Exception as error:
        LOG.warning("SELİN invitation pipeline paused: %s",type(error).__name__)
        current=get_case(actor.company_id,case_id)
        if current["status"] in ("created","interview_created","invitation_sending"):
            _transition(case_id,actor.company_id,current["status"],"needs_attention",
                        error=type(error).__name__[:100])
        # Do not auto-retry an uncertain provider result or create a duplicate invitation.
        raise
    return get_case(actor.company_id,case_id)


def _archive_pdf(case):
    """Create a separate, immutable snapshot; existing PDF endpoint stays untouched."""
    tenant,token=case["company_id"],case["interview_token"]
    with closing(get_connection()) as db:
        c=db.cursor()
        c.execute("""SELECT name,company,position,status FROM interview_links
            WHERE company_id=? AND token=?""",(tenant,token))
        applicant=c.fetchone()
        c.execute("""SELECT job_fit_analysis FROM interview_reports WHERE token=?""",(token,))
        report=c.fetchone()
        if not applicant or applicant["status"]!="completed" or not report or not report["job_fit_analysis"]:
            raise ValueError("Completed interview assessment not available")
        c.execute("""SELECT question_number,question,answer FROM interview_answers
            WHERE token=? ORDER BY question_number,id""",(token,))
        answers={}
        for row in c.fetchall():
            answers.setdefault(row["question_number"],dict(row))
    logo=Path(__file__).resolve().parent/"frontend"/"assets"/"ernasalogo.png"
    buffer,doc,styles,story=recruitment_pdf.layout("Aday Mülakat Raporu",logo,None)
    for key,label in (("name","Aday"),("company","Firma"),("position","Pozisyon")):
        story.append(recruitment_pdf.paragraph(label+": "+str(applicant[key] or "—"),styles["BodyText"]))
    story.append(recruitment_pdf.paragraph("AI değerlendirme — nihai karar insanındır",styles["Heading2"]))
    for line in report["job_fit_analysis"].splitlines():
        if line.strip():
            story.append(recruitment_pdf.paragraph(line[:3000],styles["BodyText"]))
    story.append(recruitment_pdf.paragraph("Mülakat soruları ve yanıtları",styles["Heading2"]))
    for n,item in answers.items():
        story.append(recruitment_pdf.paragraph("Soru "+str(n)+": "+item["question"],styles["BodyText"]))
        story.append(recruitment_pdf.paragraph("Yanıt: "+item["answer"],styles["BodyText"]))
        story.append(Spacer(1,4))
    doc.build(story)
    raw=buffer.getvalue()
    if not raw.startswith(b"%PDF-") or len(raw)>4*1024*1024:
        raise ValueError("Generated PDF invalid or too large")
    digest=hashlib.sha256(raw).hexdigest()
    with closing(get_connection()) as db:
        c=db.cursor()
        c.execute("""INSERT INTO selin_report_archives VALUES(?,?,?,?,?,?)
            ON CONFLICT(case_id) DO NOTHING""",
            (case["id"],tenant,token,digest,base64.b64encode(raw).decode("ascii"),stamp()))
        _event(c,case["id"],tenant,"report_archived")
        db.commit()
    return report["job_fit_analysis"]


def archived_pdf(tenant,case_id):
    with closing(get_connection()) as db:
        c=db.cursor()
        _case(c,tenant,case_id)
        c.execute("""SELECT pdf_base64,digest FROM selin_report_archives
            WHERE company_id=? AND case_id=?""",(tenant,case_id))
        row=c.fetchone()
    if not row:
        raise HTTPException(404,"PDF henüz arşivlenmedi.")
    content=base64.b64decode(row["pdf_base64"])
    if hashlib.sha256(content).hexdigest()!=row["digest"]:
        raise HTTPException(500,"Arşiv doğrulaması başarısız.")
    return content


def _training(case,analysis,send_training):
    """Assign V9 without touching previous training, then send once if enabled."""
    tenant,case_id=case["company_id"],case["id"]
    with closing(get_connection()) as db:
        c=db.cursor()
        c.execute("SELECT name,email FROM interview_links WHERE token=? AND company_id=?",
                  (case["interview_token"],tenant))
        person=c.fetchone()
    if not person or not person["email"]:
        raise ValueError("Candidate email unavailable")
    suggested=academy_assignments.suggest_from_text(analysis)
    modules=set(DEFAULT_MODULES)
    for row in suggested["suggestions"]:
        candidate=RECOMMENDATION_MAP.get(row["module_id"])
        if candidate in academy_assignments.V9_MODULE_TITLES:
            modules.add(candidate)
    modules=[m for m in academy_assignments.V9_MODULE_TITLES if m in modules]
    token=secrets.token_urlsafe(32)
    digest=hashlib.sha256(token.encode("ascii")).hexdigest()
    now=datetime.now(timezone.utc)
    expiry=(now+timedelta(days=case["training_days"])).isoformat()
    with closing(get_connection()) as db:
        c=db.cursor()
        c.execute("""INSERT INTO ernasa_training_invites
            (token_hash,recipient,email,created_at,expires_at,revoked)
            VALUES(?,?,?,?,?,0)""",
            (digest,person["name"],person["email"],now.isoformat(),expiry))
        c.execute("""INSERT INTO ernasa_training_assignments
            (token_hash,company_id,assigned_by,program_id,modules_json,created_at,video_allowed)
            VALUES(?,?,?,?,?,?,?)""",
            (digest,tenant,case["authorized_by"],"ernasa_v9",
             json.dumps(modules,ensure_ascii=False),now.isoformat(),0))
        db.commit()
    if not _transition(case_id,tenant,"processing_report","training_sending",training_hash=digest):
        raise RuntimeError("SELİN training state changed unexpectedly")
    if os.getenv("ERNASA_SELIN_SEND_ENABLED") != "1":
        return
    from services import invite_email
    result=send_training(
        recipient=person["email"],name=person["name"],
        url="https://ernasa.com/egitim/"+token,expires_at=expiry,
        idempotency_key="selin-training-"+case_id)
    if not result:
        raise RuntimeError("Training delivery not confirmed")
    _transition(case_id,tenant,"training_sending","training_invited")


def _progress(case):
    with closing(get_connection()) as db:
        c=db.cursor()
        c.execute("""SELECT a.modules_json,p.state_json FROM ernasa_training_assignments a
            LEFT JOIN ernasa_training_v9_progress p ON p.token_hash=a.token_hash
            WHERE a.company_id=? AND a.token_hash=?""",
            (case["company_id"],case["training_token_hash"]))
        row=c.fetchone()
    if not row:
        raise ValueError("SELİN training record unavailable")
    modules=json.loads(row["modules_json"])
    state=json.loads(row["state_json"]) if row["state_json"] else {}
    completed=sum(state.get("done",{}).get(mid) is True for mid in modules)
    answered=sum(len(a) for mid,a in state.get("answers",{}).items()
                 if mid in modules and isinstance(a,dict))
    return {"completed_lessons":completed,"total_lessons":len(modules),
            "answered_questions":answered,"is_self_reported":True,
            "exam_success_verified":False}


def evaluation(tenant,case_id):
    case=get_case(tenant,case_id)
    if case["status"] not in ("training_invited","training_in_progress","evaluation_ready"):
        raise HTTPException(409,"Eğitim süreci henüz başlamadı.")
    progress=_progress(case)
    return {"case_id":case_id,"status":case["status"],"training":progress,
            "decision":"Kurucu/İK değerlendirmesi bekleniyor",
            "notice":"Ders ve yanıtlar öz bildirime dayalıdır; doğrulanmış sınav başarısı veya işe alım kararı değildir."}


def process_one():
    """One bounded background tick; no retries of uncertain outbound sends."""
    with closing(get_connection()) as db:
        c=db.cursor()
        c.execute("""SELECT s.id,s.company_id,s.status FROM selin_cases s
            JOIN interview_links i ON i.token=s.interview_token AND i.company_id=s.company_id
            JOIN interview_reports r ON r.token=i.token
            WHERE s.status='interview_invited' AND i.status='completed'
            ORDER BY s.created_at LIMIT 1""")
        pending=c.fetchone()
    if pending:
        case_id,tenant=pending["id"],pending["company_id"]
        if not _transition(case_id,tenant,"interview_invited","processing_report"):
            return False
        try:
            case=get_case(tenant,case_id)
            analysis=_archive_pdf(case)
            if os.getenv("ERNASA_SELIN_SEND_ENABLED") != "1":
                # Keep processing frozen until explicitly enabled, no training assignment.
                _transition(case_id,tenant,"processing_report","needs_attention",error="send_disabled")
                return True
            from services import invite_email
            _training(case,analysis,invite_email.send_training_invitation)
        except Exception as error:
            LOG.warning("SELİN post-interview processing paused: %s",type(error).__name__)
            fresh=get_case(tenant,case_id)
            if fresh["status"] in ("processing_report","training_sending"):
                _transition(case_id,tenant,fresh["status"],"needs_attention",
                            error=type(error).__name__[:100])
        return True
    with closing(get_connection()) as db:
        c=db.cursor()
        c.execute("""SELECT id,company_id,status,training_token_hash FROM selin_cases
            WHERE status IN ('training_invited','training_in_progress')
            ORDER BY updated_at LIMIT 20""")
        candidates=[dict(x) for x in c.fetchall()]
    for case in candidates:
        try:
            data=_progress(case)
        except Exception as error:
            LOG.warning("SELİN progress read unavailable: %s",type(error).__name__)
            continue
        if data["total_lessons"] and data["completed_lessons"]==data["total_lessons"]:
            return _transition(case["id"],case["company_id"],case["status"],"evaluation_ready")
        if data["completed_lessons"] or data["answered_questions"]:
            if case["status"]=="training_invited":
                return _transition(case["id"],case["company_id"],"training_invited","training_in_progress")
    return False


async def worker(stop):
    """Opt-in background processor; default deployment does not mutate anything."""
    import asyncio
    if os.getenv("ERNASA_SELIN_WORKER_ENABLED") != "1":
        return
    while not stop.is_set():
        try:
            await asyncio.to_thread(process_one)
        except Exception as error:
            LOG.warning("SELİN worker tick failed: %s",type(error).__name__)
        try:
            await asyncio.wait_for(stop.wait(),timeout=20)
        except TimeoutError:
            pass
