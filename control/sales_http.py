"""CONTROL-integrated Sales Hub HTTP adapter. No public signup or outbound actions."""
from contextlib import closing
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from http.cookies import SimpleCookie
import hmac
import json
import re
import secrets
import time
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

from .billing_catalog import EDUCATION, HR_FLOW
from . import sales

LABELS = {
    'draft': 'Taslak', 'submitted': 'CONTROL değerlendirmesinde', 'revision': 'Revizyon istendi',
    'approved': 'Onaylandı', 'rejected': 'Reddedildi', 'pending': 'İncelemede',
    'paid': 'Ödendi olarak işaretlendi', 'reversed': 'Ters kayıt yapıldı',
    'new': 'Yeni müşteri', 'existing': 'Mevcut müşteri', 'unverified': 'Sınıflandırma bekliyor',
    'subscription': 'Abonelik', 'upgrade': 'Paket yükseltmesi', 'addon': 'Ek ürün/hizmet',
    'project': 'Özel proje', 'accepted': 'Sahiplik devredildi',
}

MONEY_FIELDS = {'requested_minor','official_minor','discount_minor','net_minor','cost_minor','amount_minor'}

# Only presentation changes: primary keys, API values and timestamps remain untouched.
ISTANBUL = ZoneInfo('Europe/Istanbul')
FOUNDER_TABS = (
    ('accounts','Temsilciler'), ('claims','Sahiplik incelemeleri'),
    ('customers','Müşteriler'), ('opportunities','Satış fırsatları'),
    ('proposals','Teklif kararları'), ('tasks','Görevler'),
    ('finance','Tahsilat ve komisyon'), ('payments','Online ödemeler'), ('catalog','Ürünler ve fiyatlar'), ('audit','Denetim kayıtları'),
)


def record_code(c, value, kind):
    """Display a compact reference; retain the real ID in every form and API."""
    raw = str(value or '')
    if not re.fullmatch(r'[0-9a-f]{32}', raw):
        return c.e(raw or '—')
    display = f'{kind}-{raw[:6].upper()}-{raw[-4:].upper()}'
    return f'<span class="sales-record-code" title="{c.e(raw)}">{c.e(display)}</span>'


def local_date(value):
    """Format existing UTC timestamps in Türkiye time without editing stored data."""
    if not value:
        return '—'
    raw = str(value).strip()
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
            return date.fromisoformat(raw).strftime('%d.%m.%Y')
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ISTANBUL).strftime('%d.%m.%Y %H:%M') + ' TSİ'
    except (ValueError, OverflowError):
        return raw


def founder_tabs(c, selected):
    links = ''.join(
        f'<a href="/satis?tab={key}"'+
        (' aria-current="page"' if selected == key else '')+
        f'>{c.e(label)}</a>' for key,label in FOUNDER_TABS)
    return '<nav class="sales-section-tabs" aria-label="Satış Yönetimi bölümleri">'+links+'</nav>'



def sales_table(c, empty_message, headings, rows):
    """Context-specific empty state; leave the shared CONTROL table renderer untouched."""
    if not rows:
        return '<p class="sales-empty" role="status">' + c.e(empty_message) + '</p>'
    return c.table(headings, rows)


def tl_to_minor(value):
    """Accept exact TL strings, never binary floats or silently rounded cents."""
    if not isinstance(value,str) or len(value)>24:
        sales.fail('Tutarı TL olarak girin; en çok iki ondalık basamak kullanın.',422)
    value=value.strip()
    if re.fullmatch(r'[0-9]{1,3}(?:\.[0-9]{3})+(?:,[0-9]{1,2})?',value):
        value=value.replace('.','').replace(',','.')
    elif re.fullmatch(r'[0-9]+(?:[,.][0-9]{1,2})?',value):
        value=value.replace(',','.')
    else:
        sales.fail('Geçerli TL tutarı girin (örnek: 1.234,56). İkiden fazla ondalık kabul edilmez.',422)
    minor=int(Decimal(value)*100)
    return sales.integer({'amount':minor},'amount')


def money(value):
    if value is None:
        return '—'
    sign = '-' if value < 0 else ''
    value = abs(value)
    return sign+f'{value//100:,}'.replace(',', '.')+f',{value%100:02d} TL'


def badge(c, value):
    return f'<span class="status status-{c.e(value)}">{c.e(LABELS.get(value, value))}</span>'


def input_field(c, name, label, kind='text', required=True, value='', maximum=1000):
    monetary=name in MONEY_FIELDS
    if monetary:
        name=name.replace('_minor','_tl')
        label=label.replace('(kuruş)','(TL)')
        kind='text'
    return (f'<label>{c.e(label)}<input name="{name}" type="{kind}" value="{c.e(value)}" '
            f'maxlength="{maximum}" '+('required ' if required else '')+
            ('inputmode="decimal" placeholder="0,00" ' if monetary else '')+
            ('min="0" step="1" ' if kind == 'number' else '')+'></label>')


def select(c, name, label, options):
    # Never show a blank native dropdown. The business prerequisite is handled
    # by guided_form(), so an empty list cannot trigger an unusable submission.
    entries = list(options)
    placeholder = 'Seçiniz' if entries else 'Henüz seçim yapılabilecek kayıt yok'
    choices = f'<option value="" disabled selected>{c.e(placeholder)}</option>'
    choices += ''.join(
        f'<option value="{c.e(str(key))}">{c.e(str(value))}</option>' for key, value in entries)
    disabled = ' disabled' if not entries else ''
    return f'<label>{c.e(label)}<select name="{name}" required{disabled}>'+choices+'</select></label>'


def guided_form(c, can_submit, form_html, explanation, target=None, action_label=None):
    """Show an actionable prerequisite instead of an empty form."""
    if can_submit:
        return form_html
    link = (f' <a class="sales-next-step" href="{c.e(target)}">{c.e(action_label)}</a>'
            if target and action_label else '')
    return f'<div class="sales-prerequisite" role="status"><p>{c.e(explanation)}</p>{link}</div>'


def form(c, path, csrf, action, fields, button='Kaydet'):
    return c.form(path, {'csrf': csrf}, c.hidden('action', action)+fields, button)


def commands(c, db, actor, data):
    data=dict(data)
    for key in MONEY_FIELDS:
        tl_key=key.replace('_minor','_tl')
        if tl_key in data:
            if key in data:
                sales.fail('TL ve kuruş alanlarını birlikte göndermeyin.',422)
            raw=data.pop(tl_key)
            if raw != '':
                data[key]=tl_to_minor(raw)
    action = sales.text(data, 'action', 40)
    if action == 'account_create':
        sales.admin(actor)
        tenant = sales.integer(data, 'company_id', 1, 10**12)
        c.company(db, tenant)
        target = sales.uid()
        db.execute('INSERT INTO sales_accounts(id,company_id,name,email,secret) VALUES(?,?,?,?,?)',
                   (target, tenant, sales.text(data, 'name', 120), c.email(data), c.new_secret(data)))
        sales.audit(db, actor, 'account_created_inactive', target)
        return {'id': target, 'active': False}
    if action == 'account_active':
        sales.admin(actor)
        target, active = sales.text(data, 'id', 32), sales.integer(data, 'active', 0, 1)
        if not db.execute('SELECT 1 FROM sales_accounts WHERE id=?', (target,)).fetchone():
            sales.fail('Temsilci bulunamadı.', 404)
        reason = sales.text(data, 'reason')
        db.execute('UPDATE sales_accounts SET active=? WHERE id=?', (active, target))
        db.execute('DELETE FROM sales_sessions WHERE account_id=?', (target,))
        sales.audit(db, actor, 'account_activated' if active else 'account_deactivated', target, reason)
        return {'id': target, 'active': bool(active)}
    if action == 'password':
        if actor.founder:
            sales.fail('CONTROL profil ayarlarını kullanın.', 403)
        row = db.execute('SELECT secret FROM sales_accounts WHERE id=?', (actor.id,)).fetchone()
        if not c.check_ik_password(sales.text(data, 'old_password', 128), row['secret']):
            sales.fail('Mevcut parola doğrulanamadı.', 403)
        db.execute('UPDATE sales_accounts SET secret=? WHERE id=?', (c.new_secret(data), actor.id))
        db.execute('DELETE FROM sales_sessions WHERE account_id=?', (actor.id,))
        sales.audit(db, actor, 'password_changed', actor.id)
        return {'detail': 'Parola değişti. Yeniden giriş yapın.'}
    if action in ('payment_request', 'link_company', 'verify_order', 'end_test'):
        return billing_command(c, db, actor, data, action)
    if action not in sales.COMMANDS:
        sales.fail('İşlem bulunamadı.', 404)
    return sales.COMMANDS[action](db, actor, data)


def billing_command(c, db, actor, data, action):
    from datetime import date as _date, datetime as _dt, timezone as _tz
    from . import billing_payments as bp, shopier_hr
    now = _dt.now(_tz.utc)
    if action == 'payment_request':
        product = sales.text(data, 'product', 20)
        if product not in ('hr_flow', 'education'):
            sales.fail('Ürün seçin.', 422)
        try:
            start = _date.fromisoformat(sales.text(data, 'period_start', 10))
        except ValueError:
            sales.fail('Geçersiz dönem başlangıcı.', 422)
        if start < sales.business_today():
            sales.fail('Dönem başlangıcı geçmiş tarih olamaz.', 422)
        proposal = data.get('proposal_id') or None
        if proposal == 'direct':
            proposal = None
        company = None if proposal else sales.integer(data, 'company_id', 1, 10**12)
        ref = bp.create_payment_request(db, actor=actor, proposal_id=proposal, company_id=company, product=product,
                                        plan_name=sales.text(data, 'plan_name', 20), period_start=start, now=now)
        return {'ref': ref, 'url': c_public_url(ref)}
    sales.admin(actor)
    if action == 'link_company':
        customer = sales.owned(db, actor, 'customers', sales.text(data, 'customer_id', 32))
        tenant = sales.integer(data, 'company_id', 1, 10**12)
        c.company(db, tenant)
        db.execute('INSERT INTO sales_customer_companies VALUES(?,?,?,?)',
                   (customer['id'], tenant, int(actor.id), int(now.timestamp())))
        sales.audit(db, actor, 'customer_company_linked', customer['id'], f'company_id={tenant}')
        return {'id': customer['id']}
    if action == 'end_test':
        closed = bp.end_test(db, actor=actor, now=now)
        sales.audit(db, actor, 'billing_test_closed', 'billing', f'subscriptions={closed}')
        return {'detail': f'{closed} test aboneliği kapatıldı. Kayıtlar korunur.'}
    # verify_order: CONTROL reconciliation when a webhook was missed. Same server-side checks.
    order_id = sales.text(data, 'order_id', 80)
    try:
        result = bp.activate_from_order(db, order_id=order_id, fetch=shopier_hr.fetch_order,
                                        contract=bp.OFFICIAL_CONTRACT, now=now, actor='founder:'+str(actor.id))
    except (bp.PaymentRejected, shopier_hr.ShopierError) as exc:
        sales.fail(f'Sipariş doğrulanmadı: {exc}', 409)
    sales.audit(db, actor, 'order_verified_manually', order_id, result)
    return {'detail': 'Sipariş doğrulandı ve abonelik etkinleştirildi.' if result == 'activated'
            else 'Bu sipariş daha önce işlenmiş.'}


def c_public_url(ref):
    import os
    return os.environ.get('ERNASA_CONTROL_PUBLIC_ORIGIN', '') + '/control/odeme/' + ref


def current(c, h, db):
    founder = h.session(db)
    if founder and founder['role'] == 'founder':
        return sales.Actor(str(founder['id']), None, True), founder['csrf'], dict(founder)
    cookie = SimpleCookie(h.headers.get('Cookie', '')).get('sales_session')
    if not cookie or not re.fullmatch(r'[A-Za-z0-9_-]{43}', cookie.value):
        return None
    row = db.execute('SELECT a.*,s.csrf FROM sales_accounts a JOIN sales_sessions s ON s.account_id=a.id JOIN companies c ON c.id=a.company_id WHERE s.token=? AND s.expires>? AND a.active=1 AND c.status=?',
                     (c.token_hash(cookie.value), int(time.time()), 'Aktif')).fetchone()
    if row is None:
        return None
    return sales.Actor(row['id'], row['company_id']), row['csrf'], dict(row)


def login(c, h, db, data):
    cookie = SimpleCookie(h.headers.get('Cookie', '')).get('sales_login_csrf')
    if not cookie or not hmac.compare_digest(cookie.value.encode(), str(data.get('csrf', '')).encode()):
        sales.fail('Giriş formunu yenileyin.', 403)
    now = int(time.time())
    with h.server.login_lock:
        h.server.attempts = [t for t in h.server.attempts if t > now-60]
        if len(h.server.attempts) >= 12:
            sales.fail('Çok fazla giriş denemesi.', 429)
        h.server.attempts.append(now)
    row = db.execute('SELECT a.* FROM sales_accounts a JOIN companies c ON c.id=a.company_id WHERE a.email=? AND a.active=1 AND c.status=?', (c.email(data), 'Aktif')).fetchone()
    valid = c.check_ik_password(str(data.get('password', '')), row['secret'] if row else None)
    if not row or not valid:
        sales.fail('Giriş bilgileri geçersiz.', 401)
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.execute('DELETE FROM sales_sessions WHERE expires<=?', (now,))
    db.execute('INSERT INTO sales_sessions VALUES(?,?,?,?)', (c.token_hash(token), row['id'], csrf, now+c.TTL))
    sales.audit(db, sales.Actor(row['id'], row['company_id']), 'session_started', row['id'])
    return token


def set_cookie(h, name, token, age):
    secure = '; Secure' if h.server.origin.startswith('https://') else ''
    return ('Set-Cookie', f'{name}={token}; Path={h.server.public_prefix}/sales; HttpOnly; SameSite=Strict; Max-Age={age}{secure}')



def sales_login_form(c, csrf, error=None):
    """Always show the usable login form and the separate CONTROL founder entry."""
    fields = input_field(c,'email','E-posta','email')+input_field(c,'password','Parola','password')
    notice = ('<p>Bu giriş yalnızca CONTROL tarafından etkinleştirilmiş '
              'satış temsilcisi hesapları içindir.</p>'
              '<p><strong>ERNASA kurucusu veya yöneticisi misiniz?</strong> '
              '<a href="/giris">CONTROL yönetim girişini açın</a>.</p>')
    alert = f'<p class="sales-login-error" role="alert">{c.e(str(error))}</p>' if error else ''
    return alert + notice + form(c,'/sales/login',csrf,'login',fields,'Temsilci girişi')



def render(c, h, title, content, identity=None, status=200, headers=(), tab='overview'):
    prefix = h.server.public_prefix
    if identity:
        actor, csrf, info = identity
        if actor.founder:
            info=dict(info)
            info['routes']=c.allowed_routes(None,info)
            if not getattr(h.server,'ernasa_invite_handler',None):
                info['routes']=info['routes']-{c.ERNASA_INVITE_PATH}
            if not getattr(h.server,'experience_summary',None):
                info['routes']=info['routes']-{'/aday-deneyimi'}
            return h.page(title, content, info, status=status, path='/satis')
        links = (('overview','Ana sayfa'),('customers','Müşterilerim'),('opportunities','Fırsatlarım'),
                 ('proposals','Tekliflerim ve kararlar'),('tasks','Görevlerim'),('finance','Komisyonlarım'),('payments','Ödeme bağlantıları'),
                 ('catalog','Ürünler ve fiyatlar'),('resources','Satış kaynakları'),('profile','Profil ve güvenlik'))
        menu = '<nav aria-label="Satış menüsü">'+''.join(
            f'<a href="/sales?tab={key}"'+(' aria-current="page"' if key==tab else '')+
            f'>{c.icon("/")}{label}</a>' for key,label in links)+'</nav>'
        body = ('<a class="skip" href="#icerik">İçeriğe geç</a><aside>'+c.brand_block('ERNASA Sales Hub')+
                f'<div class="desktop-nav">{menu}</div><details class="mobile-nav"><summary>Satış menüsü</summary>{menu}</details></aside>'+
                '<div class="shell"><header><p class="breadcrumb">ERNASA / Sales Hub</p><div class="account">'+
                f'<div><strong>{c.e(info["name"])}</strong><small>{c.e(info["email"])} · Satış temsilcisi</small></div>'+
                form(c,'/sales/logout',csrf,'logout','','Çıkış')+'</div></header><main id="icerik">'+
                f'<h1>{c.e(title)}</h1>{content}</main></div>')
    else:
        body = '<main class="login"><section class="login-brand">'+c.brand_block('ERNASA Sales Hub')+'</section><section class="login-content">'+f'<h1>{c.e(title)}</h1>{content}</section></main>'
    page = ('<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'+
            f'<title>{c.e(title)} · ERNASA</title><link rel="stylesheet" href="/control.css"><link rel="icon" href="/ernasalogo.png"><script src="/sales-menu.js" defer></script></head><body>{body}</body></html>')
    if prefix:
        page = re.sub(r'((?:href|src|action)="/)(?!/)', lambda m: m[0][:-1]+prefix+'/', page)
    h.reply(status, page.encode(), headers=headers)


def overview(c, data):
    metrics = [('Müşteri',len(data['customers'])),('Fırsat',len(data['opportunities'])),
               ('Değerlendirmede',sum(p['status']=='submitted' for p in data['proposals'])),
               ('Bekleyen hak ediş',money(sum(m['amount_minor'] for m in data['commissions'] if m['status'] in ('pending','approved'))))]
    return '<div class="stats metrics">'+''.join(c.card(label,f'<strong>{c.e(str(value))}</strong>') for label,value in metrics)+'</div>'


PAYMENT_STATUS = {'awaiting_payment':'Ödeme bekleniyor','pending':'Ödeme bekleniyor','paid':'Ödendi','refunded':'İade edildi'}
SUB_STATUS = {'active':'Aktif','renewal_due':'Yenileme bekleniyor','expired':'Süresi doldu','suspended':'Askıda',
              'cancelled':'İptal edildi','refunded':'İade edildi','awaiting_payment':'Ödeme bekleniyor'}
COMMISSION_STATUS = {'pending':'Onay bekliyor','approved':'Onaylandı','paid':'Ödendi','reversed':'Ters kayıt'}


def payments_section(c, db, actor, data, f, inp, options):
    from . import billing_payments as bp
    from .billing_quotes import PLANS
    plans = [(p.name, p.name) for p in PLANS['hr_flow']]
    products = [('hr_flow','ERNASA HR FLOW'),('education','ERNASA Eğitim Platformu')]
    common = (options('product','Ürün',products)+options('plan_name','Paket',plans)+
              inp('period_start','Dönem başlangıcı','date'))
    out = ''
    try:
        rows = bp.rep_sales(db, actor)
    except Exception:
        return c.card('Online ödemeler','<p role="status">Ödeme tabloları henüz kurulmadı (migration 005).</p>')
    if actor.founder:
        companies = db.execute('SELECT id,name FROM companies ORDER BY name').fetchall()
        linked = {r[0] for r in db.execute('SELECT customer_id FROM sales_customer_companies').fetchall()}
        unlinked = [(r['id'], r['name']) for r in data['customers'] if r['id'] not in linked and r['classification'] != 'unverified']
        out += c.card('Müşteri – CONTROL firma eşleşmesi','<p>Temsilcinin ödeme bağlantısı oluşturabilmesi için müşterinin aboneliği alacak CONTROL firmasıyla eşleşmesi gerekir. Eşleşme sonradan değiştirilemez.</p>'+
                      guided_form(c, bool(unlinked), f('link_company', options('customer_id','Satış müşterisi',unlinked)+options('company_id','CONTROL firması',[(r['id'],r['name']) for r in companies]),'Eşleştir'),
                                  'Eşleşme bekleyen sınıflandırılmış müşteri yok.'))
        out += c.card('Doğrudan satış ödeme bağlantısı','<p>Temsilcisiz satış; komisyon oluşmaz.</p>'+
                      f('payment_request', options('company_id','Firma',[(r['id'],r['name']) for r in companies])+common,'Bağlantı oluştur'))
        approved_all = [(r['id'], f"{r['brief'][:50]} · {r['owner_id']}") for r in data['proposals'] if r['status']=='approved']
        if bp.test_plan_enabled():
          out += c.card('Canlı ödeme testi (0,01 TL)','<p>Yalnız kurucu görür. Eğitim · 1 katılımcı · 0,01 TL. Temsilcinin onaylı teklifi seçilirse tahsilat ve komisyon kaydı da test edilir (komisyon tutarı 0 kuruş).</p>'+
                      f('payment_request', '<input type="hidden" name="product" value="education"><input type="hidden" name="plan_name" value="Ödeme Testi">'+
                        options('company_id','Firma (temsilci teklifi seçilmezse)',[(r['id'],r['name']) for r in companies])+
                        options('proposal_id','Temsilci teklifi (isteğe bağlı)',[('direct','— doğrudan satış (komisyonsuz) —')]+approved_all)+
                        inp('period_start','Dönem başlangıcı','date'),'Test bağlantısı oluştur')+
                      '<p>Test bitince test aboneliklerini kapatın; ardından test paketi Render ayarından kapatılır.</p>'+
                      f('end_test','','Test aboneliklerini kapat'))
        out += c.card('Shopier siparişini elle doğrula','<p>Webhook ulaşmadıysa Shopier sipariş numarasıyla aynı sunucu kontrolleri çalıştırılır.</p>'+
                      f('verify_order', inp('order_id','Shopier sipariş numarası'),'Doğrula'))
    else:
        approved = [(r['id'], f"{r['brief'][:60]} · sürüm {r['version']}") for r in data['proposals'] if r['status']=='approved']
        out += c.card('Müşteriye özel ödeme bağlantısı','<p>Yalnız CONTROL onaylı teklifleriniz için. Tutar, müşteri ödemeye başladığında güncel TCMB kuruyla hesaplanır ve 15 dakika sabitlenir.</p>'+
                      guided_form(c, bool(approved), f('payment_request', options('proposal_id','Onaylı teklif',approved)+common,'Bağlantı oluştur'),
                                  'Önce CONTROL onaylı bir teklif gerekir.', '/sales?tab=proposals', 'Tekliflerime git'))
    money_or = lambda v: money(v) if v is not None else '—'
    for r in rows:
        if r.get('is_test'):
            r['basis_minor'], r['rate'], r['commission_minor'], r['commission_window'] = (
                r['test_basis'], r['test_rate'], r['test_commission'], r['test_window'])
            r['commission_status'] = None
            r['customer'] = 'TEST · ' + r['customer']
    body = [[c.e(r['customer']), c.e({'hr_flow':'HR FLOW','education':'Eğitim'}[r['product']]), c.e(r['plan_name']),
             c.e(local_date(datetime.fromtimestamp(r['sold_at'], timezone.utc).isoformat())), c.e(str(r['paid_on'] or '—')), money_or(r['gross_minor']),
             money_or(r['basis_minor']), (f"%{r['rate']}" if r['rate'] is not None else '—'), money_or(r['commission_minor']),
             c.e(r['commission_window'] or '—'),
             c.e(PAYMENT_STATUS.get(r['payment_status'], r['payment_status'])+(' · iade incelemesi' if r['held'] else '')+
                 (' / '+COMMISSION_STATUS[r['commission_status']] if r['commission_status'] else '')),
             c.e(SUB_STATUS.get(r['subscription_status'] or '', '—')),
             f'<a href="/odeme/{c.e(r["ref"])}">{c.e(r["ref"])}</a>'] for r in rows]
    out += c.card('Satışlar, tahsilatlar ve komisyonlar' if actor.founder else 'Satışlarım ve komisyonlarım',
                  sales_table(c,'Henüz ödeme bağlantısı yok.',('Müşteri','Ürün','Paket','Satış tarihi','Ödeme tarihi','Tahsilat (KDV dahil)',
                              'Komisyon matrahı (KDV hariç)','Oran','Komisyon','Komisyon ödeme aralığı','Ödeme / komisyon','Abonelik','Bağlantı'),body))
    return out


def content(c, db, identity, tab):
    actor, csrf, info = identity
    data = sales.snapshot(db, actor)
    path = '/satis?tab='+tab if actor.founder else '/sales?tab='+tab
    f = lambda action, fields, button='Kaydet': form(c,path,csrf,action,fields,button)
    inp = lambda name,label,kind='text',required=True: input_field(c,name,label,kind,required)
    options = lambda name,label,items: select(c,name,label,items)
    status = lambda value: badge(c,value)
    out = overview(c, data)
    if actor.founder:
        out += founder_tabs(c, tab)
    sections = (tab,)
    for section in sections:
        if section == 'overview':
            out += c.card('Satış akışı','<p>Müşterinizi kaydedin, fırsat açın ve teklif taslağını CONTROL değerlendirmesine gönderin. Resmi fiyat ve indirim yalnız CONTROL onayıyla oluşur.</p><p>Tahsilat doğrulaması ve hak ediş onayı ayrı adımlardır.</p>')
        if section == 'accounts':
            companies = db.execute('SELECT id,name FROM companies ORDER BY name').fetchall()
            fields = options('company_id','Firma',[(r['id'],r['name']) for r in companies])+inp('name','Ad soyad')+inp('email','E-posta','email')+inp('password','Başlangıç parolası (12–128 karakter)','password')
            out += c.card('Temsilci hesabı oluştur', '<p>Hesap pasif oluşturulur. Etkinleştirme ayrı CONTROL kararıdır; Akademi tamamlanması yetki vermez.</p>'+f('account_create',fields))
            out += c.card('Temsilciler',sales_table(c,'Henüz satış temsilcisi oluşturulmadı.',('Ad','E-posta','Firma','Durum','Yetkilendirme'),[
                [c.e(a['name']),c.e(a['email']),str(a['company_id']),'Aktif' if a['active'] else 'Pasif',
                 f('account_active',c.hidden('id',a['id'])+c.hidden('active','0' if a['active'] else '1')+inp('reason','Yetki kararı gerekçesi'),'Pasifleştir' if a['active'] else 'Etkinleştir')]
                for a in data['accounts']]))
        if section == 'claims':
            out += c.card('Mükerrer müşteri ve sahiplik talepleri',sales_table(c,'İncelenecek müşteri sahipliği talebi bulunmuyor.',('Talep','Müşteri','Talep eden','Durum','Karar'),[
                [record_code(c,r['id'],'TALEP'),record_code(c,r['customer_id'],'MST'),record_code(c,r['owner_id'],'TEM'),status(r['status']),
                 f('resolve_claim',c.hidden('id',r['id'])+options('decision','Sahiplik kararı',[('rejected','Mevcut sahipte tut'),('accepted','Talep edene devret')])+inp('reason','Gerekçe')) if r['status']=='pending' else c.e(r['reason'])]
                for r in data['claims']]))
        if section == 'customers':
            if not actor.founder:
                out += c.card('Müşteri kaydet',f('customer',inp('name','Firma adı')+inp('tax_id','Vergi numarası','text',False)+inp('domain','Firma alan adı','text',False)))
                out += c.card('Sahiplik başvurularım',sales_table(c,'Henüz müşteri sahipliği başvurunuz yok.',('Başvuru','Durum','CONTROL açıklaması'),[[c.e(r['requested_name']),status(r['status']),c.e(r['reason'])] for r in data['claims']]))
            out += c.card('Müşteriler',sales_table(c,'Henüz müşteri kaydı bulunmuyor. Yeni müşteri kaydıyla başlayabilirsiniz.',('Firma','Kayıt','Sahip','Sınıf','İşlem'),[
                [c.e(r['name']),record_code(c,r['id'],'MST'),record_code(c,r['owner_id'],'TEM'),status(r['classification']),
                 f('classify',c.hidden('id',r['id'])+options('classification','Müşteri sınıfı',[('new','Yeni müşteri'),('existing','Mevcut müşteri')])+inp('reason','Doğrulama gerekçesi')) if actor.founder else '']
                for r in data['customers']]))
        if section == 'opportunities':
            out += c.card('Fırsat oluştur',guided_form(c, bool(data['customers']) and not actor.founder,
                f('opportunity',options('customer_id','Müşteri',[(r['id'],r['name']) for r in data['customers']])+inp('title','İhtiyaç / fırsat başlığı')+options('kind','Satış türü',[(k,LABELS[k]) for k in ('subscription','upgrade','addon','project')])),
                'CONTROL bu adımda kayıt oluşturmaz. Temsilci kendi müşterisi üzerinden fırsat açmalıdır.' if actor.founder
                else 'Henüz müşteriniz yok. Önce müşteri kaydı oluşturun.',
                '/satis?tab=accounts' if actor.founder else '/sales?tab=customers',
                'Temsilcilere git' if actor.founder else 'Müşteri kaydet'))
            out += c.card('Fırsatlar',sales_table(c,'Henüz satış fırsatı oluşturulmadı.',('Başlık','Kayıt','Müşteri','Tür'),[[c.e(r['title']),record_code(c,r['id'],'FIR'),record_code(c,r['customer_id'],'MST'),status(r['kind'])] for r in data['opportunities']]))
        if section == 'tasks':
            out += c.card('Görüşme notu / görev',guided_form(c, bool(data['opportunities']) and not actor.founder,
                f('activity',options('opportunity_id','Fırsat',[(r['id'],r['title']) for r in data['opportunities']])+inp('note','İhtiyaç, görüşme notu veya görev')+inp('due_date','Takip tarihi','date',False)),
                'Görüşme ve takip görevlerini satış temsilcisi kendi fırsatına ekler. CONTROL kayıtları izler.' if actor.founder
                else 'Henüz fırsat kaydı bulunmuyor. Görüşme veya takip görevi için önce bir fırsat oluşturun.',
                '/satis?tab=opportunities' if actor.founder else '/sales?tab=opportunities',
                'Satış fırsatlarına git'))
            out += c.card('Görevler ve görüşmeler',sales_table(c,'Henüz görüşme notu veya takip görevi bulunmuyor.',('Fırsat','Not','Takip'),[[record_code(c,r['opportunity_id'],'FIR'),c.e(r['note']),c.e(local_date(r['due_date']))] for r in data['activities']]))
        if section == 'proposals':
            out += c.card('Teklif taslağı / revizyon sürümü', '<p>Tutarları KDV hariç TL olarak girin (örnek: 1.234,56). Bu kayıt resmi teklif değildir.</p>'+guided_form(c, bool(data['opportunities']) and not actor.founder,
                f('proposal',options('opportunity_id','Fırsat',[(r['id'],r['title']) for r in data['opportunities']])+inp('requested_minor','Talep edilen tutar (kuruş)','number')+inp('brief','Kapsam ve ihtiyaç açıklaması')),
                'Teklif taslağını satış temsilcisi oluşturur. CONTROL yalnız değerlendirme, revizyon ve resmi fiyat onayı verir.' if actor.founder
                else 'Henüz teklif hazırlanabilecek bir satış fırsatı yok. Önce müşteri ve fırsat kaydını tamamlayın.',
                '/satis?tab=opportunities' if actor.founder else '/sales?tab=opportunities',
                'Satış fırsatlarına git'))
            proposals = []
            for r in data['proposals']:
                action = ''
                if r['status']=='draft':
                    action = f('submit',c.hidden('id',r['id']),'CONTROL değerlendirmesine gönder')
                if r['status']=='submitted' and actor.founder:
                    action = f('decide',c.hidden('id',r['id'])+options('decision','Karar',[('approved','Onayla'),('revision','Revizyon iste'),('rejected','Reddet')])+inp('reason','Karar gerekçesi')+inp('official_minor','Resmi liste tutarı (kuruş)','number',False)+inp('discount_minor','CONTROL indirimi (kuruş)','number',False),'Kararı kaydet')
                if r['status']=='approved':
                    action = f'<a href="/sales/official?id={r["id"]}">Onaylı teklifi görüntüle</a>'
                proposals.append([record_code(c,r['id'],'TEK'),str(r['version']),c.e(r['brief']),status(r['status']),money(r['requested_minor']),money(None if r['official_minor'] is None else r['official_minor']-r['discount_minor']),action])
            out += c.card('Teklif değerlendirme',sales_table(c,'Henüz teklif talebi bulunmuyor.',('Kayıt','Sürüm','Kapsam','Durum','Talep','Resmi net','İşlem'),proposals))
            out += c.card('Yönetim karar geçmişi',sales_table(c,'Henüz yönetim kararı kaydedilmedi.',('Teklif','Karar','Gerekçe','Karar sahibi','Tarih'),[[record_code(c,r['proposal_id'],'TEK'),status(r['decision']),c.e(r['reason']),record_code(c,r['actor_id'],'YNT'),c.e(local_date(r['stamp']))] for r in data['decisions']]))
        if section == 'finance':
            if actor.founder:
                approved = [(r['id'],r['id']) for r in data['proposals'] if r['status']=='approved']
                out += c.card('Tahsilat doğrulama', '<p>KDV hariç tam dönem tutarı girin. Sağlayıcı maliyeti ayrı raporlanır. Gerçek ödeme veya e-posta işlemi yapılmaz.</p>'+guided_form(c, bool(approved),
                    f('collection',options('proposal_id','Onaylı teklif',approved)+inp('period','Abonelik dönemi (1, 2, …)','number')+inp('reference','Benzersiz tahsilat referansı')+inp('net_minor','Tahsil edilmiş net tutar (kuruş)','number')+inp('cost_minor','Sağlayıcı maliyeti (kuruş)','number')+inp('collected_on','Tahsilat tarihi','date')+inp('reason','Doğrulama dayanağı')),
                    'Henüz CONTROL tarafından onaylanmış teklif yok. Tahsilat doğrulamak için önce teklif kararını tamamlayın.',
                    '/satis?tab=proposals','Teklif kararlarına git'))
                project_opportunities = {r['id'] for r in data['opportunities'] if r['kind']=='project'}
                project_proposals = {r['id'] for r in data['proposals'] if r['opportunity_id'] in project_opportunities}
                eligible_projects = [(r['id'],r['id']) for r in data['collections']
                    if r['proposal_id'] in project_proposals and not r['held']
                    and not any(m['collection_id']==r['id'] for m in data['commissions'])]
                out += c.card('Özel proje hak ediş kararı',guided_form(c, bool(eligible_projects),
                    f('project_commission',options('collection_id','Tahsilat',eligible_projects)+inp('amount_minor','Hak ediş tutarı (kuruş)','number')+inp('analysis','Maliyet / kâr analizi')+inp('reason','Açık yazılı komisyon kararı')),
                    'Hak ediş kararı bekleyen bir özel proje tahsilatı bulunmuyor. Yalnızca doğrulanmış, aktif proje tahsilatları burada seçilebilir.'))
            out += c.card('Doğrulanmış tahsilatlar',sales_table(c,'Henüz doğrulanmış tahsilat bulunmuyor.',('Kayıt','Dönem','Net tahsilat','Tarih','İnceleme','İşlem'),[
                [record_code(c,r['id'],'TAH'),str(r['period']),money(r['net_minor']),c.e(local_date(r['collected_on'])),'İade/iptal bekletiliyor' if r['held'] else 'Doğrulandı',
                 f('hold',c.hidden('collection_id',r['id'])+inp('reason','İade/iptal gerekçesi'),'İncelemeye al') if actor.founder and not r['held'] else ''] for r in data['collections']]))
            commissions = []
            for r in data['commissions']:
                action = ''
                if actor.founder and r['status']!='reversed':
                    decision = 'approved' if r['status']=='pending' else 'paid'
                    if r['held']:
                        action = f('reverse',c.hidden('id',r['id'])+inp('reason','Ters kayıt için kurucu kararı'),'Tam ters kayıt oluştur')
                    elif r['status']!='paid':
                        action = f('commission_action',c.hidden('id',r['id'])+c.hidden('decision',decision)+inp('reason','Karar gerekçesi'),'Hak edişi onayla' if decision=='approved' else 'Ödendi olarak işaretle')
                commissions.append([record_code(c,r['id'],'KOM'),money(r['amount_minor']),str(r['rate'])+'%',status(r['status']),c.e(local_date(r['payable_from'])+' – '+local_date(r['payable_until'])),action])
            out += c.card('Komisyonlar',sales_table(c,'Henüz tahsilata bağlı komisyon hak edişi oluşmadı.',('Kayıt','Hak ediş','Oran','Durum','Ödeme takvimi','CONTROL'),commissions))
            out += c.card('Düzeltme kayıtları',sales_table(c,'Henüz komisyon düzeltme kaydı bulunmuyor.',('Hak ediş','Düzeltme (kuruş)','Gerekçe','Tarih'),[[record_code(c,r['commission_id'],'KOM'),money(r['amount_minor']),c.e(r['reason']),c.e(local_date(r['stamp']))] for r in data['adjustments']]))
            if actor.founder:
                out += c.card('Kârlılık özeti',f'<p>Net tahsilat: {money(sum(r["net_minor"] for r in data["collections"]))} · Sağlayıcı maliyeti: {money(sum(r["cost_minor"] for r in data["collections"]))}</p><p>İade/iptal incelemesindeki kayıtlar ayrıca değerlendirilmelidir. Sağlayıcı maliyeti temsilci matrahından düşülmez.</p>')
        if section == 'audit':
            out += c.card('Silme ve güncellemeye kapalı satış denetimi',sales_table(c,'Henüz görüntülenecek satış denetim kaydı bulunmuyor.',('Tarih','İşlemi yapan','İşlem','Hedef','Açıklama'),[[c.e(local_date(r['stamp'])),record_code(c,r['actor_id'],'YNT'),c.e(r['event']),record_code(c,r['target'],'KYT'),c.e(r['detail'])] for r in data['audit']]))
        if section == 'payments':
            out += payments_section(c, db, actor, data, f, inp, options)
        if section == 'catalog':
            hr_rows = [
                [plan.name, str(plan.monthly_usd).replace('.', ','), str(plan.capacity),
                 str(plan.positions), str(plan.hr_users)]
                for plan in HR_FLOW
            ]
            education_rows = [
                [plan.name, str(plan.monthly_usd), str(plan.capacity)] for plan in EDUCATION
            ]
            out += c.card('ERNASA HR FLOW', '<p>ATS ve AI mülakat tek ürün; bir aday süreci bir işlem hakkıdır.</p>'+c.table(('Paket','USD / ay','Aylık aday','Aktif pozisyon','İK hesabı'),hr_rows))
            out += c.card('ERNASA Eğitim Platformu', '<p>Ayrı abonelik. Eğitim atama, ilerleme takibi, bilgi ölçme ve izinli sesli/görüntülü sunum. AI eğitim raporu geliştirme ve kabul testi sonrasında sunulur.</p>'+c.table(('Paket','USD / ay','Aylık eğitim katılımcısı'),education_rows))
            out += c.card('Kuruma özel eğitim içeriği', c.table(('Hizmet','Konu başına (TL)'),[['Hazır dokümandan eğitim','750'],['Özgün eğitim ve sınav','1.750'],['İleri teknik / mesleki eğitim','3.500'],['Özel video / animasyon','Özel teklif']]))
            out += c.card('Fiyat koşulları','<p>Fiyatlar KDV hariçtir. Türkiye sözleşme, fatura ve tahsilatları TL ile yapılır. Sabit kur kampanyası yoktur. Güncel kur ve TL tutarı resmi teklif aşamasında CONTROL tarafından doğrulanır. Liste fiyatı resmi teklif değildir; indirim ve teklif onayı CONTROL yetkisindedir.</p>')
        if section == 'resources':
            out += c.card('Yetki ve komisyon rehberi','<p>Yeni müşterinin ilk ürün aboneliği: ilk tahsil edilmiş dönem %25; aynı aboneliğin devamı %5. Paket yükseltme ve mevcut müşteriye ek ürün %0. Özel projede maliyet/kâr analizi ve açık CONTROL kararı gerekir.</p><p>Resmi fiyat, indirim ve taahhüt için onaylı teklif kullanın. Tahsilat yoksa hak ediş yoktur. Ödeme takvimi sonraki ayın 1–15 günleridir ve CONTROL onayına bağlıdır.</p>')
        if section == 'profile':
            out += c.card('Profil',f'<p>{c.e(info["name"])} · {c.e(info["email"])}</p><p>Firma: {actor.company_id}</p>')
            out += c.card('Parolayı değiştir',f('password',inp('old_password','Mevcut parola','password')+inp('password','Yeni parola (12–128 karakter)','password'),'Parolayı değiştir ve oturumları kapat'))
    return '<div class="sales-hub-ui">'+out+'</div>'


def handle(h, path, post):
    from . import app as c
    if path != '/satis' and path != '/sales' and not path.startswith('/sales/'):
        return False
    identity = None
    api = path.startswith('/sales/api')
    try:
        with closing(c.connect(h.server.db_path)) as db, db:
            if post:
                if h.headers.get_content_type() == 'application/json':
                    if h.headers.get('Origin') != h.server.origin:
                        sales.fail('İstek kaynağı geçersiz.', 403)
                    size = int(h.headers.get('Content-Length', '0'))
                    if not 1 <= size <= 16384:
                        sales.fail('İstek boyutu sınır dışında.', 413)
                    data = json.loads(h.rfile.read(size))
                    if not isinstance(data, dict):
                        sales.fail('JSON nesnesi gerekli.', 422)
                else:
                    data = h.data()
                db.execute('BEGIN IMMEDIATE')
            identity = current(c,h,db)
            if path == '/sales/login':
                if post:
                    token = login(c,h,db,data)
                    db.commit()
                    h.reply(303,headers=(('Location',h.server.public_prefix+'/sales'),set_cookie(h,'sales_session',token,c.TTL)))
                elif identity and identity[0].founder:
                    h.reply(303,headers=(('Location',h.server.public_prefix+'/satis'),))
                else:
                    token = secrets.token_urlsafe(32)
                    render(c,h,'Satış temsilcisi girişi',sales_login_form(c,token),
                           headers=(set_cookie(h,'sales_login_csrf',token,c.LOGIN_CSRF_TTL),))
                return True
            if not identity:
                if path == '/satis' and not post:
                    h.reply(303,headers=(('Location',h.server.public_prefix+'/giris'),))
                    return True
                if api or post:
                    sales.fail('Oturum açmanız gerekiyor.',401)
                h.reply(303,headers=(('Location',h.server.public_prefix+'/sales/login'),))
                return True
            actor,csrf,info = identity
            if path == '/satis':
                sales.admin(actor)
            if post:
                provided = h.headers.get('X-CSRF-Token', '') if h.headers.get_content_type()=='application/json' else data.get('csrf','')
                if not isinstance(provided,str) or not hmac.compare_digest(csrf.encode(),provided.encode()):
                    sales.fail('Form doğrulanamadı.',403)
                if path == '/sales/logout':
                    cookie = SimpleCookie(h.headers.get('Cookie','')).get('sales_session')
                    if cookie:
                        db.execute('DELETE FROM sales_sessions WHERE token=?',(c.token_hash(cookie.value),))
                    sales.audit(db,actor,'session_ended',actor.id)
                    db.commit()
                    h.reply(303,headers=(('Location',h.server.public_prefix+'/sales/login'),set_cookie(h,'sales_session','',0)))
                    return True
                if path not in ('/sales','/satis','/sales/api'):
                    sales.fail('İşlem bulunamadı.',404)
                result = commands(c,db,actor,data)
                db.commit()
                if api:
                    h.reply(200,json.dumps(result).encode(),'application/json')
                else:
                    tab = parse_qs(urlsplit(h.path).query).get('tab',['accounts' if actor.founder else 'overview'])[0]
                    location = '/satis?tab='+tab if actor.founder else '/sales?tab='+tab
                    h.reply(303,headers=(('Location',h.server.public_prefix+location),))
                return True
            query = parse_qs(urlsplit(h.path).query,max_num_fields=8)
            if path == '/sales/api':
                payload = sales.snapshot(db,actor)
                payload['csrf'] = csrf
                h.reply(200,json.dumps(payload).encode(),'application/json')
            elif path in ('/sales/api/record','/sales/official'):
                table = query.get('table',['proposals'])[0]
                if table not in sales.TABLES:
                    sales.fail('Kayıt türü bulunamadı.',404)
                if path == '/sales/official':
                    table = 'proposals'
                row = sales.owned(db,actor,table,query.get('id',[''])[0])
                if path == '/sales/official':
                    if row['status']!='approved':
                        sales.fail('Resmi teklif için CONTROL onayı gerekli.',409)
                    body = c.card('CONTROL onaylı teklif',f'<p>Teklif: {record_code(c,row["id"],"TEK")} · Sürüm {row["version"]}</p><p>{c.e(row["brief"])}</p><p>Liste: {money(row["official_minor"])} · İndirim: {money(row["discount_minor"])} · KDV hariç net: <strong>{money(row["official_minor"]-row["discount_minor"])}</strong></p>')
                    render(c,h,'Onaylı teklif',body,identity)
                else:
                    h.reply(200,json.dumps(dict(row)).encode(),'application/json')
            elif path in ('/sales','/satis'):
                default_tab = 'accounts' if actor.founder else 'overview'
                tab = query.get('tab',[default_tab])[0]
                allowed_tabs = tuple(key for key,_ in FOUNDER_TABS) if actor.founder else ('overview','customers','opportunities','proposals','tasks','finance','payments','catalog','resources','profile')
                if tab not in allowed_tabs:
                    sales.fail('Ekran bulunamadı.',404)
                render(c,h,'Satış Yönetimi' if actor.founder else 'ERNASA Sales Hub',content(c,db,identity,tab),identity,tab=tab)
            else:
                sales.fail('Ekran bulunamadı.',404)
    except (sales.Invalid,c.Problem) as error:
        if api:
            h.reply(error.status,json.dumps({'detail':str(error)}).encode(),'application/json')
        elif path == '/sales/login' and post:
            # Reissue a fresh CSRF pair; a rejected login must never strand the user.
            token = secrets.token_urlsafe(32)
            render(c,h,'Satış temsilcisi girişi',sales_login_form(c,token,error),
                   status=error.status,headers=(set_cookie(h,'sales_login_csrf',token,c.LOGIN_CSRF_TTL),))
        else:
            render(c,h,'İşlem tamamlanamadı',f'<p role="alert">{c.e(str(error))}</p><p><a href="/sales">Satış paneline dön</a></p>',identity,status=error.status)
    except (ValueError,TypeError,UnicodeError):
        h.reply(422,json.dumps({'detail':'İstek okunamadı.'}).encode(),'application/json')
    except (c.sqlite3.IntegrityError,c.psycopg2.IntegrityError):
        h.reply(409,json.dumps({'detail':'Mükerrer kayıt veya veri koşulu çakışması.'}).encode(),'application/json')
    except (c.sqlite3.OperationalError,c.psycopg2.OperationalError,c.psycopg2.ProgrammingError):
        h.reply(503,json.dumps({'detail':'Satış veritabanı hazır değil. Eklemeli migration ve bağlantıyı kontrol edin.'}).encode(),'application/json')
    return True
