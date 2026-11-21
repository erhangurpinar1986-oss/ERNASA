"""Manager-only, tenant-scoped ATS candidate removal.

PostgreSQL only. Fails closed on unknown dependent foreign keys and interviews.
No other candidate is selected by name or email.
"""
from contextlib import closing
from fastapi import HTTPException
from database import get_connection

CHILDREN = (
    ("ats_review_details","evaluation_id","evaluation"),
    ("ats_offer_details","offer_id","offer"),
    ("ats_interview_cv_sources","document_id","document"),
    ("ats_cv_imports","document_id","document"),
    ("ats_application_sources","application_id","application"),
    ("flow_candidate_appointment","application_id","application"),
    ("flow_candidate_contact_preferences","application_id","application"),
    ("flow_contact_outbox","application_id","application"),
    ("selin_intake_queue","application_id","application"),
    ("ats_candidate_archives","candidate_id","candidate"),
    ("ats_document_blobs","application_id","application"),
    ("ats_document_digests","application_id","application"),
    ("flow_screening_results","application_id","application"),
    ("flow_screening_jobs","document_id","document"),
    ("flow_booking_jobs","application_id","application"),
    ("ats_evaluations","application_id","application"),
    ("ats_notes","application_id","application"),
    ("ats_notices","application_id","application"),
    ("ats_offers","application_id","application"),
    ("ats_privacy_requests","application_id","application"),
    ("ats_schedules","application_id","application"),
    ("ats_stage_events","application_id","application"),
    ("ats_upload_jobs","application_id","application"),
    ("ats_documents","application_id","application"),
    ("ats_candidate_email_conflicts","candidate_id","candidate"),
    ("ats_applications","id","application"),
    ("ats_candidates","id","candidate"),
)

def delete_candidate(actor, application_id: str, confirmation: str):
    if confirmation != "KALICI SIL":
        raise HTTPException(400, "Onay metni KALICI SIL olmalı.")
    with closing(get_connection()) as conn:
        try:
            cur=conn.cursor()
            if not hasattr(conn,"connection"):
                raise HTTPException(503,"Kalıcı silme yalnızca PostgreSQL üzerinde kullanılabilir.")
            cur.execute("SELECT pg_advisory_xact_lock(3292026,1122)")
            cur.execute("""SELECT c.id,c.name FROM ats_applications a
                JOIN ats_candidates c ON c.id=a.candidate_id AND c.company_id=a.company_id
                WHERE a.id=? AND a.company_id=? FOR UPDATE OF c""",
                (application_id,actor.company_id))
            candidate=cur.fetchone()
            if not candidate:
                raise HTTPException(404,"Aday bulunamadı.")
            cid=candidate["id"]
            cur.execute("SELECT id FROM ats_applications WHERE company_id=? AND candidate_id=? FOR UPDATE",
                        (actor.company_id,cid))
            app_ids=[row["id"] for row in cur.fetchall()]
            if not app_ids:
                raise HTTPException(409,"Adayın başvurusu bulunamadı.")
            cur.execute("SELECT count(*) AS n FROM interview_links WHERE company_id=? AND application_id=ANY(?)",
                        (actor.company_id,app_ids))
            if cur.fetchone()["n"]:
                raise HTTPException(409,"Mülakat bağlantısı olan aday güvenlik nedeniyle silinemez.")
            cur.execute("SELECT id FROM ats_documents WHERE company_id=? AND application_id=ANY(?)",
                        (actor.company_id,app_ids))
            docs=[row["id"] for row in cur.fetchall()]
            cur.execute("SELECT id FROM ats_evaluations WHERE company_id=? AND application_id=ANY(?)",
                        (actor.company_id,app_ids))
            evals=[row["id"] for row in cur.fetchall()]
            cur.execute("SELECT id FROM ats_offers WHERE company_id=? AND application_id=ANY(?)",
                        (actor.company_id,app_ids))
            offers=[row["id"] for row in cur.fetchall()]
            groups={"candidate":[cid],"application":app_ids,"document":docs,"evaluation":evals,"offer":offers}
            # Detect new FK dependencies before any destructive write.
            cur.execute("""SELECT conrelid::regclass::text AS child,confrelid::regclass::text AS parent
                FROM pg_constraint WHERE contype='f' AND confrelid IN
                ('public.ats_candidates'::regclass,'public.ats_applications'::regclass,
                 'public.ats_documents'::regclass,'public.ats_evaluations'::regclass,
                 'public.ats_offers'::regclass)""")
            allowed={table for table,_,_ in CHILDREN} | {"interview_links"}  # Existing interview links are explicitly rejected above.
            unknown=[row["child"] for row in cur.fetchall()
                     if row["child"].split(".")[-1] not in allowed]
            if unknown:
                raise HTTPException(409,"Bilinmeyen bağlı tablolar var; güvenli silme engellendi.")
            removed={}
            for table,key,group in CHILDREN:
                ids=groups[group]
                if not ids:
                    continue
                cur.execute(f"DELETE FROM {table} WHERE company_id=? AND {key}=ANY(?)",
                            (actor.company_id,ids))
                removed[table]=cur.rowcount
            if removed.get("ats_candidates") != 1 or removed.get("ats_applications") != len(app_ids):
                raise HTTPException(409,"Silme sayıları tutarsız; işlem geri alındı.")
            conn.commit()
            return {"deleted":True,"candidate_id":cid,"applications":len(app_ids)}
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise HTTPException(409,"Bağlı kayıtlar nedeniyle silme tamamlanamadı; hiçbir değişiklik kaydedilmedi.")
