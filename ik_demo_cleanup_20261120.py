"""One-time, audited ERNASA YAZILIM demo interview cleanup.

Only removes the 17 known non-Merve interview-link records for company_id=2.
Archives every affected row in the same PostgreSQL transaction before deleting it.
Never touches ATS applications/documents, training, Sales Hub, or another company.
The temporary startup hook runs only with an explicit Render environment switch.
"""
import os
from contextlib import closing

COMPANY_ID = 2
KEEP_NAME = "Merve Saye Aydoğdu Sipahi"
EXPECTED_BEFORE = 18
EXPECTED_DELETIONS = 17
RUN_KEY = "ernasa_ik_keep_merve_20261120"
ARCHIVE = "ernasa_control.ik_demo_cleanup_archive_20261120"
# All known public tables with an interview token column, including non-FK links.
CHILD_TABLES = (
    "ats_interview_cv_sources",
    "ik_cv_invites",
    "ik_ernasa_invites",
    "ik_interview_assessments",
    "ik_interview_template_snapshots",
    "ik_invitation_email_sends",
    "ik_partner_cv_evidence",
    "ik_partner_extra_questions",
    "ik_partner_interviews",
    "ik_partner_introductions",
    "interview_answers",
    "interview_questions",
    "interview_reports",
)


def cleanup(dsn=None):
    """Safely archive and remove only the 17 unwanted demo interview records.

    Aborts and rolls back everything if live counts, protected data, or
    archive/delete counts differ from the expected snapshot.
    """
    dsn = dsn or os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("PostgreSQL bağlantısı yok; temizlik yapılmadı.")
    import psycopg2

    with closing(psycopg2.connect(dsn, connect_timeout=10)) as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '20000ms'")
                cur.execute("SELECT pg_advisory_xact_lock(3292026, 1120)")
                cur.execute(
                    "SELECT token,name,status FROM public.interview_links "
                    "WHERE company_id=%s ORDER BY token FOR UPDATE",
                    (COMPANY_ID,),
                )
                rows = cur.fetchall()
                if len(rows) == 1 and rows[0][1] == KEEP_NAME and rows[0][2] == "completed":
                    return {"already_clean": True, "kept": 1, "removed": 0}
                if len(rows) != EXPECTED_BEFORE:
                    raise RuntimeError("Beklenen 18 aday kaydı değişmiş; işlem iptal edildi.")
                protected = [r for r in rows if r[1] == KEEP_NAME and r[2] == "completed"]
                if len(protected) != 1:
                    raise RuntimeError("Merve kaydı tekil veya tamamlanmış değil; işlem iptal edildi.")
                keeper = protected[0][0]
                targets = [r[0] for r in rows if r[0] != keeper]
                if len(targets) != EXPECTED_DELETIONS or len(set(targets)) != EXPECTED_DELETIONS:
                    raise RuntimeError("Temizlenecek 17 kayıt tekil değil; işlem iptal edildi.")
                # Keep the real interview + report, answers and assessment intact.
                baseline = {}
                for table in ("interview_answers", "interview_reports", "ik_interview_assessments"):
                    cur.execute(f"SELECT count(*) FROM public.{table} WHERE token=%s", (keeper,))
                    baseline[table] = cur.fetchone()[0]
                if baseline != {
                    "interview_answers": 10,
                    "interview_reports": 1,
                    "ik_interview_assessments": 1,
                }:
                    raise RuntimeError("Merve mülakatının bağlı kayıtları değişmiş; işlem iptal edildi.")
                cur.execute(f"""CREATE TABLE IF NOT EXISTS {ARCHIVE} (
                    id BIGSERIAL PRIMARY KEY,
                    run_key TEXT NOT NULL,
                    source_table TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    archived_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )""")
                cur.execute(f"SELECT count(*) FROM {ARCHIVE} WHERE run_key=%s", (RUN_KEY,))
                if cur.fetchone()[0]:
                    raise RuntimeError("Önceki bir temizlik arşivi mevcut; tekrar işlem yapılmadı.")
                archived = {}
                for table in CHILD_TABLES:
                    cur.execute(
                        f"INSERT INTO {ARCHIVE}(run_key,source_table,payload) "
                        f"SELECT %s,%s,to_jsonb(t) FROM public.{table} t "
                        "WHERE t.token=ANY(%s)",
                        (RUN_KEY, table, targets),
                    )
                    archived[table] = cur.rowcount
                cur.execute(
                    f"INSERT INTO {ARCHIVE}(run_key,source_table,payload) "
                    "SELECT %s,'interview_links',to_jsonb(t) "
                    "FROM public.interview_links t "
                    "WHERE t.company_id=%s AND t.token=ANY(%s)",
                    (RUN_KEY, COMPANY_ID, targets),
                )
                archived["interview_links"] = cur.rowcount
                if archived["interview_links"] != EXPECTED_DELETIONS:
                    raise RuntimeError("Arşivlenecek aday sayısı tutarsız.")
                # FK children first; preserve all external ATS application and CV data.
                for table in CHILD_TABLES:
                    cur.execute(
                        f"DELETE FROM public.{table} WHERE token=ANY(%s)", (targets,)
                    )
                    if cur.rowcount != archived[table]:
                        raise RuntimeError("Arşiv/silme sayısı uyuşmuyor: " + table)
                cur.execute(
                    "DELETE FROM public.interview_links "
                    "WHERE company_id=%s AND token=ANY(%s)",
                    (COMPANY_ID, targets),
                )
                if cur.rowcount != EXPECTED_DELETIONS:
                    raise RuntimeError("Tam 17 aday silinemedi; işlem geri alındı.")
                cur.execute(
                    "SELECT token,name,status FROM public.interview_links WHERE company_id=%s",
                    (COMPANY_ID,),
                )
                survivors = cur.fetchall()
                if len(survivors) != 1 or survivors[0] != (keeper, KEEP_NAME, "completed"):
                    raise RuntimeError("Korunan aday doğrulaması başarısız; işlem geri alındı.")
                for table, expected in baseline.items():
                    cur.execute(f"SELECT count(*) FROM public.{table} WHERE token=%s", (keeper,))
                    if cur.fetchone()[0] != expected:
                        raise RuntimeError("Merve raporu veya yanıtları değişmiş; işlem geri alındı.")
                return {
                    "already_clean": False, "kept": 1,
                    "removed": EXPECTED_DELETIONS, "archive_rows": sum(archived.values()),
                    "archived_by_table": archived,
                }
