"""Persistent login throttle against a disposable PostgreSQL (CI service only, synthetic data)."""
import os
from pathlib import Path

import psycopg2
from psycopg2.extensions import parse_dsn
import pytest

from control import app, login_throttle, postgres


def disposable_dsn():
    dsn = os.getenv('SALES_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('Disposable local PostgreSQL DSN not supplied')
    config = parse_dsn(dsn)
    assert config.get('host') in ('127.0.0.1', 'localhost')
    assert config.get('dbname', '').startswith('sales_test_')
    root = Path(__file__).resolve().parents[1]
    raw = psycopg2.connect(dsn)
    try:
        with raw, raw.cursor() as cursor:
            cursor.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name='ernasa_control'")
            if cursor.fetchone() is None:
                cursor.execute((root / 'migrations/control_postgresql/001_schema_draft.up.sql').read_text(encoding='utf-8'))
                cursor.execute((root / 'migrations/control_postgresql/002_session_rules.up.sql').read_text(encoding='utf-8'))
            cursor.execute('DROP TABLE IF EXISTS ernasa_control.security_login_failures, ernasa_control.security_trusted_devices')
    finally:
        raw.close()
    return dsn


def test_postgres_store_persists_and_protects_known_devices():
    dsn = disposable_dsn()
    target = postgres.Target(dsn)

    def throttle():
        return login_throttle.LoginThrottle('control', login_throttle.DbStore(lambda: app.connect(target)))

    first = throttle()
    device = first.success('founder@example.test')
    for _ in range(3):
        first.failure('founder@example.test')
    restarted = throttle()  # new process state, same database
    assert restarted.check('founder@example.test') > 0
    assert restarted.check('founder@example.test', device) == 0
    assert restarted.check('someone.else@example.test') == 0
    raw = psycopg2.connect(dsn)
    try:
        with raw, raw.cursor() as cursor:
            cursor.execute("SELECT to_regclass('ernasa_control.security_login_failures') IS NOT NULL")
            assert cursor.fetchone()[0] is True
            cursor.execute('SELECT account_key FROM ernasa_control.security_login_failures')
            assert 'founder' not in repr(cursor.fetchall())
    finally:
        raw.close()
    # Idempotent DDL: a third instance creates nothing new and keeps the counter.
    assert throttle().check('founder@example.test') > 0
