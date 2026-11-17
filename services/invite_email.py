"""Send ERNASA interview invitations through a configured transactional-email API.

Never use a browser mailto: handler or accept a caller-supplied sender, recipient,
subject, message body, or URL. Credentials remain server-side.
"""
from datetime import datetime
from email.utils import parseaddr
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class EmailConfigurationError(RuntimeError):
    pass


class EmailDeliveryError(RuntimeError):
    pass


_ALLOWED_SENDERS = {"office@ernasa.com", "iletisim@ernasa.com"}


def configured_sender():
    key = os.getenv("ERNASA_RESEND_API_KEY", "").strip()
    sender = os.getenv("ERNASA_EMAIL_FROM", "office@ernasa.com").strip().lower()
    if not key or sender not in _ALLOWED_SENDERS:
        raise EmailConfigurationError("Kurumsal e-posta gönderimi henüz yapılandırılmadı.")
    return key, sender


def _recipient(value):
    if not isinstance(value, str) or len(value) > 254 or any(c in value for c in "\r\n"):
        raise ValueError("Adayın e-posta adresi geçersiz.")
    display, address = parseaddr(value.strip())
    if display or address != value.strip() or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", address):
        raise ValueError("Adayın e-posta adresi geçersiz.")
    return address


def invitation_message(candidate, url):
    recipient = _recipient(candidate["email"])
    expiry = datetime.fromisoformat(candidate["expires_at"])
    expiry_local = expiry.astimezone(__import__("zoneinfo").ZoneInfo("Europe/Istanbul"))
    deadline = expiry_local.strftime("%d.%m.%Y %H:%M")
    name = str(candidate.get("name") or "Aday").strip().replace("\r", " ").replace("\n", " ")[:160]
    company = str(candidate.get("company") or "ERNASA").strip().replace("\r", " ").replace("\n", " ")[:160]
    position = str(candidate.get("position") or "Mülakat").strip().replace("\r", " ").replace("\n", " ")[:160]
    subject = f"{company} · {position} mülakat daveti"
    body = (
        f"Merhaba {name},\n\n"
        f"{company} bünyesindeki {position} pozisyonu için mülakat bağlantınız:\n{url}\n\n"
        f"Bağlantının son geçerlilik zamanı: {deadline} (Türkiye saati).\n"
        "Mülakatı bu süre içinde başlatmanız gerekmektedir.\n\n"
        "İyi çalışmalar,\nERNASA Yazılım Sistemleri\n"
    )
    return recipient, subject, body


def send_invitation(candidate, url, *, idempotency_key):
    api_key, sender = configured_sender()
    recipient, subject, body = invitation_message(candidate, url)
    payload = json.dumps({
        "from": f"ERNASA Yazılım Sistemleri <{sender}>",
        "to": [recipient],
        "subject": subject,
        "text": body,
    }, ensure_ascii=False).encode("utf-8")
    request = Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read(8192))
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        # No API keys, provider responses or candidate details in error messages.
        raise EmailDeliveryError("Kurumsal e-posta servisi gönderimi kabul etmedi. Daha sonra yeniden deneyin.") from exc
    if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]:
        raise EmailDeliveryError("Kurumsal e-posta servisinden gönderim onayı alınamadı.")
    return result["id"]
