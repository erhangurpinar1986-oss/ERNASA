import os
import re
import sqlite3
import ats_schema
from pathlib import Path

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_PATH = Path(__file__).resolve().parent / "ernasa.db"


class PostgresCursorWrapper:
    def __init__(self, cursor):
        self.cursor = cursor

    def _convert_sql(self, sql):
        # Mevcut SQLite sorgularındaki ? işaretlerini PostgreSQL formatına çevir
        sql = sql.replace("?", "%s")

        # SQLite: INSERT OR IGNORE
        # PostgreSQL: INSERT ... ON CONFLICT DO NOTHING
        if re.search(r"INSERT\s+OR\s+IGNORE\s+INTO", sql, flags=re.IGNORECASE):
            sql = re.sub(
                r"INSERT\s+OR\s+IGNORE\s+INTO",
                "INSERT INTO",
                sql,
                flags=re.IGNORECASE
            )

            stripped = sql.rstrip()

            if stripped.endswith(";"):
                stripped = stripped[:-1]

            sql = stripped + " ON CONFLICT DO NOTHING"

        return sql

    def execute(self, sql, params=None):
        sql = self._convert_sql(sql)

        if params is None:
            return self.cursor.execute(sql)

        return self.cursor.execute(sql, params)

    def executemany(self, sql, params):
        sql = self._convert_sql(sql)
        return self.cursor.executemany(sql, params)

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def close(self):
        return self.cursor.close()

    def __getattr__(self, name):
        return getattr(self.cursor, name)


class PostgresConnectionWrapper:
    def __init__(self, connection):
        self.connection = connection

    def cursor(self):
        return PostgresCursorWrapper(self.connection.cursor())

    def commit(self):
        return self.connection.commit()

    def rollback(self):
        return self.connection.rollback()

    def close(self):
        return self.connection.close()

    def __getattr__(self, name):
        return getattr(self.connection, name)


def get_connection():
    # Render'da DATABASE_URL varsa PostgreSQL kullan
    if DATABASE_URL:
        import psycopg2
        from psycopg2.extras import DictCursor

        connection = psycopg2.connect(
            DATABASE_URL,
            cursor_factory=DictCursor,
            connect_timeout=10
        )

        return PostgresConnectionWrapper(connection)

    # Bilgisayarda geliştirme yaparken SQLite çalışmaya devam etsin
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS interview_links (
            token TEXT PRIMARY KEY,
            name TEXT,
            phone TEXT,
            email TEXT,
            company TEXT,
            position TEXT,
            expires_at TEXT,
            created_at TEXT,
            status TEXT,
            cv_text TEXT
        )
    """)
    if DATABASE_URL:
        cursor.execute("""
            ALTER TABLE interview_links
            ADD COLUMN IF NOT EXISTS cv_text TEXT
        """)

    # Var olan aday kayıtlarını koruyarak tarih sütununu ekle.
    if DATABASE_URL:
        cursor.execute("ALTER TABLE interview_links ADD COLUMN IF NOT EXISTS created_at TEXT")
    else:
        cursor.execute("PRAGMA table_info(interview_links)")
        columns = {row[1] for row in cursor.fetchall()}
        if "created_at" not in columns:
            cursor.execute("ALTER TABLE interview_links ADD COLUMN created_at TEXT")

    if DATABASE_URL:
        cursor.execute("ALTER TABLE interview_links ADD COLUMN IF NOT EXISTS company_id BIGINT")
    else:
        cursor.execute("PRAGMA table_info(interview_links)")
        if "company_id" not in {row[1] for row in cursor.fetchall()}:
            cursor.execute("ALTER TABLE interview_links ADD COLUMN company_id BIGINT")
    cursor.execute("CREATE INDEX IF NOT EXISTS interview_links_company ON interview_links(company_id, token)")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS company_positions (
            id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, name TEXT NOT NULL,
            department TEXT NOT NULL, location TEXT NOT NULL, description TEXT NOT NULL,
            qualifications TEXT NOT NULL, open INTEGER NOT NULL CHECK(open IN (0,1)),
            created_at TEXT NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS company_positions_tenant ON company_positions(company_id)")
    ats_schema.initialize(cursor)
    columns = {
        'position_id': 'TEXT REFERENCES company_positions(id)',
        'created_by': 'BIGINT',
        'assigned_user_id': 'BIGINT',
        'evaluation_status': "TEXT NOT NULL DEFAULT 'new'",
        'application_id': 'TEXT REFERENCES ats_applications(id)',
    }
    if not DATABASE_URL:
        cursor.execute("PRAGMA table_info(interview_links)")
        existing_columns = {row["name"] for row in cursor.fetchall()}
    for column, definition in columns.items():
        if DATABASE_URL:
            cursor.execute(f"ALTER TABLE interview_links ADD COLUMN IF NOT EXISTS {column} {definition}")
        elif column not in existing_columns:
            cursor.execute(f"ALTER TABLE interview_links ADD COLUMN {column} {definition}")
    cursor.execute("CREATE INDEX IF NOT EXISTS interview_links_position ON interview_links(company_id,position_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS interview_links_usage ON interview_links(company_id,created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS interview_links_application ON interview_links(company_id,application_id)")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS recruitment_audit (
            id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
            event TEXT NOT NULL, target TEXT NOT NULL, recorded_at TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS interview_questions (
            token TEXT NOT NULL REFERENCES interview_links(token),
            question_number INTEGER NOT NULL CHECK(question_number BETWEEN 1 AND 10),
            question TEXT NOT NULL, PRIMARY KEY(token,question_number)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ik_portal_sessions (
            token_hash TEXT PRIMARY KEY,
            slug TEXT NOT NULL,
            company_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            access_version TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS candidate_sequence (
            id INTEGER PRIMARY KEY,
            last_number INTEGER NOT NULL
        )
    """)

    if DATABASE_URL:
        cursor.execute("""
            INSERT INTO candidate_sequence (id, last_number)
            VALUES (1, 0)
            ON CONFLICT (id) DO NOTHING
        """)
    else:
        cursor.execute("""
            INSERT OR IGNORE INTO candidate_sequence (id, last_number)
            VALUES (1, 0)
        """)
    if DATABASE_URL:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS interview_answers (
                id SERIAL PRIMARY KEY,
                token TEXT NOT NULL,
                question_number INTEGER NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
    else:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS interview_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL,
                question_number INTEGER NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS interview_reports (
            token TEXT PRIMARY KEY,
            job_fit_analysis TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    # ERNASA Shield - IK giris denemeleri
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ik_login_attempts (
            id TEXT PRIMARY KEY,
            ip_hash TEXT NOT NULL,
            attempted_at TEXT NOT NULL
        )
    """)

    # ERNASA Shield - Guvenli IK oturumlari
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ik_sessions (
            token_hash TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            last_activity_at TEXT
        )
    """)

    if DATABASE_URL:
        cursor.execute("""
            ALTER TABLE ik_sessions
            ADD COLUMN IF NOT EXISTS last_activity_at TEXT
        """)
    else:
        cursor.execute("PRAGMA table_info(ik_sessions)")
        columns = {row[1] for row in cursor.fetchall()}
        if "last_activity_at" not in columns:
            cursor.execute(
                "ALTER TABLE ik_sessions ADD COLUMN last_activity_at TEXT"
            )

    connection.commit()
    connection.close()
