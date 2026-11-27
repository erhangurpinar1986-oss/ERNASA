"""One-time codes for SMS verification and password reset (ERNASA SECURITY 2026, section 9).

Policy:
  * 6 digits from ``secrets`` (CSPRNG)
  * valid 5 minutes, single use, max 3 wrong attempts
  * 60 s resend cooldown; a new code invalidates the previous one
  * per-subject (account), per-phone and per-IP hourly/daily send limits
  * only HMAC-SHA256(pepper, purpose|subject|code) is stored; codes never logged

``OtpStore`` is an interface; ``MemoryOtpStore`` serves tests and a single instance.
Production storage (PostgreSQL table) is added with the activation migration.

Environment: OTP_PEPPER (>= 32 random bytes, base64 or hex; required outside tests).
"""
import hashlib
import hmac
import os
import secrets
import threading
import time

CODE_DIGITS = 6
TTL_SECONDS = 300
MAX_ATTEMPTS = 3
RESEND_COOLDOWN = 60
LIMITS = {  # (window seconds, max sends)
    'subject': ((3600, 5), (86400, 10)),
    'phone': ((3600, 5), (86400, 10)),
    'ip': ((3600, 20), (86400, 100)),
}


class OtpError(Exception):
    """Generic failure. ``reason`` is for audit logs, never shown verbatim to the user."""

    def __init__(self, reason, retry_after=0):
        super().__init__(reason)
        self.reason, self.retry_after = reason, retry_after


class MemoryOtpStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = {}    # (purpose, subject) -> {digest, expires, attempts, issued}
        self.sends = {}     # (kind, value) -> [timestamps]


class OtpService:
    def __init__(self, store=None, pepper=None, clock=time.time):
        pepper = pepper if pepper is not None else os.getenv('OTP_PEPPER', '')
        if isinstance(pepper, str):
            pepper = pepper.encode()
        if len(pepper) < 32:
            raise ValueError('OTP_PEPPER must be at least 32 bytes')
        self.pepper, self.store, self.clock = pepper, store or MemoryOtpStore(), clock

    def _digest(self, purpose, subject, code):
        return hmac.new(self.pepper, f'{purpose}|{subject}|{code}'.encode(), hashlib.sha256).hexdigest()

    def issue(self, purpose, subject, phone, ip):
        """Create a code. Returns (code, expires_at). Caller sends it by SMS and discards it."""
        now = self.clock()
        s = self.store
        with s.lock:
            current = s.active.get((purpose, subject))
            if current and now - current['issued'] < RESEND_COOLDOWN:
                raise OtpError('cooldown', int(RESEND_COOLDOWN - (now - current['issued'])) + 1)
            keys = [('subject', subject), ('phone', phone), ('ip', ip or 'unknown')]
            for kind, value in keys:
                stamps = [t for t in s.sends.get((kind, value), []) if t > now - 86400]
                s.sends[(kind, value)] = stamps
                for window, maximum in LIMITS[kind]:
                    recent = [t for t in stamps if t > now - window]
                    if len(recent) >= maximum:
                        raise OtpError(f'{kind}_limit', int(recent[0] + window - now) + 1)
            for kind, value in keys:
                s.sends[(kind, value)].append(now)
            code = f'{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}'
            s.active[(purpose, subject)] = {'digest': self._digest(purpose, subject, code),
                                            'expires': now + TTL_SECONDS, 'attempts': 0, 'issued': now}
            return code, now + TTL_SECONDS

    def verify(self, purpose, subject, code):
        """True once for the right code; afterwards the code is gone. Raises OtpError otherwise."""
        now = self.clock()
        s = self.store
        with s.lock:
            entry = s.active.get((purpose, subject))
            if not entry:
                raise OtpError('no_code')
            if now >= entry['expires']:
                del s.active[(purpose, subject)]
                raise OtpError('expired')
            candidate = str(code or '').strip()
            ok = (len(candidate) == CODE_DIGITS and candidate.isdigit()
                  and hmac.compare_digest(entry['digest'], self._digest(purpose, subject, candidate)))
            if ok:
                del s.active[(purpose, subject)]
                return True
            entry['attempts'] += 1
            if entry['attempts'] >= MAX_ATTEMPTS:
                del s.active[(purpose, subject)]
                raise OtpError('too_many_attempts')
            raise OtpError('wrong_code')
