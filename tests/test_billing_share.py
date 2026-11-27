"""Sales Hub payment links: create-or-reuse, copy/WhatsApp/e-mail to the registered contact,
tracking stages, representative isolation and founder-only powers (SQLite, synthetic data).
E-mail goes to the local sandbox provider; Shopier and TCMB are simulated. No real message leaves."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import http.client
import json
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from control import app, billing_payments as bp, billing_share as share, sales, sales_http, shopier_hr, tcmb_rates

MIG = Path(__file__).resolve().parents[1] / "migrations" / "control_postgresql"
REP_A = sales.Actor("rep-a", 1)
REP_B = sales.Actor("rep-b", 1)
REP_X = sales.Actor("rep-x", 3)   # another dealer company
FOUNDER = sales.Actor("1", None, True)
RATE = tcmb_rates.ExchangeRate(Decimal("42.50000000"), date(2026, 11, 25))
START = (date.today() + timedelta(days=5)).isoformat()


def migrate(db):
    for name in ("004_billing_quotes.up.sql", "005_billing_payments.up.sql", "006_billing_test.up.sql",
                 "007_bank_transfer.up.sql", "008_payment_link_events.up.sql", "009_commission_fx.up.sql", "010_commission_policy.up.sql"):
        db.executescript(re.sub(r"CREATE OR REPLACE TRIGGER.*?;|DO \$\$.*?END \$\$;", "",
                                (MIG / name).read_text().replace("ernasa_control.", "").replace("ADD COLUMN IF NOT EXISTS", "ADD COLUMN")
                                .replace("CHECK (iban ~ '^TR[0-9]{24}$')", ""), flags=re.S))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HR_PAYMENTS_ENABLED", "1")
    monkeypatch.setenv("ERNASA_CONTROL_PUBLIC_ORIGIN", "https://ernasa.com")
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("ERNASA_EMAIL_SANDBOX_DIR", str(outbox))
    path = tmp_path / "control.sqlite3"
    app.init(path)
    db = app.connect(path)
    migrate(db)
    db.execute("INSERT INTO companies VALUES(1,'ERNASA Bayi','Aktif')")
    db.execute("INSERT INTO companies VALUES(2,'Müşteri Lojistik A.Ş.','Aktif')")
    db.execute("INSERT INTO companies VALUES(3,'Başka Bayi','Aktif')")
    db.execute("INSERT INTO companies VALUES(4,'Doğrudan Müşteri A.Ş.','Aktif')")
    db.execute("INSERT INTO companies VALUES(5,'Diğer Müşteri A.Ş.','Aktif')")
    db.execute("INSERT INTO users(id,email,name,secret,role) VALUES(1,'f@example.test','Erhan Kurucu','x','founder')")
    for rid, comp, name in (("rep-a", 1, "Deniz Temsilci"), ("rep-b", 1, "Ece Temsilci"), ("rep-x", 3, "Can Bayi")):
        db.execute("INSERT INTO sales_accounts VALUES(?,?,?,?,?,1)", (rid, comp, name, rid + "@example.test", "x"))
    for comp, person, phone, mail in ((2, "Ayşe Yılmaz", "0532 111 22 33", "ayse@example.test"),
                                      (4, "Mert Kaya", "+90 (533) 444 55 66", "mert@example.test")):
        db.execute("""INSERT INTO company_contacts(company_id,primary_contact,name,title,department,phone,email,kind)
                      VALUES(?,1,?,'Genel Müdür','Yönetim',?,?,'Şirket yetkilisi')""", (comp, person, phone, mail))
    db.commit()
    shop = {"products": {}}

    def create_product(*, title, description, gross_minor, image_url):
        pid = f"P{len(shop['products']) + 1}"
        shop["products"][pid] = gross_minor
        return pid, "https://www.shopier.com/" + pid

    monkeypatch.setattr(shopier_hr, "create_product", create_product)
    monkeypatch.setattr(tcmb_rates, "fetch_latest_usd_selling_rate", lambda today: RATE)
    server = app.server(path, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield db, server, outbox
    server.shutdown()
    db.close()


def cmd(db, actor, action, **data):
    with db:
        db.execute("BEGIN IMMEDIATE")
        return sales_http.commands(app, db, actor, {"action": action, **data})


def approved(db, rep=REP_A, company=2, tax="1234567890"):
    cust = cmd(db, rep, "customer", name="Müşteri", tax_id=tax)["id"]
    cmd(db, FOUNDER, "classify", id=cust, classification="new", reason="kontrol")
    if company:
        cmd(db, FOUNDER, "link_company", customer_id=cust, company_id=str(company))
    opp = cmd(db, rep, "opportunity", customer_id=cust, title="HR", kind="subscription")["id"]
    prop = cmd(db, rep, "proposal", opportunity_id=opp, requested_tl="100,00", brief="HR FLOW Growth")["id"]
    cmd(db, rep, "submit", id=prop)
    cmd(db, FOUNDER, "decide", id=prop, decision="approved", reason="ok", official_tl="100,00", discount_tl="0,00")
    return prop


def link(db, prop, plan="Growth", rep=REP_A):
    return cmd(db, rep, "payment_request", proposal_id=prop, product="hr_flow", plan_name=plan, period_start=START)


def session(db, account):
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO sales_sessions VALUES(?,?,?,?)", (app.token_hash(token), account, "csrf", int(time.time()) + 600))
    db.commit()
    return token


def get(server, path, token):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    conn.request("GET", path, headers={"Host": f"127.0.0.1:{server.server_port}", "Cookie": f"sales_session={token}"})
    r = conn.getresponse()
    return r.status, dict(r.getheaders()), r.read().decode()


def sent_mails(outbox):
    return [json.loads(p.read_text()) for p in sorted(outbox.glob("*.json"))] if outbox.exists() else []


EXPECTED = ("Merhaba Ayşe Yılmaz,\n"
            "ERNASA HR FLOW Growth aboneliğinize ilişkin ödeme bağlantınız hazırlanmıştır.\n"
            "Aşağıdaki bağlantı üzerinden kredi/banka kartı veya Havale/EFT/FAST yöntemlerinden birini "
            "kullanarak ödemenizi gerçekleştirebilirsiniz.\n"
            "https://ernasa.com/control/odeme/{ref}\n"
            "Ödemenizin doğrulanmasının ardından aboneliğiniz etkinleştirilecektir.\n"
            "Saygılarımızla,\nDeniz Temsilci\nERNASA Yazılım")


def test_one_link_per_proposal_and_period(env):
    db, _, _ = env
    prop = approved(db)
    first = link(db, prop)
    assert first["reused"] is False and first["url"] == f"https://ernasa.com/control/odeme/{first['ref']}"
    again = link(db, prop)
    assert again["ref"] == first["ref"] and again["reused"] is True
    with pytest.raises(sales.Invalid, match="açık bir ödeme bağlantısı var"):
        link(db, prop, plan="Business")
    assert db.execute("SELECT COUNT(*) FROM billing_payment_requests").fetchone()[0] == 1
    # paid period is never re-issued
    cmd(db, FOUNDER, "bank_settings", bank_name="B", account_holder="ERNASA", iban="TR330006100519786457841326", enabled="1")
    with db:
        db.execute("BEGIN IMMEDIATE")
        cid = bp.start_bank_checkout(db, ref=first["ref"], rate=RATE, vat_rate=Decimal("0.20"), now=datetime.now(timezone.utc))
    gross = db.execute("SELECT q.gross_minor FROM billing_checkouts c JOIN billing_quotes q ON q.id=c.quote_id WHERE c.id=?", (cid,)).fetchone()[0]
    cmd(db, FOUNDER, "confirm_bank", checkout_id=cid, transaction_id="TX-1", gross_tl=f"{gross // 100},{gross % 100:02d}")
    with pytest.raises(sales.Invalid, match="ödeme zaten alınmış"):
        link(db, prop)
    with pytest.raises(sales.Invalid, match="ödeme zaten alınmış"):
        link(db, prop, plan="Business")


def test_email_goes_to_registered_contact_with_exact_text_and_is_logged(env):
    db, _, outbox = env
    ref = link(db, approved(db))["ref"]
    out = cmd(db, REP_A, "send_payment_email", ref=ref)
    assert out["status"] == "sent" and "ay•••@example.test" in out["detail"]
    mails = sent_mails(outbox)
    assert len(mails) == 1 and mails[0]["to"] == "ayse@example.test"
    assert mails[0]["subject"] == "ERNASA HR FLOW Growth ödeme bağlantınız"
    assert mails[0]["body"] == EXPECTED.format(ref=ref)
    ev = share.events(db, ref)
    assert [(e["channel"], e["status"], e["recipient"], e["actor_id"]) for e in ev] == [("email", "sent", "ay•••@example.test", "rep-a")]
    with pytest.raises(sales.Invalid, match="10 dakika"):
        cmd(db, REP_A, "send_payment_email", ref=ref)   # accidental double send blocked
    assert len(sent_mails(outbox)) == 1


def test_email_failure_is_recorded_not_hidden(env, monkeypatch):
    db, _, outbox = env
    ref = link(db, approved(db))["ref"]
    monkeypatch.setenv("ERNASA_EMAIL_SANDBOX_FAIL", "1")
    out = cmd(db, REP_A, "send_payment_email", ref=ref)
    assert out["status"] == "failed" and "simüle edilmiş gönderim hatası" in out["detail"]
    ev = share.events(db, ref)
    assert ev[0]["status"] == "failed" and "simüle" in ev[0]["error"]
    monkeypatch.delenv("ERNASA_EMAIL_SANDBOX_FAIL")
    assert cmd(db, REP_A, "send_payment_email", ref=ref)["status"] == "sent"  # failure does not block a retry


def test_whatsapp_opens_click_to_chat_for_registered_number_only(env):
    db, server, outbox = env
    ref = link(db, approved(db))["ref"]
    status, headers, _ = get(server, f"/sales/whatsapp?ref={ref}", session(db, "rep-a"))
    assert status == 303
    target = urlsplit(headers["Location"])
    assert (target.scheme, target.netloc, target.path) == ("https", "wa.me", "/905321112233")
    assert parse_qs(target.query)["text"][0] == EXPECTED.format(ref=ref)
    assert [(e["channel"], e["status"], e["recipient"]) for e in share.events(db, ref)] == [("whatsapp", "opened", "•••••••2233")]
    assert sent_mails(outbox) == []  # nothing is sent by ERNASA itself
    # other representative of the same dealer and another dealer: not found, no redirect, no log
    for account in ("rep-b", "rep-x"):
        status, headers, _ = get(server, f"/sales/whatsapp?ref={ref}", session(db, account))
        assert status == 404 and "Location" not in headers
    assert len(share.events(db, ref)) == 1


def test_whatsapp_number_normalisation():
    assert share.whatsapp_number("0532 111 22 33") == "905321112233"
    assert share.whatsapp_number("532 111 22 33") == "905321112233"
    assert share.whatsapp_number("+90 (533) 444 55 66") == "905334445566"
    assert share.whatsapp_number("0049 30 1234 5678") == "493012345678"
    assert share.whatsapp_number("1234") is None and share.whatsapp_number("") is None


def test_missing_contact_blocks_sharing_with_clear_message(env):
    db, _, _ = env
    ref = link(db, approved(db, company=5, tax="5555555555"))["ref"]
    with pytest.raises(sales.Invalid, match="kayıtlı yetkili e-posta"):
        cmd(db, REP_A, "send_payment_email", ref=ref)
    with pytest.raises(sales.Invalid, match="kayıtlı yetkili telefon"):
        share.whatsapp_url(db, REP_A, ref, datetime.now(timezone.utc))


def test_representative_isolation_and_founder_only_powers(env):
    db, _, _ = env
    prop = approved(db)
    ref = link(db, prop)["ref"]
    for other in (REP_B, REP_X):
        with pytest.raises(sales.Invalid, match="bulunamadı"):
            cmd(db, other, "send_payment_email", ref=ref)
        with pytest.raises(sales.Invalid, match="bulunamadı"):
            link(db, prop, rep=other)          # cannot create on, or learn the link of, someone else's proposal
        props, links = share.tracking(db, other)
        assert props == [] and links == []
        with pytest.raises(sales.Invalid):
            sales_http.status_detail(app, db, other, ref)
    forbidden = [("bank_settings", dict(bank_name="B", account_holder="X", iban="TR330006100519786457841326", enabled="1")),
                 ("confirm_bank", dict(checkout_id="x", transaction_id="T", gross_tl="1,00")),
                 ("verify_order", dict(order_id="1")), ("end_test", {}),
                 ("link_company", dict(customer_id="x", company_id="2")),
                 ("decide", dict(id=prop, decision="approved", reason="x", official_tl="1,00", discount_tl="0,00")),
                 ("payment_request", dict(company_id="2", product="hr_flow", plan_name="Growth", period_start=START))]
    for action, data in forbidden:
        with pytest.raises(sales.Invalid) as err:
            cmd(db, REP_A, action, **data)
        assert err.value.status in (403, 404, 409, 422), action
    assert db.execute("SELECT COUNT(*) FROM billing_bank_settings").fetchone()[0] == 0
    # founder sees every link and can share direct sales with the same tools
    direct = cmd(db, FOUNDER, "payment_request", company_id="4", product="hr_flow", plan_name="Starter", period_start=START)
    assert cmd(db, FOUNDER, "payment_request", company_id="4", product="hr_flow", plan_name="Starter",
               period_start=START)["ref"] == direct["ref"]
    assert cmd(db, FOUNDER, "send_payment_email", ref=direct["ref"])["status"] == "sent"
    url = share.whatsapp_url(db, FOUNDER, direct["ref"], datetime.now(timezone.utc))
    assert url.startswith("https://wa.me/905334445566?text=") and "Erhan%20Kurucu" in url
    _, links = share.tracking(db, FOUNDER)
    assert {l["ref"] for l in links} == {ref, direct["ref"]}


def test_test_package_links_are_never_shared(env, monkeypatch):
    db, _, outbox = env
    monkeypatch.setenv("HR_TEST_PLAN_ENABLED", "1")
    ref = cmd(db, FOUNDER, "payment_request", company_id="2", product="education", plan_name="Ödeme Testi",
              period_start=START)["ref"]
    with pytest.raises(sales.Invalid, match="Test bağlantıları"):
        cmd(db, FOUNDER, "send_payment_email", ref=ref)
    assert sent_mails(outbox) == []
    assert all(l["ref"] != ref for l in share.tracking(db, FOUNDER)[1])


def test_tracking_stages_through_payment_commission_and_refund(env):
    db, _, _ = env
    sub_prop = approved(db)
    pending = cmd(db, REP_A, "customer", name="Bekleyen", tax_id="9999999999")["id"]
    cmd(db, FOUNDER, "classify", id=pending, classification="new", reason="k")
    opp = cmd(db, REP_A, "opportunity", customer_id=pending, title="T", kind="subscription")["id"]
    waiting = cmd(db, REP_A, "proposal", opportunity_id=opp, requested_tl="1,00", brief="Bekleyen teklif")["id"]
    cmd(db, REP_A, "submit", id=waiting)
    props, links = share.tracking(db, REP_A)
    assert {p["id"]: p["stage"] for p in props} == {sub_prop: "proposal_approved", waiting: "proposal_submitted"}
    ref = link(db, sub_prop)["ref"]
    assert share.stage(db, ref)[0] == "ready"
    now = datetime.now(timezone.utc)
    with db:
        db.execute("BEGIN IMMEDIATE")
        bp.start_checkout(db, ref=ref, rate=RATE, vat_rate=Decimal("0.20"), now=now,
                          create_product=shopier_hr.create_product, image_url="x")
    assert share.stage(db, ref)[0] == "awaiting"
    cmd(db, FOUNDER, "bank_settings", bank_name="B", account_holder="ERNASA", iban="TR330006100519786457841326", enabled="1")
    with db:
        db.execute("BEGIN IMMEDIATE")
        cid = bp.start_bank_checkout(db, ref=ref, rate=RATE, vat_rate=Decimal("0.20"), now=now)
    assert share.stage(db, ref)[0] == "bank_review"
    gross = db.execute("SELECT q.gross_minor FROM billing_checkouts c JOIN billing_quotes q ON q.id=c.quote_id WHERE c.id=?", (cid,)).fetchone()[0]
    cmd(db, FOUNDER, "confirm_bank", checkout_id=cid, transaction_id="TX-9", gross_tl=f"{gross // 100},{gross % 100:02d}")
    code, info = share.stage(db, ref)
    assert code == "active" and info["commission_minor"] == 265625   # 249.99 USD x %25 = 62.50 USD x 42.50 = 2.656,25 TL
    assert info["commission_window"] and info["commission_status"] == "pending"
    with pytest.raises(sales.Invalid, match="ödemesi tamamlandı"):
        cmd(db, REP_A, "send_payment_email", ref=ref)
    col = db.execute("SELECT id FROM sales_collections").fetchone()[0]
    cmd(db, FOUNDER, "hold", collection_id=col, reason="iade talebi")
    assert share.stage(db, ref)[0] == "cancelled"


def test_screens_show_tools_only_to_the_owner(env):
    db, _, _ = env
    prop = approved(db)
    ident = lambda actor, name: (actor, "csrf", {"name": name, "email": "x@example.test"})
    page = sales_http.content(app, db, ident(REP_A, "Deniz"), "payments")
    assert "Teklif ve ödeme takibi" in page and "Ödeme bağlantısı oluştur" in page and "Teklif onaylandı" in page
    ref = link(db, prop)["ref"]
    page = sales_http.content(app, db, ident(REP_A, "Deniz"), "payments")
    for label in ("Ödeme bağlantısını görüntüle", "Bağlantıyı kopyala", "WhatsApp ile gönder", "E-posta ile gönder",
                  "Ödeme durumunu sorgula", "Ödeme bağlantısı hazır", f"https://ernasa.com/control/odeme/{ref}",
                  f'data-copy-target="odeme-link-{ref}"', "Ayşe Yılmaz"):
        assert label in page, label
    assert "Ödeme bağlantısı gönderim geçmişi" not in page and "Bekleyen Havale" not in page and "Banka hesabı ve IBAN" not in page
    detail = sales_http.content(app, db, ident(REP_A, "Deniz"), "payments", ref)
    assert "Ödeme durumu" in detail and "Müşteri henüz ödeme yöntemi seçmedi." in detail
    other = sales_http.content(app, db, ident(REP_B, "Ece"), "payments")
    assert ref not in other and "Ayşe" not in other
    proposals_tab = sales_http.content(app, db, ident(REP_A, "Deniz"), "proposals")
    assert "tab=payments#odeme-takibi" in proposals_tab
    cmd(db, REP_A, "send_payment_email", ref=ref)
    founder = sales_http.content(app, db, ident(FOUNDER, "Kurucu"), "payments")
    assert "Ödeme bağlantısı gönderim geçmişi" in founder and "ay•••@example.test" in founder and ref in founder


def test_customer_page_shows_company_package_period_amounts_and_both_methods(env):
    db, server, _ = env
    db.execute("UPDATE company_profile SET tax_id='1234567890', tax_office='Kadıköy' WHERE company_id=2")
    db.execute("INSERT INTO company_addresses(company_id,kind,address,city,district,postal,country) "
               "VALUES(2,'Fatura','Moda Cad. 1','İstanbul','Kadıköy','34710','Türkiye')")
    db.commit()
    cmd(db, FOUNDER, "bank_settings", bank_name="B", account_holder="ERNASA", iban="TR330006100519786457841326", enabled="1")
    ref = link(db, approved(db))["ref"]
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    conn.request("GET", f"/odeme/{ref}", headers={"Host": f"127.0.0.1:{server.server_port}"})
    html = conn.getresponse().read().decode()
    start = date.fromisoformat(START)
    for text in ("Müşteri Lojistik A.Ş.", "Kadıköy", "1234567890", "Moda Cad. 1", "HR FLOW", "Growth",
                 "Aylık 500 aday işlem hakkı", f"Abonelik dönemi: {start:%d.%m.%Y} – ",
                 "11.474,58 TL", "2.294,92 TL", "13.769,50 TL",
                 "Kredi kartı / banka kartı (Shopier)", "Havale / EFT / FAST"):
        assert text in html, text
