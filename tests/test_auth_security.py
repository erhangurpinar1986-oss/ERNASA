"""Tests for auth_security: Turnstile, phone, SMS gateway, OTP (synthetic data only, no network)."""
import unittest
from datetime import datetime, timedelta, timezone

from auth_security import otp, phone, sms, turnstile

PEPPER = b'p' * 32
NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def fake_transport(payload):
    calls = []

    def post(url, fields, timeout):
        calls.append(fields)
        if isinstance(payload, Exception):
            raise payload
        return payload
    post.calls = calls
    return post


def good(**overrides):
    payload = {'success': True, 'hostname': 'ernasa.com', 'action': 'login',
               'challenge_ts': (NOW - timedelta(seconds=10)).isoformat()}
    payload.update(overrides)
    return payload


class TurnstileTests(unittest.TestCase):
    def verifier(self, payload, **kw):
        kw.setdefault('secret', '0x' + 'A' * 33)
        return turnstile.TurnstileVerifier(hostnames='ernasa.com,www.ernasa.com', mode='enforce',
                                          transport=fake_transport(payload), clock=lambda: NOW, **kw)

    def test_off_mode_never_blocks_users(self):
        v = turnstile.TurnstileVerifier(secret='', hostnames='', mode='off')
        self.assertTrue(v.verify('', 'login'))
        self.assertFalse(v.verify('', 'login').enforced)

    def test_valid_token(self):
        v = self.verifier(good())
        self.assertTrue(v.verify('tok', 'login', '203.0.113.5'))
        sent = v.transport.calls[0]
        self.assertEqual(sent['remoteip'], '203.0.113.5')
        self.assertIn('idempotency_key', sent)

    def test_rejections(self):
        cases = {
            'rejected-by-cloudflare': ({'success': False, 'error-codes': ['invalid-input-response']}, 'invalid-input-response'),
            'replayed': ({'success': False, 'error-codes': ['timeout-or-duplicate']}, 'timeout-or-duplicate'),
            'wrong-host': (good(hostname='evil.example'), 'hostname-mismatch'),
            'wrong-action': (good(action='signup'), 'action-mismatch'),
            'expired': (good(challenge_ts=(NOW - timedelta(seconds=301)).isoformat()), 'expired'),
            'garbage': ('not json', 'rejected'),
        }
        for name, (payload, reason) in cases.items():
            with self.subTest(name):
                result = self.verifier(payload).verify('tok', 'login')
                self.assertFalse(result)
                self.assertEqual(result.reason, reason)

    def test_missing_and_oversized_tokens_never_reach_cloudflare(self):
        v = self.verifier(good())
        self.assertFalse(v.verify('', 'login'))
        self.assertFalse(v.verify('x' * 2049, 'login'))
        self.assertEqual(v.transport.calls, [])

    def test_network_failure_fails_closed(self):
        self.assertEqual(self.verifier(OSError('down')).verify('tok', 'login').reason, 'verification-unavailable')

    def test_test_and_production_keys_are_separated(self):
        with self.assertRaises(turnstile.TurnstileConfigError):
            turnstile.TurnstileVerifier(secret='1x0000000000000000000000000000000AA', hostnames='ernasa.com', mode='enforce')
        with self.assertRaises(turnstile.TurnstileConfigError):
            turnstile.TurnstileVerifier(secret='0xREALKEY', hostnames='', mode='test')
        with self.assertRaises(turnstile.TurnstileConfigError):
            turnstile.TurnstileVerifier(secret='0xREALKEY', hostnames='', mode='enforce')
        v = turnstile.TurnstileVerifier(secret='1x0000000000000000000000000000000AA', hostnames='', mode='test',
                                        transport=fake_transport({'success': True}))
        self.assertTrue(v.verify('XXXX.DUMMY.TOKEN.XXXX', 'login'))


class PhoneTests(unittest.TestCase):
    def test_normalize(self):
        for raw in ('0532 123 45 67', '5321234567', '+90 (532) 123-45-67', '0090 532 123 4567'):
            self.assertEqual(phone.normalize(raw), '+905321234567')
        self.assertEqual(phone.normalize('+44 7911 123456'), '+447911123456')

    def test_rejects(self):
        for raw in ('', '123', '+90 532 123', '+0123456789', 'abc', '+9053212345678'):
            with self.subTest(raw), self.assertRaises(phone.PhoneError):
                phone.normalize(raw)

    def test_mask_reveals_only_last_two_digits(self):
        self.assertEqual(phone.mask('+905321234567'), '+90 532 *** ** 67')
        self.assertNotIn('1234', phone.mask('+447911123456'))
        self.assertEqual(phone.mask('junk'), '***')


class SmsGatewayTests(unittest.TestCase):
    def test_audit_never_contains_text_or_full_number(self):
        events = []
        g = sms.SmsGateway(sms.TestSmsProvider(), daily_limit=10, daily_cost_limit_minor=1000,
                           unit_cost_minor=50, audit=events.append)
        g.send('+905321234567', 'ERNASA kodunuz: 123456', 'password_reset')
        text = repr(events)
        self.assertNotIn('123456', text)
        self.assertNotIn('5321234567', text)
        self.assertEqual(events[0]['to'], '+90 532 *** ** 67')

    def test_daily_and_cost_limits(self):
        g = sms.SmsGateway(sms.TestSmsProvider(), daily_limit=2, daily_cost_limit_minor=10_000, unit_cost_minor=50)
        g.send('+905321234567', 'a', 't')
        g.send('+905321234567', 'a', 't')
        with self.assertRaises(sms.SmsLimitError):
            g.send('+905321234567', 'a', 't')
        g = sms.SmsGateway(sms.TestSmsProvider(), daily_limit=100, daily_cost_limit_minor=100, unit_cost_minor=50)
        g.send('+905321234567', 'a', 't')
        g.send('+905321234567', 'a', 't')
        with self.assertRaises(sms.SmsLimitError):
            g.send('+905321234567', 'a', 't')

    def test_limits_reset_next_day(self):
        clock = Clock(datetime(2026, 11, 27, 23, 59, tzinfo=timezone.utc).timestamp())
        g = sms.SmsGateway(sms.TestSmsProvider(), daily_limit=1, daily_cost_limit_minor=10_000, unit_cost_minor=1, clock=clock)
        g.send('+905321234567', 'a', 't')
        clock.t += 120
        g.send('+905321234567', 'a', 't')

    def test_invalid_destination(self):
        with self.assertRaises(sms.SmsError):
            sms.SmsGateway(sms.TestSmsProvider()).send('05321234567', 'a', 't')

    def test_unactivated_provider_is_refused(self):
        import os
        old = os.environ.get('SMS_PROVIDER')
        os.environ['SMS_PROVIDER'] = 'netgsm'
        try:
            with self.assertRaises(sms.SmsError):
                sms.gateway_from_env()
        finally:
            if old is None:
                del os.environ['SMS_PROVIDER']
            else:
                os.environ['SMS_PROVIDER'] = old


class OtpTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.svc = otp.OtpService(pepper=PEPPER, clock=self.clock)

    def issue(self, subject='u1', phone_no='+905321234567', ip='203.0.113.1'):
        return self.svc.issue('password_reset', subject, phone_no, ip)[0]

    def test_code_shape_and_single_use(self):
        code = self.issue()
        self.assertRegex(code, r'^\d{6}$')
        self.assertTrue(self.svc.verify('password_reset', 'u1', code))
        with self.assertRaises(otp.OtpError):
            self.svc.verify('password_reset', 'u1', code)

    def test_code_is_not_stored_in_clear(self):
        code = self.issue()
        self.assertNotIn(code, repr(self.svc.store.active))

    def test_expiry(self):
        code = self.issue()
        self.clock.t += otp.TTL_SECONDS
        with self.assertRaises(otp.OtpError) as e:
            self.svc.verify('password_reset', 'u1', code)
        self.assertEqual(e.exception.reason, 'expired')

    def test_three_wrong_attempts_destroy_code(self):
        code = self.issue()
        wrong = f'{(int(code) + 1) % 10**6:06d}'
        for reason in ('wrong_code', 'wrong_code', 'too_many_attempts'):
            with self.assertRaises(otp.OtpError) as e:
                self.svc.verify('password_reset', 'u1', wrong)
            self.assertEqual(e.exception.reason, reason)
        with self.assertRaises(otp.OtpError):
            self.svc.verify('password_reset', 'u1', code)

    def test_cooldown_and_new_code_invalidates_old(self):
        first = self.issue()
        with self.assertRaises(otp.OtpError) as e:
            self.issue()
        self.assertEqual(e.exception.reason, 'cooldown')
        self.clock.t += otp.RESEND_COOLDOWN
        second = self.issue()
        if first != second:
            with self.assertRaises(otp.OtpError):
                self.svc.verify('password_reset', 'u1', first)
        self.assertTrue(self.svc.verify('password_reset', 'u1', second))

    def test_sms_bombing_limits_per_phone_and_ip(self):
        for i in range(5):
            self.issue(subject=f'acct{i}', ip=f'198.51.100.{i}')
        with self.assertRaises(otp.OtpError) as e:
            self.issue(subject='acct9', ip='198.51.100.9')
        self.assertEqual(e.exception.reason, 'phone_limit')
        for i in range(20):
            self.issue(subject=f'x{i}', phone_no=f'+90532123{i:04d}', ip='192.0.2.7')
        with self.assertRaises(otp.OtpError) as e:
            self.issue(subject='y', phone_no='+905329999999', ip='192.0.2.7')
        self.assertEqual(e.exception.reason, 'ip_limit')

    def test_purposes_are_isolated(self):
        code = self.issue()
        with self.assertRaises(otp.OtpError):
            self.svc.verify('phone_verification', 'u1', code)

    def test_weak_pepper_rejected(self):
        with self.assertRaises(ValueError):
            otp.OtpService(pepper=b'short')

    def test_codes_are_not_predictable(self):
        codes = set()
        for i in range(200):
            codes.add(self.svc.issue('t', f's{i}', f'+9053200{i:05d}', f'10.0.{i // 250}.{i % 250}')[0])
        self.assertGreater(len(codes), 190)


if __name__ == '__main__':
    unittest.main()
