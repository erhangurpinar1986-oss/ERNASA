"""Opt-in checks against a disposable, explicitly named local sales_test_* DB."""
import os
from pathlib import Path

import psycopg2
from psycopg2.extensions import parse_dsn
import pytest
from uuid import uuid4

from control import app, postgres, render_gateway, sales, sales_http


def test_postgres_migrations_workflow_and_append_only_guards():
    dsn = os.getenv('SALES_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('Disposable local PostgreSQL DSN not supplied')
    config = parse_dsn(dsn)
    assert config.get('host') in ('127.0.0.1','localhost')
    assert config.get('dbname','').startswith('sales_test_')
    root = Path(__file__).resolve().parents[1]
    raw = psycopg2.connect(dsn)
    try:
        with raw, raw.cursor() as cursor:
            cursor.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name='ernasa_control'")
            if cursor.fetchone() is None:
                cursor.execute((root/'migrations/control_postgresql/001_schema_draft.up.sql').read_text(encoding='utf-8'))
                cursor.execute((root/'migrations/control_postgresql/002_session_rules.up.sql').read_text(encoding='utf-8'))
        # This is the exact startup migration path, tested on a disposable PG DB.
        setup = app.connect(postgres.Target(dsn))
        try:
            render_gateway.ensure_sales_schema(setup)
            render_gateway.ensure_sales_schema(setup)  # idempotence
            tables = {r[0] for r in setup.execute(
                "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='ernasa_control'"
            ).fetchall()}
            assert render_gateway.SALES_TABLES <= tables
            assert {'users','companies','sessions'} <= tables
        finally:
            setup.close()
        db = app.connect(postgres.Target(dsn))
        def execute(actor, action, **data):
            with db:
                db.execute('BEGIN IMMEDIATE')
                return sales_http.commands(app,db,actor,{'action':action,**data})
        try:
            with db:
                run = uuid4().hex
                company = db.execute("INSERT INTO companies(name,status) VALUES(?,'Aktif') RETURNING id", ('Sentetik PG Firma '+run,)).fetchone()[0]
                founder = db.execute("SELECT id,email FROM users WHERE role='founder'").fetchone()
                if founder:
                    assert founder['email'] == 'pg-founder@example.test', 'Unrecognized data; refusing test writes'
                    founder_id = founder['id']
                else:
                    founder_id = db.execute("INSERT INTO users(email,name,secret,role) VALUES('pg-founder@example.test','Sentetik',?,'founder') RETURNING id", (app.digest('Synthetic-postgres-only-2026!'),)).fetchone()[0]
                db.execute("INSERT INTO sales_accounts VALUES(?,?,'Sentetik Temsilci',?,?,1)", (run,company,run+'@example.test',app.digest('Synthetic-postgres-only-2026!')))
            f,a = sales.Actor(str(founder_id),None,True),sales.Actor(run,company)
            c = execute(a,'customer',name='Sentetik PG Müşteri',domain=run+'.test')['id']
            execute(f,'classify',id=c,classification='new',reason='Sentetik')
            o = execute(a,'opportunity',customer_id=c,title='PG fırsat',kind='subscription')['id']
            p = execute(a,'proposal',opportunity_id=o,requested_minor=10001,brief='Sentetik kapsam')['id']
            execute(a,'submit',id=p)
            execute(f,'decide',id=p,decision='approved',official_minor=10001,reason='Sentetik karar')
            for period,expected in ((1,2500),(2,500)):
                cid = execute(f,'collection',proposal_id=p,period=period,reference=run+'-'+str(period),net_minor=10001,cost_minor=100,collected_on='2026-01-31',reason='Sentetik dekont')['id']
                assert db.execute('SELECT amount_minor FROM sales_commissions WHERE collection_id=?',(cid,)).fetchone()[0] == expected
            assert len(sales.snapshot(db,a)['commissions']) == 2
            assert sales.snapshot(db,sales.Actor('other-rep',2))['customers'] == []
            with pytest.raises(sales.Invalid):
                execute(a,'decide',id=p,decision='approved',reason='Yetkisiz')
            with pytest.raises(sales.Invalid):
                execute(f,'collection',proposal_id=p,period=2,reference=run+'-duplicate',net_minor=10001,cost_minor=0,collected_on='2026-01-31',reason='Mükerrer dönem')
            with pytest.raises(psycopg2.IntegrityError):
                execute(f,'collection',proposal_id=p,period=3,reference=run+'-1',net_minor=10001,cost_minor=0,collected_on='2026-01-31',reason='Mükerrer referans')
            assert len(sales.snapshot(db,a)['commissions']) == 2
            from datetime import date
            first_mid = db.execute('SELECT m.id FROM sales_commissions m JOIN sales_collections c ON c.id=m.collection_id WHERE c.proposal_id=? AND c.period=1',(p,)).fetchone()[0]
            execute(f,'commission_action',id=first_mid,decision='approved',reason='Sentetik hak ediş onayı')
            with pytest.raises(sales.Invalid):
                with db:
                    sales.commission_action(db,f,{'id':first_mid,'decision':'paid','reason':'Takvim dışı'},today=date(2026,2,16))
            with db:
                db.execute('BEGIN IMMEDIATE')
                sales.commission_action(db,f,{'id':first_mid,'decision':'paid','reason':'Sentetik ödeme işareti'},today=date(2026,2,15))
            execute(f,'hold',collection_id=cid,reason='Sentetik iade')
            mid = db.execute('SELECT id FROM sales_commissions WHERE collection_id=?',(cid,)).fetchone()[0]
            execute(f,'reverse',id=mid,reason='Sentetik kurucu ters kayıt kararı')
            with pytest.raises(sales.Invalid):
                execute(f,'collection',proposal_id=p,period=3,reference=run+'-3',net_minor=10001,cost_minor=0,collected_on='2026-01-31',reason='İptal sonrası engelle')
            # Same customer: upgrades/additional products never create new first-period rates.
            for kind in ('upgrade','addon','project'):
                extra_o = execute(a,'opportunity',customer_id=c,title='Sentetik '+kind,kind=kind)['id']
                extra_p = execute(a,'proposal',opportunity_id=extra_o,requested_minor=10001,brief='Sentetik '+kind)['id']
                execute(a,'submit',id=extra_p)
                execute(f,'decide',id=extra_p,decision='approved',official_minor=10001,reason='Sentetik '+kind)
                extra_c = execute(f,'collection',proposal_id=extra_p,period=1,reference=run+'-'+kind,net_minor=10001,cost_minor=100,collected_on='2026-01-31',reason='Sentetik '+kind)['id']
                accrual = db.execute('SELECT * FROM sales_commissions WHERE collection_id=?',(extra_c,)).fetchone()
                if kind == 'project':
                    assert accrual is None
                    execute(f,'project_commission',collection_id=extra_c,amount_minor=1500,analysis='Sentetik maliyet 5000; kâr 5001',reason='Açık sentetik proje kararı')
                else:
                    assert accrual['rate'] == 0 and accrual['amount_minor'] == 0
            audit_id = db.execute('SELECT id FROM sales_audit WHERE actor_id=? ORDER BY stamp DESC LIMIT 1',(a.id,)).fetchone()[0]
            decision_id = db.execute('SELECT id FROM sales_decisions WHERE proposal_id=?',(p,)).fetchone()[0]
            adjustment_id = db.execute('SELECT id FROM sales_adjustments WHERE commission_id=?',(mid,)).fetchone()[0]
            for table,target in (('sales_audit',audit_id),('sales_decisions',decision_id),('sales_adjustments',adjustment_id)):
                # Only freshly inserted test records; always roll back, even if a guard fails.
                for statement in (f'DELETE FROM {table} WHERE id=?',f'UPDATE {table} SET id=id WHERE id=?'):
                    try:
                        with pytest.raises(psycopg2.Error):
                            db.execute(statement,(target,))
                    finally:
                        db.raw.rollback()
                # Check TRUNCATE protection without issuing a TRUNCATE command.
                row = db.execute("SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='ernasa_control' AND c.relname=? AND (t.tgtype::integer & 32)=32 AND t.tgenabled='O'",(table,)).fetchone()
                assert row[0] == 1
            # Additive sales migration and trigger installation are repeatable.
            with raw,raw.cursor() as cursor:
                cursor.execute('SET LOCAL search_path = ernasa_control, pg_catalog')
                cursor.execute(sales.DDL)
                cursor.execute((root/'migrations/control_postgresql/003_sales_hub.up.sql').read_text(encoding='utf-8'))
        finally:
            db.close()
    finally:
        raw.close()
