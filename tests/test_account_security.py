"""2FA, password reset, Turnstile gate and Security Center (SQLite, synthetic users, no network, no real e-mail)."""
import os
import sqlite3
import time
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from types import SimpleNamespace
from unittest import mock

from auth_security import turnstile as turnstile_module
from control import account_security as sec
from control import app, login_throttle

PASSWORD = 'Synthetic-Pass-2026!'
NEW_PASSWORD = 'Another-Synthetic-2026!'


class Headers(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class FakeHandler:
    def __init__(self, server, cookie='', path='/'):
        self.server, self.headers, self.path = server, Headers({'Cookie': cookie}), path
        self.pages = []

    def page(self, title, content, actor=None, status=200, path='', brand='', headers=()):
        self.pages.append(SimpleNamespace(title=title, body=content, status=status, headers=headers))
        return self.pages[-1]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'control.db')
        app.init(self.path)
        sec._schema_ready.clear()
        self.mail = []
        self.server = SimpleNamespace(attempts=[], login_lock=Lock(), db_path=self.path, origin='https://ernasa.test',
                                      public_prefix='/control', security_mail=lambda *a: self.mail.append(a) or 'id')
        # Deterministic counters here; database persistence is covered by the throttle and HTTP tests.
        self.server.control_login_throttle = login_throttle.LoginThrottle('control')
        with closing(app.connect(self.path)) as db:
            self.company = db.execute("INSERT INTO companies(name,status) VALUES('Synthetic Co','Aktif') RETURNING id").fetchone()[0]
            self.founder = db.execute("INSERT INTO users(email,name,secret,role,company_id) VALUES(?,?,?,'founder',NULL) RETURNING id",
                                      ('founder@example.test', 'Kurucu', app.digest(PASSWORD))).fetchone()[0]
            self.manager = db.execute("INSERT INTO users(email,name,secret,role,company_id) VALUES(?,?,?,'supervisor',?) RETURNING id",
                                      ('manager@example.test', 'Yönetici', app.digest(PASSWORD), self.company)).fetchone()[0]
            db.commit()
        self.env = mock.patch.dict(os.environ, {'TURNSTILE_MODE': 'off'}, clear=False)
        self.env.start()
        os.environ.pop('RENDER', None)
        os.environ.pop('SECURITY_PASSWORD_RESET', None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def actor(self, uid, role):
        with closing(app.connect(self.path)) as db:
            row = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
            token = 'session-' + str(uid)
            db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?,?)', (token, uid, 'csrf', int(time.time()) + 3600))
            db.commit()
        return dict(row) | {'csrf': 'csrf', 'token': token, 'role': role}

    def account_action(self, actor, action, **data):
        handler = FakeHandler(self.server)
        with closing(app.connect(self.path)) as db:
            try:
                html = sec.handle_page(handler, db, actor, '/guvenlik', True, {'action': action, **data}, {})
                db.commit()
                return html
            except app.Problem as problem:
                db.rollback()
                return problem

    def login(self, address, password, code=''):
        handler = FakeHandler(self.server, 'control_login_csrf=' + 'c' * 20)
        with closing(app.connect(self.path)) as db:
            try:
                app.Handler.login(handler, db, {'csrf': 'c' * 20, 'email': address, 'password': password, 'code': code})
                db.commit()
                return 200, ''
            except app.Problem as problem:
                db.rollback()
                return problem.status, str(problem)

    def enroll(self, uid, role):
        actor = self.actor(uid, role)
        self.account_action(actor, 'totp_start')
        with closing(app.connect(self.path)) as db:
            secret = sec._decrypt(sec.totp_row(db, 'control', uid)[0])
        html = self.account_action(actor, 'totp_confirm', code=sec.totp_at(secret, int(time.time() // 30)))
        codes = [line.split('<code>')[1].split('</code>')[0] for line in html.split('<li>')[1:]]
        return actor, secret, codes


class TotpTests(Base):
    def test_rfc6238_vector(self):
        secret = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'  # "12345678901234567890"
        self.assertEqual(sec.totp_at(secret, 1), '287082')  # RFC 6238 T=59s, last 6 digits of 94287082
        self.assertEqual(sec.match_totp(secret, '287082', 0, now=59), 1)
        self.assertIsNone(sec.match_totp(secret, '287082', 1, now=59))  # replay of the same step

    def test_secret_is_encrypted_at_rest(self):
        actor = self.actor(self.founder, 'founder')
        self.account_action(actor, 'totp_start')
        with closing(app.connect(self.path)) as db:
            stored = sec.totp_row(db, 'control', self.founder)[0]
            secret = sec._decrypt(stored)
        self.assertNotIn(secret, stored)
        self.assertRegex(secret, r'^[A-Z2-7]{32}$')

    def test_enrolment_needs_a_valid_code_and_gives_recovery_codes(self):
        actor = self.actor(self.founder, 'founder')
        self.account_action(actor, 'totp_start')
        self.assertIsInstance(self.account_action(actor, 'totp_confirm', code='000000'), app.Problem)
        _, _, codes = self.enroll(self.founder, 'founder')
        self.assertEqual(len(codes), 10)
        with closing(app.connect(self.path)) as db:
            stored = repr(db.execute('SELECT code_hash FROM security_recovery_codes').fetchall())
        self.assertNotIn(codes[0], stored)

    def test_login_requires_second_factor_only_when_enrolled(self):
        self.assertEqual(self.login('manager@example.test', PASSWORD)[0], 200)
        _, secret, codes = self.enroll(self.founder, 'founder')
        status, message = self.login('founder@example.test', PASSWORD)
        self.assertEqual(status, 401)
        self.assertIn('iki adımlı', message)
        self.assertEqual(self.login('founder@example.test', PASSWORD, '123456' if sec.totp_at(secret, int(time.time() // 30)) != '123456' else '654321')[0], 401)
        good = sec.totp_at(secret, int(time.time() // 30) + 1)
        self.assertEqual(self.login('founder@example.test', PASSWORD, good)[0], 200)
        self.assertEqual(self.login('founder@example.test', PASSWORD, good)[0], 401)  # replay refused
        self.assertEqual(self.login('founder@example.test', PASSWORD, codes[0])[0], 200)  # recovery code
        self.assertEqual(self.login('founder@example.test', PASSWORD, codes[0])[0], 401)  # used once
        self.assertEqual(self.login('founder@example.test', 'wrong-password', good)[0], 401)

    def test_wrong_codes_are_throttled(self):
        _, secret, _ = self.enroll(self.founder, 'founder')
        current = sec.totp_at(secret, int(time.time() // 30))
        wrong = '000000' if current != '000000' else '111111'
        for _ in range(3):
            self.assertEqual(self.login('founder@example.test', PASSWORD, wrong)[0], 401)
        good = sec.totp_at(secret, int(time.time() // 30) + 1)
        self.assertEqual(self.login('founder@example.test', PASSWORD, good)[0], 429)  # no TOTP brute force


    def test_disable_needs_password_and_code(self):
        actor, secret, codes = self.enroll(self.manager, 'supervisor')
        self.assertIsInstance(self.account_action(actor, 'totp_disable', password='wrong', code=codes[1]), app.Problem)
        self.account_action(actor, 'totp_disable', password=PASSWORD, code=codes[1])
        self.assertEqual(self.login('manager@example.test', PASSWORD)[0], 200)

    def test_revoke_other_sessions_keeps_current(self):
        actor = self.actor(self.manager, 'supervisor')
        with closing(app.connect(self.path)) as db:
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)', ('other-device', self.manager, 'x', int(time.time()) + 3600))
            db.commit()
        self.account_action(actor, 'sessions_revoke_others')
        with closing(app.connect(self.path)) as db:
            tokens = [r[0] for r in db.execute('SELECT token FROM sessions WHERE user_id=?', (self.manager,))]
        self.assertEqual(tokens, [actor['token']])

    def test_phone_actions_refused_without_sms_provider(self):
        actor = self.actor(self.manager, 'supervisor')
        result = self.account_action(actor, 'phone_save', phone='+905321234567')
        self.assertIsInstance(result, app.Problem)
        self.assertEqual(result.status, 503)


class ResetTests(Base):
    def public(self, path, post=False, data=None, query=''):
        handler = FakeHandler(self.server, 'security_reset_csrf=tok', path=path + query)
        with closing(app.connect(self.path)) as db:
            try:
                page = sec.handle_public(handler, db, path, post, data)
                db.commit()
                return page
            except app.Problem as problem:
                db.rollback()
                return problem

    def request_reset(self, address, area='control'):
        return self.public('/sifremi-unuttum', True, {'csrf': 'tok', 'email': address, 'alan': area})

    def link_token(self):
        return self.mail[-1][2].split('?t=')[1].split()[0]

    def test_disabled_by_default(self):
        page = self.request_reset('manager@example.test')
        self.assertIn('henüz etkin değil', page.body)
        self.assertEqual(self.mail, [])

    def test_uniform_response_and_single_use_link(self):
        with mock.patch.dict(os.environ, {'SECURITY_PASSWORD_RESET': 'on'}):
            unknown = self.request_reset('nobody@example.test')
            known = self.request_reset('manager@example.test')
            self.assertEqual(unknown.body, known.body)
            self.assertEqual(len(self.mail), 1)
            token = self.link_token()
            form = self.public('/sifre-sifirla', query='?t=' + token)
            self.assertIn('Yeni parola', form.body)
            done = self.public('/sifre-sifirla', True, {'csrf': 'tok', 't': token, 'password': NEW_PASSWORD})
            self.assertIn('Parolanız değiştirildi', done.body)
            self.assertEqual(self.mail[-1][1], 'ERNASA parolanız değiştirildi')
            again = self.public('/sifre-sifirla', True, {'csrf': 'tok', 't': token, 'password': NEW_PASSWORD})
            self.assertEqual(getattr(again, 'status', None), 410)
        self.assertEqual(self.login('manager@example.test', NEW_PASSWORD)[0], 200)
        self.assertEqual(self.login('manager@example.test', PASSWORD)[0], 401)

    def test_reset_closes_sessions_and_rejects_bad_csrf(self):
        self.actor(self.manager, 'supervisor')
        with mock.patch.dict(os.environ, {'SECURITY_PASSWORD_RESET': 'on'}):
            self.request_reset('manager@example.test')
            token = self.link_token()
            bad = self.public('/sifre-sifirla', True, {'csrf': 'wrong', 't': token, 'password': NEW_PASSWORD})
            self.assertEqual(bad.status, 403)
            self.public('/sifre-sifirla', True, {'csrf': 'tok', 't': token, 'password': NEW_PASSWORD})
        with closing(app.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions WHERE user_id=?', (self.manager,)).fetchone()[0], 0)

    def test_founder_needs_authenticator(self):
        with mock.patch.dict(os.environ, {'SECURITY_PASSWORD_RESET': 'on'}):
            self.request_reset('founder@example.test')
            self.assertEqual(self.mail, [])  # refused silently without 2FA
            _, secret, codes = self.enroll(self.founder, 'founder')
            self.request_reset('founder@example.test')
            token = self.link_token()
            self.assertIn('Doğrulama uygulaması', self.public('/sifre-sifirla', query='?t=' + token).body)
            refused = self.public('/sifre-sifirla', True, {'csrf': 'tok', 't': token, 'password': NEW_PASSWORD, 'code': '000000'})
            self.assertEqual(refused.status, 401)
            self.public('/sifre-sifirla', True, {'csrf': 'tok', 't': token, 'password': NEW_PASSWORD, 'code': codes[0]})
        self.assertEqual(self.login('founder@example.test', NEW_PASSWORD, codes[1])[0], 200)

    def test_per_account_rate_limit(self):
        with mock.patch.dict(os.environ, {'SECURITY_PASSWORD_RESET': 'on'}):
            for _ in range(5):
                self.request_reset('manager@example.test')
        self.assertEqual(len(self.mail), sec.RESET_PER_ACCOUNT_HOUR)

    def test_expired_link(self):
        with mock.patch.dict(os.environ, {'SECURITY_PASSWORD_RESET': 'on'}):
            self.request_reset('manager@example.test')
            token = self.link_token()
            with closing(app.connect(self.path)) as db:
                db.execute('UPDATE security_password_resets SET expires=0')
                db.commit()
            self.assertEqual(self.public('/sifre-sifirla', query='?t=' + token).status, 410)


class TurnstileGateTests(Base):
    def test_challenge_required_after_two_failures(self):
        calls = []

        def verifier():
            def post(url, fields, timeout):
                calls.append(fields)
                return {'success': fields['response'] == 'good-token', 'hostname': 'ernasa.test', 'action': 'login',
                        'challenge_ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
            return turnstile_module.TurnstileVerifier(secret='0xPRODUCTIONKEY', hostnames='ernasa.test', mode='enforce',
                                                      transport=post)
        with mock.patch.object(sec, 'turnstile', verifier):
            self.assertEqual(self.login('manager@example.test', 'wrong-1')[0], 401)
            self.assertEqual(self.login('manager@example.test', 'wrong-2')[0], 401)
            self.assertEqual(calls, [])  # no challenge before the second failure
            self.assertEqual(self.login('manager@example.test', PASSWORD)[0], 403)
            handler = FakeHandler(self.server, 'control_login_csrf=' + 'c' * 20)
            with closing(app.connect(self.path)) as db:
                app.Handler.login(handler, db, {'csrf': 'c' * 20, 'email': 'manager@example.test', 'password': PASSWORD,
                                                'cf-turnstile-response': 'good-token'})

    def test_csp_allows_only_cloudflare_when_widget_shown(self):
        policy = sec.turnstile_csp()
        self.assertIn('https://challenges.cloudflare.com', policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertEqual(sec.turnstile_widget('login'), '')  # mode off: no third-party script at all


class SecurityCenterTests(Base):
    def test_founder_only_and_no_secrets(self):
        _, secret, codes = self.enroll(self.founder, 'founder')
        handler = FakeHandler(self.server)
        with closing(app.connect(self.path)) as db:
            html = sec.handle_page(handler, db, self.actor(self.founder, 'founder'), '/guvenlik-merkezi', False, None, {})
            with self.assertRaises(app.Problem):
                sec.handle_page(handler, db, self.actor(self.manager, 'supervisor'), '/guvenlik-merkezi', False, None, {})
        self.assertIn('2FA açıldı', html)
        self.assertIn('Kurucu', html)
        self.assertNotIn(secret, html)
        self.assertNotIn(codes[0], html)

    def test_events_are_append_only(self):
        self.enroll(self.founder, 'founder')
        with closing(app.connect(self.path)) as db:
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("UPDATE security_events SET kind='x'")
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute('DELETE FROM security_events')

    def test_menu_keeps_existing_order(self):
        keys = list(app.NAV)
        self.assertEqual(keys[-2:], ['/guvenlik', '/guvenlik-merkezi'])
        self.assertIn('/guvenlik-merkezi', app.ACCESS['founder'])
        self.assertNotIn('/guvenlik-merkezi', app.ACCESS['supervisor'])
        self.assertIn('/guvenlik', app.ACCESS['staff'])


if __name__ == '__main__':
    unittest.main()
