"""CONTROL-integrated Sales Hub HTTP adapter. No public signup or outbound actions."""
from contextlib import closing
from . import login_throttle
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
    if action in ('payment_request', 'link_company', 'verify_order', 'end_test', 'bank_settings', 'confirm_bank',
                  'send_payment_email'):
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
        from . import billing_share
        ref, reused = billing_share.ensure_link(db, actor, proposal_id=proposal, company_id=company, product=product,
                                                plan_name=sales.text(data, 'plan_name', 20), period_start=start, now=now)
        return {'ref': ref, 'url': c_public_url(ref), 'reused': reused,
                'detail': 'Bu dönem için mevcut ödeme bağlantısı kullanıldı.' if reused else 'Ödeme bağlantısı oluşturuldu.'}
    if action == 'send_payment_email':
        from . import billing_share
        return billing_share.send_email(db, actor, sales.text(data, 'ref', 32), now)
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
    if action == 'bank_settings':
        bp.save_bank_settings(db, actor=actor, bank_name=sales.text(data, 'bank_name', 120),
                              account_holder=sales.text(data, 'account_holder', 180), iban=sales.text(data, 'iban', 40),
                              enabled=data.get('enabled') == '1', now=now)
        sales.audit(db, actor, 'bank_settings_updated', 'bank', 'enabled=' + str(data.get('enabled') == '1'))
        return {'detail': 'Banka hesabı kaydedildi. Yeni banka ödeme talimatları bu IBAN ile oluşur; '
                          'önceden verilmiş talimatlar değişmez.'}
    if action == 'confirm_bank':
        checkout_id = sales.text(data, 'checkout_id', 64)
        gross = tl_to_minor(sales.text(data, 'gross_tl', 24))
        try:
            result = bp.confirm_bank_payment(db, actor=actor, checkout_id=checkout_id,
                                             transaction_id=sales.text(data, 'transaction_id', 120),
                                             gross_minor=gross, now=now)
        except bp.PaymentRejected as exc:
            sales.fail(f'Banka tahsilatı onaylanmadı: {exc}', 409)
        sales.audit(db, actor, 'bank_payment_confirmed', checkout_id, result)
        return {'detail': 'Banka tahsilatı onaylandı, abonelik etkinleştirildi.' if result == 'activated'
                else 'Bu banka işlemi daha önce işlenmiş.'}
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


def current(c, h, db, area='sales'):
    """CONTROL (/satis…) and Sales Hub (/sales…) are separate areas: each recognises only its
    own session, so a CONTROL login in the same browser never takes over the representative
    screen and a representative session never reaches CONTROL."""
    if area == 'control':
        founder = h.session(db)
        if founder and founder['role'] == 'founder':
            return sales.Actor(str(founder['id']), None, True), founder['csrf'], dict(founder)
        return None
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
    address = c.email(data)
    throttle = login_throttle.for_server(h.server, 'sales')
    wait = throttle.check(address)
    if wait:
        sales.fail(login_throttle.wait_message(wait), 429)
    row = db.execute('SELECT a.* FROM sales_accounts a JOIN companies c ON c.id=a.company_id WHERE a.email=? AND a.active=1 AND c.status=?', (address, 'Aktif')).fetchone()
    valid = c.check_ik_password(str(data.get('password', '')), row['secret'] if row else None)
    if not row or not valid:
        throttle.failure(address)
        sales.fail('Giriş bilgileri geçersiz.', 401)
    throttle.success(address)
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


def payments_section(c, db, actor, data, f, inp, options, ref=None):
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
          out += c.card('Canlı ödeme testi (3,00 TL, KDV dahil)','<p>Yalnız kurucu görür. Eğitim · 1 katılımcı · sabit 3,00 TL (KDV dahil: 2,50 TL + 0,50 TL KDV). Temsilcinin onaylı teklifi seçilirse tahsilat ve komisyon hesabı da test edilir; test komisyonu yalnız gösterilir, gerçek komisyon olarak ödenmez.</p>'+
                      f('payment_request', '<input type="hidden" name="product" value="education"><input type="hidden" name="plan_name" value="Ödeme Testi">'+
                        options('company_id','Firma (temsilci teklifi seçilmezse)',[(r['id'],r['name']) for r in companies])+
                        options('proposal_id','Temsilci teklifi (isteğe bağlı)',[('direct','— doğrudan satış (komisyonsuz) —')]+approved_all)+
                        inp('period_start','Dönem başlangıcı','date'),'Test bağlantısı oluştur')+
                      '<p>Test bitince test aboneliklerini kapatın; ardından test paketi Render ayarından kapatılır.</p>'+
                      f('end_test','','Test aboneliklerini kapat'))
        out += bank_section(c, db, f, inp)
        out += c.card('Shopier siparişini elle doğrula','<p>Webhook ulaşmadıysa Shopier sipariş numarasıyla aynı sunucu kontrolleri çalıştırılır.</p>'+
                      f('verify_order', inp('order_id','Shopier sipariş numarası'),'Doğrula'))
    else:
        approved = [(r['id'], f"{r['brief'][:60]} · sürüm {r['version']}") for r in data['proposals'] if r['status']=='approved']
        out += c.card('Müşteriye özel ödeme bağlantısı','<p>Yalnız CONTROL onaylı teklifleriniz için. Tutar, müşteri ödemeye başladığında güncel TCMB kuruyla hesaplanır ve 15 dakika sabitlenir.</p>'+
                      guided_form(c, bool(approved), f('payment_request', options('proposal_id','Onaylı teklif',approved)+common,'Bağlantı oluştur'),
                                  'Önce CONTROL onaylı bir teklif gerekir.', '/sales?tab=proposals', 'Tekliflerime git'))
    out += tracking_section(c, db, actor, f, inp, options, ref)
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


def commission_cards(c, db, actor):
    """USD commission with the TRY entitlement fixed at the sale's TCMB rate (stored values only)."""
    from . import commission_report as cr
    items = cr.rows(db, actor)
    heads = ['Müşteri','Satılan paket','Satış tarihi','Tahsilat tarihi','Komisyon matrahı (USD)','Komisyon oranı',
             'Kazanılan komisyon (USD)','Satışta uygulanan sabit kur','Kur tarihi','Kesinleşmiş TL hak edişi',
             'Komisyon ödeme dönemi','Ödeme durumu','Gerçekleşen ödeme tarihi']
    if actor.founder:
        heads.insert(0, 'Temsilci')
    body = []
    for r in items:
        package = ' '.join(x for x in ({'hr_flow':'HR FLOW','education':'Eğitim'}.get(r['product'] or '', ''), r['plan_name'] or '') if x) or c.e(r['opportunity'])
        row = [c.e(r['customer']), c.e(package), c.e(cr.tr_date(r['sold_at'] if r['sold_at'] is not None else r['collected_on'])),
               c.e(cr.tr_date(r['collected_on'])),
               cr.usd(r['basis_usd']) + (f"<br><small>{c.e(r['from_plan'])} → {c.e(r['to_plan'])} farkı "
                                         f"({cr.usd(r['new_basis_usd'])} − {cr.usd(r['old_basis_usd'])})</small>" if r['from_plan'] else ''),
               f"%{r['rate']}", cr.usd(r['commission_usd']),
               cr.fx(r['fx_rate']), c.e(cr.tr_date(r['fx_date'])), '<strong>'+cr.try_text(r['amount_minor'])+'</strong>',
               c.e(r['window_text']), c.e(r['status_text']), c.e(cr.tr_date(r['paid_on']))]
        if actor.founder:
            row.insert(0, c.e(r['rep_name'] or r['owner_id']))
        body.append(row)
    out = c.card('Komisyon hak edişleri (USD → sabit TL)' if actor.founder else 'Komisyon hak edişlerim (USD → sabit TL)',
                 f'<p><strong>{c.e(cr.EXPLANATION)}</strong></p><p>{c.e(cr.POLICY_SHORT)}</p>'
                 + sales_table(c, 'Henüz doğrulanmış tahsilata bağlı komisyon hak edişi yok.', tuple(heads), body))
    if not actor.founder:
        return out
    reps = cr.by_rep(items)
    total_open = sum(s['open'] for s in reps)
    total_paid = sum(s['paid'] for s in reps)
    out += c.card('Temsilci bazında komisyon özeti',
        f'<p>Toplam bekleyen komisyon (kesinleşmiş TL): <strong>{cr.try_text(total_open)}</strong> · '
        f'Ödenmiş komisyonlar: <strong>{cr.try_text(total_paid)}</strong>. '
        'Kesinleşmiş TL borçları güncel kurla yeniden hesaplanmaz.</p>'
        + sales_table(c, 'Henüz temsilci komisyonu yok.',
                      ('Temsilci','Hak ediş sayısı','USD komisyon kazancı','Kesinleşmiş TL toplamı','Bekleyen TL borç','Ödenen TL'),
                      [[c.e(s['rep']), str(s['count']), cr.usd(s['usd']), cr.try_text(s['fixed']), cr.try_text(s['open']), cr.try_text(s['paid'])]
                       for s in reps]))
    out += c.card('Aylık komisyon ödeme dönemleri', sales_table(c, 'Henüz ödeme dönemi oluşmadı.',
        ('Ödeme dönemi','Hak ediş sayısı','Kesinleşmiş TL','Bekleyen','Ödenen'),
        [[f'{w[0]:%d.%m.%Y} – {w[1]:%d.%m.%Y}', str(s['count']), cr.try_text(s['fixed']), cr.try_text(s['open']), cr.try_text(s['paid'])]
         for w, s in cr.by_window(items)]))
    paid = [r for r in items if r['status'] == 'paid']
    out += c.card('Komisyon ödeme geçmişi', sales_table(c, 'Henüz ödenmiş komisyon yok.',
        ('Ödeme tarihi','Temsilci','Müşteri','Kesinleşmiş TL','Sabit kur','Kur tarihi','Ödeme dönemi'),
        [[c.e(cr.tr_date(r['paid_on'])), c.e(r['rep_name'] or r['owner_id']), c.e(r['customer']), cr.try_text(r['amount_minor']),
          cr.fx(r['fx_rate']), c.e(cr.tr_date(r['fx_date'])), c.e(r['window_text'])] for r in paid]))
    return out


def stage_badge(c, code):
    from .billing_share import STAGES
    label, css = STAGES[code]
    return f'<span class="status status-{css}">{c.e(label)}</span>'


def tracking_section(c, db, actor, f, inp, options, ref=None):
    """Teklif ve ödeme takibi: create / view / copy / WhatsApp / e-posta / durum for every own
    CONTROL-approved proposal (rep) or every link (founder). Existing components only."""
    from . import billing_share as share
    from .billing_quotes import PLANS
    base = '/satis' if actor.founder else '/sales'
    proposals, links = share.tracking(db, actor)
    plans = [(p.name, p.name) for p in PLANS['hr_flow']]
    products = [('hr_flow','ERNASA HR FLOW'),('education','ERNASA Eğitim Platformu')]
    rows = []
    for p in proposals:
        if p['has_link']:
            continue
        if p['stage'] == 'proposal_submitted':
            action = '—'
        elif not p['control_company']:
            action = '<p class="sales-empty" role="status">CONTROL firma eşleşmesi bekleniyor.</p>'
        else:
            action = f('payment_request', c.hidden('proposal_id', p['id'])+options('product','Ürün',products)
                       +options('plan_name','Paket',plans)+inp('period_start','Dönem başlangıcı','date'),
                       'Ödeme bağlantısı oluştur')
        rows.append([c.e(p['customer']), record_code(c,p['id'],'TEK')+'<br>'+c.e(p['brief'][:60]),
                     stage_badge(c, p['stage']), '—', action])
    for l in links:
        url = c_public_url(l['ref'])
        info, person, open_ = l['info'], l['contact'], l['stage'] in ('ready','awaiting','bank_review')
        end = share.period_end(l['period_start'])
        status_html = stage_badge(c, l['stage'])
        if l['stage'] in ('active','paid') and info['gross_minor'] is not None:
            status_html += f'<br><small>{money(int(info["gross_minor"]))}</small>'
        commission = '—'
        if info['commission_minor'] is not None:
            commission = (f'<span class="status status-approved">Komisyon hesaplandı</span><br>{money(int(info["commission_minor"]))}'
                          f'<br><small>Ödeme tarihi: {c.e(info["commission_window"])}</small>')
        if person:
            who = c.e(person['name'])+'<br><small>'+c.e(' · '.join(x for x in (share.mask_phone(person['phone']), share.mask_email(person['email'])) if x))+'</small>'
        else:
            who = '<small>Kayıtlı yetkili yok</small>'
        sent = '<br>'.join(
            f'<small>{c.e({"email":"E-posta","whatsapp":"WhatsApp"}[e["channel"]])} · '
            f'{c.e({"sent":"gönderildi","failed":"başarısız","opened":"açıldı"}[e["status"]])} · '
            f'{c.e(local_date(datetime.fromtimestamp(e["created_at"], timezone.utc).isoformat()))}'
            + (f' · {c.e(e["error"])}' if e['error'] else '') + '</small>' for e in l['events']) or '<small>Henüz gönderilmedi</small>'
        field = 'odeme-link-' + l['ref']
        actions = (f'<label>Ödeme bağlantısı<input id="{field}" type="url" readonly value="{c.e(url)}"></label>'
                   f'<button type="button" data-copy-target="{field}">Bağlantıyı kopyala</button>'
                   f'<p><a class="sales-next-step" href="{c.e(url)}" target="_blank" rel="noopener">Ödeme bağlantısını görüntüle</a></p>')
        if open_:
            if person and share.whatsapp_number(person['phone']):
                actions += (f'<p><a class="sales-next-step" href="{base}/whatsapp?ref={c.e(l["ref"])}" target="_blank" '
                            f'rel="noopener">WhatsApp ile gönder</a></p>')
            if person and person['email']:
                actions += f('send_payment_email', c.hidden('ref', l['ref']), 'E-posta ile gönder')
        actions += f'<p><a class="sales-next-step" href="{base}?tab=payments&amp;ref={c.e(l["ref"])}#odeme-durumu">Ödeme durumunu sorgula</a></p>'
        name = c.e(l['customer'] or l['company_name'])
        if l['customer'] and l['customer'] != l['company_name']:
            name += '<br><small>'+c.e(l['company_name'])+'</small>'
        rows.append([name+'<br>'+who,
                     (record_code(c,l['proposal_id'],'TEK')+'<br>' if l['proposal_id'] else '<small>Doğrudan satış</small><br>')
                     + c.e(share.LABELS[l['product']]+' '+l['plan_name'])
                     + '<br><small>'+c.e(local_date(str(l['period_start']))+' – '+local_date(end.isoformat()))+'</small>',
                     status_html + ('<br>'+commission if commission != '—' else ''), sent, actions])
    rows = [['<div>'+cell+'</div>' for cell in row] for row in rows]  # one grid item per cell on mobile
    out = c.card('Teklif ve ödeme takibi',
        '<span id="odeme-takibi"></span><p>CONTROL onaylı her teklif için tek ödeme bağlantısı oluşturulur; aynı dönem için '
        'tekrar basıldığında mevcut bağlantı kullanılır. WhatsApp ve e-posta yalnız firmanın CONTROL’de kayıtlı yetkilisine '
        'gider. Fiyat, IBAN, banka tahsilat onayı ve komisyon oranı CONTROL kurucusundadır.</p>'
        f'<p>{c.e(__import__("control.commission_report", fromlist=["x"]).POLICY_SHORT)}</p>'
        '<p id="odeme-kopya-sonuc" role="status"></p>'
        + sales_table(c, 'Takip edilecek teklif veya ödeme bağlantısı yok.',
                      ('Müşteri ve yetkili','Teklif, paket ve dönem','Durum ve komisyon','Gönderim','Bağlantı ve işlemler'), rows))
    if ref:
        out += status_detail(c, db, actor, ref)
    if actor.founder and share.ready(db):
        hist = db.execute("""SELECT e.*, r.plan_name, co.name AS company_name FROM billing_link_events e
                             JOIN billing_payment_requests r ON r.ref=e.ref JOIN companies co ON co.id=r.company_id
                             ORDER BY e.created_at DESC LIMIT 100""").fetchall()
        out += c.card('Ödeme bağlantısı gönderim geçmişi', sales_table(c, 'Henüz gönderim yapılmadı.',
            ('Tarih','Bağlantı','Firma','Kanal','Alıcı','Sonuç','Gönderen','Hata'),
            [[c.e(local_date(datetime.fromtimestamp(e['created_at'], timezone.utc).isoformat())), c.e(e['ref']),
              c.e(e['company_name']), c.e({'email':'E-posta','whatsapp':'WhatsApp'}[e['channel']]), c.e(e['recipient']),
              c.e({'sent':'Gönderildi','failed':'Başarısız','opened':'WhatsApp açıldı'}[e['status']]),
              record_code(c, e['actor_id'], 'TEM') if not str(e['actor_id']).isdigit() else 'CONTROL',
              c.e(e['error'] or '—')] for e in hist]))
    return out


def status_detail(c, db, actor, ref):
    from . import billing_payments as bp, billing_share as share
    req = share.authorized_request(db, actor, ref)
    code, info = share.stage(db, ref)
    _, checkouts = bp.request_view(db, ref)
    kind = {bp.PROVIDER: 'Kart (Shopier)', bp.BANK_PROVIDER: 'Havale / EFT / FAST'}
    state = {'pending':'Bekliyor','paid':'Ödendi','expired':'Süresi doldu','failed':'Başarısız','cancelled':'İptal','refunded':'İade edildi'}
    body = (f'<span id="odeme-durumu"></span><p>{c.e(req["company_name"])} · {c.e(req["plan_name"])} · Referans <strong>{c.e(ref)}</strong></p>'
            f'<p>{stage_badge(c, code)}</p>'
            + sales_table(c, 'Müşteri henüz ödeme yöntemi seçmedi.', ('Yöntem','Tutar (KDV dahil)','Durum','Başlangıç'),
                          [[c.e(kind.get(x['provider'], x['provider'])), money(int(x['gross_minor'])), c.e(state.get(x['status'], x['status'])),
                            c.e(local_date(datetime.fromtimestamp(x['created_at'], timezone.utc).isoformat()))] for x in checkouts]))
    if share.ready(db):
        body += sales_table(c, 'Gönderim kaydı yok.', ('Tarih','Kanal','Alıcı','Sonuç','Hata'),
            [[c.e(local_date(datetime.fromtimestamp(e['created_at'], timezone.utc).isoformat())),
              c.e({'email':'E-posta','whatsapp':'WhatsApp'}[e['channel']]), c.e(e['recipient']),
              c.e({'sent':'Gönderildi','failed':'Başarısız','opened':'WhatsApp açıldı'}[e['status']]), c.e(e['error'] or '—')]
             for e in share.events(db, ref)])
    return c.card('Ödeme durumu', body)


def bank_section(c, db, f, inp):
    """Founder-only: IBAN settings and Havale/EFT/FAST collections awaiting confirmation."""
    from . import billing_payments as bp
    if not bp.bank_ready(db):
        return c.card('Havale / EFT / FAST','<p role="status">Banka ödeme tabloları henüz kurulmadı (migration 007).</p>')
    cur = bp.bank_settings(db)
    val = lambda k: cur[k] if cur else ''
    enabled = bool(cur and cur['enabled'])
    out = c.card('Banka hesabı ve IBAN',
        '<p>Müşterilere gösterilecek ERNASA tahsilat hesabı. Her banka ödeme talimatı oluşturulduğu andaki '
        'IBAN bilgisini değişmez olarak saklar.</p>'
        + (f'<p>Durum: <strong>{"Açık" if enabled else "Kapalı"}</strong></p>')
        + f('bank_settings',
            input_field(c,'bank_name','Banka adı',value=val('bank_name'),maximum=120)
            + input_field(c,'account_holder','Hesap sahibi (ünvan)',value=val('account_holder'),maximum=180)
            + input_field(c,'iban','IBAN (TR ile başlayan 26 karakter)',value=val('iban'),maximum=40)
            + '<label><input type="checkbox" name="enabled" value="1"' + (' checked' if enabled else '')
            + '> Havale / EFT / FAST ödemesini müşterilere aç</label>', 'Kaydet'))
    rows = bp.pending_bank_payments(db)
    body = [[c.e(r['company_name']), c.e(r['plan_name']), c.e(str(r['period_start'])), money(int(r['gross_minor'])),
             f'<strong>{c.e(r["payment_reference"])}</strong>', c.e(r['owner_id'] or 'Doğrudan satış'),
             c.e(local_date(datetime.fromtimestamp(r['created_at'], timezone.utc).isoformat())),
             f('confirm_bank', c.hidden('checkout_id', r['id'])
               + inp('transaction_id','Banka işlem / dekont no')
               + input_field(c,'gross_tl','Hesaba geçen tutar (TL)',maximum=24), 'Tahsilatı onayla')]
            for r in rows]
    out += c.card('Bekleyen Havale / EFT / FAST ödemeleri',
        '<p>Banka hesabında tutarı ve açıklamadaki referansı kontrol edin. Tutar talimattakiyle kuruşu kuruşuna aynı '
        'olmalıdır; aynı banka işlem numarası ikinci kez kullanılamaz. Onay aboneliği etkinleştirir ve varsa temsilci '
        'komisyonunu Shopier ödemesiyle aynı kurallarla hesaplar.</p>'
        + sales_table(c,'Onay bekleyen banka ödemesi yok.',
                      ('Firma','Paket','Dönem','Tutar (KDV dahil)','Açıklama referansı','Temsilci','Talimat zamanı','Onay'), body))
    return out


def content(c, db, identity, tab, ref=None):
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
            from . import commission_report as cr
            out += c.card('Satış akışı','<p>Müşterinizi kaydedin, fırsat açın ve teklif taslağını CONTROL değerlendirmesine gönderin. Resmi fiyat ve indirim yalnız CONTROL onayıyla oluşur.</p><p>Tahsilat doğrulaması ve hak ediş onayı ayrı adımlardır.</p>'
                          f'<p>{c.e(cr.POLICY_SHORT)}</p>')
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
                    action = (f'<a href="{"/satis" if actor.founder else "/sales"}/official?id={r["id"]}">Onaylı teklifi görüntüle</a>'
                              f'<br><a href="{"/satis" if actor.founder else "/sales"}?tab=payments#odeme-takibi">Ödeme bağlantısı</a>')
                proposals.append([record_code(c,r['id'],'TEK'),str(r['version']),c.e(r['brief']),status(r['status']),money(r['requested_minor']),money(None if r['official_minor'] is None else r['official_minor']-r['discount_minor']),action])
            out += c.card('Teklif değerlendirme',sales_table(c,'Henüz teklif talebi bulunmuyor.',('Kayıt','Sürüm','Kapsam','Durum','Talep','Resmi net','İşlem'),proposals))
            out += c.card('Yönetim karar geçmişi',sales_table(c,'Henüz yönetim kararı kaydedilmedi.',('Teklif','Karar','Gerekçe','Karar sahibi','Tarih'),[[record_code(c,r['proposal_id'],'TEK'),status(r['decision']),c.e(r['reason']),record_code(c,r['actor_id'],'YNT'),c.e(local_date(r['stamp']))] for r in data['decisions']]))
        if section == 'finance':
            out += commission_cards(c, db, actor)
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
                out += c.card('Kârlılık özeti',f'<p>Net tahsilat: {money(sum(r["net_minor"] for r in data["collections"]))} · Sağlayıcı maliyeti: {money(sum(r["cost_minor"] for r in data["collections"]))}</p><p>İade/iptal incelemesindeki kayıtlar ayrıca değerlendirilmelidir. HR FLOW komisyonunda onaylı ana paket bedeli esas alınır; KDV ve ödeme maliyeti farkı hariçtir.</p>')
        if section == 'audit':
            out += c.card('Silme ve güncellemeye kapalı satış denetimi',sales_table(c,'Henüz görüntülenecek satış denetim kaydı bulunmuyor.',('Tarih','İşlemi yapan','İşlem','Hedef','Açıklama'),[[c.e(local_date(r['stamp'])),record_code(c,r['actor_id'],'YNT'),c.e(r['event']),record_code(c,r['target'],'KYT'),c.e(r['detail'])] for r in data['audit']]))
        if section == 'payments':
            out += payments_section(c, db, actor, data, f, inp, options, ref)
        if section == 'catalog':
            from . import commission_report as cr
            prices = cr.price_rows()
            hr_rows = [
                [r['plan'].name, cr.usd(r['plan'].monthly_usd), cr.usd(r['plan'].commission_usd), cr.usd(r['first']), cr.usd(r['later']),
                 cr.usd((r['plan'].monthly_usd * Decimal('1.20')).quantize(Decimal('0.01'))), str(r['plan'].capacity),
                 str(r['plan'].positions), str(r['plan'].hr_users)]
                for r in prices
            ]
            education_rows = [
                [plan.name, cr.usd(plan.monthly_usd), str(plan.capacity)] for plan in EDUCATION
            ]
            out += c.card('ERNASA HR FLOW', '<p>ATS ve AI mülakat tek ürün; bir aday süreci bir işlem hakkıdır. Tablodaki bütün tutarlar <strong>Amerikan doları (USD)</strong> cinsindendir.</p>'+c.table(('Paket','Aylık fiyat + KDV (USD)','Komisyon matrahı (USD)','İlk dönem %25 (USD)','Sonraki dönem %5 (USD)','KDV dahil fiyat (USD)','Aylık aday','Aktif pozisyon','İK hesabı'),hr_rows))
            rate = cr.current_rate()
            if rate is None:
                out += c.card('Güncel TCMB kuru ve tahmini TL komisyon','<p role="status">Güncel TCMB USD döviz satış kuru şu an doğrulanamadı; tahmini TL karşılıkları gösterilmiyor. Sabit veya elle girilmiş kur kullanılmaz.</p>')
            else:
                out += c.card('Güncel TCMB kuru ve tahmini TL komisyon',
                    f'<p>TCMB USD döviz satış (gösterge) kuru: <strong>{cr.fx(rate.value)}</strong> · Yayın tarihi: <strong>{rate.published_on:%d.%m.%Y}</strong>. '
                    'Hafta sonu ve resmî tatillerde son yayımlanan kur gösterilir.</p>'
                    '<p><strong>Aşağıdaki TL tutarlar tahminidir; kesinleşmiş hak ediş değildir.</strong> Kesin TL hak ediş, müşterinin satış işleminde uygulanan kurla tahsilat doğrulandığında oluşur.</p>'
                    + c.table(('Paket','İlk dönem %25 (USD)','Tahmini TL','Sonraki dönem %5 (USD)','Tahmini TL'),
                              [[r['plan'].name, cr.usd(r['first']), cr.try_text(cr.estimate_try(r['first'], rate)),
                                cr.usd(r['later']), cr.try_text(cr.estimate_try(r['later'], rate))] for r in prices]))
            out += c.card('Paket yükseltme komisyonu (%2, USD)',
                '<p>Yükseltme ayında komisyon, eski ve yeni paketin USD komisyon matrahı <strong>farkı</strong> üzerinden %2’dir; '
                'yeni paketin tamamı üzerinden hesaplanmaz. Sonraki uygun dönemlerde yeni paketin matrahı üzerinden %5 uygulanır. '
                'Yükseltme farkı tahsil edilmeden komisyon oluşmaz; belirsiz durumlar CONTROL incelemesine alınır.</p>'
                + c.table(('Yükseltme','Matrah farkı (USD)','%2 komisyon (USD)'),
                          [[f'{a} → {b}', cr.usd(d), cr.usd(u)] for a, b, d, u in cr.upgrade_examples()]))
            out += c.card('ERNASA Eğitim Platformu', '<p>Ayrı abonelik. Eğitim atama, ilerleme takibi, bilgi ölçme ve izinli sesli/görüntülü sunum. AI eğitim raporu geliştirme ve kabul testi sonrasında sunulur. '
                          'HR FLOW kullanan müşteriye satıldığında ayrı ürün aboneliğidir: ilk aydan itibaren %5 komisyon (komisyon matrahı aylık USD fiyatıdır).</p>'+c.table(('Paket','Aylık fiyat (USD)','Aylık eğitim katılımcısı'),education_rows))
            out += c.card('Kuruma özel eğitim içeriği', c.table(('Hizmet','Konu başına (TL)'),[['Hazır dokümandan eğitim','750'],['Özgün eğitim ve sınav','1.750'],['İleri teknik / mesleki eğitim','3.500'],['Özel video / animasyon','Özel teklif']]))
            out += c.card('Fiyat koşulları','<p>Fiyatlar KDV hariçtir. Türkiye sözleşme, fatura ve tahsilatları TL ile yapılır. Sabit kur kampanyası yoktur. Güncel kur ve TL tutarı resmi teklif aşamasında CONTROL tarafından doğrulanır. Liste fiyatı resmi teklif değildir; indirim ve teklif onayı CONTROL yetkisindedir.</p>')
        if section == 'resources':
            from . import commission_report as cr
            out += c.card('Yetki ve komisyon rehberi',
                '<p>Komisyon matrahı merkezi fiyat kataloğundaki USD tutardır (KDV ve fiyata dahil ödeme sistemi maliyeti hariç): '
                'Starter $99,99 · Growth $249,99 · Business $499,99 · PRO $999,99. Eğitim Platformu için aylık USD fiyatı.</p>'
                '<ul>' + ''.join(f'<li>{c.e(rule)}</li>' for rule in cr.POLICY_RULES) + '</ul>'
                f'<p><strong>{c.e(cr.EXPLANATION)}</strong></p>'
                '<p>Resmi fiyat, indirim ve taahhüt için onaylı teklif kullanın. Ödemeler tahsilatı izleyen ayın 1–15 günlerinde, '
                'CONTROL onayıyla yapılır. Test ödemelerinden komisyon doğmaz. Özel projede maliyet/kâr analizi ve açık CONTROL kararı gerekir.</p>')
            out += c.card('Komisyon hesaplama örnekleri', c.table(('Durum','USD hesap','Satış kuru','Kesinleşen TL'), [
                ['Yeni müşteri, Starter, ilk ay', '$99,99 × %25 = $25,00', '50,77 TL', '1.269,25 TL'],
                ['Aynı abonelik, sonraki ay', '$99,99 × %5 = $5,00', '50,77 TL', '253,85 TL'],
                ['Starter → Growth yükseltme ayı', '($249,99 − $99,99) $150,00 × %2 = $3,00', '50,77 TL', '152,31 TL'],
                ['Yükseltmeden sonraki ay (Growth)', '$249,99 × %5 = $12,50', '50,77 TL', '634,63 TL'],
                ['HR FLOW müşterisine Eğitim Starter, ilk ay', '$75,00 × %5 = $3,75', '50,77 TL', '190,39 TL'],
                ['İptalden 1 yıl sonra yeniden Starter', '$99,99 × %5 = $5,00', '50,77 TL', '253,85 TL'],
            ]) + '<p>Ödeme gününde kur 54 TL veya 48 TL olsa da kesinleşen TL tutarı değişmez; ERNASA sonradan oluşan kur farkından sorumlu değildir.</p>')
            out += c.card('Satış temsilcisi kullanım kılavuzu',
                '<ol><li>Müşteriyi vergi numarası veya alan adıyla kaydedin; aynı müşteri ikinci kez kaydedilemez, sahiplik talebi CONTROL’e gider.</li>'
                '<li>Fırsat türünü doğru seçin: ilk abonelik, paket yükseltme, ek/ayrı ürün veya özel proje.</li>'
                '<li>Teklifi CONTROL değerlendirmesine gönderin; onaydan sonra Ödeme bağlantıları sekmesinden bağlantı oluşturup kopyalayın, WhatsApp veya e-posta ile gönderin.</li>'
                '<li>Ödeme durumunu aynı ekrandan izleyin; komisyon tahsilat doğrulanınca USD olarak hesaplanır ve satış kuruyla TL’ye sabitlenir.</li>'
                '<li>Komisyonlarım sekmesinde matrah, oran, USD komisyon, sabit kur, TL hak ediş ve ödeme dönemini kontrol edin.</li></ol>'
                '<p>Tam metin: <code>docs/sales/satis-temsilcisi-kullanim-kilavuzu.md</code>.</p>')
            out += c.card('Yeni satış ortaklığı sözleşmesi şablonu',
                '<p>Bu şablon yalnız <strong>yeni</strong> imzalanacak satış ortaklığı sözleşmelerinde kullanılır; mevcut imzalı sözleşmeler '
                'tek taraflı değiştirilmez. İmza öncesi hukuki inceleme gerekir.</p>'
                '<p><strong>Madde – Komisyon ve ödeme.</strong> Komisyon, ERNASA merkezi fiyat kataloğundaki USD komisyon matrahı üzerinden '
                'USD olarak hesaplanır ve iki ondalık basamağa yuvarlanır. TL hak ediş, müşterinin satış işleminde uygulanan TCMB USD '
                'döviz satış kuru üzerinden, tahsilat ERNASA tarafından doğrulandığında kesinleşir ve sabitlenir. Ödeme günündeki kur '
                'kullanılmaz; sonraki kur değişiklikleri kesinleşmiş tutarı etkilemez ve ERNASA kur farkından sorumlu değildir. Ödeme, '
                'tahsilatı izleyen ayın 1–15 günleri arasında Türk lirası olarak yapılır. Doğrulanmamış tahsilat, test ödemesi, iade/iptal '
                'incelemesindeki tahsilat ve WhatsApp API modülleri için hak ediş doğmaz. Oranlar Ek-1 komisyon tablosundadır: '
                'ilk ürün aboneliği %25 ve devamı %5; paket yükseltme ayı matrah farkının %2’si, sonrası %5; ayrı ürün ve yeniden abonelik %5.</p>'
                '<p>Tam metin: depo içinde <code>docs/sales/satis-ortakligi-sozlesmesi-sablonu.md</code>.</p>')
        if section == 'profile':
            out += c.card('Profil',f'<p>{c.e(info["name"])} · {c.e(info["email"])}</p><p>Firma: {actor.company_id}</p>')
            out += c.card('Parolayı değiştir',f('password',inp('old_password','Mevcut parola','password')+inp('password','Yeni parola (12–128 karakter)','password'),'Parolayı değiştir ve oturumları kapat'))
    return '<div class="sales-hub-ui">'+out+'</div>'


def handle(h, path, post):
    from . import app as c
    if path not in ('/satis', '/sales') and not path.startswith(('/sales/', '/satis/')):
        return False
    area = 'control' if path == '/satis' or path.startswith('/satis/') else 'sales'
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
            identity = current(c,h,db,area)
            if path == '/sales/login':
                if post:
                    token = login(c,h,db,data)
                    db.commit()
                    h.reply(303,headers=(('Location',h.server.public_prefix+'/sales'),set_cookie(h,'sales_session',token,c.TTL)))
                elif identity:
                    h.reply(303,headers=(('Location',h.server.public_prefix+'/sales'),))
                else:
                    token = secrets.token_urlsafe(32)
                    render(c,h,'Satış temsilcisi girişi',sales_login_form(c,token),
                           headers=(set_cookie(h,'sales_login_csrf',token,c.LOGIN_CSRF_TTL),))
                return True
            if not identity:
                if area == 'control' and not post:
                    h.reply(303,headers=(('Location',h.server.public_prefix+'/giris'),))
                    return True
                if api or post:
                    sales.fail('Oturum açmanız gerekiyor.',401)
                h.reply(303,headers=(('Location',h.server.public_prefix+'/sales/login'),))
                return True
            actor,csrf,info = identity
            if area == 'control':
                sales.admin(actor)
            elif actor.founder:
                sales.fail('Sales Hub yalnız satış temsilcisi hesabıyla kullanılır.', 403)
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
            elif path in ('/sales/api/record','/sales/official','/satis/official'):
                table = query.get('table',['proposals'])[0]
                if table not in sales.TABLES:
                    sales.fail('Kayıt türü bulunamadı.',404)
                if path in ('/sales/official','/satis/official'):
                    table = 'proposals'
                row = sales.owned(db,actor,table,query.get('id',[''])[0])
                if path in ('/sales/official','/satis/official'):
                    if row['status']!='approved':
                        sales.fail('Resmi teklif için CONTROL onayı gerekli.',409)
                    body = c.card('CONTROL onaylı teklif',f'<p>Teklif: {record_code(c,row["id"],"TEK")} · Sürüm {row["version"]}</p><p>{c.e(row["brief"])}</p><p>Liste: {money(row["official_minor"])} · İndirim: {money(row["discount_minor"])} · KDV hariç net: <strong>{money(row["official_minor"]-row["discount_minor"])}</strong></p>')
                    render(c,h,'Onaylı teklif',body,identity)
                else:
                    h.reply(200,json.dumps(dict(row)).encode(),'application/json')
            elif path in ('/sales/whatsapp','/satis/whatsapp'):
                from . import billing_share
                from datetime import datetime as _dt, timezone as _tz
                url = billing_share.whatsapp_url(db, actor, query.get('ref', [''])[0], _dt.now(_tz.utc))
                db.commit()
                h.reply(303, headers=(('Location', url),))
            elif path in ('/sales','/satis'):
                default_tab = 'accounts' if actor.founder else 'overview'
                tab = query.get('tab',[default_tab])[0]
                allowed_tabs = tuple(key for key,_ in FOUNDER_TABS) if actor.founder else ('overview','customers','opportunities','proposals','tasks','finance','payments','catalog','resources','profile')
                if tab not in allowed_tabs:
                    sales.fail('Ekran bulunamadı.',404)
                render(c,h,'Satış Yönetimi' if actor.founder else 'ERNASA Sales Hub',content(c,db,identity,tab,query.get('ref',[None])[0] if tab=='payments' else None),identity,tab=tab)
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
            back = '/satis' if area == 'control' else '/sales'
            render(c,h,'İşlem tamamlanamadı',f'<p role="alert">{c.e(str(error))}</p><p><a href="{back}">Satış paneline dön</a></p>',identity,status=error.status)
    except (ValueError,TypeError,UnicodeError):
        h.reply(422,json.dumps({'detail':'İstek okunamadı.'}).encode(),'application/json')
    except (c.sqlite3.IntegrityError,c.psycopg2.IntegrityError):
        h.reply(409,json.dumps({'detail':'Mükerrer kayıt veya veri koşulu çakışması.'}).encode(),'application/json')
    except (c.sqlite3.OperationalError,c.psycopg2.OperationalError,c.psycopg2.ProgrammingError):
        h.reply(503,json.dumps({'detail':'Satış veritabanı hazır değil. Eklemeli migration ve bağlantıyı kontrol edin.'}).encode(),'application/json')
    return True
