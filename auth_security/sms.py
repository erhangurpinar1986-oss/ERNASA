"""Provider-independent SMS layer (ERNASA SECURITY 2026, section 9).

Application code depends only on ``SmsGateway.send(to, text, purpose)``. Changing
provider (Netgsm, İleti Merkezi, Mutlucell, Twilio, Vonage) means adding one adapter
class; nothing else is rewritten.

Environment:
  SMS_PROVIDER         test (default) | <adapter name once implemented>
  SMS_DAILY_LIMIT      max messages per day across the platform (default 500)
  SMS_DAILY_COST_LIMIT_MINOR   max estimated spend per day in kuruş (default 50000 = 500 TL)
  SMS_UNIT_COST_MINOR  estimated cost of one SMS in kuruş (default 50)

Real adapters are deliberately not included: each needs a provider account, an
approved sender header and a verified API contract before code is written against it.
"""
import os
import threading
import time
from datetime import datetime, timezone

from .phone import E164, mask


class SmsError(RuntimeError):
    pass


class SmsLimitError(SmsError):
    pass


class SmsProvider:
    name = 'abstract'

    def send(self, to, text):
        """Send one message; return provider message id. Raise SmsError on failure."""
        raise NotImplementedError


class TestSmsProvider(SmsProvider):
    """Delivers nothing. Keeps only metadata so tests can assert behaviour; never stores text."""
    name = 'test'

    def __init__(self):
        self.sent = []

    def send(self, to, text):
        self.sent.append({'to': to, 'length': len(text), 'at': time.time()})
        return f'test-{len(self.sent)}'


class SmsGateway:
    def __init__(self, provider, daily_limit=None, daily_cost_limit_minor=None, unit_cost_minor=None,
                 audit=None, clock=time.time):
        self.provider = provider
        self.daily_limit = int(daily_limit if daily_limit is not None else os.getenv('SMS_DAILY_LIMIT', '500'))
        self.daily_cost_limit = int(daily_cost_limit_minor if daily_cost_limit_minor is not None
                                    else os.getenv('SMS_DAILY_COST_LIMIT_MINOR', '50000'))
        self.unit_cost = int(unit_cost_minor if unit_cost_minor is not None else os.getenv('SMS_UNIT_COST_MINOR', '50'))
        self.audit = audit or (lambda event: None)
        self.clock = clock
        self._lock = threading.Lock()
        self._day, self._count = None, 0

    def send(self, to, text, purpose):
        if not E164.fullmatch(to or ''):
            raise SmsError('invalid destination')
        with self._lock:
            day = datetime.fromtimestamp(self.clock(), timezone.utc).date()
            if day != self._day:
                self._day, self._count = day, 0
            if self._count >= self.daily_limit:
                self.audit({'event': 'sms_blocked', 'reason': 'daily_limit', 'to': mask(to), 'purpose': purpose})
                raise SmsLimitError('daily SMS limit reached')
            if (self._count + 1) * self.unit_cost > self.daily_cost_limit:
                self.audit({'event': 'sms_blocked', 'reason': 'cost_limit', 'to': mask(to), 'purpose': purpose})
                raise SmsLimitError('daily SMS cost limit reached')
            self._count += 1
        message_id = self.provider.send(to, text)
        # The audit record never contains the message text (it may hold an OTP).
        self.audit({'event': 'sms_sent', 'provider': self.provider.name, 'to': mask(to),
                    'purpose': purpose, 'message_id': message_id, 'cost_minor': self.unit_cost})
        return message_id


def gateway_from_env(audit=None):
    name = os.getenv('SMS_PROVIDER', 'test').strip().lower()
    if name == 'test':
        return SmsGateway(TestSmsProvider(), audit=audit)
    raise SmsError(f'SMS provider {name!r} is not activated yet; see activation checklist')
