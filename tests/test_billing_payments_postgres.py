"""Real PostgreSQL + psycopg2 check of migration 005 and the paid-order path (Shopier SIMULATED).

Everything runs in one transaction that is rolled back: no synthetic row outlives the test.
"""
from datetime import date, datetime, timezone
from decimal import Decimal
import os

import psycopg2
from psycopg2.extensions import parse_dsn
import pytest

from control import billing_payments as bp, postgres, render_gateway
from control.tcmb_rates import ExchangeRate


def test_migration_005_and_paid_order_activation_on_postgres(monkeypatch):
    dsn = os.getenv("SALES_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Disposable local PostgreSQL DSN not supplied")
    config = parse_dsn(dsn)
    assert config.get("host") in ("127.0.0.1", "localhost")
    assert config.get("dbname", "").startswith("sales_test_")
    monkeypatch.setenv("HR_PAYMENTS_ENABLED", "1")
    db = postgres.Connection(postgres.Target(dsn))
    try:
        if not db.execute("SELECT to_regnamespace('ernasa_control')").fetchone()[0]:
            with db.raw, db.raw.cursor() as cursor:
                for migration in ("001_schema_draft.up.sql", "002_session_rules.up.sql"):
                    cursor.execute((render_gateway.CONTROL_MIGRATIONS / migration).read_text())
        render_gateway.ensure_sales_schema(db)
        render_gateway.ensure_billing_quote_schema(db)
        render_gateway.ensure_billing_payment_schema(db)
        render_gateway.ensure_billing_payment_schema(db)  # repeatable
        now = datetime(2026, 11, 25, 15, tzinfo=timezone.utc)
        founder = db.execute("SELECT id FROM users WHERE role='founder'").fetchone()
        company_id = db.execute("INSERT INTO companies (name, status) VALUES (?,?) RETURNING id",
                                ("Synthetic payment customer", "Aktif")).fetchone()[0]
        if founder is None:
            db.execute("INSERT INTO users(email,name,secret,role) VALUES(?,?,?,?)",
                       ("synthetic-founder@example.test", "F", "x", "founder"))
        actor = type("A", (), {"id": "1", "founder": True, "company_id": None})()
        ref = bp.create_payment_request(db, actor=actor, company_id=company_id, product="hr_flow",
                                        plan_name="Business", period_start=date(2027, 1, 1), now=now)
        rate = ExchangeRate(Decimal("42.50000000"), date(2026, 11, 25))
        url = bp.start_checkout(db, ref=ref, rate=rate, vat_rate=Decimal("0.20"), now=now,
                                create_product=lambda **k: ("PG-1", "https://www.shopier.com/PG-1"),
                                image_url="https://ernasa.com/control/ernasalogo.png")
        assert url == "https://www.shopier.com/PG-1"
        gross = db.execute("SELECT gross_minor FROM billing_quotes q JOIN billing_checkouts c ON c.quote_id=q.id "
                           "WHERE c.provider_product_id='PG-1'").fetchone()[0]
        order = {"id": "pg-o1", "paymentStatus": "paid", "currency": "TRY",
                 "totals": {"total": f"{gross // 100}.{gross % 100:02d}"},
                 "lineItems": [{"productId": "PG-1", "quantity": 1}], "refunds": [], "returns": []}
        fetch = lambda _id: order
        assert bp.activate_from_order(db, order_id="pg-o1", fetch=fetch, contract=bp.OFFICIAL_CONTRACT, now=now) == "activated"
        assert bp.activate_from_order(db, order_id="pg-o1", fetch=fetch, contract=bp.OFFICIAL_CONTRACT, now=now) == "already_processed"
        period = db.execute("SELECT p.* FROM billing_periods p JOIN billing_subscriptions s ON s.id=p.subscription_id "
                            "WHERE s.company_id=?", (company_id,)).fetchone()
        assert (period["capacity"], period["positions"], period["hr_users"]) == (1000, 20, 5)
        assert tuple(db.execute("SELECT package, starts, ends, status FROM subscriptions WHERE company_id=?",
                                (company_id,)).fetchone()) == ("HR FLOW Business", "2027-01-01", "2027-01-31", "Aktif")
        assert tuple(db.execute("SELECT operation_quota, user_quota FROM service_terms WHERE company_id=?",
                                (company_id,)).fetchone()) == (1000, 5)
        db.execute("SAVEPOINT mutation_attempt")
        with pytest.raises(psycopg2.errors.RaiseException):
            db.execute("UPDATE billing_periods SET capacity=1 WHERE id=?", (period["id"],))
        db.execute("ROLLBACK TO SAVEPOINT mutation_attempt")
        db.execute("SAVEPOINT dup")
        with pytest.raises(psycopg2.errors.UniqueViolation):
            db.execute("INSERT INTO billing_provider_orders VALUES(?,?,?,?,?,?,?,?)",
                       ("shopier_hr", "pg-o1", "other", 1, "TRY", 1, "x", "h"))
        db.execute("ROLLBACK TO SAVEPOINT dup")
    finally:
        db.raw.rollback()
        db.close()


def test_migration_007_bank_transfer_confirmation_on_postgres():
    """Real PostgreSQL 18: 007 installs idempotently through the startup path, the database itself
    rejects a malformed IBAN, issued bank details are immutable and founder confirmation activates
    exactly once. One rolled-back transaction: no synthetic row survives."""
    dsn = os.getenv("SALES_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Disposable local PostgreSQL DSN not supplied")
    config = parse_dsn(dsn)
    assert config.get("host") in ("127.0.0.1", "localhost")
    assert config.get("dbname", "").startswith("sales_test_")
    db = postgres.Connection(postgres.Target(dsn))
    try:
        if not db.execute("SELECT to_regnamespace('ernasa_control')").fetchone()[0]:
            with db.raw, db.raw.cursor() as cursor:
                for migration in ("001_schema_draft.up.sql", "002_session_rules.up.sql"):
                    cursor.execute((render_gateway.CONTROL_MIGRATIONS / migration).read_text())
        render_gateway.ensure_sales_schema(db)
        render_gateway.ensure_billing_quote_schema(db)
        render_gateway.ensure_billing_payment_schema(db)
        render_gateway.ensure_billing_payment_schema(db)  # restart: 005-007 re-run safely
        assert bp.bank_ready(db)
        now = datetime(2026, 11, 25, 15, tzinfo=timezone.utc)
        if db.execute("SELECT id FROM users WHERE role='founder'").fetchone() is None:
            db.execute("INSERT INTO users(email,name,secret,role) VALUES(?,?,?,?)",
                       ("synthetic-founder@example.test", "F", "x", "founder"))
        actor = type("A", (), {"id": "1", "founder": True, "company_id": None})()
        db.execute("SAVEPOINT bad_iban")
        with pytest.raises(psycopg2.errors.CheckViolation):
            db.execute("INSERT INTO billing_bank_settings VALUES(1,'B','H','TR12',FALSE,'1',0)")
        db.execute("ROLLBACK TO SAVEPOINT bad_iban")
        bp.save_bank_settings(db, actor=actor, bank_name="Synthetic Bank", account_holder="ERNASA Test",
                              iban="TR33 0006 1005 1978 6457 8413 26", enabled=True, now=now)
        company_id = db.execute("INSERT INTO companies (name, status) VALUES (?,?) RETURNING id",
                                ("Synthetic bank customer", "Aktif")).fetchone()[0]
        ref = bp.create_payment_request(db, actor=actor, company_id=company_id, product="hr_flow",
                                        plan_name="Growth", period_start=date(2027, 1, 1), now=now)
        rate = ExchangeRate(Decimal("42.50000000"), date(2026, 11, 25))
        checkout_id = bp.start_bank_checkout(db, ref=ref, rate=rate, vat_rate=Decimal("0.20"), now=now)
        shown = bp.bank_checkout(db, ref)
        assert shown["iban"] == "TR330006100519786457841326" and shown["gross_minor"] == 1376950
        assert [r["id"] for r in bp.pending_bank_payments(db)] == [checkout_id]
        db.execute("SAVEPOINT snapshot")
        with pytest.raises(psycopg2.errors.RaiseException):
            db.execute("UPDATE billing_bank_checkout_details SET iban=? WHERE checkout_id=?",
                       ("TR460001000000000000012345", checkout_id))
        db.execute("ROLLBACK TO SAVEPOINT snapshot")
        with pytest.raises(bp.PaymentRejected):
            bp.confirm_bank_payment(db, actor=actor, checkout_id=checkout_id, transaction_id="PG-TX-1",
                                    gross_minor=1376949, now=now)
        assert bp.confirm_bank_payment(db, actor=actor, checkout_id=checkout_id, transaction_id="PG-TX-1",
                                       gross_minor=1376950, now=now) == "activated"
        assert bp.confirm_bank_payment(db, actor=actor, checkout_id=checkout_id, transaction_id="PG-TX-1",
                                       gross_minor=1376950, now=now) == "already_processed"
        assert db.execute("SELECT status FROM billing_subscriptions WHERE company_id=?",
                          (company_id,)).fetchone()[0] == "active"
        assert bp.pending_bank_payments(db) == []
    finally:
        db.raw.rollback()
        db.close()


def test_migration_008_payment_link_sharing_on_postgres(monkeypatch):
    """Real PostgreSQL 18: 008 installs idempotently through the startup path, delivery history is
    append-only, a repeated link request reuses the open link, e-mail result is recorded and the
    founder tracking/status screens render. One rolled-back transaction; e-mail is a stub."""
    dsn = os.getenv("SALES_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Disposable local PostgreSQL DSN not supplied")
    config = parse_dsn(dsn)
    assert config.get("host") in ("127.0.0.1", "localhost")
    assert config.get("dbname", "").startswith("sales_test_")
    monkeypatch.setenv("ERNASA_CONTROL_PUBLIC_ORIGIN", "https://ernasa.com")
    from control import app, billing_share as share, sales, sales_http
    db = postgres.Connection(postgres.Target(dsn))
    try:
        if not db.execute("SELECT to_regnamespace('ernasa_control')").fetchone()[0]:
            with db.raw, db.raw.cursor() as cursor:
                for migration in ("001_schema_draft.up.sql", "002_session_rules.up.sql"):
                    cursor.execute((render_gateway.CONTROL_MIGRATIONS / migration).read_text())
        render_gateway.ensure_sales_schema(db)
        render_gateway.ensure_billing_quote_schema(db)
        render_gateway.ensure_billing_payment_schema(db)
        render_gateway.ensure_billing_payment_schema(db)  # restart: 005-008 re-run safely
        assert share.ready(db)
        now = datetime.now(timezone.utc)
        if db.execute("SELECT id FROM users WHERE role='founder'").fetchone() is None:
            db.execute("INSERT INTO users(email,name,secret,role) VALUES(?,?,?,?)",
                       ("synthetic-founder@example.test", "Kurucu", "x", "founder"))
        fid = str(db.execute("SELECT id FROM users WHERE role='founder'").fetchone()[0])
        founder = sales.Actor(fid, None, True)
        company_id = db.execute("INSERT INTO companies (name, status) VALUES (?,?) RETURNING id",
                                ("Synthetic share customer", "Aktif")).fetchone()[0]
        db.execute("""INSERT INTO company_contacts(company_id,primary_contact,name,title,department,phone,email,kind)
                      VALUES(?,1,'Ayşe Test','GM','Yönetim','0532 111 22 33','ayse@example.test','Şirket yetkilisi')""",
                   (company_id,))
        start = date(2027, 3, 1)
        ref, reused = share.ensure_link(db, founder, company_id=company_id, product="hr_flow", plan_name="Growth",
                                        period_start=start, now=now)
        assert reused is False
        assert share.ensure_link(db, founder, company_id=company_id, product="hr_flow", plan_name="Growth",
                                 period_start=start, now=now) == (ref, True)
        with pytest.raises(sales.Invalid):
            share.ensure_link(db, founder, company_id=company_id, product="hr_flow", plan_name="Business",
                              period_start=start, now=now)
        outbox = []
        out = share.send_email(db, founder, ref, now, send=lambda to, subject, body, idempotency_key: outbox.append(to) or "m-1")
        assert out["status"] == "sent" and outbox == ["ayse@example.test"]
        assert share.whatsapp_url(db, founder, ref, now).startswith("https://wa.me/905321112233?text=")
        assert [e["channel"] for e in share.events(db, ref)] in (["whatsapp", "email"], ["email", "whatsapp"])
        db.execute("SAVEPOINT history")
        with pytest.raises(psycopg2.errors.RaiseException):
            db.execute("UPDATE billing_link_events SET status='failed' WHERE ref=?", (ref,))
        db.execute("ROLLBACK TO SAVEPOINT history")
        assert share.stage(db, ref)[0] == "ready"
        f = lambda action, fields, button='Kaydet': fields + button
        inp = lambda name, label, kind='text', required=True: label
        options = lambda name, label, items: label
        html = sales_http.tracking_section(app, db, founder, f, inp, options, ref)
        assert ref in html and "Ödeme durumu" in html and "Ödeme bağlantısı gönderim geçmişi" in html
        assert "Ayşe Test" in html and "WhatsApp ile gönder" in html
        rep = sales.Actor("no-such-rep", company_id)
        assert share.tracking(db, rep) == ([], [])
    finally:
        db.raw.rollback()
        db.close()


def test_migration_009_usd_commission_fixed_try_on_postgres():
    """Real PostgreSQL 18: 009 installs idempotently; a verified bank collection books a USD-based
    commission fixed in TRY at the sale rate (25.00 USD x 50.77 = 1.269,25 TL), the FX snapshot is
    append-only and the founder report renders. Rolled back; nothing persists."""
    dsn = os.getenv("SALES_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Disposable local PostgreSQL DSN not supplied")
    config = parse_dsn(dsn)
    assert config.get("host") in ("127.0.0.1", "localhost")
    assert config.get("dbname", "").startswith("sales_test_")
    from control import app, commission_report as cr, sales, sales_http
    db = postgres.Connection(postgres.Target(dsn))
    try:
        if not db.execute("SELECT to_regnamespace('ernasa_control')").fetchone()[0]:
            with db.raw, db.raw.cursor() as cursor:
                for migration in ("001_schema_draft.up.sql", "002_session_rules.up.sql"):
                    cursor.execute((render_gateway.CONTROL_MIGRATIONS / migration).read_text())
        render_gateway.ensure_sales_schema(db)
        render_gateway.ensure_billing_quote_schema(db)
        render_gateway.ensure_billing_payment_schema(db)
        render_gateway.ensure_billing_payment_schema(db)  # restart: 005-009 re-run safely
        now = datetime(2026, 11, 27, 9, tzinfo=timezone.utc)
        if db.execute("SELECT id FROM users WHERE role='founder'").fetchone() is None:
            db.execute("INSERT INTO users(email,name,secret,role) VALUES(?,?,?,?)",
                       ("synthetic-founder@example.test", "Kurucu", "x", "founder"))
        founder = sales.Actor(str(db.execute("SELECT id FROM users WHERE role='founder'").fetchone()[0]), None, True)
        dealer = db.execute("INSERT INTO companies (name, status) VALUES (?,?) RETURNING id", ("Synthetic dealer", "Aktif")).fetchone()[0]
        buyer = db.execute("INSERT INTO companies (name, status) VALUES (?,?) RETURNING id", ("Synthetic buyer", "Aktif")).fetchone()[0]
        db.execute("INSERT INTO sales_accounts(id,company_id,name,email,secret,active) VALUES(?,?,?,?,?,1)",
                   ("pg-rep", dealer, "PG Temsilci", "pg-rep@example.test", "x"))
        rep = sales.Actor("pg-rep", dealer)
        cust = sales.customer(db, rep, {"name": "PG Müşteri", "tax_id": "9876543210"})["id"]
        sales.classify(db, founder, {"id": cust, "classification": "new", "reason": "k"})
        db.execute("INSERT INTO sales_customer_companies VALUES(?,?,?,?)", (cust, buyer, int(founder.id), 1))
        opp = sales.opportunity(db, rep, {"customer_id": cust, "title": "HR", "kind": "subscription"})["id"]
        prop = sales.proposal(db, rep, {"opportunity_id": opp, "requested_minor": "100", "brief": "b"})["id"]
        sales.submit(db, rep, {"id": prop})
        db.execute("UPDATE sales_proposals SET status='approved', official_minor=100, discount_minor=0 WHERE id=?", (prop,))
        bp.save_bank_settings(db, actor=founder, bank_name="B", account_holder="ERNASA", iban="TR330006100519786457841326",
                              enabled=True, now=now)
        ref = bp.create_payment_request(db, actor=rep, proposal_id=prop, product="hr_flow", plan_name="Starter",
                                        period_start=date(2027, 4, 1), now=now)
        rate = ExchangeRate(Decimal("50.77000000"), date(2026, 11, 27))
        cid = bp.start_bank_checkout(db, ref=ref, rate=rate, vat_rate=Decimal("0.20"), now=now)
        gross = db.execute("SELECT q.gross_minor FROM billing_checkouts c JOIN billing_quotes q ON q.id=c.quote_id WHERE c.id=?", (cid,)).fetchone()[0]
        assert bp.confirm_bank_payment(db, actor=founder, checkout_id=cid, transaction_id="PG-FX-1", gross_minor=gross, now=now) == "activated"
        fx = db.execute("""SELECT f.basis_usd, f.rate, f.commission_usd, f.fx_rate, f.fx_date, f.amount_minor, m.amount_minor AS m_amount
                           FROM sales_commission_fx f JOIN sales_commissions m ON m.id=f.commission_id""").fetchone()
        assert (fx["basis_usd"], fx["rate"], fx["commission_usd"], fx["fx_rate"]) == (Decimal("99.99"), 25, Decimal("25.00"), Decimal("50.77000000"))
        assert str(fx["fx_date"]) == "2026-11-27" and fx["amount_minor"] == fx["m_amount"] == 126925
        db.execute("SAVEPOINT fx_mut")
        with pytest.raises(psycopg2.errors.RaiseException):
            db.execute("UPDATE sales_commission_fx SET amount_minor=1")
        db.execute("ROLLBACK TO SAVEPOINT fx_mut")
        rows = cr.rows(db, founder)
        assert len(rows) == 1 and rows[0]["window_text"] == "01.12.2026 – 15.12.2026" and rows[0]["rep_name"] == "PG Temsilci"
        assert cr.rows(db, sales.Actor("other-rep", dealer)) == []
        html = sales_http.commission_cards(app, db, founder)
        assert "1.269,25 TL" in html and "$25,00" in html and "50,7700 TL" in html and "PG Temsilci" in html
    finally:
        db.raw.rollback()
        db.close()
