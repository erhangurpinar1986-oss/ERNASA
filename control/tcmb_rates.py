"""Fetch the TCMB indicative USD selling rate for a required publication date."""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.error import URLError
from urllib.request import urlopen
from xml.etree import ElementTree


SOURCE = "https://www.tcmb.gov.tr/kurlar/today.xml"
RATE_TYPE = "ForexSelling"
MAX_BYTES = 64 * 1024


class RateUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ExchangeRate:
    value: Decimal
    published_on: date
    source: str = SOURCE
    rate_type: str = RATE_TYPE


def parse_usd_selling_rate(payload: bytes, expected_on: date) -> ExchangeRate:
    if len(payload) > MAX_BYTES or b"<!DOCTYPE" in payload:
        raise RateUnavailable("TCMB kur yanıtı geçersiz.")
    try:
        root = ElementTree.fromstring(payload)
        published_on = datetime.strptime(root.attrib["Tarih"], "%d.%m.%Y").date()
        usd = root.findall("./Currency[@CurrencyCode='USD']")
        if root.tag != "Tarih_Date" or len(usd) != 1 or usd[0].findtext("Unit") != "1":
            raise ValueError
        rate = Decimal(usd[0].findtext(RATE_TYPE, ""))
    except (ElementTree.ParseError, KeyError, ValueError, InvalidOperation):
        raise RateUnavailable("TCMB kur yanıtı geçersiz.") from None
    if published_on != expected_on or not rate.is_finite() or rate <= 0:
        raise RateUnavailable("Beklenen tarihe ait TCMB döviz satış kuru bulunamadı.")
    return ExchangeRate(rate, published_on)


def fetch_usd_selling_rate(expected_on: date) -> ExchangeRate:
    try:
        with urlopen(SOURCE, timeout=5) as response:
            payload = response.read(MAX_BYTES + 1)
    except (URLError, TimeoutError, OSError):
        raise RateUnavailable("TCMB kur kaynağına erişilemedi.") from None
    return parse_usd_selling_rate(payload, expected_on)


def fetch_latest_usd_selling_rate(today: date, max_age_days: int = 4) -> ExchangeRate:
    """Latest published TCMB indicative rate (weekends/holidays keep the previous bulletin).

    Refuses bulletins older than max_age_days instead of silently using a stale rate."""
    try:
        with urlopen(SOURCE, timeout=5) as response:
            payload = response.read(MAX_BYTES + 1)
    except (URLError, TimeoutError, OSError):
        raise RateUnavailable("TCMB kur kaynağına erişilemedi.") from None
    if len(payload) > MAX_BYTES or b"<!DOCTYPE" in payload:
        raise RateUnavailable("TCMB kur yanıtı geçersiz.")
    try:
        published = datetime.strptime(ElementTree.fromstring(payload).attrib["Tarih"], "%d.%m.%Y").date()
    except (ElementTree.ParseError, KeyError, ValueError):
        raise RateUnavailable("TCMB kur yanıtı geçersiz.") from None
    if not 0 <= (today - published).days <= max_age_days:
        raise RateUnavailable("Güncel TCMB kuru bulunamadı; ödeme başlatılamaz.")
    return parse_usd_selling_rate(payload, published)
