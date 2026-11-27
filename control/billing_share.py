"""Customer payment links in Sales Hub: create-or-reuse, share (copy / WhatsApp click-to-chat /
e-mail through the existing provider), delivery history and payment tracking.

Rules
- A representative acts only on links of their own CONTROL-approved proposals; the founder on all.
- Recipients come only from the company's registered contact in CONTROL (company_contacts);
  nobody types a free phone number or e-mail address, so a link cannot be sent to a stranger.
- One open link per proposal (or direct-sale company), product and period: a repeat click reuses
  it, a different package for the same period is refused, a paid period is never re-issued.
- Founder live-test links are never shared to customers.
- Prices come only from the approved catalog through the existing payment page; nothing here
  changes price, IBAN, commission or activation.
"""
from datetime import date
import re
from urllib.parse import quote
from uuid import uuid4

from . import billing_payments as bp

LABELS = {"hr_flow": "ERNASA HR FLOW", "education": "ERNASA Eğitim Platformu"}
EMAIL_COOLDOWN = 10 * 60  # seconds: protects against an accidental double send

# (code, label, existing status CSS class) - reuse the current status badge colours only.
STAGES = {
    "proposal_submitted": ("Teklif onay bekliyor", "submitted"),
    "proposal_approved": ("Teklif onaylandı", "approved"),
    "ready": ("Ödeme bağlantısı hazır", "draft"),
    "awaiting": ("Ödeme bekleniyor", "pending"),
    "bank_review": ("Banka tahsilatı doğrulama bekliyor", "pending"),
    "paid": ("Ödeme tamamlandı", "paid"),
    "active": ("Abonelik aktif", "approved"),
    "cancelled": ("İptal veya iade", "rejected"),
}


def _probe(db, sql):
    db.execute("SAVEPOINT share_probe")
    try:
        db.execute(sql).fetchone()
        ok = True
    except Exception:
        db.execute("ROLLBACK TO SAVEPOINT share_probe")
        ok = False
    db.execute("RELEASE SAVEPOINT share_probe")
    return ok


def ready(db):
    """Migration 008 present (probed in a savepoint so a missing table never aborts the caller)."""
    return _probe(db, "SELECT 1 FROM billing_link_events LIMIT 1")


def contact(db, company_id):
    """Primary registered contact of the CONTROL company, else the first one; None if none."""
    if not _probe(db, "SELECT 1 FROM company_contacts LIMIT 1"):
        return None
    row = db.execute("""SELECT name, phone, email FROM company_contacts WHERE company_id=?
                        ORDER BY primary_contact DESC, id LIMIT 1""", (company_id,)).fetchone()
    return dict(row) if row else None


def whatsapp_number(phone):
    """Turkish and international numbers -> wa.me digits (country code, no '+'); None if unusable."""
    digits = re.sub(r"\D", "", str(phone or ""))
    if str(phone or "").strip().startswith("+"):
        pass
    elif digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = "90" + digits[1:]
    elif len(digits) == 10 and digits.startswith("5"):
        digits = "90" + digits
    return digits if 11 <= len(digits) <= 15 else None


def mask_phone(phone):
    digits = re.sub(r"\D", "", str(phone or ""))
    return ("•" * max(len(digits) - 4, 0) + digits[-4:]) if digits else ""


def mask_email(email):
    local, _, domain = str(email or "").partition("@")
    return (local[:2] + "•••@" + domain) if domain else ""


def message(*, contact_name, product, plan_name, url, sender_name):
    return (f"Merhaba {contact_name},\n"
            f"{LABELS.get(product, 'ERNASA')} {plan_name} aboneliğinize ilişkin ödeme bağlantınız hazırlanmıştır.\n"
            "Aşağıdaki bağlantı üzerinden kredi/banka kartı veya Havale/EFT/FAST yöntemlerinden birini "
            "kullanarak ödemenizi gerçekleştirebilirsiniz.\n"
            f"{url}\n"
            "Ödemenizin doğrulanmasının ardından aboneliğiniz etkinleştirilecektir.\n"
            "Saygılarımızla,\n"
            f"{sender_name}\n"
            "ERNASA Yazılım")


def sender_name(db, actor):
    if actor.founder:
        row = db.execute("SELECT name FROM users WHERE id=?", (int(actor.id),)).fetchone()
    else:
        row = db.execute("SELECT name FROM sales_accounts WHERE id=?", (actor.id,)).fetchone()
    return (row["name"] if row else "") or "ERNASA Satış Ekibi"


def authorized_request(db, actor, ref):
    """The payment request if this actor may act on it; same 404 for 'missing' and 'not yours'."""
    from . import sales
    if not isinstance(ref, str) or not re.fullmatch(r"ERN[0-9A-F]{20}", ref):
        sales.fail("Ödeme bağlantısı bulunamadı.", 404)
    sql = """SELECT r.*, co.name AS company_name FROM billing_payment_requests r
             JOIN companies co ON co.id=r.company_id"""
    params = [ref]
    if actor.founder:
        sql += " WHERE r.ref=?"
    else:
        sql += """ JOIN sales_proposals p ON p.id=r.proposal_id
                   WHERE r.ref=? AND r.owner_id=? AND p.owner_id=? AND p.company_id=? AND r.is_test=FALSE"""
        params += [actor.id, actor.id, actor.company_id]
    row = db.execute(sql, params).fetchone()
    if row is None:
        sales.fail("Ödeme bağlantısı bulunamadı.", 404)
    return row


def _paid(db, ref):
    return db.execute("""SELECT 1 FROM billing_checkout_requests x JOIN billing_checkouts c ON c.id=x.checkout_id
                         WHERE x.request_ref=? AND c.status IN ('paid','refunded')""", (ref,)).fetchone() is not None


def ensure_link(db, actor, *, product, plan_name, period_start, now, proposal_id=None, company_id=None):
    """Create the payment link, or return the existing open one for the same proposal/company,
    product and period. Returns (ref, reused)."""
    from . import sales
    # Authorisation first: never reveal (or reuse) a link before ownership is proven.
    if proposal_id:
        p = sales.owned(db, actor, "proposals", proposal_id)
        if p["status"] != "approved":
            sales.fail("Ödeme bağlantısı yalnız CONTROL onaylı teklif için oluşturulabilir.")
        proposal_id = p["id"]
    else:
        sales.admin(actor)
    if hasattr(db, "raw") and proposal_id:
        db.execute("SELECT id FROM sales_proposals WHERE id=? FOR UPDATE", (proposal_id,))  # serialise double clicks
    if proposal_id:
        scope, value = "r.proposal_id=?", proposal_id
    else:
        scope, value = "r.proposal_id IS NULL AND r.company_id=?", company_id
    from .billing_quotes import TEST_PLAN
    if plan_name != TEST_PLAN.name:
        existing = db.execute(f"""SELECT r.ref, r.plan_name FROM billing_payment_requests r
                                  WHERE {scope} AND r.product=? AND r.period_start=? AND r.is_test=FALSE
                                  ORDER BY r.created_at DESC""",
                              (value, product, period_start.isoformat())).fetchall()
        for row in existing:
            if _paid(db, row["ref"]):
                sales.fail("Bu teklif ve dönem için ödeme zaten alınmış; yeni bağlantı oluşturulamaz.")
        for row in existing:
            if row["plan_name"] == plan_name:
                return row["ref"], True
        if existing:
            sales.fail(f"Bu dönem için açık bir ödeme bağlantısı var ({existing[0]['plan_name']}). "
                       "Aynı dönem için ikinci bağlantı oluşturulamaz; değişiklik için CONTROL ile görüşün.")
    ref = bp.create_payment_request(db, actor=actor, proposal_id=proposal_id, company_id=company_id,
                                    product=product, plan_name=plan_name, period_start=period_start, now=now)
    return ref, False


def stage(db, ref):
    """Current payment stage of one link plus commission data for tracking."""
    req, checkouts = bp.request_view(db, ref)
    at = None
    paid = next((c for c in checkouts if c["status"] in ("paid", "refunded")), None)
    sub = db.execute("SELECT status FROM billing_subscriptions WHERE company_id=? AND product=?",
                     (req["company_id"], req["product"])).fetchone()
    sub = sub["status"] if sub else None
    info = {"commission_minor": None, "commission_status": None, "commission_window": None, "held": False,
            "gross_minor": paid["gross_minor"] if paid else None, "subscription": sub}
    if paid:
        col = db.execute("""SELECT col.collected_on, col.held, m.amount_minor, m.status FROM billing_checkout_sales cs
                            JOIN sales_collections col ON col.id=cs.sales_collection_id
                            LEFT JOIN sales_commissions m ON m.collection_id=col.id
                            WHERE cs.checkout_id=?""", (paid["id"],)).fetchone()
        if col:
            from . import sales
            start, end = sales.payment_window(col["collected_on"])
            info.update(commission_minor=col["amount_minor"], commission_status=col["status"],
                        commission_window=f"{start.strftime('%d.%m.%Y')} – {end.strftime('%d.%m.%Y')}",
                        held=bool(col["held"]))
        if paid["status"] == "refunded" or info["held"] or sub in ("cancelled", "refunded"):
            return "cancelled", info
        return ("active" if sub == "active" else "paid"), info
    import time
    at = int(time.time())
    if any(c["provider"] == bp.BANK_PROVIDER and c["status"] in ("pending", "expired") for c in checkouts):
        return "bank_review", info
    if any(c["provider"] == bp.PROVIDER and c["status"] == "pending" and c["expires_at"] > at for c in checkouts):
        return "awaiting", info
    return "ready", info


def events(db, ref, limit=None):
    sql = "SELECT * FROM billing_link_events WHERE ref=? ORDER BY created_at DESC, id"
    rows = db.execute(sql + (f" LIMIT {int(limit)}" if limit else ""), (ref,)).fetchall()
    return [dict(r) for r in rows]


def _log(db, *, ref, channel, status, actor, recipient, now, message_id=None, error=None):
    db.execute("INSERT INTO billing_link_events VALUES(?,?,?,?,?,?,?,?,?)",
               (uuid4().hex, ref, channel, status, str(actor.id), recipient, message_id,
                (error or None) and str(error)[:300], int(now.timestamp())))


def whatsapp_url(db, actor, ref, now):
    """wa.me click-to-chat to the registered contact with the prepared text. Nothing is sent by
    ERNASA; the representative presses send in WhatsApp. The open is logged."""
    from . import sales
    from .sales_http import c_public_url
    req = authorized_request(db, actor, ref)
    if req["is_test"]:
        sales.fail("Test bağlantıları müşteriye gönderilmez.")
    if _paid(db, ref):
        sales.fail("Bu bağlantının ödemesi tamamlandı; yeniden gönderilmez.")
    person = contact(db, req["company_id"])
    number = whatsapp_number(person and person["phone"])
    if not number:
        sales.fail("Firmanın kayıtlı yetkili telefon numarası yok veya geçersiz; CONTROL firma kaydını güncelleyin.", 422)
    text = message(contact_name=person["name"], product=req["product"], plan_name=req["plan_name"],
                   url=c_public_url(ref), sender_name=sender_name(db, actor))
    _log(db, ref=ref, channel="whatsapp", status="opened", actor=actor, recipient=mask_phone(person["phone"]), now=now)
    return f"https://wa.me/{number}?text={quote(text, safe='')}"


def send_email(db, actor, ref, now, send=None):
    """Send the prepared message to the registered contact through the configured provider
    (Brevo in production). Result or error is recorded; nothing is retried automatically."""
    from . import sales
    from .sales_http import c_public_url
    req = authorized_request(db, actor, ref)
    if req["is_test"]:
        sales.fail("Test bağlantıları müşteriye gönderilmez.")
    if _paid(db, ref):
        sales.fail("Bu bağlantının ödemesi tamamlandı; yeniden gönderilmez.")
    person = contact(db, req["company_id"])
    if not person or not person["email"]:
        sales.fail("Firmanın kayıtlı yetkili e-posta adresi yok; CONTROL firma kaydını güncelleyin.", 422)
    last = db.execute("""SELECT created_at FROM billing_link_events WHERE ref=? AND channel='email' AND status='sent'
                         ORDER BY created_at DESC LIMIT 1""", (ref,)).fetchone()
    if last and int(now.timestamp()) - int(last["created_at"]) < EMAIL_COOLDOWN:
        sales.fail("Bu bağlantı az önce e-postayla gönderildi. Mükerrer gönderimi önlemek için 10 dakika bekleyin.")
    if send is None:
        from services import invite_email
        send = invite_email.send_notice
    subject = f"{LABELS.get(req['product'], 'ERNASA')} {req['plan_name']} ödeme bağlantınız"
    body = message(contact_name=person["name"], product=req["product"], plan_name=req["plan_name"],
                   url=c_public_url(ref), sender_name=sender_name(db, actor))
    recipient = mask_email(person["email"])
    try:
        message_id = send(person["email"], subject, body, idempotency_key=f"payment-link-{ref}-{int(now.timestamp())}")
    except Exception as exc:  # provider/configuration error: record it, show it, never retry silently
        detail = str(exc) if exc.__class__.__name__ in ("EmailDeliveryError", "EmailConfigurationError", "ValueError") \
            else "E-posta servisine ulaşılamadı."
        _log(db, ref=ref, channel="email", status="failed", actor=actor, recipient=recipient, now=now, error=detail)
        return {"detail": "E-posta gönderilemedi: " + detail, "status": "failed"}
    _log(db, ref=ref, channel="email", status="sent", actor=actor, recipient=recipient, now=now,
         message_id=str(message_id)[:200])
    return {"detail": f"Ödeme bağlantısı {recipient} adresine gönderildi.", "status": "sent"}


def tracking(db, actor):
    """Rows for the 'Teklif ve ödeme takibi' card: own proposals (rep) or all (founder) awaiting
    approval / approved without link, plus every non-test link with stage and delivery state."""
    params = []
    where = "p.status IN ('submitted','approved')"
    if not actor.founder:
        where += " AND p.owner_id=? AND p.company_id=?"
        params += [actor.id, actor.company_id]
    proposals = [dict(r) for r in db.execute(f"""
        SELECT p.id, p.status, p.brief, p.version, p.owner_id, cu.name AS customer, x.company_id AS control_company
        FROM sales_proposals p JOIN sales_opportunities o ON o.id=p.opportunity_id
        JOIN sales_customers cu ON cu.id=o.customer_id
        LEFT JOIN sales_customer_companies x ON x.customer_id=cu.id
        WHERE {where} AND o.kind<>'project' ORDER BY p.id""", params).fetchall()]
    lsql = """SELECT r.ref, r.proposal_id, r.product, r.plan_name, r.period_start, r.owner_id, r.company_id,
                     r.created_at, co.name AS company_name, cu.name AS customer
              FROM billing_payment_requests r JOIN companies co ON co.id=r.company_id
              LEFT JOIN sales_proposals p ON p.id=r.proposal_id
              LEFT JOIN sales_opportunities o ON o.id=p.opportunity_id
              LEFT JOIN sales_customers cu ON cu.id=o.customer_id
              WHERE r.is_test=FALSE"""
    lparams = []
    if not actor.founder:
        lsql += " AND r.owner_id=? AND p.owner_id=? AND p.company_id=?"
        lparams = [actor.id, actor.id, actor.company_id]
    links = []
    have_events = ready(db)
    for r in db.execute(lsql + " ORDER BY r.created_at DESC", lparams).fetchall():
        row = dict(r)
        row["stage"], row["info"] = stage(db, row["ref"])
        row["contact"] = contact(db, row["company_id"])
        row["events"] = events(db, row["ref"], 3) if have_events else []
        links.append(row)
    linked = {l["proposal_id"] for l in links if l["proposal_id"]}
    for p in proposals:
        p["stage"] = "proposal_submitted" if p["status"] == "submitted" else "proposal_approved"
        p["has_link"] = p["id"] in linked
    return proposals, links


def period_end(start):
    from calendar import monthrange
    start = start if isinstance(start, date) else date.fromisoformat(str(start))
    year = start.year + (start.month == 12)
    month = start.month % 12 + 1
    return date(year, month, min(start.day, monthrange(year, month)[1]))
