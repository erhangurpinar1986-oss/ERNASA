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
            "ERNASA_EMAIL_FROM": "iletisim@ernasa.com",
        }), patch.object(invite_email, "urlopen", return_value=FakeResponse()) as call:
            msgid = invite_email.send_invitation(
                self.candidate, "https://ernasa.com/interview/test", idempotency_key="test-invite")
            self.assertEqual(msgid, "test-msg-123")
            request = call.call_args.args[0]
            payload = json.loads(request.data)
            self.assertEqual(payload["from"], "ERNASA Yazılım Sistemleri <iletisim@ernasa.com>")
            self.assertEqual(payload["to"], ["candidate@example.org"])
            self.assertEqual(request.get_header("Idempotency-key"), "test-invite")
            self.assertNotIn("test-only", str(payload))

    def test_readonly_domain_probe_verified(self):
        import json
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def read(self,*args): return json.dumps({"data":[
                {"name":"ernasa.com","status":"verified",
                 "capabilities":{"sending":{"enabled":True,"status":"verified"}}}
            ]}).encode()
        with patch.dict(os.environ, {
            "ERNASA_RESEND_API_KEY":"test-only","ERNASA_EMAIL_FROM":"iletisim@ernasa.com"
        }),patch.object(invite_email,"urlopen",return_value=FakeResponse()) as mock:
            report=invite_email.inspect_sender_domain()
            req=mock.call_args.args[0]
            self.assertEqual(req.get_method(),"GET")
            self.assertEqual(req.full_url,"https://api.resend.com/domains?limit=100")
            self.assertEqual(report,{"result":"found","domain_status":"verified",
                                    "sending_enabled":True,"sending_status":"verified"})
            self.assertNotIn("test-only",str(report))

    def test_readonly_domain_probe_restricted_key_is_inconclusive(self):
        from urllib.error import HTTPError
        import io
        with patch.dict(os.environ, {
            "ERNASA_RESEND_API_KEY":"test-only","ERNASA_EMAIL_FROM":"iletisim@ernasa.com"
        }), patch.object(invite_email,"urlopen",
            side_effect=HTTPError("https://api.resend.com/domains",401,"no",{},io.BytesIO(b'{}'))):
            report=invite_email.inspect_sender_domain()
            self.assertEqual(report["result"],"read_access_denied_401")
            self.assertEqual(report["note"],"sending_only_key_may_be_valid")

    def test_safe_resend_rejection_categories(self):
        import io
        from urllib.error import HTTPError
        examples = [
            (401,'{"name":"invalid_api_key","message":"Invalid API key"}',"API anahtarı"),
            (403,'{"name":"validation_error","message":"You can only send testing emails to your own email address"}',"test alıcısına"),
            (403,'{"name":"validation_error","message":"Domain is not verified"}',"alan adı"),
            (422,'{"name":"validation_error","message":"Invalid from sender"}',"Gönderen adresi"),
            (429,'{"name":"rate_limit_exceeded","message":"Too many requests"}',"sınırına"),
        ]
        for code, body, expected in examples:
            with self.subTest(code=code,expected=expected):
                err=HTTPError("https://api.resend.com/emails",code,"test",{},io.BytesIO(body.encode()))
                mapped=invite_email._safe_provider_error(err)
                self.assertIn(expected,mapped)
                self.assertNotIn("Invalid API key",mapped)

    def test_safe_resend_error_does_not_echo_untrusted_email(self):
        import io
        from urllib.error import HTTPError
        err=HTTPError("https://api.resend.com/emails",422,"bad",{},
            io.BytesIO(b'{"message":"recipient bad_person@example.com has failed validation"}'))
        self.assertNotIn("bad_person@example.com",invite_email._safe_provider_error(err))

    def test_provider_error_does_not_leak_key(self):
        from urllib.error import URLError
        with patch.dict(os.environ, {
            "ERNASA_RESEND_API_KEY": "secret-testing-key",
            "ERNASA_EMAIL_FROM": "iletisim@ernasa.com",
        }), patch.object(invite_email, "urlopen", side_effect=URLError("secret-testing-key")):
            with self.assertRaises(invite_email.EmailDeliveryError) as error:
                invite_email.send_invitation(
                    self.candidate, "https://ernasa.com/interview/test", idempotency_key="x")
            self.assertNotIn("secret-testing-key", str(error.exception))


if __name__ == "__main__":
    unittest.main()
