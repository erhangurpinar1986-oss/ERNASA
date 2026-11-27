"""NOVA 2.0 — ERNASA AI SALES CENTER business rules.

Caller holds one CONTROL write transaction per command (same convention as control/sales.py).
This module never talks to the network itself: e-mail and voice providers are injected
(see control/nova_providers.py), so every rule below is testable offline.

Safety defaults
- Every outbound channel starts closed: kill_switch=1, running=0, auto modes off.
- Real prospects are contacted only after the founder approves the written channel policy in
  CONTROL (email_policy_approved / call_policy_approved). Before that only founder-authorised
  test numbers / test addresses work.
- Objections are permanent and honoured on every channel (nova_suppressions is append-only).
- NOVA states only 'approved' knowledge entries; it never offers discounts, contract changes,
  special payment terms or legal commitments, and always discloses that it is an AI assistant.
"""
from datetime import datetime, timedelta, timezone
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

from .ik_bridge import ISTANBUL
from .sales import Actor, Invalid, fail, uid, stamp

ROOT = Path(__file__).resolve().parents[1]
DDL = (ROOT / 'migrations/nova/001_nova.up.sql').read_text(encoding='utf-8')
SQLITE_GUARDS = (ROOT / 'migrations/nova/002_sqlite_guards.up.sql').read_text(encoding='utf-8')
PG_GUARDS = (ROOT / 'migrations/nova/002_pg_guards.up.sql').read_text(encoding='utf-8')
TABLES = frozenset({'nova_settings', 'nova_companies', 'nova_events', 'nova_suppressions', 'nova_emails',
                    'nova_test_numbers', 'nova_calls', 'nova_demo_requests', 'nova_knowledge', 'nova_spend',
                    'nova_jobs', 'nova_reports'})

STAGES = {
    'new': 'Yeni firma', 'researching': 'Araştırılıyor', 'verified': 'Doğrulandı',
    'contactable': 'İletişime uygun', 'email_draft': 'E-posta taslağı hazır', 'email_sent': 'E-posta gönderildi',
    'awaiting_reply': 'Yanıt bekleniyor', 'call_planned': 'Telefon görüşmesi planlandı',
    'call_done': 'Görüşme gerçekleştirildi', 'interested': 'İlgileniyor', 'demo_requested': 'Demo talep etti',
    'handed_off': 'Temsilciye aktarıldı', 'proposal': 'Teklif hazırlandı', 'won': 'Satış gerçekleşti',
    'rejected': 'Reddedildi', 'do_not_contact': 'İletişim istemiyor',
}
TERMINAL = {'do_not_contact'}
# do_not_contact is reachable from everywhere and can never be left.
FLOW = {
    'new': {'researching', 'verified', 'rejected'},
    'researching': {'verified', 'rejected'},
    'verified': {'contactable', 'researching', 'rejected'},
    'contactable': {'email_draft', 'call_planned', 'handed_off', 'rejected'},
    'email_draft': {'email_sent', 'contactable', 'rejected'},
    'email_sent': {'awaiting_reply', 'interested', 'demo_requested', 'rejected', 'call_planned'},
    'awaiting_reply': {'interested', 'demo_requested', 'call_planned', 'rejected', 'email_draft'},
    'call_planned': {'call_done', 'contactable', 'awaiting_reply', 'rejected'},
    'call_done': {'interested', 'demo_requested', 'awaiting_reply', 'handed_off', 'rejected', 'call_planned'},
    'interested': {'demo_requested', 'handed_off', 'call_planned', 'proposal', 'rejected'},
    'demo_requested': {'handed_off', 'proposal', 'rejected'},
    'handed_off': {'proposal', 'won', 'rejected'},
    'proposal': {'won', 'rejected'},
    'won': set(),
    'rejected': {'researching'},
}
FREE_MAIL = frozenset({'gmail.com', 'hotmail.com', 'outlook.com', 'yahoo.com', 'icloud.com', 'yandex.com',
                       'yandex.com.tr', 'live.com', 'msn.com', 'mynet.com', 'hotmail.com.tr', 'outlook.com.tr',
                       'protonmail.com', 'proton.me', 'gmx.com', 'aol.com', 'mail.ru'})
CORPORATE_LOCALPARTS = ('info', 'iletisim', 'bilgi', 'ik', 'insankaynaklari', 'hr', 'kariyer', 'career', 'careers',
                        'contact', 'hello', 'merhaba', 'satis', 'sales', 'destek', 'support', 'muhasebe',
                        'satinalma', 'genelmudurluk', 'kurumsal', 'pazarlama', 'marketing', 'basvuru', 'office',
                        'ofis', 'recruitment', 'jobs', 'is', 'isealim', 'yonetim', 'santral', 'musteri', 'operasyon')
OPT_OUT_LINE = 'Bu tür e-postaları almak istemiyorsanız "LİSTEDEN ÇIKAR" yazarak yanıtlamanız yeterli; sizi tüm iletişim listelerimizden çıkarırız.'
SENDER_BLOCK = ('ERNASA Yazılım Sistemleri · iletisim@ernasa.com · www.ernasa.com\n'
                'Bu ileti ERNASA\'nın yapay zekâ destekli satış asistanı NOVA tarafından hazırlanmış ve yetkili onayıyla gönderilmiştir.')
# Ticari İletişim Yönetmeliği: a commercial voice call states the trade name (ticaret unvanı).
TRADE_NAME = os.getenv('ERNASA_NOVA_TRADE_NAME', 'ERNASA Yazılım Sistemleri')
AI_INTRO = ("Merhaba, ben NOVA, ERNASA'nın yapay zekâ destekli satış asistanıyım; " + TRADE_NAME + " adına arıyorum. "
            "İnsan kaynakları ve işe alım süreçlerinize yönelik geliştirdiğimiz yazılım çözümleri hakkında kısa bir bilgi "
            "vermek istiyorum. Şu anda konuşmaya uygun musunuz?")


def founder(actor):
    if not actor.founder:
        fail('NOVA AI SALES CENTER yalnızca CONTROL kurucu yetkisiyle yönetilir.', 403)


def now_local():
    return datetime.now(ISTANBUL)


def today(moment=None):
    return (moment or now_local()).astimezone(ISTANBUL).date().isoformat()


def s(data, key, limit=500, required=False):
    value = data.get(key, '')
    value = '' if value is None else str(value).strip()
    if any(ch in value for ch in '\x00'):
        fail(f'{key} alanı geçersiz karakter içeriyor.', 422)
    if len(value) > limit:
        fail(f'{key} alanı en çok {limit} karakter olabilir.', 422)
    if required and not value:
        fail(f'{key} alanı gerekli.', 422)
    return value


def event(db, company_id, kind, actor, detail='', from_stage='', to_stage=''):
    db.execute('INSERT INTO nova_events VALUES(?,?,?,?,?,?,?,?)',
               (uid(), company_id, kind, from_stage, to_stage, str(detail)[:2000], str(actor), stamp()))


# ---------------------------------------------------------------- normalisation

LEGAL_SUFFIX = re.compile(r'\b(a\.?\s?ş\.?|anonim şirketi|ltd\.?|limited|şti\.?|şirketi|san\.?|sanayi|tic\.?|ticaret|'
                          r've|inc\.?|llc|gmbh|co\.?)\b', re.I)


def name_key(name):
    text = LEGAL_SUFFIX.sub(' ', name.lower().replace('i̇', 'i'))
    text = unicodedata.normalize('NFKD', text.translate(str.maketrans('ıİşŞğĞüÜöÖçÇ', 'iissgguuoocc')))
    text = ''.join(ch for ch in text if ch.isalnum() or ch == ' ')
    return ' '.join(text.split())


DOMAIN = re.compile(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}')


def domain_of(value):
    value = (value or '').strip().lower()
    if not value:
        return None
    if '@' in value:
        value = value.rsplit('@', 1)[1]
    else:
        if '://' not in value:
            value = 'https://' + value
        value = urlsplit(value).hostname or ''
    value = value.rstrip('.')
    if value.startswith('www.'):
        value = value[4:]
    if not DOMAIN.fullmatch(value):
        return None
    return value


EMAIL = re.compile(r'[a-z0-9._%+-]{1,64}@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}')


def corporate_email(value, company_domain=None):
    """Return the address if it is a published *corporate* mailbox, else raise.

    Personal mailboxes (free-mail providers, name.surname@) are refused: NOVA never stores
    private contact data, and never guesses an address from a pattern."""
    value = (value or '').strip().lower()
    if not value:
        return ''
    if not EMAIL.fullmatch(value):
        fail('Kurumsal e-posta adresi geçersiz.', 422)
    local, host = value.split('@')
    if host in FREE_MAIL:
        fail('Ücretsiz/kişisel e-posta servisleri veri havuzuna alınmaz.', 422)
    if company_domain and not (host == company_domain or host.endswith('.' + company_domain)
                               or company_domain.endswith('.' + host)):
        fail('E-posta alan adı firmanın web sitesiyle eşleşmiyor; kaynağı doğrulayın.', 422)
    if not local.replace('.', '').replace('-', '').replace('_', '').startswith(CORPORATE_LOCALPARTS) and re.fullmatch(r'[a-z]+[._][a-z]+', local):
        fail('Kişiye ait görünen e-posta adresi (ad.soyad) kaydedilmez; kurumsal kutu kullanın.', 422)
    return value


def phone_e164(value):
    digits = re.sub(r'[^0-9+]', '', value or '')
    if not digits:
        return ''
    if digits.startswith('+'):
        digits = digits[1:]
    elif digits.startswith('00'):
        digits = digits[2:]
    elif digits.startswith('0'):
        digits = '90' + digits[1:]
    elif len(digits) == 10:
        digits = '90' + digits
    if digits.startswith('90') and len(digits) != 12:
        fail('Türkiye telefon numarası 10 haneli olmalı.', 422)
    if not 8 <= len(digits) <= 15:
        fail('Telefon numarası geçersiz.', 422)
    return '+' + digits


def is_tr_mobile(e164):
    return e164.startswith('+905')


def corporate_phone(value):
    """Corporate switchboard / landline / 444 numbers only. Mobile numbers are personal data."""
    number = phone_e164(value)
    if number and is_tr_mobile(number):
        fail('Cep telefonu numaraları (05xx) kişisel veri kabul edilir ve veri havuzuna alınmaz.', 422)
    return number


def https_url(value, key):
    value = (value or '').strip()
    if not value:
        return ''
    parts = urlsplit(value)
    if parts.scheme not in ('https', 'http') or not parts.hostname or parts.username or parts.password:
        fail(f'{key}: geçerli bir http(s) bağlantısı gerekli.', 422)
    return value[:1000]


# ---------------------------------------------------------------- scoring

def score(evidence):
    """Evidence-based 0-100 score. Each component needs its own source; no source -> 0.

    evidence keys (all optional): active_job (bool), job_source_url, job_seen_on (ISO date),
    workload_terms (list[str]), other_open_jobs (int), corporate_email (bool), corporate_phone (bool)."""
    detail = {}
    job_ok = bool(evidence.get('active_job') and evidence.get('job_source_url'))
    recent = False
    if job_ok and evidence.get('job_seen_on'):
        try:
            seen = datetime.fromisoformat(str(evidence['job_seen_on'])[:10]).date()
            recent = (now_local().date() - seen).days <= 45
        except ValueError:
            recent = False
    detail['aktif_ilan'] = 40 if job_ok and recent else (20 if job_ok else 0)
    terms = [t for t in evidence.get('workload_terms') or [] if isinstance(t, str) and t.strip()]
    detail['is_yuku'] = min(25, 10 * len(terms)) if job_ok else 0
    try:
        others = max(0, int(evidence.get('other_open_jobs') or 0))
    except (TypeError, ValueError):
        others = 0
    detail['ise_alim_hacmi'] = min(15, others * 3) if job_ok else 0
    detail['kurumsal_kanal'] = 20 if evidence.get('corporate_email') else (10 if evidence.get('corporate_phone') else 0)
    total = sum(detail.values())
    return min(100, total), detail


PACKAGES = (('Starter', 100), ('Growth', 500), ('Business', 1000), ('PRO', 2500))


def suggest_package(employee_count):
    """Map a verified head-count range to a starting HR FLOW plan. Empty when unknown."""
    numbers = [int(n) for n in re.findall(r'\d+', str(employee_count or '').replace('.', ''))]
    if not numbers:
        return ''
    size = max(numbers)
    if size < 100:
        return 'Starter'
    if size < 500:
        return 'Growth'
    if size < 2000:
        return 'Business'
    return 'PRO'


# ---------------------------------------------------------------- settings / kill switch

SETTING_INTS = {'daily_research_target': (0, 500), 'daily_email_limit': (0, 200), 'daily_call_limit': (0, 200),
                'max_call_seconds': (30, 1800), 'daily_spend_limit_minor': (0, 10**9),
                'monthly_spend_limit_minor': (0, 10**10), 'call_rate_minor_per_minute': (0, 10**6)}
SETTING_FLAGS = ('running', 'auto_email', 'auto_call', 'email_policy_approved', 'call_policy_approved', 'kill_switch', 'require_iys')
SETTING_TEXT = {'work_start': 5, 'work_end': 5, 'work_days': 20, 'target_sectors': 1000, 'target_regions': 1000,
                'product_priority': 200, 'handoff_rules': 2000}


def settings(db):
    row = db.execute('SELECT * FROM nova_settings WHERE id=1').fetchone()
    if row is None:
        fail('NOVA ayarları kurulmamış; migration uygulanmalı.', 503)
    return dict(row)


def update_settings(db, actor, data):
    founder(actor)
    current = settings(db)
    changes = {}
    for key, (low, high) in SETTING_INTS.items():
        if key in data and str(data[key]).strip() != '':
            raw = str(data[key]).strip()
            if not re.fullmatch(r'[0-9]{1,11}', raw) or not low <= int(raw) <= high:
                fail(f'{key}: {low}–{high} arası tam sayı gerekli.', 422)
            changes[key] = int(raw)
    for key in SETTING_FLAGS:
        if key in data:
            value = str(data[key]).strip()
            if value not in ('0', '1'):
                fail(f'{key}: 0 veya 1 olmalı.', 422)
            changes[key] = int(value)
    for key, limit in SETTING_TEXT.items():
        if key in data:
            changes[key] = s(data, key, limit)
    for key in ('work_start', 'work_end'):
        if key in changes and not re.fullmatch(r'([01][0-9]|2[0-3]):[0-5][0-9]', changes[key]):
            fail(f'{key}: SS:DD biçiminde olmalı.', 422)
    if 'work_days' in changes and not re.fullmatch(r'[1-7](,[1-7]){0,6}', changes['work_days']):
        fail('work_days: 1 (Pzt) – 7 (Paz) arası, virgülle ayrılmış.', 422)
    merged = {**current, **changes}
    if merged['auto_email'] and not merged['email_policy_approved']:
        fail('Otomatik e-posta için önce mevzuata uygun gönderim politikası onaylanmalı.', 409)
    if merged['auto_call'] and not merged['call_policy_approved']:
        fail('Otomatik arama için önce arama politikası ve hukuki koşullar onaylanmalı.', 409)
    if merged['running'] and merged['kill_switch']:
        fail('Acil durdurma açıkken NOVA başlatılamaz; önce acil durdurmayı kaldırın.', 409)
    if not changes:
        return current
    changes['updated_by'] = actor.id
    changes['updated_at'] = stamp()
    assignments = ','.join(f'{key}=?' for key in changes)
    db.execute(f'UPDATE nova_settings SET {assignments} WHERE id=1', tuple(changes.values()))
    event(db, None, 'settings', actor.id, json.dumps({k: v for k, v in changes.items() if k not in ('updated_by', 'updated_at')},
                                                      ensure_ascii=False))
    return settings(db)


def emergency_stop(db, actor, reason='Acil durdurma'):
    """Stop every outbound activity at once. Allowed for the founder at any time."""
    founder(actor)
    db.execute("UPDATE nova_settings SET kill_switch=1,running=0,auto_email=0,auto_call=0,updated_by=?,updated_at=? WHERE id=1",
               (actor.id, stamp()))
    cancelled = db.execute("UPDATE nova_calls SET status='cancelled' WHERE status='planned'").rowcount
    db.execute("UPDATE nova_jobs SET status='cancelled',updated_at=? WHERE status='queued' AND kind IN ('send_email','start_call')",
               (stamp(),))
    event(db, None, 'emergency_stop', actor.id, f'{reason} · iptal edilen planlı arama: {cancelled}')
    return settings(db)


def within_hours(cfg, moment=None):
    moment = (moment or now_local()).astimezone(ISTANBUL)
    days = {int(d) for d in cfg['work_days'].split(',') if d}
    if moment.isoweekday() not in days:
        return False
    hhmm = moment.strftime('%H:%M')
    return cfg['work_start'] <= hhmm < cfg['work_end']


def outbound_gate(db, channel, moment=None, test=False):
    """Raise unless outbound contact on `channel` is currently allowed."""
    cfg = settings(db)
    if cfg['kill_switch']:
        fail('Acil durdurma açık: dış iletişim kapalı.', 423)
    if not cfg['running']:
        fail('NOVA durdurulmuş durumda.', 423)
    if not test:
        if channel == 'email' and not cfg['email_policy_approved']:
            fail('Mevzuata uygun e-posta gönderim politikası CONTROL\'de onaylanmadan gerçek firmalara ileti gönderilmez.', 423)
        if channel == 'call' and not cfg['call_policy_approved']:
            fail('Arama politikası ve hukuki koşullar onaylanmadan gerçek firmalar aranmaz.', 423)
    if not within_hours(cfg, moment):
        fail('Çalışma saatleri dışında dış iletişim yapılmaz.', 423)
    return cfg


# ---------------------------------------------------------------- data pool

COMPANY_FIELDS = ('legal_name', 'sector', 'city', 'district', 'website', 'phone', 'phone_source_url', 'email',
                  'email_source_url', 'contact_department', 'contact_title', 'linkedin_url', 'employee_count',
                  'employee_source_url', 'open_jobs', 'job_source_url', 'job_seen_on', 'hiring_need', 'product_fit',
                  'suggested_package', 'source_url', 'verified_on', 'tax_id')


def clean_company(data):
    out = {key: s(data, key, 2000 if key in ('open_jobs', 'hiring_need', 'product_fit') else 300) for key in COMPANY_FIELDS}
    out['legal_name'] = s(data, 'legal_name', 300, required=True)
    for key in ('website', 'phone_source_url', 'email_source_url', 'linkedin_url', 'employee_source_url',
                'job_source_url', 'source_url'):
        out[key] = https_url(out[key], key)
    if out['linkedin_url'] and domain_of(out['linkedin_url']) not in ('linkedin.com', 'tr.linkedin.com'):
        fail('linkedin_url yalnızca LinkedIn şirket sayfası olabilir.', 422)
    if out['linkedin_url'] and '/in/' in out['linkedin_url']:
        fail('Kişisel LinkedIn profilleri kaydedilmez; yalnızca şirket sayfası.', 422)
    out['domain'] = domain_of(out['website']) if out['website'] else (domain_of(out['email']) if out['email'] else None)
    if out['website'] and not out['domain']:
        fail('Web sitesinden alan adı çıkarılamadı.', 422)
    out['email'] = corporate_email(out['email'], out['domain'])
    if out['email'] and not out['email_source_url']:
        fail('Kurumsal e-posta için yayımlandığı sayfanın adresi (email_source_url) gerekli.', 422)
    out['phone'] = corporate_phone(out['phone'])
    if out['phone'] and not out['phone_source_url']:
        fail('Kurumsal telefon için yayımlandığı sayfanın adresi (phone_source_url) gerekli.', 422)
    if out['tax_id'] and not re.fullmatch(r'[0-9]{10,11}', out['tax_id']):
        fail('Vergi numarası 10 veya 11 rakam olmalı.', 422)
    out['tax_id'] = out['tax_id'] or None
    for key in ('verified_on', 'job_seen_on'):
        if out[key] and not re.fullmatch(r'20[0-9]{2}-[01][0-9]-[0-3][0-9]', out[key]):
            fail(f'{key}: YYYY-AA-GG biçiminde olmalı.', 422)
    if out['contact_title'] and re.search(r'\b[A-ZÇĞİÖŞÜ][a-zçğıöşü]+ [A-ZÇĞİÖŞÜ][a-zçğıöşü]+\b', out['contact_title']):
        # Only the published professional title is stored ("İK Müdürü"), never a person's name.
        if not re.search(r'(müdür|yönetici|uzman|direktör|sorumlu|şef|başkan|manager|director|lead|head)', out['contact_title'], re.I):
            fail('contact_title yalnızca mesleki unvan içermeli; kişi adı kaydedilmez.', 422)
    return out


def find_duplicate(db, domain, tax_id, key, city):
    if domain:
        row = db.execute('SELECT id FROM nova_companies WHERE domain=?', (domain,)).fetchone()
        if row:
            return row['id'], 'alan adı'
    if tax_id:
        row = db.execute('SELECT id FROM nova_companies WHERE tax_id=?', (tax_id,)).fetchone()
        if row:
            return row['id'], 'vergi numarası'
    row = db.execute('SELECT id FROM nova_companies WHERE name_key=? AND city=?', (key, city)).fetchone()
    if row:
        return row['id'], 'unvan ve il'
    return None, ''


def evidence_for(c, data):
    terms = data.get('workload_terms')
    if isinstance(terms, str):
        terms = [t.strip() for t in terms.split(';') if t.strip()]
    return {'active_job': bool(c['job_source_url'] and c['open_jobs']), 'job_source_url': c['job_source_url'],
            'job_seen_on': c['job_seen_on'], 'workload_terms': terms or [],
            'other_open_jobs': data.get('other_open_jobs') or 0,
            'corporate_email': bool(c['email']), 'corporate_phone': bool(c['phone'])}


def verified(c):
    return bool(c['source_url'] and c['verified_on'] and ((c['email'] and c['email_source_url'])
                                                           or (c['phone'] and c['phone_source_url'])))


def add_company(db, actor, data, synthetic=False):
    """Insert one data-pool company. Duplicates are never inserted twice."""
    c = clean_company(data)
    key = name_key(c['legal_name'])
    if not key:
        fail('Ticari unvan geçersiz.', 422)
    existing, matched = find_duplicate(db, c['domain'], c['tax_id'], key, c['city'])
    if existing:
        return {'duplicate': existing, 'matched_on': matched}
    total, detail = score(evidence_for(c, data))
    stage = 'verified' if verified(c) else 'researching'
    if not c['suggested_package']:
        c['suggested_package'] = suggest_package(c['employee_count'])
    target, now = uid(), stamp()
    columns = ('id', 'name_key', 'domain', 'score', 'score_detail', 'stage', 'consent_status', 'synthetic',
               'created_at', 'updated_at') + tuple(k for k in COMPANY_FIELDS)
    values = (target, key, c['domain'], total, json.dumps(detail, ensure_ascii=False), stage,
              'b2b_corporate' if c['email'] or c['phone'] else 'b2b_unverified', 1 if synthetic else 0, now, now) + \
             tuple(c[k] for k in COMPANY_FIELDS)
    db.execute(f'INSERT INTO nova_companies({",".join(columns)}) VALUES({",".join("?" * len(columns))})', values)
    event(db, target, 'created', actor.id, f'Kaynak: {c["source_url"] or "—"}', '', stage)
    suppression = suppressed(db, company(db, target))
    if suppression:
        db.execute("UPDATE nova_companies SET stage='do_not_contact',consent_status='objected' WHERE id=?", (target,))
        event(db, target, 'stage', 'system', f'Ret listesinde kayıtlı: {suppression}', stage, 'do_not_contact')
    return {'id': target, 'stage': stage if not suppression else 'do_not_contact', 'score': total}


def import_companies(db, actor, rows, synthetic=False):
    founder(actor)
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        fail('İçe aktarma 1–1000 kayıt içermeli.', 422)
    result = {'created': [], 'duplicates': [], 'rejected': []}
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            result['rejected'].append({'row': index, 'reason': 'Kayıt nesne değil.'})
            continue
        db.execute('SAVEPOINT nova_import_row')
        try:
            outcome = add_company(db, actor, row, synthetic)
        except Invalid as exc:
            db.execute('ROLLBACK TO SAVEPOINT nova_import_row')
            db.execute('RELEASE SAVEPOINT nova_import_row')
            result['rejected'].append({'row': index, 'name': str(row.get('legal_name', ''))[:120], 'reason': str(exc)})
            continue
        db.execute('RELEASE SAVEPOINT nova_import_row')
        if 'duplicate' in outcome:
            result['duplicates'].append({'row': index, 'id': outcome['duplicate'], 'matched_on': outcome['matched_on']})
        else:
            result['created'].append(outcome['id'])
    event(db, None, 'import', actor.id, f'{len(result["created"])} yeni, {len(result["duplicates"])} mükerrer, '
                                         f'{len(result["rejected"])} reddedildi')
    return result


def company(db, target):
    row = db.execute('SELECT * FROM nova_companies WHERE id=?', (target,)).fetchone()
    if row is None:
        fail('Firma kaydı bulunamadı.', 404)
    return dict(row)


def transition(db, actor_id, target, to, detail=''):
    c = company(db, target)
    current = c['stage']
    if to not in STAGES:
        fail('Geçersiz satış aşaması.', 422)
    if current == to:
        return c
    if current in TERMINAL:
        fail('İletişim istemeyen firma başka aşamaya taşınamaz.', 409)
    if to != 'do_not_contact' and to not in FLOW[current]:
        fail(f'{STAGES[current]} → {STAGES[to]} geçişine izin verilmiyor.', 409)
    if to == 'verified' and not verified(c):
        fail('Doğrulandı aşaması için kaynak adresi, doğrulama tarihi ve kaynaklı kurumsal kanal gerekli.', 409)
    db.execute('UPDATE nova_companies SET stage=?,updated_at=? WHERE id=?', (to, stamp(), target))
    event(db, target, 'stage', actor_id, detail, current, to)
    c['stage'] = to
    return c


# ---------------------------------------------------------------- objection list

def suppress(db, actor_id, kind, value, reason, source, company_id=None):
    value = (value or '').strip().lower()
    if kind == 'phone':
        value = phone_e164(value)
    if kind == 'domain':
        value = domain_of(value) or ''
    if kind not in ('email', 'domain', 'phone', 'company') or not value:
        fail('Ret kaydı için tür ve değer gerekli.', 422)
    db.execute('INSERT INTO nova_suppressions VALUES(?,?,?,?,?,?) ON CONFLICT(kind,value) DO NOTHING',
               (uid(), kind, value, reason[:500], source[:200], stamp()))
    affected = []
    if company_id:
        affected.append(company_id)
    if kind == 'domain':
        affected += [r['id'] for r in db.execute('SELECT id FROM nova_companies WHERE domain=?', (value,)).fetchall()]
    if kind == 'email':
        affected += [r['id'] for r in db.execute('SELECT id FROM nova_companies WHERE email=?', (value,)).fetchall()]
    if kind == 'phone':
        affected += [r['id'] for r in db.execute('SELECT id FROM nova_companies WHERE phone=?', (value,)).fetchall()]
    for target in dict.fromkeys(affected):
        c = company(db, target)
        if c['stage'] != 'do_not_contact':
            db.execute("UPDATE nova_companies SET stage='do_not_contact',consent_status='objected',updated_at=? WHERE id=?",
                       (stamp(), target))
            event(db, target, 'stage', actor_id, f'İletişim reddi ({kind}): {reason}', c['stage'], 'do_not_contact')
        db.execute("UPDATE nova_emails SET status='cancelled',error=? WHERE company_id=? AND direction='out' AND status IN ('draft','approved')",
                   ('İletişim reddi', target))
        db.execute("UPDATE nova_calls SET status='cancelled',outcome=? WHERE company_id=? AND status='planned'",
                   ('İletişim reddi', target))
    return {'kind': kind, 'value': value, 'companies': len(set(affected))}


def suppressed(db, c):
    checks = [('company', c['id'])]
    if c.get('domain'):
        checks.append(('domain', c['domain']))
    if c.get('email'):
        checks.append(('email', c['email']))
    if c.get('phone'):
        checks.append(('phone', c['phone']))
    for kind, value in checks:
        row = db.execute('SELECT reason FROM nova_suppressions WHERE kind=? AND value=?', (kind, value)).fetchone()
        if row:
            return row['reason']
    return None


def contactable(db, c, channel):
    if c['stage'] in TERMINAL or c['consent_status'] == 'objected':
        fail('Firma iletişim istemiyor.', 409)
    reason = suppressed(db, c)
    if reason:
        fail(f'Ret listesinde: {reason}', 409)
    if c['stage'] in ('rejected', 'won'):
        fail('Bu aşamadaki firmaya tanıtım iletişimi yapılmaz.', 409)
    if channel == 'email' and (not c['email'] or c['email_invalid']):
        fail('Doğrulanmış ve geçerli kurumsal e-posta yok.', 409)
    if channel == 'call' and not c['phone']:
        fail('Doğrulanmış kurumsal telefon yok.', 409)
    if c['lock_holder'] and c['lock_until'] > time.time():
        fail('Firma şu anda başka bir temsilci/işlem tarafından kilitli.', 409)


def iys_gate(db, c):
    """6563 / Yönetmelik m.6: before a commercial message to a tacir/esnaf the address is registered in
    İYS and checked for a prior objection. Enforced for every real (non-test) e-mail or call."""
    if settings(db)['require_iys'] and not c['iys_checked_on']:
        fail('İYS ret kontrolü yapılmadan gerçek firmaya ticari ileti gönderilmez / arama yapılmaz.', 409)


def mark_iys(db, actor, data):
    founder(actor)
    target = s(data, 'id', 64, True)
    result = s(data, 'result', 20, True)
    c = company(db, target)
    if result == 'objected':
        return suppress(db, actor.id, 'company', target, 'İYS üzerinde ret kaydı', 'iys', target)
    if result != 'clear':
        fail('İYS sonucu clear veya objected olmalı.', 422)
    checked = s(data, 'checked_on', 10) or today()
    if not re.fullmatch(r'20[0-9]{2}-[01][0-9]-[0-3][0-9]', checked):
        fail('checked_on: YYYY-AA-GG', 422)
    db.execute('UPDATE nova_companies SET iys_checked_on=?,updated_at=? WHERE id=?', (checked, stamp(), target))
    event(db, target, 'iys', actor.id, f'İYS ret kontrolü temiz ({checked})')
    return {'id': target, 'iys_checked_on': checked}


def lock(db, target, holder, seconds=900):
    """Atomic claim so two representatives (or NOVA and a representative) never work a company at once."""
    now = int(time.time())
    changed = db.execute('UPDATE nova_companies SET lock_holder=?,lock_until=? WHERE id=? AND (lock_holder=? OR lock_until<=?)',
                         (holder, now + seconds, target, holder, now)).rowcount
    if not changed:
        fail('Firma şu anda başka bir temsilci/işlem tarafından kilitli.', 409)


def unlock(db, target, holder):
    db.execute("UPDATE nova_companies SET lock_holder='',lock_until=0 WHERE id=? AND lock_holder=?", (target, holder))


# ---------------------------------------------------------------- e-mail

def test_addresses():
    raw = os.getenv('ERNASA_NOVA_TEST_EMAILS', '')
    return {a.strip().lower() for a in raw.split(',') if EMAIL.fullmatch(a.strip().lower())}


def compose_body(body):
    body = body.strip()
    if 'LİSTEDEN ÇIKAR' not in body:
        body += '\n\n' + OPT_OUT_LINE
    if 'iletisim@ernasa.com' not in body:
        body += '\n\n' + SENDER_BLOCK
    return body


FORBIDDEN_CLAIMS = (r'\biso\s?\d{4,5}\b.*(sertifika|belgeli|sahibiyiz)', r'%\s?\d+\s*indirim', r'indirim (yapabiliriz|veriyoruz)',
                    r'garanti(li|liyoruz|ediyoruz)', r'personel(inizin)? yerini al', r'işten çıkar', r'kadro(nuzu)? azalt')


def claim_check(text):
    lowered = text.lower()
    for pattern in FORBIDDEN_CLAIMS:
        if re.search(pattern, lowered):
            fail('Taslak onaysız taahhüt veya iddia içeriyor (indirim, garanti, sertifika, personel azaltma).', 422)


def draft_email(db, actor, data):
    target = s(data, 'company_id', 64, required=True)
    subject = s(data, 'subject', 200, required=True)
    body = s(data, 'body', 6000, required=True)
    campaign = s(data, 'campaign', 40) or 'intro'
    if any(ch in subject for ch in '\r\n'):
        fail('Konu satırı tek satır olmalı.', 422)
    c = company(db, target)
    contactable(db, c, 'email')
    claim_check(subject + '\n' + body)
    body = compose_body(body)
    key = f'{target}:{campaign}'
    if db.execute('SELECT 1 FROM nova_emails WHERE dedupe_key=?', (key,)).fetchone():
        fail('Bu firmaya aynı tanıtım iletisi zaten hazırlanmış veya gönderilmiş.', 409)
    if campaign == 'intro' and db.execute(
            "SELECT 1 FROM nova_emails e JOIN nova_companies c ON c.id=e.company_id WHERE c.domain=? AND e.campaign='intro' "
            "AND e.direction='out' AND e.status NOT IN ('cancelled','failed')", (c['domain'],)).fetchone():
        fail('Aynı alan adına tanıtım iletisi zaten var.', 409)
    email_id = uid()
    db.execute('INSERT INTO nova_emails(id,company_id,direction,address,subject,body,status,campaign,dedupe_key,created_at) '
               'VALUES(?,?,?,?,?,?,?,?,?,?)', (email_id, target, 'out', c['email'], subject, body, 'draft', campaign, key, stamp()))
    event(db, target, 'email_draft', actor.id, subject)
    if c['stage'] in ('verified',):
        transition(db, actor.id, target, 'contactable', 'Kurumsal e-posta doğrulandı')
        c = company(db, target)
    if c['stage'] == 'contactable':
        transition(db, actor.id, target, 'email_draft', subject)
    return {'id': email_id, 'to': c['email']}


def approve_email(db, actor, data):
    founder(actor)
    email_id = s(data, 'id', 64, required=True)
    row = db.execute("SELECT * FROM nova_emails WHERE id=? AND direction='out'", (email_id,)).fetchone()
    if not row or row['status'] != 'draft':
        fail('Onaylanacak taslak bulunamadı.', 404)
    contactable(db, company(db, row['company_id']), 'email')
    db.execute("UPDATE nova_emails SET status='approved',approved_by=? WHERE id=?", (actor.id, email_id))
    event(db, row['company_id'], 'email_approved', actor.id, row['subject'])
    return {'id': email_id, 'status': 'approved'}


def reject_email(db, actor, data):
    founder(actor)
    email_id = s(data, 'id', 64, required=True)
    row = db.execute("SELECT * FROM nova_emails WHERE id=? AND direction='out' AND status IN ('draft','approved')", (email_id,)).fetchone()
    if not row:
        fail('Taslak bulunamadı.', 404)
    db.execute("UPDATE nova_emails SET status='cancelled',error=?,dedupe_key=NULL WHERE id=?",
               (s(data, 'reason', 300) or 'Yönetim reddetti', email_id))
    event(db, row['company_id'], 'email_cancelled', actor.id, row['subject'])
    return {'id': email_id, 'status': 'cancelled'}


def day_bounds(day):
    """UTC ISO range of one Istanbul calendar day (stored stamps are UTC ISO strings)."""
    start = datetime.fromisoformat(day).replace(tzinfo=ISTANBUL)
    return start.astimezone(timezone.utc).isoformat(), (start + timedelta(days=1)).astimezone(timezone.utc).isoformat()


def utc_stamp(moment=None):
    return (moment or now_local()).astimezone(timezone.utc).isoformat()


def sent_today(db, day):
    low, high = day_bounds(day)
    return db.execute("SELECT count(*) FROM nova_emails WHERE direction='out' AND status IN ('sent','delivered','bounced') "
                      "AND sent_at>=? AND sent_at<? AND campaign<>'reply'", (low, high)).fetchone()[0]


def dispatch_emails(db, transport, moment=None, limit=None):
    """Send approved e-mails within every gate. Returns the per-message outcome.

    `transport(recipient, subject, text) -> provider message id` (see nova_providers.EmailTransport)."""
    moment = moment or now_local()
    day = today(moment)
    cfg_now = settings(db)
    # Automatic mode (only possible after the founder approved the written e-mail policy) also sends
    # NOVA drafts without a per-message approval - still inside every gate below.
    statuses = ('approved', 'draft') if cfg_now['auto_email'] and cfg_now['email_policy_approved'] else ('approved',)
    rows = db.execute("SELECT e.*, c.synthetic FROM nova_emails e JOIN nova_companies c ON c.id=e.company_id "
                      f"WHERE e.direction='out' AND e.status IN ({','.join('?' * len(statuses))}) ORDER BY e.created_at",
                      statuses).fetchall()
    outcome = []
    tests = test_addresses()
    for row in rows:
        is_test = bool(row['synthetic']) and row['address'] in tests
        try:
            cfg = outbound_gate(db, 'email', moment, test=is_test)
            if not is_test and sent_today(db, day) >= cfg['daily_email_limit']:
                fail('Günlük e-posta limiti doldu.', 429)
            if limit is not None and len([o for o in outcome if o['status'] == 'sent']) >= limit:
                break
            c = company(db, row['company_id'])
            contactable(db, c, 'email')
            if not is_test:
                iys_gate(db, c)
        except Invalid as exc:
            outcome.append({'id': row['id'], 'status': 'held', 'reason': str(exc)})
            if exc.status in (423, 429):
                break
            continue
        try:
            provider, provider_id = transport(row['address'], row['subject'], row['body'], 'nova-' + row['id'])
        except Exception as exc:  # provider error text is already sanitised by the transport
            db.execute("UPDATE nova_emails SET status='failed',error=?,dedupe_key=NULL WHERE id=?", (str(exc)[:300], row['id']))
            event(db, row['company_id'], 'email_failed', 'nova', str(exc)[:300])
            outcome.append({'id': row['id'], 'status': 'failed', 'reason': str(exc)[:300]})
            continue
        db.execute("UPDATE nova_emails SET status='sent',provider=?,provider_id=?,sent_at=?,"
                   "approved_by=CASE WHEN approved_by='' THEN 'nova-auto' ELSE approved_by END WHERE id=?",
                   (provider, str(provider_id)[:200], utc_stamp(moment), row['id']))
        event(db, row['company_id'], 'email_sent', 'nova', row['subject'])
        c = company(db, row['company_id'])
        if c['stage'] == 'email_draft':
            transition(db, 'nova', row['company_id'], 'email_sent', row['subject'])
            transition(db, 'nova', row['company_id'], 'awaiting_reply')
        outcome.append({'id': row['id'], 'status': 'sent'})
    return outcome


# ---------------------------------------------------------------- inbound replies

CLASSES = {
    'bounce': ('mailer-daemon', 'delivery status notification', 'undeliverable', 'teslim edilemedi', 'delivery has failed',
               'address not found', 'user unknown', 'mail delivery failed', 'returned mail'),
    'objection': ('listeden çıkar', 'listeden cikar', 'unsubscribe', 'istemiyoruz', 'göndermeyin', 'gondermeyin',
                  'rahatsız etmeyin', 'rahatsiz etmeyin', 'aramayın', 'aramayin', 'iletişim istemiyoruz',
                  'bir daha yazmayın', 'remove me', 'kaldırın', 'çıkarın', 'ilgilenmiyoruz', 'ilgilenmiyorum'),
    'auto_reply': ('otomatik yanıt', 'otomatik cevap', 'out of office', 'izindeyim', 'automatic reply', 'auto-reply',
                   'ofis dışında', 'tatildeyim'),
    'demo': ('demo', 'toplantı ayarlayalım', 'görüşme ayarlayalım', 'görüşelim', 'sunum', 'tanıtım toplantısı',
             'online görüşme', 'randevu'),
    'interested': ('ilgileniyoruz', 'ilgileniyorum', 'bilgi alabilir', 'detaylı bilgi', 'fiyat', 'teklif', 'arayabilirsiniz',
                   'dönüş yapın', 'iletişime geçin', 'merak ediyoruz'),
}
CALL_CONSENT = ('arayabilirsiniz', 'telefonla görüşelim', 'bizi arayın', 'telefon ile görüşmek', 'aranmak isteriz')


def classify(subject, body, sender=''):
    text = f'{sender}\n{subject}\n{body}'.lower()
    quoted = re.split(r'\n(?:>|on .* wrote:|.* tarihinde .* yazdı:)', text, maxsplit=1)[0]
    if any(k in text[:600] for k in CLASSES['bounce']) or sender.lower().startswith(('mailer-daemon', 'postmaster')):
        return 'bounce'
    for label in ('objection', 'auto_reply', 'demo', 'interested'):
        # Prefix match: Turkish suffixes ("listeden çıkarın", "aramayınız") must still match.
        if any(re.search(r'(?<!\w)' + re.escape(k), quoted) for k in CLASSES[label]):
            if label == 'objection' and re.search(r'ilgilenmiyoruz.*(ama|fakat|ancak)', quoted):
                continue
            return label
    if '?' in quoted:
        return 'question'
    return 'other'


def record_inbound(db, message):
    """Store one inbound mail (from the read-only mailbox reader) and apply its consequences."""
    provider_id = str(message.get('provider_id') or '')[:200]
    sender = str(message.get('from') or '').strip().lower()[:254]
    subject = str(message.get('subject') or '')[:300]
    body = str(message.get('body') or '')[:20000]
    if not provider_id or not sender:
        fail('Gelen ileti kimliği ve gönderen gerekli.', 422)
    if db.execute("SELECT 1 FROM nova_emails WHERE direction='in' AND provider_id=?", (provider_id,)).fetchone():
        return {'status': 'duplicate'}
    label = classify(subject, body, sender)
    original = str(message.get('original_recipient') or '').lower()
    host = domain_of(original if label == 'bounce' and original else sender)
    c = None
    if label == 'bounce' and original:
        row = db.execute('SELECT * FROM nova_companies WHERE email=?', (original,)).fetchone()
        c = dict(row) if row else None
    if c is None and host:
        row = db.execute('SELECT * FROM nova_companies WHERE email=? OR domain=? ORDER BY CASE WHEN email=? THEN 0 ELSE 1 END LIMIT 1',
                         (sender, host, sender)).fetchone()
        c = dict(row) if row else None
    if c is None:
        return {'status': 'unmatched', 'classification': label}
    db.execute("INSERT INTO nova_emails(id,company_id,direction,address,subject,body,status,classification,campaign,provider_id,created_at) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (uid(), c['id'], 'in', sender, subject, body, 'received', label, 'reply', provider_id, stamp()))
    event(db, c['id'], 'email_received', 'nova', f'{label}: {subject[:120]}')
    if label == 'bounce':
        db.execute("UPDATE nova_companies SET email_invalid=1,updated_at=? WHERE id=?", (stamp(), c['id']))
        db.execute("UPDATE nova_emails SET status='bounced' WHERE company_id=? AND direction='out' AND status='sent'", (c['id'],))
        event(db, c['id'], 'email_bounced', 'nova', 'Adres teslim edilemedi; geçersiz işaretlendi')
    elif label == 'objection':
        suppress(db, 'nova', 'domain', c['domain'] or host, 'Yazılı ret/iletişimden çıkma talebi', 'email:' + provider_id, c['id'])
    elif c['stage'] not in TERMINAL:
        if any(k in body.lower() for k in CALL_CONSENT):
            db.execute('UPDATE nova_companies SET call_permission=?,updated_at=? WHERE id=?',
                       (f'E-posta {provider_id} ({stamp()[:10]})', stamp(), c['id']))
            event(db, c['id'], 'call_permission', 'nova', 'Firma telefonla görüşmeyi yazılı olarak kabul etti')
        if label == 'demo':
            create_demo(db, c['id'], 'email', subject[:300])
        elif label in ('interested', 'question') and c['stage'] in ('email_sent', 'awaiting_reply', 'call_done'):
            transition(db, 'nova', c['id'], 'interested', 'E-posta yanıtı')
    return {'status': 'stored', 'classification': label, 'company_id': c['id']}


def create_demo(db, target, source, note=''):
    c = company(db, target)
    if db.execute("SELECT 1 FROM nova_demo_requests WHERE company_id=? AND status IN ('new','assigned','scheduled')", (target,)).fetchone():
        return None
    demo = uid()
    db.execute('INSERT INTO nova_demo_requests VALUES(?,?,?,?,?,?)', (demo, target, source, note[:1000], 'new', stamp()))
    event(db, target, 'demo_requested', 'nova', note[:300])
    if 'demo_requested' in FLOW.get(c['stage'], set()):
        transition(db, 'nova', target, 'demo_requested', f'Kaynak: {source}')
    return demo


# ---------------------------------------------------------------- spending

def spent(db, day=None, month=None):
    # PostgreSQL returns sum(bigint) as Decimal; money stays integer kuruş everywhere.
    if day:
        return int(db.execute('SELECT COALESCE(sum(amount_minor),0) FROM nova_spend WHERE day=?', (day,)).fetchone()[0])
    return int(db.execute('SELECT COALESCE(sum(amount_minor),0) FROM nova_spend WHERE substr(day,1,7)=?', (month,)).fetchone()[0])


def reserve(db, cfg, amount, reference, moment=None):
    day = today(moment)
    if spent(db, day=day) + amount > cfg['daily_spend_limit_minor']:
        fail('Günlük harcama limiti aşılacağı için işlem durduruldu.', 429)
    if spent(db, month=day[:7]) + amount > cfg['monthly_spend_limit_minor']:
        fail('Aylık harcama limiti aşılacağı için işlem durduruldu.', 429)
    db.execute('INSERT INTO nova_spend VALUES(?,?,?,?,?,?)', (uid(), day, 'reservation', amount, reference, stamp()))


# ---------------------------------------------------------------- phone calls

def authorize_test_number(db, actor, data):
    founder(actor)
    number = phone_e164(s(data, 'phone', 40, required=True))
    label = s(data, 'label', 120) or 'Kurucu test hattı'
    if s(data, 'confirm', 10) != 'EVET':
        fail('Test numarasının size ait olduğunu ve aranmasına izin verdiğinizi "EVET" ile onaylayın.', 422)
    db.execute('INSERT INTO nova_test_numbers VALUES(?,?,?,?) ON CONFLICT(phone) DO NOTHING', (number, label, actor.id, stamp()))
    event(db, None, 'test_number', actor.id, f'{label} ({number[:6]}…{number[-2:]}) yetkilendirildi')
    return {'phone': number}


def plan_call(db, actor, data, moment=None):
    purpose = s(data, 'purpose', 20) or 'prospect'
    if purpose not in ('founder_test', 'prospect'):
        fail('Geçersiz arama türü.', 422)
    cfg = settings(db)
    if purpose == 'founder_test':
        founder(actor)
        number = phone_e164(s(data, 'phone', 40, required=True))
        if not db.execute('SELECT 1 FROM nova_test_numbers WHERE phone=?', (number,)).fetchone():
            fail('Bu numara kurucu tarafından test için yetkilendirilmemiş.', 403)
        target = None
    else:
        target = s(data, 'company_id', 64, required=True)
        c = company(db, target)
        contactable(db, c, 'call')
        iys_gate(db, c)
        if not c['call_permission'] and not cfg['call_policy_approved']:
            fail('Firmanın yazılı arama kabulü yok ve arama politikası onaylanmamış.', 409)
        if not actor.founder and not cfg['auto_call']:
            fail('Otomatik arama modu kapalı; arama yalnızca yönetim tarafından planlanabilir.', 403)
        number = c['phone']
        row = db.execute('SELECT reason FROM nova_suppressions WHERE kind=? AND value=?', ('phone', number)).fetchone()
        if row:
            fail(f'Numara ret listesinde: {row["reason"]}', 409)
        if db.execute("SELECT 1 FROM nova_calls WHERE company_id=? AND status IN ('planned','dialing')", (target,)).fetchone():
            fail('Bu firma için zaten planlı veya devam eden bir arama var.', 409)
    call = uid()
    db.execute('INSERT INTO nova_calls(id,company_id,phone,purpose,status,department,created_at) VALUES(?,?,?,?,?,?,?)',
               (call, target, number, purpose, 'planned', s(data, 'department', 120), stamp()))
    if target:
        event(db, target, 'call_planned', actor.id, purpose)
        c = company(db, target)
        if 'call_planned' in FLOW.get(c['stage'], set()):
            transition(db, actor.id, target, 'call_planned')
    return {'id': call, 'status': 'planned'}


def calls_today(db, day):
    low, high = day_bounds(day)
    return db.execute("SELECT count(*) FROM nova_calls WHERE purpose='prospect' AND status NOT IN ('planned','cancelled') "
                      "AND started_at>=? AND started_at<?", (low, high)).fetchone()[0]


def start_call(db, call_id, voice, moment=None):
    """Dial one planned call through the injected voice provider, inside every gate."""
    row = db.execute("SELECT * FROM nova_calls WHERE id=?", (call_id,)).fetchone()
    if not row or row['status'] != 'planned':
        fail('Başlatılacak planlı arama bulunamadı.', 404)
    test = row['purpose'] == 'founder_test'
    cfg = outbound_gate(db, 'call', moment, test=test)
    c = None
    if not test:
        c = company(db, row['company_id'])
        contactable(db, c, 'call')
        iys_gate(db, c)
        if calls_today(db, today(moment)) >= cfg['daily_call_limit']:
            fail('Günlük arama limiti doldu.', 429)
        lock(db, c['id'], 'nova-call:' + call_id, cfg['max_call_seconds'] + 120)
    elif not db.execute('SELECT 1 FROM nova_test_numbers WHERE phone=?', (row['phone'],)).fetchone():
        fail('Test numarası yetkisi kaldırılmış.', 403)
    estimate = -(-cfg['max_call_seconds'] * cfg['call_rate_minor_per_minute'] // 60)
    reserve(db, cfg, estimate, call_id, moment)
    context = agent_context(db, c, cfg)
    provider, provider_call_id = voice.start_call(row['phone'], context, cfg['max_call_seconds'], metadata={'nova_call_id': call_id})
    db.execute("UPDATE nova_calls SET status='dialing',provider=?,provider_call_id=?,started_at=? WHERE id=?",
               (provider, str(provider_call_id)[:200], utc_stamp(moment), call_id))
    if c:
        event(db, c['id'], 'call_started', 'nova', provider)
    return {'id': call_id, 'provider_call_id': provider_call_id}


CALL_STATUS = {'completed', 'failed', 'no_answer', 'declined_ai'}


def record_call_result(db, provider, provider_call_id, result):
    """Idempotent webhook handler: final call state, real cost, AI summary and consequences."""
    row = db.execute('SELECT * FROM nova_calls WHERE provider=? AND provider_call_id=?', (provider, str(provider_call_id))).fetchone()
    if not row:
        fail('Arama kaydı bulunamadı.', 404)
    if row['status'] in CALL_STATUS:
        return {'id': row['id'], 'status': row['status'], 'duplicate': True}
    status = result.get('status') if result.get('status') in CALL_STATUS else 'completed'
    try:
        duration = max(0, int(float(result.get('duration_seconds') or 0)))
    except (TypeError, ValueError):
        duration = 0
    cfg = settings(db)
    if result.get('cost_minor') is not None:
        cost, source = max(0, int(result['cost_minor'])), 'sağlayıcı raporu'
    else:
        cost, source = -(-duration * cfg['call_rate_minor_per_minute'] // 60), 'tahmin (dakika ücreti × süre)'
    # Full transcript only when the provider confirms the recording notice was given and accepted.
    recording_ok = result.get('recording_consent_confirmed') is True
    transcript = str(result.get('transcript') or '')[:60000] if recording_ok else None
    retention = (now_local() + timedelta(days=int(os.getenv('ERNASA_NOVA_TRANSCRIPT_DAYS', '90')))).date().isoformat() if transcript else ''
    fields = {k: str(result.get(k) or '')[:4000] for k in ('outcome', 'needs', 'questions', 'objections', 'interest', 'next_step', 'summary')}
    demo = 1 if result.get('demo_requested') else 0
    db.execute('UPDATE nova_calls SET status=?,ended_at=?,duration_seconds=?,cost_minor=?,cost_source=?,outcome=?,needs=?,questions=?,'
               'objections=?,interest=?,demo_requested=?,next_step=?,summary=?,recording_allowed=?,transcript=?,retention_until=? WHERE id=?',
               (status, stamp(), duration, cost, source, fields['outcome'], fields['needs'], fields['questions'], fields['objections'],
                fields['interest'], demo, fields['next_step'], fields['summary'], 1 if transcript else 0, transcript, retention, row['id']))
    db.execute('INSERT INTO nova_spend VALUES(?,?,?,?,?,?) ON CONFLICT(category,reference) DO NOTHING',
               (uid(), today(), 'call', cost, row['id'], stamp()))
    reservation = db.execute("SELECT amount_minor, day FROM nova_spend WHERE category='reservation' AND reference=?", (row['id'],)).fetchone()
    if reservation:
        db.execute('INSERT INTO nova_spend VALUES(?,?,?,?,?,?) ON CONFLICT(category,reference) DO NOTHING',
                   (uid(), reservation['day'], 'reservation', -reservation['amount_minor'], row['id'] + ':release', stamp()))
    target = row['company_id']
    if target:
        unlock(db, target, 'nova-call:' + row['id'])
        c = company(db, target)
        event(db, target, 'call_result', 'nova', f'{status} · {duration} sn · {fields["summary"][:300]}')
        if status == 'declined_ai' or result.get('do_not_call'):
            suppress(db, 'nova', 'phone', row['phone'], 'Telefonda iletişim/yapay zekâ görüşmesi reddi', 'call:' + row['id'], target)
        elif status == 'completed' and c['stage'] == 'call_planned':
            transition(db, 'nova', target, 'call_done', fields['summary'][:300])
            if demo:
                create_demo(db, target, 'call', fields['next_step'] or fields['summary'][:300])
            elif fields['interest'].lower() in ('yüksek', 'orta', 'high', 'medium', 'evet'):
                transition(db, 'nova', target, 'interested', 'Telefon görüşmesi')
            if result.get('handoff'):
                c = company(db, target)
                if 'handed_off' in FLOW.get(c['stage'], set()):
                    transition(db, 'nova', target, 'handed_off', 'İnsan temsilci talebi')
        elif status in ('no_answer', 'failed') and c['stage'] == 'call_planned':
            transition(db, 'nova', target, 'awaiting_reply' if c['email'] else 'contactable', status)
    return {'id': row['id'], 'status': status, 'cost_minor': cost}


# ---------------------------------------------------------------- product knowledge

def seed_knowledge(db):
    """Idempotently load approved prices from the CONTROL catalogue and safe defaults.

    Prices are the CONTROL-approved list (control/billing_catalog.py). Feature descriptions are
    loaded as 'draft' and must be approved by the founder before NOVA may state them."""
    from .billing_catalog import HR_FLOW
    entries = []
    for plan in HR_FLOW:
        entries.append(('hr_flow', 'fiyat', f'{plan.name} paketi ne kadar?',
                        f'{plan.name}: aylık {plan.monthly_usd} USD + KDV (ödeme günü TCMB kuruyla TL). Aylık {plan.capacity} aday '
                        f'işlem hakkı, {plan.positions} aktif pozisyon, {plan.hr_users} İK kullanıcısı.',
                        'approved', 'control/billing_catalog.py (CONTROL onaylı liste fiyatı)'))
    entries += [
        ('hr_flow', 'genel', 'ERNASA HR FLOW nedir?',
         'Başvuru toplama, CV değerlendirme (ATS), yapay zekâ destekli ön mülakat, mülakat planlama ve aday takibini tek panelde '
         'birleştiren işe alım yazılımı. Son karar her zaman şirketin İK yetkilisindedir: "Yapay zekâ analiz eder, insan karar verir."',
         'draft', 'kod: ats_*.py, interview_*.py, hr_flow_*.py (CONTROL onaylı ürün kapsamı)'),
        ('hr_flow', 'ödeme', 'Nasıl ödeme yapılır?', 'Satış temsilcimizin ilettiği kişiye özel güvenli ödeme bağlantısıyla: kredi/banka '
         'kartı (Shopier) veya, CONTROL\'de açık olduğunda, banka havalesi. Kart ve havalede aynı KDV dahil tutar uygulanır.',
         'draft', 'kod: control/billing_http.py, control/billing_payments.py'),
        ('hr_flow', 'veri güvenliği', 'Verilerimiz nerede tutuluyor?', 'Kurucu tarafından onaylanacak metin. Onaylanana kadar NOVA bu soruyu insan temsilciye aktarır.',
         'draft', 'politika bekliyor: veri güvenliği metni'),
        ('ernasa', 'sertifika', 'ISO sertifikanız var mı?', 'ERNASA\'nın şu anda alınmış bir ISO sertifikası yoktur; NOVA sertifika iddiasında bulunmaz.',
         'approved', 'NOVA 2.0 görev talimatı'),
        ('ernasa', 'indirim', 'İndirim yapabilir misiniz?', 'NOVA indirim, özel ödeme koşulu veya sözleşme değişikliği veremez; talebi yetkili satış temsilcisine iletir.',
         'approved', 'NOVA 2.0 görev talimatı'),
    ]
    from .billing_catalog import EDUCATION
    for plan in EDUCATION:
        entries.append(('egitim', 'fiyat', f'Eğitim Platformu {plan.name} paketi ne kadar?',
                        f'{plan.name}: aylık {plan.monthly_usd} USD + KDV (ödeme günü TCMB kuruyla TL), aylık {plan.capacity} eğitim katılımcısı.',
                        'approved', 'control/billing_catalog.py (CONTROL onaylı liste fiyatı)'))
    entries += [
        ('hr_flow', 'paket', 'Hangi paketi seçmeliyiz?',
         'Aylık aday hacmine göre: ~100 aday Starter, ~500 Growth, ~1.000 Business, ~2.500 PRO. Bir aday işlemi ATS değerlendirmesi ve '
         'gerekiyorsa AI ön mülakatı tek hak olarak kapsar. Kesin öneriyi satış temsilcimiz ihtiyaç görüşmesinden sonra yapar.',
         'draft', 'kod: control/billing_http.py limits(), control/billing_catalog.py'),
        ('hr_flow', 'özellik', 'ATS ve CV değerlendirme ne yapar?',
         'Başvuruları tek yerde toplar, CV dosyalarını (PDF/Word, taranmış belgeler dahil) okur ve pozisyon kriterlerine göre '
         'eşleşme değerlendirmesi üretir; sıralama İK ekibinin incelemesi içindir.',
         'draft', 'kod: ats_cv_import.py, ats_matching.py, ocr_service'),
        ('hr_flow', 'özellik', 'Yapay zekâ destekli ön mülakat nasıl çalışır?',
         'Adaya kişiye özel bağlantı gönderilir; aday şirketin belirlediği veya şablon sorulara çevrim içi yanıt verir; sistem yanıtları '
         'değerlendirip rapor hazırlar. Onay, ret veya ikinci görüşme kararı her zaman İK yetkilisindedir.',
         'draft', 'kod: interview_assessments.py, portal_interviews.py, hr_flow_decisions.py'),
        ('hr_flow', 'özellik', 'Mülakat organizasyonu ve aday takibi var mı?',
         'Mülakat randevusu planlama, adaya bildirim e-postaları, aday aşama takibi ve İK karar merkezi bulunur.',
         'draft', 'kod: hr_flow_booking.py, flow_candidate_notifications.py, hr_flow_automation.py'),
        ('hr_flow', 'özellik', 'Raporlama sunuyor musunuz?', 'Aday ve pozisyon bazında değerlendirme raporları ve PDF çıktısı sunulur.',
         'draft', 'kod: hr_flow_report.py, recruitment_pdf.py'),
        ('egitim', 'özellik', 'Eğitim modülleri nedir?',
         'Çalışanlara dijital eğitim ataması, sesli/video içerik ve ilerleme takibi sağlayan ERNASA Eğitim Platformu.',
         'draft', 'kod: academy_*.py, ernasa_training.py'),
        ('hr_flow', 'destek', 'Teknik destek nasıl alınır?', 'iletisim@ernasa.com adresinden; yanıt süresi taahhüdü kurucu tarafından belirlenecek.',
         'draft', 'politika bekliyor: destek SLA'),
        ('hr_flow', 'deneme', 'Ücretsiz deneme var mı?', 'Kurucu tarafından belirlenecek. Onaylanana kadar NOVA bu soruyu satış temsilcisine aktarır.',
         'draft', 'politika bekliyor: ücretsiz deneme'),
        ('itiraz', 'mevcut sistem', 'Zaten bir ATS / Kariyer.net kullanıyoruz.',
         'HR FLOW mevcut ilan kanallarınızın yerine geçmek zorunda değil; başvuruları tek yerde toplayıp ön değerlendirme ve mülakat '
         'organizasyonundaki manuel işi azaltmayı hedefler. 15 dakikalık bir demoda kendi sürecinizle karşılaştırabiliriz.',
         'draft', 'onaylı politika: NOVA 2.0 talimatı (indirim/taahhüt yok, son karar İK) + control/billing_catalog.py'),
        ('itiraz', 'bütçe', 'Bütçemiz yok / pahalı.',
         'Paketler aylık aday hacmine göre başlar; en küçük paket aylık 100 aday içindir. İndirim veya özel koşulu NOVA veremez; '
         'talebinizi yetkili satış temsilcisine iletirim.', 'draft', 'onaylı politika: NOVA 2.0 talimatı (indirim/taahhüt yok, son karar İK) + control/billing_catalog.py'),
        ('itiraz', 'yapay zekâ güveni', 'Yapay zekâ adayları yanlış eler mi?',
         'Sistem karar vermez, değerlendirme ve rapor üretir; adayı onaylama veya eleme kararı her zaman İK ekibinizdedir: '
         '"Yapay zekâ analiz eder, insan karar verir."', 'draft', 'onaylı politika: NOVA 2.0 talimatı (indirim/taahhüt yok, son karar İK) + control/billing_catalog.py'),
        ('itiraz', 'zamanlama', 'Şu an ilgilenmiyoruz / sonra arayın.',
         'Anlıyorum. Uygun olduğunuz bir zamanı not edeyim mi, yoksa kısa bir tanıtım e-postası mı tercih edersiniz? İstemezseniz sizi listemizden çıkarırım.',
         'draft', 'onaylı politika: NOVA 2.0 talimatı (indirim/taahhüt yok, son karar İK) + control/billing_catalog.py'),
    ]
    for product, topic, question, answer, status, source in entries:
        db.execute('INSERT INTO nova_knowledge VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(product,topic,question) DO NOTHING',
                   (uid(), product, topic, question, answer, status, source, 'system' if status == 'approved' else '', stamp()))


def knowledge_command(db, actor, data):
    founder(actor)
    action = s(data, 'knowledge_action', 20) or 'upsert'
    if action == 'approve_verified':
        # One management decision: approve every draft whose source is the code or an approved commercial
        # policy. Drafts that still wait for a policy ('politika bekliyor') are left untouched.
        rows = db.execute("SELECT id FROM nova_knowledge WHERE status='draft' AND (source LIKE 'kod:%' OR source LIKE 'onaylı politika:%')").fetchall()
        for row in rows:
            db.execute('UPDATE nova_knowledge SET status=?,approved_by=?,updated_at=? WHERE id=?', ('approved', actor.id, stamp(), row['id']))
        event(db, None, 'knowledge', actor.id, f'Toplu yönetim onayı: {len(rows)} kayıt (kod/onaylı politika kaynaklı)')
        return {'approved': len(rows)}
    if action in ('approve', 'not_live', 'draft'):
        target = s(data, 'id', 64, required=True)
        status = {'approve': 'approved', 'not_live': 'not_live', 'draft': 'draft'}[action]
        if not db.execute('UPDATE nova_knowledge SET status=?,approved_by=?,updated_at=? WHERE id=?',
                          (status, actor.id if status == 'approved' else '', stamp(), target)).rowcount:
            fail('Bilgi kaydı bulunamadı.', 404)
        event(db, None, 'knowledge', actor.id, f'{target} → {status}')
        return {'id': target, 'status': status}
    product, topic, question = s(data, 'product', 40, True), s(data, 'topic', 80, True), s(data, 'question', 300, True)
    answer, source = s(data, 'answer', 3000, True), s(data, 'source', 300, True)
    claim_check(answer)
    row = db.execute('SELECT id FROM nova_knowledge WHERE product=? AND topic=? AND question=?', (product, topic, question)).fetchone()
    if row:
        db.execute("UPDATE nova_knowledge SET answer=?,source=?,status='draft',approved_by='',updated_at=? WHERE id=?",
                   (answer, source, stamp(), row['id']))
        target = row['id']
    else:
        target = uid()
        db.execute('INSERT INTO nova_knowledge VALUES(?,?,?,?,?,?,?,?,?)',
                   (target, product, topic, question, answer, 'draft', source, '', stamp()))
    event(db, None, 'knowledge', actor.id, f'{product}/{topic}: taslak')
    return {'id': target, 'status': 'draft'}


def approved_knowledge(db):
    return [dict(r) for r in db.execute("SELECT product,topic,question,answer FROM nova_knowledge WHERE status='approved' "
                                        "ORDER BY product,topic,question").fetchall()]


def agent_context(db, c, cfg):
    """System instructions handed to the voice provider for one call."""
    seed_knowledge(db)
    facts = '\n'.join(f'- [{k["topic"]}] {k["question"]} → {k["answer"]}' for k in approved_knowledge(db))
    target = ('Kurucunun yetkilendirdiği test hattı. Karşıdaki kişiye ERNASA HR FLOW\'u gerçek bir müşteriymiş gibi tanıt.'
              if c is None else f'Firma: {c["legal_name"]} · Sektör: {c["sector"] or "—"} · İl: {c["city"] or "—"} · '
                                f'Görülen işe alım ihtiyacı: {c["hiring_need"] or "bilinmiyor"} · Önerilen paket: {c["suggested_package"] or "—"}')
    return {
        'first_message': AI_INTRO,
        'language': 'tr-TR',
        'system': (
            "Sen NOVA'sın: ERNASA'nın yapay zekâ destekli satış ve müşteri ilişkileri asistanı. Asla insan olduğunu söyleme; "
            "sorulursa ve görüşmenin başında yapay zekâ asistanı olduğunu açıkça belirt. Doğal, kısa, profesyonel ve baskısız "
            "Türkçe konuş; ezber metin okuma, karşı tarafı dinle ve sözün kesildiğinde hemen sus.\n"
            "Karşı taraf uygun değilse özür dileyip kısa sürede kapat ve uygun bir zaman sor. Yapay zekâyla görüşmek istemezse "
            "nazikçe teşekkür et, insan temsilcinin dönüş yapmasını isteyip istemediğini sor ve görüşmeyi bitir.\n"
            "Amaç: mevcut işe alım sürecini ve aday değerlendirmedeki sorunları anlamak, uygun HR FLOW özelliklerini anlatmak, "
            "ilgi varsa 15 dakikalık demo önermek.\n"
            "YASAK: indirim, özel ödeme koşulu, sözleşme değişikliği, hukuki taahhüt, garanti, sertifika iddiası, aşağıda olmayan "
            "fiyat veya özellik, ERNASA'nın iç operasyonu/mali durumu/personeli hakkında bilgi, personelin yerini alma veya işten "
            "çıkarma vaadi. Bilmediğin bir şey sorulursa 'Bunu yetkili satış temsilcimiz size net olarak iletsin' de ve not al.\n"
            f"İnsan temsilciye aktarma kuralları: {cfg['handoff_rules']}\n"
            "Karşı taraf 'beni bir daha aramayın' derse bunu kabul et, kaydedileceğini söyle ve kapat.\n"
            f"Görüşme en çok {cfg['max_call_seconds'] // 60} dakika sürer.\n\n"
            f"Görüşülen: {target}\n\nONAYLI BİLGİ TABANI (yalnızca bunları söyleyebilirsin):\n{facts or '- (onaylı bilgi yok: yalnızca tanışma ve demo talebi al)'}"
        ),
        'summary_schema': ['outcome', 'needs', 'questions', 'objections', 'interest', 'demo_requested', 'next_step', 'summary',
                           'do_not_call', 'handoff'],
    }


# ---------------------------------------------------------------- Sales Hub assignment

def assign(db, actor, data):
    """CONTROL founder assigns a NOVA company to an active Sales Hub representative.

    Creates (or links) the Sales Hub customer with the strong identity key (tax id or domain).
    An existing Sales Hub customer keeps its owner and history; commission eligibility stays with
    the existing approved Sales Hub rules - NOVA writes no commission data."""
    founder(actor)
    target, account = s(data, 'id', 64, True), s(data, 'account_id', 64, True)
    c = company(db, target)
    if c['stage'] in TERMINAL:
        fail('İletişim istemeyen firma temsilciye atanamaz.', 409)
    rep = db.execute('SELECT * FROM sales_accounts WHERE id=? AND active=1', (account,)).fetchone()
    if not rep:
        fail('Etkin satış temsilcisi bulunamadı.', 404)
    if c['owner_account_id'] and c['owner_account_id'] != account:
        fail('Firma zaten başka bir temsilciye atanmış; önce CONTROL\'de sahiplik kararı verin.', 409)
    key = f'tax:{c["tax_id"]}' if c['tax_id'] else (f'domain:{c["domain"]}' if c['domain'] else None)
    if not key:
        fail('Sales Hub aktarımı için vergi numarası veya alan adı gerekli.', 422)
    found = db.execute('SELECT c.* FROM sales_customers c JOIN sales_customer_identities i ON i.customer_id=c.id WHERE i.identity_key=?',
                       (key,)).fetchone()
    if found and found['owner_id'] != account:
        fail('Bu firma Sales Hub\'da başka bir temsilcinin müşterisi; geçmiş korunur ve otomatik devredilmez.', 409)
    if found:
        customer = found['id']
    else:
        customer = uid()
        db.execute('INSERT INTO sales_customers(id,company_id,owner_id,name,identity_key) VALUES(?,?,?,?,?)',
                   (customer, rep['company_id'], account, c['legal_name'][:160], key))
        db.execute('INSERT INTO sales_customer_identities VALUES(?,?)', (key, customer))
        if c['tax_id'] and c['domain']:
            extra = f'domain:{c["domain"]}'
            if not db.execute('SELECT 1 FROM sales_customer_identities WHERE identity_key=?', (extra,)).fetchone():
                db.execute('INSERT INTO sales_customer_identities VALUES(?,?)', (extra, customer))
        db.execute('INSERT INTO sales_audit VALUES(?,?,?,?,?,?,?)',
                   (uid(), rep['company_id'], actor.id, 'nova_customer_assigned', customer, f'NOVA {target}', stamp()))
    db.execute('UPDATE nova_companies SET owner_account_id=?,sales_customer_id=?,updated_at=? WHERE id=?',
               (account, customer, stamp(), target))
    event(db, target, 'assigned', actor.id, f'Temsilci {account} · Sales Hub müşteri {customer}')
    for demo in db.execute("SELECT id FROM nova_demo_requests WHERE company_id=? AND status='new'", (target,)).fetchall():
        db.execute("UPDATE nova_demo_requests SET status='assigned' WHERE id=?", (demo['id'],))
    if 'handed_off' in FLOW.get(c['stage'], set()):
        transition(db, actor.id, target, 'handed_off', f'Temsilci {account}')
    return {'id': target, 'sales_customer_id': customer}


# ---------------------------------------------------------------- job queue

def enqueue(db, kind, payload=None, dedupe_key=None, delay=0):
    job = uid()
    db.execute('INSERT INTO nova_jobs(id,kind,payload,run_after,dedupe_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?) '
               'ON CONFLICT(dedupe_key) DO NOTHING', (job, kind, json.dumps(payload or {}, ensure_ascii=False),
                                                       int(time.time()) + delay, dedupe_key, stamp(), stamp()))
    return job


def claim_job(db):
    now = int(time.time())
    row = db.execute("SELECT * FROM nova_jobs WHERE status='queued' AND run_after<=? ORDER BY run_after LIMIT 1", (now,)).fetchone()
    if not row:
        return None
    db.execute("UPDATE nova_jobs SET status='running',attempts=attempts+1,updated_at=? WHERE id=? AND status='queued'", (stamp(), row['id']))
    return dict(row)


def finish_job(db, job, error=None):
    if error is None:
        db.execute("UPDATE nova_jobs SET status='done',last_error='',updated_at=? WHERE id=?", (stamp(), job['id']))
        return
    attempts = job['attempts'] + 1
    if attempts >= job['max_attempts']:
        db.execute("UPDATE nova_jobs SET status='failed',last_error=?,updated_at=? WHERE id=?", (str(error)[:500], stamp(), job['id']))
    else:
        db.execute("UPDATE nova_jobs SET status='queued',last_error=?,run_after=?,updated_at=? WHERE id=?",
                   (str(error)[:500], int(time.time()) + 60 * 2 ** attempts, stamp(), job['id']))


# ---------------------------------------------------------------- reporting

def metrics(db, moment=None):
    day = today(moment)
    month = day[:7]
    one = lambda sql, *p: int(db.execute(sql, p).fetchone()[0] or 0)
    by_stage = {r['stage']: r['n'] for r in db.execute('SELECT stage, count(*) AS n FROM nova_companies WHERE synthetic=0 GROUP BY stage').fetchall()}
    sent = one("SELECT count(*) FROM nova_emails WHERE direction='out' AND status IN ('sent','delivered','bounced')")
    bounced = one("SELECT count(*) FROM nova_emails WHERE direction='out' AND status='bounced'")
    return {
        'companies': one('SELECT count(*) FROM nova_companies WHERE synthetic=0'),
        'new_today': one('SELECT count(*) FROM nova_companies WHERE synthetic=0 AND created_at>=? AND created_at<?', *day_bounds(day)),
        'verified': one("SELECT count(*) FROM nova_companies WHERE synthetic=0 AND verified_on<>'' AND source_url<>'' "
                        "AND ((email<>'' AND email_source_url<>'') OR (phone<>'' AND phone_source_url<>''))"),
        'avg_score': round(one('SELECT COALESCE(avg(score),0) FROM nova_companies WHERE synthetic=0') or 0),
        'by_stage': by_stage,
        'drafts': one("SELECT count(*) FROM nova_emails WHERE direction='out' AND status='draft'"),
        'approved': one("SELECT count(*) FROM nova_emails WHERE direction='out' AND status='approved'"),
        'sent': sent,
        'sent_today': sent_today(db, day),
        'replies': one("SELECT count(*) FROM nova_emails WHERE direction='in'"),
        'positive': one("SELECT count(*) FROM nova_emails WHERE direction='in' AND classification IN ('interested','demo')"),
        'negative': one("SELECT count(*) FROM nova_emails WHERE direction='in' AND classification='objection'"),
        'delivery_rate': (round(100 * (sent - bounced) / sent) if sent else None),
        'calls_planned': one("SELECT count(*) FROM nova_calls WHERE status='planned'"),
        'calls_done': one("SELECT count(*) FROM nova_calls WHERE status='completed'"),
        'call_seconds': one('SELECT COALESCE(sum(duration_seconds),0) FROM nova_calls'),
        'call_cost_minor': one('SELECT COALESCE(sum(cost_minor),0) FROM nova_calls'),
        'demos': one("SELECT count(*) FROM nova_demo_requests"),
        'opportunities': one("SELECT count(*) FROM nova_companies WHERE stage IN ('interested','demo_requested','handed_off','proposal')"),
        'assigned': one("SELECT count(*) FROM nova_companies WHERE owner_account_id IS NOT NULL"),
        'won': by_stage.get('won', 0),
        'suppressed': one('SELECT count(*) FROM nova_suppressions'),
        'spend_today_minor': spent(db, day=day),
        'spend_month_minor': spent(db, month=month),
    }


def daily_report(db, moment=None, store=True):
    moment = moment or now_local()
    day = today(moment)
    m = metrics(db, moment)
    cfg = settings(db)
    tl = lambda minor: f'{int(minor) // 100:,}'.replace(',', '.') + f',{int(minor) % 100:02d} TL'
    lines = [f'# NOVA günlük yönetim raporu — {day}', '',
             f'Durum: {"ÇALIŞIYOR" if cfg["running"] and not cfg["kill_switch"] else "DURDURULDU"} · '
             f'Acil durdurma: {"açık" if cfg["kill_switch"] else "kapalı"} · Otomatik e-posta: {"açık" if cfg["auto_email"] else "kapalı"} · '
             f'Otomatik arama: {"açık" if cfg["auto_call"] else "kapalı"}', '',
             '## Veri havuzu',
             f'- Toplam firma: {m["companies"]} (bugün yeni: {m["new_today"]}) · Doğrulanmış: {m["verified"]} / hedef 100',
             f'- Ortalama satış potansiyeli puanı: {m["avg_score"]}',
             '- Aşamalar: ' + (', '.join(f'{STAGES[k]} {v}' for k, v in sorted(m['by_stage'].items())) or '—'), '',
             '## E-posta',
             f'- Onay bekleyen taslak: {m["drafts"]} · Onaylı/gönderim sırasında: {m["approved"]}',
             f'- Gönderilen: {m["sent"]} (bugün {m["sent_today"]}/{cfg["daily_email_limit"]}) · Teslimat oranı: '
             f'{str(m["delivery_rate"]) + "%" if m["delivery_rate"] is not None else "ölçülemedi"}',
             f'- Gelen yanıt: {m["replies"]} · Olumlu: {m["positive"]} · Ret: {m["negative"]}', '',
             '## Telefon',
             f'- Planlı: {m["calls_planned"]} · Tamamlanan: {m["calls_done"]} · Toplam süre: {m["call_seconds"] // 60} dk {m["call_seconds"] % 60} sn',
             f'- Görüşme maliyeti (toplam): {tl(m["call_cost_minor"])}', '',
             '## Satış',
             f'- Demo talebi: {m["demos"]} · Açık fırsat: {m["opportunities"]} · Temsilciye atanan: {m["assigned"]} · Satış: {m["won"]}',
             f'- İletişimden çıkarılan kayıt: {m["suppressed"]}', '',
             '## Maliyet',
             f'- Bugün: {tl(m["spend_today_minor"])} / limit {tl(cfg["daily_spend_limit_minor"])} · Bu ay: {tl(m["spend_month_minor"])} / '
             f'limit {tl(cfg["monthly_spend_limit_minor"])}', '']
    demos = db.execute("SELECT d.created_at, c.legal_name, d.source FROM nova_demo_requests d JOIN nova_companies c ON c.id=d.company_id "
                       "WHERE d.status='new' ORDER BY d.created_at").fetchall()
    if demos:
        lines += ['## Hemen ilgilenilmesi gerekenler'] + [f'- Demo talebi: {r["legal_name"]} ({r["source"]}, {r["created_at"][:10]})' for r in demos] + ['']
    body = '\n'.join(lines)
    if store:
        db.execute('INSERT INTO nova_reports VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET body=excluded.body, created_at=excluded.created_at',
                   (day, body, stamp()))
    return body


def snapshot(db, limit=200):
    return {
        'settings': settings(db),
        'metrics': metrics(db),
        'companies': [dict(r) for r in db.execute('SELECT * FROM nova_companies ORDER BY score DESC, created_at DESC LIMIT ?', (limit,)).fetchall()],
        'drafts': [dict(r) for r in db.execute("SELECT e.*, c.legal_name FROM nova_emails e JOIN nova_companies c ON c.id=e.company_id "
                                                "WHERE e.direction='out' AND e.status IN ('draft','approved') ORDER BY e.created_at").fetchall()],
        'inbound': [dict(r) for r in db.execute("SELECT e.*, c.legal_name FROM nova_emails e JOIN nova_companies c ON c.id=e.company_id "
                                                 "WHERE e.direction='in' ORDER BY e.created_at DESC LIMIT 50").fetchall()],
        'calls': [dict(r) for r in db.execute('SELECT c.*, n.legal_name FROM nova_calls c LEFT JOIN nova_companies n ON n.id=c.company_id '
                                               'ORDER BY c.created_at DESC LIMIT 50').fetchall()],
        'demos': [dict(r) for r in db.execute('SELECT d.*, c.legal_name FROM nova_demo_requests d JOIN nova_companies c ON c.id=d.company_id '
                                               'ORDER BY d.created_at DESC LIMIT 50').fetchall()],
        'knowledge': [dict(r) for r in db.execute('SELECT * FROM nova_knowledge ORDER BY product,topic').fetchall()],
        'test_numbers': [{'label': r['label'], 'masked': r['phone'][:6] + '…' + r['phone'][-2:]} for r in
                         db.execute('SELECT * FROM nova_test_numbers').fetchall()],
        'reps': [dict(r) for r in db.execute('SELECT id,name,email FROM sales_accounts WHERE active=1 ORDER BY name').fetchall()],
        'events': [dict(r) for r in db.execute('SELECT * FROM nova_events ORDER BY stamp DESC LIMIT 40').fetchall()],
    }
