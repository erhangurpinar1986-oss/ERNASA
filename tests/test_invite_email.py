"""No-live-email regression tests for ERNASA interview invitation delivery."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from services import invite_email


class InvitationEmailTests(unittest.TestCase):
    def setUp(self):
        self.expiry = (datetime.now(timezone.utc) + timedelta(hours=7)).isoformat()
        self.candidate = {
            "name": "Deneme Aday",
            "email": "candidate@example.org",
            "company": "ERNASA",
            "position": "B2B Satış Ortağı",
            "expires_at": self.expiry,
        }

    def test_missing_provider_configuration_fails_closed(self):
        with patch.dict(os.environ, {"ERNASA_RESEND_API_KEY": ""}, clear=False):
            with self.assertRaises(invite_email.EmailConfigurationError):
                invite_email.configured_sender()

    def test_only_official_sender_addresses_allowed(self):
        with patch.dict(os.environ, {
            "ERNASA_RESEND_API_KEY": "test-only",
            "ERNASA_EMAIL_FROM": "user@gmail.com",
        }):
            with self.assertRaises(invite_email.EmailConfigurationError):
                invite_email.configured_sender()

    def test_url_and_deadline_are_present(self):
        address, subject, body = invite_email.invitation_message(
            self.candidate, "https://ernasa.com/interview/fake-test-token")
        self.assertEqual(address, "candidate@example.org")
        self.assertIn("B2B Satış Ortağı", subject)
        self.assertIn("https://ernasa.com/interview/fake-test-token", body)
        self.assertIn("Türkiye saati", body)

    def test_rejects_bad_email(self):
        for email in ("", "other@example.org\nBcc:a@b.com", "Mr X <a@example.org>", "abc"):
            with self.subTest(email=email):
                with self.assertRaises(ValueError):
                    invite_email.invitation_message({**self.candidate, "email":email}, "https://ernasa.com")

    def test_provider_receives_correct_sender_and_candidate_only(self):
        import json

        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self, *_):
                return b'{"id":"test-msg-123"}'

        with patch.dict(os.environ, {
            "ERNASA_RESEND_API_KEY": "test-only",
            "ERNASA_EMAIL_FROM": "office@ernasa.com",
        }), patch.object(invite_email, "urlopen", return_value=FakeResponse()) as call:
            msgid = invite_email.send_invitation(
                self.candidate, "https://ernasa.com/interview/test", idempotency_key="test-invite")
            self.assertEqual(msgid, "test-msg-123")
            request = call.call_args.args[0]
            payload = json.loads(request.data)
            self.assertEqual(payload["from"], "ERNASA Yazılım Sistemleri <office@ernasa.com>")
            self.assertEqual(payload["to"], ["candidate@example.org"])
            self.assertEqual(request.get_header("Idempotency-key"), "test-invite")
            self.assertNotIn("test-only", str(payload))

    def test_provider_error_does_not_leak_key(self):
        from urllib.error import URLError
        with patch.dict(os.environ, {
            "ERNASA_RESEND_API_KEY": "secret-testing-key",
            "ERNASA_EMAIL_FROM": "office@ernasa.com",
        }), patch.object(invite_email, "urlopen", side_effect=URLError("secret-testing-key")):
            with self.assertRaises(invite_email.EmailDeliveryError) as error:
                invite_email.send_invitation(
                    self.candidate, "https://ernasa.com/interview/test", idempotency_key="x")
            self.assertNotIn("secret-testing-key", str(error.exception))


if __name__ == "__main__":
    unittest.main()
