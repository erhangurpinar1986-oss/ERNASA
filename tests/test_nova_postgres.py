"""Opt-in NOVA checks on a disposable sales_test_* PostgreSQL DB (CI service container)."""
import os
from pathlib import Path

import pytest

psycopg2 = pytest.importorskip('psycopg2')
from psycopg2.extensions import parse_dsn  # noqa: E402

from control import app, nova, postgres, render_gateway, sales  # noqa: E402

F = sales.Actor('1', None, True)


def test_nova_startup_migration_rules_and_guards_on_postgres():
    dsn = os.getenv('SALES_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('Disposable local PostgreSQL DSN not supplied')
    config = parse_dsn(dsn)
    assert config.get('host') in ('127.0.0.1', 'localhost') and config.get('dbname', '').startswith('sales_test_')
    root = Path(__file__).resolve().parents[1]
    raw = psycopg2.connect(dsn)
    try:
        with raw, raw.cursor() as cursor:
            cursor.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name='ernasa_control'")
            if cursor.fetchone() is None:
                cursor.execute((root / 'migrations/control_postgresql/001_schema_draft.up.sql').read_text(encoding='utf-8'))
                cursor.execute((root / 'migrations/control_postgresql/002_session_rules.up.sql').read_text(encoding='utf-8'))
    finally:
        raw.close()
    setup = app.connect(postgres.Target(dsn))
    try:
        render_gateway.ensure_sales_schema(setup)
        render_gateway.ensure_nova_schema(setup)
        render_gateway.ensure_nova_schema(setup)          # idempotent restart
        tables = {r[0] for r in setup.execute(
            "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='ernasa_control' AND tablename LIKE 'nova%'").fetchall()}
        assert nova.TABLES <= tables
    finally:
        setup.close()
    db = app.connect(postgres.Target(dsn))
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            assert nova.settings(db)['kill_switch'] == 1
            suffix = os.urandom(4).hex()
            row = {'legal_name': f'PG Sentetik {suffix} A.Ş.', 'city': 'İzmir', 'website': f'https://pg{suffix}.example',
                   'email': f'ik@pg{suffix}.example', 'email_source_url': f'https://pg{suffix}.example/iletisim',
                   'open_jobs': 'İK Uzmanı', 'job_source_url': f'https://pg{suffix}.example/kariyer', 'job_seen_on': '2026-11-20',
                   'source_url': f'https://pg{suffix}.example', 'verified_on': '2026-11-27'}
            created = nova.add_company(db, F, row, synthetic=True)
            assert created['stage'] == 'verified'
            assert 'duplicate' in nova.add_company(db, F, row, synthetic=True)
            nova.draft_email(db, F, {'company_id': created['id'], 'subject': 'Tanışma', 'body': 'Merhaba'})
            nova.suppress(db, 'pg-test', 'domain', f'pg{suffix}.example', 'Sentetik ret', 'test')
            assert nova.company(db, created['id'])['stage'] == 'do_not_contact'
            nova.daily_report(db)
            nova.metrics(db)
        with pytest.raises(psycopg2.Error):
            with db:
                db.execute('DELETE FROM nova_suppressions')
        with pytest.raises(psycopg2.Error):
            with db:
                db.execute('UPDATE nova_events SET detail=?', ('x',))
    finally:
        db.close()
