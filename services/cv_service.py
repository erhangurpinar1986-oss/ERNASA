import io
import os
import re
from pathlib import Path
from services.parser import extract_fallback_name, extract_labeled_name
import pdfplumber
import pytesseract
import pymupdf

from PIL import Image
from docx import Document

print(">>>> YENİ CV_SERVICE ÇALIŞTI <<<<")

windows_tesseract = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
if os.name == "nt" and windows_tesseract.is_file():
    pytesseract.pytesseract.tesseract_cmd = str(windows_tesseract)

def extract_text(file_path: str) -> str:
    path = Path(file_path)
    extension = path.suffix.lower()

    if extension == ".pdf":
        text_parts = []

        # Önce PDF'nin normal metin katmanını okumayı dener.
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                text_parts.append(page_text)

        normal_text = "\n".join(text_parts).strip()

        # Yeterli metin bulunduysa OCR çalıştırılmaz.
        if len(normal_text) >= 50:
            return normal_text

        # Metin okunamadıysa PDF sayfalarına OCR uygulanır.
        ocr_parts = []
        document = pymupdf.open(path)

        try:
            languages = pytesseract.get_languages()
            language = 'tur+eng' if 'tur' in languages and 'eng' in languages else (
                'tur' if 'tur' in languages else 'eng')
            for page in document:
                matrix = pymupdf.Matrix(2.5, 2.5)

                pixmap = page.get_pixmap(
                    matrix=matrix,
                    alpha=False
                )

                image_bytes = pixmap.tobytes("png")
                image = Image.open(io.BytesIO(image_bytes))

                page_text = pytesseract.image_to_string(
                    image,
                    lang=language,
                    config="--psm 6",
                    timeout=20
                )

                ocr_parts.append(page_text)

        except (OSError, RuntimeError, pytesseract.TesseractError):
            if normal_text:
                return normal_text
            raise
        finally:
            document.close()

        return "\n".join(ocr_parts).strip() or normal_text

    if extension == ".docx":
        document = Document(path)

        return "\n".join(
            paragraph.text
            for paragraph in document.paragraphs
        ).strip()

    raise ValueError("Desteklenmeyen dosya türü.")

def extract_contact_info(text: str) -> dict:
    email_match = re.search(
        r"[\w.+-]+@[\w.-]+\.\w+",
        text,
        re.IGNORECASE
    )

    phone_matches = {}
    for match in re.finditer(
        r"(?<![\w+])(?:(?:\+90|0090|90|0)[\s.\-]*)?"
        r"(?:\(5[0-9]{2}\)|5[0-9]{2})[\s.\-]*[0-9]{3}"
        r"[\s.\-]*[0-9]{2}[\s.\-]*[0-9]{2}(?!\w)",
        text,
    ):
        digits = re.sub(r"[^0-9]", "", match.group(0))
        phone_matches.setdefault(digits[-10:], match)
    phone_match = next(iter(phone_matches.values())) if len(phone_matches) == 1 else None

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    candidate_name = extract_labeled_name(lines) or extract_fallback_name(lines)

    return {
        "name": candidate_name,
        "email": email_match.group(0) if email_match else "",
        "phone": phone_match.group(0).strip() if phone_match else ""
    }
