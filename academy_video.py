"""ERNASA Academy - consent-based optional video and tenant-scoped HR media review.

This module never changes interviews, CVs, or hiring decisions. It stores
training presentation media in dedicated tables and requires invitation, course
assignment, explicit permission, and tenant-scoped HR access.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import re

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response

import academy_assignments
import academy_audio
import ernasa_training
from database import DATABASE_URL, get_connection

router = APIRouter()
MAX_VIDEO_BYTES = 40 * 1024 * 1024
RETENTION_DAYS = 30
VIDEO_TYPES = frozenset(("video/webm", "video/mp4"))
SCENARIOS = frozenset(("tanitim", "itiraz", "serbest"))
RECORDING_ID = re.compile(r"[a-f0-9]{32}")


def initialize_schema():
    binary = "BYTEA" if DATABASE_URL else "BLOB"
    with closing(get_connection()) as db:
        c = db.cursor()
        c.execute(f"""CREATE TABLE IF NOT EXISTS ernasa_training_video (
            id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL REFERENCES ernasa_training_invites(token_hash),
            module_id TEXT NOT NULL,
            media_type TEXT NOT NULL,
            video_bytes {binary} NOT NULL,
            byte_size INTEGER NOT NULL,
            consented_at TEXT NOT NULL,
            submitted_at TEXT NOT NULL,
            UNIQUE(token_hash,module_id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS ernasa_training_media_context (
            token_hash TEXT NOT NULL,
            module_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            scenario_id TEXT NOT NULL,
            PRIMARY KEY(token_hash,module_id,kind)
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS ernasa_training_video_tenant ON ernasa_training_video(token_hash,submitted_at)")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
        c.execute("DELETE FROM ernasa_training_video WHERE submitted_at < ?", (cutoff,))
        c.execute("""DELETE FROM ernasa_training_media_context WHERE kind='video'
            AND NOT EXISTS (SELECT 1 FROM ernasa_training_video v
                            WHERE v.token_hash=ernasa_training_media_context.token_hash
                            AND v.module_id=ernasa_training_media_context.module_id)""")
        db.commit()


def validate_video_header(payload: bytes, media_type: str) -> bool:
    if len(payload) < 128:
        return False
    if media_type == "video/webm":
        return payload.startswith(b"\x1a\x45\xdf\xa3")
    if media_type == "video/mp4":
        return payload[4:8] == b"ftyp"
    return False


def save_context(db, digest: str, module_id: str, kind: str, scenario_id: str):
    if scenario_id not in SCENARIOS:
        raise HTTPException(422, "Sunum senaryosu geçersiz.")
    db.cursor().execute("""INSERT INTO ernasa_training_media_context
        (token_hash,module_id,kind,scenario_id) VALUES(?,?,?,?)
        ON CONFLICT(token_hash,module_id,kind)
        DO UPDATE SET scenario_id=excluded.scenario_id""",
        (digest, module_id, kind, scenario_id))


@router.post("/api/egitim/{token}/video", status_code=201)
async def submit_video(request: Request, token: str, module_id: str = Form(...),
                       consent: str = Form(...), scenario_id: str = Form("tanitim"),
                       video: UploadFile = File(...)):
    academy_audio._origin_ok(request)
    if consent != "yes":
        raise HTTPException(422, "Video göndermek için açık onay gerekli.")
    if scenario_id not in SCENARIOS:
        raise HTTPException(422, "Sunum senaryosu geçersiz.")
    if module_id not in academy_assignments.MODULE_TITLES:
        raise HTTPException(422, "Eğitim modülü geçersiz.")
    assignment = academy_assignments.assigned_modules(token)
    if module_id not in assignment["module_ids"] or not assignment.get("video_allowed", False):
        raise HTTPException(403, "Video sunumu bu katılımcıya atanmadı.")
    media = academy_audio._safe_type(video.content_type)
    if media not in VIDEO_TYPES:
        raise HTTPException(415, "Yalnızca WebM veya MP4 video kabul edilir.")
    data, total = [], 0
    try:
        while True:
            chunk = await video.read(256 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_VIDEO_BYTES:
                raise HTTPException(413, "Video en fazla 40 MB olabilir.")
            data.append(chunk)
    finally:
        await video.close()
    blob = b"".join(data)
    if not validate_video_header(blob, media):
        raise HTTPException(415, "Geçerli bir kamera videosu yükleyin.")
    with closing(get_connection()) as db:
        digest, _ = ernasa_training._invite(db, token)
        now = datetime.now(timezone.utc).isoformat()
        c = db.cursor()
        c.execute("""INSERT INTO ernasa_training_video
            (id,token_hash,module_id,media_type,video_bytes,byte_size,consented_at,submitted_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(token_hash,module_id) DO UPDATE SET id=excluded.id,
                media_type=excluded.media_type,video_bytes=excluded.video_bytes,
                byte_size=excluded.byte_size,consented_at=excluded.consented_at,
                submitted_at=excluded.submitted_at""",
            (uuid4().hex, digest, module_id, media, blob, len(blob), now, now))
        save_context(db, digest, module_id, "video", scenario_id)
        db.commit()
    return {"received": True, "module_id": module_id,
            "submitted_at": now, "size_bytes": len(blob), "scenario_id": scenario_id}


def _tenant(company_id):
    if company_id is None:
        return "a.company_id IS NULL", ()
    return "a.company_id=?", (company_id,)


@router.get("/api/egitim/ik/kayitlar")
def hr_recordings(request: Request):
    company_id, _ = academy_assignments._actor(request)
    where, args = _tenant(company_id)
    cutoff = (datetime.now(timezone.utc)-timedelta(days=RETENTION_DAYS)).isoformat()
    output = []
    with closing(get_connection()) as db:
        c = db.cursor()
        for table,kind in (("ernasa_training_audio","ses"),("ernasa_training_video","video")):
            c.execute(f"""SELECT r.id,r.module_id,r.submitted_at,r.byte_size,
                i.recipient,a.program_id,mc.scenario_id
                FROM {table} r
                JOIN ernasa_training_assignments a ON a.token_hash=r.token_hash
                JOIN ernasa_training_invites i ON i.token_hash=r.token_hash
                LEFT JOIN ernasa_training_media_context mc
                  ON mc.token_hash=r.token_hash AND mc.module_id=r.module_id AND mc.kind=?
                WHERE {where} AND r.submitted_at>=?
                ORDER BY r.submitted_at DESC LIMIT 100""",
                (kind,)+args+(cutoff,))
            output.extend({**dict(row), "kind": kind} for row in c.fetchall())
    output.sort(key=lambda item:item["submitted_at"], reverse=True)
    return {"recordings": output[:200]}


def _media(request: Request, recording_id: str, kind: str):
    company_id, _ = academy_assignments._actor(request)
    if not RECORDING_ID.fullmatch(recording_id):
        raise HTTPException(404, "Kayıt bulunamadı.")
    table = "ernasa_training_video" if kind == "video" else "ernasa_training_audio"
    column = "video_bytes" if kind == "video" else "audio_bytes"
    where, args = _tenant(company_id)
    cutoff = (datetime.now(timezone.utc)-timedelta(days=RETENTION_DAYS)).isoformat()
    with closing(get_connection()) as db:
        c = db.cursor()
        c.execute(f"""SELECT r.media_type, r.{column} FROM {table} r
            JOIN ernasa_training_assignments a ON a.token_hash=r.token_hash
            WHERE r.id=? AND {where} AND r.submitted_at>=?""",
            (recording_id,)+args+(cutoff,))
        row = c.fetchone()
    if not row:
        raise HTTPException(404, "Bu firmaya ait kayıt bulunamadı.")
    return bytes(row[column]), row["media_type"]


@router.get("/api/egitim/ik/kayitlar/{kind}/{recording_id}")
def hr_play(request: Request, kind: str, recording_id: str):
    if kind not in ("ses", "video"):
        raise HTTPException(404, "Kayıt türü geçersiz.")
    payload, media = _media(request, recording_id, kind)
    return Response(content=payload, media_type=media,
                    headers={**academy_audio._headers(),
                             "X-Content-Type-Options": "nosniff",
                             "Content-Disposition": "inline"})


@router.delete("/api/egitim/ik/kayitlar/{kind}/{recording_id}", status_code=204)
def hr_delete(request: Request, kind: str, recording_id: str):
    company_id, _ = academy_assignments._actor(request, write=True)
    if kind not in ("ses", "video") or not RECORDING_ID.fullmatch(recording_id):
        raise HTTPException(404, "Kayıt bulunamadı.")
    table = "ernasa_training_video" if kind == "video" else "ernasa_training_audio"
    where, args = _tenant(company_id)
    with closing(get_connection()) as db:
        c = db.cursor()
        c.execute(f"""DELETE FROM {table}
            WHERE id=? AND token_hash IN
              (SELECT a.token_hash FROM ernasa_training_assignments a WHERE {where})""",
              (recording_id,)+args)
        db.commit()
    return Response(status_code=204, headers=academy_audio._headers())
