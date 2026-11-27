"""Account-based, progressive, persistent login throttling for CONTROL and Sales Hub.

Policy (ERNASA SECURITY 2026, section 11):
  failures 1-2  -> normal error (2nd failure marks the account for a bot challenge: ``needs_challenge``)
  failures 3-4  -> 5 minute restriction
  failures 5-6  -> 15 minute restriction
  failures >= 7 -> risk-based increase: 30, 60 minutes (capped at 60)
A successful login clears the counter of the bucket it came from. Failures older than
RESET_AFTER seconds without a new failure are forgotten.

Deliberate lock-out protection (OWASP "device cookies"): after a successful login the
browser receives a random device token; only its SHA-256 is stored. Attempts that present
a valid device token for the account are counted in that device's own bucket, so an
attacker who hammers an e-mail address only restricts the shared "untrusted" bucket and
never blocks the owner's known devices.

Persistence: counters live in ``security_login_failures`` and ``security_trusted_devices``
(created idempotently on first use; no numbered migration, so no ordering conflict with
parallel work). Writes use their own connection and commit, because the login request's
own transaction is rolled back on failure. If the database is unreachable the throttle
falls back to in-memory counters instead of blocking every login.

Stored identifiers are HMAC-SHA256(key, e-mail) — the address itself is never written.
"""
import hashlib
import hmac
import os
import secrets
import sys
import threading
import time

RESET_AFTER = 30 * 60
GLOBAL_WINDOW = 60
GLOBAL_LIMIT = 120
MAX_KEYS = 10000
DEVICE_TTL = 90 * 24 * 3600
MAX_DEVICES_PER_ACCOUNT = 10
UNTRUSTED = 'untrusted'
DEGRADED_SECONDS = 300

DDL = (
    """CREATE TABLE IF NOT EXISTS security_login_failures(
        area TEXT NOT NULL, account_key TEXT NOT NULL, bucket TEXT NOT NULL,
        failures INTEGER NOT NULL, last_failure INTEGER NOT NULL, blocked_until INTEGER NOT NULL,
        PRIMARY KEY(area, account_key, bucket))""",
    """CREATE TABLE IF NOT EXISTS security_trusted_devices(
        area TEXT NOT NULL, account_key TEXT NOT NULL, device_hash TEXT NOT NULL,
        created INTEGER NOT NULL, last_seen INTEGER NOT NULL, expires INTEGER NOT NULL,
        PRIMARY KEY(area, account_key, device_hash))""",
)


def restriction_seconds(failures):
    if failures < 3:
        return 0
    if failures < 5:
        return 5 * 60
    if failures < 7:
        return 15 * 60
    return min(60 * 60, 30 * 60 * 2 ** ((failures - 7) // 2))


def _secret():
    key = os.getenv('ERNASA_LOGIN_THROTTLE_KEY') or os.getenv('ERNASA_IK_CONTROL_KEY') or ''
    return key.encode() if len(key) >= 32 else b'ernasa-local-development-throttle-key'


def account_key(address):
    normal = str(address or '').strip().lower()[:254]
    return hmac.new(_secret(), normal.encode(), hashlib.sha256).hexdigest()


def device_hash(token):
    return hashlib.sha256(str(token).encode()).hexdigest()


class MemoryStore:
    def __init__(self):
        self.failures = {}  # (area, key, bucket) -> [failures, last_failure, blocked_until]
        self.devices = {}   # (area, key, hash) -> [created, last_seen, expires]

    def get(self, area, key, bucket):
        return self.failures.get((area, key, bucket))

    def put(self, area, key, bucket, entry):
        self.failures[(area, key, bucket)] = list(entry)
        if len(self.failures) > MAX_KEYS:
            for k, _ in sorted(self.failures.items(), key=lambda kv: kv[1][1])[:len(self.failures) - MAX_KEYS]:
                del self.failures[k]

    def delete(self, area, key, bucket):
        self.failures.pop((area, key, bucket), None)

    def device_valid(self, area, key, dhash, now):
        row = self.devices.get((area, key, dhash))
        return bool(row and row[2] > now)

    def remember_device(self, area, key, dhash, now):
        self.devices[(area, key, dhash)] = [now, now, now + DEVICE_TTL]
        mine = sorted((k for k in self.devices if k[0] == area and k[1] == key),
                      key=lambda k: self.devices[k][1], reverse=True)
        for k in mine[MAX_DEVICES_PER_ACCOUNT:]:
            del self.devices[k]

    def forget_devices(self, area, key):
        for k in [k for k in self.devices if k[0] == area and k[1] == key]:
            del self.devices[k]


class DbStore:
    """Same interface, backed by the CONTROL database (SQLite locally, PostgreSQL in production)."""

    def __init__(self, connect):
        self.connect = connect
        self._ready = False
        self._lock = threading.Lock()

    def _run(self, sql, params=(), fetch=False):
        db = self.connect()
        try:
            if not self._ready:
                with self._lock:
                    for statement in DDL:
                        db.execute(statement)
                    self._ready = True
            cursor = db.execute(sql, params)
            row = cursor.fetchone() if fetch else None
            db.commit()
            return row
        finally:
            db.close()

    def get(self, area, key, bucket):
        row = self._run('SELECT failures,last_failure,blocked_until FROM security_login_failures '
                        'WHERE area=? AND account_key=? AND bucket=?', (area, key, bucket), fetch=True)
        return [row[0], row[1], row[2]] if row else None

    def put(self, area, key, bucket, entry):
        self._run('INSERT INTO security_login_failures(area,account_key,bucket,failures,last_failure,blocked_until) '
                  'VALUES(?,?,?,?,?,?) ON CONFLICT(area,account_key,bucket) DO UPDATE SET '
                  'failures=excluded.failures,last_failure=excluded.last_failure,blocked_until=excluded.blocked_until',
                  (area, key, bucket, int(entry[0]), int(entry[1]), int(entry[2])))

    def delete(self, area, key, bucket):
        self._run('DELETE FROM security_login_failures WHERE area=? AND account_key=? AND bucket=?',
                  (area, key, bucket))

    def device_valid(self, area, key, dhash, now):
        return self._run('SELECT 1 FROM security_trusted_devices WHERE area=? AND account_key=? '
                         'AND device_hash=? AND expires>?', (area, key, dhash, int(now)), fetch=True) is not None

    def remember_device(self, area, key, dhash, now):
        now = int(now)
        self._run('INSERT INTO security_trusted_devices(area,account_key,device_hash,created,last_seen,expires) '
                  'VALUES(?,?,?,?,?,?) ON CONFLICT(area,account_key,device_hash) DO UPDATE SET '
                  'last_seen=excluded.last_seen,expires=excluded.expires',
                  (area, key, dhash, now, now, now + DEVICE_TTL))
        self._run('DELETE FROM security_trusted_devices WHERE area=? AND account_key=? AND device_hash NOT IN '
                  '(SELECT device_hash FROM security_trusted_devices WHERE area=? AND account_key=? '
                  'ORDER BY last_seen DESC LIMIT ?)', (area, key, area, key, MAX_DEVICES_PER_ACCOUNT))

    def forget_devices(self, area, key):
        self._run('DELETE FROM security_trusted_devices WHERE area=? AND account_key=?', (area, key))


class LoginThrottle:
    def __init__(self, area='control', store=None, clock=time.time, global_limit=GLOBAL_LIMIT):
        self.area = area
        self.store = store or MemoryStore()
        self._fallback = MemoryStore()
        self._clock = clock
        self._lock = threading.Lock()
        self._recent = []
        self._global_limit = global_limit
        self._degraded_until = 0

    # -- storage with fail-open-to-memory (never fail-closed for every user) --
    def _call(self, name, *args):
        # After a storage error, use process memory consistently for a while instead of mixing
        # database reads with in-memory writes (which would silently stop counting).
        if self._clock() < self._degraded_until:
            return getattr(self._fallback, name)(*args)
        try:
            return getattr(self.store, name)(*args)
        except Exception as exc:  # database unavailable or locked: degrade, log without data
            self._degraded_until = self._clock() + DEGRADED_SECONDS
            print(f'LOGIN_THROTTLE_STORE_ERROR area={self.area} op={name} type={type(exc).__name__}',
                  file=sys.stderr, flush=True)
            return getattr(self._fallback, name)(*args)

    def _bucket(self, key, device_token, now):
        if device_token and self._call('device_valid', self.area, key, device_hash(device_token), now):
            return 'device:' + device_hash(device_token)[:32]
        return UNTRUSTED

    def _entry(self, key, bucket, now):
        entry = self._call('get', self.area, key, bucket)
        if entry and entry[2] <= now and now - entry[1] > RESET_AFTER:
            self._call('delete', self.area, key, bucket)
            return None
        return entry

    @staticmethod
    def key(address):
        return account_key(address)

    def check(self, address, device_token=None):
        """Seconds the caller must wait (0 = may try). Counts the attempt in the global backstop."""
        now = self._clock()
        with self._lock:
            self._recent = [t for t in self._recent if t > now - GLOBAL_WINDOW]
            if len(self._recent) >= self._global_limit:
                return GLOBAL_WINDOW
            self._recent.append(now)
        key = account_key(address)
        entry = self._entry(key, self._bucket(key, device_token, now), now)
        if entry and entry[2] > now:
            return int(entry[2] - now) + 1
        return 0

    def failure(self, address, device_token=None):
        now = self._clock()
        key = account_key(address)
        bucket = self._bucket(key, device_token, now)
        with self._lock:
            entry = self._entry(key, bucket, now) or [0, now, 0]
            entry[0] += 1
            entry[1] = now
            wait = restriction_seconds(entry[0])
            entry[2] = now + wait if wait else 0
            self._call('put', self.area, key, bucket, entry)
            return wait

    def success(self, address, device_token=None):
        """Clear the bucket used and (re)issue a trusted-device token for this browser."""
        now = self._clock()
        key = account_key(address)
        bucket = self._bucket(key, device_token, now)
        self._call('delete', self.area, key, bucket)
        token = device_token if bucket != UNTRUSTED else secrets.token_urlsafe(32)
        self._call('remember_device', self.area, key, device_hash(token), now)
        return token

    def needs_challenge(self, address, device_token=None):
        now = self._clock()
        key = account_key(address)
        entry = self._entry(key, self._bucket(key, device_token, now), now)
        return bool(entry and entry[0] >= 2)

    def forget_devices(self, address):
        """Call after a password change or reset: old browsers lose their trusted status."""
        self._call('forget_devices', self.area, account_key(address))


_attach_lock = threading.Lock()


def _connect(target):
    """Separate connection for counters. SQLite (local/dev) holds a database-wide write lock during a
    request, so wait only briefly there and fall back to memory; PostgreSQL uses row locks."""
    from . import app
    db = app.connect(target)
    if not hasattr(db, 'raw'):
        db.execute('PRAGMA busy_timeout=200')
    return db


def for_server(server, area):
    """Return the throttle of one login area ('control' or 'sales'); areas never share counters.

    Uses the server's CONTROL database when ``server.db_path`` exists, else process memory.
    """
    name = f'{area}_login_throttle'
    with _attach_lock:
        throttle = getattr(server, name, None)
        if throttle is None:
            target = getattr(server, 'db_path', None)
            store = None
            if target is not None:
                store = DbStore(lambda: _connect(target))
            throttle = LoginThrottle(area, store)
            setattr(server, name, throttle)
        return throttle


def device_cookie_name(area):
    return f'{area}_device'


def device_cookie(area, token, path, secure):
    flag = '; Secure' if secure else ''
    return ('Set-Cookie', f'{device_cookie_name(area)}={token}; Path={path}; HttpOnly; '
                          f'SameSite=Strict; Max-Age={DEVICE_TTL}{flag}')


def wait_message(seconds):
    minutes = max(1, (seconds + 59) // 60)
    return f'Çok fazla hatalı giriş denemesi. {minutes} dakika sonra tekrar deneyin.'
