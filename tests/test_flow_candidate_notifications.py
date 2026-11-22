from datetime import datetime, timedelta, timezone
import pytest
from flow_candidate_notifications import EVENTS, notice_for, status_update_due, hr_reminder_due


def test_all_events_generate_nonempty_copy_and_stable_deduplication():
    for event in EVENTS:
        a = notice_for(company_id='1', application_id='app-1', event=event, occurrence='event-1')
        b = notice_for(company_id='1', application_id='app-1', event=event, occurrence='event-1')
        assert a == b
        assert a.subject and a.message
        assert len(a.dedupe_key) == 64


def test_cross_tenant_deduplication_is_distinct():
    kw = dict(application_id='same-id', event='application_received', occurrence='first')
    assert notice_for(company_id='A', **kw).dedupe_key != notice_for(company_id='B', **kw).dedupe_key


def test_distinct_occurrences_are_distinct():
    kw = dict(company_id='A', application_id='x', event='awaiting_hr_update')
    assert notice_for(occurrence='2026-W47', **kw).dedupe_key != notice_for(occurrence='2026-W48', **kw).dedupe_key


def test_unknown_event_rejected():
    with pytest.raises(ValueError):
        notice_for(company_id='1', application_id='2', event='unknown', occurrence='3')


def test_no_final_decision_implied_by_screening_message():
    content = EVENTS['screening_completed'][1].lower()
    assert 'ret kararı değildir' in content


def test_due_after_seven_days_but_not_after_final_decision():
    now = datetime(2026, 11, 22, tzinfo=timezone.utc)
    earlier = now - timedelta(days=8)
    assert status_update_due(now=now, last_notice_at=earlier, decision_final=False)
    assert not status_update_due(now=now, last_notice_at=earlier, decision_final=True)
    assert not status_update_due(now=now, last_notice_at=now, decision_final=False)


def test_hr_reminder_after_fourteen_days():
    now = datetime(2026, 11, 22, tzinfo=timezone.utc)
    assert hr_reminder_due(now=now, awaiting_since=now-timedelta(days=14), decision_final=False)
    assert not hr_reminder_due(now=now, awaiting_since=now-timedelta(days=13), decision_final=False)


def test_timezone_required():
    with pytest.raises(ValueError):
        status_update_due(now=datetime.now(), last_notice_at=datetime.now(), decision_final=False)
