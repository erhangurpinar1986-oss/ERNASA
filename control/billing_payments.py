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

Fail-closed: no Shopier payment link is created and nothing activates unless
HR_PAYMENTS_ENABLED=1 is set on the server.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import re
from uuid import uuid4

from .ik_bridge import ISTANBUL
from .billing_catalog import commission_basis_for_quote


class PaymentRejected(RuntimeError):
    """Order must not activate anything. Message is safe to log (no secrets/PII)."""


@dataclass(frozen=True)
class OrderContract:
    """Where the verifier reads each value in a Shopier order response.

    `confirmed=False` keeps activation disabled (used in tests).
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


# Field paths from the official Order model (developer.shopier.com/reference/the-order-model),
# read 2026-11-25: id, paymentStatus (paid|unpaid), currency (TRY|USD|EUR), totals.total (string),
# lineItems[].productId (string), lineItems[].quantity (integer), refunds[], returns[].
OFFICIAL_CONTRACT = OrderContract(
    id=("id",), payment_status=("paymentStatus",), paid_value="paid", currency=("currency",),
    total=("totals", "total"), lines=("lineItems",), line_product_id=("productId",),
    line_quantity=("quantity",), refunds=("refunds",), returns=("returns",), confirmed=True,
)
PROVISIONAL_CONTRACT = OrderContract(**{**OFFICIAL_CONTRACT.__dict__, "confirmed": False})

PROVIDER = "shopier_hr"


def payments_enabled(contract):
    return os.environ.get("HR_PAYMENTS_ENABLED") == "1" and contract.confirmed


def test_plan_enabled():
    return os.environ.get("HR_TEST_PLAN_ENABLED") == "1"


def _is_test_checkout(db, checkout_id):
    row = db.execute("""SELECT r.is_test FROM billing_checkout_requests x
                        JOIN billing_payment_requests r ON r.ref=x.request_ref WHERE x.checkout_id=?""",
                     (checkout_id,)).fetchone()
    return bool(row and row[0])


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
    if not contract.confirmed or not (payments_enabled(contract) or test_plan_enabled()):
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
    is_test = _is_test_checkout(db, checkout["id"])
    if not (payments_enabled(contract) or (is_test and test_plan_enabled())):
        raise PaymentRejected("Online ödeme kabulü etkin değil.")
    if expected_company_id is not None and checkout["company_id"] != expected_company_id:
        raise PaymentRejected("Müşteri eşleşmesi hatalı.")
    # 'expired' is accepted: the 15-minute window only limits starting a payment; a customer who
    # completed Shopier checkout at the fixed amount must still be activated.
    if checkout["status"] not in ("pending", "expired"):
        raise PaymentRejected("Ödeme oturumu beklemede değil: " + checkout["status"])
    quote = db.execute("SELECT * FROM billing_quotes WHERE id=? AND company_id=?",
                       (checkout["quote_id"], checkout["company_id"])).fetchone()
    if quote is None or quote["currency"] != "TRY":
        raise PaymentRejected("Teklif ve müşteri eşleşmesi hatalı.")
    if v.gross_minor != int(quote["gross_minor"]):
        raise PaymentRejected("Ödeme tutarı teklif tutarına eşit değil.")

    return _activate_verified(db, checkout, quote, PROVIDER, order_id, v.gross_minor, v.response_sha256, now, actor, is_test)


def _activate_verified(db, checkout, quote, provider, order_id, gross_minor, digest, now, actor, is_test=False):
    # Both payment methods use the same immutable period and provider-order constraints.
    request = db.execute("SELECT request_ref FROM billing_checkout_requests WHERE checkout_id=?", (checkout["id"],)).fetchone()
    if request and db.execute("""SELECT 1 FROM billing_checkout_requests x JOIN billing_checkouts ch ON ch.id=x.checkout_id
             WHERE x.request_ref=? AND ch.status IN ('paid','refunded')""", (request["request_ref"],)).fetchone():
        raise PaymentRejected("Bu ödeme talebi daha önce tahsil edilmiş; CONTROL incelemesi gerekli.")
    stamp = int(now.timestamp())
    # Primary key makes a concurrent second delivery fail here; caller rolls back.
    db.execute("INSERT INTO billing_provider_orders VALUES(?,?,?,?,?,?,?,?)",
               (provider, order_id, checkout["id"], gross_minor, "TRY", stamp, actor, digest))
    sub = db.execute("SELECT * FROM billing_subscriptions WHERE company_id=? AND product=?",
                     (checkout["company_id"], quote["product"])).fetchone()
    if sub is None:
        sub_id = uuid4().hex
        db.execute("INSERT INTO billing_subscriptions VALUES(?,?,?,?,?)",
                   (sub_id, checkout["company_id"], quote["product"], "active", stamp))
    else:
        sub_id = sub["id"]
        if sub["status"] == "suspended":
            raise PaymentRejected("Abonelik askıda; otomatik aktivasyon yapılmaz.")
        db.execute("UPDATE billing_subscriptions SET status='active' WHERE id=?", (sub_id,))
    db.execute("INSERT INTO billing_periods VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (uuid4().hex, sub_id, quote["id"], checkout["id"], quote["period_start"], quote["period_end"],
                quote["plan_name"], quote["capacity"], quote["positions"], quote["hr_users"],
                quote["net_minor"], quote["vat_minor"], stamp))
    db.execute("UPDATE billing_checkouts SET status='paid' WHERE id=? AND status IN ('pending','expired')", (checkout["id"],))
    _audit(db, actor, "payment_verified_activated", checkout["id"],
           f"order={order_id}; gross_minor={gross_minor}; period={quote['period_start']}", now)
    if quote["product"] == "hr_flow" and not is_test:
        grant_hr_flow(db, company_id=checkout["company_id"], quote=quote)
    if is_test:
        _record_test(db, checkout, quote, order_id, now, actor)
    else:
        _book_sales(db, checkout, quote, order_id, now, actor)
    return "activated"


def _record_test(db, checkout, quote, order_id, now, actor):
    """Run the real Sales Hub booking + commission code, record what it computed, then roll the
    Sales Hub rows back: a test is never revenue and never a payable commission."""
    from . import sales
    link = db.execute("SELECT * FROM billing_checkout_sales WHERE checkout_id=?", (checkout["id"],)).fetchone()
    net = rate = amount = window = None
    detail = "Doğrudan satış: temsilci yok, komisyon oluşmaz."
    if link is not None:
        founder = db.execute("SELECT id FROM users WHERE role='founder' AND active=1").fetchone()
        db.execute("SAVEPOINT test_sales")
        try:
            collection_id, rate = sales.book_verified_payment(
                db, proposal_id=link["proposal_id"], net_minor=int(quote["net_minor"]),
                reference="TEST-shopier_hr:" + order_id,
                collected_on=now.astimezone(ISTANBUL).date().isoformat(), verifier_user_id=founder["id"])
            row = db.execute("""SELECT c.net_minor, c.collected_on, m.amount_minor FROM sales_collections c
                                JOIN sales_commissions m ON m.collection_id=c.id WHERE c.id=?""",
                             (collection_id,)).fetchone()
            net, amount = int(row["net_minor"]), int(row["amount_minor"])
            start, end = sales.payment_window(str(row["collected_on"]))
            window = f"{start.isoformat()} – {end.isoformat()}"
            detail = f"Temsilci {link['owner_id']}: Sales Hub kaydı ve komisyon hesaplandı, test olduğu için geri alındı."
        except sales.Invalid as exc:
            detail = "Sales Hub kaydı oluşturulamadı: " + str(exc)
        db.execute("ROLLBACK TO SAVEPOINT test_sales")
        db.execute("RELEASE SAVEPOINT test_sales")
    db.execute("INSERT INTO billing_test_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (checkout["id"], order_id, int(now.timestamp()), int(quote["gross_minor"]), int(quote["capacity"]),
                quote["period_start"], quote["period_end"], net, rate, amount, window, detail))
    _audit(db, actor, "test_payment_verified", checkout["id"], f"order={order_id}; {detail}", now)


def end_test(db, *, actor, now):
    """Close test subscriptions whose periods all come from test payments. Data is kept."""
    rows = db.execute("""SELECT s.id FROM billing_subscriptions s WHERE s.status='active' AND NOT EXISTS (
                             SELECT 1 FROM billing_periods p
                             JOIN billing_checkout_requests x ON x.checkout_id=p.checkout_id
                             JOIN billing_payment_requests r ON r.ref=x.request_ref
                             WHERE p.subscription_id=s.id AND r.is_test=FALSE)
                         AND EXISTS (SELECT 1 FROM billing_periods p WHERE p.subscription_id=s.id)""").fetchall()
    for r in rows:
        db.execute("UPDATE billing_subscriptions SET status='cancelled' WHERE id=?", (r["id"],))
    _audit(db, str(actor.id), "test_closed", "billing", f"cancelled_test_subscriptions={len(rows)}", now)
    return len(rows)


def grant_hr_flow(db, *, company_id, quote):
    """Mirror the paid HR FLOW period into the existing entitlement tables that the live
    portal already enforces (AI interview product `subscriptions`/`service_terms`, ATS
    `ats_subscriptions`). Previous values are snapshotted to subscription_history; nothing is deleted.
    operation_quota = monthly candidate process quota; user_quota = HR users."""
    from datetime import date, timedelta
    start = quote["period_start"]
    start = start if isinstance(start, date) else date.fromisoformat(str(start))
    end = quote["period_end"]
    end = end if isinstance(end, date) else date.fromisoformat(str(end))
    last_day = (end - timedelta(days=1)).isoformat()   # existing tables use inclusive end dates
    package = "HR FLOW " + quote["plan_name"]
    prev = {k: db.execute(f"SELECT * FROM {k} WHERE company_id=?", (company_id,)).fetchone()
            for k in ("subscriptions", "service_terms", "ats_subscriptions")}
    if any(prev.values()):
        db.execute("INSERT INTO subscription_history(company_id,snapshot) VALUES(?,?)",
                   (company_id, json.dumps({k: (dict(v) if v else None) for k, v in prev.items()},
                                           ensure_ascii=False, default=str)))
    # Renewal paid in advance must not shorten a running period.
    starts = min(start.isoformat(), str(prev["subscriptions"]["starts"])) if (
        prev["subscriptions"] and prev["subscriptions"]["status"] == "Aktif"
        and str(prev["subscriptions"]["ends"]) >= (start - timedelta(days=1)).isoformat()) else start.isoformat()
    db.execute("""INSERT INTO subscriptions VALUES(?,?,?,?,?) ON CONFLICT(company_id)
                  DO UPDATE SET package=excluded.package, starts=excluded.starts, ends=excluded.ends, status=excluded.status""",
               (company_id, package, starts, last_day, "Aktif"))
    db.execute("""INSERT INTO service_terms VALUES(?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(company_id) DO UPDATE SET product=excluded.product, version=excluded.version,
                  user_quota=excluded.user_quota, operation_quota=excluded.operation_quota, trial_days=excluded.trial_days,
                  period=excluded.period, price_minor=excluded.price_minor, currency=excluded.currency,
                  vat_basis=excluded.vat_basis, payment=excluded.payment""",
               (company_id, "ernasa-ik", "hr-flow-2026", quote["hr_users"], quote["capacity"], None, "Aylık",
                int(quote["net_minor"]), "TRY", 2000, "Ödendi"))
    db.execute("""INSERT INTO ats_subscriptions VALUES(?,?,?,?,?,?) ON CONFLICT(company_id)
                  DO UPDATE SET package=excluded.package, starts=excluded.starts, ends=excluded.ends,
                  status=excluded.status, user_quota=excluded.user_quota""",
               (company_id, package, starts, last_day, "Aktif", quote["hr_users"]))


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
            reference=checkout["provider"] + ":" + order_id, collected_on=now.astimezone(ISTANBUL).date().isoformat(),
            verifier_user_id=founder["id"], commission_basis_minor=commission_basis_for_quote(quote))
        db.execute("UPDATE billing_checkout_sales SET sales_collection_id=? WHERE checkout_id=?",
                   (collection_id, checkout["id"]))
        db.execute("RELEASE SAVEPOINT sales_booking")
        return collection_id
    except sales.Invalid as exc:
        db.execute("ROLLBACK TO SAVEPOINT sales_booking")
        db.execute("RELEASE SAVEPOINT sales_booking")
        _audit(db, actor, "sales_review_required", checkout["id"], str(exc), now)
        return None


def create_payment_request(db, *, actor, product, plan_name, period_start, now,
                           proposal_id=None, company_id=None):
    """Representative link (bound to own approved proposal) or CONTROL direct-sale link.

    Representatives cannot choose the paying company: it comes from the CONTROL-maintained
    customer link. Only the founder may create direct (commission-free) requests."""
    from . import sales
    from .billing_quotes import TEST_PLAN, find_plan
    if find_plan(product, plan_name, allow_test=actor.founder) is None:
        sales.fail("Paket bulunamadı.", 422)
    if plan_name == TEST_PLAN.name and not actor.founder:
        sales.fail("Test paketi yalnız CONTROL kurucusuna açıktır.", 403)
    owner = None
    if proposal_id:
        p = sales.owned(db, actor, "proposals", proposal_id)
        if p["status"] != "approved":
            sales.fail("Ödeme bağlantısı yalnız CONTROL onaylı teklif için oluşturulabilir.")
        op = db.execute("SELECT * FROM sales_opportunities WHERE id=?", (p["opportunity_id"],)).fetchone()
        if op["kind"] == "project":
            sales.fail("Proje satışları için abonelik ödeme bağlantısı oluşturulamaz.")
        link = db.execute("SELECT company_id FROM sales_customer_companies WHERE customer_id=?",
                          (op["customer_id"],)).fetchone()
        if link is None:
            sales.fail("Müşterinin CONTROL firma eşleşmesi yapılmamış; CONTROL yöneticisine bildirin.")
        company_id, owner, proposal_id = link["company_id"], p["owner_id"], p["id"]
    else:
        sales.admin(actor)
        if not db.execute("SELECT 1 FROM companies WHERE id=?", (company_id,)).fetchone():
            sales.fail("Firma bulunamadı.", 404)
    ref = "ERN" + uuid4().hex[:20].upper()
    is_test = plan_name == TEST_PLAN.name
    if is_test and not test_plan_enabled():
        sales.fail("Test paketi şu an devre dışı.", 409)
    db.execute("""INSERT INTO billing_payment_requests(ref,company_id,product,plan_name,period_start,proposal_id,
                  owner_id,created_by,created_at,is_test) VALUES(?,?,?,?,?,?,?,?,?,?)""",
               (ref, company_id, product, plan_name, period_start.isoformat(), proposal_id, owner,
                str(actor.id), int(now.timestamp()), is_test))
    _audit(db, str(actor.id), "payment_request_created", ref, f"{product}/{plan_name}; proposal={proposal_id}", now)
    return ref


def request_view(db, ref):
    row = db.execute("""SELECT r.*, c.name AS company_name FROM billing_payment_requests r
                        JOIN companies c ON c.id=r.company_id WHERE r.ref=?""", (ref,)).fetchone()
    if row is None:
        raise PaymentRejected("Ödeme bağlantısı bulunamadı.")
    checkouts = db.execute("""SELECT ch.*, q.gross_minor, q.expires_at FROM billing_checkout_requests x
                              JOIN billing_checkouts ch ON ch.id=x.checkout_id JOIN billing_quotes q ON q.id=ch.quote_id
                              WHERE x.request_ref=? ORDER BY ch.created_at DESC""", (ref,)).fetchall()
    return row, checkouts


def start_checkout(db, *, ref, rate, vat_rate, now, create_product, image_url, delete_product=None):
    """Fix price+rate for 15 minutes and create the single-use Shopier payment link.

    Returns the Shopier product URL. Reuses a still-valid pending checkout for the same request.
    Caller owns the transaction (commit only after this returns)."""
    from .billing_quotes import issue_quote
    req, checkouts = request_view(db, ref)
    if req["is_test"]:
        if not test_plan_enabled():
            raise PaymentRejected("Test paketi devre dışı.")
    elif os.environ.get("HR_PAYMENTS_ENABLED") != "1":
        raise PaymentRejected("Online ödeme henüz etkin değil.")
    if any(c["status"] == "paid" for c in checkouts):
        raise PaymentRejected("Bu ödeme bağlantısı zaten ödendi.")
    at = int(now.timestamp())
    for c in checkouts:
        if c["provider"] == PROVIDER and c["status"] == "pending" and c["expires_at"] > at and c["provider_url"]:
            return c["provider_url"]
    stale = [c for c in checkouts if c["status"] == "pending" and c["expires_at"] <= at]
    for c in stale:
        db.execute("UPDATE billing_checkouts SET status='expired' WHERE id=? AND status='pending'", (c["id"],))
        if delete_product and c["provider"] == PROVIDER:
            try:  # best effort: remove the outdated single-use listing from the shop
                delete_product(c["provider_product_id"])
            except Exception:
                pass
    quote_id = issue_quote(db, company_id=req["company_id"], product=req["product"], plan_name=req["plan_name"],
                           period_start=_date(req["period_start"]), rate=rate, expected_rate_date=rate.published_on,
                           vat_rate=vat_rate, now=now)
    quote = db.execute("SELECT * FROM billing_quotes WHERE id=?", (quote_id,)).fetchone()
    label = {"hr_flow": "ERNASA HR FLOW", "education": "ERNASA Eğitim Platformu"}[req["product"]]
    product_id, url = create_product(
        title=f"{label} {req['plan_name']} · {quote['period_start']} · {ref}",
        description=(f"{req['company_name']} için {quote['period_start']} – {quote['period_end']} dönemi aylık abonelik. "
                     f"Referans {ref}. Yalnız bu firma için geçerlidir; tek kullanımlıktır."),
        gross_minor=int(quote["gross_minor"]), image_url=image_url)
    checkout_id = uuid4().hex
    db.execute("INSERT INTO billing_checkouts VALUES(?,?,?,?,?,?,?,?,?)",
               (checkout_id, quote_id, req["company_id"], PROVIDER, product_id, url,
                ref + "-" + checkout_id[:6].upper(), "pending", at))
    db.execute("INSERT INTO billing_checkout_requests VALUES(?,?)", (checkout_id, ref))
    if req["proposal_id"]:
        db.execute("INSERT INTO billing_checkout_sales VALUES(?,?,?,?)",
                   (checkout_id, req["proposal_id"], req["owner_id"], None))
    _audit(db, "customer", "checkout_started", checkout_id, f"ref={ref}; gross_minor={quote['gross_minor']}", now)
    return url


def _date(value):
    from datetime import date
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def rep_sales(db, actor):
    """Payment requests with payment/commission state: representative sees own, CONTROL sees all."""
    sql = """SELECT r.ref, COALESCE(cu.name, co.name) AS customer, co.name AS company, r.product, r.plan_name,
                    r.created_at AS sold_at, r.owner_id, col.collected_on AS paid_on, q.gross_minor,
                    q.net_minor AS quote_net_minor, col.net_minor AS basis_minor, m.rate,
                    q.monthly_usd AS quote_monthly_usd, q.exchange_rate AS quote_exchange_rate, ch.provider,
                    m.amount_minor AS commission_minor, m.status AS commission_status, col.held,
                    ch.status AS payment_status, s.status AS subscription_status, r.is_test,
                    t.sales_net_minor AS test_basis, t.sales_rate AS test_rate,
                    t.sales_commission_minor AS test_commission, t.commission_window AS test_window
             FROM billing_payment_requests r
             JOIN companies co ON co.id=r.company_id
             LEFT JOIN sales_proposals p ON p.id=r.proposal_id
             LEFT JOIN sales_opportunities o ON o.id=p.opportunity_id
             LEFT JOIN sales_customers cu ON cu.id=o.customer_id
             LEFT JOIN billing_checkouts ch ON ch.id=(
                 SELECT x.checkout_id FROM billing_checkout_requests x JOIN billing_checkouts c2 ON c2.id=x.checkout_id
                 WHERE x.request_ref=r.ref AND c2.status IN ('paid','refunded') LIMIT 1)
             LEFT JOIN billing_quotes q ON q.id=ch.quote_id
             LEFT JOIN billing_checkout_sales cs ON cs.checkout_id=ch.id
             LEFT JOIN sales_collections col ON col.id=cs.sales_collection_id
             LEFT JOIN sales_commissions m ON m.collection_id=col.id
             LEFT JOIN billing_subscriptions s ON s.company_id=r.company_id AND s.product=r.product
             LEFT JOIN billing_test_results t ON t.checkout_id=ch.id"""
    params = ()
    if not actor.founder:
        sql += " WHERE r.owner_id=? AND p.company_id=? AND r.is_test=FALSE"
        params = (actor.id, actor.company_id)
    from . import sales
    out = []
    for r in db.execute(sql + " ORDER BY r.created_at DESC", params).fetchall():
        row = dict(r)
        row["payment_status"] = row["payment_status"] or "awaiting_payment"
        if row["basis_minor"] is not None and not row["is_test"]:
            # Show the basis the commission was actually computed on (approved HR FLOW basis).
            row["collection_net_minor"] = row["basis_minor"]
            row["basis_minor"] = commission_basis_for_quote({
                "net_minor": row["basis_minor"], "product": row["product"], "plan_name": row["plan_name"],
                "monthly_usd": row["quote_monthly_usd"], "exchange_rate": row["quote_exchange_rate"]})
        if row["paid_on"]:
            start, end = sales.payment_window(row["paid_on"])
            row["commission_window"] = f"{start.isoformat()} – {end.isoformat()}"
        else:
            row["commission_window"] = None
        out.append(row)
    return out


def signature_ok(raw_body: bytes, header_value: str) -> bool:
    """Shopier-Signature = HS256(webhook token, body). Encoding is not stated in the reference,
    so hex and base64 are both accepted. Without a configured token this returns True: the
    webhook is only a hint and every order is re-fetched from the API before activation."""
    import base64
    import hmac
    token = os.environ.get("SHOPIER_HR_WEBHOOK_TOKEN", "")
    if not token:
        return True
    mac = hmac.new(token.encode(), raw_body, hashlib.sha256).digest()
    given = (header_value or "").strip()
    return any(hmac.compare_digest(given, candidate)
               for candidate in (mac.hex(), base64.b64encode(mac).decode()))


def record_webhook(db, *, webhook_id, event, raw_body: bytes, now):
    """Deduplicate a delivery (Shopier-Webhook-Id is stable across retries) and extract the
    order id hint. Returns (event, order_id) for a new delivery, None for a duplicate/irrelevant one."""
    key = hashlib.sha256(((webhook_id or "") + "|" + (event or "") + "|").encode() +
                         (b"" if webhook_id else raw_body)).hexdigest()
    if db.execute("SELECT 1 FROM billing_webhook_events WHERE provider=? AND delivery_sha256=?",
                  (PROVIDER, key)).fetchone():
        return None
    try:
        payload = json.loads(raw_body)
    except ValueError:
        payload = None
    order_id = None
    if isinstance(payload, dict):
        candidate = payload.get("orderId") if event and event.startswith("refund.") else payload.get("id")
        if isinstance(candidate, (str, int)) and re.fullmatch(r"[0-9A-Za-z_-]{1,80}", str(candidate)):
            order_id = str(candidate)
    relevant = event in ("order.created", "refund.updated") and order_id is not None
    db.execute("INSERT INTO billing_webhook_events VALUES(?,?,?,?,?)",
               (PROVIDER, key, order_id, int(now.timestamp()), "received" if relevant else "ignored"))
    return (event, order_id) if relevant else None


def refund_from_order(db, *, order_id, fetch, now):
    """refund.updated hint -> re-fetch order; mark refunded only if Shopier reports a succeeded refund
    for an order this system verified. Idempotent."""
    known = db.execute("""SELECT ch.status FROM billing_provider_orders o JOIN billing_checkouts ch ON ch.id=o.checkout_id
                          WHERE o.provider=? AND o.provider_order_id=?""", (PROVIDER, order_id)).fetchone()
    if known is None or known["status"] == "refunded":
        return False
    order = fetch(order_id)
    refunds = order.get("refunds") if isinstance(order, dict) else None
    if not isinstance(refunds, list) or not any(isinstance(r, dict) and r.get("status") == "succeeded" for r in refunds):
        return False
    mark_refunded(db, order_id=order_id, actor="shopier_verifier", reason="Shopier iadesi (refund.updated, API ile doğrulandı)", now=now)
    return True


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
    sub = db.execute("SELECT company_id, product FROM billing_subscriptions WHERE id=?", (row["subscription_id"],)).fetchone()
    if sub["product"] == "hr_flow":
        # Stop new paid operations; customer data stays untouched.
        db.execute("UPDATE subscriptions SET status='Askıda' WHERE company_id=? AND package LIKE 'HR FLOW %'", (sub["company_id"],))
        db.execute("UPDATE ats_subscriptions SET status='Askıda' WHERE company_id=? AND package LIKE 'HR FLOW %'", (sub["company_id"],))
    link = db.execute("SELECT sales_collection_id FROM billing_checkout_sales WHERE checkout_id=?",
                      (row["checkout_id"],)).fetchone()
    if link and link["sales_collection_id"]:
        # Commission goes into refund review; CONTROL then posts the reversing entry (sales.reverse).
        db.execute("UPDATE sales_collections SET held=1 WHERE id=?", (link["sales_collection_id"],))
    _audit(db, actor, "payment_refunded", row["checkout_id"], reason, now)


BANK_PROVIDER = "bank_transfer"


def bank_settings(db):
    return db.execute("SELECT * FROM billing_bank_settings WHERE id=1").fetchone()


def bank_ready(db):
    """True once migration 007 exists. Probed inside a savepoint so a missing table never aborts
    the caller's transaction (PostgreSQL) or breaks the existing Shopier page."""
    db.execute("SAVEPOINT bank_probe")
    try:
        db.execute("SELECT 1 FROM billing_bank_settings WHERE id=1").fetchone()
        db.execute("SELECT 1 FROM billing_bank_checkout_details LIMIT 1").fetchone()
        ok = True
    except Exception:
        db.execute("ROLLBACK TO SAVEPOINT bank_probe")
        ok = False
    db.execute("RELEASE SAVEPOINT bank_probe")
    return ok


def save_bank_settings(db, *, actor, bank_name, account_holder, iban, enabled, now):
    from . import sales
    sales.admin(actor)
    iban = re.sub(r"\s+", "", str(iban)).upper()
    bank_name, account_holder = str(bank_name).strip(), str(account_holder).strip()
    if not re.fullmatch(r"TR[0-9]{24}", iban) or int(iban[4:] + "2927" + iban[2:4]) % 97 != 1:
        sales.fail("Geçerli Türkiye IBAN numarası girin.", 422)
    if not (1 <= len(bank_name) <= 120 and 1 <= len(account_holder) <= 180):
        sales.fail("Banka adı ve hesap sahibi zorunludur.", 422)
    db.execute("""INSERT INTO billing_bank_settings VALUES(1,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                  bank_name=excluded.bank_name, account_holder=excluded.account_holder, iban=excluded.iban,
                  enabled=excluded.enabled, updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
               (bank_name, account_holder, iban, bool(enabled), str(actor.id), int(now.timestamp())))
    _audit(db, str(actor.id), "bank_settings_updated", "bank", "enabled=" + str(bool(enabled)), now)


def start_bank_checkout(db, *, ref, rate, vat_rate, now):
    from .billing_quotes import issue_quote
    req, checkouts = request_view(db, ref)
    settings = bank_settings(db)
    if req["product"] != "hr_flow" or req["is_test"] or not settings or not settings["enabled"]:
        raise PaymentRejected("Banka ödemesi kullanılamıyor.")
    if any(ch["status"] in ("paid","refunded") for ch in checkouts):
        raise PaymentRejected("Bu ödeme bağlantısı daha önce tahsil edilmiş.")
    at = int(now.timestamp())
    for ch in checkouts:
        if ch["provider"] == BANK_PROVIDER and ch["status"] == "pending" and ch["expires_at"] > at:
            return ch["id"]
    quote_id = issue_quote(db, company_id=req["company_id"], product="hr_flow", plan_name=req["plan_name"],
                           period_start=_date(req["period_start"]), rate=rate, expected_rate_date=rate.published_on,
                           vat_rate=vat_rate, now=now)
    checkout_id = uuid4().hex
    db.execute("INSERT INTO billing_checkouts VALUES(?,?,?,?,?,?,?,?,?)",
               (checkout_id, quote_id, req["company_id"], BANK_PROVIDER, None, None,
                ref + "-" + checkout_id[:6].upper(), "pending", at))
    db.execute("INSERT INTO billing_checkout_requests VALUES(?,?)", (checkout_id, ref))
    db.execute("INSERT INTO billing_bank_checkout_details VALUES(?,?,?,?,?)",
               (checkout_id, settings["bank_name"], settings["account_holder"], settings["iban"], at))
    if req["proposal_id"]:
        db.execute("INSERT INTO billing_checkout_sales VALUES(?,?,?,?)",
                   (checkout_id, req["proposal_id"], req["owner_id"], None))
    _audit(db, "customer", "bank_checkout_started", checkout_id, "ref=" + ref, now)
    return checkout_id


def confirm_bank_payment(db, *, actor, checkout_id, transaction_id, gross_minor, now):
    from . import sales
    sales.admin(actor)
    if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,120}", transaction_id):
        raise PaymentRejected("Geçerli banka işlem numarası zorunludur.")
    checkout = db.execute("SELECT * FROM billing_checkouts WHERE id=? AND provider=?", (checkout_id, BANK_PROVIDER)).fetchone()
    if not checkout or _is_test_checkout(db, checkout_id):
        raise PaymentRejected("Banka ödeme kaydı bulunamadı.")
    previous = db.execute("SELECT checkout_id FROM billing_provider_orders WHERE provider=? AND provider_order_id=?",
                          (BANK_PROVIDER, transaction_id)).fetchone()
    if previous:
        if previous["checkout_id"] == checkout_id:
            return "already_processed"
        raise PaymentRejected("Banka işlemi başka ödeme için kullanılmış.")
    if checkout["status"] not in ("pending","expired"):
        raise PaymentRejected("Ödeme onay beklemiyor.")
    quote = db.execute("SELECT * FROM billing_quotes WHERE id=? AND company_id=?", (checkout["quote_id"], checkout["company_id"])).fetchone()
    if not quote or quote["product"] != "hr_flow" or quote["currency"] != "TRY" or int(quote["gross_minor"]) != gross_minor:
        raise PaymentRejected("Banka tahsilatı teklif tutarıyla eşleşmiyor.")
    digest = hashlib.sha256(f"{checkout_id}:{transaction_id}:{gross_minor}".encode()).hexdigest()
    return _activate_verified(db, checkout, quote, BANK_PROVIDER, transaction_id, gross_minor, digest, now, "founder:" + str(actor.id))


def bank_checkout(db, ref):
    """Customer-facing bank transfer instructions for the newest bank checkout of a request.

    Shows the immutable IBAN snapshot taken when the checkout was created, never the current
    settings row, so a later IBAN change cannot redirect an already issued transfer."""
    return db.execute("""SELECT ch.id, ch.payment_reference, ch.status, ch.created_at, q.gross_minor, q.net_minor,
                                q.vat_minor, d.bank_name, d.account_holder, d.iban
                         FROM billing_checkout_requests x JOIN billing_checkouts ch ON ch.id=x.checkout_id
                         JOIN billing_quotes q ON q.id=ch.quote_id
                         JOIN billing_bank_checkout_details d ON d.checkout_id=ch.id
                         WHERE x.request_ref=? AND ch.provider=? ORDER BY ch.created_at DESC, ch.id LIMIT 1""",
                      (ref, BANK_PROVIDER)).fetchone()


def pending_bank_payments(db):
    """Bank transfers awaiting founder confirmation in CONTROL (oldest first)."""
    return db.execute("""SELECT ch.id, ch.payment_reference, ch.status, ch.created_at, q.gross_minor, q.plan_name,
                                q.period_start, co.name AS company_name, r.ref, r.owner_id, d.iban
                         FROM billing_checkouts ch JOIN billing_quotes q ON q.id=ch.quote_id
                         JOIN companies co ON co.id=ch.company_id
                         JOIN billing_checkout_requests x ON x.checkout_id=ch.id
                         JOIN billing_payment_requests r ON r.ref=x.request_ref
                         JOIN billing_bank_checkout_details d ON d.checkout_id=ch.id
                         WHERE ch.provider=? AND ch.status IN ('pending','expired')
                           AND NOT EXISTS (SELECT 1 FROM billing_checkout_requests x2 JOIN billing_checkouts c2 ON c2.id=x2.checkout_id
                                           WHERE x2.request_ref=r.ref AND c2.status IN ('paid','refunded'))
                         ORDER BY ch.created_at, ch.id""", (BANK_PROVIDER,)).fetchall()
