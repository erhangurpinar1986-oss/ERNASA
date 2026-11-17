"""Firma bazlı ERNASA Eğitim: personel sınıfı, program, güvenli davet ve ilerleme.
Staging feature flag olmadan hiçbir rota etkinleşmez.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from typing import Literal


COOKIE = "ernasa_training_session"
SESSION_DAYS = 90


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def token_hash(raw):
    return sha256(raw.encode("utf-8")).hexdigest()


def allowed_company(actor):
    if os.getenv("ERNASA_TRAINING_STAGING") != "1":
        return False
    allowed = set(os.getenv("ERNASA_TRAINING_COMPANIES", "").replace(" ", "").split(","))
    return str(getattr(actor, "company_id", "")) in allowed and bool(getattr(actor, "portal", None))


def require_origin(request: Request):
    origin = request.headers.get("origin", "")
    requested = urlsplit(origin)
    expected = urlsplit(os.getenv("ERNASA_TRAINING_ORIGIN") or str(request.base_url).rstrip("/"))
    if not origin or (requested.scheme, requested.netloc, requested.path, requested.query, requested.fragment) != (
        expected.scheme, expected.netloc, "", "", ""):
        raise HTTPException(403, "İstek kaynağı doğrulanamadı.")


def create_tables(connect):
    with closing(connect()) as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS training_courses (
          id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, personnel_class TEXT NOT NULL,
          position_id TEXT, kind TEXT NOT NULL, title TEXT NOT NULL,
          description TEXT NOT NULL, lessons TEXT NOT NULL, created_at TEXT NOT NULL)""")
        c.execute("""CREATE INDEX IF NOT EXISTS training_courses_company ON training_courses(company_id)""")
        c.execute("""CREATE TABLE IF NOT EXISTS training_enrollments (
          id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, course_id TEXT NOT NULL,
          source_kind TEXT NOT NULL, source_id TEXT NOT NULL, learner_name TEXT NOT NULL,
          invite_hash TEXT, invite_expires_at TEXT, invite_used_at TEXT,
          session_hash TEXT, session_expires_at TEXT, revoked INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, UNIQUE(company_id,course_id,source_kind,source_id))""")
        c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS training_invite_hash ON training_enrollments(invite_hash)""")
        c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS training_session_hash ON training_enrollments(session_hash)""")
        c.execute("""CREATE TABLE IF NOT EXISTS training_answers (
          enrollment_id TEXT NOT NULL, lesson_index INTEGER NOT NULL,
          choice INTEGER NOT NULL, first_choice INTEGER NOT NULL,
          attempts INTEGER NOT NULL, updated_at TEXT NOT NULL,
          PRIMARY KEY(enrollment_id,lesson_index))""")
        conn.commit()


class Lesson(BaseModel):
    module: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=2, max_length=160)
    content: str = Field(min_length=1, max_length=4000)
    question: str = Field(min_length=2, max_length=1000)
    choices: list[str] = Field(min_length=4, max_length=4)
    best: int = Field(ge=0, le=3)
    explanation: str = Field(min_length=2, max_length=1500)

    @field_validator("choices")
    @classmethod
    def nonempty_choices(cls, choices):
        if any(not x.strip() or len(x) > 500 for x in choices):
            raise ValueError("Dört geçerli seçenek girin.")
        return choices


class NewCourse(BaseModel):
    personnel_class: str = Field(min_length=2, max_length=120)
    position_id: str | None = Field(default=None, max_length=64)
    kind: Literal["employee", "partner"] = "employee"
    title: str = Field(min_length=2, max_length=160)
    description: str = Field(default="", max_length=1500)
    lessons: list[Lesson] = Field(min_length=1, max_length=100)


class NewEnrollment(BaseModel):
    course_id: str = Field(min_length=8, max_length=80)
    source_id: str = Field(min_length=8, max_length=200)


class Exchange(BaseModel):
    token: str = Field(min_length=32, max_length=180)


class SaveAnswer(BaseModel):
    index: int = Field(ge=0, le=99)
    choice: int = Field(ge=0, le=3)


def install_training(app, connect, require_manager, frontend):
    """Yalnızca açıkça yetkilendirilmiş staging firmaları için etkin."""
    create_tables(connect)
    frontend = Path(frontend)

    def manager(actor=Depends(require_manager)):
        if not allowed_company(actor):
            raise HTTPException(404, "Eğitim modülü etkin değil.")
        return actor

    def one_enrollment(request):
        token = request.cookies.get(COOKIE, "")
        if len(token) < 30:
            raise HTTPException(401, "Eğitim bağlantınızla tekrar giriş yapın.")
        with closing(connect()) as db:
            c = db.cursor()
            c.execute("""SELECT e.*, t.title, t.personnel_class, t.lessons
              FROM training_enrollments e JOIN training_courses t
              ON t.id=e.course_id AND t.company_id=e.company_id
              WHERE e.session_hash=? AND e.revoked=0""", (token_hash(token),))
            row = c.fetchone()
        if not row or not row["session_expires_at"] or row["session_expires_at"] < iso_now():
            raise HTTPException(401, "Oturum süresi doldu; İK'dan yeni eğitim daveti isteyin.")
        return dict(row)

    @app.get("/api/ik/training/courses")
    def courses(actor=Depends(manager)):
        with closing(connect()) as db:
            c = db.cursor()
            c.execute("""SELECT id, personnel_class, position_id, kind, title, description,
              lessons, created_at FROM training_courses
              WHERE company_id=? ORDER BY created_at DESC""", (actor.company_id,))
            return {"courses":[{**dict(x), "lessons":json.loads(x["lessons"])} for x in c.fetchall()]}

    @app.post("/api/ik/training/courses", status_code=201)
    def create_course(data: NewCourse, request: Request, actor=Depends(manager)):
        require_origin(request)
        if data.position_id:
            with closing(connect()) as db:
                c=db.cursor()
                c.execute("SELECT 1 FROM company_positions WHERE id=? AND company_id=?",
                          (data.position_id,actor.company_id))
                if not c.fetchone():
                    raise HTTPException(404, "Pozisyon firmanızda bulunamadı.")
        key = uuid4().hex
        with closing(connect()) as db:
            c=db.cursor()
            c.execute("""INSERT INTO training_courses VALUES (?,?,?,?,?,?,?,?,?)""",(
                key,actor.company_id,data.personnel_class.strip(),data.position_id,
                data.kind,data.title.strip(),data.description.strip(),
                json.dumps([x.model_dump() for x in data.lessons],ensure_ascii=False),iso_now()))
            db.commit()
        return {"id":key}

    @app.get("/api/ik/training/eligible")
    def eligible(actor=Depends(manager)):
        with closing(connect()) as db:
            c=db.cursor()
            c.execute("""SELECT a.id, a.position_id, c.name, p.name AS position
              FROM ats_applications a JOIN ats_candidates c
              ON c.id=a.candidate_id AND c.company_id=a.company_id
              JOIN company_positions p ON p.id=a.position_id AND p.company_id=a.company_id
              WHERE a.company_id=? AND a.stage='hired'
              ORDER BY a.changed_at DESC LIMIT 100""",(actor.company_id,))
            employees=[dict(x) for x in c.fetchall()]
            c.execute("""SELECT i.token AS id, i.name, i.position
              FROM interview_links i WHERE i.company_id=? AND i.status='completed'
              ORDER BY i.created_at DESC LIMIT 100""",(actor.company_id,))
            partners=[dict(x) for x in c.fetchall()]
        return {"employees":employees,"completed_interviews":partners}

    @app.get("/api/ik/training/enrollments")
    def enrollments(actor=Depends(manager)):
        with closing(connect()) as db:
            c=db.cursor()
            c.execute("""SELECT e.id,e.learner_name,e.course_id,e.revoked,t.title,t.lessons,
                a.lesson_index,a.choice
              FROM training_enrollments e JOIN training_courses t
              ON t.id=e.course_id AND t.company_id=e.company_id
              LEFT JOIN training_answers a ON a.enrollment_id=e.id
              WHERE e.company_id=? ORDER BY e.created_at DESC""",(actor.company_id,))
            rows=c.fetchall()
        aggregated={}
        for row in rows:
            v=dict(row)
            item=aggregated.setdefault(v["id"],{"id":v["id"],"learner_name":v["learner_name"],
                "course_id":v["course_id"],"title":v["title"],"revoked":bool(v["revoked"]),
                "completed":0,"correct":0,"total":len(json.loads(v["lessons"]))})
            if v["lesson_index"] is not None:
                item["completed"]+=1
                lesson=json.loads(v["lessons"])[v["lesson_index"]]
                item["correct"]+=int(v["choice"]==lesson["best"])
        return {"enrollments":list(aggregated.values())}

    @app.post("/api/ik/training/enrollments", status_code=201)
    def enroll(data: NewEnrollment, request: Request, actor=Depends(manager)):
        require_origin(request)
        with closing(connect()) as db:
            c=db.cursor()
            c.execute("SELECT * FROM training_courses WHERE id=? AND company_id=?",
                      (data.course_id,actor.company_id))
            course=c.fetchone()
            if not course:
                raise HTTPException(404,"Eğitim programı bulunamadı.")
            if course["kind"]=="employee":
                c.execute("""SELECT a.position_id,c.name FROM ats_applications a
                  JOIN ats_candidates c ON c.id=a.candidate_id AND c.company_id=a.company_id
                  WHERE a.id=? AND a.company_id=? AND a.stage='hired'""",
                  (data.source_id,actor.company_id))
                target=c.fetchone()
                if not target or (course["position_id"] and course["position_id"]!=target["position_id"]):
                    raise HTTPException(409,"Bu eğitim için işe alımı tamamlanmış uygun aday gerekli.")
                kind="ats"
            else:
                c.execute("""SELECT name FROM interview_links
                  WHERE token=? AND company_id=? AND status='completed'""",
                  (data.source_id,actor.company_id))
                target=c.fetchone()
                if not target:
                    raise HTTPException(409,"İş ortaklığı eğitimi için tamamlanmış mülakat gerekli.")
                kind="interview"
            c.execute("""SELECT id FROM training_enrollments
              WHERE company_id=? AND course_id=? AND source_kind=? AND source_id=?""",
              (actor.company_id,data.course_id,kind,data.source_id))
            previous=c.fetchone()
            if previous:
                return {"id":previous["id"],"existing":True}
            key=uuid4().hex
            c.execute("""INSERT INTO training_enrollments
              (id,company_id,course_id,source_kind,source_id,learner_name,created_at)
              VALUES (?,?,?,?,?,?,?)""",
              (key,actor.company_id,data.course_id,kind,data.source_id,target["name"],iso_now()))
            db.commit()
        return {"id":key,"existing":False}

    @app.post("/api/ik/training/enrollments/{enrollment_id}/invite")
    def invite(enrollment_id: str, request: Request, actor=Depends(manager)):
        require_origin(request)
        raw=secrets.token_urlsafe(36)
        expiry=(datetime.now(timezone.utc)+timedelta(days=7)).isoformat()
        with closing(connect()) as db:
            c=db.cursor()
            c.execute("""UPDATE training_enrollments
              SET invite_hash=?,invite_expires_at=?,invite_used_at=NULL,
                  session_hash=NULL,session_expires_at=NULL,revoked=0
              WHERE id=? AND company_id=?""",
              (token_hash(raw),expiry,enrollment_id,actor.company_id))
            if c.rowcount!=1:
                raise HTTPException(404,"Eğitim kaydı bulunamadı.")
            db.commit()
        base=os.getenv("ERNASA_TRAINING_ORIGIN") or str(request.base_url).rstrip("/")
        return {"invite_url":f"{base}/training/enter#invite={raw}",
                "notice":"Bağlantı tek kullanımlıktır ve yedi gün geçerlidir."}

    @app.post("/api/ik/training/enrollments/{enrollment_id}/revoke")
    def revoke(enrollment_id: str, request: Request, actor=Depends(manager)):
        require_origin(request)
        with closing(connect()) as db:
            c=db.cursor()
            c.execute("""UPDATE training_enrollments
              SET revoked=1,invite_hash=NULL,session_hash=NULL,session_expires_at=NULL
              WHERE id=? AND company_id=?""",(enrollment_id,actor.company_id))
            if c.rowcount!=1:raise HTTPException(404,"Kayıt bulunamadı.")
            db.commit()
        return {"ok":True}

    @app.post("/api/training/exchange")
    def exchange(data: Exchange, request: Request):
        require_origin(request)
        session=secrets.token_urlsafe(36)
        expiry=(datetime.now(timezone.utc)+timedelta(days=SESSION_DAYS)).isoformat()
        with closing(connect()) as db:
            c=db.cursor()
            c.execute("""UPDATE training_enrollments
              SET invite_hash=NULL,invite_used_at=?,session_hash=?,session_expires_at=?
              WHERE invite_hash=? AND revoked=0 AND invite_used_at IS NULL AND invite_expires_at>=?""",
              (iso_now(),token_hash(session),expiry,token_hash(data.token),iso_now()))
            if c.rowcount!=1:
                raise HTTPException(409,"Davet geçersiz, süresi dolmuş veya kullanılmış.")
            db.commit()
        result=JSONResponse({"ok":True},headers={"Cache-Control":"no-store"})
        result.set_cookie(COOKIE,session,max_age=SESSION_DAYS*86400,httponly=True,
                          secure=(request.url.scheme=="https" or os.getenv("RENDER","").lower()=="true"),
                          samesite="strict",path="/")
        return result

    @app.get("/api/training/me")
    def me(request: Request):
        row=one_enrollment(request)
        with closing(connect()) as db:
            c=db.cursor()
            c.execute("SELECT lesson_index,choice FROM training_answers WHERE enrollment_id=?",(row["id"],))
            answers={str(x["lesson_index"]):x["choice"] for x in c.fetchall()}
        lessons=json.loads(row["lessons"])
        safe=[{k:v for k,v in x.items() if k not in ("best","explanation")} for x in lessons]
        session=request.cookies.get(COOKIE)
        csrf=hmac.new(session.encode(),b"ernasa-training-csrf",sha256).hexdigest()
        return JSONResponse({"name":row["learner_name"],"title":row["title"],
            "personnel_class":row["personnel_class"],"lessons":safe,
            "answers":answers,"csrf":csrf},headers={"Cache-Control":"no-store"})

    @app.post("/api/training/answer")
    def answer(data: SaveAnswer,request: Request):
        require_origin(request)
        row=one_enrollment(request)
        session=request.cookies.get(COOKIE,"")
        expected=hmac.new(session.encode(),b"ernasa-training-csrf",sha256).hexdigest()
        if not secrets.compare_digest(request.headers.get("X-Training-CSRF",""),expected):
            raise HTTPException(403,"Eğitim oturumu doğrulanamadı.")
        lessons=json.loads(row["lessons"])
        if data.index>=len(lessons):raise HTTPException(404,"Ders bulunamadı.")
        item=lessons[data.index]
        with closing(connect()) as db:
            c=db.cursor()
            c.execute("""INSERT INTO training_answers
              (enrollment_id,lesson_index,choice,first_choice,attempts,updated_at)
              VALUES (?,?,?,?,?,?)
              ON CONFLICT(enrollment_id,lesson_index) DO UPDATE SET
                choice=excluded.choice,attempts=training_answers.attempts+1,
                updated_at=excluded.updated_at""",
              (row["id"],data.index,data.choice,data.choice,1,iso_now()))
            db.commit()
        return {"ok":True,"correct":data.choice==item["best"],
                "explanation":item["explanation"],"saved_at":iso_now()}

    @app.get("/training/enter")
    def enter():
        html="""<!doctype html><html lang="tr"><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>ERNASA Eğitim Girişi</title><body style="font:16px system-ui;max-width:640px;margin:10vh auto;padding:20px"><h2>ERNASA Eğitim</h2><p id="message">Davet doğrulanıyor…</p><script>async function run(){const t=location.hash.match(/^#invite=([A-Za-z0-9_-]+)$/);history.replaceState(null,'',location.pathname);if(!t){document.querySelector('#message').textContent='Davet bağlantısı bulunamadı.';return;}try{const r=await fetch('/api/training/exchange',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:t[1]})});if(!r.ok)throw Error('Davet geçersiz veya kullanılmış.');location.replace('/training/');}catch(e){document.querySelector('#message').textContent=e.message}}run();</script></body></html>"""
        return HTMLResponse(html,headers={"Cache-Control":"no-store","Referrer-Policy":"no-referrer",
            "Content-Security-Policy":"default-src 'none';script-src 'unsafe-inline';style-src 'unsafe-inline';connect-src 'self';base-uri 'none';frame-ancestors 'none'"})

    @app.get("/training/")
    def trainee_page(request: Request):
        one_enrollment(request)
        return FileResponse(frontend/"training_student.html",headers={
            "Cache-Control":"private, no-store","Referrer-Policy":"no-referrer"})

