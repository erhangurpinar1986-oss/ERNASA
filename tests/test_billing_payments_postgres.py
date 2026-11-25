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
