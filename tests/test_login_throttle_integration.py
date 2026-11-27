"""End-to-end throttling through the real CONTROL and Sales Hub login functions (SQLite, synthetic users)."""
import secrets
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from types import SimpleNamespace

from control import app, sales, sales_http

PASSWORD = 'Synthetic-Pass-2026!'


def fake_handler(server, csrf_cookie, cookie_name, device=None, area=None):
    cookie = f'{cookie_name}={csrf_cookie}'
    if device:
        cookie += f'; {area}_device={device}'
    return SimpleNamespace(server=server, headers={'Cookie': cookie})


def issued_device(handler):
    header = handler.pending_cookies[0][1]
    return header.split(';', 1)[0].split('=', 1)[1]


class LoginThrottleIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'control.db')
        app.init(self.path)
        self.server = self.new_server()
        with closing(app.connect(self.path)) as db:
            company = db.execute("INSERT INTO companies(name,status) VALUES('Synthetic Co','Aktif') RETURNING id").fetchone()[0]
            db.execute("INSERT INTO users(email,name,secret,role,company_id) VALUES(?,?,?,'founder',NULL)",
                       ('founder@example.test', 'Synthetic', app.digest(PASSWORD)))
            db.execute("INSERT INTO users(email,name,secret,role,company_id) VALUES(?,?,?,'supervisor',?)",
                       ('second@example.test', 'Synthetic', app.digest(PASSWORD), company))
            db.execute('INSERT INTO sales_accounts(id,company_id,name,email,secret) VALUES(?,?,?,?,?)',
                       (secrets.token_hex(8), company, 'Synthetic Rep', 'rep@example.test', app.digest(PASSWORD)))
            db.commit()

    def new_server(self):
        # Fresh process state (as after a restart); counters must come from the database.
        return SimpleNamespace(attempts=[], login_lock=Lock(), db_path=self.path,
                               origin='https://ernasa.test', public_prefix='/control')

    def tearDown(self):
        self.tmp.cleanup()

    def control_login(self, address, password, device=None, want_handler=False):
        csrf = 'c' * 20
        handler = fake_handler(self.server, csrf, 'control_login_csrf', device, 'control')
        with closing(app.connect(self.path)) as db:
            try:
                app.Handler.login(handler, db, {'csrf': csrf, 'email': address, 'password': password})
                db.commit()
                status = 200
            except app.Problem as problem:
                db.rollback()  # the real dispatcher rolls back the request transaction on failure
                status = problem.status
        return (status, handler) if want_handler else status

    def sales_login(self, address, password):
        csrf = 's' * 20
        handler = fake_handler(self.server, csrf, 'sales_login_csrf')
        with closing(app.connect(self.path)) as db:
            try:
                sales_http.login(app, handler, db, {'csrf': csrf, 'email': address, 'password': password})
                db.commit()
                return 200
            except Exception as problem:  # sales.fail raises its own error type
                db.rollback()
                return getattr(problem, 'status', None) or getattr(problem, 'code', None) or problem.args[-1]

    def test_wrong_passwords_block_only_that_account(self):
        self.assertEqual([self.control_login('founder@example.test', 'wrong-password-x') for _ in range(3)],
                         [401, 401, 401])
        self.assertEqual(self.control_login('founder@example.test', PASSWORD), 429)
        self.assertEqual(self.control_login('second@example.test', PASSWORD), 200)

    def test_many_requests_no_longer_lock_out_everyone(self):
        # Before the fix, 12 requests of any kind within a minute blocked every CONTROL and Sales Hub user.
        for i in range(15):
            self.control_login(f'nobody{i}@example.test', 'wrong-password-x')
        self.assertEqual(self.control_login('founder@example.test', PASSWORD), 200)

    def test_sales_hub_attack_does_not_lock_control(self):
        for _ in range(6):
            self.sales_login('founder@example.test', 'wrong-password-x')
        self.assertEqual(self.control_login('founder@example.test', PASSWORD), 200)

    def test_sales_hub_throttles_its_own_accounts(self):
        for _ in range(3):
            self.assertNotEqual(self.sales_login('rep@example.test', 'wrong-password-x'), 200)
        self.assertEqual(self.sales_login('rep@example.test', PASSWORD), 429)

    def test_counters_survive_restart(self):
        for _ in range(3):
            self.control_login('founder@example.test', 'wrong-password-x')
        self.server = self.new_server()  # simulated process restart
        self.assertEqual(self.control_login('founder@example.test', PASSWORD), 429)
        with closing(app.connect(self.path)) as db:
            stored = db.execute('SELECT account_key FROM security_login_failures').fetchall()
        self.assertTrue(stored)
        self.assertNotIn('founder', repr([tuple(r) for r in stored]))

    def test_attacker_cannot_lock_owner_on_known_device(self):
        status, handler = self.control_login('founder@example.test', PASSWORD, want_handler=True)
        self.assertEqual(status, 200)
        device = issued_device(handler)
        self.assertIn('HttpOnly', handler.pending_cookies[0][1])
        self.assertIn('Secure', handler.pending_cookies[0][1])
        for _ in range(10):
            self.control_login('founder@example.test', 'wrong-password-x')
        self.assertEqual(self.control_login('founder@example.test', PASSWORD), 429)
        self.server = self.new_server()
        self.assertEqual(self.control_login('founder@example.test', PASSWORD, device), 200)

    def test_success_resets(self):
        self.control_login('founder@example.test', 'wrong-password-x')
        self.control_login('founder@example.test', 'wrong-password-x')
        self.assertEqual(self.control_login('founder@example.test', PASSWORD), 200)
        self.assertEqual(self.control_login('founder@example.test', 'wrong-password-x'), 401)
        self.assertEqual(self.control_login('founder@example.test', 'wrong-password-x'), 401)
        self.assertEqual(self.control_login('founder@example.test', PASSWORD), 200)


if __name__ == '__main__':
    unittest.main()
