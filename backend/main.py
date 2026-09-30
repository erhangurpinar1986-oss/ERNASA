from services import parser
from pathlib import Path
from uuid import uuid4
from services.cv_service import extract_text
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, File, HTTPException, UploadFile,Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from database import initialize_database, get_connection
from services.candidate_service import create_interview_identity
from fastapi.staticfiles import StaticFiles
from services.ai_service import generate_interview_question, generate_job_fit_analysis
import os
import secrets
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


app = FastAPI(
    title="ERNASA API",
    description="Yapay Zekâ Destekli İnsan Kaynakları Asistanı",
    version="1.0.0"
)
security = HTTPBasic()

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
UPLOAD_DIRECTORY = Path("uploads")
UPLOAD_DIRECTORY.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx"}
MAX_FILE_SIZE = 10 * 1024 * 1024

@app.get("/ik-giris")
def open_hr_login():
    return FileResponse(FRONTEND_DIR / "login.html")
@app.post("/api/ik-login")
def ik_login(
    username: str = Form(...),
    password: str = Form(...)
):
    correct_username = os.getenv("IK_USERNAME", "")
    correct_password = os.getenv("IK_PASSWORD", "")

    username_ok = secrets.compare_digest(
        username,
        correct_username
    )

    password_ok = secrets.compare_digest(
        password,
        correct_password
    )

    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=401,
            detail="Kullanıcı adı veya şifre hatalı."
        )

    return {
        "success": True
    }


@app.get("/ik")
def open_hr_panel(
    username: str = Depends(verify_ik_login)
):
    return FileResponse(FRONTEND_DIR / "ik.html")

@app.get("/")
def home():
    return FileResponse(FRONTEND_DIR / "home.html")
    @app.get("/")
    def home():
        return FileResponse(FRONTEND_DIR / "home.html")

interview_links = {}

@app.post("/api/cv/upload")
async def upload_cv(cv: UploadFile = File(...)):
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
    print("\n========== OCR METNİ ==========")
    print(text)
    print("================================\n")

    contact = parser.parse_candidate(text)
    print(contact)

    return {
        "success": True,
        "original_name": original_name,
        "stored_name": safe_name,
        "size": len(content),
        "contact": contact,
        "message": "CV başarıyla yüklendi ve analiz edildi."
    }
@app.post("/api/interview-links")
def create_interview_link(candidate: dict):
    identity = create_interview_identity()

    candidate_number = identity["candidate_number"]
    security_code = identity["security_code"]
    interview_id = identity["interview_id"]

    expires_at = datetime.now(timezone.utc) + timedelta(hours=8)

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
            status,
            cv_text
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?,?)
        """,
        (
            interview_id,
            candidate.get("name", ""),
            candidate.get("phone", ""),
            candidate.get("email", ""),
            candidate.get("company", ""),
            candidate.get("position", ""),
            expires_at.isoformat(),
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
        "status": "waiting",
        "interview_url":
            f"https://ernasa.com/interview/{interview_id}"
    }
@app.get("/api/interview-links/{token}")
def get_interview_link(token: str):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        "SELECT * FROM interview_links WHERE token = %s",
        (token,)
    )

    row = cursor.fetchone()
    connection.close()

    interview = dict(row) if row else None

    if not interview:
        raise HTTPException(
            status_code=404,
            detail="Mülakat bağlantısı bulunamadı."
        )

    expires_at = datetime.fromisoformat(interview["expires_at"])

    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(
            status_code=410,
            detail="Mülakat bağlantısının süresi dolmuş."
        )

    return {
        "success": True,
        "candidate": {
            "name": interview["name"],
            "phone": interview["phone"],
            "email": interview["email"],
            "company": interview["company"],
            "position": interview["position"]
        },
        "expires_at": interview["expires_at"],
        "status": interview["status"]
    }

@app.get("/interview/{token}")
def open_interview(token: str):
    return FileResponse(FRONTEND_DIR / "index.html")
@app.post("/api/interviews/{token}/opened")
def mark_interview_opened(token: str):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        "SELECT status FROM interview_links WHERE token = ?",
        (token,)
    )

    row = cursor.fetchone()

    if not row:
        connection.close()
        raise HTTPException(
            status_code=404,
            detail="Mülakat bağlantısı bulunamadı."
        )

    if row["status"] == "waiting":
        cursor.execute(
            """
            UPDATE interview_links
            SET status = ?
            WHERE token = ?
            """,
            ("opened", token)
        )
        connection.commit()

    connection.close()

    return {
        "success": True
    }
@app.post("/api/interviews/{token}/start")
def start_interview(token: str):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        "SELECT * FROM interview_links WHERE token = ?",
        (token,)
    )

    row = cursor.fetchone()

    if not row:
        connection.close()
        raise HTTPException(
            status_code=404,
            detail="Mülakat bağlantısı bulunamadı."
        )

    interview = dict(row)

    expires_at = datetime.fromisoformat(interview["expires_at"])

    if datetime.now(timezone.utc) > expires_at:
        cursor.execute(
            """
            UPDATE interview_links
            SET status = ?
            WHERE token = ?
            """,
            ("expired", token)
        )
        connection.commit()
        connection.close()

        raise HTTPException(
            status_code=410,
            detail="Mülakat bağlantısının süresi dolmuş."
        )

    cursor.execute(
        """
        UPDATE interview_links
        SET status = ?
        WHERE token = ?
        """,
        ("started", token)
    )

    connection.commit()
    connection.close()

    cv_text = interview.get("cv_text", "") or ""
    position = interview.get("position", "") or ""

    ai_question = (
        "Merhaba, hoş geldiniz. Bu görüşmede sizi ve çalışma deneyimlerinizi "
        "biraz daha yakından tanımaya çalışacağım. Burada doğru ya da yanlış "
        "cevap yok; soruları kendi deneyimlerinize göre rahatça yanıtlayabilirsiniz. "
        "Hazırsanız, öncelikle kendinizden biraz bahseder misiniz?"
    )

    return {
        "success": True,
        "status": "started",
        "question_number": 1,
        "question": ai_question
    }
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
        cv_text = interview.get("cv_text", "") or ""
        position = interview.get("position", "") or ""
        print("MULAKAT CV METNI:", cv_text[:500])



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
def get_hr_interviews():
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
                expires_at
            FROM interview_links
            ORDER BY token DESC
            """
        )

        rows = cursor.fetchall()
        for row in rows:
            token = row["token"]

            cursor.execute(
                """
                SELECT COUNT(*) AS answer_count
                FROM interview_answers
                WHERE token = ?
                """,
                (token,)
            )

            answer_count = cursor.fetchone()["answer_count"]

            if answer_count >= 10 and row["status"] != "completed":
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
                expires_at
            FROM interview_links
            ORDER BY token DESC
            """
        )

        rows = cursor.fetchall()
        return {
            "success": True,
            "interviews": [dict(row) for row in rows]
        }

    finally:
        connection.close()

@app.get("/api/hr/interviews/{token}")
def get_hr_interview_detail(token: str):
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

        job_fit_analysis = generate_job_fit_analysis(
            cv_text=interview_data.get("cv_text", "") or "",
            position=interview_data.get("position", "") or "",
            interview_answers=interview_answers_text
        )

        return {
            "success": True,
            "interview": interview_data,
            "answers": [dict(row) for row in answer_rows],
            "job_fit_analysis": job_fit_analysis
        }

    finally:
        connection.close()
@app.get("/api/hr/interviews/{token}/pdf")
def download_interview_pdf(token: str):
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