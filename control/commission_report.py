"""USD-based commission reporting for Sales Hub (representative) and CONTROL (founder).

Fixed TRY entitlements come only from stored rows (sales_commissions.amount_minor and the
sales_commission_fx snapshot). Nothing here recalculates an entitlement with a current rate; the
current TCMB rate is used only for clearly labelled *estimates* in the price list.
"""
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import time

from . import tcmb_rates
from .billing_catalog import HR_FLOW, commission_usd, fixed_try_minor

EXPLANATION = ("Komisyonlar Amerikan doları (USD) bazında hesaplanır. TL hak edişleri, müşterinin satış işleminde "
               "uygulanan TCMB döviz satış kuru üzerinden tahsilat doğrulandığında kesinleşir. Sonraki döviz kuru "
               "değişiklikleri kesinleşmiş hak ediş tutarını etkilemez. Komisyon ödemeleri takip eden ayın 1–15 "
               "tarihleri arasında Türk lirası olarak gerçekleştirilir.")
STATUS = {"pending": "Onay bekliyor", "approved": "Ödemeye hazır", "paid": "Ödendi", "reversed": "Ters kayıt"}
_CACHE = {}
CACHE_SECONDS = 15 * 60


def usd(value):
    if value is None:
        return "—"
    text = f"{Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"
    return "$" + text.replace(",", "X").replace(".", ",").replace("X", ".")


def fx(value):
    return "—" if value is None else f"{Decimal(str(value)):.4f}".replace(".", ",") + " TL"


def try_text(minor):
    if minor is None:
        return "—"
    minor = int(minor)
    return f"{minor // 100:,}".replace(",", ".") + f",{minor % 100:02d} TL"


def tr_date(value):
    if value in (None, ""):
        return "—"
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).astimezone(tcmb_zone()).strftime("%d.%m.%Y")
    return date.fromisoformat(str(value)[:10]).strftime("%d.%m.%Y")


def tcmb_zone():
    from zoneinfo import ZoneInfo
    return ZoneInfo("Europe/Istanbul")


def current_rate(today=None):
    """Latest published TCMB USD selling rate for display (weekends/holidays keep the last bulletin).
    Cached 15 minutes; None if it cannot be verified (never a fixed or manual fallback)."""
    today = today or datetime.now(tcmb_zone()).date()
    key = (today, id(tcmb_rates.fetch_latest_usd_selling_rate))
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        rate = tcmb_rates.fetch_latest_usd_selling_rate(today, max_age_days=10)
    except tcmb_rates.RateUnavailable:
        return None
    _CACHE.clear()
    _CACHE[key] = (time.time(), rate)
    return rate


def price_rows():
    """HR FLOW list prices and commission table in USD (central catalog)."""
    return [{"plan": p, "first": commission_usd(p.commission_usd, 25), "later": commission_usd(p.commission_usd, 5)}
            for p in HR_FLOW]


def _has(db, table):
    db.execute("SAVEPOINT report_probe")
    try:
        db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        ok = True
    except Exception:
        db.execute("ROLLBACK TO SAVEPOINT report_probe")
        ok = False
    db.execute("RELEASE SAVEPOINT report_probe")
    return ok


def rows(db, actor):
    """Commission entitlements: own (representative) or all (founder)."""
    full = _has(db, "sales_commission_fx") and _has(db, "billing_checkout_sales")
    extra = ("f.basis_usd, f.commission_usd, f.fx_rate, f.fx_date, q.plan_name, q.product, q.created_at AS sold_at"
             if full else "NULL AS basis_usd, NULL AS commission_usd, NULL AS fx_rate, NULL AS fx_date, "
                          "NULL AS plan_name, NULL AS product, NULL AS sold_at")
    joins = ("""LEFT JOIN sales_commission_fx f ON f.commission_id=m.id
                LEFT JOIN billing_checkout_sales cs ON cs.sales_collection_id=col.id
                LEFT JOIN billing_checkouts ch ON ch.id=cs.checkout_id
                LEFT JOIN billing_quotes q ON q.id=ch.quote_id""" if full else "")
    sql = f"""SELECT m.id, m.rate, m.status, m.amount_minor, m.paid_on, col.collected_on, col.held, col.net_minor,
                     p.owner_id, a.name AS rep_name, cu.name AS customer, o.title AS opportunity, {extra}
              FROM sales_commissions m JOIN sales_collections col ON col.id=m.collection_id
              JOIN sales_proposals p ON p.id=col.proposal_id JOIN sales_opportunities o ON o.id=p.opportunity_id
              JOIN sales_customers cu ON cu.id=o.customer_id
              LEFT JOIN sales_accounts a ON a.id=p.owner_id
              {joins}"""
    params = ()
    if not actor.founder:
        sql += " WHERE p.owner_id=? AND p.company_id=?"
        params = (actor.id, actor.company_id)
    from .sales import payment_window
    out = []
    for r in db.execute(sql + " ORDER BY col.collected_on DESC, m.id", params).fetchall():
        row = dict(r)
        start, end = payment_window(str(row["collected_on"])[:10])
        row["window"] = (start, end)
        row["window_text"] = f"{start:%d.%m.%Y} – {end:%d.%m.%Y}"
        row["held"] = bool(row["held"])
        row["status_text"] = "İade/iptal incelemesi" if row["held"] and row["status"] != "reversed" else STATUS[row["status"]]
        row["open"] = row["status"] in ("pending", "approved") and not row["held"]
        out.append(row)
    return out


def by_rep(items):
    reps = {}
    for r in items:
        k = r["owner_id"]
        s = reps.setdefault(k, {"rep": r["rep_name"] or k, "count": 0, "usd": Decimal("0"), "fixed": 0, "open": 0, "paid": 0})
        s["count"] += 1
        if r["commission_usd"] is not None and r["status"] != "reversed":
            s["usd"] += Decimal(str(r["commission_usd"]))
        if r["status"] != "reversed":
            s["fixed"] += int(r["amount_minor"])
        if r["open"]:
            s["open"] += int(r["amount_minor"])
        if r["status"] == "paid":
            s["paid"] += int(r["amount_minor"])
    return sorted(reps.values(), key=lambda s: s["rep"])


def by_window(items):
    windows = {}
    for r in items:
        if r["status"] == "reversed":
            continue
        s = windows.setdefault(r["window"], {"count": 0, "fixed": 0, "open": 0, "paid": 0})
        s["count"] += 1
        s["fixed"] += int(r["amount_minor"])
        s["open"] += int(r["amount_minor"]) if r["open"] else 0
        s["paid"] += int(r["amount_minor"]) if r["status"] == "paid" else 0
    return sorted(windows.items(), reverse=True)


def estimate_try(amount_usd, rate):
    """Estimate only (current rate); never stored, never an entitlement."""
    return fixed_try_minor(amount_usd, rate.value)
