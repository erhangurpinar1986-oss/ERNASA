"""PostgreSQL connection for the CONTROL repository."""
from dataclasses import dataclass

import psycopg2
from psycopg2.extras import DictCursor


@dataclass(frozen=True)
class Target:
    dsn: str


def statement(sql):
    sql = sql.replace("lower(hex(randomblob(16)))", "replace(gen_random_uuid()::text, '-', '')")
    sql = sql.replace("group_concat(g.code)", "string_agg(g.code, ',')")
    sql = sql.replace("u.name COLLATE NOCASE", "lower(u.name)")
    sql = sql.replace("instr(lower(action),lower(?))", "strpos(lower(action),lower(?)) > 0")
    sql = sql.replace("instr(lower(actor),lower(?))", "strpos(lower(actor),lower(?)) > 0")
    return sql.replace("%", "%%").replace("?", "%s")


class Connection:
    def __init__(self, target):
        self.raw = psycopg2.connect(
            target.dsn, connect_timeout=8, options="-c search_path=ernasa_control,pg_catalog",
            cursor_factory=DictCursor,
        )

    def execute(self, sql, params=()):
        cursor = self.raw.cursor()
        if sql == "BEGIN IMMEDIATE":
            # Serialize CONTROL writes as the previous SQLite transaction did.
            cursor.execute("SELECT pg_advisory_xact_lock(184093, 47621)")
        else:
            cursor.execute(statement(sql), params)
        return cursor

    def executemany(self, sql, params):
        cursor = self.raw.cursor()
        cursor.executemany(statement(sql), params)
        return cursor

    def commit(self):
        self.raw.commit()

    def close(self):
        self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.raw.commit()
        else:
            self.raw.rollback()
