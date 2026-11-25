from datetime import date
from decimal import Decimal

import pytest

from control.tcmb_rates import ExchangeRate, RateUnavailable, parse_usd_selling_rate


RATE_DATE = date(2026, 11, 25)
PAYLOAD = b"""<Tarih_Date Tarih="25.11.2026">
<Currency CurrencyCode="USD"><Unit>1</Unit><ForexBuying>48.1000</ForexBuying>
<ForexSelling>48.1234</ForexSelling></Currency></Tarih_Date>"""


def test_reads_usd_selling_rate_and_publication_date():
    assert parse_usd_selling_rate(PAYLOAD, RATE_DATE) == ExchangeRate(Decimal("48.1234"), RATE_DATE)


def test_rejects_stale_rate():
    with pytest.raises(RateUnavailable):
        parse_usd_selling_rate(PAYLOAD, date(2026, 11, 26))


@pytest.mark.parametrize("payload", [b"<html/>", PAYLOAD.replace(b"48.1234", b"NaN"),
                                     PAYLOAD.replace(b">1<", b">100<")])
def test_rejects_invalid_rate_responses(payload):
    with pytest.raises(RateUnavailable):
        parse_usd_selling_rate(payload, RATE_DATE)
