from decimal import Decimal

import pytest

from control.billing_catalog import EDUCATION, HR_FLOW, QuoteAmounts, quote_amounts


@pytest.mark.parametrize("plan, price, limits", [
    (HR_FLOW[0], "109.99", (100, 2, 1)),
    (HR_FLOW[1], "269.99", (500, 10, 3)),
    (HR_FLOW[2], "539.99", (1000, 20, 5)),
    (HR_FLOW[3], "1079.99", (2500, 50, 15)),
    (EDUCATION[0], "75", (10, None, None)),
    (EDUCATION[1], "150", (50, None, None)),
    (EDUCATION[2], "300", (100, None, None)),
    (EDUCATION[3], "500", (250, None, None)),
])
def test_approved_prices_and_limits(plan, price, limits):
    assert plan.monthly_usd == Decimal(price)
    assert (plan.capacity, plan.positions, plan.hr_users) == limits


def test_quote_rounds_net_then_vat_to_kurus():
    assert quote_amounts(HR_FLOW[0], Decimal("48.1234"), Decimal("0.20")) == (
        QuoteAmounts(529309, 105862, 635171)
    )


@pytest.mark.parametrize("rate", [Decimal("NaN"), Decimal("0"), None, 48.12])
def test_quote_rejects_missing_or_invalid_rates(rate):
    with pytest.raises(ValueError):
        quote_amounts(HR_FLOW[0], rate, Decimal("0.20"))
