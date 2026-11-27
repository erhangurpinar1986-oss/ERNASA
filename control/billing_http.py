"""Public customer payment page and Shopier webhook for HR FLOW / Education.

Routes (CONTROL server; public under /control):
  GET  /odeme/<REF>                 customer page: package, limits, TL net / KDV / total, terms
  POST /odeme/<REF>                 terms accepted -> 15-min quote + single-use Shopier link (method=shopier)
                                    or bank transfer instructions with IBAN snapshot (method=bank)
  POST /odeme/shopier/webhook       Shopier notification (hint only; order re-fetched by API)
"""
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import re
import threading

from . import billing_payments as bp, shopier_hr, tcmb_rates
from .billing_quotes import amounts_for, find_plan
from .ik_bridge import ISTANBUL

VAT_RATE = Decimal("0.20")
LABELS = {"hr_flow": "ERNASA HR FLOW", "education": "ERNASA Eğitim Platformu"}
STATUS = {"pending": "Ödeme bekleniyor", "paid": "Ödendi", "failed": "Başarısız", "expired": "Süresi doldu",
          "cancelled": "İptal edildi", "refunded": "İade edildi"}
WEBHOOK_PATH = "/odeme/shopier/webhook"
REF = re.compile(r"/odeme/(ERN[0-9A-F]{20})")


def money(minor):
    whole, cents = divmod(int(minor), 100)
    return f"{whole:,}".replace(",", ".") + f",{cents:02d} TL"


def now():
    return datetime.now(timezone.utc)


def image_url(h):
    return h.server.origin + h.server.public_prefix + "/ernasalogo.png"


def limits(plan, product):
    if product == "hr_flow":
        return (f"<li>Aylık {plan.capacity} aday işlem hakkı (ATS + gerektiğinde AI mülakat tek hak)</li>"
                f"<li>{plan.positions} aktif pozisyon</li><li>{plan.hr_users} İK kullanıcısı</li>")
    return f"<li>Aylık {plan.capacity} eğitim katılımcısı</li>"


def page(c, h, db, ref, error=None, status=200):
    req, checkouts = bp.request_view(db, ref)
    plan = find_plan(req["product"], req["plan_name"], allow_test=True)
    paid = next((x for x in checkouts if x["status"] in ("paid", "refunded")), None)
    at = int(now().timestamp())
    live = next((x for x in checkouts if x["status"] == "pending" and x["expires_at"] > at and x["provider_url"]), None)
    body = (f'<p>{c.e(req["company_name"])} için hazırlanan ödeme bağlantısı · Referans <strong>{c.e(ref)}</strong></p>'
            f'<h2>{c.e(LABELS[req["product"]])} · {c.e(plan.name)}</h2><ul>{limits(plan, req["product"])}</ul>'
            f'<p>Liste fiyatı: {plan.monthly_usd} USD / ay (KDV hariç) · Dönem başlangıcı: {c.e(str(req["period_start"]))}</p>')
    if error:
        body += f'<p role="alert">{c.e(error)}</p>'
    if paid:
        sub = db.execute("SELECT status FROM billing_subscriptions WHERE company_id=? AND product=?",
                         (req["company_id"], req["product"])).fetchone()
        body += (f'<p><strong>Ödeme durumu: {STATUS[paid["status"]]}</strong> · Tutar {money(paid["gross_minor"])}</p>'
                 f'<p>Abonelik: {c.e(sub["status"] if sub else "-")}</p>')
        return h.page("ERNASA ödeme", body, status=status, brand="ERNASA")
    try:
        rate = tcmb_rates.fetch_latest_usd_selling_rate(now().astimezone(ISTANBUL).date())
        amounts = amounts_for(plan, rate.value, VAT_RATE)
        body += (f'<table><tr><th>KDV hariç</th><td>{money(amounts.net_minor)}</td></tr>'
                 f'<tr><th>KDV (%20)</th><td>{money(amounts.vat_minor)}</td></tr>'
                 f'<tr><th>KDV dahil toplam</th><td><strong>{money(amounts.gross_minor)}</strong></td></tr></table>'
                 f'<p><small>Kur: TCMB döviz satış (gösterge) {rate.value} TL/USD · {rate.published_on.strftime("%d.%m.%Y")} bülteni. '
                 'Tutar ödeme başlatıldığında 15 dakika sabitlenir; kur güncellemesi başlatılmış ödemeyi değiştirmez.</small></p>')
    except tcmb_rates.RateUnavailable as exc:
        body += f'<p role="alert">{c.e(str(exc))} Lütfen daha sonra tekrar deneyin.</p>'
        return h.page("ERNASA ödeme", body, status=503, brand="ERNASA")
    terms = ('<label><input type="checkbox" name="terms" value="1" required> '
             'Hizmet sözleşmesini, mesafeli satış ve KVKK aydınlatma metnini okudum, kabul ediyorum. '
             'Abonelik aylıktır; otomatik yenileme yoktur, her dönem için yeni ödeme bağlantısı gönderilir.</label>')
    body += ('<h3>Ödeme yöntemi</h3><p><small>Satış fiyatı ödeme yöntemine göre değişmez; '
             'kart ve banka ödemesinde aynı KDV dahil tutar uygulanır.</small></p>')
    # 1) Shopier: credit card and debit card on the same Shopier secure page.
    body += '<section aria-labelledby="pay-card"><h4 id="pay-card">Kredi kartı / banka kartı (Shopier)</h4>'
    if live:
        body += (f'<p>Ödeme tutarı {money(live["gross_minor"])} olarak sabitlendi.</p>'
                 f'<p><a class="button" href="{c.e(live["provider_url"])}" rel="noopener">Shopier ile güvenli öde</a></p>'
                 '<p><small>Kart bilgileriniz ERNASA sunucularına gelmez; ödeme Shopier güvenli ödeme sayfasında yapılır. '
                 'Ödeme sonrası abonelik, Shopier API ile doğrulandıktan sonra otomatik etkinleşir.</small></p>')
    elif not (bp.test_plan_enabled() if req["is_test"] else os.environ.get("HR_PAYMENTS_ENABLED") == "1"):
        body += '<p role="status">Kartla online ödeme kabulü henüz açılmadı.</p>'
    else:
        body += (f'<form method="post" action="/odeme/{c.e(ref)}"><input type="hidden" name="method" value="shopier">'
                 + terms + '<button type="submit">Kartla ödemeye geç</button></form>')
    body += '</section>'
    # 2) Havale / EFT / FAST to ERNASA's own account; founder confirms the collection in CONTROL.
    ready = bp.bank_ready(db)
    settings = bp.bank_settings(db) if ready else None
    bank_ok = req["product"] == "hr_flow" and not req["is_test"] and settings is not None and bool(settings["enabled"])
    bank = bp.bank_checkout(db, ref) if ready else None
    body += '<section aria-labelledby="pay-bank"><h4 id="pay-bank">Havale / EFT / FAST</h4>'
    if bank is not None and bank["status"] in ("pending", "expired"):
        body += (f'<table><tr><th>Banka</th><td>{c.e(bank["bank_name"])}</td></tr>'
                 f'<tr><th>Hesap sahibi</th><td>{c.e(bank["account_holder"])}</td></tr>'
                 f'<tr><th>IBAN</th><td><strong>{c.e(format_iban(bank["iban"]))}</strong></td></tr>'
                 f'<tr><th>Gönderilecek tutar</th><td><strong>{money(bank["gross_minor"])}</strong></td></tr>'
                 f'<tr><th>Açıklama (zorunlu)</th><td><strong>{c.e(bank["payment_reference"])}</strong></td></tr></table>'
                 '<p><small>Tutarı kuruşu kuruşuna aynı gönderin ve açıklamaya referans kodunu yazın. '
                 'Abonelik, tahsilat ERNASA CONTROL tarafından banka hesabında doğrulandıktan sonra etkinleşir. '
                 'Bu tutar ödeme talimatı oluşturulduğu andaki kurla sabitlenmiştir.</small></p>')
    elif bank_ok:
        body += (f'<form method="post" action="/odeme/{c.e(ref)}"><input type="hidden" name="method" value="bank">'
                 + terms + '<button type="submit">Havale / EFT / FAST bilgilerini göster</button></form>')
    else:
        body += '<p role="status">Banka ödemesi bu bağlantı için kullanılamıyor.</p>'
    body += '</section>'
    h.page("ERNASA ödeme", body, status=status, brand="ERNASA")


def format_iban(iban):
    return " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))


def read_body(h, limit=65536):
    size = int(h.headers.get("Content-Length", "0") or 0)
    if not 0 < size <= limit:
        raise c_problem(413)
    return h.rfile.read(size)


class c_problem(Exception):
    def __init__(self, status):
        self.status = status


def webhook(c, h):
    raw = read_body(h)
    if not bp.signature_ok(raw, h.headers.get("Shopier-Signature", "")):
        return h.reply(200, b"ignored", "text/plain; charset=utf-8")
    sold = None
    with closing(c.connect(h.server.db_path)) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            hint = bp.record_webhook(db, webhook_id=h.headers.get("Shopier-Webhook-Id", ""),
                                     event=h.headers.get("Shopier-Event", ""), raw_body=raw, now=now())
            if hint:
                event, order_id = hint
                db.execute("SAVEPOINT activation")
                try:
                    if event == "order.created":
                        if bp.activate_from_order(db, order_id=order_id, fetch=shopier_hr.fetch_order,
                                                  contract=bp.OFFICIAL_CONTRACT, now=now()) == "activated":
                            sold = db.execute("""SELECT c.provider_product_id FROM billing_provider_orders o
                                                 JOIN billing_checkouts c ON c.id=o.checkout_id
                                                 WHERE o.provider=? AND o.provider_order_id=?""",
                                              (bp.PROVIDER, order_id)).fetchone()
                    else:
                        bp.refund_from_order(db, order_id=order_id, fetch=shopier_hr.fetch_order, now=now())
                    db.execute("RELEASE SAVEPOINT activation")
                except bp.PaymentRejected as exc:
                    # Not ours (e.g. ERNASA CV order) or does not match: keep dedupe record, activate nothing.
                    db.execute("ROLLBACK TO SAVEPOINT activation")
                    db.execute("RELEASE SAVEPOINT activation")
                    bp._audit(db, "shopier_webhook", "payment_rejected", order_id, str(exc), now())
            db.commit()
            if sold:
                # Sold single-use listing is no longer needed in the shop; best effort, off the 5 s path.
                threading.Thread(target=_quiet_delete, args=(sold[0],), daemon=True).start()
        except shopier_hr.ShopierError:
            # Transient provider problem: roll back the dedupe record so Shopier's retry is processed.
            _rollback(db)
            return h.reply(503, b"retry", "text/plain; charset=utf-8")
        except Exception:
            _rollback(db)
            raise
    h.reply(200, b"ok", "text/plain; charset=utf-8")


def _quiet_delete(product_id):
    try:
        shopier_hr.delete_product(product_id)
    except Exception:
        pass


def _rollback(db):
    try:
        (db.raw if hasattr(db, "raw") else db).rollback()
    except Exception:
        pass


def handle(h, path, post):
    from . import app as c
    if path == WEBHOOK_PATH and post:
        try:
            webhook(c, h)
        except c_problem as exc:
            h.reply(exc.status, b"", "text/plain; charset=utf-8")
        return True
    match = REF.fullmatch(path)
    if not match:
        return False
    ref = match[1]
    with closing(c.connect(h.server.db_path)) as db, db:
        try:
            if post:
                data = h.data()  # enforces same Origin; no session needed for a customer link
                if data.get("terms") != "1":
                    return page(c, h, db, ref, "Devam etmek için koşulları kabul edin.", 422)
                db.execute("BEGIN IMMEDIATE")
                rate = tcmb_rates.fetch_latest_usd_selling_rate(now().astimezone(ISTANBUL).date())
                if data.get("method") == "bank":
                    bp.start_bank_checkout(db, ref=ref, rate=rate, vat_rate=VAT_RATE, now=now())
                    db.commit()
                    return h.redirect(f"/odeme/{ref}")
                bp.start_checkout(db, ref=ref, rate=rate, vat_rate=VAT_RATE, now=now(),
                                  create_product=shopier_hr.create_product, image_url=image_url(h),
                                  delete_product=shopier_hr.delete_product)
                db.commit()
                return h.redirect(f"/odeme/{ref}")
            return page(c, h, db, ref)
        except bp.PaymentRejected as exc:
            _rollback(db)
            if str(exc) == "Ödeme bağlantısı bulunamadı.":
                return h.page("ERNASA ödeme", f"<p>{c.e(str(exc))}</p>", status=404, brand="ERNASA")
            return page(c, h, db, ref, str(exc), 409)
        except (shopier_hr.ShopierError, tcmb_rates.RateUnavailable) as exc:
            _rollback(db)
            return page(c, h, db, ref, f"Ödeme başlatılamadı: {exc}", 503)
