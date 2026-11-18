"""Send ERNASA interview invitations through a configured transactional-email API.

Never use a browser mailto: handler or accept a caller-supplied sender, recipient,
subject, message body, or URL. Credentials remain server-side.
"""
from datetime import datetime
from html import escape as html_escape
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


def send_training_invitation(*, recipient, name, url, expires_at, idempotency_key):
    """Send a real HTML + plain text academy invite (never a mailto draft).

    The target email, full invitation URL and deadline come from a validated
    stored training invite, not from arbitrary client-controlled HTML.
    """
    api_key, sender = configured_sender()
    email_address = _recipient(recipient)
    if not isinstance(url, str) or not re.fullmatch(
        r"https://(?:www\.)?ernasa\.com/egitim/[A-Za-z0-9_-]{43}", url
    ):
        raise ValueError("Eğitim bağlantısı geçersiz.")
    deadline = datetime.fromisoformat(expires_at)
    from zoneinfo import ZoneInfo
    deadline_local = deadline.astimezone(ZoneInfo("Europe/Istanbul")).strftime("%d.%m.%Y %H:%M")
    name = str(name or "Katılımcı").strip().replace("\n", " ").replace("\r", " ")[:120]
    if not name:
        name = "Katılımcı"
    subject = "ERNASA · Kişiye Özel Eğitim Davetiniz"
    plain = (
        f"Merhaba {name},\n\n"
        "ERNASA eğitim programınıza erişmek için aşağıdaki bağlantıya tıklayın:\n"
        f"{url}\n\n"
        f"Bağlantı geçerlilik zamanı: {deadline_local} (Türkiye saati).\n\n"
        "Bu bağlantı kişiye özeldir. Lütfen başkalarıyla paylaşmayın.\n"
        "Eğitim hakkında sorularınız için İK yetkilinizle iletişime geçebilirsiniz.\n\n"
        "ERNASA Yazılım ve Teknoloji Sistemleri\n"
    )
    escaped_url = html_escape(url, quote=True)
    escaped_name = html_escape(name, quote=True)
    html = (
        '<!doctype html><html lang="tr"><body style="margin:0;padding:24px;'
        'font:15px/1.6 Arial,sans-serif;background:#f4f7fb;color:#18324b">'
        '<div style="max-width:580px;margin:auto;background:#fff;border:1px solid #dce5ef;'
        'border-radius:12px;padding:32px">'
        '<h2 style="margin:0 0 20px;color:#123b63">ERNASA Eğitim Daveti</h2>'
        f'<p>Merhaba {escaped_name},</p>'
        '<p>Size özel eğitim programına ulaşmak için aşağıdaki butona tıklayın.</p>'
        f'<p style="margin:30px 0"><a href="{escaped_url}" style="display:inline-block;'
        'background:#153d66;color:#fff;text-decoration:none;border-radius:8px;'
        'padding:13px 23px;font-weight:bold">Eğitime Başla</a></p>'
        '<p>Buton açılmazsa bu bağlantıyı tarayıcınıza yapıştırabilirsiniz:</p>'
        f'<p style="overflow-wrap:anywhere"><a href="{escaped_url}">{escaped_url}</a></p>'
        f'<p><strong>Son geçerlilik:</strong> {deadline_local} (Türkiye saati)</p>'
        '<p style="font-size:13px;color:#52687c">Bu bağlantı kişiye özeldir. '
        'Lütfen başkalarıyla paylaşmayın. Eğitimle ilgili sorularınız için '
        'İK yetkilinizle iletişime geçebilirsiniz.</p>'
        '<hr style="border:0;border-top:1px solid #e3eaf0;margin:22px 0">'
        '<small>ERNASA Yazılım ve Teknoloji Sistemleri</small></div></body></html>'
    )
    payload = json.dumps({
        "from": f"ERNASA Yazılım Sistemleri <{sender}>",
        "to": [email_address],
        "subject": subject,
        "text": plain,
        "html": html,
    }, ensure_ascii=False).encode("utf-8")
    request = Request(
        "https://api.resend.com/emails", data=payload,
        headers={"Authorization": "Bearer " + api_key,
                 "Content-Type": "application/json",
                 "Idempotency-Key": idempotency_key},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read(8192))
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise EmailDeliveryError("Eğitim daveti e-posta servisi tarafından kabul edilmedi.") from exc
    if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]:
        raise EmailDeliveryError("E-posta servisi gönderim onayı döndürmedi.")
    return result["id"]
