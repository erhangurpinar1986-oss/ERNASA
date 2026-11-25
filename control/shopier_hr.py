"""Shopier REST API v1 client for ERNASA HR FLOW / Education (PAT: SHOPIER_HR_API_TOKEN).

Endpoints and fields follow the official reference at developer.shopier.com
(Order model, Product model, POST /products, GET /orders/{id}, webhooks), read 2026-11-25.
The ERNASA CV integration uses its own credentials and is never touched here.
The token is read from the environment per call and never logged or returned.
"""
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "https://api.shopier.com/v1/"
MAX_BYTES = 1024 * 1024


class ShopierError(RuntimeError):
    pass


def _token():
    token = os.environ.get("SHOPIER_HR_API_TOKEN", "").strip()
    if not token:
        raise ShopierError("HR ödeme bağlantısı yapılandırılmamış.")
    return token


def _call(method, path, body=None, timeout=4):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": "Bearer " + _token(), "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        raise ShopierError(f"Shopier isteği reddedildi (HTTP {exc.code}).") from None
    except (URLError, TimeoutError, OSError):
        raise ShopierError("Shopier'e ulaşılamadı.") from None
    if len(raw) > MAX_BYTES:
        raise ShopierError("Shopier yanıtı çok büyük.")
    try:
        return json.loads(raw)
    except ValueError:
        raise ShopierError("Shopier yanıtı okunamadı.") from None


def fetch_order(order_id):
    if not isinstance(order_id, str) or not re.fullmatch(r"[0-9A-Za-z_-]{1,80}", order_id):
        raise ShopierError("Geçersiz sipariş numarası.")
    order = _call("GET", "orders/" + order_id)
    if not isinstance(order, dict) or str(order.get("id", "")) != order_id:
        raise ShopierError("Sipariş kimliği uyuşmuyor.")
    return order


def create_product(*, title, description, gross_minor, image_url):
    """Create a single-use digital product priced at the quote's VAT-inclusive TRY total.

    stockQuantity=1 makes the link purchasable once. Returns (product_id, product_url).
    """
    if not isinstance(gross_minor, int) or gross_minor <= 0:
        raise ShopierError("Geçersiz tutar.")
    body = {
        "title": title[:120],
        "description": description[:1000],
        "type": "digital",
        "media": [{"type": "image", "url": image_url, "placement": 1}],
        "priceData": {"currency": "TRY", "price": f"{gross_minor // 100}.{gross_minor % 100:02d}",
                      "vatPercent": "20"},
        "stockQuantity": 1,
        "shippingPayer": "sellerPays",
    }
    product = _call("POST", "products", body, timeout=8)
    if not isinstance(product, dict) or not isinstance(product.get("id"), str) or not isinstance(product.get("url"), str):
        raise ShopierError("Shopier ürün yanıtı beklenen alanları içermiyor.")
    price = (product.get("priceData") or {}).get("price")
    if price is not None and price.replace(",", ".") not in (body["priceData"]["price"],
                                                              body["priceData"]["price"].rstrip("0").rstrip(".")):
        raise ShopierError("Shopier ürün fiyatı istenen tutarla eşleşmiyor.")
    if not product["url"].startswith("https://www.shopier.com/") and not product["url"].startswith("https://shopier.com/"):
        raise ShopierError("Shopier ürün bağlantısı beklenmeyen alan adında.")
    return product["id"], product["url"]


def paid_try_order(order):
    """Necessary but not sufficient for activation; see billing_payments.read_order."""
    return (isinstance(order, dict) and order.get("paymentStatus") == "paid"
            and order.get("currency") == "TRY" and order.get("refunds") == []
            and order.get("returns") == [])


WEBHOOK_EVENTS = ("order.created", "refund.updated")


def probe():
    """Read-only connectivity/permission check: GET /products?limit=1. Returns nothing sensitive."""
    products = _call("GET", "products?limit=1")
    if not isinstance(products, list):
        raise ShopierError("Shopier ürün listesi beklenen biçimde değil.")
    return "ok"


def ensure_webhooks(url):
    """Subscribe order.created and refund.updated to `url` if not already (GET/POST /webhooks).

    Only adds subscriptions for this exact URL; never deletes or edits others (e.g. ERNASA CV's).
    The signing token is returned once by Shopier; it is not stored or logged here because every
    notification is re-verified through the API anyway. Returns the list of events created."""
    existing = _call("GET", "webhooks?limit=50")
    if not isinstance(existing, list):
        raise ShopierError("Shopier webhook listesi beklenen biçimde değil.")
    have = {(w.get("event"), w.get("url")) for w in existing if isinstance(w, dict)}
    created = []
    for event in WEBHOOK_EVENTS:
        if (event, url) not in have:
            _call("POST", "webhooks", {"event": event, "url": url})
            created.append(event)
    return created


def delete_product(product_id):
    if not isinstance(product_id, str) or not re.fullmatch(r"[0-9A-Za-z_-]{1,80}", product_id):
        raise ShopierError("Geçersiz ürün numarası.")
    _call_no_body("DELETE", "products/" + product_id)


def _call_no_body(method, path, timeout=4):
    req = Request(BASE + path, method=method,
                  headers={"Authorization": "Bearer " + _token(), "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as response:
            response.read(MAX_BYTES)
    except HTTPError as exc:
        raise ShopierError(f"Shopier isteği reddedildi (HTTP {exc.code}).") from None
    except (URLError, TimeoutError, OSError):
        raise ShopierError("Shopier'e ulaşılamadı.") from None
