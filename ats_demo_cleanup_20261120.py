"""One-time transaction-safe ERNASA ATS demo applicant removal.

Scope: precisely 4 named company_id=2 candidates and their 4 applications.
Archives ALL dependent ATS database rows before deletion, preserving Merve and
other genuine applicants, interview history, Academy and Sales Hub.
Run only via explicit one-time environment switch. No external file deletion.
"""
import os
from contextlib import closing

COMPANY_ID = 2
EXPECTED_CANDIDATES_BEFORE = 24
EXPECTED_APPLICATIONS_BEFORE = 24
KEEP = "Merve Saye Aydoğdu Sipahi"
NAMES = frozenset({
    "Deniz Arman",
    "Gökhan Çeti̇n",
    "Erhan Gürpınar",
    "Erhan Gürpinar",
})
ARCHIVE_TABLE = "ernasa_control.ats_demo_cleanup_archive_20261120"
RUN_KEY = "ernasa_ats_four_demo_applicants_20261120"

# Deletion order is child-first, with fixed table identifiers.
DEPENDENCIES = (
    ("ats_review_details", "evaluation_id", "evaluation"),
    ("ats_offer_details", "offer_id", "offer"),
    ("ats_interview_cv_sources", "document_id", "document"),
    ("ats_cv_imports", "document_id", "document"),
    ("ats_document_blobs", "application_id", "application"),
    ("ats_document_digests", "application_id", "application"),
    ("ats_evaluations", "application_id", "application"),
    ("ats_notes", "application_id", "application"),
    ("ats_notices", "application_id", "application"),
    ("ats_offers", "application_id", "application"),
    ("ats_privacy_requests", "application_id", "application"),
    ("ats_schedules", "application_id", "application"),
    ("ats_stage_events", "application_id", "application"),
    ("ats_upload_jobs", "application_id", "application"),
    ("ats_documents", "application_id", "application"),
    ("ats_applications", "id", "application"),
    ("ats_candidates", "id", "candidate"),
)


def cleanup(dsn=None):
    dsn = dsn or os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("PostgreSQL bağlantısı bulunamadı; ATS temizliği yapılmadı.")
    import psycopg2
    with closing(psycopg2.connect(dsn, connect_timeout=10)) as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout='20000ms'")
                cur.execute("SELECT pg_advisory_xact_lock(3292026, 1121)")
                cur.execute("SELECT count(*) FROM public.ats_candidates WHERE company_id=%s", (COMPANY_ID,))
                total_candidates = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM public.ats_applications WHERE company_id=%s", (COMPANY_ID,))
                total_applications = cur.fetchone()[0]
                cur.execute(
                    "SELECT id,name FROM public.ats_candidates WHERE company_id=%s AND name=ANY(%s) ORDER BY name FOR UPDATE",
                    (COMPANY_ID, list(NAMES)),
                )
                candidates = cur.fetchall()
                if not candidates and total_candidates == 20 and total_applications == 20:
                    return {"already_clean": True, "removed": 0, "remaining": 20}
                if (total_candidates, total_applications) != (
                    EXPECTED_CANDIDATES_BEFORE, EXPECTED_APPLICATIONS_BEFORE
                ):
                    raise RuntimeError("ATS aday/başvuru sayıları değişmiş; işlem iptal.")
                if len(candidates) != 4 or {name for _,name in candidates} != NAMES:
                    raise RuntimeError("Dört aday eşleşmesi tam değil; işlem iptal.")
                candidate_ids = [cid for cid,_ in candidates]
                cur.execute(
                    "SELECT id,candidate_id FROM public.ats_applications "
                    "WHERE company_id=%s AND candidate_id=ANY(%s) ORDER BY id FOR UPDATE",
                    (COMPANY_ID, candidate_ids),
                )
                apps = cur.fetchall()
                if len(apps) != 4 or {c for _,c in apps} != set(candidate_ids):
                    raise RuntimeError("Her adayın tek başvurusu yok; işlem iptal.")
                app_ids = [aid for aid,_ in apps]
                cur.execute(
                    "SELECT count(*) FROM public.interview_links WHERE application_id=ANY(%s)",
                    (app_ids,),
                )
                if cur.fetchone()[0]:
                    raise RuntimeError("Bu başvurulardan mülakat bağlantısı var; işlem iptal.")
                # Protect all real interviews and all OTHER candidate records.
                cur.execute(
                    "SELECT count(*) FROM public.interview_links "
                    "WHERE company_id=%s AND name=%s AND status='completed'",
                    (COMPANY_ID,KEEP),
                )
                if cur.fetchone()[0] != 1:
                    raise RuntimeError("Merve gerçek mülakatı yok; işlem iptal.")
                cur.execute(
                    "SELECT count(*) FROM public.ats_candidates WHERE company_id=%s AND name=%s",
                    (COMPANY_ID,KEEP),
                )
                if cur.fetchone()[0] != 1:
                    raise RuntimeError("Merve ATS aday kaydı yok; işlem iptal.")
                cur.execute(
                    "SELECT id FROM public.ats_documents WHERE application_id=ANY(%s)",
                    (app_ids,),
                )
                document_ids = [row[0] for row in cur.fetchall()]
                if len(document_ids) != 6:
                    raise RuntimeError("Beklenen altı belge değişmiş; işlem iptal.")
                cur.execute(
                    "SELECT id FROM public.ats_evaluations WHERE application_id=ANY(%s)",
                    (app_ids,),
                )
                evaluation_ids = [row[0] for row in cur.fetchall()]
                cur.execute(
                    "SELECT id FROM public.ats_offers WHERE application_id=ANY(%s)",
                    (app_ids,),
                )
                offer_ids = [row[0] for row in cur.fetchall()]
                if evaluation_ids or offer_ids:
                    raise RuntimeError("Yeni değerlendirme veya teklif oluşturulmuş; işlem iptal.")

                cur.execute(f"""CREATE TABLE IF NOT EXISTS {ARCHIVE_TABLE} (
                    id BIGSERIAL PRIMARY KEY,
                    run_key TEXT NOT NULL,
                    source_table TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    archived_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )""")
                cur.execute(f"SELECT count(*) FROM {ARCHIVE_TABLE} WHERE run_key=%s", (RUN_KEY,))
                if cur.fetchone()[0]:
                    raise RuntimeError("Önceki temizlik arşivi var; işlem iptal.")

                group_ids = {
                    "candidate":candidate_ids, "application":app_ids,
                    "document":document_ids, "evaluation":evaluation_ids,
                    "offer":offer_ids,
                }
                archived = {}
                for table, key, group in DEPENDENCIES:
                    ids = group_ids[group]
                    cur.execute(
                        f"INSERT INTO {ARCHIVE_TABLE}(run_key,source_table,payload) "
                        f"SELECT %s,%s,to_jsonb(t) FROM public.{table} t "
                        f"WHERE t.{key}=ANY(%s)",
                        (RUN_KEY,table,ids),
                    )
                    archived[table] = cur.rowcount
                if archived["ats_candidates"] != 4 or archived["ats_applications"] != 4 or archived["ats_documents"] != 6:
                    raise RuntimeError("Arşiv toplamları beklenenden farklı; işlem geri alındı.")
                # In case an ATS upload is in flight, abort instead of deleting it.
                for table,expected in {
                    "ats_document_blobs":1,"ats_document_digests":6,
                    "ats_cv_imports":3,"ats_privacy_requests":1,"ats_upload_jobs":1,
                }.items():
                    if archived.get(table) != expected:
                        raise RuntimeError(f"{table} alt kayıt sayısı değişmiş; işlem iptal.")

                for table,key,group in DEPENDENCIES:
                    cur.execute(
                        f"DELETE FROM public.{table} WHERE {key}=ANY(%s)",
                        (group_ids[group],),
                    )
                    if cur.rowcount != archived[table]:
                        raise RuntimeError(table+" arşiv/silme sayısı tutarsız; geri alındı.")
                cur.execute(
                    "SELECT count(*) FROM public.ats_candidates WHERE company_id=%s", (COMPANY_ID,)
                )
                if cur.fetchone()[0] != 20:
                    raise RuntimeError("ATS kalan aday sayısı yanlış; geri alındı.")
                cur.execute(
                    "SELECT count(*) FROM public.ats_applications WHERE company_id=%s", (COMPANY_ID,)
                )
                if cur.fetchone()[0] != 20:
                    raise RuntimeError("ATS kalan başvuru sayısı yanlış; geri alındı.")
                cur.execute(
                    "SELECT count(*) FROM public.interview_links "
                    "WHERE company_id=%s AND name=%s AND status='completed'",
                    (COMPANY_ID,KEEP),
                )
                if cur.fetchone()[0] != 1:
                    raise RuntimeError("Merve gerçek mülakatı koruması başarısız; geri alındı.")
                return {
                    "already_clean": False,"removed":4,"remaining":20,
                    "archive_rows":sum(archived.values()),"archived_by_table":archived,
                }
