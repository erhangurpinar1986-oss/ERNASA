"""USD-based commission with the TRY entitlement fixed at the sale's TCMB rate.
SQLite + synthetic data; Shopier, bank and TCMB are simulated. Nothing leaves the machine."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import re

import pytest

from control import app, billing_payments as bp, commission_report as cr, sales, sales_http, tcmb_rates
from control.billing_catalog import HR_FLOW, commission_usd, fixed_try_minor

MIG = Path(__file__).resolve().parents[1] / "migrations" / "control_postgresql"
REP_A, REP_B = sales.Actor("rep-a", 1), sales.Actor("rep-b", 1)
FOUNDER = sales.Actor("1", None, True)
SALE_DAY = datetime(2026, 11, 27, 9, tzinfo=timezone.utc)
RATE_5077 = tcmb_rates.ExchangeRate(Decimal("50.77000000"), date(2026, 11, 27))
START = (date.today() + timedelta(days=3)).isoformat()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HR_PAYMENTS_ENABLED", "1")
    path = tmp_path / "c.sqlite3"
    app.init(path)
    con = app.connect(path)
    for name in ("004_billing_quotes.up.sql", "005_billing_payments.up.sql", "006_billing_test.up.sql",
                 "007_bank_transfer.up.sql", "008_payment_link_events.up.sql", "009_commission_fx.up.sql"):
        con.executescript(re.sub(r"CREATE OR REPLACE TRIGGER.*?;", "", (MIG / name).read_text()
                                 .replace("ernasa_control.", "").replace("ADD COLUMN IF NOT EXISTS", "ADD COLUMN")
                                 .replace("CHECK (iban ~ '^TR[0-9]{24}$')", ""), flags=re.S))
    con.execute("INSERT INTO companies VALUES(1,'ERNASA Bayi','Aktif')")
    for cid, name in ((2, "Alfa Lojistik A.Ş."), (3, "Beta Gıda A.Ş."), (4, "Gama Tekstil A.Ş.")):
        con.execute("INSERT INTO companies VALUES(?,?,'Aktif')", (cid, name))
    con.execute("INSERT INTO users(id,email,name,secret,role) VALUES(1,'f@example.test','Kurucu','x','founder')")
    con.execute("INSERT INTO sales_accounts VALUES('rep-a',1,'Deniz Temsilci','a@example.test','x',1)")
    con.execute("INSERT INTO sales_accounts VALUES('rep-b',1,'Ece Temsilci','b@example.test','x',1)")
    con.commit()
    yield con
    con.close()


def cmd(db, actor, action, **data):
    with db:
        db.execute("BEGIN IMMEDIATE")
        return sales_http.commands(app, db, actor, {"action": action, **data})


def sale(db, company, kind="subscription", rep=REP_A, tax="1111111111", customer=None):
    if customer is None:
        customer = cmd(db, rep, "customer", name=f"Müşteri {company}", tax_id=tax)["id"]
        cmd(db, FOUNDER, "classify", id=customer, classification="new", reason="k")
        cmd(db, FOUNDER, "link_company", customer_id=customer, company_id=str(company))
    opp = cmd(db, rep, "opportunity", customer_id=customer, title=kind, kind=kind)["id"]
    prop = cmd(db, rep, "proposal", opportunity_id=opp, requested_tl="1,00", brief=kind)["id"]
    cmd(db, rep, "submit", id=prop)
    cmd(db, FOUNDER, "decide", id=prop, decision="approved", reason="ok", official_tl="1,00", discount_tl="0,00")
    return customer, prop


def pay_bank(db, prop, plan="Starter", rate=RATE_5077, now=SALE_DAY, period_start=START, rep=REP_A):
    ref = cmd(db, rep, "payment_request", proposal_id=prop, product="hr_flow", plan_name=plan, period_start=period_start)["ref"]
    if not db.execute("SELECT 1 FROM billing_bank_settings").fetchone():
        cmd(db, FOUNDER, "bank_settings", bank_name="B", account_holder="ERNASA", iban="TR330006100519786457841326", enabled="1")
    with db:
        db.execute("BEGIN IMMEDIATE")
        cid = bp.start_bank_checkout(db, ref=ref, rate=rate, vat_rate=Decimal("0.20"), now=now)
        gross = db.execute("SELECT q.gross_minor FROM billing_checkouts c JOIN billing_quotes q ON q.id=c.quote_id WHERE c.id=?", (cid,)).fetchone()[0]
        assert bp.confirm_bank_payment(db, actor=FOUNDER, checkout_id=cid, transaction_id="TX-" + cid[:8],
                                       gross_minor=gross, now=now) == "activated"
    return ref


def pay_shopier(db, prop, plan="Starter", rate=RATE_5077, now=SALE_DAY, period_start=START):
    ref = cmd(db, REP_A, "payment_request", proposal_id=prop, product="hr_flow", plan_name=plan, period_start=period_start)["ref"]
    pid = "P-" + ref[-6:]
    with db:
        db.execute("BEGIN IMMEDIATE")
        bp.start_checkout(db, ref=ref, rate=rate, vat_rate=Decimal("0.20"), now=now,
                          create_product=lambda **k: (pid, "https://www.shopier.com/" + pid), image_url="x")
    gross = db.execute("SELECT q.gross_minor FROM billing_checkouts c JOIN billing_quotes q ON q.id=c.quote_id WHERE c.provider_product_id=?", (pid,)).fetchone()[0]
    order = {"id": "o-" + pid, "paymentStatus": "paid", "currency": "TRY", "totals": {"total": f"{gross // 100}.{gross % 100:02d}"},
             "lineItems": [{"productId": pid, "quantity": 1}], "refunds": [], "returns": []}
    with db:
        db.execute("BEGIN IMMEDIATE")
        assert bp.activate_from_order(db, order_id=order["id"], fetch=lambda _: order, contract=bp.OFFICIAL_CONTRACT, now=now) == "activated"
    return order


def fx_rows(db):
    return [dict(r) for r in db.execute("""SELECT m.rate, m.amount_minor, m.status, f.basis_usd, f.commission_usd, f.fx_rate, f.fx_date
                                          FROM sales_commissions m JOIN sales_commission_fx f ON f.commission_id=m.id
                                          ORDER BY f.created_at, m.rowid""").fetchall()]


def test_usd_table_matches_the_approved_schedule():
    expected = {"Starter": ("109.99", "99.99", "25.00", "5.00"), "Growth": ("269.99", "249.99", "62.50", "12.50"),
                "Business": ("539.99", "499.99", "125.00", "25.00"), "PRO": ("1079.99", "999.99", "250.00", "50.00")}
    for p in HR_FLOW:
        assert (str(p.monthly_usd), str(p.commission_usd), str(commission_usd(p.commission_usd, 25)),
                str(commission_usd(p.commission_usd, 5))) == expected[p.name]
    assert fixed_try_minor(Decimal("25.00"), Decimal("50.77")) == 126925          # spec example: 1.269,25 TL
    assert fixed_try_minor(Decimal("0.01"), Decimal("50.775")) == 51             # ROUND_HALF_UP to kuruş
    assert commission_usd(Decimal("0.10"), 25) == Decimal("0.03")                 # cents first, half up
    assert cr.usd(Decimal("1079.99")) == "$1.079,99"


def test_spec_example_fixed_at_sale_rate_and_unchanged_when_rate_rises_or_falls(db, monkeypatch):
    _, prop = sale(db, 2)
    pay_bank(db, prop, "Starter", RATE_5077)
    row = fx_rows(db)[0]
    assert (row["rate"], row["amount_minor"]) == (25, 126925)
    assert Decimal(str(row["basis_usd"])) == Decimal("99.99") and Decimal(str(row["commission_usd"])) == Decimal("25.00")
    assert Decimal(str(row["fx_rate"])) == Decimal("50.77") and str(row["fx_date"])[:10] == "2026-11-27"
    ident = (FOUNDER, "csrf", {"name": "K", "email": "k@example.test"})
    for current in ("54.00000000", "48.00000000"):   # payment-day rate rises / falls
        cr._CACHE.clear()
        monkeypatch.setattr(tcmb_rates, "fetch_latest_usd_selling_rate",
                            lambda today, max_age_days=4, v=current: tcmb_rates.ExchangeRate(Decimal(v), date(2026, 12, 15)))
        page = sales_http.content(app, db, ident, "finance")
        assert "1.269,25 TL" in page and "50,7700 TL" in page and "27.11.2026" in page
        assert fx_rows(db)[0]["amount_minor"] == 126925
    cm = db.execute("SELECT id FROM sales_commissions").fetchone()[0]
    sales.commission_action(db, FOUNDER, {"id": cm, "decision": "approved", "reason": "ok"})
    with pytest.raises(sales.Invalid, match="1–15"):
        sales.commission_action(db, FOUNDER, {"id": cm, "decision": "paid", "reason": "x"}, today=date(2026, 12, 16))
    sales.commission_action(db, FOUNDER, {"id": cm, "decision": "paid", "reason": "havale"}, today=date(2026, 12, 15))
    row = cr.rows(db, FOUNDER)[0]
    assert (row["amount_minor"], row["status"], row["paid_on"], row["window_text"]) == (126925, "paid", "2026-12-15", "01.12.2026 – 15.12.2026")
    page = sales_http.content(app, db, ident, "finance")
    assert "Komisyon ödeme geçmişi" in page and "15.12.2026" in page


def test_renewal_uses_its_own_sale_rate_and_never_reapplies_25(db):
    customer, prop = sale(db, 2)
    pay_bank(db, prop, "Growth", RATE_5077, period_start=START)
    later = tcmb_rates.ExchangeRate(Decimal("52.00000000"), date(2026, 12, 27))
    nxt = (date.fromisoformat(START) + timedelta(days=31)).isoformat()
    pay_bank(db, prop, "Growth", later, now=datetime(2026, 12, 27, 9, tzinfo=timezone.utc), period_start=nxt)
    rows = fx_rows(db)
    assert [(r["rate"], Decimal(str(r["commission_usd"])), r["amount_minor"]) for r in rows] == [
        (25, Decimal("62.50"), 317313), (5, Decimal("12.50"), 65000)]     # 62.50 x 50.77 ; 12.50 x 52.00
    # a new subscription for the same customer (e.g. renewal within 60 days after cancellation): no 25 %
    _, prop2 = sale(db, 2, customer=customer)
    pay_bank(db, prop2, "Starter", RATE_5077, period_start=(date.fromisoformat(START) + timedelta(days=62)).isoformat())
    assert fx_rows(db)[-1]["rate"] == 0


def test_upgrade_month_zero_then_five_percent(db):
    customer, prop = sale(db, 3, tax="3333333333")
    pay_bank(db, prop, "Starter")
    _, up = sale(db, 3, kind="upgrade", customer=customer)
    day = lambda n: (date.fromisoformat(START) + timedelta(days=n)).isoformat()
    pay_bank(db, up, "Business", period_start=day(31))
    pay_bank(db, up, "Business", period_start=day(62))
    assert [(r["rate"], r["amount_minor"]) for r in fx_rows(db)] == [(25, 126925), (0, 0), (5, 126925)]  # 25.00 USD x 50.77


def test_second_product_first_period_zero(db):
    customer, prop = sale(db, 4, tax="4444444444")
    pay_bank(db, prop, "Starter")
    _, addon = sale(db, 4, kind="addon", customer=customer)
    pay_bank(db, addon, "Growth", period_start=(date.fromisoformat(START) + timedelta(days=31)).isoformat())
    assert fx_rows(db)[-1]["rate"] == 0 and fx_rows(db)[-1]["amount_minor"] == 0


def test_shopier_and_bank_give_identical_fixed_entitlements(db):
    _, p1 = sale(db, 2)
    _, p2 = sale(db, 3, tax="2222222222")
    order = pay_shopier(db, p1, "PRO")
    pay_bank(db, p2, "PRO")
    a, b = fx_rows(db)
    assert a["amount_minor"] == b["amount_minor"] == fixed_try_minor(Decimal("250.00"), Decimal("50.77")) == 1269250
    # duplicate webhook delivery: no second commission
    with db:
        db.execute("BEGIN IMMEDIATE")
        assert bp.activate_from_order(db, order_id=order["id"], fetch=lambda _: order, contract=bp.OFFICIAL_CONTRACT, now=SALE_DAY) == "already_processed"
    assert len(fx_rows(db)) == 2 and db.execute("SELECT COUNT(*) FROM sales_commissions").fetchone()[0] == 2


def test_unverifiable_rate_creates_no_entitlement_and_goes_to_review(db, monkeypatch):
    _, prop = sale(db, 2)
    import control.billing_payments as mod
    monkeypatch.setattr(mod, "commission_fx_for_quote", lambda q: {"basis_usd": Decimal("99.99"), "fx_rate": Decimal("50.77"),
                                                                  "fx_date": "2026-11-27", "fx_source": "manual", "rate_type": "ForexSelling"})
    pay_bank(db, prop, "Starter")
    assert db.execute("SELECT COUNT(*) FROM sales_commissions").fetchone()[0] == 0
    assert db.execute("SELECT status FROM billing_subscriptions").fetchone()[0] == "active"   # customer still served
    assert db.execute("SELECT COUNT(*) FROM billing_audit WHERE event='sales_review_required'").fetchone()[0] == 1


def test_representative_sees_only_own_and_explanation_and_columns(db):
    _, pa = sale(db, 2, rep=REP_A)
    _, pb = sale(db, 3, rep=REP_B, tax="2222222222")
    pay_bank(db, pa, "Starter", rep=REP_A)
    pay_bank(db, pb, "Growth", rep=REP_B)
    assert [r["customer"] for r in cr.rows(db, REP_A)] == ["Müşteri 2"]
    assert [r["customer"] for r in cr.rows(db, REP_B)] == ["Müşteri 3"]
    page = sales_http.content(app, db, (REP_A, "csrf", {"name": "Deniz", "email": "a@example.test"}), "finance")
    assert cr.EXPLANATION in page and "Müşteri 3" not in page and "Ece" not in page
    for head in ("Müşteri", "Satılan paket", "Satış tarihi", "Tahsilat tarihi", "Komisyon matrahı (USD)", "Komisyon oranı",
                 "Kazanılan komisyon (USD)", "Satışta uygulanan sabit kur", "Kur tarihi", "Kesinleşmiş TL hak edişi",
                 "Komisyon ödeme dönemi", "Ödeme durumu", "Gerçekleşen ödeme tarihi"):
        assert head in page, head
    assert "$99,99" in page and "$25,00" in page and "1.269,25 TL" in page and "HR FLOW Starter" in page
    founder = sales_http.content(app, db, (FOUNDER, "csrf", {"name": "K", "email": "k"}), "finance")
    for text in ("Temsilci bazında komisyon özeti", "Deniz Temsilci", "Ece Temsilci", "Aylık komisyon ödeme dönemleri",
                 "01.12.2026 – 15.12.2026", "Toplam bekleyen komisyon", "Ödenmiş komisyonlar", "$25,00", "$62,50"):
        assert text in founder, text


def test_test_payment_never_creates_a_real_commission(db, monkeypatch):
    monkeypatch.setenv("HR_TEST_PLAN_ENABLED", "1")
    _, prop = sale(db, 2)
    ref = cmd(db, FOUNDER, "payment_request", proposal_id=prop, company_id="2", product="education", plan_name="Ödeme Testi", period_start=START)["ref"]
    with db:
        db.execute("BEGIN IMMEDIATE")
        bp.start_checkout(db, ref=ref, rate=RATE_5077, vat_rate=Decimal("0.20"), now=SALE_DAY,
                          create_product=lambda **k: ("PT", "https://www.shopier.com/PT"), image_url="x")
    order = {"id": "ot", "paymentStatus": "paid", "currency": "TRY", "totals": {"total": "3.00"},
             "lineItems": [{"productId": "PT", "quantity": 1}], "refunds": [], "returns": []}
    with db:
        db.execute("BEGIN IMMEDIATE")
        bp.activate_from_order(db, order_id="ot", fetch=lambda _: order, contract=bp.OFFICIAL_CONTRACT, now=SALE_DAY)
    assert db.execute("SELECT COUNT(*) FROM sales_commissions").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM sales_commission_fx").fetchone()[0] == 0


def test_price_list_in_usd_with_current_rate_estimates(db, monkeypatch):
    cr._CACHE.clear()
    monkeypatch.setattr(tcmb_rates, "fetch_latest_usd_selling_rate",
                        lambda today, max_age_days=4: tcmb_rates.ExchangeRate(Decimal("50.7700"), date(2026, 11, 27)))
    page = sales_http.content(app, db, (REP_A, "csrf", {"name": "D", "email": "a"}), "catalog")
    for text in ("Aylık fiyat + KDV (USD)", "Komisyon matrahı (USD)", "İlk dönem %25 (USD)", "Sonraki dönem %5 (USD)",
                 "$109,99", "$99,99", "$25,00", "$5,00", "$1.079,99", "$999,99", "$250,00", "$50,00",
                 "Aylık aday", "Aktif pozisyon", "İK hesabı", "KDV dahil fiyat (USD)",
                 "50,7700 TL", "27.11.2026", "tahminidir", "1.269,25 TL", "253,85 TL"):
        assert text in page, text
    cr._CACHE.clear()
    monkeypatch.setattr(tcmb_rates, "fetch_latest_usd_selling_rate",
                        lambda today, max_age_days=4: (_ for _ in ()).throw(tcmb_rates.RateUnavailable("x")))
    page = sales_http.content(app, db, (REP_A, "csrf", {"name": "D", "email": "a"}), "catalog")
    assert "doğrulanamadı" in page and "Tahmini TL" not in page and "$25,00" in page
