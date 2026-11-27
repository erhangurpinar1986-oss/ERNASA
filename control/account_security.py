"""ERNASA SECURITY 2026 — account security for CONTROL and Sales Hub.

Screens
  /guvenlik            every CONTROL user: two-factor (TOTP), recovery codes, phone, sessions
  /guvenlik-merkezi    founder only: ERNASA SECURITY CENTER (read-only security overview)
  /sifremi-unuttum     public: password reset request (CONTROL and Sales Hub accounts)
  /sifre-sifirla       public: password reset with a one-time e-mail link (+ TOTP / SMS when set up)

Controlled activation (all default to a state that never blocks existing users)
  SECURITY_PASSWORD_RESET=on   enables the reset flow (default off: page explains the manual route)
  TURNSTILE_MODE=off|test|enforce, TURNSTILE_SITE_KEY / TURNSTILE_SECRET_KEY / TURNSTILE_HOSTNAMES
  SMS_PROVIDER=<adapter>       phone verification and SMS reset codes (no adapter is active yet)
  ERNASA_SECURITY_ENC_KEY      base64 32-byte key for TOTP secrets (falls back to an HKDF of
                               ERNASA_IK_CONTROL_KEY; without either, 2FA enrolment is unavailable)

Rules
  * 2FA is enforced only for accounts that completed enrolment; nobody is locked out by rollout.
  * The founder can reset a password only with an enrolled authenticator (never e-mail or SMS alone).
  * Reset requests never reveal whether an account exists.
  * Codes, tokens and secrets are never logged or shown in the Security Center; tokens are stored
    as SHA-256, TOTP secrets with AES-256-GCM, recovery codes as scrypt-free SHA-256 of 80-bit codes.
  * security_events is append-only (database triggers).
"""
import base64
import hashlib
import hmac
import os
import secrets
import struct
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit

from auth_security import otp as otp_module
from auth_security import phone as phone_module
from auth_security import sms as sms_module
from auth_security import turnstile as turnstile_module

PUBLIC_PATHS = frozenset({'/sifremi-unuttum', '/sifre-sifirla'})
PAGES = frozenset({'/guvenlik', '/guvenlik-merkezi'})
TOTP_PERIOD = 30
TOTP_DIGITS = 6
RECOVERY_CODES = 10
RESET_TTL = 30 * 60
RESET_PER_ACCOUNT_HOUR = 3
RESET_GLOBAL_HOUR = 30
ISSUER = 'ERNASA CONTROL'
TURNSTILE_SCRIPT = 'https://challenges.cloudflare.com'

DDL = (
    """CREATE TABLE IF NOT EXISTS security_totp(
        area TEXT NOT NULL, user_id TEXT NOT NULL, secret_enc TEXT NOT NULL, enabled INTEGER NOT NULL,
        last_counter INTEGER NOT NULL, created INTEGER NOT NULL, PRIMARY KEY(area, user_id))""",
    """CREATE TABLE IF NOT EXISTS security_recovery_codes(
        area TEXT NOT NULL, user_id TEXT NOT NULL, code_hash TEXT NOT NULL, used_at INTEGER,
        PRIMARY KEY(area, user_id, code_hash))""",
    """CREATE TABLE IF NOT EXISTS security_password_resets(
        token_hash TEXT PRIMARY KEY, area TEXT NOT NULL, user_id TEXT NOT NULL,
        created INTEGER NOT NULL, expires INTEGER NOT NULL, used_at INTEGER)""",
    """CREATE TABLE IF NOT EXISTS security_phones(
        area TEXT NOT NULL, user_id TEXT NOT NULL, e164 TEXT NOT NULL, verified_at INTEGER,
        updated INTEGER NOT NULL, PRIMARY KEY(area, user_id))""",
    """CREATE TABLE IF NOT EXISTS security_events(
        id TEXT PRIMARY KEY, at INTEGER NOT NULL, area TEXT NOT NULL, kind TEXT NOT NULL,
        subject TEXT NOT NULL, detail TEXT NOT NULL)""",
)
SQLITE_GUARDS = (
    """CREATE TRIGGER IF NOT EXISTS security_events_no_update BEFORE UPDATE ON security_events
       BEGIN SELECT RAISE(ABORT, 'security_events is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS security_events_no_delete BEFORE DELETE ON security_events
       BEGIN SELECT RAISE(ABORT, 'security_events is append-only'); END""",
)
PG_GUARDS = (
    """CREATE OR REPLACE FUNCTION security_events_append_only() RETURNS trigger LANGUAGE plpgsql AS
       $fn$ BEGIN RAISE EXCEPTION 'security_events is append-only'; END $fn$""",
    "DROP TRIGGER IF EXISTS security_events_append_only ON security_events",
    """CREATE TRIGGER security_events_append_only BEFORE UPDATE OR DELETE ON security_events
       FOR EACH ROW EXECUTE FUNCTION security_events_append_only()""",
)

_schema_lock = threading.Lock()
_schema_ready = set()


def ensure_schema(server):
    """Idempotent DDL on a separate connection (no numbered migration, no ordering conflicts)."""
    from . import app as c
    target = server.db_path
    key = repr(target)
    if key in _schema_ready:
        return
    with _schema_lock:
        if key in _schema_ready:
            return
        db = c.connect(target)
        try:
            from . import login_throttle
            postgres = hasattr(db, 'raw')
            if not postgres:
                db.execute('PRAGMA busy_timeout=200')
            if postgres:
                with db.raw, db.raw.cursor() as cursor:
                    cursor.execute('SELECT pg_advisory_xact_lock(184093, 47630)')
                    for statement in login_throttle.DDL + DDL + PG_GUARDS:
                        cursor.execute(statement)
            else:
                for statement in login_throttle.DDL + DDL + SQLITE_GUARDS:
                    db.execute(statement)
                db.commit()
        except Exception as exc:
            # Never break the request that triggered it (e.g. SQLite write lock held by that request);
            # the server start-up call in app.server() creates the tables in production.
            import sys
            print(f'ACCOUNT_SECURITY_SCHEMA_DEFERRED type={type(exc).__name__}', file=sys.stderr, flush=True)
            return
        finally:
            db.close()
        _schema_ready.add(key)


def _table_exists(db, name):
    if hasattr(db, 'raw'):
        return db.execute('SELECT to_regclass(?) IS NOT NULL', (name,)).fetchone()[0]
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


# ---------------------------------------------------------------- configuration

def reset_enabled():
    return os.getenv('SECURITY_PASSWORD_RESET', 'off').strip().lower() == 'on'


def turnstile():
    try:
        return turnstile_module.TurnstileVerifier()
    except turnstile_module.TurnstileConfigError:
        return turnstile_module.TurnstileVerifier(secret='', hostnames='', mode='off')


def sms_gateway():
    try:
        return sms_module.gateway_from_env(audit=None) if os.getenv('SMS_PROVIDER', '') not in ('', 'test') else None
    except sms_module.SmsError:
        return None


def _otp_service():
    pepper = os.getenv('OTP_PEPPER', '')
    if len(pepper) < 32:
        return None
    store = _otp_service.store = getattr(_otp_service, 'store', None) or otp_module.MemoryOtpStore()
    return otp_module.OtpService(store=store, pepper=pepper)


def _encryption_key():
    raw = os.getenv('ERNASA_SECURITY_ENC_KEY', '')
    if raw:
        try:
            key = base64.b64decode(raw)
            if len(key) == 32:
                return key
        except ValueError:
            return None
        return None
    bridge = os.getenv('ERNASA_IK_CONTROL_KEY', '')
    if len(bridge) >= 32:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=b'ernasa-security-2026',
                    info=b'totp-secrets').derive(bridge.encode())
    if os.getenv('RENDER') == 'true':
        return None
    return hashlib.sha256(b'ernasa-local-development-only').digest()


def two_factor_available():
    return _encryption_key() is not None


def _encrypt(plain):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    return base64.b64encode(nonce + AESGCM(_encryption_key()).encrypt(nonce, plain.encode(), b'totp')).decode()


def _decrypt(token):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(token)
    return AESGCM(_encryption_key()).decrypt(raw[:12], raw[12:], b'totp').decode()


# ---------------------------------------------------------------- TOTP (RFC 6238)

def new_totp_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')


def totp_at(secret, counter):
    key = base64.b32decode(secret + '=' * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f'{value % 10 ** TOTP_DIGITS:0{TOTP_DIGITS}d}'


def match_totp(secret, code, last_counter, now=None):
    """Return the matching counter (window ±1 step, never reused) or None."""
    code = str(code or '').strip().replace(' ', '')
    if len(code) != TOTP_DIGITS or not code.isdigit():
        return None
    current = int((now if now is not None else time.time()) // TOTP_PERIOD)
    for counter in (current - 1, current, current + 1):
        if counter > last_counter and hmac.compare_digest(totp_at(secret, counter), code):
            return counter
    return None


def otpauth_uri(secret, account):
    label = quote(f'{ISSUER}:{account}')
    return f'otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&algorithm=SHA1&digits=6&period=30'


def _recovery_hash(code):
    return hashlib.sha256(code.replace('-', '').strip().upper().encode()).hexdigest()


def new_recovery_codes():
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    codes = []
    for _ in range(RECOVERY_CODES):
        raw = ''.join(secrets.choice(alphabet) for _ in range(16))
        codes.append(f'{raw[:4]}-{raw[4:8]}-{raw[8:12]}-{raw[12:]}')
    return codes


# ---------------------------------------------------------------- shared helpers

def log_event(db, area, kind, subject, detail=''):
    db.execute('INSERT INTO security_events(id,at,area,kind,subject,detail) VALUES(?,?,?,?,?,?)',
               (secrets.token_hex(16), int(time.time()), area, kind, str(subject)[:120], str(detail)[:300]))


def _mask_email(address):
    name, _, domain = str(address).partition('@')
    return (name[:1] + '***@' + domain) if domain else '***'


def totp_row(db, area, user_id):
    return db.execute('SELECT secret_enc,enabled,last_counter FROM security_totp WHERE area=? AND user_id=?',
                      (area, str(user_id))).fetchone()


def has_two_factor(db, area, user_id):
    row = totp_row(db, area, user_id)
    return bool(row and row[1])


def verify_second_factor(db, area, user_id, code):
    """True if code is a valid unused TOTP or recovery code for an enrolled account (consumes it)."""
    row = totp_row(db, area, user_id)
    if not row or not row[1]:
        return False
    code = str(code or '').strip()
    if '-' in code or len(code) > TOTP_DIGITS:
        hashed = _recovery_hash(code)
        hit = db.execute('SELECT 1 FROM security_recovery_codes WHERE area=? AND user_id=? AND code_hash=? '
                         'AND used_at IS NULL', (area, str(user_id), hashed)).fetchone()
        if not hit:
            return False
        db.execute('UPDATE security_recovery_codes SET used_at=? WHERE area=? AND user_id=? AND code_hash=?',
                   (int(time.time()), area, str(user_id), hashed))
        log_event(db, area, 'recovery_code_used', user_id)
        return True
    if not two_factor_available():
        return False
    counter = match_totp(_decrypt(row[0]), code, row[2])
    if counter is None:
        return False
    db.execute('UPDATE security_totp SET last_counter=? WHERE area=? AND user_id=?', (counter, area, str(user_id)))
    return True


def turnstile_widget(action):
    verifier = turnstile()
    if verifier.mode == 'off' or not os.getenv('TURNSTILE_SITE_KEY'):
        return ''
    from . import app as c
    return (f'<script src="{TURNSTILE_SCRIPT}/turnstile/v0/api.js" async defer></script>'
            f'<div class="cf-turnstile" data-sitekey="{c.e(os.getenv("TURNSTILE_SITE_KEY"))}" '
            f'data-action="{c.e(action)}"></div>')


def turnstile_csp():
    return ("default-src 'none'; style-src 'self'; img-src 'self'; "
            f"script-src 'self' {TURNSTILE_SCRIPT}; frame-src {TURNSTILE_SCRIPT}; connect-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")


def require_turnstile(handler, data, action, always=False, needed=False):
    """Server-side check. With TURNSTILE_MODE=off this never blocks (controlled activation)."""
    from . import app as c
    verifier = turnstile()
    if verifier.mode == 'off' or not (always or needed):
        return
    remote = handler.headers.get('X-Ernasa-Client-IP') or None
    if not verifier.verify(data.get('cf-turnstile-response', ''), action, remote):
        raise c.Problem(403, 'Bot doğrulaması tamamlanamadı. Sayfayı yenileyip tekrar deneyin.')


def login_second_factor(handler, db, area, row, data):
    """Called after a correct password. Raises Problem(401) when an enrolled account gives no valid code."""
    from . import app as c
    ensure_schema(handler.server)
    if not _table_exists(db, 'security_totp') or not has_two_factor(db, area, row['id']):
        return  # no table means nobody can have enrolled yet
    code = str(data.get('code', '')).strip()
    if not code:
        raise c.Problem(401, 'Bu hesapta iki adımlı doğrulama açık. Doğrulama uygulamasındaki kodu da girin.')
    if not verify_second_factor(db, area, row['id'], code):
        raise c.Problem(401, 'Giriş bilgileri geçersiz.')


def login_extra_fields():
    return ('<label>Doğrulama kodu <small>(iki adımlı doğrulama açıksa)</small>'
            '<input name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="19"></label>')


def login_links(area):
    if not reset_enabled():
        return ''
    return f'<p><a href="/sifremi-unuttum?alan={area}">Şifremi unuttum</a></p>'


# ---------------------------------------------------------------- public password reset

def _public_csrf(handler):
    secure = '; Secure' if handler.server.origin.startswith('https://') else ''
    token = secrets.token_urlsafe(32)
    cookie = (f'security_reset_csrf={token}; Path={handler.server.public_prefix or "/"}; HttpOnly; '
              f'SameSite=Strict; Max-Age=900{secure}')
    return token, ('Set-Cookie', cookie)


def _check_public_csrf(handler, data):
    from . import app as c
    from http.cookies import SimpleCookie
    cookie = SimpleCookie(handler.headers.get('Cookie', '')).get('security_reset_csrf')
    if not cookie or not hmac.compare_digest(cookie.value.encode(), str(data.get('csrf', '')).encode()):
        raise c.Problem(403, 'Form doğrulanamadı. Sayfayı yenileyin.')


def _account(db, area, address):
    if area == 'sales':
        row = db.execute("SELECT a.id,a.email,a.name,a.secret FROM sales_accounts a JOIN companies c "
                         "ON c.id=a.company_id WHERE a.email=? AND a.active=1 AND c.status='Aktif'",
                         (address,)).fetchone()
        return (dict(row) | {'role': 'sales'}) if row else None
    row = db.execute("SELECT users.id,users.email,users.name,users.secret,users.role FROM users LEFT JOIN companies "
                     "ON companies.id=company_id WHERE email=? AND users.active=1 AND (role='founder' OR "
                     "companies.status='Aktif')", (address,)).fetchone()
    return dict(row) if row else None


def _account_by_id(db, area, user_id):
    if area == 'sales':
        row = db.execute('SELECT id,email,name,active FROM sales_accounts WHERE id=?', (user_id,)).fetchone()
        return (dict(row) | {'role': 'sales'}) if row and row['active'] else None
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    row = db.execute('SELECT id,email,name,role,active FROM users WHERE id=?', (uid,)).fetchone()
    return dict(row) if row and row['active'] else None


def _send_mail(handler, recipient, subject, body, key):
    sender = getattr(handler.server, 'security_mail', None)
    if sender:
        return sender(recipient, subject, body, key)
    from services import invite_email
    return invite_email.send_notice(recipient, subject, body, idempotency_key=key)


def _public_page(handler, title, body, headers=(), status=200, widget=False):
    if widget:
        handler.csp_override = turnstile_csp()
    return handler.page(title, body, status=status, headers=headers)


def handle_public(handler, db, path, post, data):
    from . import app as c
    ensure_schema(handler.server)
    query = {k: v[-1] for k, v in parse_qs(urlsplit(handler.path).query, max_num_fields=4).items()}
    area = 'sales' if (data or query).get('alan') == 'sales' else 'control'
    if not reset_enabled():
        return _public_page(handler, 'Şifremi unuttum', c.card('Şifre sıfırlama henüz etkin değil', (
            '<p>Çevrimiçi şifre sıfırlama kurucu onayıyla etkinleştirilecek. Bu süre içinde firma yöneticiniz veya '
            'ERNASA yönetimi hesabınız için geçici parola oluşturabilir; ilk girişte parolanızı değiştirmeniz '
            'istenir.</p><p><a href="/giris">Giriş ekranına dön</a></p>')))
    if path == '/sifremi-unuttum':
        if not post:
            token, cookie = _public_csrf(handler)
            fields = (f'<input type="hidden" name="csrf" value="{token}"><input type="hidden" name="alan" value="{area}">'
                      '<label>E-posta<input type="email" name="email" autocomplete="username" required></label>'
                      + turnstile_widget('password_reset'))
            return _public_page(handler, 'Şifremi unuttum', c.card('Şifremi unuttum', (
                '<p>Kayıtlı e-posta adresinizi girin. Hesap varsa tek kullanımlık bir sıfırlama bağlantısı gönderilir.</p>'
                f'<form method="post" action="/sifremi-unuttum"><div class="fields">{fields}</div>'
                '<button>Bağlantı gönder</button></form>')), headers=(cookie,), widget=True)
        _check_public_csrf(handler, data)
        require_turnstile(handler, data, 'password_reset', always=True)
        address = c.email(data)
        now = int(time.time())
        recent = db.execute('SELECT COUNT(*) FROM security_password_resets WHERE created>?', (now - 3600,)).fetchone()[0]
        account = _account(db, area, address)
        if recent < RESET_GLOBAL_HOUR and account:
            uid = str(account['id'])
            mine = db.execute('SELECT COUNT(*) FROM security_password_resets WHERE area=? AND user_id=? AND created>?',
                              (area, uid, now - 3600)).fetchone()[0]
            if account['role'] == 'founder' and not has_two_factor(db, area, uid):
                log_event(db, area, 'reset_refused_no_2fa', uid)
            elif mine < RESET_PER_ACCOUNT_HOUR:
                token = secrets.token_urlsafe(32)
                db.execute('INSERT INTO security_password_resets(token_hash,area,user_id,created,expires) VALUES(?,?,?,?,?)',
                           (hashlib.sha256(token.encode()).hexdigest(), area, uid, now, now + RESET_TTL))
                link = f'{handler.server.origin}{handler.server.public_prefix}/sifre-sifirla?t={token}'
                try:
                    _send_mail(handler, account['email'], 'ERNASA parola sıfırlama',
                               f'Merhaba {account["name"]},\n\nParolanızı sıfırlamak için 30 dakika geçerli, tek '
                               f'kullanımlık bağlantı:\n{link}\n\nBu isteği siz yapmadıysanız bu e-postayı dikkate '
                               'almayın; parolanız değişmez.\n\nERNASA Güvenlik', f'reset-{token[:16]}')
                    log_event(db, area, 'reset_requested', uid)
                except Exception as exc:  # never reveal delivery state to the requester
                    log_event(db, area, 'reset_mail_failed', uid, type(exc).__name__)
        db.commit()
        return _public_page(handler, 'Şifremi unuttum', c.card('İsteğiniz alındı', (
            '<p>Bu e-posta adresiyle kayıtlı etkin bir hesap varsa sıfırlama bağlantısı gönderildi. '
            'Bağlantı 30 dakika geçerlidir.</p><p><a href="/giris">Giriş ekranına dön</a></p>')))
    # /sifre-sifirla
    token = (data or query).get('t', '')
    record = db.execute('SELECT area,user_id,expires,used_at FROM security_password_resets WHERE token_hash=?',
                        (hashlib.sha256(token.encode()).hexdigest(),)).fetchone() if token else None
    if not record or record[3] is not None or record[2] <= int(time.time()):
        return _public_page(handler, 'Parola sıfırlama', c.card('Bağlantı geçersiz', (
            '<p>Bağlantının süresi dolmuş ya da daha önce kullanılmış. Yeni bir bağlantı isteyin.</p>'
            '<p><a href="/sifremi-unuttum">Yeni bağlantı iste</a></p>')), status=410)
    area, uid = record[0], record[1]
    account = _account_by_id(db, area, uid)
    if not account:
        raise c.Problem(410, 'Hesap etkin değil.')
    needs_2fa = has_two_factor(db, area, uid)
    phone = db.execute('SELECT e164 FROM security_phones WHERE area=? AND user_id=? AND verified_at IS NOT NULL',
                       (area, uid)).fetchone()
    gateway, otp_service = sms_gateway(), _otp_service()
    needs_sms = bool(phone and gateway and otp_service)
    if not post:
        if needs_sms:
            try:
                code, _ = otp_service.issue('password_reset', f'{area}:{uid}', phone[0], handler.headers.get('X-Ernasa-Client-IP'))
                gateway.send(phone[0], f'ERNASA parola sıfırlama kodunuz: {code}. Kimseyle paylaşmayın.', 'password_reset')
            except (otp_module.OtpError, sms_module.SmsError):
                pass
        csrf, cookie = _public_csrf(handler)
        fields = (f'<input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="t" value="{c.e(token)}">'
                  '<label>Yeni parola (12–128 karakter)<input type="password" name="password" minlength="12" maxlength="128" '
                  'autocomplete="new-password" required></label>'
                  + ('<label>Doğrulama uygulaması kodu veya kurtarma kodu<input name="code" autocomplete="one-time-code" required></label>'
                     if needs_2fa else '')
                  + (f'<label>SMS kodu ({phone_module.mask(phone[0])})<input name="sms_code" inputmode="numeric" required></label>'
                     if needs_sms else ''))
        return _public_page(handler, 'Parola sıfırlama', c.card('Yeni parola belirleyin', (
            f'<form method="post" action="/sifre-sifirla"><div class="fields">{fields}</div><button>Parolayı değiştir</button></form>')),
            headers=(cookie,))
    _check_public_csrf(handler, data)
    if needs_2fa and not verify_second_factor(db, area, uid, data.get('code', '')):
        raise c.Problem(401, 'Doğrulama kodu geçersiz.')
    if needs_sms:
        try:
            otp_service.verify('password_reset', f'{area}:{uid}', data.get('sms_code', ''))
        except otp_module.OtpError:
            raise c.Problem(401, 'SMS kodu geçersiz veya süresi dolmuş.') from None
    secret = c.new_secret({'password': data.get('password', '')})
    if area == 'sales':
        db.execute('UPDATE sales_accounts SET secret=? WHERE id=?', (secret, uid))
        db.execute('DELETE FROM sales_sessions WHERE account_id=?', (uid,))
    else:
        db.execute('UPDATE users SET secret=? WHERE id=?', (secret, int(uid)))
        db.execute('DELETE FROM sessions WHERE user_id=?', (int(uid),))
    db.execute('UPDATE security_password_resets SET used_at=? WHERE area=? AND user_id=? AND used_at IS NULL',
               (int(time.time()), area, uid))
    log_event(db, area, 'password_reset_completed', uid)
    notify_founder(handler, db, 'ERNASA güvenlik: parola sıfırlandı',
                   f'{"Sales Hub" if area == "sales" else "CONTROL"} hesap no #{uid} parolasını e-posta bağlantısıyla sıfırladı.')
    if area != 'sales':
        c.audit(db, {'email': account['email']}, 'Parola sıfırlandı (e-posta bağlantısı)', None)
    db.commit()
    from . import login_throttle
    login_throttle.for_server(handler.server, area).forget_devices(account['email'])
    try:
        _send_mail(handler, account['email'], 'ERNASA parolanız değiştirildi',
                   'Hesabınızın parolası az önce sıfırlandı ve bütün açık oturumlar kapatıldı.\n'
                   'Bu işlemi siz yapmadıysanız hemen ERNASA yönetimiyle iletişime geçin.\n\nERNASA Güvenlik',
                   f'reset-done-{secrets.token_hex(8)}')
    except Exception:
        pass
    return _public_page(handler, 'Parola sıfırlama', c.card('Parolanız değiştirildi', (
        '<p>Bütün açık oturumlar kapatıldı. Yeni parolanızla giriş yapabilirsiniz.</p>'
        f'<p><a href="{"/sales/login" if area == "sales" else "/giris"}">Giriş yap</a></p>')))


# ---------------------------------------------------------------- logged-in screens

def _area_tables(area):
    """(session table, session owner column, account table) for an area."""
    return ('sales_sessions', 'account_id', 'sales_accounts') if area == 'sales' else ('sessions', 'user_id', 'users')


def _db_id(area, uid):
    return str(uid) if area == 'sales' else int(uid)


def handle_page(handler, db, actor, path, post, data, query):
    """CONTROL screens. Every action changes only the signed-in user's own account."""
    from . import app as c
    ensure_schema(handler.server)
    if path == '/guvenlik-merkezi':
        if actor['role'] != 'founder':
            raise c.Problem(403, 'Bu ekran için yetkiniz yok.')
        if post:
            raise c.Problem(405, 'Security Center salt okunurdur.')
        return security_center(db, actor, query, handler.server)
    uid = str(actor['id'])
    notice = ''
    if post:
        notice = account_action(handler, db, 'control', uid, actor['email'], actor['token'], data.get('action', ''), data,
                                audit=lambda text: c.audit(db, actor, text, actor['company_id']))
        db.commit()
    form = lambda fields, action, label: c.form('/guvenlik', actor, c.hidden('action', action) + fields, label)
    return notice + account_panel(db, 'control', uid, actor['email'], actor['role'], form)


def handle_sales(h, db, info, csrf, session_hash, post, data):
    """Sales Hub representative security screen (/sales/guvenlik)."""
    from . import app as c
    from . import sales_http
    ensure_schema(h.server)
    uid = str(info['id'])
    notice = ''
    if post:
        notice = account_action(h, db, 'sales', uid, info['email'], session_hash, data.get('action', ''), data)
        db.commit()
    form = lambda fields, action, label: sales_http.form(c, '/sales/guvenlik', csrf, action, fields, label)
    return notice + account_panel(db, 'sales', uid, info['email'], 'sales', form)


def account_action(handler, db, area, uid, email, session_hash, action, data, audit=None):
    from . import app as c
    audit = audit or (lambda text: None)
    session_table, owner, accounts = _area_tables(area)
    row = totp_row(db, area, uid)
    if action == 'totp_start':
        if not two_factor_available():
            raise c.Problem(503, 'İki adımlı doğrulama için şifreleme anahtarı tanımlı değil.')
        if row and row[1]:
            raise c.Problem(409, 'İki adımlı doğrulama zaten açık.')
        secret = new_totp_secret()
        db.execute('DELETE FROM security_totp WHERE area=? AND user_id=?', (area, uid))
        db.execute('INSERT INTO security_totp(area,user_id,secret_enc,enabled,last_counter,created) VALUES(?,?,?,?,?,?)',
                   (area, uid, _encrypt(secret), 0, 0, int(time.time())))
        return ''
    if action == 'totp_confirm':
        if not row or row[1]:
            raise c.Problem(409, 'Önce kurulumu başlatın.')
        counter = match_totp(_decrypt(row[0]), data.get('code', ''), row[2])
        if counter is None:
            raise c.Problem(400, 'Kod doğrulanamadı. Uygulamadaki güncel kodu girin.')
        db.execute('UPDATE security_totp SET enabled=1,last_counter=? WHERE area=? AND user_id=?', (counter, area, uid))
        codes = _replace_recovery_codes(db, area, uid)
        log_event(db, area, 'totp_enabled', uid)
        audit('İki adımlı doğrulama açıldı')
        _notify(handler, email, 'ERNASA hesabınızda iki adımlı doğrulama açıldı',
                'Hesabınızda iki adımlı doğrulama açıldı. Bu işlemi siz yapmadıysanız hemen ERNASA yönetimiyle iletişime geçin.')
        return _codes_notice(c, codes)
    if action == 'recovery_regenerate':
        if not verify_second_factor(db, area, uid, data.get('code', '')):
            raise c.Problem(401, 'Doğrulama kodu geçersiz.')
        codes = _replace_recovery_codes(db, area, uid)
        log_event(db, area, 'recovery_codes_regenerated', uid)
        return _codes_notice(c, codes)
    if action == 'totp_disable':
        saved = db.execute(f'SELECT secret FROM {accounts} WHERE id=?', (_db_id(area, uid),)).fetchone()
        if not saved or not c.check_ik_password(data.get('password', ''), saved[0]):
            raise c.Problem(401, 'Parola doğrulanamadı.')
        if not verify_second_factor(db, area, uid, data.get('code', '')):
            raise c.Problem(401, 'Doğrulama kodu geçersiz.')
        db.execute('DELETE FROM security_totp WHERE area=? AND user_id=?', (area, uid))
        db.execute('DELETE FROM security_recovery_codes WHERE area=? AND user_id=?', (area, uid))
        log_event(db, area, 'totp_disabled', uid)
        audit('İki adımlı doğrulama kapatıldı')
        notify_founder(handler, db, 'ERNASA güvenlik: bir hesapta 2FA kapatıldı',
                       f'{"Sales Hub" if area == "sales" else "CONTROL"} hesap no #{uid} iki adımlı doğrulamayı kapattı. '
                       'Ayrıntılar ERNASA Security Center\'da.')
        _notify(handler, email, 'ERNASA hesabınızda iki adımlı doğrulama kapatıldı',
                'Hesabınızda iki adımlı doğrulama kapatıldı. Bu işlemi siz yapmadıysanız hemen ERNASA yönetimiyle iletişime geçin.')
        return '<p class="notice" role="status">İki adımlı doğrulama kapatıldı.</p>'
    if action == 'sessions_revoke_others':
        db.execute(f'DELETE FROM {session_table} WHERE {owner}=? AND token<>?', (_db_id(area, uid), session_hash))
        log_event(db, area, 'sessions_revoked', uid)
        audit('Diğer oturumlar kapatıldı')
        return '<p class="notice" role="status">Bu cihaz dışındaki bütün oturumlar kapatıldı.</p>'
    if action in ('phone_save', 'phone_verify'):
        gateway, otp_service = sms_gateway(), _otp_service()
        if not gateway or not otp_service:
            raise c.Problem(503, 'SMS sağlayıcısı henüz etkin değil.')
        if action == 'phone_save':
            try:
                number = phone_module.normalize(data.get('phone', ''))
            except phone_module.PhoneError as exc:
                raise c.Problem(400, str(exc)) from None
            if has_two_factor(db, area, uid) and not verify_second_factor(db, area, uid, data.get('code', '')):
                raise c.Problem(401, 'Numara değişikliği için doğrulama kodu gerekli.')
            db.execute('DELETE FROM security_phones WHERE area=? AND user_id=?', (area, uid))
            db.execute('INSERT INTO security_phones(area,user_id,e164,verified_at,updated) VALUES(?,?,?,?,?)',
                       (area, uid, number, None, int(time.time())))
            try:
                code, _ = otp_service.issue('phone_verification', f'{area}:{uid}', number,
                                            handler.headers.get('X-Ernasa-Client-IP'))
                gateway.send(number, f'ERNASA telefon doğrulama kodunuz: {code}', 'phone_verification')
            except otp_module.OtpError as exc:
                raise c.Problem(429, 'Çok sık kod istendi. Biraz sonra tekrar deneyin.') from exc
            _notify(handler, email, 'ERNASA hesabınıza telefon eklendi',
                    f'Hesabınıza {phone_module.mask(number)} numarası eklendi. Bu işlemi siz yapmadıysanız '
                    'ERNASA yönetimiyle iletişime geçin.')
            return '<p class="notice" role="status">Doğrulama kodu gönderildi.</p>'
        try:
            otp_service.verify('phone_verification', f'{area}:{uid}', data.get('sms_code', ''))
        except otp_module.OtpError:
            raise c.Problem(400, 'Kod geçersiz veya süresi dolmuş.') from None
        db.execute('UPDATE security_phones SET verified_at=? WHERE area=? AND user_id=?', (int(time.time()), area, uid))
        log_event(db, area, 'phone_verified', uid)
        return '<p class="notice" role="status">Telefon numarası doğrulandı.</p>'
    raise c.Problem(400, 'Bilinmeyen işlem.')


def _notify(handler, email, subject, body):
    try:
        _send_mail(handler, email, subject, body + '\n\nERNASA Güvenlik', f'security-{secrets.token_hex(8)}')
    except Exception:
        pass


def _replace_recovery_codes(db, area, uid):
    codes = new_recovery_codes()
    db.execute('DELETE FROM security_recovery_codes WHERE area=? AND user_id=?', (area, uid))
    for code in codes:
        db.execute('INSERT INTO security_recovery_codes(area,user_id,code_hash,used_at) VALUES(?,?,?,NULL)',
                   (area, uid, _recovery_hash(code)))
    return codes


def _codes_notice(c, codes):
    items = ''.join(f'<li><code>{c.e(code)}</code></li>' for code in codes)
    return ('<section class="card"><h2>Kurtarma kodlarınız</h2><p><strong>Bu kodlar yalnızca şimdi gösteriliyor.</strong> '
            'Güvenli bir yere kaydedin; her kod bir kez kullanılabilir. Doğrulama uygulamanıza erişemezseniz '
            f'giriş ekranındaki doğrulama kodu alanına bir kurtarma kodu yazabilirsiniz.</p><ol>{items}</ol></section>')


def account_panel(db, area, uid, email, role, form):
    from . import app as c
    session_table, owner, _ = _area_tables(area)
    row = totp_row(db, area, uid)
    code_input = c.inp('Doğrulama kodu', 'code')
    if row and row[1]:
        remaining = db.execute('SELECT COUNT(*) FROM security_recovery_codes WHERE area=? AND user_id=? AND used_at IS NULL',
                               (area, uid)).fetchone()[0]
        warning = ('<p class="notice" role="alert">Kurtarma kodunuz azaldı; yeni kodlar oluşturun.</p>'
                   if remaining <= 3 else '')
        totp = (f'<p><strong>Açık.</strong> Kullanılmamış kurtarma kodu: {remaining}.</p>' + warning
                + form(code_input, 'recovery_regenerate', 'Kurtarma kodlarını yenile')
                + form(c.inp('Mevcut parola', 'password', kind='password') + code_input, 'totp_disable',
                       'İki adımlı doğrulamayı kapat'))
    elif row:
        secret = _decrypt(row[0])
        grouped = ' '.join(secret[i:i + 4] for i in range(0, len(secret), 4))
        from . import qr
        totp = ('<p>Doğrulama uygulamanızla (Google Authenticator, Microsoft Authenticator, 1Password vb.) QR kodu '
                'okutun veya "anahtarı elle gir" seçeneğiyle aşağıdaki anahtarı ekleyin, ardından uygulamanın gösterdiği '
                '6 haneli kodu girin. QR kod bu sunucuda oluşturulur; anahtar hiçbir dış servise gönderilmez.</p>'
                f'<p>{qr.svg(otpauth_uri(secret, email), label="TOTP kurulum QR kodu")}</p>'
                f'<p><code>{c.e(grouped)}</code></p>'
                f'<p><small>Hesap: {c.e(ISSUER)} · {c.e(email)} · zaman tabanlı, 6 hane, 30 saniye</small></p>'
                + form(code_input, 'totp_confirm', 'Kurulumu tamamla'))
    elif two_factor_available():
        totp = ('<p>Kapalı. Açtığınızda girişte parolanın yanında doğrulama uygulamasındaki kod da istenir.</p>'
                + form('', 'totp_start', 'Kurulumu başlat'))
    else:
        totp = '<p>Bu ortamda şifreleme anahtarı tanımlı olmadığı için iki adımlı doğrulama henüz kurulamaz.</p>'
    if role == 'founder' and not (row and row[1]):
        totp = ('<p class="notice" role="alert">Kurucu hesabı için iki adımlı doğrulama önerilir; çevrimiçi parola '
                'sıfırlama kurucu hesabında yalnızca iki adımlı doğrulama açıkken çalışır.</p>') + totp
    phone = db.execute('SELECT e164,verified_at FROM security_phones WHERE area=? AND user_id=?', (area, uid)).fetchone()
    if sms_gateway() and _otp_service():
        status = ('<p>Kayıtlı numara yok.</p>' if not phone else
                  f'<p>{c.e(phone_module.mask(phone[0]))} — {"doğrulandı" if phone[1] else "doğrulanmadı"}</p>')
        phone_html = (status + form(c.inp('Telefon (ülke koduyla, ör. +90 532 ...)', 'phone', kind='tel')
                                    + (code_input if row and row[1] else ''), 'phone_save', 'Kod gönder')
                      + (form(c.inp('SMS kodu', 'sms_code'), 'phone_verify', 'Numarayı doğrula')
                         if phone and not phone[1] else ''))
    else:
        phone_html = ('<p>SMS sağlayıcısı henüz etkinleştirilmediği için telefon doğrulaması kapalı. Etkinleştirildiğinde '
                      'numaranızı buradan ekleyip doğrulayabileceksiniz.</p>')
    sessions = db.execute(f'SELECT COUNT(*) FROM {session_table} WHERE {owner}=? AND expires>?',
                          (_db_id(area, uid), int(time.time()))).fetchone()[0]
    session_html = (f'<p>Açık oturum sayısı (bu cihaz dahil): {sessions}.</p>'
                    + form('', 'sessions_revoke_others', 'Diğer bütün oturumları kapat'))
    return (c.card('İki adımlı doğrulama', totp) + c.card('Telefon numarası', phone_html)
            + c.card('Oturumlar', session_html))


EVENT_LABELS = {
    'totp_enabled': '2FA açıldı', 'totp_disabled': '2FA kapatıldı', 'recovery_code_used': 'Kurtarma kodu kullanıldı',
    'recovery_codes_regenerated': 'Kurtarma kodları yenilendi', 'reset_requested': 'Sıfırlama bağlantısı gönderildi',
    'reset_refused_no_2fa': 'Kurucu sıfırlaması reddedildi (2FA yok)', 'reset_mail_failed': 'Sıfırlama e-postası gönderilemedi',
    'password_reset_completed': 'Parola sıfırlandı', 'sessions_revoked': 'Oturumlar kapatıldı',
    'phone_verified': 'Telefon doğrulandı', 'break_glass_2fa_reset': 'Acil durum 2FA kurtarması',
}
CRITICAL_EVENTS = frozenset({'totp_disabled', 'break_glass_2fa_reset', 'password_reset_completed', 'reset_mail_failed'})


def notify_founder(handler, db, subject, body):
    """Critical-event notice to the founder (best effort, never blocks the action)."""
    row = db.execute("SELECT email FROM users WHERE role='founder' AND active=1").fetchone()
    if row:
        _notify(handler, row[0], subject, body)


def security_center(db, actor, query=None, server=None):
    from . import app as c
    query = query or {}
    now = int(time.time())
    day = now - 86400

    def one(sql, params=()):
        return db.execute(sql, params).fetchone()[0]
    blocked = one('SELECT COUNT(*) FROM security_login_failures WHERE blocked_until>?', (now,))
    failing = one('SELECT COUNT(*) FROM security_login_failures WHERE last_failure>?', (day,))
    devices = one('SELECT COUNT(*) FROM security_trusted_devices WHERE expires>?', (now,))
    control_sessions = one('SELECT COUNT(*) FROM sessions WHERE expires>?', (now,))
    sales_sessions = one('SELECT COUNT(*) FROM sales_sessions WHERE expires>?', (now,))
    resets = one('SELECT COUNT(*) FROM security_password_resets WHERE created>?', (now - 30 * 86400,))
    resets_done = one('SELECT COUNT(*) FROM security_password_resets WHERE used_at>?', (now - 30 * 86400,))
    phones = one('SELECT COUNT(*) FROM security_phones WHERE verified_at IS NOT NULL')
    users = db.execute(
        "SELECT u.id,u.name,u.role,t.enabled,p.verified_at,(SELECT COUNT(*) FROM security_recovery_codes r "
        "WHERE r.area='control' AND r.user_id=CAST(u.id AS TEXT) AND r.used_at IS NULL),"
        "(SELECT COUNT(*) FROM sessions s WHERE s.user_id=u.id AND s.expires>?) FROM users u "
        "LEFT JOIN security_totp t ON t.area='control' AND t.user_id=CAST(u.id AS TEXT) "
        "LEFT JOIN security_phones p ON p.area='control' AND p.user_id=CAST(u.id AS TEXT) "
        "WHERE u.active=1 ORDER BY CASE u.role WHEN 'founder' THEN 0 WHEN 'supervisor' THEN 1 ELSE 2 END,u.name",
        (now,)).fetchall()
    reps = db.execute(
        "SELECT a.id,a.name,t.enabled,(SELECT COUNT(*) FROM sales_sessions s WHERE s.account_id=a.id AND s.expires>?) "
        "FROM sales_accounts a LEFT JOIN security_totp t ON t.area='sales' AND t.user_id=a.id WHERE a.active=1 "
        "ORDER BY a.name", (now,)).fetchall()
    verifier = turnstile()

    # Alerts: what needs attention now (no secrets, no personal data beyond names already visible in CONTROL).
    alerts = []
    founder_rows = [u for u in users if u[2] == 'founder']
    if founder_rows and not founder_rows[0][3]:
        alerts.append(('Kritik', 'Kurucu hesabında iki adımlı doğrulama kapalı.'))
    if founder_rows and founder_rows[0][3] and founder_rows[0][5] <= 3:
        alerts.append(('Yüksek', f'Kurucu hesabında {founder_rows[0][5]} kurtarma kodu kaldı; yeni kod oluşturun.'))
    supervisors_without = sum(1 for u in users if u[2] == 'supervisor' and not u[3])
    if supervisors_without:
        alerts.append(('Orta', f'{supervisors_without} firma yöneticisi hesabında 2FA kapalı.'))
    if blocked:
        alerts.append(('Orta', f'Şu anda {blocked} giriş kovası hatalı denemeler nedeniyle kısıtlı.'))
    recent_critical = db.execute('SELECT kind,COUNT(*) FROM security_events WHERE at>? AND kind IN (' +
                                 ','.join('?' * len(CRITICAL_EVENTS)) + ') GROUP BY kind',
                                 (now - 7 * 86400, *sorted(CRITICAL_EVENTS))).fetchall()
    for kind, count in recent_critical:
        alerts.append(('Yüksek' if kind != 'password_reset_completed' else 'Bilgi',
                       f'Son 7 günde {count} kez: {EVENT_LABELS.get(kind, kind)}.'))
    if not two_factor_available():
        alerts.append(('Yüksek', '2FA şifreleme anahtarı tanımlı değil; kurulum yapılamaz.'))
    if reset_enabled() and verifier.mode != 'enforce':
        alerts.append(('Orta', 'Çevrimiçi parola sıfırlama açık ama Turnstile zorunlu değil.'))
    degraded = [area for area in ('control', 'sales')
                if server is not None and getattr(getattr(server, f'{area}_login_throttle', None), '_degraded_until', 0) > now]
    for area in degraded:
        alerts.append(('Yüksek', f'{"Sales Hub" if area == "sales" else "CONTROL"} giriş sayaçları geçici olarak bellekte '
                                 '(veritabanı yazılamadı).'))
    order = {'Kritik': 0, 'Yüksek': 1, 'Orta': 2, 'Bilgi': 3}
    alerts.sort(key=lambda a: order[a[0]])
    alert_html = (c.table(('Önem', 'Uyarı'), [(level, c.e(text)) for level, text in alerts]) if alerts
                  else '<p>Şu anda dikkat gerektiren bir güvenlik uyarısı yok.</p>')

    metric = lambda label, value: c.card(label, f'<strong>{c.e(value)}</strong>')
    metrics = ''.join((metric('Açık uyarı', len(alerts)), metric('Şu an kısıtlı giriş kovası', blocked),
                       metric('Son 24 saatte hatalı giriş olan kova', failing), metric('Güvenilir cihaz', devices),
                       metric('Açık CONTROL oturumu', control_sessions), metric('Açık Sales Hub oturumu', sales_sessions),
                       metric('Doğrulanmış telefon', phones), metric('Son 30 günde sıfırlama (istek / tamamlanan)',
                                                                    f'{resets} / {resets_done}')))
    try:
        db.execute('SELECT 1').fetchone()
        database = 'Erişilebilir'
    except Exception:
        database = 'Hata'
    status = c.table(('Bileşen', 'Durum'), [
        ('Veritabanı', database),
        ('Hesap bazlı giriş kısıtlama', 'Açık (kalıcı sayaç, güvenilir cihaz koruması)'
         + ('; bellek modunda: ' + ', '.join(degraded) if degraded else '')),
        ('İki adımlı doğrulama (TOTP)', 'Kurulabilir' if two_factor_available() else 'Şifreleme anahtarı yok'),
        ('Çevrimiçi parola sıfırlama', 'Açık' if reset_enabled() else 'Kapalı (SECURITY_PASSWORD_RESET)'),
        ('Cloudflare Turnstile', {'off': 'Kapalı', 'test': 'Test anahtarı', 'enforce': 'Zorunlu'}[verifier.mode]),
        ('SMS sağlayıcısı', 'Etkin' if sms_gateway() else 'Etkin değil'),
        ('Yedekleme', 'Render sürekli yedek (PITR); arşiv durumu Render panelinden ve WAL istatistiğinden izlenir'),
    ])
    yes = lambda v: 'Açık' if v else 'Kapalı'
    user_rows = [(c.e(u[1]), c.ROLES.get(u[2], u[2]), yes(u[3]), str(u[5]) if u[3] else '—',
                  'Doğrulandı' if u[4] else '—', str(u[6])) for u in users]
    rep_rows = [(c.e(r[1]), yes(r[2]), str(r[3])) for r in reps]

    area = query.get('alan', '')
    kind = query.get('olay', '')
    where, params = [], []
    if area in ('control', 'sales'):
        where.append('area=?')
        params.append(area)
    if kind in EVENT_LABELS:
        where.append('kind=?')
        params.append(kind)
    events = db.execute('SELECT at,area,kind,subject FROM security_events' + (' WHERE ' + ' AND '.join(where) if where else '')
                        + ' ORDER BY at DESC LIMIT 60', params).fetchall()
    event_rows = [(time.strftime('%d.%m.%Y %H:%M', time.gmtime(r[0] + 3 * 3600)), 'Sales Hub' if r[1] == 'sales' else 'CONTROL',
                   ('<strong>' + c.e(EVENT_LABELS.get(r[2], r[2])) + '</strong>') if r[2] in CRITICAL_EVENTS
                   else c.e(EVENT_LABELS.get(r[2], r[2])), f'#{c.e(r[3])}') for r in events]
    options = lambda current, items: ''.join(
        f'<option value="{c.e(k)}"{" selected" if k == current else ""}>{c.e(v)}</option>' for k, v in items)
    filters = ('<form method="get" action="/guvenlik-merkezi" class="filters"><label>Alan<select name="alan">'
               + options(area, (('', 'Tümü'), ('control', 'CONTROL'), ('sales', 'Sales Hub'))) + '</select></label>'
               '<label>Olay<select name="olay">' + options(kind, (('', 'Tümü'), *EVENT_LABELS.items())) + '</select></label>'
               '<button>Filtrele</button></form>')
    return (f'<div class="stats metrics">{metrics}</div>'
            + c.card('Uyarılar', alert_html)
            + c.card('Güvenlik bileşenleri', status)
            + c.card('CONTROL kullanıcılarının güvenlik durumu',
                     c.table(('Ad', 'Rol', '2FA', 'Kalan kurtarma kodu', 'Telefon', 'Açık oturum'), user_rows))
            + c.card('Sales Hub temsilcilerinin güvenlik durumu', c.table(('Ad', '2FA', 'Açık oturum'), rep_rows)
                     if rep_rows else '<p>Etkin temsilci yok.</p>')
            + c.card('Güvenlik olayları', filters + c.table(('Zaman (TSİ)', 'Alan', 'Olay', 'Hesap no'), event_rows)
                     + '<p><small>Olay kayıtları değiştirilemez ve silinemez. Parola, kod ve anahtarlar bu ekranda '
                       'hiçbir zaman gösterilmez. Kritik olaylarda kurucuya e-posta bildirimi gider.</small></p>'))


# ---------------------------------------------------------------- break-glass recovery (infrastructure access only)

def break_glass_reset(target, area, address, operator, reason):
    """Remove 2FA from one account and close its sessions. For the case where the owner lost the
    authenticator AND every recovery code. Runs only from a shell with direct database access
    (Render dashboard + shell, itself protected by the Render account's own 2FA); there is no web
    route for it. Leaves an append-only security event and the owner keeps their password."""
    from . import app as c
    address = str(address or '').strip().lower()
    if not reason or len(reason.strip()) < 10:
        raise ValueError('reason must describe the verified identity check (min. 10 characters)')
    db = c.connect(target)
    try:
        if area == 'sales':
            row = db.execute('SELECT id FROM sales_accounts WHERE email=?', (address,)).fetchone()
        else:
            row = db.execute('SELECT id FROM users WHERE email=?', (address,)).fetchone()
        if not row:
            raise LookupError('account not found')
        uid = str(row[0])
        session_table, owner, _ = _area_tables(area)
        db.execute('DELETE FROM security_totp WHERE area=? AND user_id=?', (area, uid))
        db.execute('DELETE FROM security_recovery_codes WHERE area=? AND user_id=?', (area, uid))
        db.execute(f'DELETE FROM {session_table} WHERE {owner}=?', (_db_id(area, uid),))
        log_event(db, area, 'break_glass_2fa_reset', uid, f'operator={operator[:40]}; reason={reason.strip()[:200]}')
        db.commit()
        return uid
    finally:
        db.close()


def _main(argv=None):
    import argparse
    import getpass
    import sys
    from . import postgres
    parser = argparse.ArgumentParser(description='ERNASA break-glass 2FA recovery (requires direct database access).')
    parser.add_argument('command', choices=['recover-2fa'])
    parser.add_argument('--area', choices=['control', 'sales'], default='control')
    parser.add_argument('--email', required=True)
    parser.add_argument('--reason', required=True, help='How the owner\'s identity was verified (recorded).')
    parser.add_argument('--db-path', default='')
    args = parser.parse_args(argv)
    dsn = os.getenv('ERNASA_CONTROL_DATABASE_URL', '')
    target = postgres.Target(dsn) if dsn and not args.db_path else args.db_path
    if not target:
        parser.error('ERNASA_CONTROL_DATABASE_URL or --db-path is required')
    answer = input(f'Type the e-mail again to remove 2FA from {args.email}: ') if sys.stdin.isatty() else args.email
    if answer.strip().lower() != args.email.strip().lower():
        print('Cancelled.')
        return 1
    uid = break_glass_reset(target, args.area, args.email, getpass.getuser(), args.reason)
    print(f'2FA removed and sessions closed for account #{uid}. The owner signs in with the password and '
          'should enrol a new authenticator immediately.')
    return 0


if __name__ == '__main__':
    raise SystemExit(_main())
