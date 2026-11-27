"""Real CONTROL HTTP server (SQLite, synthetic founder): login, throttle, device trust, security screens."""
import http.client
import re
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

from control import app

PASSWORD = 'Synthetic-Pass-2026!'


class Client:
    def __init__(self, port):
        self.port, self.cookies = port, {}

    def request(self, method, path, form=None, extra_cookies=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=20)
        headers = {'Host': f'127.0.0.1:{self.port}'}
        jar = dict(self.cookies, **(extra_cookies or {}))
        if jar:
            headers['Cookie'] = '; '.join(f'{k}={v}' for k, v in jar.items())
        body = None
        if form is not None:
            body = urlencode(form)
            headers.update({'Content-Type': 'application/x-www-form-urlencoded',
                            'Origin': f'http://127.0.0.1:{self.port}'})
        started = time.time()
        conn.request(method, path, body, headers)
        response = conn.getresponse()
        text = response.read().decode('utf-8', 'replace')
        for name, value in response.getheaders():
            if name.lower() == 'set-cookie':
                key, _, rest = value.partition('=')
                self.cookies[key] = rest.split(';', 1)[0]
        conn.close()
        return response.status, text, dict(response.getheaders()), time.time() - started

    def login(self, address, password):
        _, page, _, _ = self.request('GET', '/giris')
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        return self.request('POST', '/giris', {'csrf': csrf, 'email': address, 'password': password, 'code': ''})


class HttpFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'control.db')
        app.init(self.path)
        with closing(app.connect(self.path)) as db:
            db.execute("INSERT INTO users(email,name,secret,role,company_id) VALUES(?,?,?,'founder',NULL)",
                       ('founder@example.test', 'Kurucu', app.digest(PASSWORD)))
            db.commit()
        self.httpd = app.server(self.path, 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def test_full_flow(self):
        owner = Client(self.httpd.server_port)
        status, page, headers, elapsed = owner.login('founder@example.test', PASSWORD)
        self.assertEqual(status, 303)
        self.assertLess(elapsed, 3, 'login must not wait on a database lock')
        self.assertIn('control_device', owner.cookies)
        self.assertIn('control_session', owner.cookies)

        attacker = Client(self.httpd.server_port)
        for _ in range(4):
            status, _, _, elapsed = attacker.login('founder@example.test', 'wrong-password-x')
            self.assertLess(elapsed, 3)
        self.assertEqual(status, 429)
        # A fresh browser without the device cookie is restricted ...
        self.assertEqual(Client(self.httpd.server_port).login('founder@example.test', PASSWORD)[0], 429)
        # ... but the owner's known browser is not.
        returning = Client(self.httpd.server_port)
        returning.cookies['control_device'] = owner.cookies['control_device']
        self.assertEqual(returning.login('founder@example.test', PASSWORD)[0], 303)

        status, page, headers, _ = owner.request('GET', '/guvenlik')
        self.assertEqual(status, 200)
        self.assertIn('İki adımlı doğrulama', page)
        self.assertIn('Hesap Güvenliği', page)
        status, page, _, _ = owner.request('GET', '/guvenlik-merkezi')
        self.assertEqual(status, 200)
        self.assertIn('Güvenlik bileşenleri', page)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        status, page, _, _ = Client(self.httpd.server_port).request('GET', '/sifremi-unuttum')
        self.assertEqual(status, 200)
        self.assertIn('henüz etkin değil', page)
        # Existing screens still render for the founder.
        for route in ('/', '/firmalar', '/denetim'):
            self.assertEqual(owner.request('GET', route)[0], 200, route)


if __name__ == '__main__':
    unittest.main()
