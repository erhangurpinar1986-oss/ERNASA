"""Regression tests for account-based login throttling (ERNASA SECURITY 2026, section 11)."""
import unittest
from types import SimpleNamespace

from control import login_throttle
from control.login_throttle import LoginThrottle, restriction_seconds


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class LoginThrottleTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.t = LoginThrottle('control', clock=self.clock)

    def fail(self, address, times):
        for _ in range(times):
            self.assertEqual(self.t.check(address), 0)
            self.t.failure(address)

    def test_policy_table(self):
        self.assertEqual([restriction_seconds(n) for n in (1, 2, 3, 4, 5, 6)],
                         [0, 0, 300, 300, 900, 900])
        self.assertEqual(restriction_seconds(7), 1800)
        self.assertEqual(restriction_seconds(9), 3600)
        self.assertEqual(restriction_seconds(50), 3600)

    def test_first_two_failures_do_not_block_but_second_requests_challenge(self):
        self.fail('a@ernasa.com', 1)
        self.assertFalse(self.t.needs_challenge('a@ernasa.com'))
        self.fail('a@ernasa.com', 1)
        self.assertTrue(self.t.needs_challenge('a@ernasa.com'))
        self.assertEqual(self.t.check('a@ernasa.com'), 0)

    def test_third_failure_blocks_five_minutes_then_releases(self):
        self.fail('a@ernasa.com', 3)
        self.assertGreater(self.t.check('a@ernasa.com'), 290)
        self.clock.now += 301
        self.assertEqual(self.t.check('a@ernasa.com'), 0)

    def test_attack_on_one_account_never_blocks_another(self):
        self.fail('victim@ernasa.com', 3)
        for _ in range(20):
            self.t.failure('victim@ernasa.com')
        self.assertGreater(self.t.check('victim@ernasa.com'), 0)
        self.assertEqual(self.t.check('founder@ernasa.com'), 0)

    def test_case_and_whitespace_do_not_bypass(self):
        self.fail('a@ernasa.com', 3)
        self.assertGreater(self.t.check('  A@ERNASA.COM '), 0)

    def test_success_clears_counter(self):
        self.fail('a@ernasa.com', 2)
        self.t.success('a@ernasa.com')
        self.assertFalse(self.t.needs_challenge('a@ernasa.com'))

    def test_old_failures_are_forgotten(self):
        self.fail('a@ernasa.com', 2)
        self.clock.now += login_throttle.RESET_AFTER + 1
        self.fail('a@ernasa.com', 2)
        self.assertEqual(self.t.check('a@ernasa.com'), 0)

    def test_global_backstop(self):
        t = LoginThrottle('control', clock=self.clock, global_limit=3)
        for i in range(3):
            self.assertEqual(t.check(f'u{i}@x.com'), 0)
        self.assertGreater(t.check('other@x.com'), 0)
        self.clock.now += 61
        self.assertEqual(t.check('other@x.com'), 0)

    def test_memory_is_bounded(self):
        old = login_throttle.MAX_KEYS
        login_throttle.MAX_KEYS = 50
        try:
            for i in range(200):
                self.t.failure(f'u{i}@x.com')
            self.assertLessEqual(len(self.t.store.failures), 50)
        finally:
            login_throttle.MAX_KEYS = old

    def test_email_is_never_stored(self):
        self.t.failure('secret.person@ernasa.com')
        self.assertNotIn('secret.person', repr(self.t.store.failures))

    def test_trusted_device_is_not_locked_by_attacker(self):
        device = self.t.success('owner@ernasa.com')
        for _ in range(20):  # attacker has no device token
            self.t.failure('owner@ernasa.com')
        self.assertGreater(self.t.check('owner@ernasa.com'), 0)
        self.assertEqual(self.t.check('owner@ernasa.com', device), 0)

    def test_trusted_device_still_throttled_on_its_own(self):
        device = self.t.success('owner@ernasa.com')
        for _ in range(3):
            self.t.failure('owner@ernasa.com', device)
        self.assertGreater(self.t.check('owner@ernasa.com', device), 0)

    def test_forged_or_foreign_device_token_counts_as_untrusted(self):
        other = self.t.success('other@ernasa.com')
        for _ in range(3):
            self.t.failure('owner@ernasa.com', 'forged-token')
        self.assertGreater(self.t.check('owner@ernasa.com', 'forged-token'), 0)
        self.assertGreater(self.t.check('owner@ernasa.com', other), 0)

    def test_device_trust_expires(self):
        device = self.t.success('owner@ernasa.com')
        self.clock.now += login_throttle.DEVICE_TTL + 1
        for _ in range(3):
            self.t.failure('owner@ernasa.com')
        self.assertGreater(self.t.check('owner@ernasa.com', device), 0)

    def test_store_failure_falls_back_to_memory(self):
        class Broken:
            def __getattr__(self, name):
                def fail(*a):
                    raise OSError('db down')
                return fail
        t = LoginThrottle('control', store=Broken(), clock=self.clock)
        self.assertEqual(t.check('a@x.com'), 0)
        for _ in range(3):
            t.failure('a@x.com')
        self.assertGreater(t.check('a@x.com'), 0)

    def test_control_and_sales_areas_are_separate(self):
        server = SimpleNamespace()
        control = login_throttle.for_server(server, 'control')
        sales = login_throttle.for_server(server, 'sales')
        self.assertIsNot(control, sales)
        self.assertIs(control, login_throttle.for_server(server, 'control'))
        for _ in range(10):
            sales.failure('founder@ernasa.com')
        self.assertEqual(control.check('founder@ernasa.com'), 0)


if __name__ == '__main__':
    unittest.main()
