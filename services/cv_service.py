import io
import re
from pathlib import Path
from services.parser import parse_candidate
import pdfplumber
import pytesseract
import pymupdf

from PIL import Image
from docx import Document

print(">>>> YENİ CV_SERVICE ÇALIŞTI <<<<")

pytesseract.pytesseract.tesseract_cmd = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe"
)

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
                    lang="eng",
                    config="--psm 6"
                )

                ocr_parts.append(page_text)

        finally:
            document.close()

        return "\n".join(ocr_parts).strip()

    if extension == ".docx":
        document = Document(path)

        return "\n".join(
            paragraph.text
            for paragraph in document.paragraphs
        ).strip()

    raise ValueError("Desteklenmeyen dosya türü.")

    raise ValueError("Desteklenmeyen dosya türü.")


def extract_contact_info(text: str) -> dict:
    import re

    email_match = re.search(
        r"[\w\.-]+@[\w\.-]+\.\w+",
        text,
        re.IGNORECASE
    )

    phone_match = re.search(
        r"(?:\+90|0)?\s*5\d{2}[\s\.-]?\d{3}[\s\.-]?\d{2}[\s\.-]?\d{2}",
        text
    )

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    candidate_name = ""

    # 1. Önce isim etiketlerini ara
    labels = [
        "isim",
        "ad soyad",
        "ad-soyad",
        "name"
    ]

    for line in lines:

        clean = " ".join(line.split())

        # OCR karakterlerini temizle
        clean = clean.replace("●", "")
        clean = clean.replace("•", "")
        clean = clean.replace("◉", "")
        clean = clean.replace("○", "")
        clean = clean.strip()

        lower = clean.lower()

        for label in labels:

            if label in lower and ":" in clean:

                value = clean.split(":", 1)[1].strip()

                value = re.sub(r"\s+", " ", value)

                if len(value) >= 3:
                    candidate_name = value.title()

                    return {
                        "name": candidate_name,
                        "email": email_match.group(0) if email_match else "",
                        "phone": phone_match.group(0) if phone_match else ""
                    }

    # 2. Başlıkları ele

    ignored = {
        "kişisel bilgiler",
        "kişisel bilgiler:",
        "iletişim",
        "özgeçmiş",
        "cv",
        "profil",
        "hakkımda",
        "eğitim",
        "deneyim",
        "iş deneyimi",
        "beceriler",
        "yetenekler",
        "referanslar"
    }

    for line in lines[:20]:

        clean = " ".join(line.split())

        clean = clean.replace("●", "")
        clean = clean.replace("•", "")
        clean = clean.replace("◉", "")
        clean = clean.replace("○", "")
        clean = clean.strip()

        if clean.lower() in ignored:
            continue

        if ":" in clean:
            continue

        if "@" in clean:
            continue

        if any(ch.isdigit() for ch in clean):
            continue

        words = clean.split()

        if 2 <= len(words) <= 4:

            if all(
                word.replace("-", "").isalpha()
                for word in words
            ):
                candidate_name = clean.title()
                break

    print("ADAY İSİM =", candidate_name)


    return {
        "name": candidate_name,
        "email": email_match.group(0) if email_match else "",
        "phone": phone_match.group(0) if phone_match else ""
    }