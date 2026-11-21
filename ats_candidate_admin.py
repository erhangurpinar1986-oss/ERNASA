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


def permanently_delete_candidate(actor, candidate_id: str, confirmed_name: str) -> dict:
    """Delete exactly one tenant-owned ATS candidate and dependent ATS rows.

    Fail closed on linked interview records or unknown foreign keys. A transaction
    rolls back all deletes if any constraint or integrity check fails.
    """
    if not candidate_id or len(candidate_id) > 64:
        raise HTTPException(400, "Geçersiz aday kimliği.")
    if not confirmed_name or len(confirmed_name) > 200:
        raise HTTPException(400, "Aday adını onaylayın.")
    with closing(get_connection()) as conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT id,name FROM ats_candidates WHERE id=? AND company_id=?",
                        (candidate_id, actor.company_id))
            candidate = cur.fetchone()
            if not candidate:
                raise HTTPException(404, "Aday bulunamadı.")
            if candidate["name"] != confirmed_name:
                raise HTTPException(409, "Aday adı değişmiş. Dosyayı yeniden açın.")
            cur.execute("SELECT id FROM ats_applications WHERE candidate_id=? AND company_id=?",
                        (candidate_id, actor.company_id))
            app_ids = [row["id"] for row in cur.fetchall()]
            for app_id in app_ids:
                cur.execute("SELECT count(*) AS n FROM interview_links WHERE application_id=? AND company_id=?",
                            (app_id, actor.company_id))
                if cur.fetchone()["n"]:
                    raise HTTPException(409, "Bu adayın mülakat bağlantıları var. Güvenli silme için ayrıca incelenmeli.")
                cur.execute("SELECT id FROM ats_documents WHERE application_id=? AND company_id=?",
                            (app_id, actor.company_id))
                doc_ids = [row["id"] for row in cur.fetchall()]
                cur.execute("SELECT id FROM ats_evaluations WHERE application_id=? AND company_id=?",
                            (app_id, actor.company_id))
                eval_ids = [row["id"] for row in cur.fetchall()]
                cur.execute("SELECT id FROM ats_offers WHERE application_id=? AND company_id=?",
                            (app_id, actor.company_id))
                offer_ids = [row["id"] for row in cur.fetchall()]
                for table, column, ids in (
                    ("ats_review_details", "evaluation_id", eval_ids),
                    ("ats_offer_details", "offer_id", offer_ids),
                    ("ats_interview_cv_sources", "document_id", doc_ids),
                    ("ats_cv_imports", "document_id", doc_ids),
                ):
                    for item_id in ids:
                        cur.execute(f"DELETE FROM {table} WHERE {column}=? AND company_id=?",
                                    (item_id, actor.company_id))
                for table in (
                    "ats_document_blobs", "ats_document_digests", "ats_evaluations",
                    "ats_notes", "ats_notices", "ats_offers", "ats_privacy_requests",
                    "ats_schedules", "ats_stage_events", "ats_upload_jobs",
                    "flow_screening_results", "flow_booking_jobs",
                ):
                    cur.execute(f"DELETE FROM {table} WHERE application_id=? AND company_id=?",
                                (app_id, actor.company_id))
                cur.execute("DELETE FROM ats_documents WHERE application_id=? AND company_id=?",
                            (app_id, actor.company_id))
                cur.execute("DELETE FROM ats_applications WHERE id=? AND company_id=?",
                            (app_id, actor.company_id))
                if cur.rowcount != 1:
                    raise RuntimeError("Başvuru silme doğrulaması başarısız.")
            cur.execute("DELETE FROM ats_candidate_email_conflicts WHERE candidate_id=? AND company_id=?",
                        (candidate_id, actor.company_id))
            cur.execute("DELETE FROM ats_candidate_archives WHERE candidate_id=? AND company_id=?",
                        (candidate_id, actor.company_id))
            cur.execute("DELETE FROM ats_candidates WHERE id=? AND company_id=?",
                        (candidate_id, actor.company_id))
            if cur.rowcount != 1:
                raise RuntimeError("Aday silme doğrulaması başarısız.")
            conn.commit()
            return {"deleted": True, "candidate_id": candidate_id, "applications_deleted": len(app_ids)}
        except Exception:
            conn.rollback()
            raise
