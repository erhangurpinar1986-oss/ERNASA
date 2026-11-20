import psycopg2
from . import postgres

import argparse, getpass, hashlib, hmac, html, re, secrets, sqlite3, threading, time, warnings
import json
import os
from base64 import b64decode
from email.parser import BytesParser
from email.policy import default as email_policy
from . import address_catalog, firma, ik_bridge, interview_templates, sales, sales_http
from contextlib import closing
from datetime import date, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
NAV = dict(zip(('/', '/firmalar', '/supervizorler', '/abonelikler', '/kullanicilar', '/roller', '/yetkiler', '/denetim', '/ayarlar'),
               ('Genel Bakış', 'Firmalar', 'Süpervizörler', 'Abonelikler', 'Kullanıcılar', 'Roller', 'Yetkiler', 'Denetim Kayıtları', 'Sistem Ayarları')))
NAV['/mulakat-sablonlari'] = 'Mülakat Şablonları'
ERNASA_INVITE_PATH = '/ernasa-aday-davetleri'
ERNASA_INVITE_SLUG = 'ernasa-davet'
NAV[ERNASA_INVITE_PATH] = 'ERNASA Aday Davetleri'
NAV['/aday-deneyimi'] = 'Aday Deneyimi'
NAV['/satis'] = 'Satış Yönetimi'
ROLES = {'founder':'Kurucu', 'supervisor':'Firma Süpervizörü', 'staff':'Personel'}
ACCESS = {'founder':set(NAV), 'supervisor':{'/', '/kullanicilar'}, 'staff':{'/'}}
STAFF_PERMISSION = 'subscription_read'
PACKAGES = ('Başlangıç', 'Profesyonel', 'Kurumsal')
STATES = ('Aktif', 'Askıda')
SUB = STATES + ('Bekliyor', 'Sona erdi')
TTL = 28800
LOGIN_CSRF_TTL = 600
GROUPS = (('Platform', tuple(NAV)[:4]),
          ('Yönetim', tuple(NAV)[4:7]),
          ('Güvenlik ve Sistem', tuple(route for route in NAV if route not in tuple(NAV)[:7] and route != '/satis')),
          ('Satış ve İş Geliştirme', ('/satis',)))
ICONS = (
    'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
    'M5 21V5h14v16 M9 9h1m4 0h1m-6 4h1m4 0h1m-6 4h1m4 0h1 M3 21h18',
    'M12 3l7 3v5c0 5-3 8-7 10-4-2-7-5-7-10V6z M9 12l2 2 4-4',
    'M3 6h18v13H3z M3 10h18 M7 15h4',
    'M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M4 21v-2a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v2',
    'M12 3l9 5-9 5-9-5z M3 12l9 5 9-5 M3 16l9 5 9-5',
    'M15 3a5 5 0 1 0 0 10 5 5 0 0 0 0-10z M11 12l-8 8v-4l5-5',
    'M6 3h12v18H6z M9 8h6 M9 12h6 M9 16h4',
    'M4 6h16 M4 12h16 M4 18h16 M8 4v4m8 2v4m-6 2v4',
)
ASSETS = {'/control.css': ('control.css', 'text/css; charset=utf-8'),
          '/sales-menu.js': ('sales-menu.js', 'application/javascript; charset=utf-8'),
          '/design-system.css': ('design-system.css', 'text/css; charset=utf-8'),
          '/ernasalogo.png': ('ernasalogo.png', 'image/png')}
SCHEMA = """
CREATE TABLE IF NOT EXISTS companies(id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, secret TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('founder','supervisor','staff')), company_id INTEGER REFERENCES companies,
 active INTEGER NOT NULL DEFAULT 1, CHECK((role='founder')=(company_id IS NULL)));
CREATE UNIQUE INDEX IF NOT EXISTS one_founder ON users(role) WHERE role='founder';
CREATE TABLE IF NOT EXISTS subscriptions(company_id INTEGER PRIMARY KEY REFERENCES companies,
 package TEXT NOT NULL, starts TEXT NOT NULL, ends TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users, csrf TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
 company_id INTEGER REFERENCES companies, stamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS delegation_limits(company_id INTEGER PRIMARY KEY REFERENCES companies);
CREATE TABLE IF NOT EXISTS staff_subscription_access(user_id INTEGER PRIMARY KEY REFERENCES users);
INSERT INTO settings VALUES(1,'ERNASA CONTROL') ON CONFLICT(id) DO NOTHING;
"""


class Problem(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def connect(path):
    if isinstance(path, postgres.Target):
        return postgres.Connection(path)
    db = sqlite3.connect(path, timeout=8)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db


def init(path):
    with closing(connect(path)) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(SCHEMA)
        db.executescript('BEGIN IMMEDIATE;'+firma.DDL+ik_bridge.SCHEMA+interview_templates.SCHEMA+sales.DDL+sales.SQLITE_GUARDS+'COMMIT;')


def digest(password, salt=None):
    salt = salt if salt is not None else secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1, maxmem=64*1024**2)
    return salt.hex()+':'+key.hex()


DUMMY = digest('synthetic timing account')


def check_ik_password(password,saved):
    if not 1 <= len(password) <= 128:
        return False
    encoded = saved or DUMMY
    valid = hmac.compare_digest(encoded,digest(password,bytes.fromhex(encoded.split(':')[0])))
    return bool(saved) and valid


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def field(data, key, limit=120):
    value = data.get(key, '').strip()
    if not 1 <= len(value) <= limit:
        raise Problem(400, 'Zorunlu alanları ve uzunluk sınırlarını kontrol edin.')
    return value


def email(data):
    value = field(data, 'email', 254).lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
        raise Problem(400, 'Geçerli bir e-posta girin.')
    return value


def new_secret(data):
    password = data.get('password', '')
    if not 12 <= len(password) <= 128:
        raise Problem(400, 'Parola 12–128 karakter olmalıdır.')
    return digest(password)


def choice(data, key, values):
    result = data.get(key)
    if result not in values:
        raise Problem(400, 'Geçersiz seçenek.')
    return result


def number(data, key='id', optional=False):
    value = data.get(key, '')
    if optional and not value:
        return None
    if not re.fullmatch('[1-9][0-9]{0,17}', value):
        raise Problem(400, 'Geçersiz kayıt numarası.')
    return int(value)


def require(row, label):
    if row is None:
        raise Problem(404, label+' bulunamadı.')
    return row


def company(db, target):
    return require(db.execute('SELECT * FROM companies WHERE id=?',(target,)).fetchone(),'Firma')


def audit(db, actor, action, tenant=None):
    db.execute('INSERT INTO audit(actor,action,company_id) VALUES(?,?,?)',(actor['email'],action,tenant))


def add_user(db,data,role,tenant):
    return db.execute('INSERT INTO users(email,name,secret,role,company_id) VALUES(?,?,?,?,?) RETURNING id',
                      (email(data),field(data,'name'),new_secret(data),role,tenant)).fetchone()[0]


MANAGED_IK_ROLES = ('hr_manager','hr_staff')


def require_available_seat(db,tenant):
    today = ik_bridge.today()
    limits = [plan['user_quota'] for plan in ik_bridge.subscription_details(db,tenant).values()
              if plan['status']=='Aktif' and plan['starts']<=today<=plan['ends'] and plan['user_quota'] is not None]
    if limits and ik_bridge.active_user_count(db,tenant) >= min(limits):
        raise Problem(409,'Aktif kullanıcı kotası doldu.')


def portal_actor(db,data):
    portal = ik_bridge.active_portal(db,data.get('slug',''))
    actor = ik_bridge.portal_user(db,portal,'id',number(data,'actor_id'))
    if (not actor
            or not hmac.compare_digest(ik_bridge.identity_version(portal,actor),data.get('actor_version',''))):
        raise Problem(403,'Firma oturumu geçersiz.')
    return portal, actor


def test_deliver_setup(address,password):
    directory = os.getenv('ERNASA_IK_ISOLATED_OUTBOX','')
    if os.getenv('ERNASA_IK_LOCAL_PREVIEW') != '1' or not directory or not Path(directory).is_dir():
        raise Problem(503,'Parola kurulum teslimatı yapılandırılmamış.')
    if not address.endswith('.test'):
        raise Problem(400,'İzole testte yalnızca .test e-posta adresleri kullanılabilir.')
    with (Path(directory) / (secrets.token_hex(16)+'.json')).open('x',encoding='utf-8') as message:
        json.dump({'recipient':address,'one_time_password':password},message)


def portal_personnel(db,data):
    portal, actor = portal_actor(db,data)
    if actor['role'] != 'supervisor':
        raise Problem(403,'Bu işlem firma süpervizörü yetkisi gerektirir.')
    tenant = portal['id']
    operation = data['operation']
    if operation == 'personnel_list':
        rows = [dict(row) for row in db.execute("""
            SELECT u.id,u.name,u.email,r.role,u.active,d.created_at,d.last_login_at,
                (SELECT group_concat(g.code) FROM ik_staff_permissions g WHERE g.user_id=u.id) AS permissions
            FROM users u JOIN ik_user_roles r ON r.user_id=u.id
            LEFT JOIN ik_personnel_details d ON d.user_id=u.id
            WHERE u.company_id=? AND ((u.role='supervisor' AND r.role='supervisor')
                OR (u.role='staff' AND r.role IN ('hr_manager','hr_staff')))
            ORDER BY u.name COLLATE NOCASE,u.id
        """,(tenant,))]
        active_count = sum(row['active'] for row in rows)
        audit(db,actor,'İK personel listesi görüntülendi',tenant)
        return {'users':rows,'total':len(rows),'active':active_count,'inactive':len(rows)-active_count}

    if operation == 'personnel_create':
        require_available_seat(db,tenant)
        role = choice(data,'role',MANAGED_IK_ROLES)
        password = secrets.token_urlsafe(24)
        uid = add_user(db,{**data,'password':password},'staff',tenant)
        db.execute('INSERT INTO ik_user_roles(user_id,role) VALUES(?,?)',(uid,role))
        db.execute('UPDATE ik_personnel_details SET must_change_password=1 WHERE user_id=?',(uid,))
        test_deliver_setup(email(data),password)
        audit(db,actor,f'İK personel hesabı oluşturuldu #{uid}: {role}',tenant)
        return {'id':uid,'password_setup_required':True}

    uid = number(data)
    if uid == actor['id']:
        raise Problem(403,'Süpervizör kendi hesabını bu bölümden değiştiremez.')
    target = require(db.execute("""
        SELECT u.id,u.name,u.email,u.active,r.role FROM users u JOIN ik_user_roles r ON r.user_id=u.id
        WHERE u.id=? AND u.company_id=? AND u.role='staff' AND r.role IN ('hr_manager','hr_staff')
    """,(uid,tenant)).fetchone(),'Personel')
    if operation == 'personnel_reset':
        password = secrets.token_urlsafe(24)
        db.execute('UPDATE users SET secret=? WHERE id=? AND company_id=?',(digest(password),uid,tenant))
        db.execute("""INSERT INTO ik_personnel_details(user_id,must_change_password) VALUES(?,1)
            ON CONFLICT(user_id) DO UPDATE SET must_change_password=1""",(uid,))
        db.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
        test_deliver_setup(target['email'],password)
        audit(db,actor,f'İK personel parolası yenilendi #{uid}',tenant)
        return {'id':uid,'password_setup_required':True}
    if operation == 'personnel_grants':
        if 'ats' not in ik_bridge.products(portal) or target['role'] != 'hr_staff':
            raise Problem(403,'ATS ek izinleri yalnızca İK Personeline verilebilir.')
        codes = sorted(set(filter(None,data.get('permissions','').split(','))))
        if any(code not in ik_bridge.STAFF_GRANTS for code in codes):
            raise Problem(400,'Geçersiz ek izin.')
        db.execute('DELETE FROM ik_staff_permissions WHERE user_id=?',(uid,))
        db.executemany('INSERT INTO ik_staff_permissions VALUES(?,?)',[(uid,code) for code in codes])
        db.execute('UPDATE ik_user_roles SET version=lower(hex(randomblob(16))) WHERE user_id=?',(uid,))
        db.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
        audit(db,actor,f'ATS personel ek izinleri değiştirildi #{uid}',tenant)
        return {'success':True}
    if operation != 'personnel_update':
        raise Problem(400,'Geçersiz personel işlemi.')
    role = choice(data,'role',MANAGED_IK_ROLES)
    active = int(choice(data,'active',('0','1')))
    if active and not target['active']:
        require_available_seat(db,tenant)
    db.execute('UPDATE users SET name=?,email=?,active=? WHERE id=? AND company_id=?',
               (field(data,'name'),email(data),active,uid,tenant))
    if role != target['role']:
        db.execute('UPDATE ik_user_roles SET role=? WHERE user_id=?',(role,uid))
        db.execute('DELETE FROM ik_staff_permissions WHERE user_id=?',(uid,))
        audit(db,actor,f'İK personel yetkisi değiştirildi #{uid}: {target["role"]} → {role}',tenant)
    if active != target['active']:
        if not active:
            db.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
        audit(db,actor,f'İK personel hesabı {"etkinleştirildi" if active else "pasife alındı"} #{uid}',tenant)
    audit(db,actor,f'İK personel bilgileri güncellendi #{uid}',tenant)
    return {'success':True}


def portal_account_operation(db,data):
    portal, actor = portal_actor(db,data)
    tenant = portal['id']
    operation = data['operation']
    if operation == 'change_own_password':
        current, replacement = data.get('password',''), data.get('new_password','')
        if not check_ik_password(current,actor['secret']):
            raise Problem(400,'Mevcut parola doğrulanamadı.')
        if current == replacement:
            raise Problem(400,'Yeni parola mevcut paroladan farklı olmalıdır.')
        db.execute('UPDATE users SET secret=? WHERE id=? AND company_id=?',
                   (new_secret({'password':replacement}),actor['id'],tenant))
        db.execute('DELETE FROM sessions WHERE user_id=?',(actor['id'],))
        audit(db,actor,'Kullanıcı kendi İK parolasını değiştirdi',tenant)
        return {'success':True}
    if operation == 'self':
        return {key:actor[key] for key in ('name','email','role')}
    if actor['role'] not in ('supervisor','hr_manager'):
        raise Problem(403,'Firma yöneticisi yetkisi gerekiyor.')
    if operation == 'company_settings':
        profile = db.execute("SELECT sector,brand FROM company_profile WHERE company_id=?",(tenant,)).fetchone()
        return {'name':portal['name'],'sector':profile['sector'],'brand':profile['brand']}
    if actor['role'] != 'supervisor':
        raise Problem(403,'Firma süpervizörü yetkisi gerekiyor.')
    if operation == 'company_sector':
        sector = field(data,'sector',100)
        db.execute('UPDATE company_profile SET sector=? WHERE company_id=?',(sector,tenant))
        audit(db,actor,'Firma sektörü güncellendi',tenant)
    elif operation == 'company_logo':
        try:
            png = firma.logo_png(b64decode(data.get('image',''),validate=True))
        except ValueError:
            raise Problem(400,'En fazla 500 KB geçerli PNG veya JPEG yükleyin.') from None
        db.execute('INSERT INTO company_logos(company_id,png) VALUES(?,?) ON CONFLICT(company_id) DO UPDATE SET png=excluded.png',(tenant,png))
        audit(db,actor,'İK portalından firma logosu güncellendi',tenant)
    return {'success':True}


def portal_sign_in(db,data):
    identity = ik_bridge.respond(db,data,check_ik_password)
    if not identity:
        return {}
    uid = identity['user_id']
    replacement = data.get('new_password','')
    if identity['must_change_password']:
        if not replacement:
            return identity
        if replacement == data.get('password'):
            raise Problem(400,'Yeni parola geçici paroladan farklı olmalıdır.')
        db.execute('UPDATE users SET secret=? WHERE id=?',(new_secret({'password':replacement}),uid))
        db.execute('UPDATE ik_personnel_details SET must_change_password=0 WHERE user_id=?',(uid,))
        portal = ik_bridge.active_portal(db,data['slug'])
        user = ik_bridge.portal_user(db,portal,'id',uid)
        identity.update(version=ik_bridge.identity_version(portal,user),must_change_password=False)
        audit(db,user,f'İK geçici parolası değiştirildi #{uid}',identity['company_id'])
    elif replacement:
        raise Problem(400,'Bu hesap için geçici parola değişikliği gerekmiyor.')

    db.execute("""INSERT INTO ik_personnel_details(user_id,last_login_at) VALUES(?,CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET last_login_at=CURRENT_TIMESTAMP""",(uid,))
    return identity


def mutate(db, actor, path, data):
    role = actor['role']
    if path == '/kullanicilar' and role in ('founder','supervisor'):
        tenant = actor['company_id'] if role == 'supervisor' else number(data,'company_id')
        company(db,tenant)
        uid = number(data,optional=True)
        if data.get('action') == 'ik_account':
            require_available_seat(db,tenant)
            ik_role = choice(data,'ik_role',('hr_manager','hr_staff'))
            uid = add_user(db,data,'staff',tenant)
            db.execute('INSERT INTO ik_user_roles(user_id,role) VALUES(?,?)',(uid,ik_role))
            audit(db,actor,f'İK hesabı oluşturuldu #{uid}',tenant)
            return '/kullanicilar'
        if data.get('action') == STAFF_PERMISSION:
            if role != 'supervisor':
                raise Problem(403,'Bu işlem süpervizör yetkisi gerektirir.')
            require(db.execute("SELECT id FROM users WHERE id=? AND company_id=? AND role='staff'",
                               (uid,tenant)).fetchone(),'Personel')
            enabled = choice(data,'enabled',('0','1')) == '1'
            if enabled:
                if not delegation_allowed(db,tenant):
                    raise Problem(403,'Kurucu bu firma için yetki devrine izin vermiyor.')
                db.execute('INSERT INTO staff_subscription_access VALUES(?) ON CONFLICT(user_id) DO NOTHING',(uid,))
            else:
                db.execute('DELETE FROM staff_subscription_access WHERE user_id=?',(uid,))
            audit(db,actor,f'Abonelik görüntüleme yetkisi {"atandı" if enabled else "kaldırıldı"} #{uid}',tenant)
            return
        if uid:
            active = int(choice(data,'active',('0','1')))
            if active and db.execute("""SELECT 1 FROM users JOIN ik_user_roles ON user_id=users.id
                    WHERE users.id=? AND company_id=? AND active=0""",(uid,tenant)).fetchone():
                require_available_seat(db,tenant)
            result = db.execute("UPDATE users SET name=?,active=? WHERE id=? AND company_id=? AND role='staff'",
                                (field(data,'name'),active,uid,tenant))
            if not result.rowcount:
                raise Problem(404,'Personel bulunamadı.')
            if not active:
                db.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
        else:
            uid = add_user(db,data,'staff',tenant)
        audit(db,actor,f'Personel kaydedildi #{uid}',tenant)
        return f'/kullanicilar?duzenle={uid}'
        return
    if role != 'founder':
        raise Problem(403,'Kurucu yetkisi gerekir.')
    if path == '/mulakat-sablonlari':
        target = '/mulakat-sablonlari'
        identifier = number(data, 'template_id', optional=True)
        if identifier:
            require(db.execute('SELECT id FROM ik_interview_templates WHERE id=?',(identifier,)).fetchone(),'Şablon')
        if data.get('action') == 'assign':
            tenant = number(data,'company_id')
            company(db,tenant)
            position = data.get('position_id','').strip()
            if not identifier or (position and not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',position)):
                raise Problem(400,'Geçerli şablon, firma ve pozisyon kimliği girin.')
            db.execute("""INSERT INTO ik_interview_template_assignments VALUES (?,?,?)
                ON CONFLICT(company_id,position_id) DO UPDATE SET template_id=excluded.template_id""",
                (tenant,position,identifier))
            audit(db,actor,f'Mülakat şablonu atandı #{identifier}',tenant)
            return target+f'?duzenle={identifier}'
        saved_template = db.execute('SELECT questions FROM ik_interview_templates WHERE id=?',
                                    (identifier,)).fetchone() if identifier else None
        count = (interview_templates.snapshot_question_count(saved_template) if saved_template
                 else 1 if data.get('round') == 'english_demo' else 10)
        language = 'en' if count == 1 else choice(data,'language',('tr','en'))
        questions = [{'text':field(data,f'question_{index}',700),
                      'weight':int(field(data,f'weight_{index}',3))} for index in range(1,count+1)]
        if any(not 0 <= item['weight'] <= 100 for item in questions) or sum(item['weight'] for item in questions)!=100:
            raise Problem(400,'Soru ağırlıklarının toplamı 100 olmalıdır.')
        values = (field(data,'name'),language,json.dumps(questions,ensure_ascii=False))
        if identifier:
            db.execute('UPDATE ik_interview_templates SET name=?,language=?,questions=? WHERE id=?',(*values,identifier))
        else:
            identifier = db.execute('INSERT INTO ik_interview_templates(name,language,questions) VALUES (?,?,?) RETURNING id',values).fetchone()[0]
        audit(db,actor,f'Mülakat şablonu kaydedildi #{identifier}',None)
        return target+f'?duzenle={identifier}'
    if path == '/yetkiler':
        tenant = number(data,'company_id')
        company(db,tenant)
        enabled = choice(data,'enabled',('0','1')) == '1'
        if enabled:
            db.execute('INSERT INTO delegation_limits VALUES(?) ON CONFLICT(company_id) DO NOTHING',(tenant,))
        else:
            db.execute('DELETE FROM delegation_limits WHERE company_id=?',(tenant,))
            db.execute('DELETE FROM staff_subscription_access WHERE user_id IN (SELECT id FROM users WHERE company_id=?)',(tenant,))
        audit(db,actor,'Abonelik yetki devri '+('açıldı' if enabled else 'kapatıldı'),tenant)
        return
    if path == '/firmalar':
        tenant = number(data,optional=True)
        action = data.get('action','profile')
        if action == 'activate_ik':
            company(db,tenant)
            firma.activation(db,tenant)
            if not db.execute('SELECT 1 FROM company_portals WHERE company_id=?',(tenant,)).fetchone():
                raise Problem(400,'Aktivasyon için firma portal adresi gerekir.')
            today = ik_bridge.today()
            result = db.execute("""UPDATE subscriptions SET status='Aktif' WHERE company_id=?
                AND starts<=? AND ends>=? AND EXISTS (
                    SELECT 1 FROM service_terms WHERE company_id=? AND product='ernasa-ik'
                )""",(tenant,today,today,tenant))
            if not result.rowcount:
                raise Problem(400,'Geçerli ERNASA HR aboneliği gerekir.')
            db.execute("UPDATE companies SET status='Aktif' WHERE id=?",(tenant,))
            db.execute("UPDATE company_profile SET activated=1,stage='Aktif' WHERE company_id=?",(tenant,))
            audit(db,actor,'ERNASA HR etkinleştirildi',tenant)
            return f'/firmalar?detay={tenant}'
        if action == 'logo':
            company(db,tenant)
            png = firma.logo_png(data.get('logo'))
            db.execute('INSERT INTO company_logos(company_id,png) VALUES(?,?) ON CONFLICT(company_id) DO UPDATE SET png=excluded.png',(tenant,png))
            audit(db,actor,'Firma logosu güncellendi',tenant)
            return f'/firmalar?detay={tenant}'
        if action in ('contact','contact_update','address'):
            company(db,tenant)
            contact = action in ('contact','contact_update')
            fields = firma.CONTACT if contact else firma.ADDRESS
            details = firma.values(data,fields)
            choice(data,'kind',firma.CONTACTS if contact else ('Merkez','Fatura','Şube','Tesis'))
            field(data,'name' if contact else 'address',120 if contact else 450)
            target_table = 'company_contacts' if contact else 'company_addresses'
            record = number(data,'record',optional=True)
            if action == 'contact_update' and record is None:
                raise Problem(400,'Düzenlenecek yetkili kaydı belirtilmedi.')
            previous = None
            if record:
                previous = require(db.execute(f'SELECT * FROM {target_table} WHERE id=? AND company_id=?',(record,tenant)).fetchone(),'Kayıt')
            if not contact:
                for key in address_catalog.KEYS:
                    field(data,key,80)
                try:
                    address_catalog.validate(details,previous,data.get('address_manual')=='1')
                except ValueError as error:
                    raise Problem(400,str(error)) from None
                if (address_catalog.country_code(details['country'])=='TR' and details['postal']
                        and (not previous or details['postal'] != previous['postal']
                             or any(details[k] != previous[k] for k in address_catalog.KEYS))
                        and not re.fullmatch('[0-9]{5}',details['postal'])):
                    raise Problem(400,'Türkiye posta kodu 5 rakam olmalıdır.')
            if contact:
                primary = int(choice(data,'primary_contact',('0','1')))
                old_primary = db.execute('SELECT id FROM company_contacts WHERE company_id=? AND primary_contact=1',
                                         (tenant,)).fetchone()
                old_primary_id = old_primary['id'] if old_primary else None
                if primary:
                    db.execute('UPDATE company_contacts SET primary_contact=0 WHERE company_id=?',(tenant,))
                details['primary_contact'] = primary
            if record:
                columns = ','.join(key+'=?' for key in details)
                db.execute(f'UPDATE {target_table} SET {columns} WHERE id=? AND company_id=?',(*details.values(),record,tenant))
                record_id = record
            else:
                columns = ','.join(details)
                placeholders = ','.join('?' for _ in details)
                record_id = db.execute(f'INSERT INTO {target_table}(company_id,{columns}) VALUES(?,{placeholders}) RETURNING id',
                                        (tenant,*details.values())).fetchone()[0]
            if not contact and db.execute("""SELECT 1 FROM company_profile WHERE company_id=? AND activated=1
                UNION ALL SELECT 1 FROM service_terms JOIN subscriptions USING(company_id)
                WHERE company_id=? AND (price_minor IS NOT NULL OR status='Aktif')""",(tenant,tenant)).fetchone():
                firma.activation(db,tenant)
            if contact:
                contact_id = record_id
                audit(db,actor,f'Firma yetkilisi {"düzenlendi" if record else "oluşturuldu"} #{contact_id}',tenant)
                new_primary_id = contact_id if primary else (None if old_primary_id == contact_id else old_primary_id)
                if old_primary_id != new_primary_id:
                    before = f'#{old_primary_id}' if old_primary_id else 'yok'
                    after = f'#{new_primary_id}' if new_primary_id else 'yok'
                    audit(db,actor,f'Birincil iletişim değişti ({before} → {after})',tenant)
            else:
                audit(db,actor,f'Firma adresi {"düzenlendi" if record else "oluşturuldu"} #{record_id}',tenant)
            return f'/firmalar?detay={tenant}&sekme='+('yetkililer' if contact else 'adresler')
        if action != 'profile':
            raise Problem(400,'Geçersiz firma işlemi.')
        values = (field(data,'name'),choice(data,'status',STATES))
        stage = choice(data,'stage',firma.STAGES)
        profile = firma.profile_values(data)
        previous_company = company(db,tenant) if tenant else None
        previous_stage = db.execute('SELECT stage FROM company_profile WHERE company_id=?',(tenant,)).fetchone()[0] if tenant else None
        if tenant:
            db.execute('UPDATE companies SET name=?,status=? WHERE id=?',(*values,tenant))
        else:
            tenant = db.execute('INSERT INTO companies(name,status) VALUES(?,?) RETURNING id',values).fetchone()[0]
        columns = ','.join(key+'=?' for key in profile)
        db.execute(f'UPDATE company_profile SET stage=?,{columns} WHERE company_id=?',(stage,*profile.values(),tenant))
        if 'portal_slug' in data:
            slug = ik_bridge.slug(data['portal_slug'])
            if slug:
                db.execute("""INSERT INTO company_portals(company_id,slug) VALUES(?,?)
                    ON CONFLICT(company_id) DO UPDATE SET slug=excluded.slug,version=lower(hex(randomblob(16)))
                    WHERE company_portals.slug<>excluded.slug""",(tenant,slug))
            else:
                db.execute('DELETE FROM company_portals WHERE company_id=?',(tenant,))
        activated = db.execute('SELECT activated FROM company_profile WHERE company_id=?',(tenant,)).fetchone()[0]
        billable = db.execute('SELECT 1 FROM service_terms WHERE company_id=? AND price_minor IS NOT NULL',(tenant,)).fetchone()
        if billable or (stage == 'Aktif' and (previous_stage != stage or activated)) or (values[1] == 'Aktif' and
                               (not previous_company or previous_company['status'] != 'Aktif' or activated)):
            firma.activation(db,tenant)
            db.execute('UPDATE company_profile SET activated=1 WHERE company_id=?',(tenant,))
        if values[1] == 'Askıda':
            db.execute('DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE company_id=?)',(tenant,))
        audit(db,actor,f'Firma {"düzenlendi" if previous_company else "oluşturuldu"} #{tenant}',tenant)
        return f'/firmalar?detay={tenant}'
    elif path == '/supervizorler':
        uid = number(data,optional=True)
        if uid:
            row = require(db.execute("SELECT company_id FROM users WHERE id=? AND role='supervisor'",(uid,)).fetchone(),'Süpervizör')
            db.execute('UPDATE users SET active=0 WHERE id=?',(uid,))
            db.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
            audit(db,actor,f'Süpervizör yetkisi iptal edildi #{uid}',row['company_id'])
        else:
            tenant = number(data,'company_id')
            company(db,tenant)
            require_available_seat(db,tenant)
            uid = add_user(db,data,'supervisor',tenant)
            db.execute("INSERT INTO ik_user_roles(user_id,role) VALUES(?,'supervisor')",(uid,))
            audit(db,actor,f'Süpervizör atandı #{uid}',tenant)
    elif path == '/abonelikler':
        tenant = number(data,'company_id')
        company(db,tenant)
        if data.get('action') == 'ernasa_internal_activate':
            activate_internal_portal(db, actor, tenant)
            return f'/abonelikler?duzenle={tenant}'
        edit = number(data,'subscription_edit',optional=True)
        if edit is not None and edit != tenant:
            raise Problem(400,'Aboneliğin bağlı olduğu firma değiştirilemez.')
        try:
            starts = date.fromisoformat(field(data,'starts',10))
            ends = date.fromisoformat(field(data,'ends',10))
        except ValueError:
            raise Problem(400,'Geçerli tarihler girin.') from None
        if starts > ends:
            raise Problem(400,'Bitiş tarihi başlangıçtan önce olamaz.')
        status = choice(data,'status',SUB)
        if data.get('action') == 'ats_subscription':
            if status == 'Aktif':
                firma.activation(db,tenant)
            quota = data.get('user_quota','').strip()
            if quota and not re.fullmatch(r'[0-9]{1,9}',quota):
                raise Problem(400,'Kullanıcı kotası sıfır veya pozitif tam sayı olmalıdır.')
            db.execute("""INSERT INTO ats_subscriptions VALUES(?,?,?,?,?,?)
                ON CONFLICT(company_id) DO UPDATE SET package=excluded.package, starts=excluded.starts,
                    ends=excluded.ends,status=excluded.status,user_quota=excluded.user_quota""",
                (tenant,choice(data,'package',PACKAGES),starts.isoformat(),ends.isoformat(),status,
                 int(quota) if quota else None))
            audit(db,actor,'ATS aboneliği kaydedildi',tenant)
            return f'/abonelikler?ats={tenant}'
        terms = firma.terms_values(db,data)
        if terms[0] != firma.PRODUCT:
            raise Problem(400,'ATS aboneliğini ayrı ATS formundan yönetin.')
        if status == 'Aktif' or terms[6] is not None:
            firma.activation(db,tenant)
        previous = db.execute('SELECT * FROM subscriptions WHERE company_id=?',(tenant,)).fetchone()
        if edit is not None:
            require(previous,'Abonelik')
        previous_terms = db.execute('SELECT * FROM service_terms WHERE company_id=?',(tenant,)).fetchone()
        if previous:
            db.execute('INSERT INTO subscription_history(company_id,snapshot) VALUES(?,?)',
                       (tenant,json.dumps({'subscription':dict(previous),'terms':dict(previous_terms) if previous_terms else None},ensure_ascii=False)))
        db.execute("""INSERT INTO subscriptions VALUES(?,?,?,?,?) ON CONFLICT(company_id)
          DO UPDATE SET package=excluded.package, starts=excluded.starts, ends=excluded.ends, status=excluded.status""",
          (tenant,choice(data,'package',PACKAGES),starts.isoformat(),ends.isoformat(),status))
        db.execute("""INSERT INTO service_terms VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(company_id) DO UPDATE SET product=excluded.product,version=excluded.version,
                user_quota=excluded.user_quota,operation_quota=excluded.operation_quota,trial_days=excluded.trial_days,
                period=excluded.period,price_minor=excluded.price_minor,currency=excluded.currency,
                vat_basis=excluded.vat_basis,payment=excluded.payment""",(tenant,*terms))
        audit(db,actor,'Abonelik kaydedildi ('+('düzenlendi' if previous else 'oluşturuldu')+')',tenant)
        return f'/abonelikler?duzenle={tenant}'
    elif path == '/ayarlar':
        db.execute('UPDATE settings SET name=? WHERE id=1',(field(data,'name',70),))
        audit(db,actor,'Sistem adı güncellendi')
    else:
        raise Problem(405,'Bu ekran düzenleme kabul etmiyor.')


def internal_portal(db):
    return db.execute(
        "SELECT company_id FROM company_portals WHERE slug=?", ('ernasayazilim',)
    ).fetchone()


def activate_internal_portal(db, actor, tenant):
    portal = internal_portal(db)
    if actor['role'] != 'founder' or not portal or portal['company_id'] != tenant:
        raise Problem(403, 'İç kullanım yalnızca ERNASA portalına tanımlanabilir.')
    if db.execute(
        'SELECT 1 FROM service_terms WHERE company_id=? AND price_minor IS NOT NULL',
        (tenant,),
    ).fetchone():
        raise Problem(409, 'Fiyat tanımlı abonelik iç kullanıma çevrilemez.')
    today = ik_bridge.today()
    end = (date.fromisoformat(today) + timedelta(days=365)).isoformat()
    db.execute("UPDATE companies SET status='Aktif' WHERE id=?", (tenant,))
    db.execute("UPDATE company_profile SET activated=1 WHERE company_id=?", (tenant,))
    db.execute(
        """INSERT INTO subscriptions(company_id,package,starts,ends,status) VALUES(?,?,?,?,'Aktif')
           ON CONFLICT(company_id) DO UPDATE SET package=excluded.package,
           starts=excluded.starts, ends=excluded.ends,status=excluded.status""",
        (tenant, PACKAGES[0], today, end),
    )
    db.execute(
        """INSERT INTO service_terms(company_id,product,payment) VALUES(?,?,?)
           ON CONFLICT(company_id) DO NOTHING""",
        (tenant, firma.PRODUCT, firma.PAYMENTS[0]),
    )
    db.execute(
        """INSERT INTO ats_subscriptions(company_id,package,starts,ends,status) VALUES(?,?,?,?,'Aktif')
           ON CONFLICT(company_id) DO UPDATE SET package=excluded.package,
           starts=excluded.starts, ends=excluded.ends,status=excluded.status""",
        (tenant, PACKAGES[0], today, end),
    )
    audit(db, actor, 'ERNASA iç kullanım erişimi etkinleştirildi', tenant)


def e(value):
    return html.escape(str(value), quote=True)


def inp(label, key, value='', kind='text', required=True):
    return f'<label>{e(label)}<input name="{key}" type="{kind}" value="{e(value)}" {"required" if required else ""}></label>'


def select(label, key, options, current=None, required=True):
    tags = ''.join(f'<option value="{e(value)}" {"selected" if current==value else ""}>{e(text)}</option>'
                   for value,text in options)
    return f'<label>{label}<select name="{key}" {"required" if required else ""}>{tags}</select></label>'


def choices(label,key,options,current=None):
    return select(label,key,[(v,v) for v in options],current)


def hidden(key,value):
    return f'<input name="{key}" value="{e(value)}" type="hidden">'


def form(path, actor, fields, action):
    return (f'<form method="post" action="{path}">{hidden("csrf",actor["csrf"])}'
            f'<div class="fields">{fields}</div><button>{action}</button></form>')


def card(title, body):
    return f'<section class="card"><h2>{e(title)}</h2>{body}</section>'


def icon(path):
    index = tuple(NAV).index(path)
    shape = ICONS[index] if index < len(ICONS) else 'M6 3h12v18H6z M9 8h6 M9 12h6 M9 16h4'
    return (f'<svg class="icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" '
            f'stroke-linejoin="round"><path d="{shape}"/></svg>')


def brand_block(brand):
    return (f'<div class="brand"><img src="/ernasalogo.png" width="44" height="44" alt="ERNASA">'
            f'<div><strong>{e(brand)}</strong><span>Platform Yönetim Merkezi</span></div></div>')


def delegation_allowed(db, tenant):
    return db.execute('SELECT 1 FROM delegation_limits WHERE company_id=?',(tenant,)).fetchone() is not None


def allowed_routes(db, actor):
    routes = ACCESS[actor['role']]
    if actor['role'] == 'staff' and db.execute(
        'SELECT 1 FROM staff_subscription_access JOIN delegation_limits ON company_id=? WHERE user_id=?',
        (actor['company_id'],actor['id']),
    ).fetchone():
        return routes | {'/abonelikler'}
    return routes


def navigation(routes_allowed, current):
    groups = []
    for title, routes in GROUPS:
        links = []
        for route in routes:
            if route in routes_allowed:
                active = ' aria-current="page"' if route == current else ''
                links.append(f'<a href="{route}"{active}>{icon(route)}{NAV[route]}</a>')
        if links:
            groups.append(f'<div class="nav-group"><span>{title}</span>{"".join(links)}</div>')
    return '<nav aria-label="Yönetim menüsü">' + ''.join(groups) + '</nav>'


def candidate_experience_panel(data):
    """Founder-only aggregated report; no survey response identifiers exposed."""
    names = {'ease':'Kullanım kolaylığı','clarity':'Soruların açıklığı',
             'interview':'Dijital mülakat deneyimi','training':'Eğitimin faydası',
             'overall':'Genel memnuniyet'}
    content = ('<p>Gönüllü değerlendirmeler, mülakat puanları ve aday '
               'kararlarından ayrı saklanır. Yalnızca toplam sonuçlar gösterilir.</p>')
    for key,title in (('mulakat','Dijital mülakat'),('egitim','Eğitim platformu')):
        result=data.get(key,{})
        ratings=result.get('ratings',{})
        rows=[]
        for col,label in names.items():
            item=ratings.get(col,{})
            mean=item.get('average')
            if item.get('count'):
                rows.append((e(label),e(str(mean))+' / 5',e(str(item['count']))))
        content += card(title+' · '+str(result.get('count',0))+' değerlendirme',
                        table(('Başlık','Ortalama','Yanıt sayısı'),rows))
        comments=result.get('comments') or []
        if comments:
            content += card(title+' · öneriler',
                     '<ul>'+''.join('<li>'+e(str(c))+'</li>' for c in comments)+'</ul>')
    content += '<p class="notice">Katılımcıların kimliği ve tekil mülakat yanıtları bu ekranda gösterilmez.</p>'
    return content


def table(headers, rows):
    head = ''.join(f'<th scope="col">{e(h)}</th>' for h in headers)
    body = ''.join('<tr>'+''.join(f'<td data-label="{e(label)}">{cell}</td>' for label,cell in zip(headers,row))+'</tr>' for row in rows)
    return (f'<div class="overflow"><table><thead><tr>{head}</tr></thead><tbody>'
            + (body or f'<tr><td colspan="{len(headers)}">Kayıt bulunamadı.</td></tr>')
            + '</tbody></table></div>')


def edit_link(path,key):
    return f'<a href="{path}?duzenle={key}#kayit-formu">Düzenle</a>'


def record_editor(path,actor,fields,title,edit,cancel=None):
    cancel_link = f'<p><a href="{e(cancel or path)}">Düzenlemeyi iptal et</a></p>' if edit else ''
    editor = card(title,form(path,actor,fields,'Değişiklikleri kaydet' if edit else 'Kaydet')+cancel_link)
    return f'<div id="kayit-formu" tabindex="-1">{editor}</div>'


def tenant_field(db,current=None):
    rows = db.execute('SELECT id,name FROM companies ORDER BY name').fetchall()
    if not rows:
        return '<p>Önce bir firma oluşturun.</p>'
    return select('Firma','company_id',[(r['id'],r['name']) for r in rows],current)


def account_fields():
    return inp('E-posta','email',kind='email') + inp('Başlangıç parolası (12+ karakter)','password',kind='password')


def events_table(rows, company_links=False):
    return table(('Tarih (UTC)', 'İşlem', 'Hesap', 'Firma'),
                 ([e(r['stamp']), e(r['action']), e(r['actor']),
                   f'<a href="/firmalar?detay={r["company_id"]}">#{r["company_id"]}</a>'
                   if r['company_id'] and company_links else
                   e(r['company_id'] or '—')] for r in rows))


def dashboard(db,actor):
    tenant = actor['company_id']
    if actor['role']=='staff':
        return card('Firma hesabınız',f'<p>{e(company(db,tenant)["name"])} · Personel</p>')
    where,args = (' WHERE company_id=?',(tenant,)) if tenant else ('',())
    companies = db.execute('SELECT status,count(*) AS n FROM companies'+(' WHERE id=?' if tenant else '')+
                           ' GROUP BY status',args).fetchall()
    subscriptions = db.execute('SELECT status,count(*) AS n FROM subscriptions'+where+
                               ' GROUP BY status',args).fetchall()
    accounts = db.execute("SELECT count(*) AS total, count(CASE WHEN role='supervisor' THEN 1 END) AS supervisors FROM users"+
                          where,args).fetchone()
    today = date.today()
    period = (today.isoformat(), (today + timedelta(days=30)).isoformat())
    active = db.execute("SELECT count(*) FROM subscriptions WHERE status='Aktif' AND starts<=? AND ends>=?"+
                        (' AND company_id=?' if tenant else ''),(period[0], period[0], *args)).fetchone()[0]
    expiring = db.execute("""SELECT companies.id, companies.name, subscriptions.ends,
        CASE WHEN subscriptions.status='Sona erdi' OR subscriptions.ends < ? THEN 'Sona erdi'
        ELSE 'Yaklaşıyor' END AS alert FROM subscriptions JOIN companies ON companies.id=company_id
        WHERE (subscriptions.status='Sona erdi' OR subscriptions.ends < ?
        OR (subscriptions.status='Aktif' AND subscriptions.ends <= ?))"""+
        (' AND subscriptions.company_id=?' if tenant else '')+
        ' ORDER BY subscriptions.ends LIMIT 12',(period[0], *period, *args)).fetchall()

    def total(rows):
        return sum(r['n'] for r in rows)

    def bars(rows):
        maximum = total(rows) or 1
        return ''.join(f'<div class="bar">{e(r["status"])} <meter min="0" max="{maximum}" value="{r["n"]}"></meter> {r["n"]}</div>'
                       for r in rows) or '<p>Kayıt bulunamadı.</p>'
    active_companies = sum(r['n'] for r in companies if r['status']=='Aktif')
    cards = ''.join(card(label,f'<strong>{value}</strong><small>{detail}</small>') for label,value,detail in (
        ('Toplam firma',total(companies),f'{active_companies} aktif · {total(companies)-active_companies} askıda'),
        ('Süpervizör',accounts['supervisors'],'Atanmış hesaplar'),
        ('Aktif abonelik',active,'Bugün geçerli'),
        ('Kullanıcı',accounts['total'],'Kayıtlı hesaplar')))
    events = db.execute('SELECT * FROM audit'+where+' ORDER BY id DESC LIMIT 8',args)
    alerts = table(('Firma','Bitiş','Durum'), ([e(r['name']),e(r['ends']),e(r['alert'])] for r in expiring))
    return ('<div class="stats metrics">'+cards+'</div><div class="stats charts">'+card('Firma dağılımı',bars(companies))+
            card('Abonelik dağılımı',bars(subscriptions))+'</div>'+card('Abonelik takibi · 30 gün',alerts)+
            card('Son işlemler',events_table(events, not tenant)))


COMPANY_TABS = {'genel':'Genel Bilgiler','yetkililer':'Yetkililer','adresler':'Adresler ve Şubeler',
                'abonelikler':'Abonelikler','personel':'Personel ve Süpervizörler','belgeler':'Belgeler',
                'gecmis':'İşlem / Denetim Geçmişi'}


def optional_fields(spec, record, options=None):
    options = options or {}
    return ''.join(select(label,key,[('','Belirtilmedi'),*((x,x) for x in options[key])],record.get(key,''),required=False)
                   if key in options else inp(label,key,record.get(key) or '',
                        'date' if key=='founded' else 'email' if 'email' in key else 'text',False)
                   for key,(label,_) in spec.items())


def address_fields(record):
    code = address_catalog.country_code(record.get('country','Türkiye'))
    countries, regions = address_catalog.catalog()
    country = record.get('country','Türkiye')
    city, district = record.get('city',''), record.get('district','')
    choices = regions.get(code,{})
    known_city = address_catalog.match(city,choices)
    manual = bool(record and code != 'TR' and (
        (choices and not known_city) or (choices.get(known_city) and not address_catalog.match(district,choices[known_city]))
        or not choices or not choices.get(known_city)))
    fields = []
    for key,value,label,options in (
        ('country',country,'Ülke',countries.values()),('city',city,'İl / eyalet / bölge',choices),
        ('district',district,'İlçe / alt bölge',choices.get(known_city,())),
    ):
        items = ''.join(f'<option value="{e(name)}"></option>' for name in sorted(set(options),key=address_catalog.folded))
        fields.append(f'<label>{label}<input name="{key}" id="address-{key}" list="address-{key}-list" '
                      f'value="{e(value)}" maxlength="80" autocomplete="off" required>'
                      f'<datalist id="address-{key}-list">{items}</datalist></label>')
    return ('<fieldset class="company-fields address-location" data-address-picker>'
            '<legend>Adres konumu</legend>'+''.join(fields)+
            '<label class="address-manual"><span><input type="checkbox" name="address_manual" value="1"'
            + (' checked' if manual else '') + '> Yurt dışı bölgesini elle gir</span></label>'
            '<p class="muted address-help" aria-live="polite">Listelerden ülke ve bölge seçin. '
            'Yurt dışında alt bölge listesi eksikse manuel girişi kullanın.</p></fieldset>'
            '<script defer src="/address-picker.js"></script>')


def company_detail(db,actor,tenant,q):
    row = company(db,tenant)
    profile = db.execute('SELECT * FROM company_profile WHERE company_id=?',(tenant,)).fetchone()
    base = f'/firmalar?detay={tenant}'
    tab = q.get('sekme','genel')
    if tab not in COMPANY_TABS:
        raise Problem(404,'Sekme bulunamadı.')
    links = ''.join(f'<a href="{base}&amp;sekme={key}"'+(' aria-current="page"' if key==tab else '')+
                    f'>{title}</a>' for key,title in COMPANY_TABS.items())
    header = f'<p><a href="/firmalar">Firma listesi</a> · {e(profile["code"])}</p><h2>{e(row["name"])}</h2><nav class="company-tabs" aria-label="Firma detayları">{links}</nav>'
    if tab=='genel':
        items = [('Ticari unvan',row['name']),('Firma kodu',profile['code']),('Ticari aşama',profile['stage']),('Erişim',row['status'])]
        portal = db.execute('SELECT slug FROM company_portals WHERE company_id=?',(tenant,)).fetchone()
        items.append(('Firma Portal Adresi',portal['slug']+'.ernasa.com' if portal else ''))
        activate = form('/firmalar',actor,hidden('id',tenant)+hidden('action','activate_ik'),'ERNASA HR’yı etkinleştir')
        items += [(label,profile[key]) for key,(label,_) in firma.PROFILE.items()]
        logo = ''
        if db.execute('SELECT 1 FROM company_logos WHERE company_id=?',(tenant,)).fetchone():
            logo = f'<img class="company-logo" src="/firma-logo?id={tenant}" alt="Firma logosu">'
        upload = (f'<form method="post" action="/firmalar" enctype="multipart/form-data">{hidden("csrf",actor["csrf"])}'
                  f'{hidden("id",tenant)}{hidden("action","logo")}<label>Firma logosu (PNG/JPEG, 500 KB)'
                  '<input type="file" name="logo" accept="image/png,image/jpeg" required></label><button>Logoyu kaydet</button></form>')
        return header+card('Kurumsal profil',logo+table(('Alan','Değer'),([e(k),e(v or '—')] for k,v in items))+
                           f'<p>{edit_link("/firmalar",tenant)}</p>'+upload+activate)
    if tab in ('yetkililer','adresler'):
        contact = tab=='yetkililer'
        spec = firma.CONTACT if contact else firma.ADDRESS
        source = 'company_contacts' if contact else 'company_addresses'
        rows = db.execute(f'SELECT * FROM {source} WHERE company_id=? ORDER BY id',(tenant,)).fetchall()
        edit = number({'id':q.get('kayit','')},optional=True)
        current = dict(require(next((r for r in rows if r['id']==edit),None),'Kayıt')) if edit else {}
        kinds = firma.CONTACTS if contact else ('Merkez','Fatura','Şube','Tesis')
        action = ('contact_update' if edit else 'contact') if contact else 'address'
        fields = hidden('id',tenant)+hidden('action',action)+hidden('record',edit or '')
        fields += choices('Tür','kind',kinds,current.get('kind'))
        fields += optional_fields({key:value for key,value in spec.items()
                                   if key!='kind' and (contact or key not in address_catalog.KEYS)},current)
        if not contact:
            fields += address_fields(current)
        if contact:
            fields += select('Birincil iletişim','primary_contact',[('0','Hayır'),('1','Evet')],
                             str(current.get('primary_contact',0)))
        headers = tuple(label for label,_ in spec.values())+ (('Birincil',) if contact else ()) + ('İşlem',)
        entries = ([e(r[key] or '—') for key in spec]+(['Evet' if r['primary_contact'] else 'Hayır'] if contact else [])+
                   [f'<a href="{base}&amp;sekme={tab}&amp;kayit={r["id"]}#kayit-formu">Düzenle</a>'] for r in rows)
        title = ('Yetkiliyi düzenle' if edit else 'Yetkili ekle') if contact else ('Adresi düzenle' if edit else 'Adres ekle')
        return (header+record_editor('/firmalar',actor,fields,title,edit,f'{base}&sekme={tab}')
                +card('Kayıtlar',table(headers,entries)))
    if tab=='abonelikler':
        subscription = db.execute('SELECT * FROM subscriptions WHERE company_id=?',(tenant,)).fetchone()
        history = db.execute('SELECT * FROM subscription_history WHERE company_id=? ORDER BY id DESC',(tenant,))
        terms = read_terms(db,tenant)
        summary = list(zip(('Paket','Başlangıç','Bitiş','Abonelik durumu'),tuple(subscription)[1:])) if subscription else []
        summary += [(label,terms[key]) for key,(label,_) in firma.SERVICE.items()] if terms else []
        return header+card('Abonelik',table(('Alan','Değer'),([e(k),e(v if v not in (None,'') else '—')] for k,v in summary))+
            f'<p><a href="/abonelikler?firma={tenant}">Aboneliği yönet</a></p>')+card('Yenileme geçmişi',
            table(('Kayıt tarihi','Önceki paket','Önceki bitiş'),([e(r['recorded']),e(s['package']),e(s['ends'])]
                for r in history for s in (json.loads(r['snapshot'])['subscription'],))))
    if tab=='personel':
        rows = db.execute('SELECT name,role,active FROM users WHERE company_id=? ORDER BY name',(tenant,))
        return header+card('Hesaplar',table(('Ad soyad','Rol','Durum'),
                           ([e(r['name']),e(ROLES[r['role']]),'Aktif' if r['active'] else 'Kapalı'] for r in rows)))
    if tab=='gecmis':
        rows = db.execute('SELECT * FROM audit WHERE company_id=? ORDER BY id DESC LIMIT 200',(tenant,))
        return header+card('Firma denetim kayıtları',events_table(rows))
    return header+card('Belgeler','<p>Belge yükleme ve sözleşme arşivi bu sürümde kullanılmıyor.</p>')


def read_terms(db,tenant):
    row = db.execute('SELECT * FROM service_terms WHERE company_id=?',(tenant,)).fetchone()
    if not row:
        return {}
    terms = dict(row)
    for key,stored in (('price','price_minor'),('vat','vat_basis')):
        terms[key] = '' if row[stored] is None else f'{row[stored]//100}.{row[stored]%100:02d}'
    return terms


def screen(db, actor, path, q):
    if path == '/mulakat-sablonlari':
        selected = number({'id':q.get('duzenle','')},optional=True)
        row = require(db.execute('SELECT * FROM ik_interview_templates WHERE id=?',(selected,)).fetchone(),'Şablon') if selected else None
        demo = not row and q.get('tur') == 'english_demo'
        first_round = not row and q.get('tur') == 'partner_first'
        if row:
            questions = json.loads(row['questions'])
        elif demo:
            questions = [{'text':interview_templates.PARTNER_ENGLISH_DEMO,'weight':100}]
        elif first_round:
            questions = interview_templates.partner_first_round()
        else:
            questions = [{'text':'','weight':''} for _ in range(10)]
        language = row['language'] if row else 'en' if demo else 'tr'
        name = row['name'] if row else ''
        if demo:
            name = interview_templates.PARTNER_ENGLISH_DEMO_NAME
        elif first_round:
            name = interview_templates.PARTNER_FIRST_ROUND_NAME
        fields = hidden('template_id',selected or '') + inp('Şablon adı','name',name)
        if len(questions) == 1:
            fields += hidden('round','english_demo') + '<p>Şablon dili: English · İkinci tur satış demosu</p>'
        else:
            fields += ('<label>Şablon dili<select name="language">'
                       + ''.join(f'<option value="{code}" {"selected" if code==language else ""}>{label}</option>'
                                 for code,label in (('tr','Türkçe'),('en','English'))) + '</select></label>')
        for index,item in enumerate(questions,1):
            fields += (f'<div class="template-question"><label>{index}. soru'
                       f'<textarea name="question_{index}" maxlength="700" required>{e(item["text"])}</textarea></label>'
                       + inp('Ağırlık · '+str(index),'weight_'+str(index),item['weight'],kind='number') + '</div>')
        rows = db.execute('SELECT id,name,language FROM ik_interview_templates WHERE name<>? ORDER BY name',
                          ('ERNASA B2B Satış Ortağı',)).fetchall()
        listing = table(('Şablon','Dil','İşlem'),([e(t['name']),e(t['language']),
            f'<a href="{path}?duzenle={t["id"]}">Düzenle</a>'] for t in rows))
        assign = ''
        if selected:
            firms = db.execute('SELECT id,name FROM companies ORDER BY name')
            options = ''.join(f'<option value="{firm["id"]}">{e(firm["name"])}</option>' for firm in firms)
            assign = card('Atama',form(path,actor,hidden('action','assign')+hidden('template_id',selected)
                + f'<label>Firma<select name="company_id" required>{options}</select></label>'
                + inp('Pozisyon kimliği (firma geneli için boş bırakın)','position_id',required=False),'Ata'))
            assignments = db.execute("""SELECT c.name,a.position_id FROM ik_interview_template_assignments a
                JOIN companies c ON c.id=a.company_id WHERE a.template_id=? ORDER BY c.name,a.position_id""",(selected,))
            assign += card('Mevcut atamalar',table(('Firma','Pozisyon'),([e(a['name']),e(a['position_id'] or 'Firma geneli')] for a in assignments)))
        links = (f'<a href="{path}">Yeni şablon</a> · '
                 f'<a href="{path}?tur=partner_first">ERNASA satış ortaklığı · 1. tur</a> · '
                 f'<a href="{path}?tur=english_demo">Yeni İngilizce demo şablonu</a>')
        title = 'İngilizce satış demosu · İkinci tur' if len(questions) == 1 else 'On soru ve değerlendirme ağırlıkları'
        return card('Şablonlar',listing+links) + card(title,form(path,actor,fields,'Şablonu kaydet')) + assign
    edit = number({'id':q.get('duzenle','')},optional=True)
    tenant = actor['company_id']
    if path=='/':
        return dashboard(db,actor)
    if path=='/firmalar':
        detail = number({'id':q.get('detay','')},optional=True)
        if detail:
            return company_detail(db,actor,detail,q)
        search = q.get('q','')[:120]
        current = company(db,edit) if edit else {'name':'','status':'Askıda'}
        profile = dict(db.execute('SELECT * FROM company_profile WHERE company_id=?',(edit,)).fetchone()) if edit else {}
        rows = [r for r in db.execute('SELECT companies.*,code,stage,brand FROM companies JOIN company_profile ON company_id=id ORDER BY name')
                if any(search.casefold() in r[key].casefold() for key in ('name','brand','code'))]
        fields = hidden('id',edit or '')+inp('Ticari unvan','name',current['name'])+choices('Erişim','status',STATES,current['status'])
        portal = db.execute('SELECT slug FROM company_portals WHERE company_id=?',(edit,)).fetchone() if edit else None
        fields += inp('Firma Portal Adresi','portal_slug',portal['slug']+'.ernasa.com' if portal else '',required=False)
        fields += choices('Ticari aşama','stage',firma.STAGES,profile.get('stage',firma.STAGES[0]))
        keys = tuple(firma.PROFILE)
        for title,group in (('Kurumsal bilgiler',keys[:8]),('Sektör ve faaliyet',keys[8:16]),('İletişim',keys[16:])):
            fields += f'<fieldset class="company-fields"><legend>{title}</legend>'+optional_fields(
                {key:firma.PROFILE[key] for key in group},profile,{'company_type':firma.TYPES})+'</fieldset>'
        search_form = f'<form method="get" class="search">{inp("Firma ara","q",search,required=False)}<button>Ara</button></form>'
        return (record_editor(path,actor,fields,'Firma düzenle' if edit else 'Firma oluştur',edit)+
                card('Firma kayıtları',search_form+table(('Firma','Firma kodu','Ticari aşama','Erişim','İşlem'),
                    ([f'<a href="/firmalar?detay={r["id"]}">{e(r["name"])}</a>',e(r['code']),e(r['stage']),e(r['status']),edit_link(path,r['id'])] for r in rows))))
    if path=='/supervizorler':
        rows = db.execute("""SELECT users.*,companies.name AS company FROM users JOIN companies ON companies.id=company_id
                             WHERE role='supervisor' ORDER BY users.id DESC""")
        fields = tenant_field(db)+inp('Ad soyad','name')+account_fields()
        return card('Süpervizör ata',form(path,actor,fields,'Ata'))+card('Atamalar',table(
            ('Ad soyad','Firma','E-posta','Durum','İşlem'),
            ([e(r['name']),e(r['company']),e(r['email']),'Aktif' if r['active'] else 'İptal edildi',
              form(path,actor,hidden('id',r['id']),'Yetkiyi iptal et') if r['active'] else '—'] for r in rows)))
    if path=='/abonelikler':
        if actor['role'] == 'staff':
            if q:
                raise Problem(403,'Yalnızca kendi firmanızın aboneliğini görüntüleyebilirsiniz.')
            rows = db.execute('SELECT package,starts,ends,status FROM subscriptions WHERE company_id=?',(tenant,))
            return card('Firmanızın aboneliği',table(('Paket','Başlangıç','Bitiş','Durum'),
                        ([e(value) for value in row] for row in rows)))
        target = edit or number({'id':q.get('firma','')},optional=True)
        existing = db.execute('SELECT * FROM subscriptions WHERE company_id=?',(target,)).fetchone()
        if edit:
            require(existing,'Abonelik')
        current = existing or {
            'package':PACKAGES[0],'starts':'','ends':'','status':'Bekliyor'}
        terms = read_terms(db,target)
        products = db.execute('SELECT code,name FROM products WHERE code=?',(firma.PRODUCT,)).fetchall()
        target_fields = (hidden('subscription_edit',edit)+hidden('company_id',edit)
                         +f'<p>Firma: {e(company(db,edit)["name"])}</p>' if edit else tenant_field(db,target))
        fields = (target_fields+choices('Paket','package',PACKAGES,current['package'])+
                  inp('Başlangıç','starts',current['starts'],'date')+inp('Bitiş','ends',current['ends'],'date')+
                  choices('Durum','status',SUB,current['status']))
        fields += select('Ürün','product',[(r['code'],r['name']) for r in products],terms.get('product',firma.PRODUCT))
        fields += optional_fields({key:value for key,value in firma.SERVICE.items() if key not in ('product','payment')},
                                  terms,{'period':('Aylık','Yıllık'),'currency':('TRY','EUR','USD')})
        fields += choices('Ödeme durumu (manuel kayıt)','payment',firma.PAYMENTS,terms.get('payment',firma.PAYMENTS[0]))
        rows = db.execute('SELECT subscriptions.*,companies.name FROM subscriptions JOIN companies ON companies.id=company_id ORDER BY companies.name')
        ats_id = number({'id':q.get('ats','')},optional=True)
        ats = db.execute('SELECT * FROM ats_subscriptions WHERE company_id=?',(ats_id,)).fetchone()
        ats = dict(ats) if ats else {}
        ats_fields = (hidden('action','ats_subscription')+tenant_field(db,ats_id)+
                      choices('ATS paketi','package',PACKAGES,ats.get('package',PACKAGES[0]))+
                      inp('Başlangıç','starts',ats.get('starts',''),'date')+
                      inp('Bitiş','ends',ats.get('ends',''),'date')+
                      choices('Durum','status',SUB,ats.get('status','Bekliyor'))+
                      inp('Aktif kullanıcı kotası','user_quota',ats.get('user_quota') if ats.get('user_quota') is not None else '',
                          'number',required=False))
        ats_rows = db.execute('SELECT ats_subscriptions.*,companies.name FROM ats_subscriptions JOIN companies ON companies.id=company_id ORDER BY name')
        ats_card = card('ATS aboneliği',form(path,actor,ats_fields,'ATS aboneliğini kaydet')+table(
            ('Firma','Paket','Başlangıç','Bitiş','Durum','İşlem'),
            ([e(r[k]) for k in ('name','package','starts','ends','status')]+
             [f'<a href="/abonelikler?ats={r["company_id"]}">Düzenle</a>'] for r in ats_rows)))
        internal = internal_portal(db)
        internal_card = ''
        if internal:
            internal_fields = (hidden('action', 'ernasa_internal_activate')
                               + hidden('company_id', internal['company_id']))
            internal_card = card(
                'ERNASA iç kullanım',
                '<p>ERNASA portalı için 365 günlük faturalandırmasız ATS ve AI İK erişimi.</p>'
                + form(path, actor, internal_fields, 'İç kullanımı etkinleştir'),
            )
        return internal_card+record_editor(path,actor,fields,'Aboneliği düzenle' if edit else 'Abonelik kaydet',edit)+card('Abonelik kayıtları',table(
            ('Firma','Paket','Başlangıç','Bitiş','Durum','İşlem'),
            ([e(r[k]) for k in ('name','package','starts','ends','status')]+[edit_link(path,r['company_id'])] for r in rows)))+ats_card
    if path=='/kullanicilar':
        rows = db.execute('''SELECT users.*,companies.name AS company FROM users LEFT JOIN companies ON companies.id=company_id'''+
                          (" WHERE company_id=? AND role='staff'" if tenant else '')+' ORDER BY users.id DESC',
                          (tenant,) if tenant else ()).fetchall()
        current = require(next((r for r in rows if r['id']==edit and r['role']=='staff'),None),'Personel') if edit else None
        fields = inp('Ad soyad','name',current['name'] if current else '')
        if not tenant:
            fields += hidden('company_id',current['company_id']) if current else tenant_field(db)
        fields += (hidden('id',edit)+select('Durum','active',[('1','Aktif'),('0','Kapalı')],str(current['active']))
                   if current else account_fields())
        permissions = ''
        if not current:
            ik_fields = (hidden('action','ik_account')+fields+
                         select('İK rolü','ik_role',[('hr_manager','İK Müdürü'),('hr_staff','İK Personeli')]))
            permissions = card('ERNASA HR hesabı',form(path,actor,ik_fields,'İK hesabı oluştur'))
        if actor['role'] == 'supervisor' and current:
            granted = db.execute('SELECT 1 FROM staff_subscription_access WHERE user_id=?',(edit,)).fetchone()
            if granted or delegation_allowed(db,tenant):
                fields_access = (hidden('id',edit)+hidden('action',STAFF_PERMISSION)+
                                 hidden('enabled','0' if granted else '1'))
                permissions = card('Abonelik görüntüleme',form(path,actor,fields_access,
                                   'Yetkiyi kaldır' if granted else 'Yetki ver'))
        return record_editor(path,actor,fields,'Personel düzenle' if edit else 'Personel oluştur',edit)+permissions+card('Hesaplar',table(
            ('Ad soyad','Firma','E-posta','Rol','Durum','İşlem'),
            ([e(r['name']),e(r['company'] or '—'),e(r['email']),e(ROLES[r['role']]),'Aktif' if r['active'] else 'Kapalı',
              edit_link(path,r['id']) if r['role']=='staff' else '—'] for r in rows)))
    if path=='/roller':
        return card('Sistem rolleri',table(('Rol','Kapsam'),zip(ROLES.values(),(
            'Platform yönetimi','Kendi firmasındaki personel','Kendi firma hesabı'))))
    if path=='/yetkiler':
        limits = db.execute('SELECT companies.id,name,delegation_limits.company_id AS enabled FROM companies LEFT JOIN delegation_limits ON company_id=companies.id ORDER BY name')
        return card('Ekran erişimi',table(('Ekran',*ROLES.values()),
            ([label,*('Yetkiyle' if route=='/abonelikler' and role=='staff' else
                      'Var' if route in ACCESS[role] else '—' for role in ROLES)] for route,label in NAV.items())))+card(
            'Firma bazında abonelik görüntüleme yetki devri',table(('Firma','Durum','İşlem'),
                ([e(row['name']),'Açık' if row['enabled'] else 'Kapalı',
                  form(path,actor,hidden('company_id',row['id'])+hidden('enabled','0' if row['enabled'] else '1'),
                       'Devri kapat' if row['enabled'] else 'Devri aç')] for row in limits)))
    if path=='/denetim':
        search = q.get('q','')[:120].strip()
        tenant = number({'id':q.get('firma','')},optional=True)
        rows = db.execute("""SELECT * FROM audit WHERE (? IS NULL OR company_id=?)
            AND (instr(lower(action),lower(?)) OR instr(lower(actor),lower(?)))
            ORDER BY id DESC LIMIT 200""",(tenant,tenant,search,search))
        filters = (f'<form method="get" class="search">{inp("İşlem veya hesap","q",search,required=False)}'
                   f'{inp("Firma numarası","firma",tenant or "",kind="number",required=False)}<button>Filtrele</button></form>')
        return card('Son 200 eşleşme',filters+events_table(rows,True))
    name = db.execute('SELECT name FROM settings WHERE id=1').fetchone()[0]
    return card('Platform kimliği',form(path,actor,inp('Sistem adı','name',name),'Kaydet'))


def ernasa_cv_evidence(items):
    if not items:
        return ''
    return ('<section id="ernasa-cv-evidence"><h3>CV’deki dayanaklar</h3>'
            '<p>CV’de kanıt bulunmaması tek başına olumsuz değerlendirme sebebi değildir. '
            'Ortak puan yalnızca 10 kilit mülakat yanıtından hesaplanır.</p>'
            +table(('Kriter','CV dayanağı'),([e(row['label']),e(row['evidence'])] for row in items))+'</section>')


def ernasa_invitation_page(handler, db, actor, submitted, query):
    profile = ik_bridge.active_portal(db, ERNASA_INVITE_SLUG)
    if profile is None:
        raise Problem(503,'ERNASA davet hesabı etkin değil.')
    profile = dict(profile,month_bounds=ik_bridge.month_bounds())
    first_round_template = interview_templates.resolve_partner_first_round(db)
    if submitted is not None:
        action = submitted.get('action')
        if action == 'prepare':
            token = handler('prepare',actor['id'],profile,name=field(submitted,'name',160),email=email(submitted),
                            filename=submitted.get('cv_filename',''),cv=submitted.get('cv',b''))
            return ERNASA_INVITE_PATH+'?taslak='+token
        if action == 'create':
            draft = submitted.get('draft','')
            if not re.fullmatch(r'[a-f0-9]{32}',draft):
                raise Problem(404,'CV soru taslağı bulunamadı.')
            extras = [submitted.get(f'extra_{index}','').strip() for index in range(1,4)]
            introduction = submitted.get('introduction','') if submitted.get('include_introduction') == '1' else None
            token = handler('create',actor['id'],profile,token=draft,
                            extras=tuple(question for question in extras if question),introduction=introduction,
                            template=first_round_template)
        elif action == 'evaluate':
            token = submitted.get('token','')
            if not re.fullmatch(r'[A-Za-z0-9_-]{43}',token):
                raise Problem(404,'Davet bulunamadı.')
            handler('evaluate',actor['id'],profile,token=token)
        else:
            raise Problem(400,'Davet işlemi geçersiz.')
        return ERNASA_INVITE_PATH+'?davet='+token
    statuses = {'waiting':'Davet hazır','created':'Davet hazır','opened':'Bağlantı açıldı',
                'started':'Mülakat başladı','completed':'Tamamlandı','expired':'Süresi doldu'}
    create = (f'<form method="post" action="{ERNASA_INVITE_PATH}" enctype="multipart/form-data">'
              +hidden('csrf',actor['csrf'])+hidden('action','prepare')+'<div class="fields">'
              +inp('Aday adı ve soyadı','name')+inp('Aday e-posta adresi','email',kind='email')
              +'<label>Sentetik CV (PDF veya DOCX)<input name="cv" type="file" accept=".pdf,.docx" required></label>'
              +'</div><button>CV’yi analiz et</button></form>')
    content = card('ERNASA Satış Ortağı',create)
    draft_id = query.get('taslak','')
    if draft_id:
        if not re.fullmatch(r'[a-f0-9]{32}',draft_id):
            raise Problem(404,'CV soru taslağı bulunamadı.')
        draft = handler('draft',actor['id'],profile,token=draft_id,template=first_round_template)
        review = f'<p><strong>{e(draft["name"])}</strong> · CV analizi tamamlandı.</p>'
        review += ernasa_cv_evidence(draft['cv_evidence'])
        if draft['locked']:
            review += '<details><summary>10 kilit soru · mevcut sıra</summary><ol>'+''.join(
                f'<li>{e(question)}</li>' for question in draft['locked'])+'</ol></details>'
        else:
            review += '<p role="alert">Onaylı 10 Türkçe soru bekleniyor. Davet bağlantısı henüz oluşturulamaz.</p>'
        fields = hidden('action','create')+hidden('draft',draft_id)
        fields += ('<div class="template-question"><label>'
                   '<input type="checkbox" name="include_introduction" value="1">'
                   'Tanışma sorusu ekle (puansız)</label>'
                   '<label>Tanışma sorusu metni'
                   f'<textarea name="introduction" maxlength="{interview_templates.PARTNER_INTRODUCTION_LIMIT}">'
                   f'{e(interview_templates.PARTNER_INTRODUCTION)}</textarea></label></div>')
        for index in range(1,4):
            suggestion = draft['suggestions'][index-1] if len(draft['suggestions']) >= index else {}
            fields += (f'<div class="template-question"><label>Ek soru {index}'
                       f'<textarea name="extra_{index}" maxlength="700" {"required" if index<3 else ""}>'
                       f'{e(suggestion.get("question",""))}</textarea></label>'
                       f'<small>{e(suggestion.get("evidence","İsteğe bağlı"))}</small></div>')
        review_form = form(ERNASA_INVITE_PATH,actor,fields,'Ek soruları onayla ve davet oluştur')
        if not draft['locked']:
            review_form = review_form.replace('<button>','<button disabled>',1)
        content = card('CV’ye özel soruları incele',review+review_form)+content
    token = query.get('davet','')
    if token:
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}',token):
            raise Problem(404,'Davet bulunamadı.')
        invite = handler('detail',actor['id'],profile,token=token)
        detail = (f'<p><strong>{e(invite["name"])}</strong> · {e(statuses.get(invite["status"],"Bilinmiyor"))}</p>'
            '<label>Adaya özel mülakat bağlantısı'
            f'<input id="ernasa-invite-url" type="url" readonly value="{e(invite["url"])}"></label>'
            '<button type="button" id="ernasa-invite-copy">Bağlantıyı kopyala</button>'
            '<p><a href="https://privateemail.com/spm/mail/INBOX/" target="_blank" rel="noopener noreferrer">PrivateEmail’de Aç</a></p>'
            f'<label>Alıcı adresi<input id="ernasa-invite-to" readonly value="{e(invite["email"])}"></label>'
            '<button type="button" id="ernasa-invite-copy-to">Alıcıyı Kopyala</button>'
            '<label>Davet konusu<input id="ernasa-invite-subject" readonly value="ERNASA · B2B Satış Ortağı 1. Tur Mülakat Daveti"></label>'
            '<button type="button" id="ernasa-invite-copy-subject">Konuyu Kopyala</button>'
            f'<label>Davet mesajı<textarea id="ernasa-invite-body" readonly>Merhaba {e(invite["name"])},\n\n'
            f'ERNASA B2B Satış Ortağı 1. Tur mülakat bağlantınız: {e(invite["url"])}\n\n'
            'Bağlantı oluşturulduğu andan itibaren 8 saat geçerlidir.\n\nİyi çalışmalar,\nERNASA Yazılım Sistemleri</textarea></label>'
            '<button type="button" id="ernasa-invite-copy-body">Mesajı Kopyala</button>'
            '<p id="ernasa-invite-copy-feedback" role="status"></p>'
            '<script src="/ernasa-invite-copy.js?v=20261117-privateemail2" defer></script>')
        content = card('Aday daveti',detail)+content
        if invite['status'] == 'completed':
            answers = invite.get('answers',())
            def answer_table(items):
                return table(('Sıra','Soru','Adayın cevabı'),([item['question_number'],e(item['question']),e(item['answer'])] for item in items))
            result = f'<p>{e(invite.get("analysis") or "Değerlendirme yapılmadı")}</p>'
            if invite.get('report'):
                report = json.loads(invite['report'])
                result = f'<p>Ortak puanı <strong>{e(report["score"])} / 100</strong>. Nihai karar kurucudadır.</p>'
                result += table(('Kriter','Ağırlık','Puan','Gerekçe ve yanıt dayanağı'),(
                    [e(item['label']),str(item['weight'])+'%',str(item['score'])+'/100',e(item['reason']) +
                     ''.join(f'<br>Soru {quote["number"]} · {e(quote["quote"])}' for quote in item['evidence'])]
                    for item in report['criteria']))
            if invite.get('question_count'):
                if invite.get('error'):
                    result += f'<p role="alert">{e(invite["error"])}</p>'
                label = 'AI değerlendirmesini yeniden dene' if invite.get('error') else 'AI değerlendirmesi çalıştır'
                result += form(ERNASA_INVITE_PATH,actor,hidden('token',token)+hidden('action','evaluate'),label)
            result += ernasa_cv_evidence(invite.get('cv_evidence'))
            if invite.get('introduction_note'):
                result += f'<h3>Tanışma notu</h3><p>{e(invite["introduction_note"])}</p>'
            title = '10 kilit soru' if invite.get('question_count') else 'Mülakat soruları'
            result += f'<h3>{title}</h3>'+answer_table(item for item in answers if item['question_number']<=10)
            if invite.get('question_count'):
                result += '<h3>CV’ye özel sorular</h3>'+answer_table(item for item in answers if item['question_number']>10)
            content += card('Mülakat sonucu',result)
    rows = ([e(item['name']),e(item['email']),e(statuses.get(item['status'],'Bilinmiyor')),
        f'<a href="{ERNASA_INVITE_PATH}?davet={e(item["token"])}">'+
        ('Sonucu aç' if item['status']=='completed' else 'Daveti aç')+'</a>']
        for item in handler('list',actor['id'],profile))
    return content+card('ERNASA davetleri',table(('Aday','E-posta','Durum','İşlem'),rows))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, body=b'', content='text/html; charset=utf-8', headers=()):
        self.send_response(status)
        self.send_header('Content-Type',content)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        # Form navigations under no-referrer send Origin: null.
        self.send_header('Referrer-Policy','same-origin')
        self.send_header('Content-Security-Policy',
                         "default-src 'none'; style-src 'self'; img-src 'self'; script-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        for key,value in headers:
            self.send_header(key,value)
        self.end_headers()
        self.wfile.write(body)

    def page(self, title, content, actor=None, status=200, path='', brand='ERNASA CONTROL', headers=()):
        if actor:
            routes = actor['routes'] if 'routes' in actor.keys() else ACCESS[actor['role']]
            menu = navigation(routes,path)
            group = next((label for label,routes in GROUPS if path in routes),'Platform')
            body = ('<a class="skip" href="#icerik">İçeriğe geç</a>'
                    f'<aside>{brand_block(brand)}<div class="desktop-nav">{menu}</div>'
                    f'<details class="mobile-nav"><summary>Yönetim menüsü</summary>{menu}</details></aside>'
                    f'<div class="shell"><header><p class="breadcrumb">{e(group)} <span>/</span> {e(title)}</p>'
                    '<div class="account"><span class="health">Yönetim paneli</span>'
                    f'<div><strong>{e(actor["name"])}</strong><small>{e(actor["email"])} · {ROLES[actor["role"]]}</small></div>'
                    f'{form("/cikis",actor,"","Çıkış")}</div></header>'
                    f'<main id="icerik"><h1>{e(title)}</h1>{content}</main></div>')
        else:
            body = (f'<main class="login"><section class="login-brand">{brand_block(brand)}</section>'
                    f'<section class="login-content"><h1>{e(title)}</h1>{content}</section></main>')
        page = ('<!doctype html><html lang="tr"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                f'<title>{e(title)} · ERNASA CONTROL</title><link rel="stylesheet" href="/control.css">'
                '<link rel="icon" type="image/png" href="/ernasalogo.png">'+
                ('<script src="/sales-menu.js" defer></script>' if path=='/satis' else '')+
                f'</head><body>{body}</body></html>')
        if self.server.public_prefix:
            page = re.sub(r'((?:href|src|action)="/)(?!/)', lambda match: match[0][:-1] + self.server.public_prefix + '/', page)
        self.reply(status,page.encode(),headers=headers)

    def redirect(self, path, token=None, age=TTL):
        headers = [('Location',self.server.public_prefix + path)]
        if token is not None:
            secure = '; Secure' if self.server.origin.startswith('https://') else ''
            cookie_path = self.server.public_prefix or '/'
            headers.append(('Set-Cookie',f'control_session={token}; Path={cookie_path}; HttpOnly; SameSite=Strict; Max-Age={age}{secure}'))
        self.reply(303,headers=headers)

    def session(self,db):
        cookies = SimpleCookie(self.headers.get('Cookie',''))
        cookie = cookies.get('control_session')
        if not cookie:
            return None
        return db.execute("""SELECT users.*,sessions.csrf,sessions.token FROM sessions JOIN users ON users.id=user_id
          LEFT JOIN companies ON companies.id=users.company_id WHERE token=? AND expires>? AND users.active=1
          AND (role='founder' OR companies.status='Aktif')""",(token_hash(cookie.value),int(time.time()))).fetchone()

    def data(self):
        size = int(self.headers.get('Content-Length','0'))
        is_multipart = self.headers.get_content_type() == 'multipart/form-data'
        multipart = self.path if is_multipart and self.path in ('/firmalar',ERNASA_INVITE_PATH) else None
        limits = {'/firmalar':520000,ERNASA_INVITE_PATH:10*1024*1024+8192}
        if not 1 <= size <= limits.get(multipart,16384):
            raise Problem(413,'Form boyutu sınır dışında.')
        body = self.rfile.read(size)
        if self.headers.get('Origin') != self.server.origin:
            raise Problem(403,'İstek kaynağı doğrulanamadı.')
        if multipart:
            message = BytesParser(policy=email_policy).parsebytes(
                ('Content-Type: '+self.headers['Content-Type']+'\r\n\r\n').encode()+body)
            if not message.is_multipart() or message.defects:
                raise Problem(400,'Dosya formu okunamadı.')
            fields = {}
            keys = ('csrf','id','action','logo') if multipart=='/firmalar' else ('csrf','action','name','email','cv')
            file_key = 'logo' if multipart=='/firmalar' else 'cv'
            for part in message.iter_parts():
                key = part.get_param('name',header='content-disposition')
                if key not in keys or key in fields or part.is_multipart():
                    raise Problem(400,'Dosya formu geçersiz.')
                raw = part.get_payload(decode=True)
                fields[key] = raw if key == file_key else raw.decode()
                if key == 'cv':
                    fields['cv_filename'] = part.get_filename() or ''
            if fields.get('action') != ('logo' if multipart=='/firmalar' else 'prepare'):
                raise Problem(400,'Dosya işlemi geçersiz.')
            return fields
        if self.headers.get_content_type() != 'application/x-www-form-urlencoded':
            raise Problem(415,'Form içeriği bekleniyor.')
        return {k:v[-1] for k,v in parse_qs(body.decode(),keep_blank_values=True,max_num_fields=40).items()}

    def login(self,db,data):
        cookie = SimpleCookie(self.headers.get('Cookie','')).get('control_login_csrf')
        csrf = data.get('csrf','')
        if not cookie or not csrf or not hmac.compare_digest(cookie.value.encode(),csrf.encode()):
            raise Problem(403,'Giriş formu doğrulanamadı. Giriş ekranını yenileyin.')
        now = int(time.time())
        with self.server.login_lock:
            self.server.attempts = [t for t in self.server.attempts if t > now-60]
            if len(self.server.attempts)>=12:
                raise Problem(429,'Çok fazla giriş denemesi. Bir dakika sonra tekrar deneyin.')
            self.server.attempts.append(now)
        address = email(data)
        password = data.get('password','')
        if not 1 <= len(password) <= 128:
            raise Problem(401,'Giriş bilgileri geçersiz.')
        row = db.execute("""SELECT users.* FROM users LEFT JOIN companies ON companies.id=company_id
          LEFT JOIN ik_personnel_details d ON d.user_id=users.id
          WHERE email=? AND users.active=1 AND (role='founder' OR companies.status='Aktif')
          AND COALESCE(d.must_change_password,0)=0""",(address,)).fetchone()
        saved = row['secret'] if row else DUMMY
        valid = hmac.compare_digest(saved,digest(password,bytes.fromhex(saved.split(':')[0])))
        if row is None or not valid:
            raise Problem(401,'Giriş bilgileri geçersiz.')
        token = secrets.token_urlsafe(32)
        db.execute('DELETE FROM sessions WHERE expires<=?',(now,))
        db.execute('INSERT INTO sessions VALUES(?,?,?,?)',(token_hash(token),row['id'],secrets.token_urlsafe(32),now+TTL))
        audit(db,row,'Oturum açıldı',row['company_id'])
        return token

    def dispatch(self,post):
        actor = None
        try:
            internal = self.path == '/internal/ik' and self.headers.get('Host') == getattr(self.server,'internal_host',None)
            if not internal and self.headers.get('Host') != urlsplit(self.server.origin).netloc:
                raise Problem(403,'Yerel sunucu adresini kullanın.')
            url = urlsplit(self.path)
            path = url.path
            if not post and path in ASSETS:
                filename, content_type = ASSETS[path]
                return self.reply(200,(ROOT/filename).read_bytes(),content_type)
            if sales_http.handle(self,path,post):
                return
            if not post and path == '/ernasa-invite-copy.js':
                return self.reply(200,(ROOT/'ernasa-invite-copy.js').read_bytes(),'application/javascript; charset=utf-8')
            if not post and path == '/address-picker.js':
                payload = json.dumps(address_catalog.catalog(),ensure_ascii=True,separators=(',',':'))
                script = (f'const addressCatalog = {payload};\n'.encode()
                          + (ROOT/'address-picker.js').read_bytes())
                return self.reply(200,script,'application/javascript; charset=utf-8')
            if path == '/internal/ik':
                if not post or not ik_bridge.authenticated(self.headers.get('Authorization','')):
                    return self.reply(403)
                size = int(self.headers.get('Content-Length','0'))
                if self.headers.get_content_type() != 'application/json' or not 1 <= size <= 700000:
                    return self.reply(400)
                data = json.loads(self.rfile.read(size))
                if not isinstance(data,dict) or any(not isinstance(value,str) for value in data.values()):
                    return self.reply(400)
                with closing(connect(self.server.db_path)) as db:
                    operation = data.get('operation')
                    if size > 4096 and operation != 'company_logo':
                        return self.reply(400)
                    account_ops = ('self','change_own_password','company_settings','company_sector','company_logo')
                    if operation in (*account_ops,'login','personnel_list','personnel_update','personnel_create','personnel_reset','personnel_grants'):
                        try:
                            with db:
                                db.execute('BEGIN IMMEDIATE')
                                if operation == 'login':
                                    result = portal_sign_in(db,data)
                                elif operation in account_ops:
                                    result = portal_account_operation(db,data)
                                else:
                                    result = portal_personnel(db,data)
                        except Problem as error:
                            result = {'error':{'status':error.status,'detail':str(error)}}
                        except (sqlite3.IntegrityError, psycopg2.IntegrityError):
                            result = {'error':{'status':409,'detail':'E-posta adresi zaten kayıtlı.'}}
                    else:
                        result = ik_bridge.respond(db,data,check_ik_password)
                return self.reply(200,json.dumps(result).encode(),'application/json')
            with closing(connect(self.server.db_path)) as db, db:
                if post:
                    if path==ERNASA_INVITE_PATH:
                        owner = self.session(db)
                        if not owner or owner['role'] != 'founder' or not getattr(self.server,'ernasa_invite_handler',None):
                            raise Problem(403,'Kurucu yetkisi gerekir.')
                    data = self.data()
                    if path!=ERNASA_INVITE_PATH:
                        db.execute('BEGIN IMMEDIATE')
                    if path=='/giris':
                        token = self.login(db,data)
                        db.commit()
                        return self.redirect('/',token)
                actor = self.session(db)
                if not actor:
                    if post:
                        raise Problem(401,'Oturum açmanız gerekiyor.')
                    if path!='/giris':
                        return self.redirect('/giris')
                    fields = ('<label>E-posta<input type="email" name="email" autocomplete="username" required></label>'
                              '<label>Parola<input type="password" name="password" autocomplete="current-password" required></label>')
                    csrf = secrets.token_urlsafe(32)
                    secure = '; Secure' if self.server.origin.startswith('https://') else ''
                    cookie = f'control_login_csrf={csrf}; Path={self.server.public_prefix}/giris; HttpOnly; SameSite=Strict; Max-Age={LOGIN_CSRF_TTL}{secure}'
                    return self.page('Yönetim girişi',form('/giris',{'csrf':csrf},fields,'Giriş yap'),
                                     headers=(('Set-Cookie',cookie),))
                if post and not hmac.compare_digest(actor['csrf'].encode(),data.get('csrf','').encode()):
                    raise Problem(403,'Form doğrulanamadı.')
                if post and path=='/cikis':
                    db.execute('DELETE FROM sessions WHERE token=?',(actor['token'],))
                    audit(db,actor,'Oturum kapatıldı',actor['company_id'])
                    db.commit()
                    return self.redirect('/giris','',0)
                if path=='/giris':
                    return self.redirect('/')
                if not post and path=='/firma-logo':
                    if actor['role']!='founder':
                        raise Problem(403,'Bu dosya için yetkiniz yok.')
                    query = {k:v[-1] for k,v in parse_qs(url.query,max_num_fields=4).items()}
                    logo = require(db.execute('SELECT png FROM company_logos WHERE company_id=?',
                                   (number(query),)).fetchone(),'Logo')
                    return self.reply(200,logo['png'],'image/png')
                if path not in NAV:
                    raise Problem(404,'Ekran bulunamadı.')
                actor = dict(actor)
                actor['routes'] = allowed_routes(db,actor)
                if not getattr(self.server,'ernasa_invite_handler',None):
                    actor['routes'] = actor['routes'] - {ERNASA_INVITE_PATH}
                if not getattr(self.server,'experience_summary',None):
                    actor['routes'] = actor['routes'] - {'/aday-deneyimi'}
                if path not in actor['routes'] or (post and actor['role']=='staff'):
                    raise Problem(403,'Bu ekran için yetkiniz yok.')
                if post and path=='/aday-deneyimi':
                    raise Problem(405,'Bu ekranda değişiklik yapılamaz.')
                if post:
                    target = (ernasa_invitation_page(self.server.ernasa_invite_handler,db,actor,data,{})
                              if path==ERNASA_INVITE_PATH else mutate(db,actor,path,data))
                    db.commit()
                    target = target or path
                    return self.redirect(target+('&' if '?' in target else '?')+'kaydedildi=1')
                query = {k:v[-1] for k,v in parse_qs(url.query,max_num_fields=12).items()}
                content = (candidate_experience_panel(self.server.experience_summary()) if path=='/aday-deneyimi'
                           else ernasa_invitation_page(self.server.ernasa_invite_handler,db,actor,None,query)
                           if path==ERNASA_INVITE_PATH else screen(db,actor,path,query))
                if query.get('kaydedildi')=='1':
                    content = '<p class="notice" role="status">İşlem kaydedildi.</p>'+content
                brand = db.execute('SELECT name FROM settings WHERE id=1').fetchone()[0]
            self.page(NAV[path],content,actor,path=path,brand=brand)
            return
        except (sqlite3.IntegrityError, psycopg2.IntegrityError):
            error = Problem(409,'Ad veya e-posta zaten kayıtlı ya da kayıt koşulları sağlanmadı.')
        except (sqlite3.OperationalError, psycopg2.OperationalError):
            error = Problem(503,'Veritabanı işlemi tamamlanamadı.')
        except Problem as exc:
            error = exc
        except firma.Invalid as exc:
            error = Problem(400,str(exc))
        except ValueError:
            error = Problem(400,'İstek okunamadı.')
        if actor and (error.status == 403 or (post and error.status == 404)) and path in ('/firmalar','/abonelikler','/kullanicilar'):
            # Record denials after the original transaction rolls back. Never log request fields.
            try:
                with closing(connect(self.server.db_path)) as db, db:
                    audit(db,actor,f'Erişim reddedildi ({self.command} {path}, HTTP {error.status})',actor['company_id'])
            except (sqlite3.Error, psycopg2.Error):
                pass
        self.page('İşlem tamamlanamadı',f'<p>{e(error)}</p><p><a href="{"/" if actor else "/giris"}">Geri dön</a></p>',actor,error.status)

    def do_GET(self):
        self.dispatch(False)

    def do_POST(self):
        self.dispatch(True)


def server(path,port):
    httpd = ThreadingHTTPServer(('127.0.0.1',port),Handler)
    httpd.db_path = path
    httpd.origin = f'http://127.0.0.1:{httpd.server_port}'
    httpd.public_prefix = ''
    httpd.attempts = []
    httpd.login_lock = threading.Lock()
    return httpd


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command',choices=('serve','kurucu'))
    parser.add_argument('--port',type=int,default=8108)
    parser.add_argument('--db',type=Path,default=ROOT/'control.sqlite3')
    args = parser.parse_args()
    path = args.db
    init(path)
    if args.command=='kurucu':
        with closing(connect(path)) as db, db:
            if db.execute("SELECT 1 FROM users WHERE role='founder'").fetchone():
                parser.exit(1,'Kurucu hesabı zaten kayıtlı.\n')
            try:
                address = input('Kurucu e-posta: ')
                with warnings.catch_warnings():
                    warnings.simplefilter('error',getpass.GetPassWarning)
                    password = getpass.getpass('Kurucu parolası: ')
                    if password != getpass.getpass('Parolayı tekrar girin: '):
                        raise Problem(400,'Parolalar eşleşmiyor.')
                add_user(db,{'email':address,'password':password,'name':'Kurucu'},'founder',None)
            except (EOFError,KeyboardInterrupt,getpass.GetPassWarning,Problem,sqlite3.IntegrityError):
                parser.exit(1,'Kayıt tamamlanamadı. E-posta ve en az 12 karakter parola kullanın.\n')
        print('Kurucu hesabı oluşturuldu.')
        return
    with server(path,args.port) as httpd:
        print(httpd.origin,flush=True)
        httpd.serve_forever()


if __name__=='__main__':
    main()
