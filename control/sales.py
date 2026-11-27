"""Sales business rules. Caller holds one CONTROL write transaction per command.

Amounts are integer kuruş, exclusive of VAT. No mail or payment provider calls.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
import re
from uuid import uuid4
from .ik_bridge import ISTANBUL


class Invalid(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


@dataclass(frozen=True)
class Actor:
    id: str
    company_id: int | None
    founder: bool = False


DDL = (Path(__file__).resolve().parents[1] / 'migrations/sales/001_sales.up.sql').read_text(encoding='utf-8')
SQLITE_GUARDS = (Path(__file__).resolve().parents[1] / 'migrations/sales/002_sqlite_guards.up.sql').read_text(encoding='utf-8')


def uid():
    return uuid4().hex


def stamp():
    return datetime.now(timezone.utc).isoformat()


def business_today():
    return datetime.now(ISTANBUL).date()


def fail(message, status=409):
    raise Invalid(status, message)


def text(data, key, limit=1000):
    value = data.get(key)
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        fail(f'Geçerli {key} alanı gerekli (en çok {limit} karakter).', 422)
    return value.strip()


def integer(data, key, minimum=0, maximum=10**12):
    value = data.get(key)
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]{1,13}', str(value)):
        fail(f'{key}: tam sayı kuruş/değer gerekli.', 422)
    value = int(value)
    if not minimum <= value <= maximum:
        fail(f'{key}: değer sınır dışında.', 422)
    return value


def admin(actor):
    if not actor.founder:
        fail('CONTROL kurucu yetkisi gerekli.', 403)


def audit(db, actor, event, target, detail=''):
    db.execute('INSERT INTO sales_audit VALUES(?,?,?,?,?,?,?)',
               (uid(), actor.company_id, actor.id, event, target, detail, stamp()))


TABLES = {'customers', 'opportunities', 'activities', 'proposals'}


def owned(db, actor, table, target):
    assert table in TABLES
    sql = f'SELECT * FROM sales_{table} WHERE id=?'
    params = [target]
    if not actor.founder:
        sql += ' AND company_id=? AND owner_id=?'
        params += [actor.company_id, actor.id]
    row = db.execute(sql, params).fetchone()
    if row is None:
        fail('Kayıt bulunamadı.', 404)
    return row


def rows(db, actor, table):
    assert table in TABLES
    if actor.founder:
        return [dict(r) for r in db.execute(f'SELECT * FROM sales_{table} ORDER BY id').fetchall()]
    return [dict(r) for r in db.execute(f'SELECT * FROM sales_{table} WHERE company_id=? AND owner_id=? ORDER BY id',
                                      (actor.company_id, actor.id)).fetchall()]


def identity_key(data):
    # A strong business identifier is required. No fuzzy matches reveal other owners.
    tax = str(data.get('tax_id', '')).strip()
    domain = str(data.get('domain', '')).strip().lower().rstrip('.')
    if tax:
        if not re.fullmatch(r'[0-9]{10,11}', tax):
            fail('Vergi numarası 10 veya 11 rakam olmalı.', 422)
        return 'tax:' + tax
    if not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', domain):
        fail('Vergi numarası veya geçerli firma alan adı gerekli.', 422)
    return 'domain:' + domain


def customer(db, actor, data):
    if actor.founder:
        fail('Müşteri temsilci hesabından oluşturulmalı.', 403)
    name, key = text(data, 'name', 160), identity_key(data)
    keys = [key]
    if data.get('tax_id') and data.get('domain'):
        keys.append(identity_key({'domain': data['domain']}))
    prior = None
    for match in keys:
        found = db.execute('SELECT c.* FROM sales_customers c JOIN sales_customer_identities i ON i.customer_id=c.id WHERE i.identity_key=?', (match,)).fetchone()
        if found:
            prior = found
    if prior:
        target = uid()
        db.execute('INSERT INTO sales_claims(id,company_id,owner_id,customer_id,requested_name) VALUES(?,?,?,?,?)',
                   (target, actor.company_id, actor.id, prior['id'], name))
        audit(db, actor, 'ownership_claim', target)
        return {'claim_id': target, 'status': 'pending', 'detail': 'Sahiplik incelemesi CONTROL kuyruğuna alındı.'}
    target = uid()
    db.execute('INSERT INTO sales_customers(id,company_id,owner_id,name,identity_key) VALUES(?,?,?,?,?)',
               (target, actor.company_id, actor.id, name, key))
    for match in keys:
        db.execute('INSERT INTO sales_customer_identities VALUES(?,?)', (match, target))
    audit(db, actor, 'customer_created', target)
    return {'id': target}


def resolve_claim(db, actor, data):
    admin(actor)
    target, decision, reason = text(data, 'id', 32), text(data, 'decision', 20), text(data, 'reason')
    row = db.execute('SELECT * FROM sales_claims WHERE id=? AND status=?', (target, 'pending')).fetchone()
    if not row or decision not in ('accepted', 'rejected'):
        fail('Bekleyen sahiplik talebi ve geçerli karar gerekli.')
    if decision == 'accepted':
        # History must retain its original owner. Transfer only before any opportunity exists.
        if db.execute('SELECT 1 FROM sales_opportunities WHERE customer_id=?', (row['customer_id'],)).fetchone():
            fail('Fırsat geçmişi olan müşteri devredilemez; mevcut sahipte tutup talebi gerekçeyle reddedin.')
        db.execute('UPDATE sales_customers SET company_id=?,owner_id=? WHERE id=?',
                   (row['company_id'], row['owner_id'], row['customer_id']))
    db.execute('UPDATE sales_claims SET status=?,reason=? WHERE id=?', (decision, reason, target))
    audit(db, actor, 'ownership_' + decision, target, reason)
    return {'id': target, 'status': decision}


def classify(db, actor, data):
    admin(actor)
    row = owned(db, actor, 'customers', text(data, 'id', 32))
    value, reason = text(data, 'classification', 20), text(data, 'reason')
    if value not in ('new', 'existing'):
        fail('Yeni/mevcut müşteri seçin.', 422)
    if db.execute('SELECT 1 FROM sales_subscriptions WHERE customer_id=?', (row['id'],)).fetchone():
        fail('Tahsilat geçmişinden sonra müşteri sınıfı değiştirilemez.')
    db.execute('UPDATE sales_customers SET classification=? WHERE id=?', (value, row['id']))
    audit(db, actor, 'customer_classified', row['id'], value + ': ' + reason)
    return {'id': row['id']}


def opportunity(db, actor, data):
    row = owned(db, actor, 'customers', text(data, 'customer_id', 32))
    kind = text(data, 'kind', 20)
    if kind not in ('subscription', 'upgrade', 'addon', 'project'):
        fail('Geçersiz satış türü.', 422)
    target = uid()
    db.execute('INSERT INTO sales_opportunities VALUES(?,?,?,?,?,?)',
               (target, row['company_id'], row['owner_id'], row['id'], text(data, 'title', 200), kind))
    audit(db, actor, 'opportunity_created', target)
    return {'id': target}


def activity(db, actor, data):
    row = owned(db, actor, 'opportunities', text(data, 'opportunity_id', 32))
    due = data.get('due_date', '')
    if due:
        try:
            due = date.fromisoformat(due).isoformat()
        except (ValueError, TypeError):
            fail('Geçersiz görev tarihi.', 422)
    target = uid()
    db.execute('INSERT INTO sales_activities VALUES(?,?,?,?,?,?)',
               (target, row['company_id'], row['owner_id'], row['id'], text(data, 'note', 4000), due))
    audit(db, actor, 'activity_created', target)
    return {'id': target}


def proposal(db, actor, data):
    row = owned(db, actor, 'opportunities', text(data, 'opportunity_id', 32))
    previous = db.execute('SELECT * FROM sales_proposals WHERE opportunity_id=? ORDER BY version DESC LIMIT 1', (row['id'],)).fetchone()
    if previous and previous['status'] != 'revision':
        fail('Yeni sürüm için CONTROL revizyon kararı gerekli.')
    target = uid()
    db.execute('INSERT INTO sales_proposals(id,company_id,owner_id,opportunity_id,version,status,requested_minor,brief) VALUES(?,?,?,?,?,?,?,?)',
               (target, row['company_id'], row['owner_id'], row['id'], 1 if not previous else previous['version']+1,
                'draft', integer(data, 'requested_minor', 1), text(data, 'brief', 4000)))
    audit(db, actor, 'proposal_drafted', target)
    return {'id': target}


def submit(db, actor, data):
    row = owned(db, actor, 'proposals', text(data, 'id', 32))
    if row['status'] != 'draft':
        fail('Yalnız taslak gönderilebilir.')
    db.execute("UPDATE sales_proposals SET status='submitted' WHERE id=?", (row['id'],))
    audit(db, actor, 'proposal_submitted', row['id'])
    return {'id': row['id']}


def decide(db, actor, data):
    admin(actor)
    row = owned(db, actor, 'proposals', text(data, 'id', 32))
    decision, reason = text(data, 'decision', 20), text(data, 'reason')
    if row['status'] != 'submitted' or decision not in ('approved', 'revision', 'rejected'):
        fail('Karar yalnız gönderilmiş teklif için verilebilir.')
    official = integer(data, 'official_minor', 1) if decision == 'approved' else None
    discount = integer({'discount_minor': data.get('discount_minor') or '0'}, 'discount_minor') if decision == 'approved' else 0
    if official and discount >= official:
        fail('İndirim liste tutarından küçük olmalı.', 422)
    db.execute('UPDATE sales_proposals SET status=?,official_minor=?,discount_minor=? WHERE id=?',
               (decision, official, discount, row['id']))
    db.execute('INSERT INTO sales_decisions VALUES(?,?,?,?,?,?)', (uid(), row['id'], actor.id, decision, reason, stamp()))
    audit(db, actor, 'proposal_' + decision, row['id'], reason)
    return {'id': row['id'], 'status': decision}


def collection(db, actor, data):
    admin(actor)
    p = owned(db, actor, 'proposals', text(data, 'proposal_id', 32))
    if p['status'] != 'approved':
        fail('Onaysız teklif için tahsilat doğrulanamaz.')
    op = owned(db, actor, 'opportunities', p['opportunity_id'])
    c = owned(db, actor, 'customers', op['customer_id'])
    if c['classification'] == 'unverified':
        fail('CONTROL müşteri sınıflandırmasını tamamlamalı.')
    amount = integer(data, 'net_minor', 1)
    if amount != p['official_minor'] - p['discount_minor']:
        fail('MVP: KDV hariç tam dönem tahsilatı resmi net teklife eşit olmalı.', 422)
    try:
        collected = date.fromisoformat(text(data, 'collected_on', 10))
    except ValueError:
        fail('Geçersiz tahsilat tarihi.', 422)
    if collected > business_today():
        fail('Gelecek tarihli tahsilat doğrulanamaz.', 422)
    period = integer(data, 'period', 1, 12000)
    target, rate = _book(db, p, op, c, period, text(data, 'reference', 120), amount,
                         integer(data, 'cost_minor'), collected.isoformat(), actor.id, text(data, 'reason'))
    audit(db, actor, 'collection_verified', target, f'net_minor={amount}; rate={rate}')
    return {'id': target}


def _book(db, p, op, c, period, reference, amount, cost, collected_on, verified_by, reason, commission_basis_minor=None):
    """Shared booking + automatic commission rules (manual CONTROL entry and verified Shopier payment)."""
    sub_id, rate = None, 0
    if op['kind'] != 'project':
        sub = db.execute('SELECT * FROM sales_subscriptions WHERE opportunity_id=?', (op['id'],)).fetchone()
        if sub is None:
            if period != 1:
                fail('İlk tahsilat dönemi 1 olmalı.')
            # %25 only for a NEW customer's first product subscription; upgrade/addon/existing => 0.
            eligible = int(c['classification'] == 'new' and op['kind'] == 'subscription' and not db.execute(
                'SELECT 1 FROM sales_collections x JOIN sales_proposals p ON p.id=x.proposal_id JOIN sales_opportunities o ON o.id=p.opportunity_id WHERE o.customer_id=?', (c['id'],)).fetchone())
            sub_id = uid()
            db.execute('INSERT INTO sales_subscriptions VALUES(?,?,?,?)', (sub_id, op['id'], c['id'], eligible))
        else:
            sub_id, eligible = sub['id'], sub['eligible']
            if db.execute('SELECT 1 FROM sales_collections WHERE subscription_id=? AND held=1', (sub_id,)).fetchone():
                fail('İade/iptal incelemesindeki aboneliğe yeni tahakkuk üretilemez.')
            last = db.execute('SELECT MAX(period) FROM sales_collections WHERE subscription_id=?', (sub_id,)).fetchone()[0]
            if period != last+1:
                fail('Tahsilat dönemi mükerrer veya sırasız.')
        rate = (25 if period == 1 else 5) if eligible else 0
    elif period != 1:
        fail('Tek seferlik proje dönemi 1 olmalı.', 422)
    target = uid()
    db.execute('INSERT INTO sales_collections(id,proposal_id,subscription_id,period,reference,net_minor,cost_minor,collected_on,verified_by,reason) VALUES(?,?,?,?,?,?,?,?,?,?)',
               (target, p['id'], sub_id, period, reference, amount, cost, collected_on, verified_by, reason))
    basis = amount if commission_basis_minor is None else commission_basis_minor
    if not 0 <= basis <= amount:
        fail('Komisyon matrahı geçersiz.', 422)
    # Projects deliberately have no automatic accrual.
    if op['kind'] != 'project':
        db.execute('INSERT INTO sales_commissions(id,collection_id,amount_minor,rate,status) VALUES(?,?,?,?,?)',
                   (uid(), target, (basis*rate+50)//100, rate, 'pending'))
    return target, rate


def next_period(db, opportunity_id):
    sub = db.execute('SELECT id FROM sales_subscriptions WHERE opportunity_id=?', (opportunity_id,)).fetchone()
    if sub is None:
        return 1
    return (db.execute('SELECT MAX(period) FROM sales_collections WHERE subscription_id=?', (sub['id'],)).fetchone()[0] or 0) + 1


def book_verified_payment(db, *, proposal_id, net_minor, reference, collected_on, verifier_user_id, commission_basis_minor=None):
    """Called inside the payment-activation transaction after Shopier verification.
    HR FLOW may supply a separate approved commission basis; other products retain net collection."""
    p = db.execute('SELECT * FROM sales_proposals WHERE id=?', (proposal_id,)).fetchone()
    if p is None or p['status'] != 'approved':
        fail('Onaylı teklif yok; tahsilat CONTROL incelemesine alındı.')
    op = db.execute('SELECT * FROM sales_opportunities WHERE id=?', (p['opportunity_id'],)).fetchone()
    c = db.execute('SELECT * FROM sales_customers WHERE id=?', (op['customer_id'],)).fetchone()
    if c['classification'] == 'unverified':
        fail('Müşteri sınıflandırması bekliyor; tahsilat CONTROL incelemesine alındı.')
    if op['kind'] == 'project':
        fail('Proje satışları otomatik komisyona girmez.')
    target, rate = _book(db, p, op, c, next_period(db, op['id']), reference, net_minor, 0,
                         collected_on, verifier_user_id, 'Shopier API ile doğrulanmış otomatik tahsilat', commission_basis_minor)
    db.execute('INSERT INTO sales_audit VALUES(?,?,?,?,?,?,?)',
               (uid(), p['company_id'], 'shopier_verifier', 'collection_verified', target,
                f'net_minor={net_minor}; basis={commission_basis_minor}; rate={rate}; ref={reference}', stamp()))
    return target, rate


def project_commission(db, actor, data):
    admin(actor)
    target = text(data, 'collection_id', 32)
    row = db.execute('SELECT c.* FROM sales_collections c JOIN sales_proposals p ON p.id=c.proposal_id JOIN sales_opportunities o ON o.id=p.opportunity_id WHERE c.id=? AND o.kind=?', (target, 'project')).fetchone()
    if not row or row['held']:
        fail('Aktif ve doğrulanmış proje tahsilatı gerekli.')
    reason = text(data, 'reason', 4000)
    # This written decision must include the founder's cost/profit analysis, not a rate.
    analysis = text(data, 'analysis', 4000)
    amount = integer(data, 'amount_minor', 0, row['net_minor'])
    cid = uid()
    db.execute('INSERT INTO sales_commissions(id,collection_id,amount_minor,rate,status,manual_reason,approved_by) VALUES(?,?,?,?,?,?,?)',
               (cid, target, amount, 0, 'approved', reason+' | Maliyet/kâr: '+analysis, actor.id))
    audit(db, actor, 'project_commission_decided', cid, reason+' | '+analysis)
    return {'id': cid}


def commission_row(db, target):
    row = db.execute('SELECT m.*,c.collected_on,c.held FROM sales_commissions m JOIN sales_collections c ON c.id=m.collection_id WHERE m.id=?', (target,)).fetchone()
    if row is None:
        fail('Hak ediş bulunamadı.', 404)
    return row


def payment_window(collected_on):
    d = date.fromisoformat(collected_on)
    start = date(d.year + (d.month == 12), 1 if d.month == 12 else d.month+1, 1)
    return start, start.replace(day=15)


def commission_action(db, actor, data, today=None):
    admin(actor)
    row = commission_row(db, text(data, 'id', 32))
    action, reason = text(data, 'decision', 20), text(data, 'reason')
    if row['held'] or row['status'] == 'reversed':
        fail('İade/iptal incelemesindeki hak ediş ödenemez.')
    if action == 'approved' and row['status'] == 'pending':
        db.execute("UPDATE sales_commissions SET status='approved',approved_by=? WHERE id=?", (actor.id, row['id']))
    elif action == 'paid' and row['status'] == 'approved':
        start, end = payment_window(row['collected_on'])
        today = today or business_today()
        if not start <= today <= end:
            fail('Ödeme işareti tahsilatı izleyen ayın 1–15 günleri arasında verilebilir.')
        db.execute("UPDATE sales_commissions SET status='paid',paid_on=? WHERE id=?", (today.isoformat(), row['id']))
    else:
        fail('Geçersiz hak ediş geçişi.')
    audit(db, actor, 'commission_' + action, row['id'], reason)
    return {'id': row['id']}


def hold(db, actor, data):
    admin(actor)
    target, reason = text(data, 'collection_id', 32), text(data, 'reason')
    row = db.execute('SELECT * FROM sales_collections WHERE id=?', (target,)).fetchone()
    if not row or row['held']:
        fail('Aktif tahsilat bulunamadı.')
    db.execute('UPDATE sales_collections SET held=1 WHERE id=?', (target,))
    audit(db, actor, 'refund_or_cancellation_hold', target, reason)
    return {'id': target}


def reverse(db, actor, data):
    admin(actor)
    row = commission_row(db, text(data, 'id', 32))
    if not row['held'] or row['status'] == 'reversed':
        fail('Ters kayıt için önce iade/iptal incelemesi gerekli.')
    reason = text(data, 'reason', 4000)
    db.execute('INSERT INTO sales_adjustments VALUES(?,?,?,?,?,?)', (uid(), row['id'], -row['amount_minor'], reason, actor.id, stamp()))
    db.execute("UPDATE sales_commissions SET status='reversed' WHERE id=?", (row['id'],))
    audit(db, actor, 'commission_reversed', row['id'], reason)
    return {'id': row['id']}


COMMANDS = {'customer': customer, 'resolve_claim': resolve_claim, 'classify': classify,
            'opportunity': opportunity, 'activity': activity, 'proposal': proposal,
            'submit': submit, 'decide': decide, 'collection': collection,
            'project_commission': project_commission, 'commission_action': commission_action,
            'hold': hold, 'reverse': reverse}


def snapshot(db, actor):
    result = {t: rows(db, actor, t) for t in TABLES}
    scope = '' if actor.founder else ' WHERE p.company_id=? AND p.owner_id=?'
    params = () if actor.founder else (actor.company_id, actor.id)
    result['decisions'] = [dict(r) for r in db.execute('SELECT d.* FROM sales_decisions d JOIN sales_proposals p ON p.id=d.proposal_id'+scope+' ORDER BY d.stamp', params).fetchall()]
    result['collections'] = [dict(r) for r in db.execute('SELECT c.* FROM sales_collections c JOIN sales_proposals p ON p.id=c.proposal_id'+scope, params).fetchall()]
    result['commissions'] = [dict(r) for r in db.execute('SELECT m.*,c.collected_on,c.held FROM sales_commissions m JOIN sales_collections c ON c.id=m.collection_id JOIN sales_proposals p ON p.id=c.proposal_id'+scope, params).fetchall()]
    for item in result['commissions']:
        start, end = payment_window(item['collected_on'])
        item.update(payable_from=start.isoformat(), payable_until=end.isoformat())
    result['adjustments'] = [dict(r) for r in db.execute('SELECT a.* FROM sales_adjustments a JOIN sales_commissions m ON m.id=a.commission_id JOIN sales_collections c ON c.id=m.collection_id JOIN sales_proposals p ON p.id=c.proposal_id'+scope, params).fetchall()]
    if actor.founder:
        result['accounts'] = [dict(r) for r in db.execute('SELECT id,company_id,name,email,active FROM sales_accounts').fetchall()]
        result['claims'] = [dict(r) for r in db.execute('SELECT * FROM sales_claims').fetchall()]
        result['audit'] = [dict(r) for r in db.execute('SELECT * FROM sales_audit ORDER BY stamp').fetchall()]
    else:
        result['claims'] = [dict(r) for r in db.execute('SELECT id,status,requested_name,reason FROM sales_claims WHERE company_id=? AND owner_id=?', params).fetchall()]
        # Provider costs and management profitability are not representative data.
        for item in result['collections']:
            item.pop('cost_minor', None)
    return result
