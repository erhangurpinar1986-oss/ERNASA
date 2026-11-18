"""ERNASA Academy: tenant-scoped course assignments and local PDF report suggestions.

Candidate reports are parsed in memory; PDF bytes and extracted report text are
never persisted or forwarded to an external model. Training selection is human-led.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import json
import os
import re
import secrets
import unicodedata

import fitz
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import control_integration as portal_access
import ernasa_training
from database import get_connection
from shield_sessions import verify_ik_session

router = APIRouter()
MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_REPORT_PAGES = 12
MODULE_TITLES = {
    "vizyon": "ERNASA'yı doğru konumlandırmak",
    "paneller": "Paneller, roller ve şirket ayrımı",
    "ats": "ATS: başvuru, CV ve aday yönetimi",
    "cv_analiz": "CV analiz sistemi",
    "iki_asama": "İki aşamalı değerlendirme",
    "mulakat": "Dijital mülakat ve davet",
    "rapor": "Yapay zekâ değerlendirmesi ve raporlama",
    "guvenlik": "Gizlilik, erişim ve ürün sınırları",
    "demo": "7 dakikalık ürün tanıtımı",
    "avcilik": "Sıfırdan B2B müşteri kazanımı",
    "kesif": "Danışmanlık temelli müşteri keşfi",
    "crm": "CRM, satış hunisi ve 30/60/90 gün",
    "müzakere": "Fiyat itirazı, ROI ve müzakere",
    "kurumsal": "Kurumsal satın alma ve çok paydaş",
    "almanya": "Almanya pazarı: doğrulama ve açılım",
    "metrik": "Satış hunisi teşhisi ve A/B testleri",
}
PROGRAMS = {
    "b2b_satis": {
        "title": "B2B Satış Ortaklığı",
        "modules": ("vizyon","paneller","ats","cv_analiz","iki_asama","mulakat","rapor","guvenlik","demo","kesif"),
    },
    "ik_urun": {
        "title": "ERNASA İK Ürün Tanıtımı",
        "modules": ("vizyon","paneller","ats","cv_analiz","iki_asama","mulakat","rapor","guvenlik","demo"),
    },
    "ik_personel": {
        "title": "İK Personeli: Ürün ve Değerlendirme",
        "modules": ("paneller","ats","cv_analiz","iki_asama","mulakat","rapor","guvenlik"),
    },
}
# Phrase matching, not a clinical assessment or automatic employment decision.
# Each match is constrained to the report's explicit "development areas" section.
RULES = (
    ("avcilik", ("sıfırdan b2b", "avcılık", "hunting", "müşteri kazanımı", "pipeline oluşturma"), "Sıfırdan kurumsal müşteri kazanımı ve fırsat oluşturma"),
    ("crm", ("30/60/90", "crm", "kpi", "ölçülebilir hedefler", "sekanslar", "pipeline"), "CRM, ölçülebilir plan ve satış hunisi"),
    ("müzakere", ("müzakere", "roi", "give-get", "fiyat itiraz", "fiyat müzakere"), "Fiyat itirazları ve değer temelli müzakere"),
    ("kurumsal", ("kurumsal satın alma", "güvenlik/uyum", "kvkk", "gdpr", "dpi a", "dpia", "iso", "soc/iso"), "Çok paydaşlı satın alma ve güvenlik/uyum"),
    ("almanya", ("almanya", "pazar genişleme", "yerel ortaklar", "veri barındırma"), "Uluslararası pazar doğrulaması"),
    ("metrik", ("demo oranı", "kök neden", "a/b test", "dönüşüm ölçümü", "deneysel iyileştirme"), "Dönüşüm, metrik analizi ve deneysel iyileştirme"),
    ("demo", ("canlı demo", "ürün sunumu", "sunum becerisi"), "Ürün demosu ve sunum becerisi"),
    ("kesif", ("ihtiyaç analizi", "müşteri keşfi"), "Müşteri keşfi ve ihtiyaç analizi"),
)
SECTION_START = re.compile(r"(?m)^\s*(?:GEL[İI]ŞT[İI]R[İI]LMES[İI]\s+GEREKEN\s+(?:ALANLAR|Y[ÖO]NLER)|GEL[İI]Ş[İI]M\s+ALANLARI)\s*:\s*$", re.I)
SECTION_STOP = re.compile(r"(?m)^\s*(?:D[İI]KKAT\s+ED[İI]LMES[İI]\s+GEREKEN|[İI]K\s+DE[ĞG]ERLEND[İI]RME\s+[ÖO]ZET[İI]|M[ÜU]LAKAT\s+SORULARI|G[ÜU][ÇC]L[ÜU]\s+Y[ÖO]NLER)\b",re.I)
ASSIGNMENT_HTML = Path(__file__).resolve().parent / "academy_assign.html"


def initialize_schema():
    with closing(get_connection()) as db:
        cur = db.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS ernasa_training_assignments (
            token_hash TEXT PRIMARY KEY REFERENCES ernasa_training_invites(token_hash),
            company_id BIGINT,
            assigned_by BIGINT,
            program_id TEXT NOT NULL,
            modules_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            video_allowed INTEGER NOT NULL DEFAULT 0
        )""")
        # Additive migration without changing existing invite rows.
        from database import DATABASE_URL
        if DATABASE_URL:
            cur.execute("ALTER TABLE ernasa_training_assignments ADD COLUMN IF NOT EXISTS video_allowed INTEGER NOT NULL DEFAULT 0")
        else:
            cur.execute("PRAGMA table_info(ernasa_training_assignments)")
            if "video_allowed" not in {row["name"] for row in cur.fetchall()}:
                cur.execute("ALTER TABLE ernasa_training_assignments ADD COLUMN video_allowed INTEGER NOT NULL DEFAULT 0")
        cur.execute("CREATE INDEX IF NOT EXISTS ernasa_training_assignments_tenant ON ernasa_training_assignments(company_id,created_at)")
        db.commit()


def _actor(request: Request, write: bool = False):
    portal = portal_access.portal_for(request)
    if portal:
        if write:
            portal_access.check_origin(request,portal)
        actor = portal_access.session_actor(request, portal)
        if not actor:
            raise HTTPException(401, "Geçerli İK personeli oturumu gerekli.")
        if actor.role not in portal_access.STAFF_ROLES:
            raise HTTPException(403, "Eğitim atama yetkisi bulunmuyor.")
        if actor.company_id is None:
            raise HTTPException(403, "Firma kimliği bulunamadı.")
        return actor.company_id, actor.user_id
    if not verify_ik_session(request.cookies.get("ernasa_ik_session", "")):
        raise HTTPException(401, "ERNASA yetkili oturumu gerekli.")
    if write:
        origin = request.headers.get("origin", "")
        allowed = {"https://ernasa.com", "https://www.ernasa.com"}
        if os.getenv("ERNASA_IK_LOCAL_PREVIEW") == "1":
            allowed.update({"http://localhost:8081","http://127.0.0.1:8081"})
        if origin not in allowed:
            raise HTTPException(403, "İstek kaynağı geçersiz.")
    return None, None


def _normalized(text: str):
    text = unicodedata.normalize("NFKC", text).casefold().replace("ı","i").replace("İ","i")
    return unicodedata.normalize("NFKD", text).translate({ord(c):None for c in "\u0300\u0301\u0302\u0303\u0304\u0306\u0307\u0308\u030a\u0327"})


def _development_section(text):
    # Match headings without depending on extraction punctuation (bullet headers on PDF).
    match = SECTION_START.search(text)
    if not match:
        return ""
    part = text[match.end():][:9000]
    stop = SECTION_STOP.search(part)
    return (part[:stop.start()] if stop else part).strip()


def suggest_from_text(text):
    if not isinstance(text, str):
        raise HTTPException(422, "Rapor metni geçersiz.")
    text = text[:35000]
    section = _development_section(text)
    match_name = re.search(r"(?m)^\s*Aday\s*:\s*([^\r\n]{2,120})", text, re.I)
    match_role = re.search(r"(?m)^\s*Pozisyon\s*:\s*([^\r\n]{2,150})", text, re.I)
    norm = _normalized(section)
    results = []
    for module_id, phrases, reason in RULES:
        if any(_normalized(phrase) in norm for phrase in phrases):
            results.append({"module_id":module_id,"title":MODULE_TITLES[module_id],"reason":reason})
    return {
        "candidate_name":match_name.group(1).strip() if match_name else "",
        "position":match_role.group(1).strip() if match_role else "",
        "development_section_found":bool(section),
        "suggestions":results,
        "notice":("Bu öneriler yalnızca rapordaki gelişim alanlarından üretilmiştir; son seçim İK personeline aittir."
                  if section else "Raporda gelişim alanları başlığı bulunamadı. Dersleri personel manuel seçebilir."),
        "report_saved":False,
    }


def parse_pdf_bytes(data: bytes):
    if len(data)>MAX_REPORT_BYTES:
        raise HTTPException(413,"Rapor en fazla 4 MB olabilir.")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(415,"Geçerli bir PDF dosyası yükleyin.")
    try:
        with fitz.open(stream=data,filetype="pdf") as document:
            if document.needs_pass:
                raise HTTPException(422,"Şifreli raporlar okunamıyor.")
            if document.page_count<1 or document.page_count>MAX_REPORT_PAGES:
                raise HTTPException(422,"Rapor 1-12 sayfa olmalıdır.")
            text = "\n".join(page.get_text(sort=True) for page in document)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422,"PDF metni okunamadı.") from None
    if not text.strip():
        raise HTTPException(422,"Tarama görüntüsü içeren PDF okunamıyor; metin içeren rapor yükleyin.")
    return suggest_from_text(text)


class ManualText(BaseModel):
    text: str = Field(min_length=15,max_length=35000)


class AssignmentRequest(BaseModel):
    recipient: str = Field(min_length=2,max_length=120)
    email: str = Field(default="",max_length=254)
    program_id: str = Field(min_length=2,max_length=40)
    module_ids: list[str] = Field(min_length=1,max_length=16)
    days: int = Field(default=14,ge=1,le=30)
    video_allowed: bool = False


def _catalog():
    return {"programs":[{"id":key,"title":val["title"],"default_modules":list(val["modules"])}
                        for key,val in PROGRAMS.items()],
            "modules":[{"id":key,"title":value} for key,value in MODULE_TITLES.items()]}


@router.get("/api/egitim/ik/katalog")
def catalog(request: Request):
    _actor(request)
    return _catalog()


@router.get("/api/egitim/ik/kayitli-raporlar")
def saved_interview_reports(request: Request):
    company_id,_ = _actor(request)
    with closing(get_connection()) as db:
        c=db.cursor()
        if company_id is None:
            c.execute("""SELECT i.token,i.name,i.position,i.created_at
                FROM interview_links i JOIN interview_reports r ON r.token=i.token
                WHERE i.status='completed' AND r.job_fit_analysis IS NOT NULL
                ORDER BY i.created_at DESC LIMIT 100""")
        else:
            c.execute("""SELECT i.token,i.name,i.position,i.created_at
                FROM interview_links i JOIN interview_reports r ON r.token=i.token
                WHERE i.company_id=? AND i.status='completed'
                  AND r.job_fit_analysis IS NOT NULL
                ORDER BY i.created_at DESC LIMIT 100""",(company_id,))
        return {"reports":[dict(row) for row in c.fetchall()]}


@router.get("/api/egitim/ik/kayitli-raporlar/{interview_token}/oneriler")
def saved_report_suggestions(request: Request, interview_token: str):
    company_id,_ = _actor(request)
    if not interview_token or len(interview_token)>128:
        raise HTTPException(404,"Mülakat raporu bulunamadı.")
    with closing(get_connection()) as db:
        c=db.cursor()
        if company_id is None:
            c.execute("""SELECT i.name,i.position,r.job_fit_analysis
                FROM interview_links i JOIN interview_reports r ON r.token=i.token
                WHERE i.token=? AND i.status='completed'""",(interview_token,))
        else:
            c.execute("""SELECT i.name,i.position,r.job_fit_analysis
                FROM interview_links i JOIN interview_reports r ON r.token=i.token
                WHERE i.token=? AND i.company_id=? AND i.status='completed'""",
                (interview_token,company_id))
        row=c.fetchone()
    if not row:
        raise HTTPException(404,"Bu firmaya ait tamamlanmış mülakat raporu bulunamadı.")
    result=suggest_from_text(row["job_fit_analysis"] or "")
    result["candidate_name"]=row["name"] or ""
    result["position"]=row["position"] or ""
    return result


@router.post("/api/egitim/ik/rapor-onerileri")
async def report_suggestions(request: Request, report: UploadFile=File(...)):
    _actor(request, write=True)
    if report.content_type not in ("application/pdf","application/octet-stream"):
        raise HTTPException(415, "Yalnızca PDF raporu destekleniyor.")
    data = await report.read(MAX_REPORT_BYTES+1)
    await report.close()
    return parse_pdf_bytes(data)


@router.post("/api/egitim/ik/metin-onerileri")
def text_suggestions(request: Request, fields: ManualText):
    _actor(request, write=True)
    return suggest_from_text(fields.text)


@router.post("/api/egitim/ik/atama",status_code=201)
def create_assignment(request: Request, body: AssignmentRequest):
    company_id, user_id = _actor(request, write=True)
    if body.program_id not in PROGRAMS:
        raise HTTPException(422,"Eğitim programı geçersiz.")
    if not body.module_ids or len(body.module_ids)!=len(set(body.module_ids)):
        raise HTTPException(422,"Modül seçimi geçersiz.")
    if any(module not in MODULE_TITLES for module in body.module_ids):
        raise HTTPException(422,"Bilinmeyen eğitim modülü.")
    email=body.email.strip().lower()
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+",email):
        raise HTTPException(422,"E-posta adresi geçersiz.")
    base=os.getenv("ERNASA_PUBLIC_URL","https://ernasa.com").rstrip("/")
    if base not in ("https://ernasa.com","https://www.ernasa.com"):
        raise HTTPException(503,"Eğitim bağlantısı alan adı yapılandırılmamış.")
    token=secrets.token_urlsafe(32)
    digest=sha256(token.encode("ascii")).hexdigest()
    now=datetime.now(timezone.utc)
    expires=(now+timedelta(days=body.days)).isoformat()
    modules=sorted(body.module_ids,key=list(MODULE_TITLES).index)
    with closing(get_connection()) as db:
        cur=db.cursor()
        cur.execute("""INSERT INTO ernasa_training_invites
            (token_hash,recipient,email,created_at,expires_at,revoked)
            VALUES(?,?,?,?,?,0)""",
            (digest,body.recipient.strip(),email,now.isoformat(),expires))
        cur.execute("""INSERT INTO ernasa_training_assignments
            (token_hash,company_id,assigned_by,program_id,modules_json,created_at,video_allowed)
            VALUES(?,?,?,?,?,?,?)""",
            (digest,company_id,user_id,body.program_id,json.dumps(modules,ensure_ascii=False),now.isoformat(),int(body.video_allowed)))
        db.commit()
    return {"recipient":body.recipient.strip(),"program_id":body.program_id,
            "module_ids":modules,"video_allowed":body.video_allowed,"expires_at":expires,
            "link":base+"/egitim/"+token,"sent":False}


@router.get("/api/egitim/ik/eposta-durumu")
def training_email_status(request: Request):
    """Expose only whether corporate outbound email is configured; no secrets."""
    _actor(request)
    from services import invite_email
    try:
        _, sender=invite_email.configured_sender()
    except invite_email.EmailConfigurationError:
        return {"configured":False,"sender":None,
                "message":"Kurumsal e-posta servisi yapılandırılmamış. Render'da ERNASA_RESEND_API_KEY ve ERNASA_EMAIL_FROM ayarlarını kontrol edin."}
    return {"configured":True,"sender":sender,
            "message":"ERNASA kurumsal e-posta gönderimi hazır."}


@router.post("/api/egitim/ik/davet/{token}/gonder")
def send_assignment_mail(request: Request, token: str):
    """Explicit send by authenticated HR: reuse existing invite, never reassign."""
    company_id, _ = _actor(request,write=True)
    if not ernasa_training.TOKEN_PATTERN.fullmatch(token):
        raise HTTPException(404,"Eğitim daveti bulunamadı.")
    digest = sha256(token.encode("ascii")).hexdigest()
    with closing(get_connection()) as db:
        c=db.cursor()
        if company_id is None:
            c.execute("""SELECT i.recipient,i.email,i.expires_at,i.revoked
                FROM ernasa_training_assignments a
                JOIN ernasa_training_invites i ON i.token_hash=a.token_hash
                WHERE a.token_hash=? AND a.company_id IS NULL""",(digest,))
        else:
            c.execute("""SELECT i.recipient,i.email,i.expires_at,i.revoked
                FROM ernasa_training_assignments a
                JOIN ernasa_training_invites i ON i.token_hash=a.token_hash
                WHERE a.token_hash=? AND a.company_id=?""",(digest,company_id))
        row=c.fetchone()
    if not row:
        raise HTTPException(404,"Bu firmaya ait eğitim daveti bulunamadı.")
    if row["revoked"] or datetime.fromisoformat(row["expires_at"])<=datetime.now(timezone.utc):
        raise HTTPException(410,"Davetin geçerlilik süresi sona erdi.")
    if not row["email"]:
        raise HTTPException(422,"Davet e-postası belirtilmemiş.")
    from services import invite_email
    url="https://ernasa.com/egitim/"+token
    try:
        provider_id=invite_email.send_training_invitation(
            recipient=row["email"],name=row["recipient"],url=url,
            expires_at=row["expires_at"],
            idempotency_key="ernasa-academy-"+digest[:32]+"-"+secrets.token_hex(8))
    except invite_email.EmailConfigurationError as exc:
        raise HTTPException(503,str(exc)) from None
    except (invite_email.EmailDeliveryError, ValueError) as exc:
        raise HTTPException(502,str(exc)) from None
    return {"sent":True,"provider":"transactional-email","message":"Eğitim daveti e-posta servisine teslim edildi."}


@router.get("/api/egitim/ik/atamalar")
def list_assignments(request: Request):
    company_id,_=_actor(request)
    with closing(get_connection()) as db:
        c=db.cursor()
        if company_id is None:
            c.execute("""SELECT i.recipient,i.created_at,i.expires_at,a.program_id,a.modules_json
                        FROM ernasa_training_assignments a JOIN ernasa_training_invites i
                        ON i.token_hash=a.token_hash WHERE a.company_id IS NULL
                        ORDER BY i.created_at DESC LIMIT 50""")
        else:
            c.execute("""SELECT i.recipient,i.created_at,i.expires_at,a.program_id,a.modules_json
                        FROM ernasa_training_assignments a JOIN ernasa_training_invites i
                        ON i.token_hash=a.token_hash WHERE a.company_id=?
                        ORDER BY i.created_at DESC LIMIT 50""",(company_id,))
        return {"assignments":[{"recipient":x["recipient"],"program_id":x["program_id"],
                 "module_ids":json.loads(x["modules_json"]),
                 "created_at":x["created_at"],"expires_at":x["expires_at"]} for x in c.fetchall()]}


def assigned_modules(token):
    with closing(get_connection()) as db:
        digest,_=ernasa_training._invite(db,token)
        cur=db.cursor()
        cur.execute("SELECT modules_json,program_id,video_allowed FROM ernasa_training_assignments WHERE token_hash=?", (digest,))
        row=cur.fetchone()
        if not row:  # Old training links remain functional.
            return {"program_id":"legacy","module_ids":list(MODULE_TITLES),"video_allowed":False}
        return {"program_id":row["program_id"],
                "module_ids":json.loads(row["modules_json"]),
                "video_allowed":bool(row["video_allowed"])}


@router.get("/api/egitim/{token}/program")
def participant_program(token: str):
    return assigned_modules(token)


@router.get("/egitim-atama",include_in_schema=False)
def assignment_page(request: Request):
    _actor(request)
    if not ASSIGNMENT_HTML.is_file():
        raise HTTPException(503,"Eğitim atama ekranı bulunamadı.")
    return HTMLResponse(ASSIGNMENT_HTML.read_text("utf-8"),headers={
        "Cache-Control":"private, no-store","X-Robots-Tag":"noindex, nofollow",
        "Referrer-Policy":"no-referrer"
    })
