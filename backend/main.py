from services import parser
from pathlib import Path
from uuid import uuid4
from services.cv_service import extract_text
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from database import initialize_database, get_connection
from services.candidate_service import create_interview_identity
from fastapi.staticfiles import StaticFiles
from services.ai_service import generate_interview_question

app = FastAPI(
    title="ERNASA API",
    description="Yapay Zekâ Destekli İnsan Kaynakları Asistanı",
    version="1.0.0"
)
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
@app.get("/ik")
def open_hr_panel():
    return FileResponse(FRONTEND_DIR / "ik.html")

@app.get("/")
def home():
    return {
        "status": "online",
        "project": "ERNASA",
        "message": "ERNASA Backend başarıyla çalışıyor."
    }
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

    expires_at = datetime.now(timezone.utc) + timedelta(hours=48)

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
            f"http://127.0.0.1:8000/interview/{interview_id}"
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