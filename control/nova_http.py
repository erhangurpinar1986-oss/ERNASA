"""CONTROL › NOVA AI SALES CENTER (founder only) and NOVA provider hooks.

Routes (CONTROL server; public under /control):
  GET  /nova?tab=…            founder panel (overview, pool, emails, calls, knowledge, settings, report)
  POST /nova                  founder commands (CSRF + Origin checked, one write transaction)
  GET  /nova/api              founder JSON snapshot
  GET  /nova/havuz.csv        founder CSV export of the data pool (corporate data only)
  POST /nova/hook/vapi        voice provider end-of-call report (shared secret header)
  POST /nova/hook/tick        scheduler tick (Bearer ERNASA_NOVA_TICK_SECRET): send approved e-mail within
                              limits, read the mailbox read-only, write the daily report
No route here can lift a security control: the kill switch, limits, objection list and policy
gates are enforced in control/nova.py for every caller, including the scheduler.
"""
from contextlib import closing
import csv
import hmac
import io
import json
import os
from urllib.parse import parse_qs, urlsplit

from . import nova, nova_providers
from .sales import Actor, Invalid

TABS = (('overview', 'Genel bakış'), ('pool', 'Veri havuzu'), ('emails', 'E-posta'), ('calls', 'Telefon'),
        ('knowledge', 'Ürün bilgi merkezi'), ('settings', 'Ayarlar ve limitler'), ('report', 'Günlük rapor'))
MAX_FORM = 2 * 1024 * 1024


def tl(minor):
    minor = int(minor or 0)
    sign = '-' if minor < 0 else ''
    minor = abs(minor)
    return sign + f'{minor // 100:,}'.replace(',', '.') + f',{minor % 100:02d} TL'


def begin(db, global_lock=False):
    """NOVA's own write lock. CONTROL's global write lock (BEGIN IMMEDIATE -> advisory 47621 on PG) is
    taken only for commands that write Sales Hub tables, so slow provider calls never block CONTROL."""
    if global_lock or not hasattr(db, 'raw'):
        db.execute('BEGIN IMMEDIATE')
    else:
        db.raw.cursor().execute('SELECT pg_advisory_xact_lock(184093, 47624)')


def read_form(h):
    if h.headers.get('Origin') != h.server.origin:
        raise Invalid(403, 'İstek kaynağı doğrulanamadı.')
    size = int(h.headers.get('Content-Length', '0'))
    if not 1 <= size <= MAX_FORM:
        raise Invalid(413, 'Form boyutu sınır dışında.')
    if h.headers.get_content_type() != 'application/x-www-form-urlencoded':
        raise Invalid(415, 'Form içeriği bekleniyor.')
    body = h.rfile.read(size).decode()
    return {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True, max_num_fields=80).items()}


def read_json(h, limit=262144):
    size = int(h.headers.get('Content-Length', '0'))
    if not 1 <= size <= limit or h.headers.get_content_type() != 'application/json':
        raise Invalid(400, 'JSON gövdesi bekleniyor.')
    data = json.loads(h.rfile.read(size))
    if not isinstance(data, dict):
        raise Invalid(422, 'JSON nesnesi gerekli.')
    return data


def tabs(c, selected):
    links = ''.join(f'<a href="/nova?tab={key}"' + (' aria-current="page"' if key == selected else '') + f'>{c.e(label)}</a>'
                    for key, label in TABS)
    return '<nav class="sales-section-tabs" aria-label="NOVA bölümleri">' + links + '</nav>'


def post_form(c, csrf, action, fields, button, tab):
    return (f'<form method="post" action="/nova?tab={tab}">{c.hidden("csrf", csrf)}{c.hidden("action", action)}'
            f'<div class="fields">{fields}</div><button>{c.e(button)}</button></form>')


def inp(c, label, key, value='', kind='text', required=True):
    return c.inp(label, key, value, kind, required)


def area(c, label, key, value='', rows=6, required=True):
    return (f'<label>{c.e(label)}<textarea name="{key}" rows="{rows}" ' + ('required' if required else '') +
            f'>{c.e(value)}</textarea></label>')


def flag(value):
    return 'Açık' if value else 'Kapalı'


def overview(c, snap, csrf):
    m, cfg = snap['metrics'], snap['settings']
    readiness = nova_providers.status()
    stats = [('Toplam firma', m['companies']), ('Doğrulanmış / hedef 100', f'{m["verified"]} / 100'),
             ('Ortalama puan', m['avg_score']), ('Onay bekleyen e-posta', m['drafts']), ('Gönderilen e-posta', m['sent']),
             ('Teslimat oranı', f'%{m["delivery_rate"]}' if m['delivery_rate'] is not None else 'ölçülemedi'),
             ('Gelen yanıt', m['replies']), ('Olumlu / ret', f'{m["positive"]} / {m["negative"]}'),
             ('Planlı arama', m['calls_planned']), ('Tamamlanan görüşme', m['calls_done']),
             ('Görüşme süresi', f'{m["call_seconds"] // 60} dk'), ('Görüşme maliyeti', tl(m['call_cost_minor'])),
             ('Demo talebi', m['demos']), ('Açık fırsat', m['opportunities']), ('Temsilciye atanan', m['assigned']),
             ('Satış', m['won']), ('İletişimden çıkarılan', m['suppressed']),
             ('Harcama bugün / ay', f'{tl(m["spend_today_minor"])} / {tl(m["spend_month_minor"])}')]
    out = '<div class="stats metrics">' + ''.join(c.card(label, f'<strong>{c.e(str(value))}</strong>') for label, value in stats) + '</div>'
    state = ('ÇALIŞIYOR' if cfg['running'] and not cfg['kill_switch'] else 'DURDURULDU')
    control = (f'<p>Durum: <strong>{state}</strong> · Acil durdurma: {flag(cfg["kill_switch"])} · Otomatik e-posta: '
               f'{flag(cfg["auto_email"])} · Otomatik arama: {flag(cfg["auto_call"])}</p>'
               f'<p>E-posta politikası onayı: {flag(cfg["email_policy_approved"])} · Arama politikası onayı: '
               f'{flag(cfg["call_policy_approved"])} · İYS ret kontrolü zorunlu: {flag(cfg["require_iys"])}</p>')
    control += post_form(c, csrf, 'emergency_stop', inp(c, 'Gerekçe', 'reason', 'Acil durdurma', required=False),
                         'TÜM DIŞ İLETİŞİMİ DURDUR', 'overview')
    out += c.card('NOVA kontrol', control)
    rows = [[c.e(name), 'Hazır' if info['ready'] else 'Eksik', c.e(', '.join(info['needs']) or '—')]
            for name, info in (('Giden e-posta', readiness['email_out']), ('Gelen kutusu (salt okunur)', readiness['mailbox']),
                               ('Sesli arama (Vapi + TR SIP)', readiness['voice']), ('Zamanlayıcı', readiness['scheduler']))]
    out += c.card('Harici servis hazırlığı', c.table(('Bileşen', 'Durum', 'Eksik ortam değişkeni'), rows))
    demos = [[c.e(d['legal_name']), c.e(d['source']), c.e(d['status']), c.e(d['created_at'][:16])] for d in snap['demos']]
    out += c.card('Demo talepleri', c.table(('Firma', 'Kaynak', 'Durum', 'Tarih'), demos))
    events = [[c.e(ev['stamp'][:16]), c.e(ev['kind']), c.e(ev['detail'][:160])] for ev in snap['events']]
    out += c.card('Son NOVA hareketleri', c.table(('Zaman', 'Tür', 'Ayrıntı'), events))
    return out


def pool(c, snap, csrf):
    reps = snap['reps']
    rows = []
    for co in snap['companies']:
        actions = ''
        if co['stage'] != 'do_not_contact':
            if reps and not co['owner_account_id']:
                options = ''.join(f'<option value="{c.e(r["id"])}">{c.e(r["name"])}</option>' for r in reps)
                actions += (f'<form method="post" action="/nova?tab=pool">{c.hidden("csrf", csrf)}{c.hidden("action", "assign")}'
                            f'{c.hidden("id", co["id"])}<select name="account_id" required>{options}</select><button>Ata</button></form>')
            if not co['iys_checked_on']:
                actions += (f'<form method="post" action="/nova?tab=pool">{c.hidden("csrf", csrf)}{c.hidden("action", "iys")}'
                            f'{c.hidden("id", co["id"])}{c.hidden("result", "clear")}<button>İYS temiz</button></form>')
        rows.append([c.e(co['legal_name']) + (' <small>(sentetik)</small>' if co['synthetic'] else ''), c.e(co['sector']),
                     c.e(co['city']), c.e(co['email'] or '—'), c.e(co['phone'] or '—'), c.e(str(co['score'])),
                     c.e(nova.STAGES[co['stage']]), c.e(co['iys_checked_on'] or '—'), actions])
    out = c.card(f'ERNASA DATA POOL ({snap["metrics"]["companies"]} firma · {snap["metrics"]["verified"]} doğrulanmış)',
                 '<p><a href="/nova/havuz.csv">CSV olarak indir</a></p>' +
                 c.table(('Firma', 'Sektör', 'İl', 'Kurumsal e-posta', 'Kurumsal telefon', 'Puan', 'Aşama', 'İYS', 'İşlem'), rows))
    example = json.dumps([{"legal_name": "Örnek Lojistik A.Ş.", "sector": "Lojistik", "city": "İstanbul", "district": "Tuzla",
                           "website": "https://www.ornek.com.tr", "email": "ik@ornek.com.tr",
                           "email_source_url": "https://www.ornek.com.tr/iletisim", "phone": "0216 000 00 00",
                           "phone_source_url": "https://www.ornek.com.tr/iletisim", "open_jobs": "İK Uzmanı",
                           "job_source_url": "https://www.ornek.com.tr/kariyer", "job_seen_on": "2026-11-27",
                           "workload_terms": "CV tarama; mülakat planlama", "source_url": "https://www.ornek.com.tr",
                           "verified_on": "2026-11-27"}], ensure_ascii=False, indent=1)
    out += c.card('Toplu içe aktar (JSON)', '<p>Yalnızca kamuya açık kurumsal kaynaklardan, kaynak adresiyle. Mükerrer kayıtlar '
                  'alan adı, vergi no veya unvan+il ile otomatik engellenir; kişisel e-posta/cep telefonu reddedilir.</p>' +
                  post_form(c, csrf, 'import', area(c, 'Kayıtlar (JSON dizi)', 'rows', example, 14), 'İçe aktar', 'pool'))
    out += c.card('İletişim reddi ekle', post_form(c, csrf, 'suppress',
                  c.select('Tür', 'kind', [('domain', 'Alan adı'), ('email', 'E-posta'), ('phone', 'Telefon')]) +
                  inp(c, 'Değer', 'value') + inp(c, 'Gerekçe', 'reason', 'Firma iletişim istemiyor'), 'Ret listesine ekle', 'pool'))
    return out


def emails(c, snap, csrf):
    rows = []
    for d in snap['drafts']:
        actions = ''
        if d['status'] == 'draft':
            actions = (f'<form method="post" action="/nova?tab=emails">{c.hidden("csrf", csrf)}{c.hidden("action", "approve_email")}'
                       f'{c.hidden("id", d["id"])}<button>Onayla</button></form>')
        actions += (f'<form method="post" action="/nova?tab=emails">{c.hidden("csrf", csrf)}{c.hidden("action", "reject_email")}'
                    f'{c.hidden("id", d["id"])}<button>İptal</button></form>')
        rows.append([c.e(d['legal_name']), c.e(d['address']), c.e(d['subject']),
                     f'<details><summary>Metni göster</summary><pre>{c.e(d["body"])}</pre></details>', c.e(d['status']), actions])
    out = c.card('Onay kuyruğu', c.table(('Firma', 'Kime', 'Konu', 'Metin', 'Durum', 'İşlem'), rows))
    out += c.card('Onaylı e-postaları şimdi gönder', '<p>Acil durdurma, çalışma saatleri, günlük limit, politika onayı, İYS ve ret '
                  'listesi kontrolleri uygulanır.</p>' + post_form(c, csrf, 'dispatch', '', 'Gönderimi çalıştır', 'emails'))
    companies = [(co['id'], f'{co["legal_name"]} ({co["email"]})') for co in snap['companies']
                 if co['email'] and co['stage'] in ('verified', 'contactable', 'awaiting_reply')]
    out += c.card('Yeni taslak', post_form(c, csrf, 'draft_email', c.select('Firma', 'company_id', companies) +
                  inp(c, 'Konu', 'subject') + area(c, 'Metin (ret satırı ve gönderici bilgisi otomatik eklenir)', 'body', '', 8),
                  'Taslak oluştur', 'emails'))
    inbound = [[c.e(i['legal_name']), c.e(i['address']), c.e(i['subject']), c.e(i['classification']), c.e(i['created_at'][:16])]
               for i in snap['inbound']]
    out += c.card('Gelen yanıtlar', c.table(('Firma', 'Gönderen', 'Konu', 'Sınıf', 'Tarih'), inbound))
    return out


def calls(c, snap, csrf):
    rows = [[c.e(x['legal_name'] or 'Kurucu test hattı'), c.e(x['purpose']), c.e(x['status']), c.e(str(x['duration_seconds'])),
             c.e(tl(x['cost_minor'])) + (f' <small>({c.e(x["cost_source"])})</small>' if x['cost_source'] else ''),
             c.e(x['summary'][:300] or '—'),
             (f'<form method="post" action="/nova?tab=calls">{c.hidden("csrf", csrf)}{c.hidden("action", "start_call")}'
              f'{c.hidden("id", x["id"])}<button>Aramayı başlat</button></form>' if x['status'] == 'planned' else '')]
            for x in snap['calls']]
    out = c.card('Görüşmeler', c.table(('Görüşülen', 'Tür', 'Durum', 'Süre (sn)', 'Maliyet', 'Özet', 'İşlem'), rows))
    numbers = [(n['masked'], f'{n["label"]} ({n["masked"]})') for n in snap['test_numbers']]
    out += c.card('İlk gerçek test: kurucuyu ara', '<p>1) Kendi numaranızı yetkilendirin. 2) Test araması planlayın. '
                  '3) “Aramayı başlat”. Sağlayıcı yapılandırılmamışsa arama başlamaz ve tamamlanmış gösterilmez.</p>' +
                  post_form(c, csrf, 'test_number', inp(c, 'Telefon (+90…)', 'phone', kind='tel') + inp(c, 'Etiket', 'label', 'Kurucu') +
                            inp(c, 'Bu numara bana ait ve test aramasına izin veriyorum (EVET yazın)', 'confirm'), 'Numarayı yetkilendir', 'calls') +
                  post_form(c, csrf, 'plan_call', c.hidden('purpose', 'founder_test') + inp(c, 'Yetkili test numarası', 'phone', kind='tel'),
                            'Test araması planla', 'calls') + (f'<p>Yetkili numaralar: {c.e(", ".join(n[1] for n in numbers))}</p>' if numbers else ''))
    prospects = [(co['id'], co['legal_name']) for co in snap['companies'] if co['phone'] and co['stage'] not in ('do_not_contact', 'won', 'rejected')]
    out += c.card('Firma araması planla', '<p>Yazılı arama kabulü veya onaylı arama politikası, İYS kontrolü ve ret listesi zorunludur.</p>' +
                  post_form(c, csrf, 'plan_call', c.hidden('purpose', 'prospect') + c.select('Firma', 'company_id', prospects) +
                            inp(c, 'Görüşülecek departman', 'department', 'İnsan Kaynakları', required=False), 'Arama planla', 'calls'))
    return out


def knowledge(c, snap, csrf):
    rows = []
    for k in snap['knowledge']:
        actions = ''
        for key, label in (('approve', 'Onayla'), ('not_live', 'Canlı değil'), ('draft', 'Taslağa al')):
            actions += (f'<form method="post" action="/nova?tab=knowledge">{c.hidden("csrf", csrf)}{c.hidden("action", "knowledge")}'
                        f'{c.hidden("knowledge_action", key)}{c.hidden("id", k["id"])}<button>{label}</button></form>')
        rows.append([c.e(k['product']), c.e(k['topic']), c.e(k['question']), c.e(k['answer']), c.e(k['status']), c.e(k['source']), actions])
    out = c.card('Ürün bilgi merkezi — NOVA yalnızca “approved” kayıtları müşteriye söyler',
                 c.table(('Ürün', 'Konu', 'Soru', 'Cevap', 'Durum', 'Kaynak', 'İşlem'), rows))
    out += c.card('Bilgi ekle / güncelle (taslak olarak kaydedilir)', post_form(c, csrf, 'knowledge',
                  inp(c, 'Ürün', 'product', 'hr_flow') + inp(c, 'Konu', 'topic') + inp(c, 'Soru', 'question') +
                  area(c, 'Onaylı cevap', 'answer', '', 4) + inp(c, 'Kaynak', 'source'), 'Kaydet', 'knowledge'))
    return out


def settings_tab(c, snap, csrf):
    cfg = snap['settings']
    yes_no = [('0', 'Kapalı / Hayır'), ('1', 'Açık / Evet')]
    fields = ''.join(c.select(label, key, yes_no, str(cfg[key])) for key, label in (
        ('running', 'NOVA çalışıyor'), ('kill_switch', 'Acil durdurma'), ('auto_email', 'Otomatik e-posta'),
        ('auto_call', 'Otomatik arama'), ('email_policy_approved', 'E-posta gönderim politikası onaylandı'),
        ('call_policy_approved', 'Arama politikası ve hukuki koşullar onaylandı'), ('require_iys', 'İYS ret kontrolü zorunlu')))
    fields += ''.join(inp(c, label, key, str(cfg[key]), 'number') for key, label in (
        ('daily_research_target', 'Günlük araştırma hedefi'), ('daily_email_limit', 'Günlük e-posta limiti'),
        ('daily_call_limit', 'Günlük arama limiti'), ('max_call_seconds', 'Maksimum görüşme süresi (sn)'),
        ('daily_spend_limit_minor', 'Günlük harcama limiti (kuruş)'), ('monthly_spend_limit_minor', 'Aylık harcama limiti (kuruş)'),
        ('call_rate_minor_per_minute', 'Tahmini dakika maliyeti (kuruş)')))
    fields += ''.join(inp(c, label, key, str(cfg[key]), required=False) for key, label in (
        ('work_start', 'Çalışma başlangıcı (SS:DD)'), ('work_end', 'Çalışma bitişi (SS:DD)'), ('work_days', 'Çalışma günleri (1=Pzt … 7=Paz)'),
        ('target_sectors', 'Hedef sektörler'), ('target_regions', 'Hedef bölgeler'), ('product_priority', 'Ürün önceliği')))
    fields += area(c, 'İnsan temsilciye yönlendirme kuralları', 'handoff_rules', cfg['handoff_rules'], 3, False)
    return c.card('Ayarlar ve limitler', '<p>Politika onayı olmadan otomatik modlar açılamaz; acil durdurma açıkken NOVA başlatılamaz.</p>' +
                  post_form(c, csrf, 'settings', fields, 'Ayarları kaydet', 'settings'))


def report(c, db, csrf):
    body = nova.daily_report(db, store=False)
    stored = db.execute('SELECT day FROM nova_reports ORDER BY day DESC LIMIT 14').fetchall()
    return (c.card('Bugünkü yönetim raporu', f'<pre>{c.e(body)}</pre>' + post_form(c, csrf, 'report', '', 'Raporu kaydet', 'report')) +
            c.card('Kayıtlı raporlar', '<p>' + c.e(', '.join(r['day'] for r in stored) or '—') + '</p>'))


def command(db, actor, data, origin):
    action = data.get('action', '')
    if action == 'settings':
        return nova.update_settings(db, actor, data)
    if action == 'emergency_stop':
        return nova.emergency_stop(db, actor, data.get('reason') or 'Acil durdurma')
    if action == 'import':
        try:
            rows = json.loads(data.get('rows', ''))
        except ValueError:
            raise Invalid(422, 'JSON okunamadı.')
        return nova.import_companies(db, actor, rows)
    if action == 'suppress':
        nova.founder(actor)
        return nova.suppress(db, actor.id, data.get('kind', ''), data.get('value', ''), nova.s(data, 'reason', 300, True), 'control')
    if action == 'iys':
        return nova.mark_iys(db, actor, data)
    if action == 'draft_email':
        return nova.draft_email(db, actor, data)
    if action == 'approve_email':
        return nova.approve_email(db, actor, data)
    if action == 'reject_email':
        return nova.reject_email(db, actor, data)
    if action == 'dispatch':
        nova.founder(actor)
        return {'outcome': nova.dispatch_emails(db, nova_providers.EmailTransport())}
    if action == 'test_number':
        return nova.authorize_test_number(db, actor, data)
    if action == 'plan_call':
        return nova.plan_call(db, actor, data)
    if action == 'start_call':
        nova.founder(actor)
        return nova.start_call(db, nova.s(data, 'id', 64, True), nova_providers.VapiVoice(origin))
    if action == 'assign':
        return nova.assign(db, actor, data)
    if action == 'knowledge':
        return nova.knowledge_command(db, actor, data)
    if action == 'transition':
        nova.founder(actor)
        return nova.transition(db, actor.id, nova.s(data, 'id', 64, True), nova.s(data, 'stage', 30, True), nova.s(data, 'detail', 300))
    if action == 'report':
        nova.founder(actor)
        return {'report': nova.daily_report(db)}
    raise Invalid(404, 'İşlem bulunamadı.')


def tick(db, origin, messages):
    """One scheduler pass. Every step re-checks the kill switch and limits inside nova.py."""
    result = {}
    try:
        result['emails'] = nova.dispatch_emails(db, nova_providers.EmailTransport())
    except (nova_providers.NotConfigured, Invalid) as exc:
        result['emails'] = str(exc)
    if isinstance(messages, list):
        stored = [nova.record_inbound(db, m) for m in messages]
        result['inbound'] = {'read': len(stored), 'new': sum(1 for s in stored if s['status'] == 'stored')}
    else:
        result['inbound'] = messages
    cfg = nova.settings(db)
    if cfg['auto_call'] and cfg['call_policy_approved']:
        started = []
        for row in db.execute("SELECT id FROM nova_calls WHERE status='planned' AND purpose='prospect' ORDER BY created_at").fetchall():
            db.execute('SAVEPOINT nova_tick_call')
            try:
                nova.start_call(db, row['id'], nova_providers.VapiVoice(origin))
                started.append(row['id'])
            except (Invalid, nova_providers.NotConfigured, nova_providers.ProviderError) as exc:
                db.execute('ROLLBACK TO SAVEPOINT nova_tick_call')
                db.execute('RELEASE SAVEPOINT nova_tick_call')
                result['calls'] = str(exc)[:200]
                if isinstance(exc, Invalid) and exc.status in (423, 429):
                    break
                continue
            db.execute('RELEASE SAVEPOINT nova_tick_call')
        result['calls_started'] = len(started)
    if nova.now_local().strftime('%H:%M') >= cfg['work_end']:
        nova.daily_report(db)
        result['report'] = 'stored'
    return result


def hook(c, h, path):
    if path == '/nova/hook/vapi':
        if not nova_providers.VapiVoice.verify({'x-vapi-secret': h.headers.get('x-vapi-secret', '')}):
            return h.reply(403, b'{}', 'application/json')
        call_id, result = nova_providers.VapiVoice.parse_webhook(read_json(h))
        if not call_id:
            return h.reply(200, b'{"ignored":true}', 'application/json')
        with closing(c.connect(h.server.db_path)) as db, db:
            begin(db)
            try:
                outcome = nova.record_call_result(db, 'vapi', call_id, result)
            except Invalid as exc:
                return h.reply(exc.status, json.dumps({'detail': str(exc)}).encode(), 'application/json')
        return h.reply(200, json.dumps(outcome).encode(), 'application/json')
    if path == '/nova/hook/tick':
        secret = os.getenv('ERNASA_NOVA_TICK_SECRET', '')
        provided = h.headers.get('Authorization', '').removeprefix('Bearer ')
        if len(secret) < 32 or not hmac.compare_digest(secret.encode(), provided.encode()):
            return h.reply(403, b'{}', 'application/json')
        try:  # network read happens before any database lock is taken
            messages = nova_providers.Mailbox().fetch()
        except (nova_providers.NotConfigured, nova_providers.ProviderError, OSError) as exc:
            messages = str(exc)[:200]
        with closing(c.connect(h.server.db_path)) as db, db:
            begin(db)
            outcome = tick(db, h.server.origin, messages)
        return h.reply(200, json.dumps(outcome, ensure_ascii=False, default=str).encode(), 'application/json')
    return h.reply(404, b'{}', 'application/json')


def handle(h, path, post):
    from . import app as c
    if path != '/nova' and not path.startswith('/nova/'):
        return False
    if path.startswith('/nova/hook/'):
        if not post:
            h.reply(405)
            return True
        try:
            hook(c, h, path)
        except Invalid as exc:
            h.reply(exc.status, json.dumps({'detail': str(exc)}).encode(), 'application/json')
        except (ValueError, TypeError):
            h.reply(422, b'{"detail":"invalid"}', 'application/json')
        return True
    api = path == '/nova/api'
    founder = None
    try:
        data = read_form(h) if post else None
        with closing(c.connect(h.server.db_path)) as db, db:
            if post:
                begin(db, global_lock=data.get('action') == 'assign')
            founder = h.session(db)
            if not founder or founder['role'] != 'founder':
                if post or api:
                    raise Invalid(403, 'NOVA AI SALES CENTER yalnızca CONTROL kurucu oturumuyla kullanılır.')
                h.reply(303, headers=(('Location', h.server.public_prefix + '/giris'),))
                return True
            actor = Actor(str(founder['id']), None, True)
            nova.seed_knowledge(db)
            if post:
                if not hmac.compare_digest(founder['csrf'].encode(), data.get('csrf', '').encode()):
                    raise Invalid(403, 'Form doğrulanamadı.')
                if path != '/nova':
                    raise Invalid(404, 'İşlem bulunamadı.')
                result = command(db, actor, data, h.server.origin)
                db.commit()
                tab = parse_qs(urlsplit(h.path).query).get('tab', ['overview'])[0]
                tab = tab if tab in dict(TABS) else 'overview'
                notice = ''
                if isinstance(result, dict) and 'created' in result:
                    notice = f'İçe aktarma: {len(result["created"])} yeni, {len(result["duplicates"])} mükerrer, {len(result["rejected"])} reddedildi.'
                    if result['rejected']:
                        notice += ' Reddedilenler: ' + '; '.join(f'#{r["row"]} {r.get("name", "")}: {r["reason"]}' for r in result['rejected'][:10])
                elif isinstance(result, dict) and 'outcome' in result:
                    notice = 'Gönderim: ' + ', '.join(f'{o["status"]}' + (f' ({o["reason"]})' if o.get('reason') else '') for o in result['outcome'][:10]) if result['outcome'] else 'Gönderilecek onaylı e-posta yok.'
                if notice:
                    snap = nova.snapshot(db)
                    render(c, h, db, founder, tab, f'<p role="status">{c.e(notice)}</p>', snap)
                    return True
                h.reply(303, headers=(('Location', h.server.public_prefix + '/nova?tab=' + tab),))
                return True
            if api:
                h.reply(200, json.dumps(nova.snapshot(db), default=str).encode(), 'application/json')
                return True
            if path == '/nova/havuz.csv':
                buffer = io.StringIO()
                writer = csv.writer(buffer)
                columns = ('legal_name', 'sector', 'city', 'district', 'website', 'email', 'email_source_url', 'phone',
                           'phone_source_url', 'linkedin_url', 'employee_count', 'open_jobs', 'job_source_url', 'hiring_need',
                           'suggested_package', 'score', 'stage', 'source_url', 'verified_on', 'iys_checked_on', 'owner_account_id')
                writer.writerow(columns)
                for row in db.execute('SELECT * FROM nova_companies WHERE synthetic=0 ORDER BY score DESC').fetchall():
                    # Spreadsheet formula injection guard.
                    writer.writerow([("'" + str(row[k])) if str(row[k] or '').startswith(('=', '+', '-', '@')) else row[k] for k in columns])
                h.reply(200, ('﻿' + buffer.getvalue()).encode('utf-8'), 'text/csv; charset=utf-8',
                        headers=(('Content-Disposition', 'attachment; filename="ernasa-data-pool.csv"'),))
                return True
            if path != '/nova':
                raise Invalid(404, 'Ekran bulunamadı.')
            tab = parse_qs(urlsplit(h.path).query, max_num_fields=4).get('tab', ['overview'])[0]
            if tab not in dict(TABS):
                raise Invalid(404, 'Ekran bulunamadı.')
            render(c, h, db, founder, tab, '', nova.snapshot(db))
    except Invalid as error:
        if api or path.endswith('.csv'):
            h.reply(error.status, json.dumps({'detail': str(error)}).encode(), 'application/json')
        else:
            info = page_actor(c, founder) if founder and founder['role'] == 'founder' else None
            h.page('İşlem tamamlanamadı', f'<p role="alert">{c.e(str(error))}</p><p><a href="/nova">NOVA paneline dön</a></p>',
                   info, error.status, path='/nova')
    except (c.sqlite3.IntegrityError, c.psycopg2.IntegrityError):
        h.page('İşlem tamamlanamadı', '<p role="alert">Mükerrer kayıt veya veri koşulu çakışması.</p><p><a href="/nova">Geri dön</a></p>',
               page_actor(c, founder) if founder else None, 409, path='/nova')
    except (c.sqlite3.OperationalError, c.psycopg2.OperationalError, c.psycopg2.ProgrammingError):
        h.page('NOVA hazır değil', '<p role="alert">NOVA tabloları kurulmamış. Eklemeli migration (migrations/nova) uygulanmalı.</p>',
               page_actor(c, founder) if founder else None, 503, path='/nova')
    return True


def page_actor(c, founder):
    return dict(founder)


def render(c, h, db, founder, tab, notice, snap):
    info = dict(founder)
    info['routes'] = c.allowed_routes(db, info)
    if not getattr(h.server, 'ernasa_invite_handler', None):
        info['routes'] = info['routes'] - {c.ERNASA_INVITE_PATH}
    if not getattr(h.server, 'experience_summary', None):
        info['routes'] = info['routes'] - {'/aday-deneyimi'}
    csrf = founder['csrf']
    body = {'overview': lambda: overview(c, snap, csrf), 'pool': lambda: pool(c, snap, csrf),
            'emails': lambda: emails(c, snap, csrf), 'calls': lambda: calls(c, snap, csrf),
            'knowledge': lambda: knowledge(c, snap, csrf), 'settings': lambda: settings_tab(c, snap, csrf),
            'report': lambda: report(c, db, csrf)}[tab]()
    h.page('NOVA AI SALES CENTER', '<div class="sales-hub-ui">' + tabs(c, tab) + notice + body + '</div>', info, path='/nova')
