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


def fake_handler(server, csrf_cookie, cookie_name):
    return SimpleNamespace(server=server, headers={'Cookie': f'{cookie_name}={csrf_cookie}'})


class LoginThrottleIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'control.db')
        app.init(self.path)
        self.server = SimpleNamespace(attempts=[], login_lock=Lock())  # legacy fields kept for comparison
        with closing(app.connect(self.path)) as db:
            company = db.execute("INSERT INTO companies(name,status) VALUES('Synthetic Co','Aktif') RETURNING id").fetchone()[0]
            db.execute("INSERT INTO users(email,name,secret,role,company_id) VALUES(?,?,?,'founder',NULL)",
                       ('founder@example.test', 'Synthetic', app.digest(PASSWORD)))
            db.execute("INSERT INTO users(email,name,secret,role,company_id) VALUES(?,?,?,'supervisor',?)",
                       ('second@example.test', 'Synthetic', app.digest(PASSWORD), company))
            db.execute('INSERT INTO sales_accounts(id,company_id,name,email,secret) VALUES(?,?,?,?,?)',
                       (secrets.token_hex(8), company, 'Synthetic Rep', 'rep@example.test', app.digest(PASSWORD)))
            db.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def control_login(self, address, password):
        csrf = 'c' * 20
        handler = fake_handler(self.server, csrf, 'control_login_csrf')
        with closing(app.connect(self.path)) as db:
            try:
                app.Handler.login(handler, db, {'csrf': csrf, 'email': address, 'password': password})
                db.commit()
                return 200
            except app.Problem as problem:
                return problem.status

    def sales_login(self, address, password):
        csrf = 's' * 20
        handler = fake_handler(self.server, csrf, 'sales_login_csrf')
        with closing(app.connect(self.path)) as db:
            try:
                sales_http.login(app, handler, db, {'csrf': csrf, 'email': address, 'password': password})
                db.commit()
                return 200
            except Exception as problem:  # sales.fail raises its own error type
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

    def test_success_resets(self):
        self.control_login('founder@example.test', 'wrong-password-x')
        self.control_login('founder@example.test', 'wrong-password-x')
        self.assertEqual(self.control_login('founder@example.test', PASSWORD), 200)
        self.assertEqual(self.control_login('founder@example.test', 'wrong-password-x'), 401)
        self.assertEqual(self.control_login('founder@example.test', 'wrong-password-x'), 401)
        self.assertEqual(self.control_login('founder@example.test', PASSWORD), 200)


if __name__ == '__main__':
    unittest.main()
