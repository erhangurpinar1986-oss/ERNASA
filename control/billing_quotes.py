"""Immutable checkout quotes for existing CONTROL companies."""
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from .billing_catalog import EDUCATION, HR_FLOW, quote_amounts
from .tcmb_rates import ExchangeRate, RATE_TYPE, SOURCE


PLANS = {"hr_flow": HR_FLOW, "education": EDUCATION}
QUOTE_LIFETIME = timedelta(minutes=15)


def issue_quote(db, *, company_id: int, product: str, plan_name: str,
                period_start: date, rate: ExchangeRate, expected_rate_date: date,
                vat_rate: Decimal, now: datetime) -> str:
    """Persist a server-priced quote. The caller owns the transaction and tenant authorization."""
    if now.utcoffset() is None:
        raise ValueError("Saat dilimi bilgisi gerekiyor.")
    today = now.astimezone(ZoneInfo("Europe/Istanbul")).date()
    if rate.published_on != expected_rate_date or expected_rate_date > today:
        raise ValueError("Beklenen yayın tarihine ait kur gerekiyor.")
    if rate.source != SOURCE or rate.rate_type != RATE_TYPE:
        raise ValueError("TCMB döviz satış kuru gerekiyor.")
    plan = next((plan for plan in PLANS.get(product, ()) if plan.name == plan_name), None)
    if plan is None:
        raise ValueError("Paket bulunamadı.")

    amounts = quote_amounts(plan, rate.value, vat_rate)
    # Reject inputs the database would round differently from the amount calculation.
    if rate.value >= 10**12 or rate.value != rate.value.quantize(Decimal("0.00000001")):
        raise ValueError("Kur hassasiyeti desteklenmiyor.")
    if vat_rate != vat_rate.quantize(Decimal("0.00000001")):
        raise ValueError("KDV hassasiyeti desteklenmiyor.")

    year = period_start.year + (period_start.month == 12)
    month = period_start.month % 12 + 1
    period_end = date(year, month, min(period_start.day, monthrange(year, month)[1]))
    quote_id = uuid4().hex
    db.execute(
        """INSERT INTO billing_quotes (
               id, company_id, product, plan_name, period_start, period_end,
               monthly_usd, capacity, positions, hr_users, exchange_rate, rate_date,
               rate_source, rate_type, vat_rate, net_minor, vat_minor, gross_minor,
               created_at, expires_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (quote_id, company_id, product, plan.name, period_start.isoformat(), period_end.isoformat(),
         str(plan.monthly_usd), plan.capacity, plan.positions, plan.hr_users, str(rate.value),
         rate.published_on.isoformat(), rate.source, rate.rate_type, str(vat_rate),
         amounts.net_minor, amounts.vat_minor, amounts.gross_minor,
         int(now.timestamp()), int((now + QUOTE_LIFETIME).timestamp())),
    )
    return quote_id


def active_quote(db, *, company_id: int, quote_id: str, now: datetime):
    """Return a checkout quote only to its company and within its validity window."""
    if now.utcoffset() is None:
        raise ValueError("Saat dilimi bilgisi gerekiyor.")
    at = int(now.timestamp())
    return db.execute(
        """SELECT * FROM billing_quotes
           WHERE id=? AND company_id=? AND created_at<=? AND expires_at>?""",
        (quote_id, company_id, at, at),
    ).fetchone()
