"""NOVA 2.0 offline checks: synthetic CONTROL data only, no network, fake providers."""
from contextlib import closing
from datetime import datetime
import json

import pytest

from control import app, nova, nova_http, nova_providers, sales

PASSWORD = 'Synthetic-nova-test-2026!'
F = sales.Actor('1', None, True)
REP = sales.Actor('rep-a', 1)
WORK = datetime(2026, 11, 25, 11, 0, tzinfo=nova.ISTANBUL)      # Wednesday 11:00
NIGHT = datetime(2026, 11, 25, 22, 0, tzinfo=nova.ISTANBUL)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / 'synthetic-control.sqlite3'
    app.init(path)
    connection = app.connect(path)
    connection.execute("INSERT INTO companies VALUES(1,'Sentetik ERNASA','Aktif')")
    connection.execute("INSERT INTO users(id,email,name,secret,role) VALUES(1,'founder@example.test','Kurucu',?,'founder')",
                       (app.digest(PASSWORD),))
    connection.execute('INSERT INTO sales_accounts VALUES(?,?,?,?,?,1)', ('rep-a', 1, 'Temsilci A', 'rep-a@example.test', app.digest(PASSWORD)))
    connection.execute('INSERT INTO sales_accounts VALUES(?,?,?,?,?,1)', ('rep-b', 1, 'Temsilci B', 'rep-b@example.test', app.digest(PASSWORD)))
    connection.commit()
    yield connection
    connection.close()


def run(db, fn, *args, **kwargs):
    with db:
        db.execute('BEGIN IMMEDIATE')
        return fn(db, *args, **kwargs)


def sample(n=1, **extra):
    row = {'legal_name': f'Sentetik Lojistik {n} A.Ş.', 'sector': 'Lojistik', 'city': 'Kocaeli', 'district': 'Gebze',
           'website': f'https://www.sentetik{n}.example', 'email': f'ik@sentetik{n}.example',
           'email_source_url': f'https://www.sentetik{n}.example/iletisim', 'phone': '0262 000 00 0' + str(n % 10),
           'phone_source_url': f'https://www.sentetik{n}.example/iletisim', 'open_jobs': 'İK Uzmanı',
           'job_source_url': f'https://www.sentetik{n}.example/kariyer', 'job_seen_on': '2026-11-20',
           'workload_terms': 'CV tarama; mülakat planlama', 'other_open_jobs': 3, 'employee_count': '250-300',
           'source_url': f'https://www.sentetik{n}.example', 'verified_on': '2026-11-27'}
    row.update(extra)
    return row


def open_channels(db, email=True, call=False):
    run(db, nova.update_settings, F, {'kill_switch': '0', 'running': '1', 'email_policy_approved': '1' if email else '0',
                                       'call_policy_approved': '1' if call else '0'})


class FakeTransport:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, recipient, subject, text, key=''):
        if self.fail:
            raise nova_providers.ProviderError('Sağlayıcı reddetti')
        self.sent.append((recipient, subject, text, key))
        return 'fake', f'msg-{len(self.sent)}'


class FakeVoice:
    def __init__(self):
        self.calls = []

    def start_call(self, phone, context, max_seconds, metadata=None):
        self.calls.append((phone, context, max_seconds))
        return 'vapi', f'call-{len(self.calls)}'


# ---------------------------------------------------------------- schema

def test_schema_additive_repeatable_and_closed_by_default(db, tmp_path):
    before = db.execute('SELECT count(*) FROM sales_accounts').fetchone()[0]
    app.init(tmp_path / 'synthetic-control.sqlite3')
    assert db.execute('SELECT count(*) FROM sales_accounts').fetchone()[0] == before
    cfg = nova.settings(db)
    assert cfg['kill_switch'] == 1 and cfg['running'] == 0 and cfg['auto_email'] == 0 and cfg['auto_call'] == 0
    assert cfg['email_policy_approved'] == 0 and cfg['call_policy_approved'] == 0 and cfg['require_iys'] == 1
    assert cfg['daily_email_limit'] == 10 and cfg['daily_call_limit'] == 5 and cfg['max_call_seconds'] == 300
    assert cfg['daily_research_target'] == 20


def test_pg_migration_files_use_own_family_and_names():
    for text in (nova.DDL, nova.PG_GUARDS):
        assert 'DROP' not in text.upper().replace('DROP TRIGGER', '')
        for statement in text.split(';'):
            if 'CREATE TABLE' in statement:
                assert 'nova_' in statement
    assert 'sales_reject_history_mutation()' not in nova.PG_GUARDS.split('$$')[0]


# ---------------------------------------------------------------- data pool

def test_verified_company_scored_and_duplicates_blocked(db):
    result = run(db, nova.add_company, F, sample(1))
    assert result['stage'] == 'verified' and result['score'] > 0
    c = nova.company(db, result['id'])
    assert c['suggested_package'] == 'Growth' and c['domain'] == 'sentetik1.example' and c['phone'] == '+902620000001'
    assert run(db, nova.add_company, F, sample(1, legal_name='Başka Unvan'))['matched_on'] == 'alan adı'
    assert run(db, nova.add_company, F, sample(2, website='', email='', legal_name='Sentetik Lojistik 1 Anonim Şirketi',
                                                phone='', phone_source_url='', email_source_url=''))['matched_on'] == 'unvan ve il'
    assert db.execute('SELECT count(*) FROM nova_companies').fetchone()[0] == 1


def test_no_evidence_means_no_points_and_not_verified(db):
    result = run(db, nova.add_company, F, {'legal_name': 'Kanıtsız Firma', 'city': 'Ankara'})
    assert result['stage'] == 'researching' and result['score'] == 0
    with pytest.raises(sales.Invalid):
        run(db, nova.transition, 'x', result['id'], 'verified')


@pytest.mark.parametrize('field,value', [
    ('email', 'ahmet.yilmaz@sentetik1.example'),   # personal mailbox pattern
    ('email', 'ik.sentetik@gmail.com'),             # free-mail provider
    ('email', 'ik@baska-firma.example'),            # domain does not match website
    ('phone', '0532 000 00 00'),                    # mobile = personal data
    ('linkedin_url', 'https://www.linkedin.com/in/kisi'),
])
def test_personal_or_unsourced_data_rejected(db, field, value):
    with pytest.raises(sales.Invalid):
        run(db, nova.add_company, F, sample(1, **{field: value}))


def test_contact_requires_source_url(db):
    with pytest.raises(sales.Invalid):
        run(db, nova.add_company, F, sample(1, email_source_url=''))


def test_import_reports_created_duplicates_rejected(db):
    rows = [sample(1), sample(1), sample(2, phone='05321112233'), 'x', sample(3)]
    result = run(db, nova.import_companies, F, rows)
    assert len(result['created']) == 2 and len(result['duplicates']) == 1 and len(result['rejected']) == 2
    with pytest.raises(sales.Invalid):
        run(db, nova.import_companies, REP, rows)


# ---------------------------------------------------------------- objections

def test_objection_is_permanent_and_cross_channel(db):
    target = run(db, nova.add_company, F, sample(1))['id']
    run(db, nova.draft_email, F, {'company_id': target, 'subject': 'Tanışma', 'body': 'Merhaba'})
    run(db, nova.suppress, 'test', 'domain', 'https://www.sentetik1.example', 'Talep', 'test')
    c = nova.company(db, target)
    assert c['stage'] == 'do_not_contact' and c['consent_status'] == 'objected'
    assert db.execute("SELECT status FROM nova_emails").fetchone()['status'] == 'cancelled'
    for action in (lambda: run(db, nova.transition, 'x', target, 'contactable'),
                   lambda: run(db, nova.draft_email, F, {'company_id': target, 'subject': 'Yeni', 'body': 'x', 'campaign': 'b'}),
                   lambda: run(db, nova.plan_call, F, {'company_id': target})):
        with pytest.raises(sales.Invalid):
            action()
    with pytest.raises(Exception):
        run(db, lambda d: d.execute('DELETE FROM nova_suppressions'))
    with pytest.raises(Exception):
        run(db, lambda d: d.execute('UPDATE nova_events SET detail=?', ('x',)))
    # A re-import of the same domain is stored as do-not-contact immediately.
    db.execute("DELETE FROM nova_emails")
    db.commit()
    again = run(db, nova.add_company, F, sample(1, website='https://www.sentetik1.example', legal_name='Yeni Unvan', city='Bursa'))
    assert 'duplicate' in again


# ---------------------------------------------------------------- e-mail

def test_email_flow_gates_limits_and_dedupe(db, monkeypatch):
    target = run(db, nova.add_company, F, sample(1))['id']
    draft = run(db, nova.draft_email, F, {'company_id': target, 'subject': 'İK ilanınız hakkında', 'body': 'Merhaba, ...'})
    body = db.execute('SELECT body FROM nova_emails WHERE id=?', (draft['id'],)).fetchone()['body']
    assert 'LİSTEDEN ÇIKAR' in body and 'iletisim@ernasa.com' in body and 'yapay zekâ' in body
    with pytest.raises(sales.Invalid):
        run(db, nova.draft_email, F, {'company_id': target, 'subject': 'Tekrar', 'body': 'x'})
    with pytest.raises(sales.Invalid):
        run(db, nova.approve_email, REP, {'id': draft['id']})
    transport = FakeTransport()
    run(db, nova.approve_email, F, {'id': draft['id']})
    # Kill switch on by default: nothing leaves.
    assert run(db, nova.dispatch_emails, transport, WORK)[0]['status'] == 'held' and not transport.sent
    open_channels(db)
    assert run(db, nova.dispatch_emails, transport, NIGHT)[0]['status'] == 'held'
    held = run(db, nova.dispatch_emails, transport, WORK)
    assert held[0]['status'] == 'held' and 'İYS' in held[0]['reason']
    run(db, nova.mark_iys, F, {'id': target, 'result': 'clear', 'checked_on': '2026-11-25'})
    assert run(db, nova.dispatch_emails, transport, WORK)[0]['status'] == 'sent'
    assert len(transport.sent) == 1 and transport.sent[0][3] == 'nova-' + draft['id']
    assert nova.company(db, target)['stage'] == 'awaiting_reply'
    assert run(db, nova.dispatch_emails, transport, WORK) == []          # never sent twice


def test_daily_email_limit(db):
    open_channels(db)
    run(db, nova.update_settings, F, {'daily_email_limit': '2'})
    transport = FakeTransport()
    for n in range(1, 5):
        target = run(db, nova.add_company, F, sample(n))['id']
        run(db, nova.mark_iys, F, {'id': target, 'result': 'clear'})
        run(db, nova.approve_email, F, {'id': run(db, nova.draft_email, F, {'company_id': target, 'subject': 'S', 'body': 'B'})['id']})
    outcome = run(db, nova.dispatch_emails, transport, WORK)
    assert [o['status'] for o in outcome].count('sent') == 2 and len(transport.sent) == 2


def test_auto_mode_sends_drafts_only_after_policy(db):
    target = run(db, nova.add_company, F, sample(1))['id']
    run(db, nova.mark_iys, F, {'id': target, 'result': 'clear'})
    run(db, nova.draft_email, F, {'company_id': target, 'subject': 'S', 'body': 'B'})
    run(db, nova.update_settings, F, {'kill_switch': '0', 'running': '1'})
    transport = FakeTransport()
    assert run(db, nova.dispatch_emails, transport, WORK) == []           # draft needs approval
    open_channels(db)
    run(db, nova.update_settings, F, {'auto_email': '1'})
    assert run(db, nova.dispatch_emails, transport, WORK)[0]['status'] == 'sent'
    assert db.execute("SELECT approved_by FROM nova_emails").fetchone()['approved_by'] == 'nova-auto'


def test_forbidden_claims_rejected(db):
    target = run(db, nova.add_company, F, sample(1))['id']
    for body in ('Size %20 indirim yapabiliriz.', 'ISO 27001 sertifikamız var, sertifikalıyız.', 'Personelinizin yerini alır.'):
        with pytest.raises(sales.Invalid):
            run(db, nova.draft_email, F, {'company_id': target, 'subject': 'S', 'body': body})


def test_provider_failure_is_not_shown_as_sent(db):
    open_channels(db)
    target = run(db, nova.add_company, F, sample(1))['id']
    run(db, nova.mark_iys, F, {'id': target, 'result': 'clear'})
    run(db, nova.approve_email, F, {'id': run(db, nova.draft_email, F, {'company_id': target, 'subject': 'S', 'body': 'B'})['id']})
    assert run(db, nova.dispatch_emails, FakeTransport(fail=True), WORK)[0]['status'] == 'failed'
    assert nova.metrics(db)['sent'] == 0


def test_test_address_can_be_used_before_policy_approval(db, monkeypatch):
    monkeypatch.setenv('ERNASA_NOVA_TEST_EMAILS', 'ik@sentetik1.example')
    run(db, nova.update_settings, F, {'kill_switch': '0', 'running': '1'})
    target = run(db, nova.add_company, F, sample(1), synthetic=True)['id']
    run(db, nova.approve_email, F, {'id': run(db, nova.draft_email, F, {'company_id': target, 'subject': 'S', 'body': 'B'})['id']})
    transport = FakeTransport()
    assert run(db, nova.dispatch_emails, transport, WORK)[0]['status'] == 'sent'


# ---------------------------------------------------------------- inbound

def inbound(db, target_email, body, subject='Re: Tanışma', msg='m1', sender=None, original=''):
    return run(db, nova.record_inbound, {'provider_id': msg, 'from': sender or target_email, 'subject': subject, 'body': body,
                                         'original_recipient': original})


def test_reply_classification_and_consequences(db):
    a = run(db, nova.add_company, F, sample(1))['id']
    b = run(db, nova.add_company, F, sample(2))['id']
    c = run(db, nova.add_company, F, sample(3))['id']
    for target in (a, b):
        run(db, nova.transition, 'x', target, 'contactable')
        run(db, nova.transition, 'x', target, 'email_draft')
        run(db, nova.transition, 'x', target, 'email_sent')
    assert inbound(db, 'ik@sentetik1.example', 'Merhaba, demo görmek isteriz. Bizi arayabilirsiniz.')['classification'] == 'demo'
    assert nova.company(db, a)['stage'] == 'demo_requested' and nova.company(db, a)['call_permission']
    assert inbound(db, 'ik@sentetik1.example', 'x', msg='m1')['status'] == 'duplicate'
    assert inbound(db, 'ik@sentetik2.example', 'Lütfen listeden çıkarın.', msg='m2')['classification'] == 'objection'
    assert nova.company(db, b)['stage'] == 'do_not_contact'
    out = inbound(db, 'x', 'Delivery Status Notification (Failure) address not found', subject='Undeliverable', msg='m3',
                  sender='mailer-daemon@mail.example', original='ik@sentetik3.example')
    assert out['classification'] == 'bounce' and nova.company(db, c)['email_invalid'] == 1
    assert nova.classify('', 'Şu an izindeyim, otomatik yanıt') == 'auto_reply'
    assert nova.classify('', 'Fiyat bilgisi alabilir miyiz?') == 'interested'
    assert db.execute("SELECT count(*) FROM nova_demo_requests").fetchone()[0] == 1


# ---------------------------------------------------------------- calls

def test_founder_test_call_flow_records_cost_and_summary(db):
    with pytest.raises(sales.Invalid):
        run(db, nova.plan_call, F, {'purpose': 'founder_test', 'phone': '+905320000000'})
    with pytest.raises(sales.Invalid):
        run(db, nova.authorize_test_number, F, {'phone': '+905320000000'})
    run(db, nova.authorize_test_number, F, {'phone': '0532 000 00 00', 'confirm': 'EVET'})
    call = run(db, nova.plan_call, F, {'purpose': 'founder_test', 'phone': '+905320000000'})['id']
    voice = FakeVoice()
    with pytest.raises(sales.Invalid):                 # kill switch still on
        run(db, nova.start_call, call, voice, WORK)
    run(db, nova.update_settings, F, {'kill_switch': '0', 'running': '1'})   # no call policy needed for the founder test line
    started = run(db, nova.start_call, call, voice, WORK)
    phone, context, seconds = voice.calls[0]
    assert phone == '+905320000000' and seconds == 300
    assert context['first_message'].startswith("Merhaba, ben NOVA, ERNASA'nın yapay zekâ destekli satış asistanıyım")
    assert 'indirim' in context['system'] and 'Starter' in context['system']        # approved prices included
    assert nova.spent(db, day=nova.today(WORK)) > 0                                   # reservation held
    result = run(db, nova.record_call_result, 'vapi', started['provider_call_id'],
                 {'duration_seconds': 184, 'cost_minor': 1250, 'summary': 'Kurucuya HR FLOW tanıtıldı', 'interest': 'yüksek',
                  'transcript': 'tam metin', 'recording_consent_confirmed': False})
    assert result['cost_minor'] == 1250
    row = db.execute('SELECT * FROM nova_calls WHERE id=?', (call,)).fetchone()
    assert row['status'] == 'completed' and row['transcript'] is None and row['summary']
    assert nova.spent(db, day=nova.today()) >= 1250
    assert run(db, nova.record_call_result, 'vapi', started['provider_call_id'], {})['duplicate']


def test_prospect_call_requires_policy_or_written_permission_and_respects_limits(db):
    target = run(db, nova.add_company, F, sample(1))['id']
    run(db, nova.mark_iys, F, {'id': target, 'result': 'clear'})
    with pytest.raises(sales.Invalid):
        run(db, nova.plan_call, F, {'company_id': target})
    db.execute("UPDATE nova_companies SET call_permission='E-posta m1' WHERE id=?", (target,))
    db.commit()
    with pytest.raises(sales.Invalid):                 # representative cannot plan while auto-call is off
        run(db, nova.plan_call, REP, {'company_id': target})
    call = run(db, nova.plan_call, F, {'company_id': target})['id']
    with pytest.raises(sales.Invalid):                 # second concurrent call for the same company
        run(db, nova.plan_call, F, {'company_id': target})
    voice = FakeVoice()
    open_channels(db, email=False, call=False)
    with pytest.raises(sales.Invalid):                 # call policy not approved -> no real dial
        run(db, nova.start_call, call, voice, WORK)
    open_channels(db, email=False, call=True)
    run(db, nova.update_settings, F, {'daily_spend_limit_minor': '100'})
    with pytest.raises(sales.Invalid):                 # spend limit
        run(db, nova.start_call, call, voice, WORK)
    run(db, nova.update_settings, F, {'daily_spend_limit_minor': '20000'})
    started = run(db, nova.start_call, call, voice, WORK)
    run(db, nova.record_call_result, 'vapi', started['provider_call_id'],
        {'duration_seconds': 60, 'summary': 'Yapay zekâ ile görüşmek istemedi', 'do_not_call': True, 'status': 'declined_ai'})
    assert nova.company(db, target)['stage'] == 'do_not_contact'
    assert db.execute("SELECT 1 FROM nova_suppressions WHERE kind='phone'").fetchone()


def test_emergency_stop_cancels_planned_calls(db):
    run(db, nova.authorize_test_number, F, {'phone': '+905320000000', 'confirm': 'EVET'})
    call = run(db, nova.plan_call, F, {'purpose': 'founder_test', 'phone': '+905320000000'})['id']
    open_channels(db, call=True)
    run(db, nova.update_settings, F, {'auto_email': '1'})
    run(db, nova.emergency_stop, F)
    cfg = nova.settings(db)
    assert cfg['kill_switch'] == 1 and cfg['running'] == 0 and cfg['auto_email'] == 0
    assert db.execute('SELECT status FROM nova_calls WHERE id=?', (call,)).fetchone()['status'] == 'cancelled'
    with pytest.raises(sales.Invalid):
        run(db, nova.update_settings, F, {'running': '1'})


def test_settings_validation_and_founder_only(db):
    with pytest.raises(sales.Invalid):
        run(db, nova.update_settings, REP, {'daily_email_limit': '5'})
    with pytest.raises(sales.Invalid):
        run(db, nova.update_settings, F, {'auto_email': '1'})          # policy not approved
    with pytest.raises(sales.Invalid):
        run(db, nova.update_settings, F, {'daily_email_limit': '9999'})
    with pytest.raises(sales.Invalid):
        run(db, nova.update_settings, F, {'work_start': '25:00'})


# ---------------------------------------------------------------- Sales Hub

def test_assign_creates_sales_hub_customer_and_never_steals(db):
    target = run(db, nova.add_company, F, sample(1))['id']
    result = run(db, nova.assign, F, {'id': target, 'account_id': 'rep-a'})
    customer = db.execute('SELECT * FROM sales_customers WHERE id=?', (result['sales_customer_id'],)).fetchone()
    assert customer['owner_id'] == 'rep-a' and customer['identity_key'] == 'domain:sentetik1.example'
    assert customer['classification'] == 'unverified'           # commission rules stay with CONTROL classification
    assert db.execute("SELECT 1 FROM sales_audit WHERE event='nova_customer_assigned'").fetchone()
    with pytest.raises(sales.Invalid):
        run(db, nova.assign, F, {'id': target, 'account_id': 'rep-b'})
    other = run(db, nova.add_company, F, sample(2))['id']
    with db:
        db.execute('BEGIN IMMEDIATE')
        sales.customer(db, sales.Actor('rep-b', 1), {'name': 'Var olan', 'domain': 'sentetik2.example'})
    with pytest.raises(sales.Invalid):
        run(db, nova.assign, F, {'id': other, 'account_id': 'rep-a'})
    assert db.execute('SELECT count(*) FROM sales_commissions').fetchone()[0] == 0


def test_lock_prevents_parallel_work(db):
    target = run(db, nova.add_company, F, sample(1))['id']
    run(db, nova.lock, target, 'rep-a')
    with pytest.raises(sales.Invalid):
        run(db, nova.lock, target, 'rep-b')
    run(db, nova.unlock, target, 'rep-a')
    run(db, nova.lock, target, 'rep-b')


# ---------------------------------------------------------------- knowledge / reports / queue

def test_knowledge_seed_and_approval(db):
    run(db, nova.seed_knowledge)
    run(db, nova.seed_knowledge)
    approved = nova.approved_knowledge(db)
    assert any('Starter' in k['question'] for k in approved)
    assert not any(k['topic'] == 'genel' for k in approved)       # features wait for founder approval
    draft = db.execute("SELECT id FROM nova_knowledge WHERE topic='genel'").fetchone()['id']
    run(db, nova.knowledge_command, F, {'knowledge_action': 'approve', 'id': draft})
    assert any(k['topic'] == 'genel' for k in nova.approved_knowledge(db))
    with pytest.raises(sales.Invalid):
        run(db, nova.knowledge_command, F, {'product': 'hr_flow', 'topic': 't', 'question': 'q', 'answer': 'Garantiliyoruz', 'source': 's'})
    with pytest.raises(sales.Invalid):
        run(db, nova.knowledge_command, REP, {'knowledge_action': 'approve_verified'})
    assert run(db, nova.knowledge_command, F, {'knowledge_action': 'approve_verified'})['approved'] >= 8
    waiting = db.execute("SELECT count(*) FROM nova_knowledge WHERE status='draft' AND source LIKE 'politika bekliyor%'").fetchone()[0]
    assert waiting == 3 and not any(k['topic'] == 'deneme' for k in nova.approved_knowledge(db))


def test_report_and_metrics(db):
    run(db, nova.add_company, F, sample(1))
    run(db, nova.add_company, F, sample(2), synthetic=True)
    report = run(db, nova.daily_report)
    assert 'Doğrulanmış: 1 / hedef 100' in report and 'ölçülemedi' in report
    assert db.execute('SELECT count(*) FROM nova_reports').fetchone()[0] == 1


def test_job_queue_retries_with_backoff(db):
    job = run(db, nova.enqueue, 'research', {'n': 1}, 'k1')
    run(db, nova.enqueue, 'research', {'n': 1}, 'k1')
    assert db.execute('SELECT count(*) FROM nova_jobs').fetchone()[0] == 1
    claimed = run(db, nova.claim_job)
    assert claimed['id'] == job
    for _ in range(3):
        run(db, nova.finish_job, claimed, 'hata')
        claimed = dict(db.execute('SELECT * FROM nova_jobs WHERE id=?', (job,)).fetchone())
    assert claimed['status'] in ('queued', 'failed')


# ---------------------------------------------------------------- providers

def test_vapi_webhook_parsing_and_secret(monkeypatch):
    monkeypatch.setenv('ERNASA_NOVA_VAPI_WEBHOOK_SECRET', 'x' * 32)
    assert nova_providers.VapiVoice.verify({'x-vapi-secret': 'x' * 32})
    assert not nova_providers.VapiVoice.verify({'x-vapi-secret': 'wrong'})
    monkeypatch.setenv('ERNASA_NOVA_USD_TRY', '50')
    call_id, result = nova_providers.VapiVoice.parse_webhook({'message': {
        'type': 'end-of-call-report', 'call': {'id': 'c1'}, 'durationSeconds': 120, 'cost': 0.3, 'endedReason': 'customer-ended-call',
        'analysis': {'summary': 'Özet', 'structuredData': {'interest': 'orta', 'demo_requested': True}}}})
    assert call_id == 'c1' and result['cost_minor'] == 1500 and result['demo_requested'] and not result['recording_consent_confirmed']
    assert nova_providers.VapiVoice.parse_webhook({'message': {'type': 'status-update'}}) == (None, None)


def test_providers_not_configured_without_keys(monkeypatch):
    for name in ('ERNASA_NOVA_VAPI_API_KEY', 'ERNASA_NOVA_IMAP_USER', 'ERNASA_NOVA_IMAP_PASSWORD'):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(nova_providers.NotConfigured):
        nova_providers.VapiVoice().start_call('+902620000000', {'first_message': '', 'system': ''}, 60)
    with pytest.raises(nova_providers.NotConfigured):
        nova_providers.Mailbox().fetch()
    assert not nova_providers.status()['voice']['ready']


def test_bounce_parser():
    raw = (b'From: Mail Delivery System <mailer-daemon@mx.example>\r\nSubject: Undelivered Mail\r\nMessage-ID: <b1@mx>\r\n'
           b'X-Failed-Recipients: ik@sentetik3.example\r\n\r\nDelivery has failed.')
    parsed = nova_providers.parse_message(raw)
    assert parsed['original_recipient'] == 'ik@sentetik3.example' and parsed['provider_id'] == '<b1@mx>'


# ---------------------------------------------------------------- HTTP

def test_http_panel_founder_only_and_csrf(tmp_path):
    from http.client import HTTPConnection
    from threading import Thread
    from urllib.parse import urlencode
    path = tmp_path / 'http.sqlite3'
    app.init(path)
    with closing(app.connect(path)) as db, db:
        db.execute("INSERT INTO users(id,email,name,secret,role) VALUES(1,'founder@example.test','Kurucu',?,'founder')", (app.digest(PASSWORD),))
    with app.server(path, 0) as httpd:
        Thread(target=httpd.serve_forever, daemon=True).start()
        host, port = httpd.server_address
        def request(method, url, body=None, headers=None):
            conn = HTTPConnection(host, port, timeout=10)
            conn.request(method, url, body, {'Host': f'{host}:{port}', **(headers or {})})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        assert request('GET', '/nova')[0] == 303
        assert request('GET', '/nova/api')[0] == 403
        assert request('POST', '/nova/hook/tick', b'{}', {'Content-Type': 'application/json'})[0] == 403
        assert request('POST', '/nova/hook/vapi', b'{}', {'Content-Type': 'application/json'})[0] == 403
        status, headers, page = request('GET', '/giris')
        csrf = page.split('name="csrf" value="')[1].split('"')[0]
        cookie = headers['Set-Cookie'].split(';')[0]
        form = urlencode({'csrf': csrf, 'email': 'founder@example.test', 'password': PASSWORD}).encode()
        status, headers, _ = request('POST', '/giris', form, {'Content-Type': 'application/x-www-form-urlencoded', 'Cookie': cookie,
                                                              'Origin': httpd.origin})
        session = headers['Set-Cookie'].split(';')[0]
        status, _, page = request('GET', '/nova', headers={'Cookie': session})
        assert status == 200 and 'NOVA AI SALES CENTER' in page and 'TÜM DIŞ İLETİŞİMİ DURDUR' in page
        for tab in ('pool', 'emails', 'calls', 'knowledge', 'settings', 'report'):
            assert request('GET', '/nova?tab=' + tab, headers={'Cookie': session})[0] == 200
        bad = urlencode({'csrf': 'wrong', 'action': 'emergency_stop'}).encode()
        assert request('POST', '/nova', bad, {'Content-Type': 'application/x-www-form-urlencoded', 'Cookie': session,
                                              'Origin': httpd.origin})[0] == 403
        snapshot = json.loads(request('GET', '/nova/api', headers={'Cookie': session})[2])
        assert snapshot['settings']['kill_switch'] == 1
        good_csrf = page.split('name="csrf" value="')[1].split('"')[0]
        rows = json.dumps([sample(7)])
        body = urlencode({'csrf': good_csrf, 'action': 'import', 'rows': rows}).encode()
        assert request('POST', '/nova?tab=pool', body, {'Content-Type': 'application/x-www-form-urlencoded', 'Cookie': session,
                                                       'Origin': 'https://evil.example'})[0] == 403
        status, _, page = request('POST', '/nova?tab=pool', body, {'Content-Type': 'application/x-www-form-urlencoded',
                                                                   'Cookie': session, 'Origin': httpd.origin})
        assert status == 200 and '1 yeni' in page
        status, headers, csv_body = request('GET', '/nova/havuz.csv', headers={'Cookie': session})
        assert status == 200 and 'sentetik7.example' in csv_body
        httpd.shutdown()
