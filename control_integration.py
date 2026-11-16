"""CONTROL-backed access for the local company portal."""
from base64 import b64decode
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from ipaddress import ip_address
import json
import os
import re
import secrets
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request as URLRequest, build_opener

from fastapi import HTTPException, Request

from database import get_connection

COOKIE = 'ernasa_company_session'
STAFF_ROLES = ('supervisor', 'hr_manager', 'hr_staff')
PDF_ROLES = ('supervisor', 'hr_manager')


@dataclass(frozen=True)
class Portal:
    slug: str
    origin: str


@dataclass(frozen=True)
class Actor:
    company_id: int | None = None
    role: str = 'legacy'
    portal: Portal | None = None
    user_id: int | None = None
    access_version: str = ''
    company_name: str | None = None
    user_name: str | None = None
    products: tuple[str, ...] = ()
    interview_quota: int | None = None
    month_bounds: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()


def portal_for(request):
    host = request.headers.get('host', '').lower()
    authority, _, port = host.partition(':')
    match = re.fullmatch(r'([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\.ernasa\.com', authority)
    enabled = bool(os.getenv('ERNASA_IK_CONTROL_URL'))
    if match and match[1] != 'www':
        expected_port = os.getenv('ERNASA_IK_PORTAL_PORT', '')
        if not enabled or port != expected_port or host != authority + (f':{expected_port}' if expected_port else ''):
            raise HTTPException(404, 'Firma adresi bulunamadı.')
        scheme = 'http' if expected_port else 'https'
        return Portal(match[1], f'{scheme}://{host}')
    if enabled and authority not in ('127.0.0.1', 'localhost', 'ernasa.com', 'www.ernasa.com'):
        raise HTTPException(404, 'Adres bulunamadı.')
    return None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def control_call(portal, operation, **fields):
    url = urlsplit(os.getenv('ERNASA_IK_CONTROL_URL', ''))
    key = os.getenv('ERNASA_IK_CONTROL_KEY', '')
    try:
        allowed = url.scheme == 'http' and ip_address(url.hostname).is_loopback and url.port
    except (TypeError, ValueError):
        allowed = False
    if not allowed or url.username or url.password or url.path or url.query or url.fragment or len(key) < 32:
        raise HTTPException(503, 'Firma doğrulaması yapılandırılmamış.')
    payload = json.dumps({'operation': operation, 'slug': portal.slug, **fields}).encode()
    request = URLRequest(url.geturl() + '/internal/ik', data=payload,
                      headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {key}'})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=4) as response:
            result = json.loads(response.read(2_100_001))
    except (URLError, OSError, ValueError) as error:
        raise HTTPException(503, 'Firma doğrulaması şu anda kullanılamıyor.') from error
    if 'error' in result:
        error = result['error']
        raise HTTPException(error['status'], error['detail'])
    return result


def check_origin(request, portal):
    if request.headers.get('origin') != portal.origin:
        raise HTTPException(403, 'İstek kaynağı geçersiz.')


def token_hash(token):
    return sha256(token.encode()).hexdigest()


def new_session(portal, identity):
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        cursor.execute('DELETE FROM ik_portal_sessions WHERE expires_at<=?', (now.isoformat(),))
        cursor.execute('INSERT INTO ik_portal_sessions VALUES (?,?,?,?,?,?)',
                       (token_hash(token), portal.slug, identity['company_id'], identity['user_id'],
                        identity['version'], (now + timedelta(hours=8)).isoformat()))
        connection.commit()
    return token


def revoke_session(portal, token):
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        cursor.execute('DELETE FROM ik_portal_sessions WHERE token_hash=? AND slug=?', (token_hash(token), portal.slug))
        connection.commit()


def session_actor(request, portal):
    token = request.cookies.get(COOKIE, '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        return None
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        cursor.execute('SELECT * FROM ik_portal_sessions WHERE token_hash=? AND slug=? AND expires_at>?',
                       (token_hash(token), portal.slug, datetime.now(timezone.utc).isoformat()))
        row = cursor.fetchone()
    if not row:
        return None
    identity = control_call(portal, 'session', id=str(row['user_id']))
    if (identity.get('company_id') != row['company_id'] or identity.get('version') != row['access_version']
            or identity.get('role') not in STAFF_ROLES or identity.get('must_change_password')):
        revoke_session(portal, token)
        return None
    return Actor(row['company_id'], identity['role'], portal, row['user_id'], row['access_version'],
                 identity.get('company_name'), identity.get('user_name'), tuple(identity['products']),
                 identity.get('interview_quota'), tuple(identity['month_bounds']), tuple(identity.get('permissions', ())))


def personnel_call(actor, operation, **fields):
    return control_call(actor.portal, operation, actor_id=str(actor.user_id),
                        actor_version=actor.access_version, **fields)


def candidate_company(request: Request):
    portal = portal_for(request)
    if not portal:
        return None
    if request.method == 'POST':
        check_origin(request, portal)
    identity = control_call(portal, 'resolve')
    if not identity.get('company_id') or 'ai' not in identity.get('products', ()):
        raise HTTPException(404, 'Mülakat bağlantısı bulunamadı.')
    return identity['company_id']


def logo(portal, company_id):
    if not portal:
        return None
    if control_call(portal, 'resolve').get('company_id') != company_id:
        raise HTTPException(404, 'Firma logosu bulunamadı.')
    result = control_call(portal, 'logo', id=str(company_id))
    return b64decode(result['png'], validate=True) if result.get('png') else None
