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


def _safe_provider_error(error: HTTPError) -> str:
    """Map Resend HTTP rejection to actionable, non-sensitive diagnostics.

    Never disclose provider response bodies, recipients, invitation tokens,
    API keys or other untrusted fields. Only whitelist known categories.
    """
    status = int(getattr(error, "code", 0) or 0)
    try:
        details = json.loads(error.read(2048))
        if not isinstance(details, dict):
            details = {}
    except (ValueError, OSError, UnicodeError):
        details = {}
    message = str(details.get("message") or "").lower()
    name = str(details.get("name") or "").lower()
    if status in (401,):
        meaning = "API anahtarı geçersiz veya erişim izni yetersiz. Resend API Keys bölümünü kontrol edin."
    elif status == 403:
        if name == "invalid_api_key":
            meaning = "Resend API anahtarı geçersiz görünüyor. Render ortam değişkeninin yeni anahtarın tam değeri olduğunu kontrol edin."
        elif "testing emails" in message or "test email" in message or "your own email" in message:
            meaning = "Hesap yalnızca test alıcısına gönderime izin veriyor. Resend üzerinde ernasa.com alan adını doğrulayın."
        elif "domain" in message or "verified" in message or "sender" in message:
            meaning = "Gönderici alan adı doğrulanmamış veya gönderim için yetkili değil. Resend Domains bölümünü kontrol edin."
        elif "restricted" in name or "permission" in message or "access" in message:
            meaning = "API anahtarının gönderim yetkisi bulunmuyor. Resend API Keys izinlerini kontrol edin."
        else:
            meaning = "Resend gönderim yetkisini reddetti. Domains ve API Keys ayarlarını kontrol edin."
    elif status in (400, 422):
        if "from" in message or "sender" in message:
            meaning = "Gönderen adresi reddedildi. iletisim@ernasa.com adresini ve doğrulanmış alan adını kontrol edin."
        elif "to" in message or "recipient" in message:
            meaning = "Alıcı adresi reddedildi. Kayıtlı katılımcı e-postasını kontrol edin."
        elif "html" in message or "text" in message or "subject" in message:
            meaning = "E-posta içeriği doğrulamadan geçemedi. ERNASA yazılım ekibi kontrol etmelidir."
        else:
            meaning = "E-posta isteği doğrulanamadı. Gönderici ve alıcı ayarları kontrol edilmeli."
    elif status == 429:
        meaning = "Resend gönderim sınırına ulaşıldı. Hesap limitlerini kontrol edin."
    elif 500 <= status < 600:
        meaning = "Resend servisinde geçici sunucu hatası var."
    else:
        meaning = "E-posta sağlayıcısı gönderimi reddetti. Resend işlem kayıtlarını kontrol edin."
    return f"Resend HTTP {status}: {meaning}"


_ALLOWED_SENDERS = {"iletisim@ernasa.com"}


def _email_provider():
    """Use exactly one explicit provider; never retry automatically via another."""
    provider = os.getenv("ERNASA_EMAIL_PROVIDER", "resend").strip().lower()
    if provider not in {"resend", "brevo"}:
        raise EmailConfigurationError("Kurumsal e-posta sağlayıcısı desteklenmiyor.")
    return provider


def configured_sender():
    provider = _email_provider()
    key_name = "ERNASA_BREVO_API_KEY" if provider == "brevo" else "ERNASA_RESEND_API_KEY"
    key = os.getenv(key_name, "").strip()
    sender = os.getenv("ERNASA_EMAIL_FROM", "iletisim@ernasa.com").strip().lower()
    if not key or sender not in _ALLOWED_SENDERS:
        raise EmailConfigurationError("Kurumsal e-posta gönderimi henüz yapılandırılmadı.")
    return key, sender


def validate_recipient(value):
    if not isinstance(value, str) or len(value) > 254 or any(c in value for c in "\r\n"):
        raise ValueError("Adayın e-posta adresi geçersiz.")
    display, address = parseaddr(value.strip())
    if display or address != value.strip() or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", address):
        raise ValueError("Adayın e-posta adresi geçersiz.")
    return address


def invitation_message(candidate, url):
    recipient = validate_recipient(candidate["email"])
    expiry = datetime.fromisoformat(candidate["expires_at"])
    expiry_local = expiry.astimezone(__import__("zoneinfo").ZoneInfo("Europe/Istanbul"))
    deadline = expiry_local.strftime("%d.%m.%Y %H:%M")
    name = str(candidate.get("name") or "Aday").strip().replace("\r", " ").replace("\n", " ")[:160]
    company = str(candidate.get("company") or "ERNASA").strip().replace("\r", " ").replace("\n", " ")[:160]
    position = str(candidate.get("position") or "Mülakat").strip().replace("\r", " ").replace("\n", " ")[:160]
    subject = f"{company} · {position} mülakat daveti"
    scheduled = candidate.get("scheduled_at")
    appointment = ""
    if scheduled:
        local = datetime.fromisoformat(scheduled).astimezone(__import__("zoneinfo").ZoneInfo("Europe/Istanbul"))
        appointment = (f"Görüşme başlangıcı: {local:%d.%m.%Y %H:%M} (Türkiye saati).\n"
                       "Mülakatı planlanan başlangıçtan itibaren 8 saat içinde başlatabilirsiniz.\n\n")
    body = (
        f"Merhaba {name},\n\n"
        f"{company} bünyesindeki {position} pozisyonu için mülakat bağlantınız:\n{url}\n\n"
        f"{appointment}"
        f"Bağlantının son geçerlilik zamanı: {deadline} (Türkiye saati).\n"
        "Mülakatı bu süre içinde başlatmanız gerekmektedir.\n\n"
        "İyi çalışmalar,\nERNASA Yazılım Sistemleri\n"
    )
    return recipient, subject, body


def _send_brevo(*, api_key, sender, recipient, subject, text, html=None):
    """Send one explicitly approved invitation via HTTPS; no retries or fallbacks.

    Brevo does not provide Resend's Idempotency-Key behavior. The interview
    endpoint's persisted pending/sent guard prevents routine duplicate clicks.
    Ambiguous network failures must be investigated before manual retry.
    """
    payload = {
        "sender": {"name": "ERNASA Yazılım Sistemleri", "email": sender},
        "to": [{"email": recipient}],
        "subject": subject,
        "textContent": text,
    }
    if html is not None:
        payload["htmlContent"] = html
    request = Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "ERNASA-Mailer/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read(8192))
    except HTTPError as exc:
        # Do not read/log provider bodies: they can contain addresses or tokens,
        # and reading itself can raise an OSError.
        status = int(getattr(exc, "code", 0) or 0)
        if status in (401, 403):
            meaning = "API anahtarı veya gönderim yetkisi reddedildi. Brevo hesabını ve doğrulanmış göndericiyi kontrol edin."
        elif status in (400, 404, 422):
            meaning = "E-posta isteği veya gönderici reddedildi. Brevo gönderici ayarlarını kontrol edin."
        elif status == 429:
            meaning = "Brevo gönderim sınırına ulaşıldı."
        elif 500 <= status < 600:
            meaning = "Brevo servisinde geçici hata var."
        else:
            meaning = "E-posta sağlayıcısı isteği reddetti."
        raise EmailDeliveryError(f"Brevo HTTP {status}: {meaning}") from None
    except (URLError, TimeoutError, OSError, ValueError, UnicodeError, TypeError):
        raise EmailDeliveryError(
            "Brevo gönderim sonucu doğrulanamadı. Tekrar göndermeden önce işlem kayıtlarını kontrol edin."
        ) from None
    if not isinstance(result, dict) or not isinstance(result.get("messageId"), str) or not result["messageId"]:
        raise EmailDeliveryError(
            "Brevo gönderim onayı alınamadı. Tekrar göndermeden önce işlem kayıtlarını kontrol edin."
        )
    return result["messageId"]


def send_invitation(candidate, url, *, idempotency_key):
    recipient, subject, body = invitation_message(candidate, url)
    return _send_text(recipient, subject, body, idempotency_key)


def send_availability(candidate, url, *, idempotency_key):
    address = validate_recipient(candidate["email"])
    subject = "ERNASA · Mülakat için uygun gün ve saat"
    body = ("Merhaba,\n\nBaşvurunuz için bir görüşme saati seçmenizi rica ediyoruz.\n"
            f"{url}\n\nBu saat seçme bağlantısı size özeldir ve 7 gün geçerlidir. Mülakat bağlantısı saat seçiminizden sonra ayrı bir e-postayla gönderilir.\nERNASA")
    return _send_text(address, subject, body, idempotency_key)


def _send_text(recipient, subject, body, idempotency_key):
    api_key, sender = configured_sender()
    if _email_provider() == "brevo":
        return _send_brevo(api_key=api_key, sender=sender, recipient=recipient,
                           subject=subject, text=body)
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
    except HTTPError as exc:
        raise EmailDeliveryError(_safe_provider_error(exc)) from None
    except (URLError, TimeoutError, ValueError):
        raise EmailDeliveryError("E-posta servisine erişilemedi veya geçerli yanıt alınamadı.") from None
    if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]:
        raise EmailDeliveryError("Kurumsal e-posta servisinden gönderim onayı alınamadı.")
    return result["id"]


def send_training_invitation(*, recipient, name, url, expires_at, idempotency_key):
    """Send a real HTML + plain text academy invite (never a mailto draft).

    The target email, full invitation URL and deadline come from a validated
    stored training invite, not from arbitrary client-controlled HTML.
    """
    api_key, sender = configured_sender()
    email_address = validate_recipient(recipient)
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
    if _email_provider() == "brevo":
        return _send_brevo(api_key=api_key, sender=sender, recipient=email_address,
                           subject=subject, text=plain, html=html)
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
    except HTTPError as exc:
        raise EmailDeliveryError(_safe_provider_error(exc)) from None
    except (URLError, TimeoutError, ValueError):
        raise EmailDeliveryError("E-posta servisine erişilemedi veya geçerli yanıt alınamadı.") from None
    if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]:
        raise EmailDeliveryError("E-posta servisi gönderim onayı döndürmedi.")
    return result["id"]


def inspect_sender_domain():
    """Read-only Resend domain probe; never query Resend for Brevo setups.

    Returns fixed status fields only, never raw API responses, credentials,
    domain IDs, DNS records or names of unrelated domains. No email is sent.
    A 401 on GET /domains often means the production key is send-only.
    """
    try:
        if _email_provider() == "brevo":
            configured_sender()
            return {"result": "not_applicable", "provider": "brevo"}
    except EmailConfigurationError:
        return {"result": "missing_configuration"}
    try:
        key, sender = configured_sender()
    except EmailConfigurationError:
        return {"result":"missing_configuration"}
    expected_domain = sender.rsplit("@",1)[1]
    try:
        request = Request(
            "https://api.resend.com/domains?limit=100",
            headers={"Authorization":"Bearer "+key,"Accept":"application/json"},
            method="GET",
        )
        with urlopen(request,timeout=8) as response:
            data=json.loads(response.read(131072))
        domains=data.get("data",[]) if isinstance(data,dict) else []
        if not isinstance(domains,list):
            return {"result":"unexpected_response"}
        for item in domains:
            if isinstance(item,dict) and str(item.get("name","")).lower()==expected_domain:
                status=str(item.get("status","")).lower()
                if status not in {"verified","pending","not_started","failed","temporary_failure"}:
                    status="unknown"
                capabilities=item.get("capabilities",{})
                sending=capabilities.get("sending",{}) if isinstance(capabilities,dict) else {}
                enabled=sending.get("enabled") if isinstance(sending,dict) else None
                sendstatus=sending.get("status") if isinstance(sending,dict) else None
                if sendstatus not in {"verified","pending","not_started","failed","temporary_failure"}:
                    sendstatus="unknown"
                return {
                    "result":"found","domain_status":status,
                    "sending_enabled":enabled if type(enabled) is bool else None,
                    "sending_status":sendstatus,
                }
        # A limited listing cannot conclusively establish nonexistence.
        more=data.get("has_more") if isinstance(data,dict) else None
        return {"result":"not_in_first_page" if more is True else "not_found"}
    except HTTPError as exc:
        status=int(getattr(exc,"code",0) or 0)
        # Read only the provider's documented error name. Do not expose
        # arbitrary messages, credentials, recipients, or request details.
        try:
            data=json.loads(exc.read(2048))
            provider_name=str(data.get("name","")).lower() if isinstance(data,dict) else ""
        except (ValueError,OSError,TypeError,UnicodeError):
            provider_name=""
        allowed_names={
            "invalid_api_key","restricted_api_key","validation_error",
            "missing_api_key","security_error","invalid_access",
        }
        error_name=provider_name if provider_name in allowed_names else "other"
        if status==401:
            return {"result":"read_access_denied_401",
                    "error_name":error_name,
                    "note":"sending_only_key_may_be_valid"}
        if status==403:
            return {"result":"read_access_denied_403","error_name":error_name}
        return {"result":"provider_http_error",
                "status":status if 400<=status<=599 else None,
                "error_name":error_name}
    except (URLError,TimeoutError,OSError,ValueError,TypeError):
        return {"result":"read_request_failed"}
