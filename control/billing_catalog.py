"""Approved monthly list prices and deterministic TRY quote calculation."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


@dataclass(frozen=True)
class Plan:
    name: str
    monthly_usd: Decimal
    capacity: int
    positions: int | None = None
    hr_users: int | None = None
    commission_usd: Decimal | None = None


HR_FLOW = (
    Plan("Starter", Decimal("109.99"), 100, 2, 1, Decimal("99.99")),
    Plan("Growth", Decimal("269.99"), 500, 10, 3, Decimal("249.99")),
    Plan("Business", Decimal("539.99"), 1000, 20, 5, Decimal("499.99")),
    Plan("PRO", Decimal("1079.99"), 2500, 50, 15, Decimal("999.99")),
)
EDUCATION = (
    Plan("Starter", Decimal("75"), 10),
    Plan("Growth", Decimal("150"), 50),
    Plan("Business", Decimal("300"), 100),
    Plan("PRO", Decimal("500"), 250),
)


@dataclass(frozen=True)
class QuoteAmounts:
    net_minor: int
    vat_minor: int
    gross_minor: int


def quote_amounts(plan: Plan, usd_try: Decimal, vat_rate: Decimal) -> QuoteAmounts:
    """Convert an approved price using the caller's verified rate and VAT fraction."""
    if not isinstance(usd_try, Decimal) or not usd_try.is_finite() or usd_try <= 0:
        raise ValueError("Geçerli USD/TRY kuru gerekiyor.")
    if not isinstance(vat_rate, Decimal) or not vat_rate.is_finite() or not 0 <= vat_rate <= 1:
        raise ValueError("Geçerli KDV oranı gerekiyor.")
    net = int((plan.monthly_usd * usd_try * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if net < 1:
        raise ValueError("Teklif tutarı en az bir kuruş olmalı.")
    vat = int((net * vat_rate).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return QuoteAmounts(net, vat, net + vat)


def commission_basis_for_quote(quote):
    """Preserve historical quote pricing; exclude the approved payment allowance on new HR quotes."""
    net = int(quote["net_minor"])
    if quote["product"] != "hr_flow":
        return net
    plan = next((p for p in HR_FLOW if p.name == quote["plan_name"]), None)
    if plan is None or Decimal(str(quote["monthly_usd"])) != plan.monthly_usd:
        return net
    basis = (plan.commission_usd * Decimal(str(quote["exchange_rate"])) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return min(net, int(basis))


CENT = Decimal("0.01")


def commission_usd(basis_usd: Decimal, rate: int) -> Decimal:
    """USD commission, rounded to cents first (ROUND_HALF_UP)."""
    return (Decimal(basis_usd) * Decimal(rate) / Decimal(100)).quantize(CENT, rounding=ROUND_HALF_UP)


def fixed_try_minor(amount_usd: Decimal, fx_rate: Decimal) -> int:
    """TRY entitlement in kuruş at the sale's TCMB rate: USD (2 dp) x rate, ROUND_HALF_UP to kuruş."""
    return int((Decimal(amount_usd) * Decimal(fx_rate) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def commission_basis_usd(product, plan_name, monthly_usd):
    """Approved USD commission basis from the central catalog; None if the quote does not match
    a current catalog price (historical quotes keep their TRY rules)."""
    plans = HR_FLOW if product == "hr_flow" else EDUCATION if product == "education" else ()
    plan = next((p for p in plans if p.name == plan_name), None)
    if plan is None or Decimal(str(monthly_usd)) != plan.monthly_usd:
        return None
    return plan.commission_usd if product == "hr_flow" else plan.monthly_usd


def commission_fx_for_quote(quote):
    """USD basis + the TCMB rate fixed in the sale's quote (never a later rate)."""
    basis = commission_basis_usd(quote["product"], quote["plan_name"], quote["monthly_usd"])
    if basis is None:
        return None
    return {"basis_usd": basis, "fx_rate": Decimal(str(quote["exchange_rate"])),
            "fx_date": str(quote["rate_date"])[:10], "fx_source": quote["rate_source"],
            "rate_type": quote["rate_type"]}
