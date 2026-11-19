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
    ('finance','Tahsilat ve komisyon'), ('audit','Denetim kayıtları'),
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
    return f'<label>{c.e(label)}<select name="{name}" required>'+''.join(
        f'<option value="{c.e(str(key))}">{c.e(str(value))}</option>' for key, value in options)+'</select></label>'


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
    if action not in sales.COMMANDS:
        sales.fail('İşlem bulunamadı.', 404)
    return sales.COMMANDS[action](db, actor, data)


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
                 ('proposals','Tekliflerim ve kararlar'),('tasks','Görevlerim'),('finance','Komisyonlarım'),
                 ('resources','Satış kaynakları'),('profile','Profil ve güvenlik'))
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
            out += c.card('Temsilciler',c.table(('Ad','E-posta','Firma','Durum','Yetkilendirme'),[
                [c.e(a['name']),c.e(a['email']),str(a['company_id']),'Aktif' if a['active'] else 'Pasif',
                 f('account_active',c.hidden('id',a['id'])+c.hidden('active','0' if a['active'] else '1')+inp('reason','Yetki kararı gerekçesi'),'Pasifleştir' if a['active'] else 'Etkinleştir')]
                for a in data['accounts']]))
        if section == 'claims':
            out += c.card('Mükerrer müşteri ve sahiplik talepleri',c.table(('Talep','Müşteri','Talep eden','Durum','Karar'),[
                [c.e(r['id']),c.e(r['customer_id']),c.e(r['owner_id']),status(r['status']),
                 f('resolve_claim',c.hidden('id',r['id'])+options('decision','Sahiplik kararı',[('rejected','Mevcut sahipte tut'),('accepted','Talep edene devret')])+inp('reason','Gerekçe')) if r['status']=='pending' else c.e(r['reason'])]
                for r in data['claims']]))
        if section == 'customers':
            if not actor.founder:
                out += c.card('Müşteri kaydet',f('customer',inp('name','Firma adı')+inp('tax_id','Vergi numarası','text',False)+inp('domain','Firma alan adı','text',False)))
                out += c.card('Sahiplik başvurularım',c.table(('Başvuru','Durum','CONTROL açıklaması'),[[c.e(r['requested_name']),status(r['status']),c.e(r['reason'])] for r in data['claims']]))
            out += c.card('Müşteriler',c.table(('Firma','Kayıt','Sahip','Sınıf','İşlem'),[
                [c.e(r['name']),c.e(r['id']),c.e(r['owner_id']),status(r['classification']),
                 f('classify',c.hidden('id',r['id'])+options('classification','Müşteri sınıfı',[('new','Yeni müşteri'),('existing','Mevcut müşteri')])+inp('reason','Doğrulama gerekçesi')) if actor.founder else '']
                for r in data['customers']]))
        if section == 'opportunities':
            out += c.card('Fırsat oluştur',f('opportunity',options('customer_id','Müşteri',[(r['id'],r['name']) for r in data['customers']])+inp('title','İhtiyaç / fırsat başlığı')+options('kind','Satış türü',[(k,LABELS[k]) for k in ('subscription','upgrade','addon','project')])))
            out += c.card('Fırsatlar',c.table(('Başlık','Kayıt','Müşteri','Tür'),[[c.e(r['title']),c.e(r['id']),c.e(r['customer_id']),status(r['kind'])] for r in data['opportunities']]))
        if section == 'tasks':
            out += c.card('Görüşme notu / görev',f('activity',options('opportunity_id','Fırsat',[(r['id'],r['title']) for r in data['opportunities']])+inp('note','İhtiyaç, görüşme notu veya görev')+inp('due_date','Takip tarihi','date',False)))
            out += c.card('Görevler ve görüşmeler',c.table(('Fırsat','Not','Takip'),[[c.e(r['opportunity_id']),c.e(r['note']),c.e(r['due_date'])] for r in data['activities']]))
        if section == 'proposals':
            out += c.card('Teklif taslağı / revizyon sürümü', '<p>Tutarları KDV hariç TL olarak girin (örnek: 1.234,56). Bu kayıt resmi teklif değildir.</p>'+f('proposal',options('opportunity_id','Fırsat',[(r['id'],r['title']) for r in data['opportunities']])+inp('requested_minor','Talep edilen tutar (kuruş)','number')+inp('brief','Kapsam ve ihtiyaç açıklaması')))
            proposals = []
            for r in data['proposals']:
                action = ''
                if r['status']=='draft':
                    action = f('submit',c.hidden('id',r['id']),'CONTROL değerlendirmesine gönder')
                if r['status']=='submitted' and actor.founder:
                    action = f('decide',c.hidden('id',r['id'])+options('decision','Karar',[('approved','Onayla'),('revision','Revizyon iste'),('rejected','Reddet')])+inp('reason','Karar gerekçesi')+inp('official_minor','Resmi liste tutarı (kuruş)','number',False)+inp('discount_minor','CONTROL indirimi (kuruş)','number',False),'Kararı kaydet')
                if r['status']=='approved':
                    action = f'<a href="/sales/official?id={r["id"]}">Onaylı teklifi görüntüle</a>'
                proposals.append([c.e(r['id']),str(r['version']),c.e(r['brief']),status(r['status']),money(r['requested_minor']),money(None if r['official_minor'] is None else r['official_minor']-r['discount_minor']),action])
            out += c.card('Teklif değerlendirme',c.table(('Kayıt','Sürüm','Kapsam','Durum','Talep','Resmi net','İşlem'),proposals))
            out += c.card('Yönetim karar geçmişi',c.table(('Teklif','Karar','Gerekçe','Karar sahibi','Tarih'),[[c.e(r['proposal_id']),status(r['decision']),c.e(r['reason']),str(r['actor_id']),c.e(r['stamp'])] for r in data['decisions']]))
        if section == 'finance':
            if actor.founder:
                approved = [(r['id'],r['id']) for r in data['proposals'] if r['status']=='approved']
                out += c.card('Tahsilat doğrulama', '<p>KDV hariç tam dönem tutarı girin. Sağlayıcı maliyeti ayrı raporlanır. Gerçek ödeme veya e-posta işlemi yapılmaz.</p>'+f('collection',options('proposal_id','Onaylı teklif',approved)+inp('period','Abonelik dönemi (1, 2, …)','number')+inp('reference','Benzersiz tahsilat referansı')+inp('net_minor','Tahsil edilmiş net tutar (kuruş)','number')+inp('cost_minor','Sağlayıcı maliyeti (kuruş)','number')+inp('collected_on','Tahsilat tarihi','date')+inp('reason','Doğrulama dayanağı')))
                out += c.card('Özel proje hak ediş kararı',f('project_commission',options('collection_id','Tahsilat',[(r['id'],r['id']) for r in data['collections']])+inp('amount_minor','Hak ediş tutarı (kuruş)','number')+inp('analysis','Maliyet / kâr analizi')+inp('reason','Açık yazılı komisyon kararı')))
            out += c.card('Doğrulanmış tahsilatlar',c.table(('Kayıt','Dönem','Net tahsilat','Tarih','İnceleme','İşlem'),[
                [c.e(r['id']),str(r['period']),money(r['net_minor']),c.e(r['collected_on']),'İade/iptal bekletiliyor' if r['held'] else 'Doğrulandı',
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
                commissions.append([c.e(r['id']),money(r['amount_minor']),str(r['rate'])+'%',status(r['status']),c.e(r['payable_from']+' – '+r['payable_until']),action])
            out += c.card('Komisyonlar',c.table(('Kayıt','Hak ediş','Oran','Durum','Ödeme takvimi','CONTROL'),commissions))
            out += c.card('Düzeltme kayıtları',c.table(('Hak ediş','Düzeltme (kuruş)','Gerekçe','Tarih'),[[c.e(r['commission_id']),str(r['amount_minor']),c.e(r['reason']),c.e(r['stamp'])] for r in data['adjustments']]))
            if actor.founder:
                out += c.card('Kârlılık özeti',f'<p>Net tahsilat: {money(sum(r["net_minor"] for r in data["collections"]))} · Sağlayıcı maliyeti: {money(sum(r["cost_minor"] for r in data["collections"]))}</p><p>İade/iptal incelemesindeki kayıtlar ayrıca değerlendirilmelidir. Sağlayıcı maliyeti temsilci matrahından düşülmez.</p>')
        if section == 'audit':
            out += c.card('Silme ve güncellemeye kapalı satış denetimi',c.table(('Tarih','İşlemi yapan','İşlem','Hedef','Açıklama'),[[c.e(r['stamp']),c.e(r['actor_id']),c.e(r['event']),c.e(r['target']),c.e(r['detail'])] for r in data['audit']]))
        if section == 'resources':
            out += c.card('Yetki ve komisyon rehberi','<p>Yeni müşterinin ilk ürün aboneliği: ilk tahsil edilmiş dönem %25; aynı aboneliğin devamı %5. Paket yükseltme ve mevcut müşteriye ek ürün %0. Özel projede maliyet/kâr analizi ve açık CONTROL kararı gerekir.</p><p>Resmi fiyat, indirim ve taahhüt için onaylı teklif kullanın. Tahsilat yoksa hak ediş yoktur. Ödeme takvimi sonraki ayın 1–15 günleridir ve CONTROL onayına bağlıdır.</p>')
        if section == 'profile':
            out += c.card('Profil',f'<p>{c.e(info["name"])} · {c.e(info["email"])}</p><p>Firma: {actor.company_id}</p>')
            out += c.card('Parolayı değiştir',f('password',inp('old_password','Mevcut parola','password')+inp('password','Yeni parola (12–128 karakter)','password'),'Parolayı değiştir ve oturumları kapat'))
    return out


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
                else:
                    token = secrets.token_urlsafe(32)
                    fields = input_field(c,'email','E-posta','email')+input_field(c,'password','Parola','password')
                    render(c,h,'Satış temsilcisi girişi',form(c,'/sales/login',token,'login',fields,'Giriş yap')+'<p>Hesabınız CONTROL tarafından etkinleştirilmelidir. Akademi erişimi satış yetkisi sağlamaz.</p>',headers=(set_cookie(h,'sales_login_csrf',token,c.LOGIN_CSRF_TTL),))
                return True
            if not identity:
                if api or post or path == '/satis':
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
                    tab = parse_qs(urlsplit(h.path).query).get('tab',['overview'])[0]
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
                    body = c.card('CONTROL onaylı teklif',f'<p>Teklif: {c.e(row["id"])} · Sürüm {row["version"]}</p><p>{c.e(row["brief"])}</p><p>Liste: {money(row["official_minor"])} · İndirim: {money(row["discount_minor"])} · KDV hariç net: <strong>{money(row["official_minor"]-row["discount_minor"])}</strong></p>')
                    render(c,h,'Onaylı teklif',body,identity)
                else:
                    h.reply(200,json.dumps(dict(row)).encode(),'application/json')
            elif path in ('/sales','/satis'):
                default_tab = 'accounts' if actor.founder else 'overview'
                tab = query.get('tab',[default_tab])[0]
                allowed_tabs = tuple(key for key,_ in FOUNDER_TABS) if actor.founder else ('overview','customers','opportunities','proposals','tasks','finance','resources','profile')
                if tab not in allowed_tabs:
                    sales.fail('Ekran bulunamadı.',404)
                render(c,h,'Satış Yönetimi' if actor.founder else 'ERNASA Sales Hub',content(c,db,identity,tab),identity,tab=tab)
            else:
                sales.fail('Ekran bulunamadı.',404)
    except (sales.Invalid,c.Problem) as error:
        if api:
            h.reply(error.status,json.dumps({'detail':str(error)}).encode(),'application/json')
        else:
            render(c,h,'İşlem tamamlanamadı',f'<p role="alert">{c.e(str(error))}</p><p><a href="/sales">Satış paneline dön</a></p>',identity,status=error.status)
    except (ValueError,TypeError,UnicodeError):
        h.reply(422,json.dumps({'detail':'İstek okunamadı.'}).encode(),'application/json')
    except (c.sqlite3.IntegrityError,c.psycopg2.IntegrityError):
        h.reply(409,json.dumps({'detail':'Mükerrer kayıt veya veri koşulu çakışması.'}).encode(),'application/json')
    except (c.sqlite3.OperationalError,c.psycopg2.OperationalError,c.psycopg2.ProgrammingError):
        h.reply(503,json.dumps({'detail':'Satış veritabanı hazır değil. Eklemeli migration ve bağlantıyı kontrol edin.'}).encode(),'application/json')
    return True
