"""Server-side Cloudflare Turnstile verification (ERNASA SECURITY 2026, section 10).

The browser widget is only a hint; this check is mandatory on the server.
Reference: https://developers.cloudflare.com/turnstile/get-started/server-side-validation/

Environment:
  TURNSTILE_SECRET_KEY   secret key (never sent to the client)
  TURNSTILE_SITE_KEY     public key for the widget
  TURNSTILE_HOSTNAMES    comma-separated allowed hostnames, e.g. ernasa.com,www.ernasa.com
  TURNSTILE_MODE         off | test | enforce  (default off = keys missing, do not block users)
"""
import json
import os
import uuid
from datetime import datetime, timezone
from urllib import parse, request as urlrequest

SITEVERIFY_URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'
MAX_TOKEN_LENGTH = 2048
MAX_AGE_SECONDS = 300

# Cloudflare's published dummy keys, accepted only when TURNSTILE_MODE=test.
TEST_SECRET_KEYS = {
    '1x0000000000000000000000000000000AA',  # always passes
    '2x0000000000000000000000000000000AA',  # always fails
    '3x0000000000000000000000000000000AA',  # yields "token already spent"
}


class TurnstileConfigError(RuntimeError):
    pass


class Result:
    __slots__ = ('ok', 'reason', 'enforced')

    def __init__(self, ok, reason='', enforced=True):
        self.ok, self.reason, self.enforced = ok, reason, enforced

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return f'Result(ok={self.ok}, reason={self.reason!r}, enforced={self.enforced})'


def _post(url, fields, timeout):
    data = parse.urlencode(fields).encode()
    req = urlrequest.Request(url, data=data, headers={'Content-Type': 'application/x-www-form-urlencoded'})
    with urlrequest.urlopen(req, timeout=timeout) as response:  # nosec - fixed Cloudflare URL
        return json.loads(response.read(65536).decode('utf-8'))


class TurnstileVerifier:
    def __init__(self, secret=None, hostnames=None, mode=None, transport=_post, clock=None, timeout=5):
        self.mode = (mode or os.getenv('TURNSTILE_MODE', 'off')).strip().lower()
        if self.mode not in {'off', 'test', 'enforce'}:
            raise TurnstileConfigError('TURNSTILE_MODE must be off, test or enforce')
        self.secret = secret if secret is not None else os.getenv('TURNSTILE_SECRET_KEY', '')
        raw_hosts = hostnames if hostnames is not None else os.getenv('TURNSTILE_HOSTNAMES', '')
        self.hostnames = {h.strip().lower() for h in (raw_hosts.split(',') if isinstance(raw_hosts, str) else raw_hosts) if h.strip()}
        self.transport = transport
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.timeout = timeout
        if self.mode == 'enforce':
            if not self.secret or self.secret in TEST_SECRET_KEYS:
                raise TurnstileConfigError('enforce mode needs a production secret key')
            if not self.hostnames:
                raise TurnstileConfigError('enforce mode needs TURNSTILE_HOSTNAMES')
        if self.mode == 'test' and self.secret not in TEST_SECRET_KEYS:
            raise TurnstileConfigError('test mode accepts only Cloudflare dummy secret keys')

    def verify(self, token, action, remote_ip=None):
        """Check one widget token for one expected action (e.g. 'login', 'password_reset')."""
        if self.mode == 'off':
            return Result(True, 'disabled', enforced=False)
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH:
            return Result(False, 'missing-or-invalid-token')
        fields = {'secret': self.secret, 'response': token, 'idempotency_key': str(uuid.uuid4())}
        if remote_ip:
            fields['remoteip'] = remote_ip
        try:
            payload = self.transport(SITEVERIFY_URL, fields, self.timeout)
        except Exception:
            # Fail closed in enforce/test mode; the caller shows a retry message.
            return Result(False, 'verification-unavailable')
        if not isinstance(payload, dict) or payload.get('success') is not True:
            codes = payload.get('error-codes', []) if isinstance(payload, dict) else []
            return Result(False, ','.join(map(str, codes)) or 'rejected')
        if self.mode == 'test':
            return Result(True, 'test-key')
        if str(payload.get('hostname', '')).lower() not in self.hostnames:
            return Result(False, 'hostname-mismatch')
        if payload.get('action') != action:
            return Result(False, 'action-mismatch')
        try:
            solved = datetime.fromisoformat(str(payload.get('challenge_ts', '')).replace('Z', '+00:00'))
        except ValueError:
            return Result(False, 'bad-timestamp')
        age = (self.clock() - solved).total_seconds()
        if age < -30 or age > MAX_AGE_SECONDS:
            return Result(False, 'expired')
        return Result(True)
