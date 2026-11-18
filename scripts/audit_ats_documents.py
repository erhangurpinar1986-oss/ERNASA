"""Read-only audit of ATS CV file availability; NEVER prints CV content or personal data.

Run from the ERNASA application directory with the same DATABASE_URL and
ERNASA_IK_UPLOAD_DIRECTORY settings as the web service.

    python scripts/audit_ats_documents.py

This tool does not change application records, files, or migrations.
"""
from collections import Counter
from contextlib import closing
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database import get_connection  # noqa: E402
from ats import document_path  # noqa: E402


def audit(upload_root):
    counters = Counter()
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        # Compatible with the currently deployed schema, before the new archive table exists.
        if os.environ.get("DATABASE_URL"):
            cursor.execute("""SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema=current_schema() AND table_name='ats_document_blobs'
            ) AS archive_exists""")
            archive_exists = bool(cursor.fetchone()["archive_exists"])
        else:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ats_document_blobs'")
            archive_exists = cursor.fetchone() is not None
        if archive_exists:
            cursor.execute("""SELECT d.id,d.company_id,d.filename,
                        b.document_id AS archive_id
                FROM ats_documents d
                LEFT JOIN ats_document_blobs b ON b.document_id=d.id
                    AND b.company_id=d.company_id AND b.application_id=d.application_id""")
        else:
            cursor.execute("""SELECT id,company_id,filename,NULL AS archive_id
                FROM ats_documents""")
        for entry in cursor.fetchall():
            counters["total"] += 1
            base = document_path(upload_root, entry["company_id"], entry["id"])
            suffix = Path(entry["filename"]).suffix.lower()
            path = base.with_suffix(suffix)
            has_file = path.is_file() or base.is_file()
            archived = bool(entry["archive_id"])
            if has_file:
                counters["file_present"] += 1
                if not archived:
                    counters["legacy_unarchived_file"] += 1
            elif archived:
                counters["missing_disk_recoverable_from_archive"] += 1
            else:
                counters["missing_disk_and_archive"] += 1
    return counters


def main():
    root = Path(os.environ.get("ERNASA_IK_UPLOAD_DIRECTORY") or "uploads")
    counts = audit(root)
    for key in ("total", "file_present", "legacy_unarchived_file",
                "missing_disk_recoverable_from_archive",
                "missing_disk_and_archive"):
        print(f"{key}: {counts[key]}")


if __name__ == "__main__":
    main()
