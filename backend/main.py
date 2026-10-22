from services import parser
from pathlib import Path
from uuid import uuid4
from services.cv_service import extract_text
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, File, HTTPException, UploadFile,Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse,RedirectResponse,JSONResponse
from database import initialize_database, get_connection
from services.candidate_service import create_interview_identity
from fastapi.staticfiles import StaticFiles
from services.ai_service import generate_interview_question, generate_job_fit_analysis
import os
import secrets
import hmac
import hashlib
from fastapi import Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi import Request
from fastapi.responses import StreamingResponse
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer,Image
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
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
        return response


app = FastAPI(
    title="ERNASA API",
    description="Yapay Zekâ Destekli İnsan Kaynakları Asistanı",
    version="1.0.0"
)
app.add_middleware(SecurityHeadersMiddleware)
security = HTTPBasic()
IK_SESSION_SECRET = os.getenv("IK_SESSION_SECRET", "")

from shield_sessions import (
    create_ik_session,
    verify_ik_session,
    revoke_ik_session
)
from shield_rate_limit import get_login_identity, hash_login_identity, check_login_limit, register_failed_login_atomically


def require_ik_session(request: Request) -> str:
    """Require a valid HR session and protect state-changing requests."""
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

        if origin not in allowed_origins:
            raise HTTPException(
                status_code=403,
                detail="Guvenlik kontrolu: Gecersiz istek kaynagi."
            )

    return "authenticated"



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

UPLOAD_DIRECTORY = Path("uploads")
UPLOAD_DIRECTORY.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx"}
MAX_FILE_SIZE = 10 * 1024 * 1024

@app.get("/ik-giris")
def open_hr_login():
    return FileResponse(FRONTEND_DIR / "login.html")

@app.post("/api/ik-login")
def ik_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):
    allowed_origins = {
        "https://ernasa.com",
        "https://www.ernasa.com",
        "http://127.0.0.1:8081",
        "http://localhost:8081",
    }

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


@app.get("/ik")
def open_hr_panel(request: Request):
    session = request.cookies.get("ernasa_ik_session")

    if not verify_ik_session(session):
        return RedirectResponse(url="/ik-giris", status_code=302)

    return RedirectResponse(url="/ik-v5", status_code=302)
@app.get("/ik-v5")
def open_hr_panel_v5(request: Request):
    session = request.cookies.get("ernasa_ik_session")

    if not verify_ik_session(session):
        return RedirectResponse(
            url="/ik-giris",
            status_code=302
        )

    return FileResponse(FRONTEND_DIR / "ernasa_v5_ik.html")
@app.get("/aday-takip")
def open_candidate_tracking(request: Request):
    session = request.cookies.get("ernasa_ik_session")

    if not verify_ik_session(session):
        return RedirectResponse(url="/ik-giris", status_code=302)

    return FileResponse(FRONTEND_DIR / "aday-takip.html")



@app.get("/")
def home():
    return RedirectResponse(url="/corporate/", status_code=302)


interview_links = {}

@app.post("/api/cv/upload")
async def upload_cv(cv: UploadFile = File(...), _ik: str = Depends(require_ik_session)):
    original_name = cv.filename or "cv"

    extension = Path(original_name).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Yalnızca PDF, DOC veya DOCX dosyaları yüklenebilir."
        )

    content = await cv.read()

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
    target_path = UPLOAD_DIRECTORY / safe_name

    target_path.write_bytes(content)

    text = extract_text(str(target_path))

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
def create_interview_link(candidate: dict, _ik: str = Depends(require_ik_session)):
    identity = create_interview_identity()

    candidate_number = identity["candidate_number"]
    security_code = identity["security_code"]
    interview_id = secrets.token_urlsafe(32)

    created_at = datetime.now(timezone.utc)
    expires_at = created_at + timedelta(hours=8)

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        """
        INSERT INTO interview_links (
            token,
            name,
            phone,
            email,
            company,
            position,
            expires_at,
            created_at,
            status,
            cv_text
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            interview_id,
            candidate.get("name", ""),
            candidate.get("phone", ""),
            candidate.get("email", ""),
            candidate.get("company", ""),
            candidate.get("position", ""),
            expires_at.isoformat(),
            created_at.isoformat(),
            "waiting",
            candidate.get("cv_text", "")
        )
    )

    connection.commit()
    connection.close()

    return {
        "success": True,
        "candidate_number": candidate_number,
        "security_code": security_code,
        "interview_id": interview_id,
        "expires_at": expires_at.isoformat(),
        "created_at": created_at.isoformat(),
        "validity_hours": 8,
        "status": "waiting",
        "interview_url":
            f"{'http://127.0.0.1:8081' if os.getenv('RENDER') is None else 'https://ernasa.com'}/interview/{interview_id}"
    }
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
def get_interview_link(token: str):
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT * FROM interview_links WHERE token = ?", (token,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Mülakat bağlantısı bulunamadı.")
        interview = dict(row)
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
                "can_start": False,
            }
        return {
            "success": True,
            "candidate": {key: interview.get(key) for key in ("name", "company", "position")},
            "expires_at": interview["expires_at"],
            "status": status,
        }
    finally:
        connection.close()


@app.get("/interview/{token}")
def open_interview(token: str):
    # The candidate UI validates the token via /api/interview-links/{token}.
    return FileResponse(FRONTEND_DIR / "index.html")


@app.post("/api/interviews/{token}/opened")
def mark_interview_opened(token: str):
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT * FROM interview_links WHERE token = ?", (token,))
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
def start_interview(token: str):
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT * FROM interview_links WHERE token = ?", (token,))
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
def submit_interview_answer(token: str, payload: dict):
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
            WHERE token = ?
            """,
            (token,)
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
def get_hr_interviews(_ik: str = Depends(require_ik_session)):
    connection = get_connection()
    cursor = connection.cursor()

    try:
        cursor.execute(
            """
            SELECT
                token,
                name,
                phone,
                email,
                company,
                position,
                status,
                expires_at,
                created_at
            FROM interview_links
            ORDER BY token DESC
            """
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
                SELECT COUNT(*) AS answer_count
                FROM interview_answers
                WHERE token = ?
                """,
                (token,)
            )

            answer_count = cursor.fetchone()["answer_count"]

            if answer_count >= 10 and row["status"] in ("started", "completed"):
                cursor.execute(
                    """
                    UPDATE interview_links
                    SET status = ?
                    WHERE token = ?
                    """,
                    ("completed", token)
                )

        connection.commit()

        cursor.execute(
            """
            SELECT
                token,
                name,
                phone,
                email,
                company,
                position,
                status,
                expires_at,
                created_at
            FROM interview_links
            ORDER BY token DESC
            """
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

@app.get("/api/hr/interviews/{token}")
def get_hr_interview_detail(token: str, _ik: str = Depends(require_ik_session)):
    connection = get_connection()
    cursor = connection.cursor()

    try:
        cursor.execute(
            """
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
            WHERE token = ?
            """,
            (token,)
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
        if saved_report:
            job_fit_analysis = saved_report["job_fit_analysis"]
        else:
            job_fit_analysis = generate_job_fit_analysis(
                cv_text=interview_data.get("cv_text", "") or "",
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
def download_interview_pdf(token: str, _ik: str = Depends(require_ik_session)):
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
                cv_text
            FROM interview_links
            WHERE token = ?
            """,
            (token,)
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
        interview_answers_text = "\n\n".join(
            [
                f"Soru {row['question_number']}: {row['question']}\n"
                f"Cevap: {row['answer']}"
                for row in answer_rows
            ]
        )

        job_fit_analysis = generate_job_fit_analysis(
            cv_text=interview.get("cv_text") or "",
            position=interview.get("position") or "",
            interview_answers=interview_answers_text
        )

        buffer = BytesIO()

        document = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            rightMargin=40,
            leftMargin=40,
            topMargin=40,
            bottomMargin=40
        )

        styles = getSampleStyleSheet()

        pdfmetrics.registerFont(
            TTFont("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        )

        styles["Title"].fontName = "DejaVuSans"
        styles["Heading2"].fontName = "DejaVuSans"
        styles["BodyText"].fontName = "DejaVuSans"
        styles["Normal"].fontName = "DejaVuSans"
        story = []

        logo_path = FRONTEND_DIR / "assets" / "ernasalogo.png"

        if logo_path.exists():
            logo = Image(str(logo_path), width=90, height=90)
            logo.hAlign = "CENTER"
            story.append(logo)
            story.append(Spacer(1, 10))

        story.append(Paragraph("ERNASA - Aday Mülakat Raporu", styles["Title"]))
        story.append(Spacer(1, 18))

        story.append(
            Paragraph(
                f"<b>Aday:</b> {interview.get('name') or '-'}",
                styles["Normal"]
            )
        )
        story.append(
            Paragraph(
                f"<b>Aday No:</b> {interview.get('token') or '-'}",
                styles["Normal"]
            )
        )
        story.append(
            Paragraph(
                f"<b>Firma:</b> {interview.get('company') or '-'}",
                styles["Normal"]
            )
        )
        story.append(
            Paragraph(
                f"<b>Pozisyon:</b> {interview.get('position') or '-'}",
                styles["Normal"]
            )
        )

        story.append(Spacer(1, 20))
        story.append(
            Paragraph(
                "AI Pozisyon Uyum Değerlendirmesi",
                styles["Heading2"]
            )
        )
        story.append(Spacer(1, 10))

        for line in job_fit_analysis.splitlines():
            if line.strip():
                story.append(
                    Paragraph(
                        line.strip(),
                        styles["Normal"]
                    )
                )
                story.append(Spacer(1, 5))

        story.append(Spacer(1, 15))
        story.append(Paragraph("Mülakat Soruları ve Cevapları", styles["Heading2"]))
        story.append(Spacer(1, 10))

        for row in answer_rows:
            story.append(
                Paragraph(
                    f"<b>Soru {row['question_number']}:</b> {row['question']}",
                    styles["Normal"]
                )
            )
            story.append(Spacer(1, 5))
            story.append(
                Paragraph(
                    f"<b>Cevap:</b> {row['answer']}",
                    styles["Normal"]
                )
            )
            story.append(Spacer(1, 14))

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
