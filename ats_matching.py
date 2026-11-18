"""Evidence-only CV comparison for the ATS subscription."""
import json
import os
import re
import unicodedata
from contextlib import closing

from fastapi import HTTPException

from ats import application, get_document
from database import get_connection
from services.cv_service import extract_text


PROTECTED = re.compile(
    r"\b(?:yas|cinsiyet|dogum|fotograf|etnik|din|inanc|mezhep|saglik|engel|engellilik|medeni|hamile|"
    r"age|gender|birth|photo|ethnicity|race|religion|health|disability|marital|pregnancy)\b", re.I
)
HEADINGS = {
    'skills': ('beceriler','yetkinlikler','skills'),
    'experience': ('deneyim','is deneyimi','experience','work experience'),
    'education': ('egitim','education'),
    'certifications': ('sertifikalar','sertifika','certifications','certificates'),
}


def plain(value):
    return ''.join(char for char in unicodedata.normalize('NFKD',value.casefold().replace('ı','i'))
                   if not unicodedata.combining(char))


def safe_line(value):
    return not PROTECTED.search(plain(value)) and '@' not in value


def relevant_text(value):
    return '\n'.join(line for line in value[:80000].splitlines() if safe_line(line))


def stored_cv_text(actor, app_id, document_id):
    """Read ONLY verified CV text linked to this exact ATS document and tenant.

    The interview's extracted text can support an evidence comparison when an
    ephemeral original PDF was lost; it is NOT a replacement for the PDF.
    """
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        application(cursor, actor, app_id)
        cursor.execute("""SELECT 1 FROM ats_documents
            WHERE id=? AND company_id=? AND application_id=?""",
            (document_id, actor.company_id, app_id))
        if not cursor.fetchone():
            raise HTTPException(404, "Belge kaydı bulunamadı.")
        cursor.execute("""SELECT i.cv_text FROM ats_interview_cv_sources s
            JOIN interview_links i ON i.token=s.token AND i.company_id=s.company_id
            WHERE s.document_id=? AND s.company_id=? AND i.application_id=?
                AND NULLIF(TRIM(i.cv_text),'') IS NOT NULL
            ORDER BY i.created_at DESC LIMIT 1""",
            (document_id, actor.company_id, app_id))
        row = cursor.fetchone()
    return relevant_text(row["cv_text"]) if row else ""


def source_text(root,actor,app_id,document_id):
    try:
        path, _, _ = get_document(root,actor,app_id,document_id)
    except HTTPException as error:
        if error.status_code != 404:
            raise
        saved = stored_cv_text(actor,app_id,document_id)
        if saved:
            return saved
        raise
    try:
        if path.suffix.lower() == ".pdf":
            lines = cv_lines(path)
            return relevant_text("\n".join(line for line, _ in lines))
        result = extract_text(str(path))
    except (OSError,ValueError,RuntimeError):
        raise HTTPException(400, 'CV metni okunamadı.') from None
    return relevant_text(result)


CATEGORIES = ("Yetkinlik", "Mesleki deneyim", "Eğitim", "Sertifika")
MISSING = "CV’de kanıt bulunamadı"


def parse_criteria(value):
    rows = []
    for line in value.splitlines():
        if not line.strip():
            continue
        columns = [item.strip() for item in line.split("|")]
        if len(columns) != 4:
            raise HTTPException(400, "Kriter biçimi: kategori | kriter | ağırlık | kanıt ifadeleri.")
        category, label, weight, indicators = columns
        signals = list(dict.fromkeys(" ".join(plain(item.strip()).split()) for item in indicators.split(",") if item.strip()))
        if (category not in CATEGORIES or not 2 <= len(label) <= 120 or not re.fullmatch(r"[1-9]\d?|100",weight)
                or not 1 <= len(signals) <= 8 or not safe_line(label)
                or any(not 2 <= len(term) <= 100 or not safe_line(term) for term in signals)):
            raise HTTPException(400, "İşle ilgili kriter, 1–100 ağırlık ve kanıt ifadeleri girin.")
        rows.append((category,label,int(weight),signals))
    if len(rows) > 30:
        raise HTTPException(400,"En fazla 30 eşleşme kriteri girin.")
    return rows


def _pdf_text_lines(path):
    """Return page-aware native PDF text without any external data transfer."""
    from pymupdf import open as open_pdf
    lines = []
    with open_pdf(path) as pdf:
        for number, page in enumerate(pdf, 1):
            if number > 60:
                break
            lines.extend((line, number) for line in relevant_text(page.get_text()).splitlines() if line.strip())
    return lines


def _ocr_pdf_lines(path):
    """Offline OCR of image-based PDF, bounded for Render's small CPU runtime."""
    from rapidocr import ModelType, RapidOCR
    from pymupdf import Matrix, open as open_pdf
    import numpy as np
    from PIL import Image

    engine = RapidOCR(params={
        "Det.model_type": ModelType.TINY,
        "Rec.model_type": ModelType.TINY,
    })
    found = []
    with open_pdf(path) as pdf:
        for number, page in enumerate(pdf, 1):
            if number > 3:
                break
            pix = page.get_pixmap(matrix=Matrix(1.3, 1.3), alpha=False)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            response = engine(np.asarray(image))
            for text in (response.txts or ()):
                if isinstance(text, str):
                    filtered = relevant_text(text)
                    if filtered.strip():
                        found.append((filtered.strip(), number))
    return found


def cv_lines(path):
    if path.suffix.lower() == ".pdf":
        native = _pdf_text_lines(path)
        if len(" ".join(line for line, _ in native)) >= 100:
            return native
        # PDF files with nonstandard text encoding can sometimes be read by
        # pdfplumber even when PyMuPDF returns an empty text layer.
        try:
            import pdfplumber
            secondary = []
            with pdfplumber.open(path) as pdf:
                for number, page in enumerate(pdf.pages, 1):
                    if number > 60:
                        break
                    secondary.extend((line, number) for line in
                                     relevant_text(page.extract_text() or "").splitlines() if line.strip())
            if len(" ".join(line for line, _ in secondary)) > len(" ".join(line for line, _ in native)):
                native = secondary
        except (OSError, ValueError, RuntimeError):
            pass
        if len(" ".join(line for line, _ in native)) >= 100:
            return native
        # On the 512 MB Render instance, ONNX OCR caused memory-limit
        # restarts. Opt in ONLY on a separately capacity-tested larger host.
        # Otherwise return an explicit, safe manual-text fallback to the UI.
        if os.getenv("ERNASA_ATS_ENABLE_LOCAL_OCR") == "1":
            try:
                ocr_lines = _ocr_pdf_lines(path)
                if len(" ".join(line for line, _ in ocr_lines)) > len(" ".join(line for line, _ in native)):
                    return ocr_lines
            except (ImportError, OSError, RuntimeError, ValueError, MemoryError) as error:
                import logging
                logging.getLogger(__name__).warning(
                    "ATS PDF OCR failed: %s", type(error).__name__)
        return native
    return [(line, None) for line in relevant_text(extract_text(str(path))).splitlines() if line.strip()]


def compare(criteria, lines):
    normalized = [(line,page," ".join(plain(line).split())) for line,page in lines if safe_line(line)]
    results = []
    weights = sum(row[2] for row in criteria)
    known_weight = points = 0
    for category,label,weight,signals in criteria:
        confirmed = 0
        denied = False
        evidence = []
        for signal in signals:
            pattern = r"(?<!\w)"+re.escape(signal)+r"(?!\w)"
            negative = re.compile(r"(?:\b(?:no|not|never)\s+"+pattern+
                r"|"+pattern+r"\s+(?:deneyimim\s+|bilgim\s+|sertifikam\s+)?(?:yok|bilmiyorum|kullanmadim|degil)\b)")
            matches = [(bool(negative.search(normal)),line,page) for line,page,normal in normalized if re.search(pattern,normal)]
            if not matches:
                continue
            opposite = next((item for item in matches if item[0]),None)
            positive = next((item for item in matches if not item[0]),None)
            denied = denied or opposite is not None
            confirmed += int(positive is not None and opposite is None)
            for match in (positive,opposite):
                if match is not None:
                    entry = {"text":match[1][:300],"page":match[2]}
                    if entry not in evidence:
                        evidence.append(entry)
        score = None
        if denied:
            score,status = 0,"Eşleşmedi"
        elif confirmed == len(signals):
            score,status = 1,"Eşleşti"
        elif confirmed:
            score,status = confirmed/len(signals),"Kısmi eşleşme"
        else:
            status = MISSING
        if score is not None:
            known_weight += weight
            points += weight*score
        results.append({"category":category,"requirement":label,"weight":weight,"status":status,
                        "evidence_items":evidence,"evidence":" / ".join(item["text"] for item in evidence) or None})
    return {"requirements":results,"match_score":round(100*points/known_weight,1) if known_weight else None,
            "evidence_coverage":round(100*known_weight/weights,1) if weights else None,
            "human_review_required":True}


def evidence_match(root,actor,app_id,document_id,override_text=None):
    source = "original_document"
    if override_text is not None:
        # Authenticated, scoped PDF lookup: manual text is NOT a new CV and
        # cannot be used to guess or replace a different candidate's document.
        get_document(root,actor,app_id,document_id)
        lines = [(line,None) for line in relevant_text(override_text).splitlines() if line.strip()]
        if not lines:
            raise HTTPException(422,"Girdiğiniz metinde analiz edilebilir iş deneyimi veya yetkinlik bilgisi bulunamadı.")
        source = "reviewer_transcribed_cv_text"
    else:
        try:
            path, _, _ = get_document(root,actor,app_id,document_id)
            try:
                lines = cv_lines(path)
            except (OSError,ValueError,RuntimeError):
                lines = []
        except HTTPException as error:
            if error.status_code != 404:
                raise
            saved = stored_cv_text(actor,app_id,document_id)
            if not saved:
                raise  # Keep the missing-file error, never invent a CV.
            lines = [(line,None) for line in saved.splitlines() if line.strip()]
            source = "stored_interview_cv_text"
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        row = application(cursor,actor,app_id)
        cursor.execute("""SELECT fields FROM ats_position_profiles WHERE company_id=? AND position_id=?""",
                       (actor.company_id,row["position_id"]))
        position = cursor.fetchone()
    profile = json.loads(position["fields"] or "{}") if position else {}
    criteria = parse_criteria(profile.get("matching_criteria",""))
    if not criteria:
        return dict(compare([],[]),reason="Pozisyon için ağırlıklı kriter tanımlanmadı.",
                    source=source,original_file_available=(source=="original_document"))
    result = compare(criteria,lines)
    result["source"] = source
    result["original_file_available"] = source in ("original_document","reviewer_transcribed_cv_text")
    result["manual_verification_required"] = source == "reviewer_transcribed_cv_text"
    if not lines:
        result["reason"] = "PDF dosyasının metin katmanı okunamadı. Belge taranmış veya özel kodlamalı olabilir; OCR gerekli."
        result["text_extraction_status"] = "unreadable"
    elif result["match_score"] is None:
        result["reason"] = "Okunabilen CV metninde kriterlere ilişkin kanıt bulunamadı."
        result["text_extraction_status"] = "readable_no_evidence"
    else:
        result["text_extraction_status"] = "readable"
    return result
