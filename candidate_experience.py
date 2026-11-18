"""Voluntary, pseudonymous candidate-experience feedback, isolated from hiring scores.

No candidate name, email, address, report, interview responses, or raw access token
is stored in the survey table. One submission per invitation (HMAC dedupe).
Only aggregated ratings and a limited selection of sanitized comments may be
accessed by the authenticated CONTROL founder.
"""
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
from pathlib import Path
from urllib.parse import urlencode
import hmac
import os
import re
import secrets
import statistics

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from database import get_connection
import ernasa_training

router = APIRouter()
TYPES = {"egitim", "mulakat"}
RATING_LABELS = ("Kullanım kolaylığı","Soruların açıklığı","Dijital mülakat deneyimi",
                 "Eğitim modüllerinin faydası","Genel memnuniyet")
COLUMNS = ("ease","clarity","interview","training","overall")
SALT_ENV = ("ERNASA_SURVEY_KEY","IK_SESSION_SECRET","ERNASA_IK_CONTROL_KEY")


def initialize_schema():
    with closing(get_connection()) as db:
        db.cursor().execute("""CREATE TABLE IF NOT EXISTS ernasa_candidate_experience (
            response_key TEXT PRIMARY KEY,
            source TEXT NOT NULL CHECK(source IN ('egitim','mulakat')),
            ease INTEGER NOT NULL CHECK(ease BETWEEN 1 AND 5),
            clarity INTEGER NOT NULL CHECK(clarity BETWEEN 1 AND 5),
            interview INTEGER CHECK(interview BETWEEN 1 AND 5),
            training INTEGER CHECK(training BETWEEN 1 AND 5),
            overall INTEGER NOT NULL CHECK(overall BETWEEN 1 AND 5),
            comment TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )""")
        db.commit()


def _fingerprint(source, token):
    secret = next((os.getenv(n,"") for n in SALT_ENV if len(os.getenv(n,""))>=32), "")
    if not secret:
        raise HTTPException(503,"Anket güvenlik yapılandırması eksik.")
    return hmac.new(secret.encode(), (source+":"+token).encode(), sha256).hexdigest()


def _source_valid(source, token):
    if source not in TYPES or not isinstance(token,str) or len(token)>128:
        raise HTTPException(404,"Değerlendirme bağlantısı bulunamadı.")
    with closing(get_connection()) as db:
        if source=="egitim":
            ernasa_training._invite(db,token)
        else:
            row=db.cursor().execute(
                "SELECT status FROM interview_links WHERE token=?", (token,)
            ).fetchone()
            if not row or row["status"]!="completed":
                raise HTTPException(404,"Tamamlanan mülakat bulunamadı.")


class Feedback(BaseModel):
    source: str
    token: str = Field(min_length=16,max_length=128)
    ease: int = Field(ge=1,le=5)
    clarity: int = Field(ge=1,le=5)
    interview: int | None = Field(default=None,ge=1,le=5)
    training: int | None = Field(default=None,ge=1,le=5)
    overall: int = Field(ge=1,le=5)
    comment: str = Field(default="",max_length=500)
    voluntary: bool = False


def _origin_ok(request):
    origin=request.headers.get("origin","")
    allowed={"https://ernasa.com","https://www.ernasa.com"}
    if os.getenv("ERNASA_IK_LOCAL_PREVIEW")=="1":
        allowed.update(("http://localhost:8081","http://127.0.0.1:8081"))
    if origin not in allowed:
        raise HTTPException(403,"İstek kaynağı geçersiz.")


@router.post("/api/aday-deneyimi")
def submit(request:Request, data:Feedback):
    _origin_ok(request)
    if not data.voluntary:
        raise HTTPException(422,"Gönüllü katılım onayı gerekir.")
    _source_valid(data.source,data.token)
    key=_fingerprint(data.source,data.token)
    comment=data.comment.replace("\r"," ").replace("\n"," ").strip()
    with closing(get_connection()) as db:
        row=db.cursor().execute("SELECT 1 FROM ernasa_candidate_experience WHERE response_key=?", (key,)).fetchone()
        if row:
            return {"received":True,"already_submitted":True}
        db.cursor().execute("""INSERT INTO ernasa_candidate_experience
            (response_key,source,ease,clarity,interview,training,overall,comment,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (key,data.source,data.ease,data.clarity,
             data.interview if data.source=="mulakat" else None,
             data.training if data.source=="egitim" else None,
             data.overall,comment,datetime.now(timezone.utc).isoformat()))
        db.commit()
    return {"received":True,"already_submitted":False}


@router.get("/aday-deneyimi",include_in_schema=False)
def public_survey(source:str="",token:str=""):
    _source_valid(source,token)
    # Token is not embedded in a third-party URL or referrer; static HTML owns a
    # same-origin POST. The page URL itself remains bearer-sensitive.
    return HTMLResponse(_SURVEY_HTML.read_text(encoding="utf-8"),headers={
        "Cache-Control":"no-store", "Referrer-Policy":"no-referrer",
        "X-Robots-Tag":"noindex,nofollow"})


_SURVEY_HTML=Path(__file__).resolve().parent/"candidate_experience.html"


def summary():
    """Aggregate only; callable solely from authenticated CONTROL founder route."""
    with closing(get_connection()) as db:
        rows=db.cursor().execute("""SELECT source,ease,clarity,interview,training,overall,comment
            FROM ernasa_candidate_experience""").fetchall()
    entries=[dict(x) for x in rows]
    output={}
    for source in ("mulakat","egitim"):
        records=[x for x in entries if x["source"]==source]
        result={"count":len(records),"ratings":{}}
        for col in COLUMNS:
            vals=[int(x[col]) for x in records if x[col] is not None]
            result["ratings"][col]={"count":len(vals),
                    "average":round(sum(vals)/len(vals),2) if vals else None}
        # Show text only when at least 5 responses exist to limit identity inference.
        result["comments"]=[x["comment"] for x in records if x["comment"]][:15] if len(records)>=5 else []
        output[source]=result
    return output
