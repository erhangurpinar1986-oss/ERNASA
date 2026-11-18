"""Offline-only Brevo email tests: no network calls or real recipients."""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest import mock
from urllib.error import HTTPError, URLError

from services import invite_email


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_):
        return b'{"messageId":"<offline-test-id@brevo.invalid>"}'


class BrevoInvitationTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {
            "ERNASA_EMAIL_PROVIDER": "brevo",
            "ERNASA_BREVO_API_KEY": "offline-secret-key",
            "ERNASA_RESEND_API_KEY": "",
            "ERNASA_EMAIL_FROM": "iletisim@ernasa.com",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.expiry = (datetime.now(timezone.utc) + timedelta(hours=7)).isoformat()

    def test_missing_brevo_key_fails_closed_without_resend_fallback(self):
        with mock.patch.dict(os.environ, {"ERNASA_BREVO_API_KEY": ""}):
            with self.assertRaises(invite_email.EmailConfigurationError):
                invite_email.configured_sender()

    def test_unknown_provider_fails_closed(self):
        with mock.patch.dict(os.environ, {"ERNASA_EMAIL_PROVIDER": "unknown"}):
            with self.assertRaises(invite_email.EmailConfigurationError):
                invite_email.configured_sender()

    def test_brevo_interview_request(self):
        candidate = {"name": "Offline Test", "email": "test@example.invalid",
                     "company": "ERNASA", "position": "Test",
                     "expires_at": self.expiry}
        with mock.patch.object(invite_email, "urlopen", return_value=_Response()) as opener:
            message_id = invite_email.send_invitation(
                candidate, "https://ernasa.com/interview/offline", idempotency_key="offline-1")
        self.assertEqual(message_id, "<offline-test-id@brevo.invalid>")
        self.assertEqual(opener.call_count, 1)
        req = opener.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.brevo.com/v3/smtp/email")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Api-key"), "offline-secret-key")
        self.assertEqual(req.get_header("Accept"), "application/json")
        self.assertIsNone(req.get_header("Idempotency-key"))
        data = json.loads(req.data)
        self.assertEqual(data["sender"]["email"], "iletisim@ernasa.com")
        self.assertEqual(data["to"], [{"email": "test@example.invalid"}])
        self.assertIn("/interview/offline", data["textContent"])
        self.assertNotIn("offline-secret-key", str(data))

    def test_brevo_training_request_contains_html_and_text(self):
        token = "a" * 43
        with mock.patch.object(invite_email, "urlopen", return_value=_Response()) as opener:
            invite_email.send_training_invitation(
                recipient="test@example.invalid", name="Offline Test",
                url="https://ernasa.com/egitim/" + token,
                expires_at=self.expiry, idempotency_key="offline-2")
        self.assertEqual(opener.call_count, 1)
        data = json.loads(opener.call_args.args[0].data)
        self.assertIn("htmlContent", data)
        self.assertIn("textContent", data)
        self.assertIn(token, data["htmlContent"])
        self.assertEqual(data["to"], [{"email": "test@example.invalid"}])

    def test_rejected_http_does_not_read_or_leak_provider_body(self):
        body = b'secret-recipient@example.invalid api-secret'
        for status in (400, 401, 403, 422, 429, 503):
            with self.subTest(status=status):
                error = HTTPError("https://api.brevo.com/v3/smtp/email",
                                  status, "Rejected", {}, BytesIO(body))
                with mock.patch.object(invite_email, "urlopen", side_effect=error):
                    with self.assertRaises(invite_email.EmailDeliveryError) as ctx:
                        invite_email.send_invitation(
                            {"name": "A", "email": "test@example.invalid",
                             "company": "ERNASA", "position": "Test",
                             "expires_at": self.expiry},
                            "https://ernasa.com/interview/offline",
                            idempotency_key="offline")
                self.assertNotIn("secret-recipient", str(ctx.exception))
                self.assertNotIn("api-secret", str(ctx.exception))
                self.assertEqual(error.fp.tell(), 0)

    def test_ambiguous_network_error_never_falls_back_to_resend(self):
        with mock.patch.object(invite_email, "urlopen",
                               side_effect=URLError("offline-secret-key")) as opener:
            with self.assertRaises(invite_email.EmailDeliveryError) as ctx:
                invite_email.send_training_invitation(
                    recipient="test@example.invalid", name="Offline",
                    url="https://ernasa.com/egitim/" + "a" * 43,
                    expires_at=self.expiry, idempotency_key="offline")
        self.assertEqual(opener.call_count, 1)
        self.assertNotIn("offline-secret-key", str(ctx.exception))
        self.assertIn("Tekrar göndermeden", str(ctx.exception))

    def test_no_resend_domain_probe_for_brevo(self):
        with mock.patch.object(invite_email, "urlopen") as opener:
            self.assertEqual(invite_email.inspect_sender_domain(),
                             {"result": "not_applicable", "provider": "brevo"})
        opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
