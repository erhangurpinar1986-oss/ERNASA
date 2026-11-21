"""Read-only preflight for privileged ATS candidate removal.

No DELETE statements here. A destructive endpoint must not be enabled until
a restorable production backup and dependency-complete transaction are tested.
"""
from contextlib import closing
from fastapi import HTTPException
from database import get_connection


def candidate_removal_preflight(actor, candidate_id: str) -> dict:
    """Identify the exact tenant-owned candidate and blockers without mutation."""
    if not candidate_id or len(candidate_id) > 64:
        raise HTTPException(400, "Geçersiz aday kimliği.")
    if actor.role not in ("admin", "owner", "company_admin", "hr_manager"):
        raise HTTPException(403, "Bu işlem için yönetici yetkisi gerekli.")
    with closing(get_connection()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id,name,email,company_id FROM ats_candidates WHERE id=? AND company_id=?",
            (candidate_id, actor.company_id),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "Aday bulunamadı.")
        candidate = dict(row)
        cur.execute(
            "SELECT id FROM ats_applications WHERE company_id=? AND candidate_id=?",
            (actor.company_id, candidate_id),
        )
        application_ids = [r["id"] for r in cur.fetchall()]
        documents = 0
        interviews = 0
        for application_id in application_ids:
            cur.execute(
                "SELECT count(*) AS n FROM ats_documents WHERE company_id=? AND application_id=?",
                (actor.company_id, application_id),
            )
            documents += cur.fetchone()["n"]
            cur.execute(
                "SELECT count(*) AS n FROM interview_links WHERE company_id=? AND application_id=?",
                (actor.company_id, application_id),
            )
            interviews += cur.fetchone()["n"]
        return {
            "candidate_id": candidate["id"],
            "name": candidate["name"],
            "email": candidate["email"],
            "applications": len(application_ids),
            "documents": documents,
            "interviews": interviews,
            "deletion_enabled": False,
            "reason": "Kalıcı silme için doğrulanmış üretim yedeği ve ilişkili kayıt işlemi gerekli.",
        }


def set_candidate_archive(actor, candidate_id: str, archived: bool) -> dict:
    """Soft archive: preserve every candidate, CV, application and interview row."""
    if not candidate_id or len(candidate_id) > 64:
        raise HTTPException(400, "Geçersiz aday kimliği.")
    from datetime import datetime, timezone
    with closing(get_connection()) as conn:
        try:
            cur = conn.cursor()
            cur.execute("""CREATE TABLE IF NOT EXISTS ats_candidate_archives (
                candidate_id TEXT PRIMARY KEY REFERENCES ats_candidates(id) ON DELETE CASCADE,
                company_id BIGINT NOT NULL,
                archived_by BIGINT NOT NULL,
                archived_at TEXT NOT NULL
            )""")
            cur.execute("SELECT id FROM ats_candidates WHERE id=? AND company_id=?",
                        (candidate_id, actor.company_id))
            if cur.fetchone() is None:
                raise HTTPException(404, "Aday bulunamadı.")
            if archived:
                cur.execute("""INSERT INTO ats_candidate_archives
                    (candidate_id,company_id,archived_by,archived_at)
                    VALUES (?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET
                    archived_by=excluded.archived_by,archived_at=excluded.archived_at
                    WHERE ats_candidate_archives.company_id=excluded.company_id""",
                    (candidate_id, actor.company_id, actor.user_id,
                     datetime.now(timezone.utc).isoformat()))
            else:
                cur.execute("DELETE FROM ats_candidate_archives WHERE candidate_id=? AND company_id=?",
                            (candidate_id, actor.company_id))
            conn.commit()
            return {"candidate_id": candidate_id, "archived": archived}
        except Exception:
            conn.rollback()
            raise
