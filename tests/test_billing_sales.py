"""Verified payment -> Sales Hub collection + automatic commission (SIMULATED Shopier responses)."""
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import re
import sqlite3

import pytest

from control import billing_payments as bp, sales
from control.tcmb_rates import ExchangeRate

MIG = Path(__file__).resolve().parents[1] / "migrations" / "control_postgresql"
NOW = datetime(2026, 11, 25, 15, tzinfo=timezone.utc)
RATE = ExchangeRate(Decimal("42.50000000"), date(2026, 11, 25))
CONTRACT = replace(bp.PROVISIONAL_CONTRACT, confirmed=True)
REP = sales.Actor("rep1", 10)
REP2 = sales.Actor("rep2", 10)
FOUNDER = sales.Actor("1", None, True)


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
    con.executescript(sales.SQLITE_GUARDS)
    for name in ("004_billing_quotes.up.sql", "005_billing_payments.up.sql"):
        sql = re.sub(r"CREATE OR REPLACE TRIGGER.*?;", "", (MIG / name).read_text().replace("ernasa_control.", ""), flags=re.S)
        con.executescript(sql)
    for rep in ("rep1", "rep2"):
        con.execute("INSERT INTO sales_accounts(id,company_id,name,email,secret,active) VALUES(?,?,?,?,?,1)",
                    (rep, 10, rep, rep + "@x.tr", "s"))
    return con


def approved_proposal(db, rep=REP, kind="subscription", classification="new", company=20, tax="1234567890"):
    cust = sales.customer(db, rep, {"name": "Firma " + tax, "tax_id": tax})["id"]
    sales.classify(db, FOUNDER, {"id": cust, "classification": classification, "reason": "kontrol"})
    db.execute("INSERT OR IGNORE INTO sales_customer_companies VALUES(?,?,?,?)", (cust, company, 1, 1))
    opp = sales.opportunity(db, rep, {"customer_id": cust, "title": "HR", "kind": kind})["id"]
    prop = sales.proposal(db, rep, {"opportunity_id": opp, "requested_minor": "100", "brief": "b"})["id"]
    sales.submit(db, rep, {"id": prop})
    db.execute("UPDATE sales_proposals SET status='approved', official_minor=100 WHERE id=?", (prop,))
    return cust, opp, prop


def pay(db, checkout_id, oid):
    ch = db.execute("SELECT * FROM billing_checkouts WHERE id=?", (checkout_id,)).fetchone()
    gross = db.execute("SELECT gross_minor FROM billing_quotes WHERE id=?", (ch["quote_id"],)).fetchone()[0]
    o = {"id": oid, "paymentStatus": "paid", "currency": "TRY",
         "totals": {"total": f"{gross // 100}.{gross % 100:02d}"},
         "lineItems": [{"productId": ch["provider_product_id"], "quantity": 1}], "refunds": [], "returns": []}
    db.execute("BEGIN")
    try:
        r = bp.activate_from_order(db, order_id=oid, fetch=lambda _: o, contract=CONTRACT, now=NOW)
        db.execute("COMMIT")
        return r
    except Exception:
        db.execute("ROLLBACK")
        raise


def link(db, prop, pid, rep=REP, plan="Business", start=date(2026, 12, 1), product="hr_flow"):
    return bp.create_rep_checkout(db, actor=rep, proposal_id=prop, product=product, plan_name=plan,
                                  period_start=start, rate=RATE, expected_rate_date=RATE.published_on,
                                  vat_rate=Decimal("0.20"), provider_product_id=pid, now=NOW)


def test_first_period_25_then_5_on_net_basis_and_visible_only_to_owner(db):
    _, _, prop = approved_proposal(db)
    c1 = link(db, prop, "P-1")
    assert pay(db, c1, "o1") == "activated"
    c2 = link(db, prop, "P-2", start=date(2027, 1, 1))
    pay(db, c2, "o2")
    rows = bp.rep_sales(db, REP)
    assert [r["rate"] for r in rows] == [5, 25] or sorted(r["rate"] for r in rows) == [5, 25]
    net = db.execute("SELECT net_minor FROM billing_quotes q JOIN billing_checkouts c ON c.quote_id=q.id WHERE c.id=?", (c1,)).fetchone()[0]
    first = next(r for r in rows if r["rate"] == 25)
    assert first["basis_minor"] == net and first["commission_minor"] == (net * 25 + 50) // 100
    assert first["commission_window"] == "2026-12-01 – 2026-12-15"
    assert first["customer"].startswith("Firma") and first["plan_name"] == "Business"
    assert bp.rep_sales(db, REP2) == []
    assert len(bp.rep_sales(db, FOUNDER)) == 2


def test_duplicate_order_creates_no_second_collection_or_commission(db):
    _, _, prop = approved_proposal(db)
    c1 = link(db, prop, "P-1")
    pay(db, c1, "o1")
    assert pay(db, c1, "o1") == "already_processed"
    assert db.execute("SELECT COUNT(*) FROM sales_commissions").fetchone()[0] == 1


def test_direct_sale_has_no_commission(db):
    from control.billing_quotes import issue_quote
    q = issue_quote(db, company_id=21, product="education", plan_name="Starter", period_start=date(2026, 12, 1),
                    rate=RATE, expected_rate_date=RATE.published_on, vat_rate=Decimal("0.20"), now=NOW)
    db.execute("INSERT INTO billing_checkouts VALUES('d1',?,21,'shopier_hr','P-D','ref-d','pending',1)", (q,))
    assert pay(db, "d1", "od") == "activated"
    assert db.execute("SELECT COUNT(*) FROM sales_commissions").fetchone()[0] == 0


def test_existing_customer_and_addon_get_zero_automatic_commission(db):
    _, _, prop = approved_proposal(db, classification="existing")
    pay(db, link(db, prop, "P-1"), "o1")
    _, _, prop2 = approved_proposal(db, kind="addon", company=21, tax="2234567890")
    pay(db, link(db, prop2, "P-2", product="education", plan="Growth"), "o2")
    assert [r[0] for r in db.execute("SELECT rate FROM sales_commissions")] == [0, 0]


def test_rep_cannot_create_link_for_others_or_unapproved_proposal(db):
    _, _, prop = approved_proposal(db)
    with pytest.raises(sales.Invalid):
        link(db, prop, "P-X", rep=REP2)
    db.execute("UPDATE sales_proposals SET status='rejected' WHERE id=?", (prop,))
    with pytest.raises(sales.Invalid, match="onaylı"):
        link(db, prop, "P-Y")


def test_unclassified_customer_still_activates_but_goes_to_review(db):
    _, _, prop = approved_proposal(db)
    cust = db.execute("SELECT o.customer_id FROM sales_proposals p JOIN sales_opportunities o ON o.id=p.opportunity_id WHERE p.id=?", (prop,)).fetchone()[0]
    c1 = link(db, prop, "P-1")
    db.execute("UPDATE sales_customers SET classification='unverified' WHERE id=?", (cust,))
    assert pay(db, c1, "o1") == "activated"
    assert db.execute("SELECT COUNT(*) FROM sales_commissions").fetchone()[0] == 0
    assert db.execute("SELECT event FROM billing_audit WHERE event='sales_review_required'").fetchone()


def test_refund_holds_commission_and_allows_reversal(db):
    _, _, prop = approved_proposal(db)
    pay(db, link(db, prop, "P-1"), "o1")
    bp.mark_refunded(db, order_id="o1", actor="1", reason="iade", now=NOW)
    cid = db.execute("SELECT id FROM sales_commissions").fetchone()[0]
    with pytest.raises(sales.Invalid):
        sales.commission_action(db, FOUNDER, {"id": cid, "decision": "approved", "reason": "x"})
    sales.reverse(db, FOUNDER, {"id": cid, "reason": "Shopier iadesi"})
    assert db.execute("SELECT status FROM sales_commissions").fetchone()[0] == "reversed"
