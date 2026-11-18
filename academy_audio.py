"""ERNASA Academy: invited voice practice and owner-only review.

The learning HTML contains synthetic examples only. Speech recordings are uploaded
only with explicit consent, kept in PostgreSQL, and never exposed via /static.
"""
from contextlib import closing
from datetime import datetime, timezone, timedelta
from io import BytesIO
from pathlib import Path
from uuid import uuid4
import os
import re

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

import ernasa_training
from database import DATABASE_URL, get_connection
from shield_sessions import verify_ik_session

router = APIRouter()
MAX_BYTES = 6 * 1024 * 1024
ALLOWED_MODULES = frozenset((
    "vizyon", "paneller", "ats", "cv_analiz", "iki_asama", "mulakat",
    "rapor", "guvenlik", "demo", "avcilik", "kesif", "crm",
    "müzakere", "kurumsal", "almanya", "metrik",
))
# Browser-generated formats accepted; never accept arbitrary file extensions.
AUDIO_TYPES = {
    "audio/webm": "webm",
    "audio/mp4": "m4a",
    "audio/ogg": "ogg",
}
ACADEMY_HTML = Path(__file__).resolve().parent / "academy_portal.html"


def initialize_schema():
    with closing(get_connection()) as db:
        c = db.cursor()
        binary_type = "BYTEA" if DATABASE_URL else "BLOB"
        c.execute(f"""CREATE TABLE IF NOT EXISTS ernasa_training_audio (
            id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL REFERENCES ernasa_training_invites(token_hash),
            module_id TEXT NOT NULL,
            media_type TEXT NOT NULL,
            audio_bytes {binary_type} NOT NULL,
            byte_size INTEGER NOT NULL,
            consented_at TEXT NOT NULL,
            submitted_at TEXT NOT NULL,
            UNIQUE(token_hash,module_id)
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS ernasa_training_audio_owner ON ernasa_training_audio(token_hash,submitted_at)")
        # Default 30-day retention for training audio. Expired material is purged on startup.
        expires_before=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
        c.execute("DELETE FROM ernasa_training_audio WHERE submitted_at < ?", (expires_before,))
        db.commit()


def _origin_ok(request: Request):
    origin = request.headers.get("origin", "")
    trusted = {"https://ernasa.com", "https://www.ernasa.com"}
    if os.getenv("ERNASA_IK_LOCAL_PREVIEW") == "1":
        trusted.update({"http://127.0.0.1:8081", "http://localhost:8081"})
    if origin not in trusted:
        raise HTTPException(status_code=403, detail="İstek kaynağı geçersiz.")


def _owner(request: Request, modify=False):
    # Only the ERNASA owner/legacy dashboard account, not tenants in company portals.
    from control_integration import portal_for
    if portal_for(request):
        raise HTTPException(403, "Eğitim yönetimi kurucu hesabına özeldir.")
    if not verify_ik_session(request.cookies.get("ernasa_ik_session", "")):
        raise HTTPException(401, "ERNASA yetkili oturumu gerekli.")
    if modify:
        _origin_ok(request)


def _valid_audio(data: bytes, media_type: str) -> bool:
    if media_type == "audio/webm":
        return data.startswith(b"\x1a\x45\xdf\xa3")
    if media_type == "audio/ogg":
        return data.startswith(b"OggS")
    if media_type == "audio/mp4":
        return len(data) >= 12 and data[4:8] == b"ftyp"
    return False


def _safe_type(content_type: str) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _headers():
    return {"Cache-Control":"private, no-store", "X-Robots-Tag":"noindex, nofollow",
            "Referrer-Policy":"no-referrer"}


@router.get("/egitim/{token}", include_in_schema=False)
def academy_page(token: str):
    # A named recipient is never presented through /static or a public index.
    with closing(get_connection()) as db:
        _, row = ernasa_training._invite(db, token)
        recipient = row["recipient"]
    if not ACADEMY_HTML.exists():
        raise HTTPException(503, "Eğitim ekranı henüz sunucuda bulunamadı.")
    import json
    import academy_assignments
    html = ACADEMY_HTML.read_text(encoding="utf-8")
    start_marker = "const ACADEMY_DATA="
    start = html.find(start_marker)
    if start < 0:
        raise HTTPException(503, "Eğitim içeriği bulunamadı.")
    data_start = start + len(start_marker)
    try:
        data, data_size = json.JSONDecoder().raw_decode(html[data_start:])
    except ValueError:
        raise HTTPException(503, "Eğitim içeriği okunamadı.") from None
    assignments = academy_assignments.assigned_modules(token)
    selected = set(assignments["module_ids"])
    data["name"] = recipient
    data["modules"] = [module for module in data["modules"] if module["id"] in selected]
    if not data["modules"]:
        raise HTTPException(409, "Katılımcıya henüz eğitim atanmadı.")
    # The final assessment must match the assigned modules, not unseen lessons.
    # The complete B2B curriculum retains its original 14-question final.
    from academy_assignments import MODULE_TITLES
    if not (assignments["program_id"] == "b2b_satis"
            and selected == set(MODULE_TITLES)):
        data["final"] = [
            module["questions"][1] if len(module["questions"]) > 1 else module["questions"][0]
            for module in data["modules"] if module.get("questions")
        ][:14]
    encoded = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    html = html[:data_start] + encoded + html[data_start + data_size:]
    return HTMLResponse(content=html, headers=_headers())


@router.post("/api/egitim/{token}/ses", status_code=201)
async def submit_voice(request: Request, token: str, module_id: str = Form(...),
                       consent: str = Form(...), scenario_id: str = Form("tanitim"),
                       audio: UploadFile = File(...)):
    _origin_ok(request)
    if module_id not in ALLOWED_MODULES:
        raise HTTPException(422, "Eğitim modülü geçersiz.")
    from academy_assignments import assigned_modules
    if module_id not in assigned_modules(token)["module_ids"]:
        raise HTTPException(403, "Bu ders katılımcıya atanmadı.")
    if consent != "yes":
        raise HTTPException(422, "Ses kaydını göndermek için açık onay gerekli.")
    if scenario_id not in ("tanitim","itiraz","serbest"):
        raise HTTPException(422, "Sunum senaryosu geçersiz.")
    media_type = _safe_type(audio.content_type)
    if media_type not in AUDIO_TYPES:
        raise HTTPException(415, "Yalnızca WebM, MP4 veya OGG ses kaydı kabul edilir.")
    chunks, total = [], 0
    try:
        while True:
            part = await audio.read(256 * 1024)
            if not part:
                break
            total += len(part)
            if total > MAX_BYTES:
                raise HTTPException(413, "Ses kaydı en fazla 6 MB olabilir.")
            chunks.append(part)
    finally:
        await audio.close()
    data = b"".join(chunks)
    if len(data) < 128 or not _valid_audio(data, media_type):
        raise HTTPException(415, "Geçerli bir tarayıcı ses kaydı gerekli.")
    with closing(get_connection()) as db:
        digest, row = ernasa_training._invite(db, token)
        now = datetime.now(timezone.utc).isoformat()
        recording_id = uuid4().hex
        db.cursor().execute("""INSERT INTO ernasa_training_audio
            (id,token_hash,module_id,media_type,audio_bytes,byte_size,consented_at,submitted_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(token_hash,module_id) DO UPDATE SET
                id=excluded.id,media_type=excluded.media_type,audio_bytes=excluded.audio_bytes,
                byte_size=excluded.byte_size,consented_at=excluded.consented_at,
                submitted_at=excluded.submitted_at""",
            (recording_id, digest, module_id, media_type, data, len(data), now, now))
        from academy_video import save_context
        save_context(db, digest, module_id, "ses", scenario_id)
        db.commit()
    return {"received": True, "module_id":module_id,
            "size_bytes":len(data), "submitted_at":now}


@router.get("/api/egitim/{token}/sesler")
def participant_uploads(token: str):
    with closing(get_connection()) as db:
        digest, _ = ernasa_training._invite(db, token)
        c = db.cursor()
        c.execute("""SELECT module_id,submitted_at,byte_size
            FROM ernasa_training_audio WHERE token_hash=? ORDER BY submitted_at DESC""", (digest,))
        return {"recordings":[dict(r) for r in c.fetchall()]}


class InviteRequest(BaseModel):
    recipient: str = Field(min_length=2, max_length=120)
    email: str = Field(default="", max_length=254)
    days: int = Field(default=14, ge=1, le=30)


@router.post("/api/egitim/admin/davet", status_code=201)
def owner_create_invite(request: Request, body: InviteRequest):
    _owner(request, modify=True)
    # Allowed public URL is fixed, never based on a client-supplied Host header.
    base = os.getenv("ERNASA_PUBLIC_URL", "https://ernasa.com")
    if base not in ("https://ernasa.com", "https://www.ernasa.com"):
        raise HTTPException(503, "Eğitim bağlantısı alan adı doğrulanamadı.")
    return ernasa_training.create_invite(
        ernasa_training.InviteForm(recipient=body.recipient,email=body.email,days=body.days),base
    )


@router.get("/api/egitim/admin/kayitlar")
def owner_recordings(request: Request):
    _owner(request)
    with closing(get_connection()) as db:
        c=db.cursor()
        c.execute("""SELECT a.id,a.module_id,a.media_type,a.byte_size,a.submitted_at,
            i.recipient,i.email
            FROM ernasa_training_audio a
            JOIN ernasa_training_invites i ON i.token_hash=a.token_hash
            ORDER BY a.submitted_at DESC LIMIT 200""")
        return {"recordings":[dict(row) for row in c.fetchall()]}


@router.get("/api/egitim/admin/kayitlar/{recording_id}/dinle")
def owner_play_recording(request: Request, recording_id: str):
    _owner(request)
    if not re.fullmatch(r"[a-f0-9]{32}", recording_id):
        raise HTTPException(404, "Ses kaydı bulunamadı.")
    with closing(get_connection()) as db:
        c=db.cursor()
        c.execute("SELECT media_type,audio_bytes FROM ernasa_training_audio WHERE id=?", (recording_id,))
        row=c.fetchone()
        if not row:
            raise HTTPException(404, "Ses kaydı bulunamadı.")
        content = bytes(row["audio_bytes"])
        media_type = row["media_type"]
    return Response(content=content,media_type=media_type,headers={
        **_headers(),"Content-Disposition":"inline"
    })


@router.delete("/api/egitim/admin/kayitlar/{recording_id}",status_code=204)
def owner_delete_recording(request: Request, recording_id: str):
    _owner(request, modify=True)
    if not re.fullmatch(r"[a-f0-9]{32}", recording_id):
        raise HTTPException(404, "Ses kaydı bulunamadı.")
    with closing(get_connection()) as db:
        c=db.cursor()
        c.execute("DELETE FROM ernasa_training_audio WHERE id=?", (recording_id,))
        db.commit()
    return Response(status_code=204, headers=_headers())


ADMIN_HTML = """<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ERNASA Eğitim Yönetimi</title><style>
body{font:15px/1.6 system-ui,Arial;background:#f2f5fa;color:#19334f;max-width:1050px;margin:30px auto;padding:0 18px}
main{background:white;border:1px solid #dae4f0;border-radius:15px;padding:26px;margin:16px 0}
input,button{font:inherit;padding:9px 12px;border-radius:8px;border:1px solid #b5cadd}
button{cursor:pointer;background:#173b5d;color:white}button:disabled{opacity:.6}
label{display:inline-block;margin:8px 10px 8px 0}input{max-width:260px}
table{border-collapse:collapse;width:100%;margin-top:10px}td,th{padding:9px 6px;border-bottom:1px solid #e5ebf3;text-align:left}
audio{max-width:250px}code{word-break:break-word}small{color:#5a6f82}
</style></head><body><h1>ERNASA · Eğitim Yönetimi</h1>
<main><h2>Kişiye özel eğitim bağlantısı</h2>
<form id="f"><label>Katılımcı <input id="n" required maxlength="120" value=""></label>
<label>E-posta (isteğe bağlı) <input id="e" type="email" maxlength="254"></label>
<label>Geçerlilik (gün) <input id="d" type="number" min="1" max="30" value="14"></label>
<button type="submit">Bağlantı oluştur</button></form>
<div id="link" role="status"></div><p><small>Bağlantı yalnızca burada gösterilir; otomatik e-posta gönderilmez.</small></p></main>
<main><h2>Gönderilen ses kayıtları</h2><button id="refresh">Yenile</button><div id="list" role="status">Yükleniyor…</div>
<p><small>Kayıtları yalnızca yetkili kurucu oturumunda açabilirsiniz.</small></p></main>
<script>
const el=id=>document.getElementById(id);
async function parse(r){const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||('HTTP '+r.status));return d;}
el('f').addEventListener('submit',async e=>{e.preventDefault();el('link').textContent='Oluşturuluyor…';
try {const r=await fetch('/api/egitim/admin/davet',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({recipient:el('n').value,email:el('e').value,days:Number(el('d').value)})});const d=await parse(r);
el('link').replaceChildren();const label=document.createElement('p');label.textContent='Kişisel erişim bağlantısı (gönderilmedi):';const a=document.createElement('a');a.href=d.link;a.textContent=d.link;a.rel='noreferrer';el('link').append(label,a);}
catch(err){el('link').textContent='Hata: '+err.message;}});
async function load(){try{const d=await parse(await fetch('/api/egitim/admin/kayitlar'));const t=document.createElement('table');const tr=t.insertRow();for(const h of ['Katılımcı','Modül','Tarih','Ses kaydı','İşlem']){const th=document.createElement('th');th.textContent=h;tr.appendChild(th)}
for(const x of d.recordings){const row=t.insertRow();for(const value of [x.recipient,x.module_id,x.submitted_at])row.insertCell().textContent=value;const audio=document.createElement('audio');audio.controls=true;audio.preload='none';audio.src='/api/egitim/admin/kayitlar/'+encodeURIComponent(x.id)+'/dinle';row.insertCell().appendChild(audio);
const btn=document.createElement('button');btn.textContent='Sil';btn.onclick=async()=>{if(!confirm('Bu ses kaydı kalıcı olarak silinsin mi?'))return;const resp=await fetch('/api/egitim/admin/kayitlar/'+encodeURIComponent(x.id),{method:'DELETE'});if(resp.ok)load();else alert('Silme başarısız.');};row.insertCell().appendChild(btn);}
el('list').replaceChildren(t);if(!d.recordings.length)el('list').textContent='Henüz gönderilmiş ses kaydı yok.';}catch(e){el('list').textContent='Kayıtlar yüklenemedi: '+e.message;}}
el('refresh').onclick=load;load();
</script></body></html>"""


@router.get("/egitim-yonetim", include_in_schema=False)
def owner_training_page(request: Request):
    _owner(request)
    return HTMLResponse(content=ADMIN_HTML,headers=_headers())
