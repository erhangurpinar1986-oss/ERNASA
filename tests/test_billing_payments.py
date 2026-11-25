"""Payment verification + exactly-once activation (synthetic data, simulated Shopier responses).

These are SIMULATIONS: Shopier is replaced by an in-memory fetcher. They do not prove the
real API field names; see OrderContract in control/billing_payments.py.
"""
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import re
import sqlite3

import pytest

from control import billing_payments as bp
from control.billing_quotes import issue_quote
from control.tcmb_rates import ExchangeRate

MIG = Path(__file__).resolve().parents[1] / "migrations" / "control_postgresql"
NOW = datetime(2026, 11, 25, 15, tzinfo=timezone.utc)
CONTRACT = bp.OFFICIAL_CONTRACT


def sqlite_schema(sql):
    sql = sql.replace("ernasa_control.", "")
    return re.sub(r"CREATE OR REPLACE TRIGGER.*?;", "", sql, flags=re.S)


LEGACY = """CREATE TABLE subscriptions(company_id INTEGER PRIMARY KEY, package TEXT, starts TEXT, ends TEXT, status TEXT);
CREATE TABLE service_terms(company_id INTEGER PRIMARY KEY, product TEXT, version TEXT, user_quota INTEGER, operation_quota INTEGER,
 trial_days INTEGER, period TEXT, price_minor INTEGER, currency TEXT, vat_basis INTEGER, payment TEXT NOT NULL);
CREATE TABLE ats_subscriptions(company_id INTEGER PRIMARY KEY, package TEXT, starts TEXT, ends TEXT, status TEXT, user_quota INTEGER);
CREATE TABLE subscription_history(id INTEGER PRIMARY KEY, company_id INTEGER, snapshot TEXT, recorded TEXT DEFAULT CURRENT_TIMESTAMP);"""


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("HR_PAYMENTS_ENABLED", "1")
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("CREATE TABLE companies(id INTEGER PRIMARY KEY, name TEXT, status TEXT)")
    con.executescript(LEGACY)
    con.executemany("INSERT INTO companies VALUES(?,?,?)", [(1, "A", "Aktif"), (2, "B", "Aktif")])
    for name in ("004_billing_quotes.up.sql", "005_billing_payments.up.sql"):
        con.executescript(sqlite_schema((MIG / name).read_text()))
    return con


def checkout(db, company=1, plan="Starter", product="hr_flow", start=date(2026, 12, 1), pid="P-1"):
    rate = ExchangeRate(Decimal("42.50000000"), date(2026, 11, 25))
    qid = issue_quote(db, company_id=company, product=product, plan_name=plan, period_start=start,
                      rate=rate, expected_rate_date=date(2026, 11, 25), vat_rate=Decimal("0.20"), now=NOW)
    cid = "c-" + pid
    db.execute("INSERT INTO billing_checkouts VALUES(?,?,?,?,?,?,?,?,?)",
               (cid, qid, company, "shopier_hr", pid, "https://www.shopier.com/" + pid, "ref-" + pid, "pending",
                int(NOW.timestamp())))
    gross = db.execute("SELECT gross_minor FROM billing_quotes WHERE id=?", (qid,)).fetchone()[0]
    return cid, gross


def order(oid="9001", pid="P-1", gross=0, **over):
    o = {"id": oid, "paymentStatus": "paid", "currency": "TRY",
         "totals": {"total": f"{gross // 100}.{gross % 100:02d}"},
         "lineItems": [{"productId": pid, "quantity": 1}], "refunds": [], "returns": []}
    o.update(over)
    return o


def run(db, o, **kw):
    db.execute("BEGIN")
    try:
        r = bp.activate_from_order(db, order_id=o["id"], fetch=lambda _id: o, contract=CONTRACT, now=NOW, **kw)
        db.execute("COMMIT")
        return r
    except Exception:
        db.execute("ROLLBACK")
        raise


def test_starter_quote_amounts_use_decimal_kurus(db):
    _, gross = checkout(db)
    # 99.99 USD * 42.5 = 4249.575 -> 4249.58 TL net, +20% KDV 849.92 -> 5099.50
    assert gross == 509950


def test_successful_payment_activates_once_and_freezes_entitlements(db):
    cid, gross = checkout(db, plan="Growth")
    assert run(db, order(gross=gross)) == "activated"
    assert run(db, order(gross=gross)) == "already_processed"
    p = db.execute("SELECT * FROM billing_periods").fetchall()
    assert len(p) == 1 and p[0]["capacity"] == 500 and p[0]["positions"] == 10 and p[0]["hr_users"] == 3
    assert db.execute("SELECT status FROM billing_checkouts WHERE id=?", (cid,)).fetchone()[0] == "paid"
    assert db.execute("SELECT status FROM billing_subscriptions").fetchone()[0] == "active"
    # mirrored into the entitlement tables the live portal already enforces
    assert tuple(db.execute("SELECT package,starts,ends,status FROM subscriptions").fetchone()) == (
        "HR FLOW Growth", "2026-12-01", "2026-12-31", "Aktif")
    assert tuple(db.execute("SELECT product,user_quota,operation_quota,payment FROM service_terms").fetchone()) == (
        "ernasa-ik", 3, 500, "Ödendi")
    assert tuple(db.execute("SELECT status,user_quota FROM ats_subscriptions").fetchone()) == ("Aktif", 3)


@pytest.mark.parametrize("mutate,msg", [
    (lambda o: o.update(paymentStatus="pending"), "tamamlanmamış"),
    (lambda o: o.update(currency="USD"), "TRY"),
    (lambda o: o["totals"].update(total="1.00"), "tutar"),
    (lambda o: o["totals"].update(total=5099.5), "kesin"),
    (lambda o: o["lineItems"][0].update(productId="CV-PRODUCT"), "ait değil"),
    (lambda o: o["lineItems"][0].update(quantity=2), "miktar"),
    (lambda o: o.update(refunds=[{"id": "r"}]), "iade"),
    (lambda o: o.pop("totals"), "eksik"),
])
def test_rejections_activate_nothing(db, mutate, msg):
    _, gross = checkout(db)
    o = order(gross=gross)
    mutate(o)
    with pytest.raises(bp.PaymentRejected, match=msg):
        run(db, o)
    assert db.execute("SELECT COUNT(*) FROM billing_periods").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM billing_provider_orders").fetchone()[0] == 0


def test_wrong_customer_and_disabled_flag_and_unconfirmed_contract(db, monkeypatch):
    _, gross = checkout(db)
    with pytest.raises(bp.PaymentRejected, match="Müşteri"):
        run(db, order(gross=gross), expected_company_id=2)
    monkeypatch.setenv("HR_PAYMENTS_ENABLED", "0")
    with pytest.raises(bp.PaymentRejected, match="etkin değil"):
        run(db, order(gross=gross))
    monkeypatch.setenv("HR_PAYMENTS_ENABLED", "1")
    with pytest.raises(bp.PaymentRejected, match="etkin değil"):
        bp.activate_from_order(db, order_id="9001", fetch=lambda _: order(gross=gross),
                               contract=bp.PROVISIONAL_CONTRACT, now=NOW)


def test_forged_order_id_mismatch_rejected(db):
    _, gross = checkout(db)
    o = order(gross=gross)
    with pytest.raises(bp.PaymentRejected, match="kimliği"):
        db.execute("BEGIN")
        try:
            bp.activate_from_order(db, order_id="1234", fetch=lambda _: o, contract=CONTRACT, now=NOW)
        finally:
            db.execute("ROLLBACK")


def test_existing_manual_subscription_is_snapshotted_not_lost(db):
    db.execute("INSERT INTO subscriptions VALUES(1,'Kurumsal','2026-01-01','2026-11-30','Aktif')")
    db.execute("INSERT INTO service_terms VALUES(1,'ernasa-ik','v1',9,999,NULL,'Yıllık',1,'TRY',2000,'Ödendi')")
    _, gross = checkout(db)
    run(db, order(gross=gross))
    snap = db.execute("SELECT snapshot FROM subscription_history").fetchone()[0]
    assert '"Kurumsal"' in snap and '999' in snap
    assert db.execute("SELECT starts,ends FROM subscriptions").fetchone()[:] == ("2026-01-01", "2026-12-31")


def test_renewal_adds_next_period_and_same_period_cannot_be_paid_twice(db):
    _, g1 = checkout(db, pid="P-1")
    _, g2 = checkout(db, pid="P-2", start=date(2027, 1, 1))
    _, g3 = checkout(db, pid="P-3", start=date(2027, 1, 1))
    run(db, order("1", "P-1", g1))
    run(db, order("2", "P-2", g2))
    with pytest.raises(sqlite3.IntegrityError):
        run(db, order("3", "P-3", g3))
    assert db.execute("SELECT COUNT(*) FROM billing_periods").fetchone()[0] == 2


def test_webhook_is_deduplicated_by_webhook_id_and_only_yields_hint(db):
    body = b'{"id":"9001","paymentStatus":"paid"}'
    assert bp.record_webhook(db, webhook_id="w1", event="order.created", raw_body=body, now=NOW) == ("order.created", "9001")
    assert bp.record_webhook(db, webhook_id="w1", event="order.created", raw_body=body, now=NOW) is None
    assert bp.record_webhook(db, webhook_id="w2", event="product.updated", raw_body=b'{"id":"5"}', now=NOW) is None
    assert bp.record_webhook(db, webhook_id="w3", event="refund.updated",
                             raw_body=b'{"id":"r1","orderId":"9001","status":"succeeded"}', now=NOW) == ("refund.updated", "9001")
    assert db.execute("SELECT COUNT(*) FROM billing_periods").fetchone()[0] == 0


def test_signature_check_accepts_hex_or_base64_hs256(monkeypatch):
    import base64, hashlib, hmac
    monkeypatch.setenv("SHOPIER_HR_WEBHOOK_TOKEN", "tok")
    mac = hmac.new(b"tok", b"{}", hashlib.sha256).digest()
    assert bp.signature_ok(b"{}", mac.hex()) and bp.signature_ok(b"{}", base64.b64encode(mac).decode())
    assert not bp.signature_ok(b"{}", "forged")


def test_refund_webhook_requires_api_confirmed_succeeded_refund(db):
    _, gross = checkout(db)
    run(db, order(gross=gross))
    pending = order(gross=gross, refunds=[{"id": "r", "status": "pending"}])
    assert bp.refund_from_order(db, order_id="9001", fetch=lambda _: pending, now=NOW) is False
    done = order(gross=gross, refunds=[{"id": "r", "status": "succeeded"}])
    assert bp.refund_from_order(db, order_id="9001", fetch=lambda _: done, now=NOW) is True
    assert bp.refund_from_order(db, order_id="9001", fetch=lambda _: done, now=NOW) is False
    assert db.execute("SELECT status FROM billing_subscriptions").fetchone()[0] == "refunded"
    # unknown (e.g. ERNASA CV) order: nothing happens, API not even called
    assert bp.refund_from_order(db, order_id="CV1", fetch=lambda _: 1 / 0, now=NOW) is False


def test_server_side_quota_counts_each_candidate_once_and_stops_at_capacity(db):
    _, gross = checkout(db)
    run(db, order(gross=gross))
    pid = db.execute("SELECT id FROM billing_periods").fetchone()[0]
    when = datetime(2026, 12, 2, tzinfo=timezone.utc)
    assert bp.consume(db, pid, "cand-1", when) is True
    assert bp.consume(db, pid, "cand-1", when) is False  # ATS + AI interview = one unit
    for i in range(2, 101):
        bp.consume(db, pid, f"cand-{i}", when)
    with pytest.raises(bp.PaymentRejected, match="doldu"):
        bp.consume(db, pid, "cand-101", when)
    with pytest.raises(bp.PaymentRejected, match="Aktif"):
        bp.consume(db, pid, "late", datetime(2027, 1, 5, tzinfo=timezone.utc))


def test_refund_stops_entitlement_but_keeps_records(db):
    _, gross = checkout(db)
    run(db, order(gross=gross))
    bp.mark_refunded(db, order_id="9001", actor="founder", reason="Shopier iadesi", now=NOW)
    pid = db.execute("SELECT id FROM billing_periods").fetchone()[0]
    with pytest.raises(bp.PaymentRejected):
        bp.consume(db, pid, "x", datetime(2026, 12, 2, tzinfo=timezone.utc))
    assert db.execute("SELECT COUNT(*) FROM billing_periods").fetchone()[0] == 1
    assert db.execute("SELECT status FROM subscriptions").fetchone()[0] == "Askıda"
