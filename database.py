import os
import re
import sqlite3
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
            cursor_factory=DictCursor
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
            status TEXT
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

    connection.commit()
    connection.close()