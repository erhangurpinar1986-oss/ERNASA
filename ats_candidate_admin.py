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
