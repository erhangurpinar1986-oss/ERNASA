"""Havale / EFT / FAST: founder-managed IBAN, immutable checkout snapshot, founder confirmation,
shared exactly-once activation and the same commission rules as Shopier (synthetic data only)."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import re
import sqlite3

import pytest

from control import billing_payments as bp, sales
from control.billing_catalog import HR_FLOW, quote_amounts
from control.tcmb_rates import ExchangeRate

MIG = Path(__file__).resolve().parents[1] / "migrations" / "control_postgresql"
NOW = datetime(2026, 11, 25, 15, tzinfo=timezone.utc)
RATE = ExchangeRate(Decimal("42.50000000"), date(2026, 11, 25))
VAT = Decimal("0.20")
REP = sales.Actor("rep1", 10)
FOUNDER = sales.Actor("1", None, True)
IBAN = "TR330006100519786457841326"
IBAN2 = "TR460001000000000000012345"

LEGACY = """CREATE TABLE subscriptions(company_id INTEGER PRIMARY KEY, package TEXT, starts TEXT, ends TEXT, status TEXT);
CREATE TABLE service_terms(company_id INTEGER PRIMARY KEY, product TEXT, version TEXT, user_quota INTEGER, operation_quota INTEGER,
 trial_days INTEGER, period TEXT, price_minor INTEGER, currency TEXT, vat_basis INTEGER, payment TEXT NOT NULL);
CREATE TABLE ats_subscriptions(company_id INTEGER PRIMARY KEY, package TEXT, starts TEXT, ends TEXT, status TEXT, user_quota INTEGER);
CREATE TABLE subscription_history(id INTEGER PRIMARY KEY, company_id INTEGER, snapshot TEXT, recorded TEXT DEFAULT CURRENT_TIMESTAMP);"""


def sqlite_sql(sql):
    sql = sql.replace("ernasa_control.", "").replace("ADD COLUMN IF NOT EXISTS", "ADD COLUMN")
    sql = sql.replace("CHECK (iban ~ '^TR[0-9]{24}$')", "")  # PostgreSQL regex check; enforced in code too
    return re.sub(r"CREATE OR REPLACE TRIGGER.*?;", "", sql, flags=re.S)


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("HR_PAYMENTS_ENABLED", "1")
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript("""CREATE TABLE companies(id INTEGER PRIMARY KEY, name TEXT, status TEXT);
        CREATE TABLE users(id INTEGER PRIMARY KEY, role TEXT, active INTEGER);
        INSERT INTO users VALUES(1,'founder',1);
        INSERT INTO companies VALUES(10,'Bayi','Aktif'),(20,'Müşteri A','Aktif'),(21,'Müşteri B','Aktif');""")
    con.executescript(sales.DDL)
    con.executescript(LEGACY)
    con.executescript(sales.SQLITE_GUARDS)
    for name in ("004_billing_quotes.up.sql", "005_billing_payments.up.sql", "006_billing_test.up.sql",
                 "007_bank_transfer.up.sql", "009_commission_fx.up.sql"):
        con.executescript(sqlite_sql((MIG / name).read_text()))
    con.execute("INSERT INTO sales_accounts(id,company_id,name,email,secret,active) VALUES('rep1',10,'rep1','r@x.tr','s',1)")
    return con


def tx(db, fn, **kw):
    db.execute("BEGIN")
    try:
        r = fn(db, **kw)
        db.execute("COMMIT")
        return r
    except Exception:
        db.execute("ROLLBACK")
        raise


def settings(db, iban=IBAN, enabled=True):
    tx(db, bp.save_bank_settings, actor=FOUNDER, bank_name="Synthetic Bank", account_holder="ERNASA Test Ltd.",
       iban=iban, enabled=enabled, now=NOW)


def approved_proposal(db):
    cust = sales.customer(db, REP, {"name": "Firma", "tax_id": "1234567890"})["id"]
    sales.classify(db, FOUNDER, {"id": cust, "classification": "new", "reason": "kontrol"})
    db.execute("INSERT INTO sales_customer_companies VALUES(?,?,?,?)", (cust, 20, 1, 1))
    opp = sales.opportunity(db, REP, {"customer_id": cust, "title": "HR", "kind": "subscription"})["id"]
    prop = sales.proposal(db, REP, {"opportunity_id": opp, "requested_minor": "100", "brief": "b"})["id"]
    sales.submit(db, REP, {"id": prop})
    db.execute("UPDATE sales_proposals SET status='approved', official_minor=100 WHERE id=?", (prop,))
    return prop


def request(db, plan="Growth", rep=True, product="hr_flow", company=None):
    if rep:
        return bp.create_payment_request(db, actor=REP, proposal_id=approved_proposal(db), product=product,
                                         plan_name=plan, period_start=date(2026, 12, 1), now=NOW)
    return bp.create_payment_request(db, actor=FOUNDER, company_id=company or 21, product=product,
                                     plan_name=plan, period_start=date(2026, 12, 1), now=NOW)


def bank_start(db, ref, now=NOW):
    return tx(db, bp.start_bank_checkout, ref=ref, rate=RATE, vat_rate=VAT, now=now)


def confirm(db, checkout_id, txid, gross, actor=FOUNDER):
    return tx(db, bp.confirm_bank_payment, actor=actor, checkout_id=checkout_id, transaction_id=txid,
              gross_minor=gross, now=NOW)


def gross_of(db, checkout_id):
    return db.execute("SELECT q.gross_minor FROM billing_checkouts c JOIN billing_quotes q ON q.id=c.quote_id WHERE c.id=?",
                      (checkout_id,)).fetchone()[0]


def test_iban_is_normalised_validated_and_founder_only(db):
    tx(db, bp.save_bank_settings, actor=FOUNDER, bank_name="Bank", account_holder="ERNASA",
       iban="tr33 0006 1005 1978 6457 8413 26", enabled=True, now=NOW)
    assert bp.bank_settings(db)["iban"] == IBAN
    with pytest.raises(sales.Invalid, match="IBAN"):
        settings(db, iban="TR330006100519786457841327")  # wrong check digits
    with pytest.raises(sales.Invalid, match="IBAN"):
        settings(db, iban="DE89370400440532013000")
    with pytest.raises(sales.Invalid):
        tx(db, bp.save_bank_settings, actor=REP, bank_name="B", account_holder="X", iban=IBAN, enabled=True, now=NOW)
    assert bp.bank_settings(db)["iban"] == IBAN


def test_bank_price_equals_card_price(db):
    settings(db)
    ref = request(db, plan="Growth")
    cid = bank_start(db, ref)
    expected = quote_amounts(HR_FLOW[1], RATE.value, VAT)
    assert gross_of(db, cid) == expected.gross_minor == 1376950  # same amounts_for() as the Shopier path
    seen = {}

    def create(*, title, description, gross_minor, image_url):
        seen["gross"] = gross_minor
        return "P-1", "https://www.shopier.com/P-1"
    tx(db, bp.start_checkout, ref=ref, rate=RATE, vat_rate=VAT, now=NOW, create_product=create, image_url="x")
    assert seen["gross"] == expected.gross_minor


def test_bank_unavailable_when_disabled_or_not_hr_flow(db):
    ref = request(db)
    with pytest.raises(bp.PaymentRejected):
        bank_start(db, ref)  # no settings yet
    settings(db, enabled=False)
    with pytest.raises(bp.PaymentRejected):
        bank_start(db, ref)
    settings(db)
    edu = request(db, product="education", plan="Starter", rep=False)
    with pytest.raises(bp.PaymentRejected):
        bank_start(db, edu)
    assert db.execute("SELECT COUNT(*) FROM billing_checkouts").fetchone()[0] == 0


def test_issued_instructions_keep_iban_snapshot(db):
    settings(db)
    ref = request(db)
    cid = bank_start(db, ref)
    assert bank_start(db, ref) == cid  # live checkout reused, no second quote
    settings(db, iban=IBAN2)
    shown = bp.bank_checkout(db, ref)
    assert shown["id"] == cid and shown["iban"] == IBAN and shown["payment_reference"].startswith(ref)


def test_confirmation_activates_once_and_books_commission_on_approved_basis(db):
    settings(db)
    ref = request(db, plan="Growth")
    cid = bank_start(db, ref)
    assert [r["id"] for r in bp.pending_bank_payments(db)] == [cid]
    gross = gross_of(db, cid)
    assert confirm(db, cid, "DEKONT-001", gross) == "activated"
    assert confirm(db, cid, "DEKONT-001", gross) == "already_processed"
    assert db.execute("SELECT status FROM billing_checkouts WHERE id=?", (cid,)).fetchone()[0] == "paid"
    assert db.execute("SELECT status FROM billing_subscriptions").fetchone()[0] == "active"
    assert db.execute("SELECT COUNT(*) FROM billing_periods").fetchone()[0] == 1
    assert db.execute("SELECT status, user_quota FROM ats_subscriptions WHERE company_id=20").fetchone()[:] == ("Aktif", 3)
    m = db.execute("SELECT rate, amount_minor FROM sales_commissions").fetchall()
    # Growth: basis 249.99 USD x %25 = 62.50 USD, x sale rate 42.50 = 2.656,25 TL fixed
    assert [tuple(r) for r in m] == [(25, 265625)]
    fx = db.execute("SELECT basis_usd, rate, commission_usd, fx_rate, fx_date, amount_minor FROM sales_commission_fx").fetchone()
    assert tuple(str(x) for x in fx) == ("249.99", "25", "62.5", "42.50000000", "2026-11-25", "265625") or \
        (str(fx[2]) in ("62.5", "62.50") and fx[5] == 265625 and str(fx[4]).startswith("2026-11-25"))
    ref_row = db.execute("SELECT reference, net_minor FROM sales_collections").fetchone()
    assert ref_row["reference"] == "bank_transfer:DEKONT-001" and ref_row["net_minor"] == 1147458
    assert bp.pending_bank_payments(db) == []
    with pytest.raises(bp.PaymentRejected):
        confirm(db, cid, "DEKONT-002", gross)  # second transfer for an already paid checkout


def test_wrong_amount_reused_transaction_and_non_founder_are_rejected(db):
    settings(db)
    cid = bank_start(db, request(db))
    gross = gross_of(db, cid)
    with pytest.raises(bp.PaymentRejected, match="tutar"):
        confirm(db, cid, "TX-1", gross - 1)
    with pytest.raises(bp.PaymentRejected):
        confirm(db, cid, "bad id with spaces", gross)
    with pytest.raises(sales.Invalid):
        confirm(db, cid, "TX-1", gross, actor=REP)
    assert db.execute("SELECT COUNT(*) FROM billing_provider_orders").fetchone()[0] == 0
    assert confirm(db, cid, "TX-1", gross) == "activated"
    other = bank_start(db, request(db, rep=False, company=21))
    with pytest.raises(bp.PaymentRejected, match="başka ödeme"):
        confirm(db, other, "TX-1", gross_of(db, other))


def test_late_transfer_on_expired_instruction_still_confirms(db):
    settings(db)
    ref = request(db, rep=False)
    cid = bank_start(db, ref)
    later = NOW + timedelta(minutes=20)
    db.execute("UPDATE billing_checkouts SET status='expired' WHERE id=?", (cid,))
    assert [r["id"] for r in bp.pending_bank_payments(db)] == [cid]
    db.execute("BEGIN")
    assert bp.confirm_bank_payment(db, actor=FOUNDER, checkout_id=cid, transaction_id="LATE-1",
                                   gross_minor=gross_of(db, cid), now=later) == "activated"
    db.execute("COMMIT")
    assert db.execute("SELECT COUNT(*) FROM sales_commissions").fetchone()[0] == 0  # direct sale


def test_card_order_after_bank_collection_does_not_activate_twice(db):
    settings(db)
    ref = request(db)
    bank = bank_start(db, ref)

    def create(*, title, description, gross_minor, image_url):
        return "P-9", "https://www.shopier.com/P-9"
    tx(db, bp.start_checkout, ref=ref, rate=RATE, vat_rate=VAT, now=NOW, create_product=create, image_url="x")
    gross = gross_of(db, bank)
    assert confirm(db, bank, "TX-9", gross) == "activated"
    order = {"id": "o9", "paymentStatus": "paid", "currency": "TRY",
             "totals": {"total": f"{gross // 100}.{gross % 100:02d}"},
             "lineItems": [{"productId": "P-9", "quantity": 1}], "refunds": [], "returns": []}
    with pytest.raises(bp.PaymentRejected, match="daha önce tahsil"):
        tx(db, bp.activate_from_order, order_id="o9", fetch=lambda _: order, contract=bp.OFFICIAL_CONTRACT, now=NOW)
    assert db.execute("SELECT COUNT(*) FROM billing_periods").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM sales_commissions").fetchone()[0] == 1


def test_bank_ready_probe_does_not_break_transaction_without_migration_007():
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.execute("BEGIN")
    con.execute("CREATE TABLE t(x)")
    assert bp.bank_ready(con) is False
    con.execute("INSERT INTO t VALUES(1)")
    con.execute("COMMIT")
    assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
