"""Read-only Shopier order lookup for ERNASA HR. No activation side effects."""
import json
import os
import re
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

class ShopierError(RuntimeError):
    pass

def fetch_order(order_id):
    if not isinstance(order_id, str) or not re.fullmatch(r"[0-9A-Za-z_-]{1,80}", order_id):
        raise ShopierError("Geçersiz sipariş numarası.")
    token = os.environ.get("SHOPIER_HR_API_TOKEN", "").strip()
    if not token:
        raise ShopierError("HR ödeme bağlantısı yapılandırılmamış.")
    req = Request("https://api.shopier.com/v1/orders/" + order_id,
                  headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    # No automatic financial actions: response requires independent matching.
    try:
        with urlopen(req, timeout=10) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ShopierError("Sipariş yanıtı çok büyük.")
        order = json.loads(raw)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise ShopierError("Shopier siparişi doğrulanamadı.") from None
    if not isinstance(order, dict) or str(order.get("id", "")) != order_id:
        raise ShopierError("Sipariş kimliği uyuşmuyor.")
    return order

def paid_try_order(order):
    """Necessary but not sufficient for activation; refunds/identity/amount still require matching."""
    return (isinstance(order, dict) and order.get("paymentStatus") == "paid"
            and order.get("currency") == "TRY" and order.get("refunds") == []
            and order.get("returns") == [])
