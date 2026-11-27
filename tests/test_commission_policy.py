"""Acceptance tests for the commission policy of 2026-11-27 (SQLite, synthetic data; Shopier, bank and
TCMB simulated). Rates: first product of a won customer %25, then %5; upgrade month %2 of the positive
USD basis difference, then %5 of the new basis; separate products and re-subscriptions %5; never a
second %25 for the same customer."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import re

import pytest

from control import app, billing_payments as bp, commission_report as cr, sales, sales_http, tcmb_rates
from control.billing_catalog import fixed_try_minor

MIG = Path(__file__).resolve().parents[1] / "migrations" / "control_postgresql"
REP_A, REP_B = sales.Actor("rep-a", 1), sales.Actor("rep-b", 1)
FOUNDER = sales.Actor("1", None, True)
T0 = datetime(2026, 11, 27, 9, tzinfo=timezone.utc)
FX = Decimal("50.77000000")
BASE = date.today() + timedelta(days=3)


def rate_on(day):
    return tcmb_rates.ExchangeRate(FX, day.date())


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HR_PAYMENTS_ENABLED", "1")
    path = tmp_path / "c.sqlite3"
    app.init(path)
    con = app.connect(path)
    for name in ("004_billing_quotes.up.sql", "005_billing_payments.up.sql", "006_billing_test.up.sql",
                 "007_bank_transfer.up.sql", "008_payment_link_events.up.sql", "009_commission_fx.up.sql",
                 "010_commission_policy.up.sql"):
        con.executescript(re.sub(r"CREATE OR REPLACE TRIGGER.*?;|DO \$\$.*?END \$\$;", "", (MIG / name).read_text()
                                 .replace("ernasa_control.", "").replace("ADD COLUMN IF NOT EXISTS", "ADD COLUMN")
                                 .replace("CHECK (iban ~ '^TR[0-9]{24}$')", ""), flags=re.S))
    con.execute("INSERT INTO companies VALUES(1,'ERNASA Bayi','Aktif')")
    for cid in range(2, 30):
        con.execute("INSERT INTO companies VALUES(?,?,'Aktif')", (cid, f"Firma {cid}"))
    con.execute("INSERT INTO users(id,email,name,secret,role) VALUES(1,'f@example.test','Kurucu','x','founder')")
    con.execute("INSERT INTO sales_accounts VALUES('rep-a',1,'Deniz Temsilci','a@example.test','x',1)")
    con.execute("INSERT INTO sales_accounts VALUES('rep-b',1,'Ece Temsilci','b@example.test','x',1)")
    con.commit()
    cmd(con, FOUNDER, "bank_settings", bank_name="B", account_holder="ERNASA", iban="TR330006100519786457841326", enabled="1")
    yield con
    con.close()


def cmd(db, actor, action, **data):
    with db:
        db.execute("BEGIN IMMEDIATE")
        return sales_http.commands(app, db, actor, {"action": action, **data})


def customer(db, company, rep=REP_A, tax=None, classification="new"):
    cid = cmd(db, rep, "customer", name=f"Müşteri {company}", tax_id=tax or f"{company:010d}")["id"]
    cmd(db, FOUNDER, "classify", id=cid, classification=classification, reason="k")
    cmd(db, FOUNDER, "link_company", customer_id=cid, company_id=str(company))
    return cid


def proposal(db, cust, kind="subscription", rep=REP_A):
    opp = cmd(db, rep, "opportunity", customer_id=cust, title=kind, kind=kind)["id"]
    prop = cmd(db, rep, "proposal", opportunity_id=opp, requested_tl="1,00", brief=kind)["id"]
    cmd(db, rep, "submit", id=prop)
    cmd(db, FOUNDER, "decide", id=prop, decision="approved", reason="ok", official_tl="1,00", discount_tl="0,00")
    return prop


def pay(db, prop, plan, *, product="hr_flow", start, when=T0, rep=REP_A, method="bank"):
    ref = cmd(db, rep, "payment_request", proposal_id=prop, product=product, plan_name=plan, period_start=start.isoformat())["ref"]
    if method == "bank":
        with db:
            db.execute("BEGIN IMMEDIATE")
            cid = bp.start_bank_checkout(db, ref=ref, rate=rate_on(when), vat_rate=Decimal("0.20"), now=when)
            gross = db.execute("SELECT q.gross_minor FROM billing_checkouts c JOIN billing_quotes q ON q.id=c.quote_id WHERE c.id=?", (cid,)).fetchone()[0]
            assert bp.confirm_bank_payment(db, actor=FOUNDER, checkout_id=cid, transaction_id="TX" + cid[:10],
                                           gross_minor=gross, now=when) == "activated"
        return ref
    pid = "P" + ref[-8:]
    with db:
        db.execute("BEGIN IMMEDIATE")
        bp.start_checkout(db, ref=ref, rate=rate_on(when), vat_rate=Decimal("0.20"), now=when,
                          create_product=lambda **k: (pid, "https://www.shopier.com/" + pid), image_url="x")
    gross = db.execute("SELECT q.gross_minor FROM billing_checkouts c JOIN billing_quotes q ON q.id=c.quote_id WHERE c.provider_product_id=?", (pid,)).fetchone()[0]
    order = {"id": "o" + pid, "paymentStatus": "paid", "currency": "TRY", "totals": {"total": f"{gross // 100}.{gross % 100:02d}"},
             "lineItems": [{"productId": pid, "quantity": 1}], "refunds": [], "returns": []}
    with db:
        db.execute("BEGIN IMMEDIATE")
        assert bp.activate_from_order(db, order_id=order["id"], fetch=lambda _: order, contract=bp.OFFICIAL_CONTRACT, now=when) == "activated"
    return order


def last(db):
    r = db.execute("""SELECT m.rate, m.amount_minor, f.basis_usd, f.commission_usd FROM sales_commissions m
                      JOIN sales_commission_fx f ON f.commission_id=m.id ORDER BY f.created_at DESC, m.rowid DESC LIMIT 1""").fetchone()
    return r["rate"], Decimal(str(r["commission_usd"])), r["amount_minor"], Decimal(str(r["basis_usd"]))


def month(n):
    return BASE + timedelta(days=31 * n)


def test_first_subscription_25_then_continuation_5(db):
    prop = proposal(db, customer(db, 2))
    pay(db, prop, "Growth", start=month(0))
    assert last(db)[:3] == (25, Decimal("62.50"), fixed_try_minor(Decimal("62.50"), FX))
    pay(db, prop, "Growth", start=month(1))
    assert last(db)[:3] == (5, Decimal("12.50"), fixed_try_minor(Decimal("12.50"), FX))


@pytest.mark.parametrize("old,new,diff,usd", [
    ("Starter", "Growth", "150.00", "3.00"), ("Starter", "Business", "400.00", "8.00"), ("Starter", "PRO", "900.00", "18.00"),
    ("Growth", "Business", "250.00", "5.00"), ("Growth", "PRO", "750.00", "15.00"), ("Business", "PRO", "500.00", "10.00")])
def test_six_upgrades_two_percent_of_basis_difference_then_five_percent_of_new_basis(db, old, new, diff, usd):
    cust = customer(db, 3)
    pay(db, proposal(db, cust), old, start=month(0))
    up = proposal(db, cust, "upgrade")
    pay(db, up, new, start=month(1), when=T0 + timedelta(days=31))
    rate, c_usd, amount, basis = last(db)
    assert (rate, basis, c_usd, amount) == (2, Decimal(diff), Decimal(usd), fixed_try_minor(Decimal(usd), FX))
    u = db.execute("SELECT from_plan, to_plan, old_basis_usd, new_basis_usd, diff_usd, commission_usd FROM sales_commission_upgrades").fetchone()
    assert (u["from_plan"], u["to_plan"], Decimal(str(u["diff_usd"])), Decimal(str(u["commission_usd"]))) == (old, new, Decimal(diff), Decimal(usd))
    assert Decimal(str(u["new_basis_usd"])) - Decimal(str(u["old_basis_usd"])) == Decimal(diff)   # never %2 of the full new basis
    pay(db, up, new, start=month(2), when=T0 + timedelta(days=62))
    new_basis = {"Growth": "249.99", "Business": "499.99", "PRO": "999.99"}[new]
    assert last(db)[0] == 5 and last(db)[3] == Decimal(new_basis)


def test_second_product_five_percent_first_and_later_months_hr_flow_continuation_kept(db):
    cust = customer(db, 4)
    hr = proposal(db, cust)
    pay(db, hr, "Starter", start=month(0))
    edu = proposal(db, cust, "addon")
    pay(db, edu, "Starter", product="education", start=month(0), method="shopier")
    assert last(db)[:2] == (5, Decimal("3.75"))                  # Eğitim Starter 75 USD x %5
    pay(db, edu, "Starter", product="education", start=month(1), method="shopier", when=T0 + timedelta(days=31))
    assert last(db)[:2] == (5, Decimal("3.75"))
    pay(db, hr, "Starter", start=month(1), when=T0 + timedelta(days=31))
    assert last(db)[:2] == (5, Decimal("5.00"))                  # HR FLOW continuation unaffected
    rates = [r[0] for r in db.execute("SELECT rate FROM sales_commissions ORDER BY rowid")]
    assert rates.count(25) == 1 and len({r[0] for r in db.execute("SELECT subscription_id FROM sales_collections")}) == 2


@pytest.mark.parametrize("gap", [30, 60, 61, 90, 365])
def test_resubscription_after_any_gap_is_five_percent_never_25(db, gap):
    cust = customer(db, 5)
    pay(db, proposal(db, cust), "Business", start=month(0))
    db.execute("UPDATE billing_subscriptions SET status='cancelled'")
    db.commit()
    again = proposal(db, cust)                                  # new opportunity, new subscription number
    pay(db, again, "Business", start=month(0) + timedelta(days=31 + gap), when=T0 + timedelta(days=31 + gap))
    assert last(db)[:2] == (5, Decimal("25.00"))
    assert [r[0] for r in db.execute("SELECT rate FROM sales_commissions ORDER BY rowid")] == [25, 5]


def test_recreated_customer_record_or_other_rep_cannot_earn_a_second_25(db):
    cust = customer(db, 6, tax="6666666666")
    pay(db, proposal(db, cust), "Starter", start=month(0))
    claim = cmd(db, REP_B, "customer", name="Aynı firma", tax_id="6666666666")   # same tax number -> ownership claim
    assert claim["status"] == "pending"
    other = cmd(db, REP_B, "customer", name="Yeni kayıt", tax_id="6666666667")["id"]   # new record, different tax no
    cmd(db, FOUNDER, "classify", id=other, classification="new", reason="k")
    with pytest.raises(Exception):                                                    # one sales customer per CONTROL company
        cmd(db, FOUNDER, "link_company", customer_id=other, company_id="6")
    db.rollback()
    # same customer, new opportunity / subscription number: still no second %25
    pay(db, proposal(db, cust), "Starter", start=month(1), when=T0 + timedelta(days=31))
    assert last(db)[0] == 5
    assert [r[0] for r in db.execute("SELECT rate FROM sales_commissions ORDER BY rowid")].count(25) == 1


def test_fixed_try_entitlement_survives_rate_changes(db, monkeypatch):
    pay(db, proposal(db, customer(db, 7)), "Starter", start=month(0))
    before = last(db)[2]
    for v in ("54.00", "48.00"):
        cr._CACHE.clear()
        monkeypatch.setattr(tcmb_rates, "fetch_latest_usd_selling_rate",
                            lambda today, max_age_days=4, v=v: tcmb_rates.ExchangeRate(Decimal(v), date(2026, 12, 10)))
        page = sales_http.content(app, db, (FOUNDER, "csrf", {"name": "K", "email": "k"}), "finance")
        assert "1.269,25 TL" in page and last(db)[2] == before == 126925


def test_duplicate_collection_and_second_upgrade_in_same_month_are_blocked_or_reviewed(db):
    cust = customer(db, 8)
    order = pay(db, proposal(db, cust), "Starter", start=month(0), method="shopier")
    with db:
        db.execute("BEGIN IMMEDIATE")
        assert bp.activate_from_order(db, order_id=order["id"], fetch=lambda _: order, contract=bp.OFFICIAL_CONTRACT, now=T0) == "already_processed"
    assert db.execute("SELECT COUNT(*) FROM sales_commissions").fetchone()[0] == 1
    up1 = proposal(db, cust, "upgrade")
    pay(db, up1, "Growth", start=month(1), when=T0 + timedelta(days=31))
    up2 = proposal(db, cust, "upgrade")
    pay(db, up2, "Business", start=month(1) + timedelta(days=10), when=T0 + timedelta(days=33))   # same month
    assert [r[0] for r in db.execute("SELECT rate FROM sales_commissions ORDER BY rowid")] == [25, 2]
    assert db.execute("SELECT COUNT(*) FROM billing_audit WHERE event='sales_review_required'").fetchone()[0] == 1


def test_downgrade_or_unknown_previous_package_goes_to_review(db):
    cust = customer(db, 9)
    pay(db, proposal(db, cust), "Business", start=month(0))
    down = proposal(db, cust, "upgrade")
    pay(db, down, "Starter", start=month(1), when=T0 + timedelta(days=31))          # no positive difference
    fresh = customer(db, 10)
    up_without_history = proposal(db, fresh, "upgrade")
    pay(db, up_without_history, "Growth", start=month(0))                            # previous package unknown
    assert [r[0] for r in db.execute("SELECT rate FROM sales_commissions ORDER BY rowid")] == [25]
    assert db.execute("SELECT COUNT(*) FROM billing_audit WHERE event='sales_review_required'").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM billing_subscriptions WHERE status='active'").fetchone()[0] == 2   # customers still served


def test_refund_and_cancellation_block_and_reverse_entitlement(db):
    cust = customer(db, 11)
    order = pay(db, proposal(db, cust), "Starter", start=month(0), method="shopier")
    with db:
        db.execute("BEGIN IMMEDIATE")
        bp.mark_refunded(db, order_id=order["id"], actor="1", reason="iade", now=T0)
    row = cr.rows(db, FOUNDER)[0]
    assert row["held"] and not row["open"] and row["status_text"] == "İade/iptal incelemesi"
    cm = db.execute("SELECT id FROM sales_commissions").fetchone()[0]
    with pytest.raises(sales.Invalid):
        sales.commission_action(db, FOUNDER, {"id": cm, "decision": "approved", "reason": "x"})
    cmd(db, FOUNDER, "reverse", id=cm, reason="iade onaylandı")
    assert cr.rows(db, FOUNDER)[0]["status"] == "reversed"


def test_representative_isolation(db):
    pay(db, proposal(db, customer(db, 12)), "Starter", start=month(0))
    pay(db, proposal(db, customer(db, 13, rep=REP_B), rep=REP_B), "PRO", start=month(0), rep=REP_B)
    assert [r["customer"] for r in cr.rows(db, REP_A)] == ["Müşteri 12"]
    assert [r["customer"] for r in cr.rows(db, REP_B)] == ["Müşteri 13"]
    page = sales_http.content(app, db, (REP_A, "csrf", {"name": "D", "email": "a"}), "finance")
    assert "Müşteri 13" not in page and "$250,00" not in page


def test_upgrade_row_shows_difference_on_commission_screen(db):
    cust = customer(db, 14)
    pay(db, proposal(db, cust), "Starter", start=month(0))
    pay(db, proposal(db, cust, "upgrade"), "PRO", start=month(1), when=T0 + timedelta(days=31))
    page = sales_http.content(app, db, (REP_A, "csrf", {"name": "D", "email": "a"}), "finance")
    assert "%2" in page and "$18,00" in page and "Starter → PRO farkı" in page and "$900,00" in page
