"""Offline Sales Hub integration checks; only synthetic temporary CONTROL data."""
from contextlib import closing
from datetime import date
from http.client import HTTPResponse
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from threading import Lock
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from control import app, sales, sales_http

PASSWORD = 'Synthetic-sales-test-2026!'


@pytest.fixture
def store(tmp_path):
    path = tmp_path / 'synthetic-control.sqlite3'
    app.init(path)
    db = app.connect(path)
    db.execute("INSERT INTO companies VALUES(1,'Sentetik ERNASA','Aktif')")
    db.execute("INSERT INTO companies VALUES(2,'Sentetik İkinci Firma','Aktif')")
    db.execute("INSERT INTO users(id,email,name,secret,role) VALUES(1,'founder@example.test','Sentetik Kurucu',?,'founder')", (app.digest(PASSWORD),))
    for target, tenant in [('rep-a',1),('rep-b',1),('rep-c',2)]:
        db.execute('INSERT INTO sales_accounts VALUES(?,?,?,?,?,1)', (target,tenant,target,target+'@example.test',app.digest(PASSWORD)))
    db.commit()
    yield db, path
    db.close()


A = sales.Actor('rep-a',1)
B = sales.Actor('rep-b',1)
C = sales.Actor('rep-c',2)
F = sales.Actor('1',None,True)


def execute(db, actor, command, **data):
    with db:
        db.execute('BEGIN IMMEDIATE')
        return sales_http.commands(app,db,actor,{'action':command,**data})


def setup_sale(db, kind='subscription', classification='new', amount=10001, key='sample.test'):
    customer = execute(db,A,'customer',name='Sentetik müşteri',domain=key)['id']
    execute(db,F,'classify',id=customer,classification=classification,reason='Sentetik firma kontrolü')
    opportunity = execute(db,A,'opportunity',customer_id=customer,title='Sentetik fırsat',kind=kind)['id']
    proposal = execute(db,A,'proposal',opportunity_id=opportunity,requested_minor=amount,brief='Sentetik kapsam')['id']
    execute(db,A,'submit',id=proposal)
    execute(db,F,'decide',id=proposal,decision='approved',reason='Sentetik fiyat kararı',official_minor=amount,discount_minor=0)
    return customer,opportunity,proposal


def collect(db, proposal, period=1, amount=10001, reference=None, collected_on='2026-01-31'):
    return execute(db,F,'collection',proposal_id=proposal,period=period,reference=reference or f'{proposal}-{period}',
                   net_minor=amount,cost_minor=750,collected_on=collected_on,reason='Sentetik dekont kontrolü')['id']


def test_migration_additive_repeatable_and_preserves_existing(store):
    db,path = store
    before = tuple(db.execute('SELECT name,email,secret,role FROM users').fetchone())
    app.init(path)
    assert tuple(db.execute('SELECT name,email,secret,role FROM users').fetchone()) == before
    assert db.execute('SELECT count(*) FROM companies').fetchone()[0] == 2
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []


@pytest.mark.parametrize('actor',[B,C])
def test_owner_and_company_isolation_on_every_resource(store,actor):
    db,_ = store
    customer,opportunity,proposal = setup_sale(db)
    execute(db,A,'activity',opportunity_id=opportunity,note='Özel sentetik görüşme',due_date='2026-11-30')
    collect(db,proposal)
    data = sales.snapshot(db,actor)
    assert all(not data[t] for t in ('customers','opportunities','activities','proposals','collections','commissions','decisions','adjustments'))
    for table,target in [('customers',customer),('opportunities',opportunity),('proposals',proposal)]:
        with pytest.raises(sales.Invalid) as error:
            sales.owned(db,actor,table,target)
        assert error.value.status == 404
    for command,payload in [('opportunity',{'customer_id':customer,'title':'IDOR','kind':'subscription'}),
                            ('activity',{'opportunity_id':opportunity,'note':'IDOR'}),
                            ('proposal',{'opportunity_id':opportunity,'requested_minor':100,'brief':'IDOR'}),
                            ('submit',{'id':proposal})]:
        with pytest.raises(sales.Invalid) as error:
            execute(db,actor,command,**payload)
        assert error.value.status == 404


@pytest.mark.parametrize('command', ['classify','resolve_claim','decide','collection','project_commission','commission_action','hold','reverse','account_create','account_active'])
def test_all_admin_mutations_reject_representatives(store,command):
    db,_ = store
    with pytest.raises(sales.Invalid) as error:
        execute(db,A,command)
    assert error.value.status == 403


def test_duplicate_detection_does_not_disclose_customer_and_control_resolves(store):
    db,_ = store
    original = execute(db,A,'customer',name='Sentetik A',tax_id='1234567890',domain='EXAMPLE.TEST')['id']
    claim = execute(db,B,'customer',name='Sentetik B',domain='example.test')
    assert 'id' not in claim and 'customer_id' not in claim
    assert len(sales.snapshot(db,B)['customers']) == 0
    own_claim = sales.snapshot(db,B)['claims'][0]
    assert 'customer_id' not in own_claim and 'owner_id' not in own_claim
    execute(db,F,'resolve_claim',id=claim['claim_id'],decision='accepted',reason='Sentetik sahiplik kararı')
    assert sales.owned(db,B,'customers',original)['owner_id'] == B.id
    assert sales.snapshot(db,A)['customers'] == []
    second = execute(db,C,'customer',name='Sentetik C',tax_id='1234567890')
    execute(db,B,'opportunity',customer_id=original,title='Kayıtlı fırsat',kind='subscription')
    with pytest.raises(sales.Invalid):
        execute(db,F,'resolve_claim',id=second['claim_id'],decision='accepted',reason='Geçmiş var')
    execute(db,F,'resolve_claim',id=second['claim_id'],decision='rejected',reason='Mevcut sahipte kalır')


def test_proposal_revision_official_price_and_decision_history(store):
    db,_ = store
    c = execute(db,A,'customer',name='Sentetik',domain='revision.test')['id']
    o = execute(db,A,'opportunity',customer_id=c,title='Sentetik',kind='subscription')['id']
    p = execute(db,A,'proposal',opportunity_id=o,requested_minor=10000,brief='<script>sentetik</script>')['id']
    with pytest.raises(sales.Invalid):
        execute(db,F,'decide',id=p,decision='approved',reason='Erken',official_minor=10000)
    execute(db,A,'submit',id=p)
    execute(db,F,'decide',id=p,decision='revision',reason='Kapsamı güncelle')
    p2 = execute(db,A,'proposal',opportunity_id=o,requested_minor=12000,brief='Yeni kapsam')['id']
    execute(db,A,'submit',id=p2)
    execute(db,F,'decide',id=p2,decision='approved',reason='Fiyat kararı',official_minor=15000,discount_minor=1000)
    assert sales.owned(db,A,'proposals',p2)['version'] == 2
    assert sales.owned(db,A,'proposals',p2)['official_minor'] == 15000
    assert len(sales.snapshot(db,A)['decisions']) == 2
    with pytest.raises(sales.Invalid):
        execute(db,F,'decide',id=p2,decision='rejected',reason='Tekrar')


@pytest.mark.parametrize('kind,classification,rates', [
    ('subscription','new',[25,5]),('subscription','existing',[0,0]),
    ('upgrade','new',[0,0]),('addon','existing',[0,0]),('addon','new',[0,0])])
def test_commission_rates_collections_and_provider_costs(store,kind,classification,rates):
    db,_ = store
    _,_,p = setup_sale(db,kind,classification)
    assert sales.snapshot(db,A)['commissions'] == []
    for period,rate in enumerate(rates,1):
        collect(db,p,period)
        row = db.execute('SELECT * FROM sales_commissions ORDER BY rowid DESC LIMIT 1').fetchone()
        assert row['rate'] == rate and row['amount_minor'] == (10001*rate+50)//100
        assert row['status'] == 'pending'
    assert all('cost_minor' not in item for item in sales.snapshot(db,A)['collections'])
    assert all(item['cost_minor'] == 750 for item in sales.snapshot(db,F)['collections'])


def test_only_first_product_subscription_eligible(store):
    db,_ = store
    customer,_,p = setup_sale(db)
    collect(db,p)
    o2 = execute(db,A,'opportunity',customer_id=customer,title='İkinci ürün',kind='subscription')['id']
    p2 = execute(db,A,'proposal',opportunity_id=o2,requested_minor=10001,brief='İkinci kapsam')['id']
    execute(db,A,'submit',id=p2)
    execute(db,F,'decide',id=p2,decision='approved',reason='Sentetik',official_minor=10001)
    cid = collect(db,p2)
    assert db.execute('SELECT rate FROM sales_commissions WHERE collection_id=?',(cid,)).fetchone()[0] == 0
    with pytest.raises(sales.Invalid):
        execute(db,F,'classify',id=customer,classification='existing',reason='Geçmişi değiştirme')


def test_duplicate_out_of_order_collection_and_failed_transaction_leave_no_accrual(store):
    db,_ = store
    _,_,p = setup_sale(db)
    collect(db,p)
    for period in (1,3):
        with pytest.raises(sales.Invalid):
            collect(db,p,period,reference='another')
    with pytest.raises(sqlite3.IntegrityError):
        collect(db,p,2,reference=p+'-1')
    assert db.execute('SELECT count(*) FROM sales_commissions').fetchone()[0] == 1
    assert db.execute('SELECT count(*) FROM sales_collections').fetchone()[0] == 1


def test_project_no_automatic_commission_requires_profit_analysis_and_written_decision(store):
    db,_ = store
    _,_,p = setup_sale(db,'project')
    cid = collect(db,p)
    assert sales.snapshot(db,F)['commissions'] == []
    with pytest.raises(sales.Invalid):
        execute(db,F,'project_commission',collection_id=cid,amount_minor=2000,reason='Eksik analiz')
    execute(db,F,'project_commission',collection_id=cid,amount_minor=2000,reason='Açık sentetik karar',analysis='Maliyet 5000; kalan kâr 5001 kuruş')
    assert db.execute('SELECT amount_minor,status FROM sales_commissions').fetchone()[:] == (2000,'approved')
    with pytest.raises(sqlite3.IntegrityError):
        execute(db,F,'project_commission',collection_id=cid,amount_minor=2000,reason='Tekrar',analysis='Sentetik analiz')


def test_payment_window_approval_refund_hold_and_immutable_reversal(store):
    db,_ = store
    _,_,p = setup_sale(db)
    cid = collect(db,p)
    mid = db.execute('SELECT id FROM sales_commissions').fetchone()[0]
    with pytest.raises(sales.Invalid):
        execute(db,F,'commission_action',id=mid,decision='paid',reason='Onaysız')
    execute(db,F,'commission_action',id=mid,decision='approved',reason='Hak ediş kontrolü')
    for day in (date(2026,1,31),date(2026,2,16)):
        with pytest.raises(sales.Invalid):
            with db:
                sales.commission_action(db,F,{'id':mid,'decision':'paid','reason':'Takvim dışı'},today=day)
    with db:
        sales.commission_action(db,F,{'id':mid,'decision':'paid','reason':'Sentetik ödeme kaydı'},today=date(2026,2,15))
    execute(db,F,'hold',collection_id=cid,reason='Sentetik iade bildirimi')
    with pytest.raises(sales.Invalid):
        execute(db,F,'commission_action',id=mid,decision='paid',reason='Tekrar ödeme')
    execute(db,F,'reverse',id=mid,reason='Kurucu tam iade düzeltme kararı')
    assert db.execute('SELECT amount_minor FROM sales_adjustments').fetchone()[0] == -2500
    assert db.execute('SELECT amount_minor FROM sales_commissions').fetchone()[0] == 2500
    for table in ('sales_audit','sales_decisions','sales_adjustments'):
        for statement in (f'DELETE FROM {table}',f'UPDATE {table} SET id=id'):
            with pytest.raises(sqlite3.IntegrityError):
                with db:
                    db.execute(statement)


@pytest.mark.parametrize('raw',[1.25,True,-1,'NaN','1.00','1000000000001',None])
def test_money_rejects_float_negative_bool_or_unbounded_values(raw):
    with pytest.raises(sales.Invalid):
        sales.integer({'amount':raw},'amount')


def test_year_rollover_and_leap_year_windows():
    assert sales.payment_window('2026-12-31') == (date(2027,1,1),date(2027,1,15))
    assert sales.payment_window('2028-02-29') == (date(2028,3,1),date(2028,3,15))


@pytest.mark.parametrize('value,expected',[('1234,56',123456),('1.234,56',123456),('1234.56',123456),('0,01',1),('0,29',29),('1.000',100000),('10000',1000000)])
def test_tl_input_is_exact_integer_kurus(value,expected):
    assert sales_http.tl_to_minor(value) == expected


@pytest.mark.parametrize('value',['1,005','0,001','1.2345','1e3','-1','NaN','1,234.56',True,0.29,'10000000000,01'])
def test_tl_input_rejects_ambiguous_or_silently_rounded_values(value):
    with pytest.raises(sales.Invalid):
        sales_http.tl_to_minor(value)


def test_tl_form_payload_and_minor_api_share_the_same_rules(store):
    db,_=store
    customer=execute(db,A,'customer',name='TL Sentetik',domain='tl.test')['id']
    opportunity=execute(db,A,'opportunity',customer_id=customer,title='TL fırsat',kind='subscription')['id']
    proposal=execute(db,A,'proposal',opportunity_id=opportunity,requested_tl='1.234,56',brief='TL kapsam')['id']
    assert sales.owned(db,A,'proposals',proposal)['requested_minor'] == 123456
    with pytest.raises(sales.Invalid):
        execute(db,A,'proposal',opportunity_id=opportunity,requested_tl='1,00',requested_minor=100,brief='Çift matrah')


def test_refund_hold_blocks_future_subscription_accrual(store):
    db,_=store
    _,_,p=setup_sale(db)
    cid=collect(db,p)
    execute(db,F,'hold',collection_id=cid,reason='Sentetik iptal')
    with pytest.raises(sales.Invalid):
        collect(db,p,period=2)


class Socket:
    def __init__(self,payload):
        self.payload,self.output = payload,BytesIO()

    def makefile(self,*_):
        return BytesIO(self.payload)

    def sendall(self,data):
        self.output.write(data)


def http(server,path,method='GET',data=None,cookie='',origin='https://ernasa.example.test',csrf=''):
    body = json.dumps(data).encode() if data is not None else b''
    headers = f'Host: ernasa.example.test\r\nCookie: {cookie}\r\nOrigin: {origin}\r\nX-CSRF-Token: {csrf}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n'
    socket = Socket(f'{method} {path} HTTP/1.0\r\n{headers}\r\n'.encode()+body)
    app.Handler(socket,('127.0.0.1',40000),server)
    response = HTTPResponse(Socket(socket.output.getvalue()))
    response.begin()
    return response.status,dict(response.getheaders()),response.read()


@pytest.fixture
def web(store):
    db,path = store
    server = SimpleNamespace(db_path=path,origin='https://ernasa.example.test',public_prefix='/control',attempts=[],login_lock=Lock())
    for actor in (A,B,C):
        db.execute('INSERT INTO sales_sessions VALUES(?,?,?,?)',(app.token_hash(actor.id+'-cookie'),actor.id,'test-csrf',9999999999))
    # Above short cookies intentionally fail validation; real tests use the login flow.
    db.execute('INSERT INTO sessions VALUES(?,?,?,?)',(app.token_hash('founder-cookie'),1,'founder-csrf',9999999999))
    db.commit()
    return server


def sign_in(server,account='rep-a'):
    status,headers,_ = http(server,'/sales/login')
    assert status == 200
    cookie = headers['Set-Cookie'].split(';')[0]
    csrf = cookie.split('=',1)[1]
    status,headers,body = http(server,'/sales/login','POST',{'email':account+'@example.test','password':PASSWORD,'csrf':csrf},cookie)
    assert status == 303,body
    assert 'HttpOnly' in headers['Set-Cookie'] and '; Secure' in headers['Set-Cookie']
    assert 'Path=/control/sales;' in headers['Set-Cookie']
    cookie = headers['Set-Cookie'].split(';')[0]
    status,_,body = http(server,'/sales/api',cookie=cookie)
    assert status == 200
    return cookie,json.loads(body)['csrf']



def test_sales_login_explains_founder_route_and_allows_retry(web):
    status, headers, body = http(web, '/sales/login')
    assert status == 200
    assert b'href="/control/giris"' in body
    assert 'Satış temsilcisi girişi'.encode() in body
    assert b'action="/control/sales/login"' in body
    original_cookie = headers['Set-Cookie'].split(';')[0]
    csrf = original_cookie.split('=', 1)[1]

    status, headers, body = http(web, '/sales/login', 'POST',
                                 {'email':'unknown@example.test','password':'wrong','csrf':csrf},
                                 original_cookie)
    assert status == 401
    assert 'Giriş bilgileri geçersiz.'.encode() in body
    assert b'role="alert"' in body
    assert b'action="/control/sales/login"' in body
    assert b'href="/control/giris"' in body
    new_cookie = headers['Set-Cookie'].split(';')[0]
    assert new_cookie != original_cookie
    assert b'<input name="password"' in body


def test_sales_founder_navigation_without_changing_auth_boundaries(web):
    status,headers,_ = http(web,'/satis')
    assert status == 303
    assert headers['Location'] == '/control/giris'
    status,headers,_ = http(web,'/sales/login',cookie='control_session=founder-cookie')
    assert status == 303
    assert headers['Location'] == '/control/satis'
    status,_,_ = http(web,'/satis',cookie='control_session=founder-cookie')
    assert status == 200


def test_http_auth_roles_csrf_disabled_accounts_and_no_academy_elevation(store,web):
    db,_ = store
    assert http(web,'/sales/api')[0] == 401
    assert http(web,'/sales/api',cookie='ik_session=synthetic-academy-cookie')[0] == 401
    assert http(web,'/sales/api',cookie='sales_session=rep-a-cookie')[0] == 401
    cookie,csrf = sign_in(web)
    assert http(web,'/satis',cookie=cookie)[0] == 403
    payload = {'action':'customer','name':'Sentetik','domain':'http.test'}
    assert http(web,'/sales/api','POST',payload,cookie,csrf='bad')[0] == 403
    assert http(web,'/sales/api','POST',payload,cookie,origin='https://foreign.test',csrf=csrf)[0] == 403
    assert http(web,'/sales/api','POST',payload,cookie,csrf=csrf)[0] == 200
    assert http(web,'/sales/api','POST',{'action':'decide'},cookie,csrf=csrf)[0] == 403
    execute(db,F,'account_active',id=A.id,active=0,reason='Sentetik devre dışı')
    assert http(web,'/sales/api',cookie=cookie)[0] == 401


def test_http_api_and_official_document_isolation_and_html_escape(store,web):
    db,_ = store
    _,_,p = setup_sale(db)
    with db:
        db.execute('UPDATE sales_proposals SET brief=? WHERE id=?',('<script>alert(1)</script>',p))
    a,csrf = sign_in(web)
    b,_ = sign_in(web,'rep-b')
    assert http(web,'/sales/api/record?table=proposals&id='+p,cookie=b)[0] == 404
    assert http(web,'/sales/official?id='+p,cookie=b)[0] == 404
    status,_,body = http(web,'/sales/official?id='+p,cookie=a)
    assert status == 200 and b'<script>alert' not in body and b'&lt;script&gt;' in body
    status,_,body = http(web,'/sales?tab=proposals',cookie=a)
    assert status == 200 and b'/control/control.css' in body and b'/control/ernasalogo.png' in body
    assert b'action="/control/sales?' in body
    for tab in ('overview','customers','opportunities','tasks','finance','resources','profile'):
        assert http(web,'/sales?tab='+tab,cookie=a)[0] == 200


def test_founder_management_navigation_shared_design_and_existing_control_routes(web):
    status,_,page = http(web,'/satis',cookie='control_session=founder-cookie')
    assert status == 200 and b'href="/control/satis"' in page
    assert 'Satış Yönetimi'.encode() in page and 'Temsilci hesabı oluştur'.encode() in page
    for path in ('/','/firmalar','/abonelikler','/kullanicilar','/denetim'):
        assert http(web,path,cookie='control_session=founder-cookie')[0] == 200
    for path in ('/control.css','/design-system.css','/ernasalogo.png'):
        assert http(web,path)[0] == 200




def test_sales_empty_dropdown_has_readable_placeholder():
    html = sales_http.select(app, 'customer_id', 'Müşteri', [])
    assert 'Henüz seçim yapılabilecek kayıt yok'.encode() in html.encode()
    assert '<select name="customer_id" required disabled>' in html
    assert 'disabled selected' in html
    populated = sales_http.select(app, 'customer_id', 'Müşteri', [('c-123','Deneme AŞ')])
    assert 'Seçiniz' in populated
    assert 'value="c-123"' in populated
    assert '<select name="customer_id" required disabled>' not in populated


@pytest.mark.parametrize('route,tab,notice,action', [
    ('/satis?tab=opportunities','opportunities','CONTROL bu adımda kayıt oluşturmaz','opportunity'),
    ('/satis?tab=tasks','tasks','Görüşme ve takip görevlerini satış temsilcisi','activity'),
    ('/satis?tab=proposals','proposals','Teklif taslağını satış temsilcisi','proposal'),
    ('/satis?tab=finance','finance','Henüz CONTROL tarafından onaylanmış teklif yok','collection'),
])
def test_founder_empty_dependencies_do_not_show_action_forms(web,route,tab,notice,action):
    status, _, content = http(web,route,cookie='control_session=founder-cookie')
    assert status == 200
    assert notice.encode() in content
    assert b'role="status"' in content
    assert ('name="action" value="'+action+'"').encode() not in content
    assert b'href="/control/satis?tab=' in content or tab == 'finance'


@pytest.mark.parametrize('route,notice,action', [
    ('/sales?tab=opportunities','Henüz müşteriniz yok','opportunity'),
    ('/sales?tab=tasks','Henüz fırsat kaydı bulunmuyor','activity'),
    ('/sales?tab=proposals','Henüz teklif hazırlanabilecek','proposal'),
])
def test_representative_empty_dependencies_are_guided(web,route,notice,action):
    cookie, _ = sign_in(web)
    status, _, content = http(web,route,cookie=cookie)
    assert status == 200
    assert notice.encode() in content
    assert ('name="action" value="'+action+'"').encode() not in content


def test_sales_cards_and_navigation_are_consistently_grouped(web):
    status, _, body = http(web,'/satis',cookie='control_session=founder-cookie')
    assert status == 200
    assert b'class="sales-hub-ui"' in body
    assert 'Satış ve İş Geliştirme'.encode() in body
    assert 'Yerel servis'.encode() not in body
    assert 'Yönetim paneli'.encode() in body


def test_sales_founder_sections_separated_without_changing_original_ids(web):
    from urllib.parse import quote
    status, _, page = http(web, '/satis', cookie='control_session=founder-cookie')
    assert status == 200
    assert b'href="/control/satis?tab=accounts"' in page
    assert 'Satış Yönetimi bölümleri'.encode() in page
    assert 'Temsilci hesabı oluştur'.encode() in page
    assert 'Tahsilat doğrulama'.encode() not in page
    for tab in ('accounts', 'claims', 'customers', 'opportunities', 'proposals', 'tasks', 'finance', 'audit'):
        status, _, html = http(web, '/satis?tab='+tab, cookie='control_session=founder-cookie')
        assert status == 200, tab
        assert b'aria-current="page"' in html
    assert http(web, '/satis?tab=profile', cookie='control_session=founder-cookie')[0] == 404
    status, headers, _ = http(web, '/satis?tab=finance')
    assert status == 303 and headers['Location'] == '/control/giris'
    cookie, _ = sign_in(web)
    assert http(web, '/satis?tab=finance', cookie=cookie)[0] == 403


def test_sales_compact_reference_and_turkiye_local_date_are_presentation_only():
    raw = 'abcdef0123456789abcdef0123456789'
    assert 'MST-ABCDEF-6789' in sales_http.record_code(app, raw, 'MST')
    assert raw in sales_http.record_code(app, raw, 'MST')
    assert sales_http.local_date('2026-11-19T18:00:00+00:00') == '19.11.2026 21:00 TSİ'
    assert sales_http.local_date('2026-11-19 18:00:00') == '19.11.2026 21:00 TSİ'
    assert sales_http.local_date('2026-11-19') == '19.11.2026'
    assert sales_http.local_date(None) == '—'
    assert sales_http.local_date('invalid') == 'invalid'


def test_sales_form_stays_in_selected_founder_section(web):
    response, _, html = http(web, '/satis?tab=accounts', cookie='control_session=founder-cookie')
    assert response == 200
    assert b'action="/control/satis?tab=accounts"' in html

    # With no approved proposals, do not offer an unusable collection form.
    response, _, html = http(web, '/satis?tab=finance', cookie='control_session=founder-cookie')
    assert response == 200
    assert 'Henüz CONTROL tarafından onaylanmış teklif yok'.encode() in html
    assert b'name="action" value="collection"' not in html

    # An approved proposal restores the normal scoped form/action.
    with closing(app.connect(web.db_path)) as db:
        setup_sale(db)
    response, _, html = http(web, '/satis?tab=finance', cookie='control_session=founder-cookie')
    assert response == 200
    assert b'action="/control/satis?tab=finance"' in html
    assert b'name="action" value="collection"' in html


def test_accounts_are_created_inactive_then_explicitly_activated(store,web):
    db,_ = store
    result = execute(db,F,'account_create',company_id=1,name='Sentetik Yeni Temsilci',email='new@example.test',password=PASSWORD)
    assert result['active'] is False
    execute(db,F,'account_active',id=result['id'],active=1,reason='İnsan tarafından rol onayı')
    assert db.execute('SELECT active FROM sales_accounts WHERE id=?',(result['id'],)).fetchone()[0] == 1


def test_password_change_revokes_every_session(store,web):
    cookie,csrf = sign_in(web)
    payload = {'action':'password','old_password':PASSWORD,'password':'Synthetic-new-password-2026!'}
    assert http(web,'/sales/api','POST',payload,cookie,csrf=csrf)[0] == 200
    assert http(web,'/sales/api',cookie=cookie)[0] == 401


def test_no_approved_proposal_or_classification_no_collection(store):
    db,_ = store
    c = execute(db,A,'customer',name='Sentetik',domain='blocked.test')['id']
    o = execute(db,A,'opportunity',customer_id=c,title='Sentetik',kind='subscription')['id']
    p = execute(db,A,'proposal',opportunity_id=o,requested_minor=10001,brief='Sentetik')['id']
    with pytest.raises(sales.Invalid):
        collect(db,p)
    execute(db,A,'submit',id=p)
    execute(db,F,'decide',id=p,decision='approved',official_minor=10001,reason='Sentetik')
    with pytest.raises(sales.Invalid):
        collect(db,p)
    assert db.execute('SELECT count(*) FROM sales_collections').fetchone()[0] == 0


def test_company_sales_contact_can_start_founder_only_provisioning(store,web):
    """The company contact does not confer login; founder explicitly provisions it."""
    db,_ = store
    from control import firma
    assert 'Satış Temsilcisi' in firma.CONTACTS
    with db:
        db.execute(
            "INSERT INTO company_contacts(company_id,primary_contact,name,title,department,phone,email,kind) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (1,0,'Örnek Temsilci','B2B Satış','Satış','','temsilci@example.test','Satış Temsilcisi'),
        )
        contact_id = db.execute("SELECT id FROM company_contacts WHERE email=?",('temsilci@example.test',)).fetchone()[0]
    route = f'/firmalar?detay=1&sekme=yetkililer'
    status,_,page = http(web,route,cookie='control_session=founder-cookie')
    assert status == 200
    assert 'Satış hesabı aç'.encode() in page
    url = f'/satis?tab=accounts&company_id=1&contact_id={contact_id}'
    status,_,form_page = http(web,url,cookie='control_session=founder-cookie')
    assert status == 200
    assert 'value="Örnek Temsilci"'.encode() in form_page
    assert b'value="temsilci@example.test"' in form_page
    assert b'<option value="1" selected>' in form_page
    assert db.execute("SELECT count(*) FROM sales_accounts WHERE email=?",('temsilci@example.test',)).fetchone()[0] == 0
    assert http(web,url)[0] == 303

    created = execute(db,F,'account_create',company_id=1,name='Örnek Temsilci',
                      email='temsilci@example.test',password=PASSWORD)
    assert created['active'] is False
    status,_,changed = http(web,route,cookie='control_session=founder-cookie')
    assert status == 200
    assert 'Satış hesabı mevcut'.encode() in changed
    assert 'Satış hesabı aç'.encode() not in changed


def test_sales_prefill_cannot_cross_company_or_contact_type(store,web):
    db,_ = store
    with db:
        db.execute(
            "INSERT INTO company_contacts(company_id,primary_contact,name,title,department,phone,email,kind) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (2,0,'İkinci Firma Kişisi','İK','İnsan Kaynakları','','person@example.test','İK'),
        )
        contact_id = db.execute("SELECT id FROM company_contacts WHERE email=?",('person@example.test',)).fetchone()[0]
    for company_id in (1,2):
        status,_,page = http(web,f'/satis?tab=accounts&company_id={company_id}&contact_id={contact_id}',
                             cookie='control_session=founder-cookie')
        assert status == 200
        assert b'value="person@example.test"' not in page
