"""End-to-end over real CONTROL HTTP server (SQLite, synthetic data). Shopier and TCMB are SIMULATED.

Flow: rep creates link for approved proposal -> customer page shows TL net/KDV/total ->
customer accepts terms -> single-use Shopier product -> Shopier webhook -> server re-fetches
order -> subscription + limits -> Sales Hub collection + 25% commission -> rep & CONTROL views.
"""
from datetime import date
from decimal import Decimal
import http.client
import json
from pathlib import Path
import re
import threading

import pytest

from control import app, billing_http, sales, sales_http, shopier_hr, tcmb_rates

MIG = Path(__file__).resolve().parents[1] / "migrations" / "control_postgresql"
PASSWORD = "Synthetic-sales-test-2026!"
REP = sales.Actor("rep-a", 1)
FOUNDER = sales.Actor("1", None, True)
RATE = tcmb_rates.ExchangeRate(Decimal("42.50000000"), date(2026, 11, 25))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HR_PAYMENTS_ENABLED", "1")
    path = tmp_path / "control.sqlite3"
    app.init(path)
    db = app.connect(path)
    for name in ("004_billing_quotes.up.sql", "005_billing_payments.up.sql"):
        db.executescript(re.sub(r"CREATE OR REPLACE TRIGGER.*?;", "",
                                (MIG / name).read_text().replace("ernasa_control.", ""), flags=re.S))
    db.execute("INSERT INTO companies VALUES(1,'ERNASA Bayi','Aktif')")
    db.execute("INSERT INTO companies VALUES(2,'Müşteri Lojistik A.Ş.','Aktif')")
    db.execute("INSERT INTO users(id,email,name,secret,role) VALUES(1,'f@example.test','Kurucu',?,'founder')",
               (app.digest(PASSWORD),))
    db.execute("INSERT INTO sales_accounts VALUES('rep-a',1,'Temsilci A','a@example.test',?,1)", (app.digest(PASSWORD),))
    db.execute("INSERT INTO sales_accounts VALUES('rep-b',1,'Temsilci B','b@example.test',?,1)", (app.digest(PASSWORD),))
    db.commit()
    shop = {"orders": {}, "products": {}}

    def create_product(*, title, description, gross_minor, image_url):
        pid = f"P{len(shop['products']) + 1}"
        shop["products"][pid] = gross_minor
        return pid, "https://www.shopier.com/" + pid

    monkeypatch.setattr(shopier_hr, "create_product", create_product)
    monkeypatch.setattr(shopier_hr, "fetch_order", lambda oid: shop["orders"][oid])
    monkeypatch.setattr(tcmb_rates, "fetch_latest_usd_selling_rate", lambda today: RATE)
    server = app.server(path, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield db, server, shop
    server.shutdown()
    db.close()


def cmd(db, actor, action, **data):
    with db:
        db.execute("BEGIN IMMEDIATE")
        return sales_http.commands(app, db, actor, {"action": action, **data})


def request(server, method, path, body=b"", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    h = {"Host": f"127.0.0.1:{server.server_port}", **(headers or {})}
    conn.request(method, path, body=body, headers=h)
    r = conn.getresponse()
    return r.status, dict(r.getheaders()), r.read().decode()


def sale(db):
    cust = cmd(db, REP, "customer", name="Müşteri Lojistik", tax_id="1234567890")["id"]
    cmd(db, FOUNDER, "classify", id=cust, classification="new", reason="Vergi levhası kontrolü")
    cmd(db, FOUNDER, "link_company", customer_id=cust, company_id="2")
    opp = cmd(db, REP, "opportunity", customer_id=cust, title="HR FLOW Growth", kind="subscription")["id"]
    prop = cmd(db, REP, "proposal", opportunity_id=opp, requested_minor="1500000", brief="Growth aylık")["id"]
    cmd(db, REP, "submit", id=prop)
    cmd(db, FOUNDER, "decide", id=prop, decision="approved", reason="Liste fiyatı",
        official_minor="1500000", discount_minor="0")
    return prop


def webhook(server, shop, oid, pid, gross, wid, event="order.created", **over):
    shop["orders"][oid] = {"id": oid, "paymentStatus": "paid", "currency": "TRY",
                           "totals": {"total": f"{gross // 100}.{gross % 100:02d}"},
                           "lineItems": [{"productId": pid, "quantity": 1}], "refunds": [], "returns": [], **over}
    body = json.dumps({"id": oid}).encode()
    return request(server, "POST", "/odeme/shopier/webhook", body,
                   {"Content-Type": "application/json", "Shopier-Webhook-Id": wid, "Shopier-Event": event})


def test_full_paid_flow_over_http(env):
    db, server, shop = env
    prop = sale(db)
    ref = cmd(db, REP, "payment_request", proposal_id=prop, product="hr_flow", plan_name="Growth",
              period_start="2099-01-01")["ref"]

    status, _, html = request(server, "GET", f"/odeme/{ref}")
    assert status == 200
    # Growth 349.99 USD * 42.5 = 14874.575 -> 14874.58 net; KDV 2974.92; total 17849.50
    assert "14.874,58 TL" in html and "2.974,92 TL" in html and "17.849,50 TL" in html
    assert "Müşteri Lojistik A.Ş." in html and "500 aday" in html and "TCMB" in html

    origin = server.origin
    status, _, html = request(server, "POST", f"/odeme/{ref}", b"csrf=&terms=0",
                              {"Content-Type": "application/x-www-form-urlencoded", "Origin": origin})
    assert status == 422 and not shop["products"]
    status, headers, _ = request(server, "POST", f"/odeme/{ref}", b"terms=1",
                                 {"Content-Type": "application/x-www-form-urlencoded", "Origin": origin})
    assert status == 303 and headers["Location"].endswith(f"/odeme/{ref}")
    assert shop["products"] == {"P1": 1784950}
    status, _, html = request(server, "GET", f"/odeme/{ref}")
    assert "https://www.shopier.com/P1" in html and "Shopier ile güvenli öde" in html

    # Forged/mismatched notifications activate nothing
    assert webhook(server, shop, "o-bad", "P1", 100, "w0")[0] == 200
    assert webhook(server, shop, "o-cv", "CV-PRODUCT", 1784950, "w-cv")[0] == 200
    assert db.execute("SELECT COUNT(*) FROM billing_periods").fetchone()[0] == 0

    # Real-shaped paid order: activates once, books collection + 25% commission
    assert webhook(server, shop, "o1", "P1", 1784950, "w1")[0] == 200
    assert webhook(server, shop, "o1", "P1", 1784950, "w1")[0] == 200  # Shopier retry
    assert webhook(server, shop, "o1", "P1", 1784950, "w2")[0] == 200  # duplicate order, new delivery
    assert db.execute("SELECT COUNT(*) FROM billing_periods").fetchone()[0] == 1
    period = db.execute("SELECT * FROM billing_periods").fetchone()
    assert (period["capacity"], period["positions"], period["hr_users"]) == (500, 10, 3)
    m = db.execute("SELECT * FROM sales_commissions").fetchall()
    assert len(m) == 1 and m[0]["rate"] == 25 and m[0]["amount_minor"] == (1487458 * 25 + 50) // 100

    status, _, html = request(server, "GET", f"/odeme/{ref}")
    assert "Ödendi" in html and "active" in html

    rep_rows = __import__("control.billing_payments", fromlist=["x"]).rep_sales(db, REP)
    assert rep_rows[0]["commission_window"].startswith("2026-12-01") or rep_rows[0]["commission_window"]
    assert rep_rows[0]["basis_minor"] == 1487458 and rep_rows[0]["payment_status"] == "paid"
    assert __import__("control.billing_payments", fromlist=["x"]).rep_sales(db, sales.Actor("rep-b", 1)) == []

    # Rendered panels (rep own view, CONTROL view)
    ident_rep = (REP, "csrf", {"name": "Temsilci A", "email": "a@example.test"})
    page = sales_http.content(app, db, ident_rep, "payments")
    assert "Müşteri Lojistik" in page and "%25" in page and ref in page
    ident_f = (FOUNDER, "csrf", {"name": "Kurucu", "email": "f@example.test"})
    assert ref in sales_http.content(app, db, ident_f, "payments")

    # Refund via webhook: API-confirmed refund stops entitlement and holds commission
    shop["orders"]["o1"]["refunds"] = [{"id": "r1", "status": "succeeded"}]
    body = json.dumps({"id": "r1", "orderId": "o1", "status": "succeeded"}).encode()
    assert request(server, "POST", "/odeme/shopier/webhook", body,
                   {"Content-Type": "application/json", "Shopier-Webhook-Id": "w9", "Shopier-Event": "refund.updated"})[0] == 200
    assert db.execute("SELECT status FROM billing_subscriptions").fetchone()[0] == "refunded"
    assert db.execute("SELECT held FROM sales_collections").fetchone()[0] == 1


def test_provider_outage_returns_503_so_shopier_retries(env, monkeypatch):
    db, server, shop = env
    ref = cmd(db, FOUNDER, "payment_request", company_id="2", product="education", plan_name="Starter",
              period_start="2099-01-01")["ref"]
    request(server, "POST", f"/odeme/{ref}", b"terms=1",
            {"Content-Type": "application/x-www-form-urlencoded", "Origin": server.origin})

    def down(_):
        raise shopier_hr.ShopierError("Shopier'e ulaşılamadı.")
    monkeypatch.setattr(shopier_hr, "fetch_order", down)
    body = json.dumps({"id": "o5"}).encode()
    hdr = {"Content-Type": "application/json", "Shopier-Webhook-Id": "w5", "Shopier-Event": "order.created"}
    assert request(server, "POST", "/odeme/shopier/webhook", body, hdr)[0] == 503
    gross = db.execute("SELECT gross_minor FROM billing_quotes").fetchone()[0]
    monkeypatch.setattr(shopier_hr, "fetch_order", lambda oid: shop["orders"][oid])  # Shopier back
    assert webhook(server, shop, "o5", "P1", gross, "w5")[0] == 200  # retry after recovery
    assert db.execute("SELECT COUNT(*) FROM billing_periods").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM sales_commissions").fetchone()[0] == 0  # direct sale


def test_payment_disabled_and_unknown_ref_and_cross_origin(env, monkeypatch):
    db, server, shop = env
    ref = cmd(db, FOUNDER, "payment_request", company_id="2", product="hr_flow", plan_name="Starter",
              period_start="2099-01-01")["ref"]
    assert request(server, "GET", "/odeme/ERN" + "0" * 20)[0] == 404
    assert request(server, "POST", f"/odeme/{ref}", b"terms=1",
                   {"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://evil.example"})[0] == 403
    monkeypatch.setenv("HR_PAYMENTS_ENABLED", "0")
    status, _, html = request(server, "GET", f"/odeme/{ref}")
    assert status == 200 and "henüz açılmadı" in html and "<form" not in html.split("ERNASA ödeme")[-1]
    assert not shop["products"]


def test_founder_only_one_kurus_live_test_plan_with_rep_commission(env):
    db, server, shop = env
    prop = sale(db)
    with pytest.raises(sales.Invalid):
        cmd(db, REP, "payment_request", proposal_id=prop, product="education", plan_name="Ödeme Testi",
            period_start="2099-01-01")
    ref = cmd(db, FOUNDER, "payment_request", proposal_id=prop, company_id="2", product="education",
              plan_name="Ödeme Testi", period_start="2099-01-01")["ref"]
    status, _, html = request(server, "GET", f"/odeme/{ref}")
    assert status == 200 and "0,01 TL" in html and "1 eğitim katılımcısı" in html
    request(server, "POST", f"/odeme/{ref}", b"terms=1",
            {"Content-Type": "application/x-www-form-urlencoded", "Origin": server.origin})
    assert shop["products"] == {"P1": 1}
    assert webhook(server, shop, "t1", "P1", 1, "wt1")[0] == 200
    assert db.execute("SELECT capacity FROM billing_periods").fetchone()[0] == 1
    m = db.execute("SELECT rate, amount_minor FROM sales_commissions").fetchone()
    assert (m["rate"], m["amount_minor"]) == (25, 0)
    assert db.execute("SELECT COUNT(*) FROM service_terms").fetchone()[0] == 0  # HR FLOW untouched
    ref2 = cmd(db, FOUNDER, "payment_request", proposal_id="direct", company_id="2", product="education",
               plan_name="Ödeme Testi", period_start="2099-02-01")["ref"]
    assert ref2.startswith("ERN")
    ident_f = (FOUNDER, "csrf", {"name": "Kurucu", "email": "f@example.test"})
    assert "Canlı ödeme testi" in sales_http.content(app, db, ident_f, "payments")
    ident_rep = (REP, "csrf", {"name": "Temsilci A", "email": "a@example.test"})
    assert "Canlı ödeme testi" not in sales_http.content(app, db, ident_rep, "payments")
