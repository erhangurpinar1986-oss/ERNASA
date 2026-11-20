from services import parser
import control_integration as portal_access
import recruitment
import portal_interviews
import partner_interview
import academy_audio
import academy_assignments
import academy_video
import ernasa_training
import candidate_experience
import ats
import ats_cv_import
import ik_cv_invites
from services import invite_email
import interview_assessments
import selin_workflow
import ats_public_cv
import ats_workflow
import ats_upload_queue
import ats_matching
from contextlib import asynccontextmanager, closing, nullcontext
import asyncio
from control import render_gateway
from control.interview_templates import PARTNER_INTRODUCTION_LIMIT, snapshot_question_count
from base64 import b64encode
from xml.sax.saxutils import escape
from pathlib import Path
from uuid import uuid4
from services.cv_service import extract_text
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, File, HTTPException, UploadFile,Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse,RedirectResponse,JSONResponse,Response,HTMLResponse
from database import initialize_database, get_connection
from services.candidate_service import create_interview_identity
from fastapi.staticfiles import StaticFiles
from services.ai_service import generate_interview_question, generate_job_fit_analysis
import os
import json
import secrets
import hmac
import hashlib
from fastapi import Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi import Request, Query
from pydantic import BaseModel, Field, SecretStr, ValidationError
from fastapi.responses import StreamingResponse
from reportlab.platypus import Spacer
import recruitment_pdf
from io import BytesIO


from starlette.middleware.base import BaseHTTPMiddleware

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        from fastapi.responses import Response
        from urllib.parse import unquote

        protected_html = {
            "aday-takip.html",
            "ernasa_1_1_panel.html",
            "ernasa_1_1_panel_duzeltilmis.html",
            "ernasa_v5_hazir.html",
            "ernasa_v5_ik.html",
            "ernasa_v5_ik_shield_backup.html",
            "ik.html",
            "ik_yedek.html",
            "index_yedek.html",
            "ik_yedek.css",
            "ik_yedek.js",
            "script_yedek.js",
            "style_yedek.css",
        }

        request_path = unquote(request.scope.get("path", ""))

        if request_path.startswith(("/platform", "/api/platform")):
            try:
                if portal_access.portal_for(request):
                    return JSONResponse({"detail": "Adres bulunamadı."}, status_code=404)
            except HTTPException as error:
                return JSONResponse({"detail": error.detail}, status_code=error.status_code)

        if request_path.lower().startswith("/static/"):
            filename = request_path.rsplit("/", 1)[-1].lower()

            if (
                filename in protected_html
                or ".bak" in filename
            ):
                return Response(
                    status_code=403,
                    headers={
                        "X-Content-Type-Options": "nosniff",
                        "X-Frame-Options": "DENY",
                        "Referrer-Policy": "strict-origin-when-cross-origin",
                    }
                )

        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if os.getenv("ERNASA_IK_LOCAL_PREVIEW") == "1":
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
            )
        if request_path.startswith(("/api/", "/ik", "/aday-takip", "/interview/")):
            response.headers["Cache-Control"] = "private, no-store"
        return response


@asynccontextmanager
async def lifespan(_app):
    # Isolated training schemas: do not modify existing candidate/interview data.
    ernasa_training.initialize_schema()
    candidate_experience.initialize_schema()
    academy_audio.initialize_schema()
    academy_assignments.initialize_schema()
    academy_video.initialize_schema()
    selin_workflow.initialize_schema()
    ats_public_cv.initialize_schema()
    # Explicit, one-time ERNASA education demo cleanup. Never enabled by default.
    if os.getenv("ERNASA_TRAINING_DEMO_CLEANUP") == "keep-merve-20261120":
        import training_demo_cleanup_20261120
        cleanup_result = await asyncio.to_thread(training_demo_cleanup_20261120.cleanup)
        print("ERNASA_TRAINING_DEMO_CLEANUP " + json.dumps(cleanup_result,ensure_ascii=True,sort_keys=True),flush=True)
    if os.getenv("ERNASA_ATS_DEMO_CLEANUP") == "four-demo-applicants-20261120":
        import ats_demo_cleanup_20261120
        result = await asyncio.to_thread(ats_demo_cleanup_20261120.cleanup)
        print("ERNASA_ATS_DEMO_CLEANUP " + json.dumps(result,ensure_ascii=True,sort_keys=True),flush=True)
    # Explicit, one-time live cleanup. The fixed snapshot and archive guards
    # abort without deleting if the targeted company data have changed.
    if os.getenv("ERNASA_IK_DEMO_CLEANUP") == "keep-merve-20261120":
        import ik_demo_cleanup_20261120
        outcome = await asyncio.to_thread(ik_demo_cleanup_20261120.cleanup)
        print("ERNASA_IK_DEMO_CLEANUP " + json.dumps(outcome,ensure_ascii=True,sort_keys=True),flush=True)
    # Read-only sender-domain diagnostic where supported; never send email at startup.
    # A restricted sending-only Resend API key may disallow domain listing.
    try:
        domain_diagnostic = await asyncio.to_thread(invite_email.inspect_sender_domain)
        print("ERNASA_EMAIL_PROVIDER_DIAGNOSTIC " + json.dumps(
            domain_diagnostic,ensure_ascii=True,sort_keys=True), flush=True)
    except Exception:
        print("ERNASA_EMAIL_PROVIDER_DIAGNOSTIC probe_unavailable",flush=True)
    stop = asyncio.Event()
    with render_gateway.start_control() as control_server:
        _app.state.control_server = control_server
        workers = [
            asyncio.create_task(ats_upload_queue.worker(UPLOAD_DIRECTORY,stop)),
            asyncio.create_task(interview_assessments.worker(generate_job_fit_analysis,stop)),
            asyncio.create_task(selin_workflow.worker(stop)),
        ]
        try:
            yield
        finally:
            stop.set()
            await asyncio.gather(*workers)


app = FastAPI(
    title="ERNASA API",
    description="Yapay Zekâ Destekli İnsan Kaynakları Asistanı",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(SecurityHeadersMiddleware)
app.include_router(ernasa_training.router)
app.include_router(candidate_experience.router)
app.include_router(academy_audio.router)
app.include_router(academy_assignments.router)
app.include_router(academy_video.router)

@app.middleware("http")
async def route_control(request: Request, call_next):
    server = getattr(app.state,"control_server",None)
    path = request.scope["path"]
    if server and (path == "/control" or path.startswith("/control/")):
        return await render_gateway.forward(request,server)
    return await call_next(request)

security = HTTPBasic()
IK_SESSION_SECRET = os.getenv("IK_SESSION_SECRET", "")

from shield_sessions import (
    create_ik_session,
    verify_ik_session,
    revoke_ik_session
)
from shield_rate_limit import get_login_identity, hash_login_identity, check_login_limit, register_failed_login_atomically


def require_ik_session(request: Request) -> portal_access.Actor:
    """Require a valid HR session and protect state-changing requests."""
    portal = portal_access.portal_for(request)
    if portal:
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            portal_access.check_origin(request, portal)
        actor = portal_access.session_actor(request, portal)
        if not actor:
            raise HTTPException(401, "Firma oturumu gerekli.")
        return actor
    session_value = request.cookies.get("ernasa_ik_session", "")

    if not verify_ik_session(session_value):
        raise HTTPException(
            status_code=401,
            detail="IK oturumu gerekli. Lutfen yeniden giris yapin."
        )

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin", "")
        allowed_origins = {
            "https://ernasa.com",
            "https://www.ernasa.com",
            "http://127.0.0.1:8081",
            "http://localhost:8081",
        }
        allowed_origins.add(legacy_ik_origin())

        if origin not in allowed_origins:
            raise HTTPException(
                status_code=403,
                detail="Guvenlik kontrolu: Gecersiz istek kaynagi."
            )

    return portal_access.Actor()



def verify_ik_login(
    credentials: HTTPBasicCredentials = Depends(security)
):
    correct_username = os.getenv("IK_USERNAME", "")
    correct_password = os.getenv("IK_PASSWORD", "")

    username_ok = secrets.compare_digest(
        credentials.username,
        correct_username
    )

    password_ok = secrets.compare_digest(
        credentials.password,
        correct_password
    )

    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=401,
            detail="Kullanıcı adı veya şifre hatalı.",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials.username
initialize_database()
app.add_middleware(
    CORSMiddleware,
allow_origins=[
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "https://ernasa.com",
    "https://www.ernasa.com",
],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

app.mount(
    "/assets",
    StaticFiles(directory=FRONTEND_DIR / "assets"),
    name="assets"
)

app.mount(
    "/static",
    StaticFiles(directory=FRONTEND_DIR),
    name="static"
)
app.mount(
    "/corporate",
    StaticFiles(directory=FRONTEND_DIR / "corporate", html=True),
    name="corporate"
)

UPLOAD_DIRECTORY = Path(os.getenv("ERNASA_IK_UPLOAD_DIRECTORY") or "uploads")
UPLOAD_DIRECTORY.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx"}
MAX_FILE_SIZE = 10 * 1024 * 1024

@app.get("/ik-giris")
def open_hr_login(request: Request):
    portal = portal_access.portal_for(request)
    if portal and not portal_access.control_call(portal, 'resolve'):
        raise HTTPException(403, "Firma portalı şu anda kullanıma kapalı.")
    return FileResponse(FRONTEND_DIR / "login.html")


def legacy_ik_origin():
    if os.getenv("ERNASA_IK_LOCAL_PREVIEW") == "1":
        port = os.getenv("ERNASA_IK_PORTAL_PORT")
        if port:
            return f"http://127.0.0.1:{port}"
    return 'http://127.0.0.1:8081' if os.getenv('RENDER') is None else 'https://ernasa.com'

@app.post("/api/ik-login")
def ik_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    new_password: str = Form(default=""),
):
    portal = portal_access.portal_for(request)
    if portal:
        portal_access.check_origin(request, portal)
        ip_hash = hash_login_identity(f"{portal.slug}:{get_login_identity(request)}")
        if check_login_limit(ip_hash):
            raise HTTPException(429, "Çok fazla giriş denemesi.")
        identity = {}
        if len(new_password) > 128:
            raise HTTPException(400, "Yeni parola 12–128 karakter olmalıdır.")
        if len(username) <= 254 and len(password) <= 128:
            identity = portal_access.control_call(portal, 'login', email=username.strip().lower(),
                                                  password=password, new_password=new_password)
        if identity.get('role') not in portal_access.STAFF_ROLES:
            register_failed_login_atomically(ip_hash)
            raise HTTPException(401, "Firma hesabı veya erişim bilgileri geçersiz.")
        if identity.get('must_change_password'):
            return JSONResponse({"success": False, "password_change_required": True})
        response = JSONResponse({"success": True})
        response.set_cookie(portal_access.COOKIE, portal_access.new_session(portal, identity),
                            httponly=True, secure=portal.origin.startswith('https://'), samesite='strict', max_age=28800)
        return response
    allowed_origins = {
        "https://ernasa.com",
        "https://www.ernasa.com",
        "http://127.0.0.1:8081",
        "http://localhost:8081",
    }
    allowed_origins.add(legacy_ik_origin())

    origin = request.headers.get("origin", "")

    if origin not in allowed_origins:
        raise HTTPException(
            status_code=403,
            detail="Guvenlik kontrolu: Gecersiz giris kaynagi."
        )

    correct_username = os.getenv("IK_USERNAME", "")
    correct_password = os.getenv("IK_PASSWORD", "")

    if not correct_username or not correct_password:
        raise HTTPException(
            status_code=503,
            detail="IK giris sistemi yapilandirilmamis."
        )

    username_ok = secrets.compare_digest(
        username,
        correct_username
    )

    password_ok = secrets.compare_digest(
        password,
        correct_password
    )

    ip_hash = hash_login_identity(get_login_identity(request))

    if check_login_limit(ip_hash):
        raise HTTPException(
            status_code=429,
            detail="Cok fazla hatali giris. 15 dakika sonra tekrar deneyin."
        )

    if not (username_ok and password_ok):
        if register_failed_login_atomically(ip_hash):
            raise HTTPException(
                status_code=429,
                detail="Cok fazla hatali giris. 15 dakika sonra tekrar deneyin."
            )
        raise HTTPException(
            status_code=401,
            detail="Kullanici adi veya sifre hatali."
        )

    response = JSONResponse(
        content={"success": True}
    )

    response.set_cookie(
        key="ernasa_ik_session",
        value=create_ik_session(username),
        httponly=True,
        secure=(os.getenv("RENDER") == "true" or request.url.scheme == "https"),
        samesite="lax"
    )

    return response


@app.post("/api/ik-logout")
def ik_logout(request: Request):
    portal = portal_access.portal_for(request)
    if portal:
        portal_access.check_origin(request, portal)
        token = request.cookies.get(portal_access.COOKIE, '')
        if token:
            portal_access.revoke_session(portal, token)
        response = JSONResponse({"success": True})
        response.delete_cookie(portal_access.COOKIE, httponly=True,
                               secure=portal.origin.startswith('https://'), samesite='strict')
        return response
    token = request.cookies.get("ernasa_ik_session", "")

    if token:
        revoke_ik_session(token)

    response = JSONResponse(content={"success": True})
    response.delete_cookie(
        key="ernasa_ik_session",
        path="/",
        samesite="lax",
        secure=(os.getenv("RENDER") == "true" or request.url.scheme == "https"),
        httponly=True
    )
    return response


def has_hr_panel_session(request):
    portal = portal_access.portal_for(request)
    if portal:
        return portal_access.session_actor(request, portal) is not None
    return verify_ik_session(request.cookies.get("ernasa_ik_session"))


@app.get("/api/ik/portal")
def portal_metadata(actor: portal_access.Actor = Depends(require_ik_session)):
    return {"company_portal": bool(actor.portal), "can_pdf": actor.role != 'hr_staff' and (not actor.portal or 'ai' in actor.products),
            "can_manage_personnel": bool(actor.portal and actor.role == 'supervisor'),
            "company_name": actor.company_name, "user_name": actor.user_name, "role": actor.role,
            "products": actor.products, "permissions": actor.permissions, "user_id": actor.user_id,
            "portal_slug": actor.portal.slug if actor.portal else None}


@app.get("/api/ik/ui-revision", include_in_schema=False)
def ui_revision(request: Request):
    if os.getenv("ERNASA_IK_LOCAL_PREVIEW") != "1" or not portal_access.portal_for(request):
        raise HTTPException(404)
    return {"revision":"commercial-polish"}


def require_ai(actor: portal_access.Actor = Depends(require_ik_session)):
    if actor.portal and 'ai' not in actor.products:
        raise HTTPException(403, "AI İK aboneliği gerekiyor.")
    return actor


def require_company(actor: portal_access.Actor = Depends(require_ik_session)):
    if not actor.portal:
        raise HTTPException(403, "Firma portalı oturumu gerekiyor.")
    return actor


def require_manager(actor: portal_access.Actor = Depends(require_company)):
    if actor.role not in recruitment.MANAGERS:
        raise HTTPException(403, "Firma yöneticisi yetkisi gerekiyor.")
    return actor


def require_ats(actor: portal_access.Actor = Depends(require_company)):
    if 'ats' not in actor.products:
        raise HTTPException(403, "ATS aboneliği gerekiyor.")
    return actor


def require_ats_manager(actor: portal_access.Actor = Depends(require_ats)):
    return require_manager(actor)


@app.get("/api/ik/usage")
def company_usage(actor: portal_access.Actor = Depends(require_manager)):
    usage = portal_access.control_call(actor.portal, 'usage', id=str(actor.user_id), version=actor.access_version)
    if not usage:
        raise HTTPException(403, "Paket bilgisi erişilebilir değil.")
    with closing(get_connection()) as connection:
        usage['monthly_interviews'] = recruitment.monthly_usage(connection.cursor(), actor)
    usage['interview_month'] = actor.month_bounds[1][:7] if len(actor.month_bounds) == 2 else None
    return usage


@app.get("/api/ik/positions")
def list_positions(actor: portal_access.Actor = Depends(require_ats)):
    return {"positions": recruitment.positions(actor)}


@app.post("/api/ik/positions", status_code=201)
def create_position(fields: recruitment.Position, actor: portal_access.Actor = Depends(require_ats_manager)):
    return recruitment.save_position(actor, fields)


@app.patch("/api/ik/positions/{position_id}")
def update_position(position_id: str, fields: recruitment.Position, actor: portal_access.Actor = Depends(require_ats_manager)):
    return recruitment.save_position(actor, fields, position_id)


@app.post("/api/ik/positions/{position_id}/close")
def close_managed_position(position_id: str, actor: portal_access.Actor = Depends(require_ats_manager)):
    return recruitment.close_position(actor,position_id)


@app.post("/api/ik/candidates", status_code=201)
def add_ats_candidate(fields: recruitment.AtsCandidate, actor: portal_access.Actor = Depends(require_ats)):
    return recruitment.create_ats_candidate(actor, fields)


def eligible_assignees(actor):
    result = portal_access.control_call(actor.portal, 'assignees', id=str(actor.user_id), version=actor.access_version)
    if 'users' not in result:
        raise HTTPException(403, "Aday atama yetkisi gerekiyor.")
    return result


@app.get("/api/ik/assignees")
def list_assignees(actor: portal_access.Actor = Depends(require_manager)):
    return eligible_assignees(actor)


@app.patch("/api/ik/candidates/{token}/assignment")
def assign_candidate(token: str, fields: recruitment.Assignment, actor: portal_access.Actor = Depends(require_manager)):
    if fields.assigned_user_id is not None and not any(
            user['id'] == fields.assigned_user_id for user in eligible_assignees(actor)['users']):
        raise HTTPException(404, "Firma personeli bulunamadı.")
    return recruitment.change_candidate(actor, token, fields)


@app.patch("/api/ik/candidates/{token}/evaluation")
def evaluate_candidate(token: str, fields: recruitment.Evaluation, actor: portal_access.Actor = Depends(require_manager)):
    return recruitment.change_candidate(actor, token, fields)


@app.get("/api/ik/ats/applications")
def list_ats_applications(search: str = Query("", max_length=160), position_id: str = Query("", max_length=64),
                          stage: str = Query("", max_length=30), limit: int = Query(25, ge=1, le=100),
                          offset: int = Query(0, ge=0), assignee: str = Query("",max_length=18),
                          actor: portal_access.Actor = Depends(require_ats)):
    return ats.list_applications(actor, search, position_id, stage, limit, offset, assignee)


@app.post("/api/ik/ats/applications", status_code=201)
def create_ats_application(fields: ats.ApplicationCreate, actor: portal_access.Actor = Depends(require_ats)):
    return ats.create_application(actor, fields)


@app.get("/api/ik/ats/applications/{application_id}")
def get_ats_application(application_id: str, actor: portal_access.Actor = Depends(require_ats)):
    return ats.details(actor, application_id)


@app.patch("/api/ik/ats/applications/{application_id}/candidate-name")
def update_ats_candidate_name(application_id: str, fields: ats.CandidateName,
                              actor: portal_access.Actor = Depends(require_ats_manager)):
    return ats.update_candidate_name(actor, application_id, fields)


@app.post("/api/ik/ats/cv-import/preview")
def preview_ats_cv(cv: UploadFile = File(...), actor: portal_access.Actor = Depends(require_ats)):
    return ats_cv_import.preview(cv.filename or 'cv', cv.file.read(ats.MAX_DOCUMENT + 1))


@app.post("/api/ik/ats/cv-import/save")
def save_ats_cv(selection: str = Form(..., max_length=4096), cv: UploadFile = File(...),
                actor: portal_access.Actor = Depends(require_ats)):
    try:
        fields = ats_cv_import.Selection.model_validate_json(selection)
    except ValidationError as error:
        raise HTTPException(422, 'Önizleme alanlarını kontrol edin.') from error
    try:
        return ats_cv_import.save(UPLOAD_DIRECTORY, actor, fields, cv.filename or 'cv',
                                  cv.file.read(ats.MAX_DOCUMENT + 1))
    except HTTPException as error:
        if error.status_code == 409:
            return {'status': 'skipped', 'message': error.detail}
        raise


@app.patch("/api/ik/ats/applications/{application_id}/assignment")
def assign_ats_application(application_id: str, fields: recruitment.Assignment,
                           actor: portal_access.Actor = Depends(require_ats_manager)):
    validate_assignee(actor, fields.assigned_user_id)
    return ats.assign_application(actor, application_id, fields.assigned_user_id)


def validate_assignee(actor, user_id):
    if user_id is not None and not any(user['id'] == user_id for user in eligible_assignees(actor)['users']):
        raise HTTPException(404, "Firma personeli bulunamadı.")


@app.put("/api/ik/ats/positions/{position_id}/members/{user_id}")
def change_position_member(position_id: str, user_id: int, enabled: bool,
                           actor: portal_access.Actor = Depends(require_ats_manager)):
    if enabled:
        validate_assignee(actor, user_id)
    return ats.set_position_member(actor, position_id, user_id, enabled)


@app.get("/api/ik/ats/positions/{position_id}/members")
def get_position_members(position_id: str, actor: portal_access.Actor = Depends(require_ats_manager)):
    return {"users":eligible_assignees(actor)['users'], "members":ats.position_members(actor,position_id)}


@app.post("/api/ik/ats/applications/{application_id}/notes", status_code=201)
def add_ats_note(application_id: str, fields: ats.Note, actor: portal_access.Actor = Depends(require_ats)):
    return ats.add_note(actor, application_id, fields)


@app.post("/api/ik/ats/applications/{application_id}/documents", status_code=201)
async def attach_ats_document(application_id: str, cv: UploadFile = File(...),
                              actor: portal_access.Actor = Depends(require_ats)):
    content = await cv.read(ats.MAX_DOCUMENT + 1)
    return ats.upload_document(UPLOAD_DIRECTORY, actor, application_id, cv.filename or 'cv', content)


@app.get("/api/ik/ats/applications/{application_id}/document-availability")
def ats_document_availability(application_id: str, actor: portal_access.Actor = Depends(require_ats)):
    return ats.document_availability(UPLOAD_DIRECTORY, actor, application_id)


@app.get("/api/ik/ats/applications/{application_id}/documents/{document_id}")
def read_ats_document(application_id: str, document_id: str, actor: portal_access.Actor = Depends(require_ats)):
    path, name, media = ats.get_document(UPLOAD_DIRECTORY, actor, application_id, document_id)
    return FileResponse(path, media_type=media, filename=name)


@app.post("/api/ik/ats/applications/{application_id}/upload-jobs", status_code=202)
def enqueue_ats_document(application_id: str, cv: UploadFile = File(...),
                         actor: portal_access.Actor = Depends(require_ats)):
    return ats_upload_queue.enqueue(actor,application_id,cv.filename or "cv",cv.file.read(ats.MAX_DOCUMENT+1))


@app.get("/api/ik/ats/applications/{application_id}/upload-jobs")
def ats_upload_status(application_id: str, actor: portal_access.Actor = Depends(require_ats)):
    return {"jobs":ats_upload_queue.jobs(actor,application_id)}


@app.get("/api/ik/ats/jobs")
def list_ats_jobs(actor: portal_access.Actor = Depends(require_ats_manager)):
    return {"jobs": ats.list_jobs(actor)}


@app.post("/api/ik/ats/jobs")
def save_ats_job(fields: ats.Job, actor: portal_access.Actor = Depends(require_ats_manager)):
    return ats.save_job(actor, fields)


def public_ats_company(request: Request):
    portal = portal_access.portal_for(request)
    if portal is None:
        raise HTTPException(404, "Başvuru bağlantısı bulunamadı.")
    if request.method == 'POST':
        portal_access.check_origin(request, portal)
    profile = portal_access.control_call(portal, 'resolve')
    if not profile or 'ats' not in profile['products']:
        raise HTTPException(404, "Başvuru bağlantısı bulunamadı.")
    return profile


@app.get("/api/ats/jobs/{job_id}")
def get_public_ats_job(job_id: str, profile: dict = Depends(public_ats_company)):
    with closing(get_connection()) as connection:
        return {**ats.public_job(connection.cursor(), profile['company_id'], job_id),
                "company_name": profile['name'],
                "cv_upload_enabled": ats_public_cv.enabled()}


@app.get("/basvuru/{job_id}")
def open_public_ats_job(job_id: str, profile: dict = Depends(public_ats_company)):
    get_public_ats_job(job_id, profile)
    return FileResponse(FRONTEND_DIR / 'ats_application.html', headers={"Cache-Control": "private, no-store"})


@app.post("/api/ats/jobs/{job_id}/apply", status_code=201)
def send_ats_application(job_id: str, fields: ats.PublicApplication, profile: dict = Depends(public_ats_company)):
    try:
        return ats.public_application(profile['company_id'], job_id, fields)
    except HTTPException as error:
        if error.status_code != 409:
            raise
        return {"success":True}


# A published ERNASA HR job can accept a PDF/DOCX resume directly.
# Each request is tenant-scoped and protected by the same candidate notice.
@app.post("/api/ats/jobs/{job_id}/apply-with-cv",status_code=201)
def send_ats_application_cv(
    job_id: str,
    name: str = Form(...,min_length=1,max_length=160),
    email: str = Form(...,min_length=3,max_length=254),
    phone: str = Form(default="",max_length=50),
    notice_version: str = Form(...,min_length=1,max_length=40),
    acknowledge: bool = Form(...),
    source: str = Form(default="direct",max_length=32),
    cv: UploadFile = File(...),
    request: Request = None,
    profile: dict = Depends(public_ats_company)
):
    if not ats_public_cv.enabled():
        raise HTTPException(404,"CV başvurusu etkin değil.")
    # Do not trust proxy headers. Use only the ASGI connection peer for rate limiting.
    client_ip=request.client.host if request and request.client else ""
    fields=ats.PublicApplication(name=name,email=email,phone=phone,
                                 acknowledge=acknowledge,notice_version=notice_version)
    result=ats_public_cv.submit(profile["company_id"],job_id,fields,
                                cv.filename or "cv",cv.file.read(ats.MAX_DOCUMENT+1),
                                source,client_ip)
    # The public response never exposes internal tenant or candidate identifiers.
    return {"success":True,"message":"Başvurunuz ve CV dosyanız kaydedildi."}


@app.patch("/api/ik/ats/applications/{application_id}/stage")
def move_ats_card(application_id: str, fields: ats_workflow.Move,
                  actor: portal_access.Actor = Depends(require_ats)):
    return ats_workflow.move(actor, application_id, fields)


def reviewer_roster(actor):
    response = portal_access.control_call(actor.portal, 'assignees', id=str(actor.user_id),
                                          version=actor.access_version, scope='reviewers')
    if 'users' not in response:
        raise HTTPException(403, "Firma yöneticisi yetkisi gerekiyor.")
    return response


@app.get("/api/ik/ats/reviewers")
def list_ats_reviewers(actor: portal_access.Actor = Depends(require_ats_manager)):
    return reviewer_roster(actor)


def validate_interviewer(actor, user_id):
    if actor.role != 'hr_staff':
        if not any(member['id']==user_id for member in reviewer_roster(actor)['users']):
            raise HTTPException(404, "Firma görüşmecisi bulunamadı.")
    elif actor.user_id != user_id or 'ats_schedule' not in actor.permissions:
        raise HTTPException(403, "Görüşme planlama yetkiniz yok.")


@app.get("/api/ik/ats/calendar")
def get_ats_calendar(actor: portal_access.Actor = Depends(require_ats)):
    return {"schedules":ats_workflow.list_schedules(actor)}


@app.get("/api/ik/ats/metrics")
def get_ats_metrics(actor: portal_access.Actor = Depends(require_ats)):
    return ats_workflow.metrics(actor)


@app.post("/api/ik/ats/applications/{application_id}/schedules", status_code=201)
def schedule_ats_interview(application_id: str, fields: ats_workflow.Schedule,
                           actor: portal_access.Actor = Depends(require_ats)):
    validate_interviewer(actor, fields.interviewer_id)
    return ats_workflow.save_schedule(actor, application_id, fields)


@app.patch("/api/ik/ats/applications/{application_id}/schedules/{schedule_id}")
def update_ats_interview(application_id: str, schedule_id: str, fields: ats_workflow.Schedule,
                         actor: portal_access.Actor = Depends(require_ats)):
    validate_interviewer(actor, fields.interviewer_id)
    return ats_workflow.save_schedule(actor, application_id, fields, schedule_id)


@app.put("/api/ik/ats/positions/{position_id}/criteria")
def set_ats_criteria(position_id: str, fields: ats_workflow.Criteria,
                     actor: portal_access.Actor = Depends(require_ats_manager)):
    return ats_workflow.save_criteria(actor, position_id, fields)


@app.get("/api/ik/ats/applications/{application_id}/evaluations")
def list_ats_evaluations(application_id: str, actor: portal_access.Actor = Depends(require_ats)):
    return ats_workflow.list_reviews(actor, application_id)


@app.put("/api/ik/ats/applications/{application_id}/evaluations")
def save_ats_evaluation(application_id: str, fields: ats_workflow.Review,
                        actor: portal_access.Actor = Depends(require_ats)):
    return ats_workflow.review(actor, application_id, fields)


@app.patch("/api/ik/ats/applications/{application_id}/evaluations/{evaluation_id}/decision")
def review_ats_evaluation(application_id: str,evaluation_id: str,fields: ats_workflow.ReviewDecision,
                          actor: portal_access.Actor = Depends(require_ats_manager)):
    return ats_workflow.decide_review(actor,application_id,evaluation_id,fields)


@app.get("/api/ik/ats/applications/{application_id}/offers")
def list_ats_offers(application_id: str, actor: portal_access.Actor = Depends(require_ats_manager)):
    return {"offers":ats_workflow.offers(actor, application_id)}


@app.post("/api/ik/ats/applications/{application_id}/offers", status_code=201)
def create_ats_offer(application_id: str, fields: ats_workflow.OfferCreate,
                     actor: portal_access.Actor = Depends(require_ats_manager)):
    return ats_workflow.create_offer(actor, application_id, fields)


@app.patch("/api/ik/ats/applications/{application_id}/offers/{offer_id}")
def update_ats_offer(application_id: str, offer_id: str, fields: ats_workflow.OfferChange,
                     actor: portal_access.Actor = Depends(require_ats_manager)):
    return ats_workflow.change_offer(actor, application_id, offer_id, fields)


@app.get("/api/ik/ats/applications/{application_id}/offers/{offer_id}/document")
def read_offer_document(application_id: str, offer_id: str, actor: portal_access.Actor = Depends(require_ats_manager)):
    candidate, offer = ats_workflow.offer_document(actor, application_id, offer_id)
    title = "İş Teklifi"
    buffer, document, styles, story = recruitment_pdf.layout(
        title, FRONTEND_DIR / "assets" / "ernasalogo.png", portal_access.logo(actor.portal, actor.company_id))
    missing = "Belirtilmedi"
    basis = {"gross": "Brüt", "net": "Net"}.get(offer["pay_basis"], missing)
    states = {"draft":"Taslak","approved":"Onaylandı","ready":"Gönderime hazır","sent":"Gönderildi",
              "accepted":"Kabul edildi","rejected":"Reddedildi","withdrawn":"Geri çekildi"}
    for heading, fields in (
        ("Teklif bilgileri", (("Firma", actor.company_name), ("Aday", candidate["name"]),
            ("Pozisyon", candidate["position"]), ("İş tanımı", offer["description"]))),
        ("Ücret ve koşullar", (("Ücret", f'{offer["amount_minor"] / 100:.2f} {offer["currency"]}'),
            ("Ücret türü", basis), ("Yan haklar", offer["benefits"]), ("Başlangıç", offer["starts"]),
            ("Son geçerlilik tarihi", offer["valid_until"]), ("Çalışma koşulları", offer["terms"]))),
        ("Onay", (("Kayıt durumu", states.get(offer["status"])),
                  ("Firma yetkilisi", "Ad, tarih ve imza: ____________________"),
                  ("Aday", "Tarih ve imza: ____________________"))),
    ):
        story.append(recruitment_pdf.paragraph(heading, styles["Heading2"]))
        for label, value in fields:
            story.append(recruitment_pdf.paragraph(f"{label}: {value or missing}", styles["BodyText"]))
    document.build(story)
    buffer.seek(0)
    return StreamingResponse(buffer, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="ERNASA_Teklif_{offer_id}.pdf"',
        "Cache-Control": "private, no-store",
    })


@app.get("/api/ik/history")
def company_history(actor: portal_access.Actor = Depends(require_manager)):
    control_data = portal_access.control_call(actor.portal,'history',id=str(actor.user_id),version=actor.access_version)
    if 'events' not in control_data:
        raise HTTPException(403, "İşlem geçmişi erişilebilir değil.")
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        cursor.execute('''SELECT r.user_id,r.event,r.target,r.recorded_at,COALESCE(c.name,p.name,d.name) AS record_name
            FROM recruitment_audit r
            LEFT JOIN ats_applications a ON r.target=a.id AND r.company_id=a.company_id
            LEFT JOIN ats_candidates c ON c.id=a.candidate_id AND c.company_id=a.company_id
            LEFT JOIN company_positions p ON r.target=p.id AND r.company_id=p.company_id
            LEFT JOIN ats_departments d ON r.target=d.id AND r.company_id=d.company_id
            WHERE r.company_id=? ORDER BY r.recorded_at DESC LIMIT 100''',(actor.company_id,))
        names = control_data.get('actors',{})
        return {'control':control_data['events'],'recruitment':[
            dict(row,actor_name=names.get(str(row['user_id']))) for row in cursor.fetchall()]}


@app.get("/api/ik/ats/privacy")
def privacy_settings(actor: portal_access.Actor = Depends(require_ats_manager)):
    return ats_workflow.privacy(actor)


@app.put("/api/ik/ats/privacy")
def set_privacy_settings(fields: ats_workflow.Retention, actor: portal_access.Actor = Depends(require_ats_manager)):
    return ats_workflow.privacy(actor, fields.retention_days)


@app.post("/api/ik/ats/applications/{application_id}/privacy-requests", status_code=201)
def request_candidate_privacy(application_id: str, fields: ats_workflow.PrivacyRequest,
                              actor: portal_access.Actor = Depends(require_ats_manager)):
    return ats_workflow.request_privacy(actor, application_id, fields)


@app.patch("/api/ik/ats/privacy/requests/{request_id}")
def review_privacy_request(request_id: str, fields: ats_workflow.PrivacyDecision,
                           actor: portal_access.Actor = Depends(require_ats_manager)):
    return ats_workflow.decide_privacy(actor, request_id, fields)


@app.get("/api/ik/ats/applications/{application_id}/export")
def export_candidate(application_id: str, actor: portal_access.Actor = Depends(require_ats_manager)):
    return ats_workflow.export_application(actor, application_id)


def require_supervisor(actor: portal_access.Actor = Depends(require_ik_session)):
    if not actor.portal or actor.role != 'supervisor':
        raise HTTPException(403, "Bu işlem firma süpervizörü yetkisi gerektirir.")
    return actor


class OwnPassword(BaseModel):
    current_password: SecretStr
    new_password: SecretStr
    confirm_password: SecretStr


async def read_own_password(request: Request):
    try:
        return OwnPassword.model_validate(await request.json())
    except ValueError:
        raise HTTPException(422,"Parola alanları gerekli.") from None


@app.get("/api/ik/profile")
def profile_details(actor: portal_access.Actor = Depends(require_company)):
    return portal_access.personnel_call(actor,'self')


@app.put("/api/ik/profile/password")
def change_own_password(request: Request, actor: portal_access.Actor = Depends(require_company),
                        fields: OwnPassword = Depends(read_own_password)):
    current, new = fields.current_password.get_secret_value(), fields.new_password.get_secret_value()
    if new != fields.confirm_password.get_secret_value():
        raise HTTPException(400,"Yeni parolalar eşleşmiyor.")
    if not 1 <= len(current) <= 128 or not 12 <= len(new) <= 128 or current == new:
        raise HTTPException(400,"Farklı ve 12–128 karakterlik yeni bir parola girin.")
    ip_hash = hash_login_identity(f"password:{actor.portal.slug}:{actor.user_id}:{get_login_identity(request)}")
    if check_login_limit(ip_hash):
        raise HTTPException(429,"Çok fazla parola denemesi.")
    try:
        return portal_access.personnel_call(actor,'change_own_password',password=current,new_password=new)
    except HTTPException as error:
        if error.status_code == 400:
            register_failed_login_atomically(ip_hash)
        raise


class CompanySector(BaseModel):
    sector: str = Field(min_length=1,max_length=100)


@app.get("/api/ik/company-settings")
def company_settings(actor: portal_access.Actor = Depends(require_manager)):
    return portal_access.personnel_call(actor,'company_settings')


@app.put("/api/ik/company-settings/sector")
def update_company_sector(fields: CompanySector, actor: portal_access.Actor = Depends(require_supervisor)):
    return portal_access.personnel_call(actor,'company_sector',sector=fields.sector)


@app.put("/api/ik/company-settings/logo")
async def update_company_logo(logo: UploadFile = File(...), actor: portal_access.Actor = Depends(require_supervisor)):
    image = await logo.read(512001)
    if not 0 < len(image) <= 512000:
        raise HTTPException(400,"En fazla 500 KB PNG veya JPEG yükleyin.")
    return portal_access.personnel_call(actor,'company_logo',image=b64encode(image).decode('ascii'))


class Department(BaseModel):
    name: str = Field(min_length=1,max_length=120)


@app.get("/api/ik/departments")
def list_departments(actor: portal_access.Actor = Depends(require_ats)):
    return {"departments":recruitment.departments(actor)}


@app.post("/api/ik/departments", status_code=201)
def create_department(fields: Department, actor: portal_access.Actor = Depends(require_ats_manager)):
    return recruitment.add_department(actor,fields.name)


@app.patch("/api/ik/departments/{department_id}")
def update_department(department_id: str,fields: Department,actor: portal_access.Actor = Depends(require_ats_manager)):
    return recruitment.rename_department(actor,department_id,fields.name)


class PersonnelDetails(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=1, max_length=254)
    role: str = Field(min_length=1, max_length=20)


class PersonnelUpdate(PersonnelDetails):
    active: bool


async def read_personnel_create(request: Request) -> PersonnelDetails:
    try:
        return PersonnelDetails.model_validate(await request.json())
    except (ValueError, ValidationError):
        raise HTTPException(422, "Personel formundaki alanları kontrol edin.") from None


@app.get("/api/ik/personnel")
def list_personnel(actor: portal_access.Actor = Depends(require_supervisor)):
    return portal_access.personnel_call(actor, 'personnel_list')


@app.post("/api/ik/personnel", status_code=201)
def create_personnel(actor: portal_access.Actor = Depends(require_supervisor),
                     details: PersonnelDetails = Depends(read_personnel_create)):
    return portal_access.personnel_call(actor, 'personnel_create', name=details.name, email=details.email,
                                        role=details.role)


@app.post("/api/ik/personnel/{user_id}/password-reset")
def reset_personnel_password(user_id: int, actor: portal_access.Actor = Depends(require_supervisor)):
    return portal_access.personnel_call(actor, 'personnel_reset', id=str(user_id))


class PersonnelGrants(BaseModel):
    permissions: list[str] = Field(max_length=2)


@app.patch("/api/ik/personnel/{user_id}/permissions")
def update_personnel_grants(user_id: int, fields: PersonnelGrants, actor: portal_access.Actor = Depends(require_supervisor)):
    if any(code not in ('ats_schedule','ats_stage') for code in fields.permissions):
        raise HTTPException(400, "Geçersiz ATS izni.")
    return portal_access.personnel_call(actor, 'personnel_grants', id=str(user_id),
                                        permissions=','.join(fields.permissions))


@app.patch("/api/ik/personnel/{user_id}")
def update_personnel(user_id: int, details: PersonnelUpdate, actor: portal_access.Actor = Depends(require_supervisor)):
    return portal_access.personnel_call(
        actor, 'personnel_update', id=str(user_id), name=details.name, email=details.email,
        role=details.role, active='1' if details.active else '0',
    )


def logo_response(content):
    if content is None:
        raise HTTPException(404, "Firma logosu bulunamadı.")
    return Response(content, media_type="image/png", headers={"Cache-Control": "private, no-store"})


@app.get("/api/ik/company-logo")
def hr_company_logo(actor: portal_access.Actor = Depends(require_ik_session)):
    return logo_response(portal_access.logo(actor.portal, actor.company_id))


@app.get("/ik")
def open_hr_panel(request: Request):
    if not has_hr_panel_session(request):
        return RedirectResponse(url="/ik-giris", status_code=302)

    return RedirectResponse(url="/ik-v5", status_code=302)
@app.get("/ik-v5")
def open_hr_panel_v5(request: Request):
    if not has_hr_panel_session(request):
        return RedirectResponse(
            url="/ik-giris",
            status_code=302
        )

    return FileResponse(FRONTEND_DIR / "ernasa_v5_ik.html")
@app.get("/aday-takip")
def open_candidate_tracking(request: Request):
    if not has_hr_panel_session(request):
        return RedirectResponse(url="/ik-giris", status_code=302)

    return FileResponse(FRONTEND_DIR / "aday-takip.html")



@app.get("/robots.txt", include_in_schema=False)
def seo_robots():
    content = (FRONTEND_DIR / "corporate" / "robots.txt").read_text(encoding="utf-8-sig")
    return Response(content=content, media_type="text/plain")

@app.get("/sitemap.xml", include_in_schema=False)
def seo_sitemap():
    content = (FRONTEND_DIR / "corporate" / "sitemap.xml").read_text(encoding="utf-8")
    return Response(content=content, media_type="application/xml")


# ERNASA_LEGAL_ROUTES_START — tek merkezden yasal sayfalar
@app.get("/kvkk", include_in_schema=False)
@app.get("/kvkk.html", include_in_schema=False)
def ernasa_public_kvkk():
    return FileResponse(FRONTEND_DIR / "kvkk.html", media_type="text/html; charset=utf-8")

@app.get("/gizlilik", include_in_schema=False)
@app.get("/gizlilik.html", include_in_schema=False)
def ernasa_public_privacy():
    return FileResponse(FRONTEND_DIR / "gizlilik.html", media_type="text/html; charset=utf-8")
# ERNASA_LEGAL_ROUTES_END

@app.get("/")
def home(request: Request):
    if portal_access.portal_for(request):
        return RedirectResponse("/ik", status_code=302, headers={"Cache-Control": "private, no-store"})
    from fastapi.responses import HTMLResponse
    html = (FRONTEND_DIR / "corporate" / "index.html").read_text(encoding="utf-8")
    html = html.replace('="assets/', '="/corporate/assets/')
    html = html.replace('="detay/', '="/corporate/detay/')
    html = html.replace('="style.css"', '="/corporate/style.css"')
    html = html.replace('="script.js"', '="/corporate/script.js"')
    html = html.replace('data-href="detay/', 'data-href="/corporate/detay/')
    html = html.replace('data-fallback="assets/', 'data-fallback="/corporate/assets/')
    return HTMLResponse(content=html)


interview_links = {}

@app.post("/api/cv/upload")
async def upload_cv(cv: UploadFile = File(...), _ik: portal_access.Actor = Depends(require_ai)):
    original_name = cv.filename or "cv"

    extension = Path(original_name).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Yalnızca PDF, DOC veya DOCX dosyaları yüklenebilir."
        )

    content = await cv.read(MAX_FILE_SIZE + 1)

    if not content:
        raise HTTPException(
            status_code=400,
            detail="Yüklenen dosya boş."
        )

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail="Dosya boyutu en fazla 10 MB olabilir."
        )

    safe_name = f"{uuid4().hex}{extension}"
    upload_directory = UPLOAD_DIRECTORY
    if _ik.company_id is not None:
        upload_directory = UPLOAD_DIRECTORY / "companies" / str(_ik.company_id)
        upload_directory.mkdir(parents=True, exist_ok=True)
    target_path = upload_directory / safe_name

    target_path.write_bytes(content)

    try:
        text = extract_text(str(target_path))
    except Exception:
        if _ik.company_id is not None:
            target_path.unlink(missing_ok=True)
        raise

    contact = parser.parse_candidate(text)

    return {
        "success": True,
        "original_name": original_name,
        "stored_name": safe_name,
        "text": text,
        "size": len(content),
        "contact": contact,
        "message": "CV başarıyla yüklendi ve analiz edildi."
    }
@app.post("/api/interview-links")
def create_interview_link(candidate: dict, _ik: portal_access.Actor = Depends(require_ai)):
    return save_interview_link(candidate, _ik)


def require_company_ai(actor: portal_access.Actor = Depends(require_company)):
    return require_ai(actor)


@app.get("/api/ik/ai/cv/positions")
def interview_cv_positions(actor: portal_access.Actor = Depends(require_company_ai)):
    return {"positions": [
        {"id": row["id"], "name": row["name"]} for row in recruitment.positions(actor) if row["open"]
    ]}


@app.post("/api/ik/ai/cv/preview")
def preview_interview_cv(cv: UploadFile = File(...), actor: portal_access.Actor = Depends(require_company_ai)):
    return ats_cv_import.preview(cv.filename or 'cv', cv.file.read(ats.MAX_DOCUMENT + 1))


@app.post("/api/ik/ai/cv/invite")
def invite_from_cv(selection: str = Form(..., max_length=4096), cv: UploadFile = File(...),
                   actor: portal_access.Actor = Depends(require_company_ai)):
    try:
        fields = ik_cv_invites.Selection.model_validate_json(selection)
    except ValidationError as error:
        raise HTTPException(422, 'Aday bilgilerini ve pozisyonu kontrol edin.') from error
    return ik_cv_invites.save(UPLOAD_DIRECTORY, actor, fields, cv.filename or 'cv',
                              cv.file.read(ats.MAX_DOCUMENT + 1), save_interview_link)


class LinkedInterview(BaseModel):
    document_id: str | None = Field(default=None,max_length=64)


def require_combined(actor: portal_access.Actor = Depends(require_ats)):
    return require_ai(actor)


@app.post("/api/ik/interviews/{token}/send-email")
def send_interview_invitation_email(
    token: str, actor: portal_access.Actor = Depends(require_ai)
):
    """Dispatch an existing invitation using the verified ERNASA sender.

    Recipient and URL are read from the tenant-scoped stored invitation; the
    browser cannot provide an arbitrary address, message or destination.
    """
    if not __import__("re").fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise HTTPException(404, "Mülakat daveti bulunamadı.")
    try:
        invite_email.configured_sender()
    except invite_email.EmailConfigurationError as error:
        raise HTTPException(503, str(error)) from error

    now = datetime.now(timezone.utc)
    scope, parameters = recruitment.scope(actor)
    with recruitment.write(actor.company_id) as cursor:
        cursor.execute(f"""SELECT token,name,email,company,position,expires_at,status
            FROM interview_links WHERE token=? AND {scope}""", (token, *parameters))
        record = cursor.fetchone()
        if record is None:
            raise HTTPException(404, "Mülakat daveti bulunamadı.")
        candidate = dict(record)
        if candidate["status"] not in ("waiting", "created", "opened"):
            raise HTTPException(409, "Bu mülakat için yeni davet gönderilemez.")
        if not candidate.get("expires_at") or datetime.fromisoformat(candidate["expires_at"]) <= now:
            raise HTTPException(410, "Mülakat bağlantısının süresi dolmuş.")
        try:
            invite_email.invitation_message(candidate, "")
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

        cursor.execute("SELECT status, initiated_at FROM ik_invitation_email_sends WHERE token=?", (token,))
        previous = cursor.fetchone()
        if previous and previous["status"] == "sent":
            return {"success": True, "already_sent": True,
                    "message": "Bu davet daha önce kurumsal e-postadan gönderildi."}
        if previous and previous["status"] == "pending":
            try:
                recent = (now - datetime.fromisoformat(previous["initiated_at"])).total_seconds() < 120
            except (TypeError, ValueError):
                recent = True
            if recent:
                raise HTTPException(409, "E-posta gönderimi sürüyor. Lütfen yeniden göndermeyin.")
        cursor.execute("""INSERT INTO ik_invitation_email_sends
            (token,company_id,status,initiated_at,sent_at,provider_message_id)
            VALUES (?,?,?, ?,NULL,NULL)
            ON CONFLICT (token) DO UPDATE SET status='pending', initiated_at=excluded.initiated_at,
                sent_at=NULL,provider_message_id=NULL""",
            (token,actor.company_id,"pending",now.isoformat()))

    origin = actor.portal.origin if actor.portal else legacy_ik_origin()
    url = f"{origin}/interview/{token}"
    try:
        provider_id = invite_email.send_invitation(
            candidate, url, idempotency_key="ernasa-interview-" + token)
    except invite_email.EmailDeliveryError as error:
        with recruitment.write(actor.company_id) as cursor:
            cursor.execute("""UPDATE ik_invitation_email_sends SET status='failed'
                WHERE token=? AND status='pending'""", (token,))
        raise HTTPException(502, str(error)) from error

    with recruitment.write(actor.company_id) as cursor:
        cursor.execute("""UPDATE ik_invitation_email_sends
            SET status='sent',sent_at=?,provider_message_id=?
            WHERE token=? AND status='pending'""",
            (datetime.now(timezone.utc).isoformat(),provider_id,token))
        if actor.company_id is not None:
            recruitment.record(cursor, actor, "interview_invitation_email_sent", token)
    return {"success":True, "already_sent":False,
            "message":"Mülakat daveti kurumsal e-posta servisine teslim edildi."}


@app.post("/api/ik/ats/applications/{application_id}/ai-interview", status_code=201)
def create_linked_ai_interview(application_id: str, fields: LinkedInterview,
                               actor: portal_access.Actor = Depends(require_combined)):
    text = ats_matching.source_text(UPLOAD_DIRECTORY,actor,application_id,fields.document_id) if fields.document_id else ''
    return save_interview_link({'cv_text':text}, actor, application_id,cv_document_id=fields.document_id)



# SELİN automated candidate-to-training case flow (tenant-scoped and opt-in).
# Existing ATS, interview, PDF and Academy routes remain unchanged.
class SelinStart(BaseModel):
    document_id: str = Field(min_length=1,max_length=64)


@app.post("/api/ik/selin/applications/{application_id}/start",status_code=201)
def selin_start(application_id: str,body: SelinStart,
                actor: portal_access.Actor = Depends(require_combined)):
    require_manager(actor)
    return selin_workflow.begin(actor,application_id,body.document_id,UPLOAD_DIRECTORY,
                                save_interview_link,send_interview_invitation_email)


@app.get("/api/ik/selin/cases")
def selin_cases(actor: portal_access.Actor = Depends(require_combined)):
    require_manager(actor)
    return {"cases":selin_workflow.list_cases(actor.company_id)}


@app.get("/api/ik/selin/cases/{case_id}")
def selin_case(case_id: str,actor: portal_access.Actor = Depends(require_combined)):
    require_manager(actor)
    return selin_workflow.get_case(actor.company_id,case_id)


@app.get("/api/ik/selin/cases/{case_id}/report.pdf")
def selin_case_pdf(case_id: str,actor: portal_access.Actor = Depends(require_combined)):
    require_manager(actor)
    data=selin_workflow.archived_pdf(actor.company_id,case_id)
    return Response(data,media_type="application/pdf",
                    headers={"Content-Disposition":'attachment; filename="ERNASA_SELIN_Mulakat_Raporu.pdf"'})


@app.get("/api/ik/selin/cases/{case_id}/evaluation")
def selin_case_evaluation(case_id: str,actor: portal_access.Actor = Depends(require_combined)):
    require_manager(actor)
    return selin_workflow.evaluation(actor.company_id,case_id)


@app.get("/api/ik/ats/applications/{application_id}/documents/{document_id}/match")
def match_ats_document(application_id: str, document_id: str,
                       actor: portal_access.Actor = Depends(require_ats)):
    return ats_matching.evidence_match(UPLOAD_DIRECTORY, actor, application_id, document_id)

class ATSManualCVText(BaseModel):
    text: str = Field(min_length=10,max_length=40000)


@app.post("/api/ik/ats/applications/{application_id}/documents/{document_id}/match-text")
def match_ats_document_supplied_text(
        application_id: str, document_id: str, body: ATSManualCVText,
        actor: portal_access.Actor = Depends(require_ats)):
    # No persistence: reviewer-entered evidence is scored only for this request.
    # The original CV, completed interviews, answers and reports remain untouched.
    return ats_matching.evidence_match(
        UPLOAD_DIRECTORY,actor,application_id,document_id,override_text=body.text)


def save_interview_link(candidate, _ik, application_id=None, *, ernasa_invite=False, partner_questions=None,
                        partner_template=None, draft_id=None, cv_evidence=(), cv_document_id=None,
                        active_cursor=None, introduction=None):
    company_name = _ik.company_name if _ik.portal else candidate.get("company", "")
    if _ik.portal and not company_name:
        raise HTTPException(503, "Doğrulanmış firma unvanı alınamadı.")
    if _ik.portal and "company" in candidate and candidate["company"] != company_name:
        raise HTTPException(422, "Firma bilgisi doğrulanmış portal unvanıyla eşleşmiyor.")
    interview_id = secrets.token_urlsafe(32)

    created_at = datetime.now(timezone.utc)
    expires_at = created_at + timedelta(hours=8)

    position_id = candidate.get("position_id") if _ik.portal else None
    position = candidate.get("position", "")
    transaction = recruitment.write(_ik.company_id) if active_cursor is None else nullcontext(active_cursor)
    with transaction as cursor:
        if _ik.portal:
            if application_id is not None:
                application = ats.application(cursor,_ik,application_id)
                if application['closure']:
                    raise HTTPException(409, 'Kapalı başvuruda mülakat oluşturulamaz.')
                candidate = {**{key:application[key] for key in ('name','phone','email')}, **candidate}
                candidate["name"] = ats.validate_candidate_name(candidate["name"])
                position_id, position = application['position_id'], application['position']
            elif position_id is not None:
                if 'ats' not in _ik.products:
                    raise HTTPException(403, "ATS aboneliği gerekiyor.")
                if not isinstance(position_id, str) or len(position_id) > 64:
                    raise HTTPException(400, "Geçersiz pozisyon.")
                position = recruitment.position_name(cursor, _ik.company_id, position_id)
                recruitment.staff_position(cursor, _ik, position_id)
            recruitment.enforce_interview_quota(cursor, _ik)
            template = (partner_template if partner_questions else portal_access.control_call(
                _ik.portal,"interview_template",id=str(_ik.company_id),position_id=position_id or ""))
            if draft_id:
                cursor.execute("""DELETE FROM ik_partner_drafts WHERE id=? AND company_id=?
                    AND created_by=? AND expires_at>?""",(draft_id,_ik.company_id,_ik.user_id,created_at.isoformat()))
                if cursor.rowcount != 1:
                    raise HTTPException(409,"CV soru taslağı kullanılmış veya süresi dolmuş.")
        identity = create_interview_identity(cursor)
        cursor.execute("""
            INSERT INTO interview_links (
                token, name, phone, email, company, position, expires_at,
                created_at, status, cv_text, company_id, position_id, created_by, application_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (interview_id, candidate.get("name", ""), candidate.get("phone", ""),
              candidate.get("email", ""), company_name, position, expires_at.isoformat(),
              created_at.isoformat(), "waiting", candidate.get("cv_text", ""),
              _ik.company_id, position_id, _ik.user_id, application_id))
        if _ik.portal:
            if cv_document_id:
                cursor.execute("INSERT INTO ats_interview_cv_sources VALUES (?,?,?)",
                               (interview_id,_ik.company_id,cv_document_id))
            if ernasa_invite:
                cursor.execute("INSERT INTO ik_ernasa_invites VALUES (?,?,?)",
                               (interview_id,_ik.company_id,_ik.user_id))
            if partner_questions:
                cursor.execute("INSERT INTO ik_partner_interviews(token,company_id,question_count) VALUES (?,?,?)",
                               (interview_id,_ik.company_id,len(partner_questions)))
                if introduction is not None:
                    cursor.execute("INSERT INTO ik_partner_introductions(token,company_id,question) VALUES (?,?,?)",
                                   (interview_id,_ik.company_id,introduction))
                if cv_evidence:
                    cursor.execute("INSERT INTO ik_partner_cv_evidence VALUES (?,?,?)",
                                   (interview_id,_ik.company_id,json.dumps(cv_evidence,ensure_ascii=False)))
                for number, question in enumerate(partner_questions,1):
                    if number <= 10:
                        cursor.execute("INSERT INTO interview_questions VALUES (?,?,?)",(interview_id,number,question))
                    else:
                        cursor.execute("INSERT INTO ik_partner_extra_questions(token,question_number,question) VALUES (?,?,?)",
                                       (interview_id,number,question))
            if template:
                cursor.execute("INSERT INTO ik_interview_template_snapshots VALUES (?,?,?,?,?)",
                    (interview_id,_ik.company_id,template["name"],template["language"],
                     json.dumps(template["questions"],ensure_ascii=False)))
                if not partner_questions:
                    for number, question in enumerate(template["questions"],1):
                        cursor.execute("INSERT INTO interview_questions VALUES (?,?,?)",
                                       (interview_id,number,question["text"]))
            recruitment.record(cursor, _ik, "interview_link_created", interview_id)

    origin = _ik.portal.origin if _ik.portal else legacy_ik_origin()
    return {
        "success": True,
        "candidate_number": identity["candidate_number"],
        "security_code": identity["security_code"],
        "interview_id": interview_id,
        "expires_at": expires_at.isoformat(),
        "created_at": created_at.isoformat(),
        "validity_hours": 8,
        "status": "waiting",
        "name": candidate.get("name",""),
        "email": candidate.get("email",""),
        "phone": candidate.get("phone",""),
        "company": company_name,
        "position": position,
        "interview_url": f"{origin}/interview/{interview_id}"
    }


def ernasa_invite_service(tenant, slug, port, runtime, evaluator=None):
    """An in-process adapter installed only by the isolated CONTROL launcher."""
    portal = portal_access.Portal(slug, f"http://{slug}.ernasa.com:{port}")

    def invoke(operation, founder_id, profile, *, name="", email="", token="", filename="", cv=b"",
               extras=(), introduction=None, template=None):
        if profile['id'] != tenant or profile['name'] != 'ERNASA' or not profile['ai_allowed']:
            raise HTTPException(403,"ERNASA davet hesabı etkin değil.")
        if operation == "prepare":
            review = partner_interview.analyze_cv(runtime,filename,cv)
            token = secrets.token_hex(16)
            now = datetime.now(timezone.utc)
            with recruitment.write(tenant) as cursor:
                cursor.execute("DELETE FROM ik_partner_drafts WHERE company_id=? AND expires_at<=?",(tenant,now.isoformat()))
                cursor.execute("INSERT INTO ik_partner_drafts VALUES (?,?,?,?,?,?,?)",
                    (token,tenant,founder_id,name,email,json.dumps(review,ensure_ascii=False),(now+timedelta(hours=1)).isoformat()))
            return token
        if operation in ("draft","create"):
            with closing(get_connection()) as db:
                cursor = db.cursor()
                cursor.execute("""SELECT name,email,suggestions FROM ik_partner_drafts
                    WHERE id=? AND company_id=? AND created_by=? AND expires_at>?""",
                    (token,tenant,founder_id,datetime.now(timezone.utc).isoformat()))
                draft = cursor.fetchone()
            if draft is None:
                raise HTTPException(404,"CV soru taslağı bulunamadı.")
            review = json.loads(draft["suggestions"])
            if isinstance(review,list):
                review = {"suggestions":review,"cv_evidence":[]}
            locked = partner_interview.first_round_questions(template)
            if operation == "draft":
                return {"name":draft["name"],"locked":locked,**review}
            if locked is None:
                raise HTTPException(409,"Onaylı 10 Türkçe soru yapılandırılmadı.")
            if not 2 <= len(extras) <= 3 or any(not question or len(question)>700 for question in extras):
                raise HTTPException(400,"İki veya üç geçerli ek soru girin.")
            if introduction is not None:
                introduction = introduction.strip()
                if not 1 <= len(introduction) <= PARTNER_INTRODUCTION_LIMIT:
                    raise HTTPException(400,"Tanışma sorusu 1–700 karakter olmalıdır.")
            actor = portal_access.Actor(company_id=tenant, role='founder', portal=portal, user_id=founder_id,
                company_name=profile['name'], products=('ai',),
                interview_quota=profile['interview_quota'], month_bounds=tuple(profile['month_bounds']))
            result = save_interview_link({"name":draft["name"],"email":draft["email"],"position":"ERNASA Satış Ortağı"},
                                         actor,ernasa_invite=True,partner_questions=locked+list(extras),
                                         partner_template=template,draft_id=token,
                                         cv_evidence=review["cv_evidence"],introduction=introduction)
            return result["interview_id"]
        with closing(get_connection()) as db:
            cursor = db.cursor()
            query = """SELECT i.token,i.name,i.email,i.status,i.position,i.expires_at,p.question_count,p.report,p.error
                FROM ik_ernasa_invites n JOIN interview_links i ON i.token=n.token AND i.company_id=n.company_id
                LEFT JOIN ik_partner_interviews p ON p.token=i.token AND p.company_id=i.company_id
                WHERE n.company_id=?"""
            if operation == "list":
                cursor.execute(query + " ORDER BY i.created_at DESC LIMIT 100", (tenant,))
                return [invitation(row) for row in cursor.fetchall()]
            cursor.execute(query + " AND i.token=?", (tenant,token))
            row = cursor.fetchone()
            if row is None:
                raise HTTPException(404,"Davet bulunamadı.")
            result = invitation(row)
            if row["status"] == "completed":
                result["answers"] = [dict(item) for item in portal_interviews.answers(cursor,token,row["question_count"] or 10).values()]
                result["introduction_note"] = portal_interviews.introduction_note(cursor,token,tenant)
                cursor.execute("SELECT criteria FROM ik_partner_cv_evidence WHERE token=? AND company_id=?",(token,tenant))
                cv = cursor.fetchone()
                result["cv_evidence"] = json.loads(cv["criteria"]) if cv else []
                cursor.execute("SELECT job_fit_analysis FROM interview_reports WHERE token=?", (token,))
                analysis = cursor.fetchone()
                if analysis and analysis["job_fit_analysis"] != "Sentetik mülakat değerlendirmesi.":
                    result["analysis"] = analysis["job_fit_analysis"]
        if operation != "evaluate":
            return result
        if row["status"] != "completed" or not row["question_count"]:
            raise HTTPException(409,"Tamamlanmış kilitli mülakat gerekir.")
        report, error = None, None
        try:
            if evaluator is None:
                raise RuntimeError("İzole ortamda AI değerlendirme sağlayıcısı yapılandırılmadı.")
            report = partner_interview.assessment(result["answers"],evaluator)
        except (ValueError, RuntimeError) as failure:
            error = str(failure)
        except Exception:
            error = "AI değerlendirme sağlayıcısı yanıt vermedi veya işlem tamamlanamadı."
        with recruitment.write(tenant) as cursor:
            cursor.execute("""UPDATE ik_partner_interviews SET report=COALESCE(?,report),error=?
                WHERE token=? AND company_id=?""",(json.dumps(report,ensure_ascii=False) if report else None,error,token,tenant))
        return token

    def invitation(row):
        data = dict(row)
        if data["status"] in ("waiting","created","opened") and _link_deadline_passed(data):
            data["status"] = "expired"
        data["url"] = f"{portal.origin}/interview/{row['token']}"
        return data

    return invoke
def _link_deadline_passed(interview: dict) -> bool:
    """The eight-hour deadline limits STARTING, not finishing."""
    expires = datetime.fromisoformat(interview["expires_at"])
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= expires


def _intro_question() -> str:
    return (
        "Merhaba, hoş geldiniz. Bu görüşmede sizi ve çalışma deneyimlerinizi "
        "biraz daha yakından tanımaya çalışacağım. Burada doğru ya da yanlış "
        "cevap yok; soruları kendi deneyimlerinize göre rahatça yanıtlayabilirsiniz. "
        "Hazırsanız, öncelikle kendinizden biraz bahseder misiniz?"
    )


@app.get("/api/interview-links/{token}")
def get_interview_link(token: str, company_id: int | None = Depends(portal_access.candidate_company)):
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT * FROM interview_links WHERE token = ? AND company_id IS NOT DISTINCT FROM ? AND status IS DISTINCT FROM 'new'", (token, company_id))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Mülakat bağlantısı bulunamadı.")
        interview = dict(row)
        cursor.execute("SELECT name,language,questions FROM ik_interview_template_snapshots WHERE token=? AND company_id=?",
                       (token,company_id))
        template = cursor.fetchone()
        language = template["language"] if template else "tr"
        cursor.execute("SELECT question_count FROM ik_partner_interviews WHERE token=? AND company_id=?",(token,company_id))
        partner = cursor.fetchone()
        question_count = partner["question_count"] if partner else snapshot_question_count(template)
        sales_round = None
        if partner or (template and language == "tr" and template["name"] in {
            "ERNASA B2B Satış Ortağı", "ERNASA B2B Satış Ortağı · 1. Tur",
        }):
            sales_round = "first"
        elif template and language == "en" and question_count == 1:
            sales_round = "english_demo"
        status = interview["status"]
        expired = status == "expired" or (
            status in ("waiting", "created", "opened") and _link_deadline_passed(interview)
        )
        if expired:
            # Yalnızca bilgilendirme: başlatma endpoint'i 410 engelini korur.
            return {
                "success": True,
                "candidate": {key: interview.get(key) for key in ("name", "company", "position")},
                "expires_at": interview["expires_at"],
                "status": "expired",
                "language": language,
                "question_count": question_count,
                "sales_round": sales_round,
                "can_start": False,
            }
        return {
            "success": True,
            "candidate": {key: interview.get(key) for key in ("name", "company", "position")},
            "expires_at": interview["expires_at"],
            "status": status,
            "language": language,
            "question_count": question_count,
            "sales_round": sales_round,
        }
    finally:
        connection.close()


@app.get("/api/interview-links/{token}/company-logo")
def candidate_logo(token: str, request: Request, company_id: int | None = Depends(portal_access.candidate_company)):
    if company_id is None:
        raise HTTPException(404, "Firma logosu bulunamadı.")
    get_interview_link(token, company_id)
    return logo_response(portal_access.logo(portal_access.portal_for(request), company_id))


@app.get("/interview/{token}")
def open_interview(token: str, company_id: int | None = Depends(portal_access.candidate_company)):
    if company_id is not None:
        get_interview_link(token, company_id)
    return FileResponse(FRONTEND_DIR / "index.html")


@app.post("/api/interviews/{token}/opened")
def mark_interview_opened(token: str, company_id: int | None = Depends(portal_access.candidate_company)):
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT * FROM interview_links WHERE token = ? AND company_id IS NOT DISTINCT FROM ? AND status IS DISTINCT FROM 'new'", (token, company_id))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Mülakat bağlantısı bulunamadı.")
        interview = dict(row)
        status = interview["status"]
        if status == "expired" or (status in ("waiting", "created", "opened") and _link_deadline_passed(interview)):
            raise HTTPException(status_code=410, detail="Mülakata başlama süresi dolmuş.")
        if status in ("waiting", "created"):
            cursor.execute("UPDATE interview_links SET status = ? WHERE token = ? AND status = ?", ("opened", token, status))
            connection.commit()
        return {"success": True}
    finally:
        connection.close()


@app.post("/api/interviews/{token}/start")
def start_interview(token: str, company_id: int | None = Depends(portal_access.candidate_company)):
    if company_id is not None:
        return portal_interviews.start(token, company_id, _link_deadline_passed, _intro_question, generate_interview_question)
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT * FROM interview_links WHERE token = ? AND company_id IS NOT DISTINCT FROM ? AND status IS DISTINCT FROM 'new'", (token, company_id))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Mülakat bağlantısı bulunamadı.")
        interview = dict(row)
        status = interview["status"]
        if status == "completed":
            raise HTTPException(status_code=409, detail="Bu mülakat zaten tamamlandı.")
        if status == "expired":
            raise HTTPException(status_code=410, detail="Mülakata başlama süresi dolmuş.")
        if status != "started":
            if status not in ("waiting", "created", "opened"):
                raise HTTPException(status_code=409, detail="Mülakat durumu geçersiz.")
            if _link_deadline_passed(interview):
                cursor.execute("UPDATE interview_links SET status = 'expired' WHERE token = ? AND status IN ('waiting', 'created', 'opened')", (token,))
                connection.commit()
                raise HTTPException(status_code=410, detail="Mülakata başlama süresi dolmuş.")
            # Starting is persisted BEFORE the deadline, so later requests can continue.
            cursor.execute("UPDATE interview_links SET status = 'started' WHERE token = ? AND status IN ('waiting', 'created', 'opened')", (token,))
            connection.commit()

        # A started candidate may reopen the interview after the 8-hour deadline.
        cursor.execute("SELECT question_number, question, answer FROM interview_answers WHERE token = ? ORDER BY question_number ASC", (token,))
        rows = cursor.fetchall()
        unique = {}
        for answer_row in rows:
            unique.setdefault(answer_row["question_number"], answer_row)
        if len(unique) >= 10:
            raise HTTPException(status_code=409, detail="Bu mülakat zaten tamamlandı.")
        if not unique:
            next_number, question = 1, _intro_question()
        else:
            next_number = max(unique) + 1
            history = "\n".join(f"Soru {r['question_number']}: {r['question']}\nCevap: {r['answer']}" for r in unique.values())
            question = generate_interview_question(
                cv_text=interview.get("cv_text", "") or "",
                position=interview.get("position", "") or "",
                previous_answers=history,
                question_number=next_number,
            )
        return {"success": True, "status": "started", "question_number": next_number, "question": question}
    finally:
        connection.close()


@app.post("/api/interviews/{token}/answer")
def submit_interview_answer(token: str, payload: dict, company_id: int | None = Depends(portal_access.candidate_company)):
    if company_id is not None:
        return portal_interviews.submit(token, company_id, payload, _intro_question, generate_interview_question)
    answer = str(payload.get("answer", "")).strip()
    question_number = int(payload.get("question_number", 1))
    question_text = str(payload.get("question", "")).strip()
    if not answer:
        raise HTTPException(
            status_code=400,
            detail="Cevap boş bırakılamaz."
        )

    connection = get_connection()
    cursor = connection.cursor()

    try:
        cursor.execute(
            """
            SELECT * FROM interview_links
            WHERE token = ? AND company_id IS NOT DISTINCT FROM ? AND status IS DISTINCT FROM 'new'
            """,
            (token, company_id)
        )

        row = cursor.fetchone()

        if not row:
            raise HTTPException(
                status_code=404,
                detail="Mülakat bağlantısı bulunamadı."
            )
        interview = dict(row)
        if interview["status"] != "started":
            raise HTTPException(
                status_code=409,
                detail="Cevap göndermek için mülakatın başlamış olması gerekir."
            )
        # An already-started interview is allowed to finish after expires_at.
        cv_text = interview.get("cv_text", "") or ""
        position = interview.get("position", "") or ""



        cursor.execute(
            """
            INSERT INTO interview_answers (
                token,
                question_number,
                question,
                answer,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                token,
                question_number,
                question_text,
                answer,
                datetime.now(timezone.utc).isoformat()
            )
        )

        connection.commit()
        cursor.execute(
            """
            SELECT question_number, question, answer
            FROM interview_answers
            WHERE token = ?
            ORDER BY question_number ASC
            """,
            (token,)
        )

        history_rows = cursor.fetchall()

        previous_answers = "\n".join(
            [
                f"Soru {row['question_number']}: {row['question']}\n"
                f"Cevap: {row['answer']}"
                for row in history_rows
            ]
        )
        if question_number >= 10:
            cursor.execute(
                """
                UPDATE interview_links
                SET status = ?
                WHERE token = ?
                """,
                ("completed", token)
            )
            connection.commit()

            return {
                "success": True,
                "completed": True,
                "message": "Mülakat tamamlandı.",
                "question_number": 10,
                "question": None
            }
        next_question_number = question_number + 1

        ai_question = generate_interview_question(
            cv_text=cv_text,
            position=position,
            previous_answers=previous_answers,
            question_number=next_question_number
        )

        return {
            "success": True,
            "message": "Cevap kaydedildi.",
            "question_number": next_question_number,
            "question": ai_question
        }

    finally:
        connection.close()

@app.get("/api/hr/interviews")
def get_hr_interviews(_ik: portal_access.Actor = Depends(require_ai)):
    return candidate_listing(_ik, ai_only=True)


@app.get("/api/ik/candidates")
def get_company_candidates(actor: portal_access.Actor = Depends(require_company)):
    return candidate_listing(actor)


def candidate_listing(_ik: portal_access.Actor, ai_only=False):
    scope, params = recruitment.scope(_ik)
    if ai_only and _ik.portal:
        scope += " AND status IS DISTINCT FROM 'new'"
    connection = get_connection()
    cursor = connection.cursor()

    try:
        cursor.execute(
            f"""
            SELECT
                token,
                name,
                phone,
                email,
                company,
                position,
                status,
                expires_at,
                created_at, position_id, evaluation_status, assigned_user_id
            FROM interview_links
            WHERE {scope}
            ORDER BY token DESC
            """,
            params
        )

        rows = cursor.fetchall()
        for row in rows:
            token = row["token"]
            if row["status"] in ("waiting", "created", "opened") and _link_deadline_passed(dict(row)):
                cursor.execute(
                    "UPDATE interview_links SET status = 'expired' WHERE token = ? AND status IN ('waiting', 'created', 'opened')",
                    (token,)
                )

            cursor.execute(
                """
                SELECT COUNT(DISTINCT question_number) AS answer_count
                FROM interview_answers
                WHERE token = ? AND question_number BETWEEN 1 AND 10
                """,
                (token,)
            )

            answer_count = cursor.fetchone()["answer_count"]

            if answer_count >= 10 and row["status"] in ("started", "completed"):
                cursor.execute(
                    """
                    UPDATE interview_links
                    SET status = ?
                    WHERE token = ? AND NOT EXISTS (SELECT 1 FROM ik_partner_interviews p WHERE p.token=interview_links.token)
                    """,
                    ("completed", token)
                )

        connection.commit()

        cursor.execute(
            f"""
            SELECT
                token,
                name,
                phone,
                email,
                company,
                position,
                status,
                expires_at,
                created_at, position_id, evaluation_status, assigned_user_id
            FROM interview_links
            WHERE {scope}
            ORDER BY token DESC
            """,
            params
        )

        rows = cursor.fetchall()
        interviews = []
        for row in rows:
            item = dict(row)
            item["validity_hours"] = 8
            item["created_at_estimated"] = False
            if not item.get("created_at") and item.get("expires_at"):
                try:
                    expiry = datetime.fromisoformat(item["expires_at"])
                    item["estimated_created_at"] = (expiry - timedelta(hours=8)).isoformat()
                    item["created_at_estimated"] = True
                except (TypeError, ValueError):
                    item["estimated_created_at"] = None
            else:
                item["estimated_created_at"] = None
            interviews.append(item)
        return {"success": True, "interviews": interviews}

    finally:
        connection.close()

@app.post("/api/hr/interviews/{token}/assessment")
def retry_interview_assessment(token: str, actor: portal_access.Actor = Depends(require_manager)):
    require_ai(actor)
    return {"assessment_status": interview_assessments.retry(actor,token)}


@app.get("/api/hr/interviews/{token}")
def get_hr_interview_detail(token: str, _ik: portal_access.Actor = Depends(require_ai)):
    scope, params = recruitment.scope(_ik)
    connection = get_connection()
    cursor = connection.cursor()

    try:
        cursor.execute(
            f"""
            SELECT
                token,
                name,
                phone,
                email,
                company,
                position,
                status,
                expires_at,
                created_at,
                cv_text
            FROM interview_links
            WHERE token = ? AND {scope} AND status IS DISTINCT FROM 'new'
            """,
            (token, *params)
        )

        interview_row = cursor.fetchone()

        if not interview_row:
            raise HTTPException(
                status_code=404,
                detail="Mülakat bulunamadı."
            )

        cursor.execute(
            """
            SELECT
                question_number,
                question,
                answer,
                created_at
            FROM interview_answers
            WHERE token = ?
            ORDER BY question_number ASC
            """,
            (token,)
        )

        answer_rows = cursor.fetchall()
        unique_answers = {}

        for row in answer_rows:
            question_number = row["question_number"]

            if question_number not in unique_answers:
                unique_answers[question_number] = row

        answer_rows = list(unique_answers.values())
        interview_data = dict(interview_row)
        if _ik.portal and (_ik.role == 'hr_staff' or interview_data['status'] != 'completed'):
            return {"success": True, "interview": interview_data,
                    "answers": [dict(row) for row in answer_rows], "job_fit_analysis": ""}

        interview_answers_text = "\n\n".join(
            [
                f"Soru {row['question_number']}: {row['question']}\n"
                f"Cevap: {row['answer']}"
                for row in answer_rows
            ]
        )
        cursor.execute(
            """
            SELECT job_fit_analysis
            FROM interview_reports
            WHERE token = ?
            """,
            (token,)
        )

        saved_report = cursor.fetchone()
        if _ik.portal:
            assessment_status = interview_assessments.status(cursor,_ik.company_id,token,bool(saved_report))
            cursor.execute("SELECT 1 FROM ik_partner_interviews WHERE token=? AND company_id=?", (token,_ik.company_id))
            can_retry = cursor.fetchone() is None and assessment_status in ("not_started","failed")
            return {"success": True, "interview": interview_data, "answers": [dict(row) for row in answer_rows],
                    "job_fit_analysis": saved_report["job_fit_analysis"] if saved_report else "",
                    "assessment_status":assessment_status, "can_retry_assessment":can_retry}
        if saved_report:
            job_fit_analysis = saved_report["job_fit_analysis"]
        else:
            cv_text = interview_data.get("cv_text", "") or ""
            job_fit_analysis = generate_job_fit_analysis(
                cv_text=cv_text,
                position=interview_data.get("position", "") or "",
                interview_answers=interview_answers_text
            )
            cursor.execute(
                """
                INSERT INTO interview_reports
                    (token, job_fit_analysis, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT (token) DO NOTHING
                """,
                (
                    token,
                    job_fit_analysis,
                    datetime.now(timezone.utc).isoformat()
                )
            )

            connection.commit()



        return {
            "success": True,
            "interview": interview_data,
            "answers": [dict(row) for row in answer_rows],
            "job_fit_analysis": job_fit_analysis
        }

    finally:
        connection.close()
@app.get("/api/hr/interviews/{token}/pdf")
def download_interview_pdf(token: str, _ik: portal_access.Actor = Depends(require_ai)):
    if _ik.portal and _ik.role not in portal_access.PDF_ROLES:
        raise HTTPException(403, "PDF raporu için yetkiniz yok.")
    connection = get_connection()
    cursor = connection.cursor()

    try:
        cursor.execute(
            """
            SELECT
                token,
                name,
                company,
                position,
                status,
                application_id
            FROM interview_links
            WHERE token = ? AND company_id IS NOT DISTINCT FROM ? AND status IS DISTINCT FROM 'new'
            """,
            (token, _ik.company_id)
        )

        interview_row = cursor.fetchone()

        if not interview_row:
            raise HTTPException(
                status_code=404,
                detail="Mülakat bulunamadı."
            )
        if _ik.portal and interview_row['status'] != 'completed':
            raise HTTPException(409, "PDF için mülakat tamamlanmış olmalıdır.")

        cursor.execute(
            """
            SELECT
                question_number,
                question,
                answer
            FROM interview_answers
            WHERE token = ?
            ORDER BY question_number ASC
            """,
            (token,)
        )

        answer_rows = cursor.fetchall()

        # Aynı soru numarasının PDF'de tekrar etmesini engelle
        unique_answers = {}
        for row in answer_rows:
            question_number = row["question_number"]
            if question_number not in unique_answers:
                unique_answers[question_number] = row

        answer_rows = list(unique_answers.values())
        interview = dict(interview_row)
        cursor.execute("SELECT job_fit_analysis FROM interview_reports WHERE token = ?", (token,))
        report = cursor.fetchone()
        analysis = report["job_fit_analysis"].strip() if report and report["job_fit_analysis"] else ""
        if analysis == "Sentetik mülakat değerlendirmesi.":
            analysis = ""
        cursor.execute("SELECT language FROM ik_interview_template_snapshots WHERE token=? AND company_id=?",
                       (token,_ik.company_id))
        template = cursor.fetchone()
        english = bool(template and template["language"] == "en")
        labels = ("Candidate Interview Report","Candidate","Company","Position","AI Interview Assessment",
                  "Assessment not performed","Interview Questions and Answers","Question","Answer") if english else (
                  "Aday Mülakat Raporu","Aday","Firma","Pozisyon","AI Mülakat Değerlendirmesi",
                  "Değerlendirme yapılmadı","Mülakat Soruları ve Cevapları","Soru","Cevap")
        title, name_label, company_label, position_label, assessment, absent, answers_title, question, answer = labels
        buffer, document, styles, story = recruitment_pdf.layout(
            title, FRONTEND_DIR / "assets" / "ernasalogo.png",
            portal_access.logo(_ik.portal, _ik.company_id))
        for label, key in ((name_label, "name"), (company_label, "company"), (position_label, "position")):
            story.append(recruitment_pdf.paragraph(f"{label}: {interview.get(key) or '—'}", styles["BodyText"]))
        if "ats" in _ik.products and interview["application_id"]:
            cursor.execute("SELECT document_id FROM ats_interview_cv_sources WHERE token=? AND company_id=?",
                           (token,_ik.company_id))
            source = cursor.fetchone()
            if source:
                try:
                    cv = ats_matching.evidence_match(UPLOAD_DIRECTORY,_ik,interview["application_id"],source["document_id"])
                except HTTPException as error:
                    if error.status_code != 404:
                        raise
                    message = ("CV match unavailable: the linked document could not be found." if english else "CV eşleşmesi gösterilemedi: bağlı belge bulunamadı.")
                else:
                    label = "CV–position match score" if english else "CV–pozisyon eşleşme puanı"
                    value = f'%{cv["match_score"]:g}' if cv["match_score"] is not None else (
                        "No score" if english else "Puan oluşmadı")
                    message = f"{label}: {value}"
                story.append(recruitment_pdf.paragraph(message, styles["BodyText"]))
        story.append(recruitment_pdf.paragraph(assessment, styles["Heading2"]))
        if _ik.portal:
            state = interview_assessments.status(cursor,_ik.company_id,token,bool(report))
            assessment_labels = {
                "not_started": ("Başlatılmadı","Not started"),
                "queued": ("Sırada","Queued"),
                "running": ("Devam ediyor","In progress"),
                "failed": ("Başarısız · İK panelinden yeniden denenebilir","Failed · HR can retry"),
                "completed": ("Tamamlandı","Completed"),
            }
            label = "Assessment status" if english else "Değerlendirme durumu"
            story.append(recruitment_pdf.paragraph(f"{label}: {assessment_labels[state][int(english)]}",styles["BodyText"]))
        for line in (analysis or absent).splitlines():
            if line.strip():
                story.append(recruitment_pdf.paragraph(line.strip(), styles["BodyText"]))
        story.append(Spacer(1, 10))
        note = portal_interviews.introduction_note(cursor,token,_ik.company_id)
        if note:
            story.append(recruitment_pdf.paragraph("Tanışma notu",styles["Heading2"]))
            story.append(recruitment_pdf.paragraph(note,styles["BodyText"]))
        story.append(recruitment_pdf.paragraph(answers_title, styles["Heading2"]))
        for row in answer_rows:
            story.append(recruitment_pdf.paragraph(f"{question} {row['question_number']}: {row['question']}", styles["BodyText"]))
            story.append(recruitment_pdf.paragraph(f"{answer}: {row['answer']}", styles["BodyText"]))
            story.append(Spacer(1, 3))

        document.build(story)

        buffer.seek(0)

        filename = f"ERNASA_{token}_Mulakat_Raporu.pdf"

        return StreamingResponse(
            buffer,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
        )

    finally:
        connection.close()
