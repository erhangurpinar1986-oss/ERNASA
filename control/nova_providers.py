"""External channels for NOVA: outbound e-mail, read-only mailbox, AI voice calls.

Every adapter is OFF unless its credentials are configured, and none of them decides on its own
whether a message/call is allowed - control/nova.py gates every action first.

Environment (all optional; missing -> feature reports "not configured"):
  E-mail out   ERNASA_EMAIL_PROVIDER (brevo|resend), ERNASA_BREVO_API_KEY / ERNASA_RESEND_API_KEY,
               ERNASA_EMAIL_FROM (default iletisim@ernasa.com)  - same keys as services/invite_email.py
  Mailbox      ERNASA_NOVA_IMAP_HOST (default mail.privateemail.com), ERNASA_NOVA_IMAP_USER,
               ERNASA_NOVA_IMAP_PASSWORD (a separate app password), ERNASA_NOVA_IMAP_FOLDER (INBOX)
  Voice (Vapi) ERNASA_NOVA_VAPI_API_KEY, ERNASA_NOVA_VAPI_PHONE_NUMBER_ID (BYO SIP number of a
               Turkish licensed operator), ERNASA_NOVA_VAPI_WEBHOOK_SECRET,
               ERNASA_NOVA_VAPI_API_BASE (default https://api.vapi.ai; EU: https://api.eu.vapi.ai),
               ERNASA_NOVA_VOICE_ID (default tr-TR-EmelNeural, provider azure), ERNASA_NOVA_LLM_MODEL,
               ERNASA_NOVA_USD_TRY (fallback rate for converting provider USD cost to kuruş)
  Scheduler    ERNASA_NOVA_TICK_SECRET (Bearer token for the /control/nova/hook/tick endpoint)
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from email import message_from_bytes
from email.header import decode_header, make_header
from email.policy import default as email_policy
from email.utils import parseaddr
import hmac
import imaplib
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class NotConfigured(RuntimeError):
    pass


class ProviderError(RuntimeError):
    pass


def configured(*names):
    return all(os.getenv(name, '').strip() for name in names)


def status():
    """Non-secret readiness overview for the CONTROL panel."""
    provider = os.getenv('ERNASA_EMAIL_PROVIDER', 'resend').strip().lower()
    key = 'ERNASA_BREVO_API_KEY' if provider == 'brevo' else 'ERNASA_RESEND_API_KEY'
    return {
        'email_out': {'ready': configured(key), 'provider': provider, 'needs': [] if configured(key) else [key]},
        'mailbox': {'ready': configured('ERNASA_NOVA_IMAP_USER', 'ERNASA_NOVA_IMAP_PASSWORD'),
                    'host': os.getenv('ERNASA_NOVA_IMAP_HOST', 'mail.privateemail.com'),
                    'needs': [n for n in ('ERNASA_NOVA_IMAP_USER', 'ERNASA_NOVA_IMAP_PASSWORD') if not configured(n)]},
        'voice': {'ready': configured('ERNASA_NOVA_VAPI_API_KEY', 'ERNASA_NOVA_VAPI_PHONE_NUMBER_ID', 'ERNASA_NOVA_VAPI_WEBHOOK_SECRET'),
                  'provider': 'vapi',
                  'needs': [n for n in ('ERNASA_NOVA_VAPI_API_KEY', 'ERNASA_NOVA_VAPI_PHONE_NUMBER_ID',
                                        'ERNASA_NOVA_VAPI_WEBHOOK_SECRET') if not configured(n)]},
        'scheduler': {'ready': len(os.getenv('ERNASA_NOVA_TICK_SECRET', '')) >= 32,
                      'needs': [] if len(os.getenv('ERNASA_NOVA_TICK_SECRET', '')) >= 32 else ['ERNASA_NOVA_TICK_SECRET (≥32 karakter)']},
    }


# ---------------------------------------------------------------- outbound e-mail

class EmailTransport:
    """Reuses the audited sender in services/invite_email.py (fixed sender allowlist, sandbox, no retries)."""

    def __call__(self, recipient, subject, text, idempotency_key=''):
        from services import invite_email as mail
        sandbox = bool(os.getenv('ERNASA_EMAIL_SANDBOX_DIR'))
        if not sandbox:
            try:
                mail.configured_sender()
            except mail.EmailConfigurationError as exc:
                raise NotConfigured('Kurumsal e-posta gönderimi yapılandırılmadı.') from exc
        try:
            message_id = mail.send_notice(recipient, subject, text, idempotency_key=idempotency_key or 'nova-' + os.urandom(8).hex())
        except mail.EmailDeliveryError as exc:
            raise ProviderError(str(exc)) from exc
        return ('sandbox' if sandbox else mail._email_provider()), str(message_id)


# ---------------------------------------------------------------- read-only mailbox

def _text(message):
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == 'text/plain' and not part.get_filename():
                try:
                    return part.get_content()
                except (LookupError, UnicodeError):
                    return part.get_payload(decode=True).decode('utf-8', 'replace')
        for part in message.walk():
            if part.get_content_type() == 'text/html' and not part.get_filename():
                return re.sub(r'<[^>]+>', ' ', part.get_content())
        return ''
    try:
        return message.get_content()
    except (LookupError, UnicodeError, KeyError):
        return ''


def parse_message(raw):
    message = message_from_bytes(raw, policy=email_policy)
    sender = parseaddr(str(message.get('From', '')))[1].lower()
    subject = str(make_header(decode_header(str(message.get('Subject', '')))))
    body = _text(message)
    original = ''
    failed = message.get('X-Failed-Recipients')
    if failed:
        original = parseaddr(str(failed).split(',')[0])[1].lower()
    else:
        match = re.search(r'Final-Recipient:\s*rfc822;\s*([^\s;]+)', body, re.I) or \
                re.search(r'Original-Recipient:\s*rfc822;\s*([^\s;]+)', raw.decode('utf-8', 'replace'), re.I)
        if match:
            original = match.group(1).strip().lower()
    return {'provider_id': str(message.get('Message-ID', '')).strip()[:200], 'from': sender, 'subject': subject[:300],
            'body': body[:20000], 'original_recipient': original}


class Mailbox:
    """IMAP in read-only mode (EXAMINE + BODY.PEEK): cannot delete, move, flag or mark as read."""

    def fetch(self, since_days=3, limit=200):
        if not configured('ERNASA_NOVA_IMAP_USER', 'ERNASA_NOVA_IMAP_PASSWORD'):
            raise NotConfigured('Gelen kutusu okuyucu yapılandırılmadı.')
        host = os.getenv('ERNASA_NOVA_IMAP_HOST', 'mail.privateemail.com')
        since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime('%d-%b-%Y')
        out = []
        with imaplib.IMAP4_SSL(host, 993, timeout=20) as box:
            box.login(os.environ['ERNASA_NOVA_IMAP_USER'], os.environ['ERNASA_NOVA_IMAP_PASSWORD'])
            box.select(os.getenv('ERNASA_NOVA_IMAP_FOLDER', 'INBOX'), readonly=True)
            kind, data = box.search(None, 'SINCE', since)
            if kind != 'OK':
                raise ProviderError('Gelen kutusu araması başarısız.')
            for number in data[0].split()[-limit:]:
                kind, parts = box.fetch(number, '(BODY.PEEK[])')
                if kind == 'OK' and parts and isinstance(parts[0], tuple):
                    parsed = parse_message(parts[0][1])
                    if parsed['provider_id'] and parsed['from'] != os.getenv('ERNASA_EMAIL_FROM', 'iletisim@ernasa.com'):
                        out.append(parsed)
        return out


# ---------------------------------------------------------------- AI voice (Vapi)

def usd_to_minor(usd):
    rate = Decimal(os.getenv('ERNASA_NOVA_USD_TRY', '0') or '0')
    if rate <= 0:
        try:
            from datetime import date
            from . import tcmb_rates
            rate = tcmb_rates.fetch_latest_usd_selling_rate(date.today()).value
        except Exception:
            return None
    return int((Decimal(str(usd)) * rate * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


class VapiVoice:
    name = 'vapi'

    def __init__(self, origin=''):
        self.origin = origin

    def _request(self, method, path, payload=None):
        if not configured('ERNASA_NOVA_VAPI_API_KEY', 'ERNASA_NOVA_VAPI_PHONE_NUMBER_ID', 'ERNASA_NOVA_VAPI_WEBHOOK_SECRET'):
            raise NotConfigured('Sesli arama sağlayıcısı (Vapi) yapılandırılmadı.')
        base = os.getenv('ERNASA_NOVA_VAPI_API_BASE', 'https://api.vapi.ai').rstrip('/')
        if not base.startswith('https://'):
            raise NotConfigured('Vapi API adresi HTTPS olmalı.')
        request = Request(base + path, method=method,
                          data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
                          headers={'Authorization': 'Bearer ' + os.environ['ERNASA_NOVA_VAPI_API_KEY'],
                                   'Content-Type': 'application/json', 'User-Agent': 'ERNASA-NOVA/2.0'})
        try:
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read(65536) or b'{}')
        except HTTPError as exc:
            # Never echo provider bodies: they can contain phone numbers or keys.
            raise ProviderError(f'Vapi isteği reddedildi (HTTP {exc.code}).') from None
        except (URLError, TimeoutError, OSError):
            raise ProviderError('Vapi servisine ulaşılamadı.') from None

    def assistant(self, context, max_seconds):
        override = os.getenv('ERNASA_NOVA_VAPI_ASSISTANT_JSON', '')
        base = json.loads(override) if override else {}
        schema = {'type': 'object', 'properties': {
            'outcome': {'type': 'string'}, 'needs': {'type': 'string'}, 'questions': {'type': 'string'},
            'objections': {'type': 'string'}, 'interest': {'type': 'string', 'enum': ['yüksek', 'orta', 'düşük', 'yok']},
            'demo_requested': {'type': 'boolean'}, 'next_step': {'type': 'string'}, 'do_not_call': {'type': 'boolean'},
            'handoff': {'type': 'boolean'}, 'recording_consent': {'type': 'boolean'}}}
        assistant = {
            'name': 'NOVA ERNASA',
            'firstMessage': context['first_message'],
            'model': {'provider': 'openai', 'model': os.getenv('ERNASA_NOVA_LLM_MODEL', 'gpt-4.1-mini'),
                      'temperature': 0.3, 'messages': [{'role': 'system', 'content': context['system']}]},
            'voice': {'provider': os.getenv('ERNASA_NOVA_VOICE_PROVIDER', 'azure'),
                      'voiceId': os.getenv('ERNASA_NOVA_VOICE_ID', 'tr-TR-EmelNeural')},
            'transcriber': {'provider': 'deepgram', 'model': os.getenv('ERNASA_NOVA_STT_MODEL', 'nova-3'), 'language': 'tr'},
            'maxDurationSeconds': int(max_seconds),
            'recordingEnabled': False,
            'endCallPhrases': ['iyi günler dilerim', 'görüşmek üzere'],
            'analysisPlan': {'summaryPlan': {'enabled': True},
                             'structuredDataPlan': {'enabled': True, 'schema': schema}},
        }
        if self.origin:
            assistant['server'] = {'url': self.origin.rstrip('/') + '/control/nova/hook/vapi',
                                   'secret': os.environ.get('ERNASA_NOVA_VAPI_WEBHOOK_SECRET', '')}
        for key, value in base.items():
            if key not in ('model', 'firstMessage', 'server', 'maxDurationSeconds'):
                assistant[key] = value
        return assistant

    def start_call(self, phone, context, max_seconds, metadata=None):
        result = self._request('POST', '/call', {
            'phoneNumberId': os.environ.get('ERNASA_NOVA_VAPI_PHONE_NUMBER_ID', ''),
            'customer': {'number': phone},
            'assistant': self.assistant(context, max_seconds),
            'metadata': metadata or {},
        })
        call_id = result.get('id')
        if not call_id:
            raise ProviderError('Vapi arama kimliği döndürmedi.')
        return self.name, call_id

    @staticmethod
    def verify(headers):
        secret = os.getenv('ERNASA_NOVA_VAPI_WEBHOOK_SECRET', '')
        provided = headers.get('x-vapi-secret', '') or ''
        return len(secret) >= 24 and hmac.compare_digest(secret.encode(), provided.encode())

    @staticmethod
    def parse_webhook(body):
        """Return (provider_call_id, result) for end-of-call reports, else (None, None)."""
        message = body.get('message') if isinstance(body, dict) else None
        if not isinstance(message, dict) or message.get('type') != 'end-of-call-report':
            return None, None
        call = message.get('call') or {}
        call_id = str(call.get('id') or '')
        analysis = message.get('analysis') or {}
        data = analysis.get('structuredData') or {}
        reason = str(message.get('endedReason') or '')
        status = 'completed'
        if 'no-answer' in reason or 'busy' in reason or 'voicemail' in reason:
            status = 'no_answer'
        elif 'error' in reason or 'failed' in reason:
            status = 'failed'
        if data.get('do_not_call') and 'yapay' in str(data.get('objections', '')).lower():
            status = 'declined_ai'
        duration = message.get('durationSeconds')
        if duration is None and message.get('startedAt') and message.get('endedAt'):
            try:
                start = datetime.fromisoformat(str(message['startedAt']).replace('Z', '+00:00'))
                end = datetime.fromisoformat(str(message['endedAt']).replace('Z', '+00:00'))
                duration = (end - start).total_seconds()
            except ValueError:
                duration = 0
        cost = message.get('cost')
        result = {
            'status': status, 'duration_seconds': duration or 0,
            'cost_minor': usd_to_minor(cost) if cost is not None else None,
            'summary': analysis.get('summary') or '',
            **{k: data.get(k) for k in ('outcome', 'needs', 'questions', 'objections', 'interest', 'next_step')},
            'demo_requested': bool(data.get('demo_requested')), 'do_not_call': bool(data.get('do_not_call')),
            'handoff': bool(data.get('handoff')),
            # Transcript is kept only if the assistant confirmed the recording notice was accepted.
            'recording_consent_confirmed': data.get('recording_consent') is True,
            'transcript': (message.get('artifact') or {}).get('transcript') or message.get('transcript') or '',
        }
        return call_id, result


class DisabledVoice:
    name = 'disabled'

    def start_call(self, *args, **kwargs):
        raise NotConfigured('Sesli arama sağlayıcısı yapılandırılmadı.')
