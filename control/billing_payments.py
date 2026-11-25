"""Server-side payment verification and exactly-once subscription activation.

Security model
- A browser redirect or webhook is only a *hint* that carries an order id. Activation
  always re-fetches the order from Shopier with SHOPIER_HR_API_TOKEN and matches it
  against an immutable server-side quote.
- An order binds to a checkout only through a single-use provider product id issued
  for that checkout. ERNASA CV products are never registered here, so CV orders can
  never activate HR FLOW / Education.
- Exactly-once is enforced by database keys (provider order PK, checkout UNIQUE,
  quote UNIQUE, subscription+period UNIQUE), not by application checks alone.

Fail-closed: activation is refused unless HR_PAYMENTS_ENABLED=1 *and* the order
field contract has been confirmed against real Shopier API responses.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import re
from uuid import uuid4

from .ik_bridge import ISTANBUL


class PaymentRejected(RuntimeError):
    """Order must not activate anything. Message is safe to log (no secrets/PII)."""


@dataclass(frozen=True)
class OrderContract:
    """Where the verifier reads each value in a Shopier order response.

    The official reference (developer.shopier.com) could not be read from the build
    environment, so these paths are PROVISIONAL until checked against a recorded real
    response. `confirmed=False` keeps activation disabled.
    """
    id: tuple
    payment_status: tuple
    paid_value: str
    currency: tuple
    total: tuple
    lines: tuple
    line_product_id: tuple
    line_quantity: tuple
    refunds: tuple
    returns: tuple
    confirmed: bool


# Paths used by the earlier shopier_hr.py draft plus placeholders for amount/lines.
PROVISIONAL_CONTRACT = OrderContract(
    id=("id",), payment_status=("paymentStatus",), paid_value="paid", currency=("currency",),
    total=("totals", "total"), lines=("lineItems",), line_product_id=("productId",),
    line_quantity=("quantity",), refunds=("refunds",), returns=("returns",), confirmed=False,
)

PROVIDER = "shopier_hr"


def payments_enabled(contract):
    return os.environ.get("HR_PAYMENTS_ENABLED") == "1" and contract.confirmed


def _get(obj, path):
    for key in path:
        if not isinstance(obj, dict) or key not in obj:
            raise PaymentRejected("Sipariş yanıtında zorunlu alan eksik: " + ".".join(path))
        obj = obj[key]
    return obj


def to_minor(value):
    """Exact TRY string/number -> kuruş. Rejects floats and sub-kuruş precision."""
    if isinstance(value, bool) or isinstance(value, float):
        raise PaymentRejected("Tutar kesin ondalık olarak gelmeli.")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise PaymentRejected("Tutar okunamadı.") from None
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal("0.01")):
        raise PaymentRejected("Tutar geçersiz.")
    return int(amount * 100)


@dataclass(frozen=True)
class VerifiedOrder:
    order_id: str
    product_id: str
    gross_minor: int
    response_sha256: str


def read_order(order, contract):
    """Validate provider-side facts that do not depend on our records."""
    order_id = str(_get(order, contract.id))
    if _get(order, contract.payment_status) != contract.paid_value:
        raise PaymentRejected("Ödeme tamamlanmamış.")
    if _get(order, contract.currency) != "TRY":
        raise PaymentRejected("Para birimi TRY değil.")
    if _get(order, contract.refunds) != [] or _get(order, contract.returns) != []:
        raise PaymentRejected("Siparişte iade veya iptal var.")
    lines = _get(order, contract.lines)
    if not isinstance(lines, list) or len(lines) != 1:
        raise PaymentRejected("Sipariş tam olarak bir abonelik kalemi içermeli.")
    quantity = _get(lines[0], contract.line_quantity)
    if isinstance(quantity, bool) or quantity != 1:
        raise PaymentRejected("Ürün miktarı 1 olmalı.")
    product_id = str(_get(lines[0], contract.line_product_id))
    digest = hashlib.sha256(json.dumps(order, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return VerifiedOrder(order_id, product_id, to_minor(_get(order, contract.total)), digest)


def _audit(db, actor, event, target, detail, now):
    db.execute("INSERT INTO billing_audit VALUES(?,?,?,?,?,?)",
               (uuid4().hex, actor, event, target, detail, int(now.timestamp())))


def activate_from_order(db, *, order_id, fetch, contract, now, actor="shopier_verifier",
                        expected_company_id=None):
    """Verify one Shopier order and activate exactly one subscription period.

    Caller owns the transaction: commit on return, roll back on any exception.
    Returns 'activated' or 'already_processed'.
    """
    if not payments_enabled(contract):
        raise PaymentRejected("HR ödeme kabulü etkin değil (sağlayıcı sözleşmesi doğrulanmadı).")
    if not isinstance(order_id, str) or not re.fullmatch(r"[0-9A-Za-z_-]{1,80}", order_id):
        raise PaymentRejected("Geçersiz sipariş numarası.")
    done = db.execute("SELECT checkout_id FROM billing_provider_orders WHERE provider=? AND provider_order_id=?",
                      (PROVIDER, order_id)).fetchone()
    if done:
        return "already_processed"
    v = read_order(fetch(order_id), contract)
    if v.order_id != order_id:
        raise PaymentRejected("Sipariş kimliği uyuşmuyor.")
    checkout = db.execute("SELECT * FROM billing_checkouts WHERE provider=? AND provider_product_id=?",
                          (PROVIDER, v.product_id)).fetchone()
    if checkout is None:
        # Includes every ERNASA CV product and any product not issued by this system.
        raise PaymentRejected("Ürün bu sistemde verilmiş bir HR/Eğitim ödeme oturumuna ait değil.")
    if expected_company_id is not None and checkout["company_id"] != expected_company_id:
        raise PaymentRejected("Müşteri eşleşmesi hatalı.")
    if checkout["status"] != "pending":
        raise PaymentRejected("Ödeme oturumu beklemede değil: " + checkout["status"])
    quote = db.execute("SELECT * FROM billing_quotes WHERE id=? AND company_id=?",
                       (checkout["quote_id"], checkout["company_id"])).fetchone()
    if quote is None or quote["currency"] != "TRY":
        raise PaymentRejected("Teklif ve müşteri eşleşmesi hatalı.")
    if v.gross_minor != int(quote["gross_minor"]):
        raise PaymentRejected("Ödeme tutarı teklif tutarına eşit değil.")

    stamp = int(now.timestamp())
    # Primary key makes a concurrent second delivery fail here; caller rolls back.
    db.execute("INSERT INTO billing_provider_orders VALUES(?,?,?,?,?,?,?,?)",
               (PROVIDER, order_id, checkout["id"], v.gross_minor, "TRY", stamp, actor, v.response_sha256))
    sub = db.execute("SELECT * FROM billing_subscriptions WHERE company_id=? AND product=?",
                     (checkout["company_id"], quote["product"])).fetchone()
    if sub is None:
        sub_id = uuid4().hex
        db.execute("INSERT INTO billing_subscriptions VALUES(?,?,?,?,?)",
                   (sub_id, checkout["company_id"], quote["product"], "active", stamp))
    else:
        sub_id = sub["id"]
        if sub["status"] in ("refunded", "suspended"):
            raise PaymentRejected("Abonelik inceleme durumunda; otomatik aktivasyon yapılmaz.")
        db.execute("UPDATE billing_subscriptions SET status='active' WHERE id=?", (sub_id,))
    db.execute("INSERT INTO billing_periods VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (uuid4().hex, sub_id, quote["id"], checkout["id"], quote["period_start"], quote["period_end"],
                quote["plan_name"], quote["capacity"], quote["positions"], quote["hr_users"],
                quote["net_minor"], quote["vat_minor"], stamp))
    db.execute("UPDATE billing_checkouts SET status='paid' WHERE id=? AND status='pending'", (checkout["id"],))
    _audit(db, actor, "payment_verified_activated", checkout["id"],
           f"order={order_id}; gross_minor={v.gross_minor}; period={quote['period_start']}", now)
    _book_sales(db, checkout, quote, order_id, now, actor)
    return "activated"


def _book_sales(db, checkout, quote, order_id, now, actor):
    """Book the collection + automatic commission for representative-linked checkouts.

    Runs in a savepoint: if a sales rule blocks automatic booking, the subscription stays
    active (customer paid) and the case is queued for CONTROL review instead."""
    from . import sales
    link = db.execute("SELECT * FROM billing_checkout_sales WHERE checkout_id=?", (checkout["id"],)).fetchone()
    if link is None:
        return None  # direct sale: no representative, no commission
    founder = db.execute("SELECT id FROM users WHERE role='founder' AND active=1").fetchone()
    db.execute("SAVEPOINT sales_booking")
    try:
        if founder is None:
            sales.fail("Aktif CONTROL kurucusu yok.")
        collection_id, _ = sales.book_verified_payment(
            db, proposal_id=link["proposal_id"], net_minor=int(quote["net_minor"]),
            reference="shopier_hr:" + order_id, collected_on=now.astimezone(ISTANBUL).date().isoformat(),
            verifier_user_id=founder["id"])
        db.execute("UPDATE billing_checkout_sales SET sales_collection_id=? WHERE checkout_id=?",
                   (collection_id, checkout["id"]))
        db.execute("RELEASE SAVEPOINT sales_booking")
        return collection_id
    except sales.Invalid as exc:
        db.execute("ROLLBACK TO SAVEPOINT sales_booking")
        db.execute("RELEASE SAVEPOINT sales_booking")
        _audit(db, actor, "sales_review_required", checkout["id"], str(exc), now)
        return None


def create_rep_checkout(db, *, actor, proposal_id, product, plan_name, period_start, rate,
                        expected_rate_date, vat_rate, provider_product_id, now):
    """Representative-specific payment session bound to customer + approved proposal.

    The representative may only use their own approved proposal; the paying CONTROL
    company comes from the CONTROL-maintained customer link, never from the rep."""
    from . import sales
    from .billing_quotes import issue_quote
    p = sales.owned(db, actor, "proposals", proposal_id)
    if p["status"] != "approved":
        sales.fail("Ödeme bağlantısı yalnız CONTROL onaylı teklif için oluşturulabilir.")
    op = db.execute("SELECT * FROM sales_opportunities WHERE id=?", (p["opportunity_id"],)).fetchone()
    link = db.execute("SELECT company_id FROM sales_customer_companies WHERE customer_id=?",
                      (op["customer_id"],)).fetchone()
    if link is None:
        sales.fail("Müşterinin CONTROL firma eşleşmesi yapılmamış.")
    quote_id = issue_quote(db, company_id=link["company_id"], product=product, plan_name=plan_name,
                           period_start=period_start, rate=rate, expected_rate_date=expected_rate_date,
                           vat_rate=vat_rate, now=now)
    checkout_id = uuid4().hex
    db.execute("INSERT INTO billing_checkouts VALUES(?,?,?,?,?,?,?,?)",
               (checkout_id, quote_id, link["company_id"], PROVIDER, provider_product_id,
                "ERN-" + checkout_id[:16].upper(), "pending", int(now.timestamp())))
    db.execute("INSERT INTO billing_checkout_sales VALUES(?,?,?,?)",
               (checkout_id, p["id"], p["owner_id"], None))
    _audit(db, actor.id, "rep_checkout_created", checkout_id, f"proposal={p['id']}; plan={product}/{plan_name}", now)
    return checkout_id


def rep_sales(db, actor):
    """Rows for the representative panel (own records only) or CONTROL (all)."""
    sql = """SELECT cu.name AS customer, q.product, q.plan_name, ch.created_at AS sold_at,
                    col.collected_on AS paid_on, q.gross_minor, col.net_minor AS basis_minor,
                    m.rate, m.amount_minor AS commission_minor, m.status AS commission_status,
                    ch.status AS payment_status, s.status AS subscription_status, ch.id AS checkout_id,
                    cs.owner_id
             FROM billing_checkout_sales cs
             JOIN billing_checkouts ch ON ch.id=cs.checkout_id
             JOIN billing_quotes q ON q.id=ch.quote_id
             JOIN sales_proposals p ON p.id=cs.proposal_id
             JOIN sales_opportunities o ON o.id=p.opportunity_id
             JOIN sales_customers cu ON cu.id=o.customer_id
             LEFT JOIN sales_collections col ON col.id=cs.sales_collection_id
             LEFT JOIN sales_commissions m ON m.collection_id=col.id
             LEFT JOIN billing_subscriptions s ON s.company_id=ch.company_id AND s.product=q.product"""
    params = ()
    if not actor.founder:
        sql += " WHERE cs.owner_id=? AND p.company_id=?"
        params = (actor.id, actor.company_id)
    out = []
    from . import sales
    for r in db.execute(sql + " ORDER BY ch.created_at DESC", params).fetchall():
        row = dict(r)
        if row["paid_on"]:
            start, end = sales.payment_window(row["paid_on"])
            row["commission_window"] = f"{start.isoformat()} – {end.isoformat()}"
        else:
            row["commission_window"] = None
        out.append(row)
    return out


def record_webhook(db, *, raw_body: bytes, now):
    """Deduplicate a delivery and extract only the order id hint. Never activates by itself."""
    digest = hashlib.sha256(raw_body).hexdigest()
    try:
        payload = json.loads(raw_body)
    except ValueError:
        payload = None
    order_id = None
    if isinstance(payload, dict):
        for candidate in (payload.get("id"), (payload.get("data") or {}).get("id") if isinstance(payload.get("data"), dict) else None):
            if isinstance(candidate, (str, int)) and re.fullmatch(r"[0-9A-Za-z_-]{1,80}", str(candidate)):
                order_id = str(candidate)
                break
    seen = db.execute("SELECT 1 FROM billing_webhook_events WHERE provider=? AND delivery_sha256=?",
                      (PROVIDER, digest)).fetchone()
    if seen:
        return None
    db.execute("INSERT INTO billing_webhook_events VALUES(?,?,?,?,?)",
               (PROVIDER, digest, order_id, int(now.timestamp()), "received" if order_id else "ignored"))
    return order_id


def consume(db, quote_period_id, subject_key, now):
    """Server-side quota: one unit per candidate/participant process within a paid period.

    Returns True if a new unit was consumed, False if this subject was already counted.
    Raises PaymentRejected when the period is not current or the quota is exhausted.
    """
    period = db.execute(
        """SELECT p.*, s.status FROM billing_periods p JOIN billing_subscriptions s ON s.id=p.subscription_id
           WHERE p.id=?""", (quote_period_id,)).fetchone()
    today = now.date().isoformat()
    if period is None or period["status"] != "active" or not (str(period["period_start"]) <= today < str(period["period_end"])):
        raise PaymentRejected("Aktif ücretli dönem yok; yeni ücretli işlem başlatılamaz.")
    if db.execute("SELECT 1 FROM billing_usage WHERE period_id=? AND subject_key=?",
                  (quote_period_id, subject_key)).fetchone():
        return False
    used = db.execute("SELECT COUNT(*) FROM billing_usage WHERE period_id=?", (quote_period_id,)).fetchone()[0]
    if used >= period["capacity"]:
        raise PaymentRejected("Paket işlem hakkı doldu.")
    db.execute("INSERT INTO billing_usage VALUES(?,?,?)", (quote_period_id, subject_key, int(now.timestamp())))
    return True


def mark_refunded(db, *, order_id, actor, reason, now):
    """Admin action after a provider-confirmed refund: stop entitlement, keep all data."""
    row = db.execute(
        """SELECT o.checkout_id, p.subscription_id FROM billing_provider_orders o
           JOIN billing_periods p ON p.checkout_id=o.checkout_id
           WHERE o.provider=? AND o.provider_order_id=?""", (PROVIDER, order_id)).fetchone()
    if row is None:
        raise PaymentRejected("Doğrulanmış sipariş bulunamadı.")
    db.execute("UPDATE billing_checkouts SET status='refunded' WHERE id=?", (row["checkout_id"],))
    db.execute("UPDATE billing_subscriptions SET status='refunded' WHERE id=?", (row["subscription_id"],))
    link = db.execute("SELECT sales_collection_id FROM billing_checkout_sales WHERE checkout_id=?",
                      (row["checkout_id"],)).fetchone()
    if link and link["sales_collection_id"]:
        # Commission goes into refund review; CONTROL then posts the reversing entry (sales.reverse).
        db.execute("UPDATE sales_collections SET held=1 WHERE id=?", (link["sales_collection_id"],))
    _audit(db, actor, "payment_refunded", row["checkout_id"], reason, now)
