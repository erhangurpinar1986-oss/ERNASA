"""Offline-only regression tests for transactional email HTTP handling."""
from datetime import datetime, timedelta, timezone
from email.message import Message
from io import BytesIO
import json
import os
from unittest import TestCase, mock
from urllib.error import HTTPError

from services import invite_email


def _error(body, content_type="application/json"):
    headers = Message()
    headers["Content-Type"] = content_type
    return HTTPError(
        "https://api.resend.com/emails", 403, "Forbidden", headers, BytesIO(body)
    )


class ResendHTTPTests(TestCase):
    def test_html_forbidden_does_not_expose_response(self):
        err = _error(b"<!DOCTYPE html><html>secret-candidate-token</html>", "text/html")
        message = invite_email._safe_provider_error(err)
        self.assertIn("HTML", message)
        self.assertNotIn("secret-candidate-token", message)

    def test_json_domain_error_remains_actionable(self):
        err = _error(json.dumps({
            "name": "validation_error", "message": "sender domain is not verified"
        }).encode())
        self.assertIn("alan ad", invite_email._safe_provider_error(err))

    @mock.patch.dict(os.environ, {
        "ERNASA_RESEND_API_KEY": "re_test_offline_mock",
        "ERNASA_EMAIL_FROM": "iletisim@ernasa.com",
    })
    @mock.patch.object(invite_email, "urlopen")
    def test_interview_and_training_use_explicit_http_identity(self, mock_open):
        class Reply:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, *args):
                return b'{"id":"test-id"}'
        mock_open.return_value = Reply()
        expiry = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        invite_email.send_invitation({
            "email": "dummy@example.org",
            "name": "Test",
            "company": "ERNASA",
            "position": "Test",
            "expires_at": expiry,
        }, "https://ernasa.com/interview/dummy", idempotency_key="unit-test-one")
        invite_email.send_training_invitation(
            recipient="dummy@example.org",
            name="Test",
            url="https://ernasa.com/egitim/" + "a" * 43,
            expires_at=expiry,
            idempotency_key="unit-test-two",
        )
        self.assertEqual(mock_open.call_count, 2)
        for call in mock_open.call_args_list:
            request = call.args[0]
            self.assertTrue(request.get_header("User-agent").startswith("ERNASA-Mailer/"))
            self.assertEqual(request.get_header("Accept"), "application/json")
            self.assertTrue(request.get_header("Authorization").startswith("Bearer "))
            self.assertEqual(request.get_method(), "POST")
