"""Evidence-only CV comparison for the ATS subscription."""
import json
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


def cv_lines(path):
    if path.suffix.lower()==".pdf":
        from pymupdf import open as open_pdf
        with open_pdf(path) as pdf:
            pages = [(index,page.get_text()) for index,page in enumerate(pdf,1) if index <= 60]
        if any(text.strip() for _,text in pages):
            return [(line,index) for index,text in pages for line in relevant_text(text).splitlines() if line.strip()]
    return [(line,None) for line in relevant_text(extract_text(str(path))).splitlines() if line.strip()]


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


def evidence_match(root,actor,app_id,document_id):
    source = "original_document"
    try:
        path, _, _ = get_document(root,actor,app_id,document_id)
        try:
            lines = cv_lines(path)
        except Exception:
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
    result["original_file_available"] = source == "original_document"
    if not lines:
        result["reason"] = "CV metni okunamadı."
    elif result["match_score"] is None:
        result["reason"] = "Hiçbir kriter için CV’de kanıt bulunamadı."
    return result
