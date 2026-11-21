"""Synthetic Resend webhook security regression tests."""
import base64
import hashlib
import hmac
import json

from services.email_delivery_webhook import verify_signature, accept_event


def _signed(body, secret="whsec_" + base64.b64encode(b"test-secret-key").decode()):
    timestamp = "1800000000"
    msg_id = "evt-test-123"
    message = msg_id.encode() + b"." + timestamp.encode() + b"." + body
    signature = base64.b64encode(hmac.new(b"test-secret-key", message, hashlib.sha256).digest()).decode()
    return {"svix-id": msg_id, "svix-timestamp": timestamp, "svix-signature": "v1," + signature}, secret


def test_valid_resend_signature():
    body = json.dumps({"type": "email.delivered", "data": {"email_id": "email-test"}}).encode()
    headers, secret = _signed(body)
    assert verify_signature(body, headers, secret=secret, now=1800000000)


def test_tampered_body_rejected():
    body = b'{"type":"email.sent"}'
    headers, secret = _signed(body)
    assert not verify_signature(b'{"type":"email.delivered"}', headers, secret=secret, now=1800000000)


def test_expired_signature_rejected():
    headers, secret = _signed(b"{}")
    assert not verify_signature(b"{}", headers, secret=secret, now=1800001000)


def test_unsigned_webhook_rejected_before_database():
    try:
        accept_event(b'{"type":"email.delivered"}', {})
    except ValueError:
        pass
    else:
        raise AssertionError("Unsigned webhook must be rejected")
