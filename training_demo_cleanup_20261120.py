"""One-time, reversible cleanup of seven ERNASA demo training invitations.

Scope is restricted to the seven Erhan training invite hashes for company_id=2.
Retains Merve Saye Aydoğdu Sipahi's exact V9 invitation. Every deleted row is
saved transactionally as JSONB in CONTROL's dedicated archive schema.
No interview, ATS, sales, or unrelated training records can be removed.
"""
import os
from contextlib import closing

COMPANY_ID = 2
KEEP_RECIPIENT = "Merve Saye Aydoğdu Sipahi"
REMOVE_NAMES = frozenset(("ERHAN GÜRPINAR", "Erhan Gürpınar"))
RUN_KEY = "ernasa_training_seven_demo_20261120"
ARCHIVE_TABLE = "ernasa_control.training_demo_cleanup_archive_20261120"

# Known training token_hash tables; only static SQL identifiers are used.
CHILD_TABLES = (
    "ernasa_training_answers",
    "ernasa_training_audio",
    "ernasa_training_media_context",
    "ernasa_training_video",
    "ernasa_training_v9_activity",
    "ernasa_training_v9_progress",
    "ernasa_training_assignments",
)
EXPECTED_COUNTS = {
    "ernasa_training_answers": 0,
    "ernasa_training_audio": 0,
    "ernasa_training_media_context": 0,
    "ernasa_training_video": 0,
    "ernasa_training_v9_activity": 0,
    "ernasa_training_v9_progress": 1,
    "ernasa_training_assignments": 7,
    "ernasa_training_invites": 7,
}


def cleanup(dsn=None):
    """Archive then atomically remove seven demo training invitations.

    In any mismatch, raise and roll the entire transaction back. A repeat run
    after successful deletion is a no-op. Production activation is explicit.
    """
    dsn = dsn or os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("PostgreSQL bağlantısı yok; eğitim temizliği durduruldu.")
    import psycopg2

    with closing(psycopg2.connect(dsn, connect_timeout=10)) as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '20000ms'")
                cur.execute("SELECT pg_advisory_xact_lock(3292026, 1122)")
                cur.execute(
                    "SELECT i.token_hash,i.recipient,a.company_id,a.program_id "
                    "FROM public.ernasa_training_invites i "
                    "JOIN public.ernasa_training_assignments a "
                    "ON a.token_hash=i.token_hash "
                    "ORDER BY i.token_hash FOR UPDATE OF i,a"
                )
                rows = cur.fetchall()
                if len(rows) == 1 and rows[0][1:] == (KEEP_RECIPIENT, COMPANY_ID, "ernasa_v9"):
                    return {"already_clean": True, "removed": 0, "remaining": 1}
                if len(rows) != 8:
                    raise RuntimeError("Beklenen sekiz eğitim daveti değişmiş; işlem iptal.")
                cur.execute("SELECT count(*) FROM public.ernasa_training_invites")
                if cur.fetchone()[0] != 8:
                    raise RuntimeError("Eşleşmeyen eğitim daveti mevcut; işlem iptal.")
                keep = [r for r in rows if r[1] == KEEP_RECIPIENT]
                targets = [r for r in rows if r[1] in REMOVE_NAMES]
                if len(keep) != 1 or keep[0][2:] != (COMPANY_ID, "ernasa_v9"):
                    raise RuntimeError("Merve'nin V9 eğitim kaydı koruma koşulunu karşılamıyor.")
                if len(targets) != 7 or any(r[2] != COMPANY_ID for r in targets):
                    raise RuntimeError("Yedi demo davetinin firması veya sayısı değişmiş.")
                if sorted(r[1] for r in targets).count("ERHAN GÜRPINAR") != 3:
                    raise RuntimeError("Büyük harfli üç demo daveti eşleşmiyor.")
                if sorted(r[1] for r in targets).count("Erhan Gürpınar") != 4:
                    raise RuntimeError("Dört demo daveti eşleşmiyor.")
                if sum(r[3] == "ernasa_v9" for r in targets) != 1:
                    raise RuntimeError("Demo V9 davet sayısı değişmiş.")
                if any(r[3] not in ("ernasa_v9", "b2b_satis") for r in targets):
                    raise RuntimeError("Beklenmeyen eğitim programı bulundu.")
                target_hashes = [r[0] for r in targets]
                keeper_hash = keep[0][0]
                if len(set(target_hashes + [keeper_hash])) != 8:
                    raise RuntimeError("Eğitim davetleri tekil değil.")

                # Snapshot Merve's related rows; these must survive unchanged.
                protected = {}
                for table in CHILD_TABLES:
                    cur.execute(
                        f"SELECT count(*) FROM public.{table} WHERE token_hash=%s",
                        (keeper_hash,),
                    )
                    protected[table] = cur.fetchone()[0]
                if protected["ernasa_training_assignments"] != 1:
                    raise RuntimeError("Merve'nin V9 ders ataması yok; iptal.")

                cur.execute(f"""CREATE TABLE IF NOT EXISTS {ARCHIVE_TABLE} (
                    id BIGSERIAL PRIMARY KEY,
                    run_key TEXT NOT NULL,
                    source_table TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    archived_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )""")
                cur.execute(f"SELECT count(*) FROM {ARCHIVE_TABLE} WHERE run_key=%s", (RUN_KEY,))
                if cur.fetchone()[0]:
                    raise RuntimeError("Bu eğitim temizliğinin arşivi önceden var; iptal.")

                archived = {}
                for table in CHILD_TABLES + ("ernasa_training_invites",):
                    cur.execute(
                        f"INSERT INTO {ARCHIVE_TABLE}(run_key,source_table,payload) "
                        f"SELECT %s,%s,to_jsonb(t) FROM public.{table} t "
                        "WHERE t.token_hash=ANY(%s)",
                        (RUN_KEY, table, target_hashes),
                    )
                    archived[table] = cur.rowcount
                if archived != EXPECTED_COUNTS:
                    raise RuntimeError("Eğitim arşiv sayıları beklenenden farklı; geri alındı.")

                # Children first, then invite parents. The archive is transactionally
                # inseparable from deletion; no rows disappear on partial failure.
                for table in CHILD_TABLES + ("ernasa_training_invites",):
                    cur.execute(
                        f"DELETE FROM public.{table} WHERE token_hash=ANY(%s)",
                        (target_hashes,),
                    )
                    if cur.rowcount != archived[table]:
                        raise RuntimeError(table + " arşiv/silme sayısı eşleşmedi; geri alındı.")

                cur.execute(
                    "SELECT i.token_hash,i.recipient,a.company_id,a.program_id "
                    "FROM public.ernasa_training_invites i "
                    "JOIN public.ernasa_training_assignments a ON a.token_hash=i.token_hash"
                )
                survivors = cur.fetchall()
                if survivors != [(keeper_hash, KEEP_RECIPIENT, COMPANY_ID, "ernasa_v9")]:
                    raise RuntimeError("Merve tek kalan eğitim daveti değil; geri alındı.")
                for table,count in protected.items():
                    cur.execute(
                        f"SELECT count(*) FROM public.{table} WHERE token_hash=%s",
                        (keeper_hash,),
                    )
                    if cur.fetchone()[0] != count:
                        raise RuntimeError("Merve'nin eğitim verisi değişti; geri alındı.")
                return {
                    "already_clean": False,
                    "removed": 7,
                    "remaining": 1,
                    "archive_rows": sum(archived.values()),
                    "archived_by_table": archived,
                }
