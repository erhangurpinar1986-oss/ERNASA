from services import parser
from services.cv_service import extract_contact_info, extract_text
from pathlib import Path
from uuid import uuid4
from services.cv_service import extract_contact_info, extract_text

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="ERNASA API",
    description="Yapay Zekâ Destekli İnsan Kaynakları Asistanı",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5500",
        "http://localhost:5500"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

UPLOAD_DIRECTORY = Path("uploads")
UPLOAD_DIRECTORY.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx"}
MAX_FILE_SIZE = 10 * 1024 * 1024


@app.get("/")
def home():
    return {
        "status": "online",
        "project": "ERNASA",
        "message": "ERNASA Backend başarıyla çalışıyor."
    }


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
