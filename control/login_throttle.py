"""Account-based, progressive login throttling for CONTROL and Sales Hub.

Replaces the former single shared list (12 attempts/minute for *all* users of both
CONTROL and Sales Hub), which let anyone lock the founder out with 12 requests a minute.

Policy (ERNASA SECURITY 2026, section 11):
  failures 1-2  -> normal error (2nd failure marks the account for a bot challenge
                   once Turnstile is activated: ``needs_challenge``)
  failures 3-4  -> 5 minute restriction
  failures 5-6  -> 15 minute restriction
  failures >= 7 -> risk-based increase: 30, 60 minutes (capped at 60)
A successful login clears the account's counter. Failures older than RESET_AFTER
seconds without a new failure are forgotten.

The key is the normalised e-mail address, so attacking one account never blocks
another. A high global ceiling remains only as a CPU-exhaustion backstop for scrypt.

State is in memory (single Render instance). Persisting it across restarts needs a
table/migration and is deliberately left for coordination with parallel work.
"""
import threading
import time

RESET_AFTER = 30 * 60
GLOBAL_WINDOW = 60
GLOBAL_LIMIT = 120
MAX_KEYS = 10000


def restriction_seconds(failures):
    if failures < 3:
        return 0
    if failures < 5:
        return 5 * 60
    if failures < 7:
        return 15 * 60
    return min(60 * 60, 30 * 60 * 2 ** ((failures - 7) // 2))


class LoginThrottle:
    def __init__(self, clock=time.time, global_limit=GLOBAL_LIMIT):
        self._clock = clock
        self._lock = threading.Lock()
        self._accounts = {}  # key -> [failures, last_failure, blocked_until]
        self._recent = []
        self._global_limit = global_limit

    @staticmethod
    def key(address):
        return str(address or '').strip().lower()[:254]

    def check(self, address):
        """Return seconds the caller must wait (0 = may try). Counts the attempt globally."""
        now = self._clock()
        with self._lock:
            self._recent = [t for t in self._recent if t > now - GLOBAL_WINDOW]
            if len(self._recent) >= self._global_limit:
                return GLOBAL_WINDOW
            self._recent.append(now)
            entry = self._entry(self.key(address), now)
            if entry and entry[2] > now:
                return int(entry[2] - now) + 1
            return 0

    def failure(self, address):
        now = self._clock()
        with self._lock:
            key = self.key(address)
            entry = self._entry(key, now) or [0, now, 0]
            entry[0] += 1
            entry[1] = now
            wait = restriction_seconds(entry[0])
            entry[2] = now + wait if wait else 0
            self._accounts[key] = entry
            self._prune(now)
            return wait

    def success(self, address):
        with self._lock:
            self._accounts.pop(self.key(address), None)

    def needs_challenge(self, address):
        with self._lock:
            entry = self._entry(self.key(address), self._clock())
            return bool(entry and entry[0] >= 2)

    def _entry(self, key, now):
        entry = self._accounts.get(key)
        if entry and entry[2] <= now and now - entry[1] > RESET_AFTER:
            del self._accounts[key]
            return None
        return entry

    def _prune(self, now):
        if len(self._accounts) <= MAX_KEYS:
            return
        for key in [k for k, v in self._accounts.items() if v[2] <= now]:
            del self._accounts[key]
        if len(self._accounts) > MAX_KEYS:  # still full: drop oldest
            for key, _ in sorted(self._accounts.items(), key=lambda kv: kv[1][1])[:len(self._accounts) - MAX_KEYS]:
                del self._accounts[key]


def wait_message(seconds):
    minutes = max(1, (seconds + 59) // 60)
    return f'Çok fazla hatalı giriş denemesi. {minutes} dakika sonra tekrar deneyin.'


_attach_lock = threading.Lock()


def for_server(server, area):
    """Return the throttle of one login area ('control' or 'sales'); areas never share counters."""
    name = f'{area}_login_throttle'
    with _attach_lock:
        throttle = getattr(server, name, None)
        if throttle is None:
            throttle = LoginThrottle()
            setattr(server, name, throttle)
        return throttle
