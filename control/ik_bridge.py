from base64 import b64encode
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import hmac
import os
import re

ROLES = ('supervisor', 'hr_manager', 'hr_staff')
ATS_PRODUCT = 'ernasa-ats'
STAFF_GRANTS = ('ats_schedule', 'ats_stage')
try:
    ISTANBUL = ZoneInfo('Europe/Istanbul')
except ZoneInfoNotFoundError:
    with Path(__file__).with_name('Europe-Istanbul.tzif').open('rb') as zone_file:
        ISTANBUL = ZoneInfo.from_file(zone_file, key='Europe/Istanbul')

SCHEMA = """
CREATE TABLE IF NOT EXISTS company_portals(
 company_id INTEGER PRIMARY KEY REFERENCES companies(id), slug TEXT NOT NULL UNIQUE COLLATE NOCASE,
 version TEXT NOT NULL DEFAULT (lower(hex(randomblob(16)))));
CREATE TABLE IF NOT EXISTS ik_user_roles(
 user_id INTEGER PRIMARY KEY REFERENCES users(id), role TEXT NOT NULL CHECK(role IN ('supervisor','hr_manager','hr_staff')),
 version TEXT NOT NULL DEFAULT (lower(hex(randomblob(16)))));
CREATE TABLE IF NOT EXISTS ats_subscriptions(
 company_id INTEGER PRIMARY KEY REFERENCES companies(id), package TEXT NOT NULL,
 starts TEXT NOT NULL, ends TEXT NOT NULL, status TEXT NOT NULL, user_quota INTEGER);
INSERT OR IGNORE INTO products(code,name) VALUES('ernasa-ats','ERNASA ATS');
CREATE TABLE IF NOT EXISTS ik_staff_permissions(
 user_id INTEGER NOT NULL REFERENCES users(id), code TEXT NOT NULL CHECK(code IN ('ats_schedule','ats_stage')),
 PRIMARY KEY(user_id,code));
CREATE TRIGGER IF NOT EXISTS ats_subscription_insert AFTER INSERT ON ats_subscriptions
 BEGIN UPDATE company_portals SET version=lower(hex(randomblob(16))) WHERE company_id=NEW.company_id; END;
CREATE TRIGGER IF NOT EXISTS ats_subscription_update AFTER UPDATE OF status,starts,ends ON ats_subscriptions
 WHEN OLD.status<>NEW.status OR OLD.starts<>NEW.starts OR OLD.ends<>NEW.ends
 BEGIN UPDATE company_portals SET version=lower(hex(randomblob(16))) WHERE company_id=NEW.company_id; END;
CREATE TABLE IF NOT EXISTS ik_personnel_details(
 user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 created_at TEXT, last_login_at TEXT,
 must_change_password INTEGER NOT NULL DEFAULT 0 CHECK(must_change_password IN (0,1)));
INSERT OR IGNORE INTO ik_personnel_details(user_id,created_at)
 SELECT u.id,MIN(a.stamp) FROM users u JOIN ik_user_roles r ON r.user_id=u.id
 JOIN audit a ON a.company_id=u.company_id AND a.action IN (
    'Süpervizör atandı #' || u.id, 'İK hesabı oluşturuldu #' || u.id,
    'Personel kaydedildi #' || u.id)
 GROUP BY u.id;
CREATE TRIGGER IF NOT EXISTS ik_user_created AFTER INSERT ON users
 BEGIN INSERT INTO ik_personnel_details(user_id,created_at) VALUES(NEW.id,CURRENT_TIMESTAMP); END;
CREATE TRIGGER IF NOT EXISTS ik_company_change AFTER UPDATE OF status ON companies WHEN OLD.status<>NEW.status
 BEGIN UPDATE company_portals SET version=lower(hex(randomblob(16))) WHERE company_id=NEW.id; END;
CREATE TRIGGER IF NOT EXISTS ik_subscription_change AFTER UPDATE ON subscriptions
 WHEN OLD.status<>NEW.status OR OLD.starts<>NEW.starts OR OLD.ends<>NEW.ends
 BEGIN UPDATE company_portals SET version=lower(hex(randomblob(16))) WHERE company_id=NEW.company_id; END;
CREATE TRIGGER IF NOT EXISTS ik_user_change AFTER UPDATE ON users
 WHEN OLD.active<>NEW.active OR OLD.secret<>NEW.secret OR OLD.role<>NEW.role OR OLD.company_id IS NOT NEW.company_id
 BEGIN UPDATE ik_user_roles SET version=lower(hex(randomblob(16))) WHERE user_id=NEW.id; END;
CREATE TRIGGER IF NOT EXISTS ik_product_change AFTER UPDATE OF product ON service_terms
 WHEN OLD.product IS NOT NEW.product
 BEGIN UPDATE company_portals SET version=lower(hex(randomblob(16))) WHERE company_id=NEW.company_id; END;
CREATE TRIGGER IF NOT EXISTS ik_role_change AFTER UPDATE OF role ON ik_user_roles
 WHEN OLD.role IS NOT NEW.role
 BEGIN UPDATE ik_user_roles SET version=lower(hex(randomblob(16))) WHERE user_id=NEW.user_id; END;
"""


def today():
    return datetime.now(ISTANBUL).date().isoformat()


def month_bounds(now=None):
    current = (now or datetime.now(timezone.utc)).astimezone(ISTANBUL)
    starts = datetime(current.year, current.month, 1, tzinfo=ISTANBUL)
    ends = datetime(current.year + (current.month == 12), current.month % 12 + 1, 1, tzinfo=ISTANBUL)
    return [value.astimezone(timezone.utc).isoformat() for value in (starts, ends)]


def slug(value):
    value = value.strip().lower().removesuffix('.ernasa.com')
    reserved = {'www', 'api', 'admin', 'control', 'ik', 'mail', 'ftp', 'static', 'assets', 'cdn', 'localhost', 'status', 'app'}
    if value and (value in reserved or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', value)):
        raise ValueError('Firma portal adresi geçersiz veya ayrılmış.')
    return value or None


def authenticated(header):
    key = os.getenv('ERNASA_IK_CONTROL_KEY', '')
    return len(key) >= 32 and hmac.compare_digest(header.encode(), f'Bearer {key}'.encode())


def active_portal(db, name):
    date = today()
    return db.execute("""
        SELECT c.id,c.name,p.version,s.company_id IS NOT NULL AS ai_allowed,
               a.company_id IS NOT NULL AS ats_allowed,t.operation_quota AS interview_quota
        FROM company_portals p JOIN companies c ON c.id=p.company_id
        JOIN company_profile f ON f.company_id=c.id
        LEFT JOIN service_terms t ON t.company_id=c.id AND t.product='ernasa-ik'
        LEFT JOIN subscriptions s ON s.company_id=t.company_id AND s.status='Aktif' AND s.starts<=? AND s.ends>=?
        LEFT JOIN ats_subscriptions a ON a.company_id=c.id AND a.status='Aktif' AND a.starts<=? AND a.ends>=?
        WHERE p.slug=? AND c.status='Aktif' AND f.activated=1
          AND (s.company_id IS NOT NULL OR a.company_id IS NOT NULL)
    """, (date, date, date, date, slug(name))).fetchone()


def products(portal):
    return [product for product, allowed in (('ai',portal['ai_allowed']),('ats',portal['ats_allowed'])) if allowed]


def active_user_count(db, tenant):
    return db.execute("""SELECT count(*) FROM users u JOIN ik_user_roles r ON r.user_id=u.id
        WHERE u.company_id=? AND u.active=1""",(tenant,)).fetchone()[0]


def subscription_details(db, tenant):
    ai = db.execute("""SELECT s.package,s.starts,s.ends,s.status,t.user_quota,t.operation_quota
        FROM subscriptions s JOIN service_terms t USING(company_id)
        WHERE s.company_id=? AND t.product='ernasa-ik'""",(tenant,)).fetchone()
    ats = db.execute("SELECT package,starts,ends,status,user_quota FROM ats_subscriptions WHERE company_id=?",
                     (tenant,)).fetchone()
    return {key:{**dict(row),'product': code} for key,code,row in
            (('ai','ernasa-ik',ai),('ats',ATS_PRODUCT,ats)) if row is not None}


def portal_user(db, portal, key, value):
    if not portal:
        return None
    column = 'email' if key == 'email' else 'id'
    return db.execute(f"""
        SELECT u.id,u.name,u.email,u.secret,r.role,r.version,
            COALESCE(d.must_change_password,0) AS must_change_password
        FROM users u JOIN ik_user_roles r ON r.user_id=u.id
        LEFT JOIN ik_personnel_details d ON d.user_id=u.id
        WHERE u.{column}=? AND u.company_id=? AND u.active=1 AND
            ((u.role='supervisor' AND r.role='supervisor') OR
             (u.role='staff' AND r.role IN ('hr_manager','hr_staff')))
    """, (value, portal['id'])).fetchone()


def identity_version(portal, user):
    return portal['version'] + user['version']


def respond(db, data, verify):
    operation = data.get('operation')
    portal = active_portal(db, data.get('slug', ''))
    if operation == 'resolve':
        return {'company_id': portal['id'], 'name': portal['name'], 'products':products(portal)} if portal else {}
    if operation == 'logo':
        row = None
        if portal and str(portal['id']) == data.get('id'):
            row = db.execute('SELECT png FROM company_logos WHERE company_id=?', (portal['id'],)).fetchone()
        return {'png': b64encode(row['png']).decode()} if row else {}
    if operation == 'interview_template':
        if portal and str(portal['id']) == data.get('id') and 'ai' in products(portal):
            from .interview_templates import resolve
            return resolve(db,portal['id'],data.get('position_id',''))
        return {}
    if operation not in ('login', 'session', 'usage', 'assignees', 'history'):
        return {}
    key = 'email' if operation == 'login' else 'id'
    user = portal_user(db, portal, key, data.get(key, ''))
    if operation == 'login' and not verify(data.get('password', ''), user['secret'] if user else None):
        return {}
    if not user:
        return {}
    if operation in ('usage', 'assignees', 'history'):
        if user['role'] not in ('supervisor','hr_manager') or not hmac.compare_digest(
                identity_version(portal,user), data.get('version','')):
            return {}
        if operation == 'assignees':
            roles = "'hr_staff'" if data.get('scope') != 'reviewers' else "'hr_staff','hr_manager','supervisor'"
            return {'users':[dict(row) for row in db.execute(f"""SELECT u.id,u.name
                FROM users u JOIN ik_user_roles r ON r.user_id=u.id WHERE u.company_id=?
                AND u.active=1 AND r.role IN ({roles}) ORDER BY u.name""",(portal['id'],))]}
        if operation == 'history':
            return {'events':[dict(row) for row in db.execute('''SELECT id,actor,action,stamp FROM audit
                    WHERE company_id=? AND action NOT LIKE 'İK personel listesi%'
                    ORDER BY id DESC LIMIT 100''',(portal['id'],))],
                    'actors':{str(row['id']):row['name'] for row in db.execute(
                        'SELECT id,name FROM users WHERE company_id=?',(portal['id'],))}}
        return {'plans':subscription_details(db,portal['id']), 'active_users':active_user_count(db,portal['id'])}
    permissions = [row[0] for row in db.execute('SELECT code FROM ik_staff_permissions WHERE user_id=?',(user['id'],))]
    return {'company_id': portal['id'], 'user_id': user['id'], 'role': user['role'],
            'company_name': portal['name'], 'user_name': user['name'], 'version': identity_version(portal, user),
            'must_change_password': bool(user['must_change_password']), 'products':products(portal),
            'interview_quota':portal['interview_quota'], 'month_bounds':month_bounds(), 'permissions':permissions}
