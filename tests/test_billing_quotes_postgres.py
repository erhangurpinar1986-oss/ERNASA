from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os

import psycopg2
from psycopg2.extensions import parse_dsn
import pytest

from control import postgres, render_gateway
from control.billing_quotes import active_quote, issue_quote
from control.tcmb_rates import ExchangeRate


def test_checkout_quote_is_tenant_scoped_time_limited_and_immutable():
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
        render_gateway.ensure_billing_quote_schema(db)
        now = datetime(2026, 11, 25, 15, tzinfo=timezone.utc)
        start = date(2027, 1, 31)
        with db:
            company_id = db.execute(
                "INSERT INTO companies (name, status) VALUES (?,?) RETURNING id",
                ("Synthetic billing quote customer", "Aktif"),
            ).fetchone()[0]
            quote_id = issue_quote(
                db, company_id=company_id, product="hr_flow", plan_name="Starter",
                period_start=start, rate=ExchangeRate(Decimal("48.1234"), now.date()),
                expected_rate_date=now.date(), vat_rate=Decimal("0.20"), now=now,
            )
            row = active_quote(db, company_id=company_id, quote_id=quote_id, now=now)
            assert row["period_end"] == date(2027, 2, 28)
            assert (row["currency"], row["capacity"], row["positions"], row["hr_users"]) == (
                "TRY", 100, 2, 1,
            )
            # Approved Starter sale price 109.99 USD * 48.1234 = 5293.0928 -> 5293.09 TL net
            assert (row["net_minor"], row["vat_minor"], row["gross_minor"]) == (
                529309, 105862, 635171,
            )
            assert active_quote(db, company_id=company_id + 1, quote_id=quote_id, now=now) is None
            assert active_quote(
                db, company_id=company_id, quote_id=quote_id, now=now + timedelta(minutes=15),
            ) is None

            db.execute("SAVEPOINT mutation_attempt")
            with pytest.raises(psycopg2.errors.RaiseException):
                db.execute("UPDATE billing_quotes SET gross_minor=gross_minor WHERE id=?", (quote_id,))
            db.execute("ROLLBACK TO SAVEPOINT mutation_attempt")
            assert active_quote(db, company_id=company_id, quote_id=quote_id, now=now)
            db.raw.rollback()  # Quote and synthetic company must not outlive this test.
        assert active_quote(db, company_id=company_id, quote_id=quote_id, now=now) is None
    finally:
        db.close()
