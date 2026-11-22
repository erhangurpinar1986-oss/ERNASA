"""ERNASA HR FLOW tam otomasyon (Issue #65): sentetik adaylarla uçtan uca doğrulama.

Gerçek backend/main, ATS, takvim, mülakat ve İK Karar Merkezi kodu çalışır. Dış servisler
(CONTROL oturumu, e-posta sağlayıcısı, OpenAI) sentetik yanıtlarla değiştirilir.
Gerçek adaya e-posta, üretim veritabanı veya Render kullanılmaz.
"""
import json
import re
from contextlib import closing
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from fastapi import HTTPException

import ats
import database
import hr_flow_automation as auto
import hr_flow_decisions
from tests.test_ats_public_cv import intake  # noqa: F401
from tests.test_hr_flow_decision_center import SYNTHETIC_REPORT, flow  # noqa: F401

pytestmark = pytest.mark.parametrize("intake", ["sqlite", "postgres"], indirect=True)
ALL_DAY = auto.AutomationConfig(enabled=True, slot_weekdays=[1, 2, 3, 4, 5, 6, 7], slot_start="00:00",
                                slot_end="23:30", slot_minutes=30)


def _db():
    return closing(database.get_connection())


def _one(sql, args=()):
    with _db() as db:
        row = db.cursor().execute(sql, args).fetchone()
        return dict(row) if row else None


def _all(sql, args=()):
    with _db() as db:
        return [dict(r) for r in db.cursor().execute(sql, args).fetchall()]


def _exec(sql, args=()):
    with _db() as db:
        db.cursor().execute(sql, args)
        db.commit()


@pytest.fixture
def automated(flow, monkeypatch):  # noqa: F811
    """Pozisyon kriterleri tanımlı ve otomasyon İK yöneticisi tarafından bir kez açılmış."""
    import hr_flow_booking
    import hr_flow_screening
    monkeypatch.setenv("ERNASA_FLOW_TRACKING_KEY", "synthetic-tracking-key-0123456789abcdef")
    main, actor, client = flow["main"], flow["actor"], flow["client"]
    position = _one("SELECT id FROM company_positions WHERE company_id=?", (actor.company_id,))["id"]
    fields = json.loads(_one("SELECT fields FROM ats_position_profiles WHERE position_id=?", (position,))["fields"])
    fields["matching_criteria"] = "Yetkinlik | Depo deneyimi | 80 | depo"
    _exec("UPDATE ats_position_profiles SET fields=? WHERE position_id=?", (json.dumps(fields), position))
    config = auto.configure(actor, position, ALL_DAY)
    assert config["enabled"] and config["notice_days"] == 7 and config["hr_reminder_days"] == 14
    job = _one("SELECT id FROM ats_jobs WHERE company_id=?", (actor.company_id,))["id"]
    pdf = flow["pdf"] if "pdf" in flow else None
    sent, availability = [], []

    def send_notice(recipient, subject, body, *, idempotency_key):
        sent.append({"to": recipient, "subject": subject, "body": body, "key": idempotency_key})
        return "synthetic-notice-" + uuid4().hex[:8]

    def send_availability(candidate, url, **kwargs):
        availability.append((candidate["email"], url))
        flow["outgoing"].append(("availability", candidate["email"], url))
        return "synthetic-availability-" + uuid4().hex[:6]

    def apply(name, email, cv=None):
        from tests.test_ats_public_cv import pymupdf
        if cv is None:
            doc = pymupdf.open()
            doc.new_page().insert_text((72, 100), "Sentetik aday: lojistik depo ve WMS deneyimi.")
            cv = doc.tobytes()
            doc.close()
        result = client.post("/api/ats/jobs/" + job + "/apply-with-cv",
                             data={"name": name, "email": email, "notice_version": "v1", "acknowledge": "true",
                                   "source": "direct"},
                             files={"cv": ("sentetik_cv.pdf", cv, "application/pdf")})
        assert result.status_code == 201, result.text
        app = _one("""SELECT a.id FROM ats_applications a JOIN ats_candidates c ON c.id=a.candidate_id
            WHERE c.email=? AND a.company_id=?""", (email, actor.company_id))
        return app["id"]

    def screen():
        while hr_flow_screening.process_one(main.UPLOAD_DIRECTORY):
            pass

    def book_worker():
        while hr_flow_booking.process_one(main.start_booked_flow, send_availability):
            pass

    def notices():
        while auto.process_notice(send_notice):
            pass

    def booking_url(email):
        return urlsplit([u for e, u in availability if e == email][-1]).path

    def slots(url):
        page = client.get(url)
        assert page.status_code == 200
        return page.text, re.findall(r'<option value="([^"]+)"', page.text)

    return {**flow, "position": position, "apply": apply, "screen": screen, "book": book_worker,
            "notices": notices, "sent": sent, "availability": availability, "booking_url": booking_url,
            "slots": slots}


def _invitation_token(flow, email):
    return urlsplit([o for o in flow["outgoing"] if o[0] == "invitation" and o[1] == email][-1][2]).path.rsplit("/", 1)[1]


def _complete_interview(a, token, app_id):
    client = a["client"]
    now = datetime.now(timezone.utc)
    _exec("UPDATE ats_schedules SET starts_at=?,ends_at=? WHERE application_id=? AND status='planned'",
          ((now - timedelta(minutes=1)).isoformat(), (now + timedelta(minutes=29)).isoformat(), app_id))
    started = client.post("/api/interviews/" + token + "/start")
    assert started.status_code == 200, started.text
    for number in range(started.json()["question_number"], 11):
        assert client.post("/api/interviews/" + token + "/answer", json={
            "question_number": number, "answer": f"Sentetik cevap {number}"}).status_code == 200
    _exec("INSERT INTO interview_reports VALUES (?,?,?)", (token, SYNTHETIC_REPORT, ats.now()))


def test_full_automation_without_hr_pre_approval(automated):
    a = automated
    app_id = a["apply"]("Ayşe Otomatik", "ayse.otomatik@example.test")
    # Başvuru alındı bildirimi kuyruğa alındı; public istek e-posta göndermedi
    assert [n["event"] for n in _all("SELECT event FROM flow_candidate_notices WHERE application_id=?", (app_id,))] \
        == ["application_received"]
    assert a["sent"] == []
    a["screen"]()
    permission = _one("SELECT * FROM flow_interview_permissions WHERE application_id=?", (app_id,))
    # İK onay talebi YOK: otomasyonu açan yönetici adına açıklamalı otomatik izin
    assert permission["status"] == "approved" and "Otomatik akış" in permission["decision_note"]
    assert permission["decided_by"] == a["manager"].user_id
    a["book"]()
    assert len(a["availability"]) == 1  # İK müdahalesi olmadan davet
    a["book"]()
    assert len(a["availability"]) == 1  # mükerrer davet yok
    url = a["booking_url"]("ayse.otomatik@example.test")
    html, options = a["slots"](url)
    text = unescape(html)
    assert "Mülakatınız Hakkında Önemli Bilgi" in text and "8 saat" in text
    assert "süre dolduğunda mülakat bağlantınız otomatik olarak kapanacaktır" in text.lower()
    assert options, "şirketin sunduğu saatler listelenmeli"
    # Liste dışı saat reddedilir (şirketin sunduğu saatlerden seçilmeli)
    off_grid = (datetime.now(auto.LOCAL_ZONE) + timedelta(days=2)).strftime("%Y-%m-%dT03:17")
    assert a["client"].post(url, data={"starts_at": off_grid}).status_code == 409
    choice = options[3]
    assert a["client"].post(url, data={"starts_at": choice}, follow_redirects=False).status_code == 303
    start = datetime.strptime(choice, "%Y-%m-%dT%H:%M").replace(tzinfo=auto.LOCAL_ZONE)
    a["book"]()
    token = _invitation_token(a, "ayse.otomatik@example.test")
    link = _one("SELECT expires_at,status FROM interview_links WHERE token=?", (token,))
    # 8 saat: erişim seçilen saatte açılır, tam 8 saat sonra kapanır (gönderim zamanından bağımsız)
    assert datetime.fromisoformat(link["expires_at"]) == start + timedelta(hours=8)
    import portal_interviews
    with _db() as db:
        cur = db.cursor()
        interview = {"application_id": app_id, "token": token}
        assert portal_interviews.availability(cur, interview, a["actor"].company_id,
                                              now=start - timedelta(minutes=1))["state"] == "scheduled"
        assert portal_interviews.availability(cur, interview, a["actor"].company_id, now=start)["state"] == "available"
        assert portal_interviews.availability(cur, interview, a["actor"].company_id,
                                              now=start + timedelta(hours=7, minutes=59))["state"] == "available"
        assert portal_interviews.availability(cur, interview, a["actor"].company_id,
                                              now=start + timedelta(hours=8))["state"] == "expired"
    # Seçilen saatten önce başlatılamaz (sunucu tarafı)
    assert a["client"].post("/api/interviews/" + token + "/start").status_code == 409
    a["notices"]()
    events = [n["subject"] for n in a["sent"]]
    assert "ERNASA · Başvurunuz alındı" in events and "ERNASA · Mülakat randevunuz" in events
    confirm = next(n for n in a["sent"] if n["subject"] == "ERNASA · Mülakat randevunuz")
    assert f"{start:%d.%m.%Y %H:%M}" in confirm["body"]
    assert f"{start + timedelta(hours=8):%d.%m.%Y %H:%M}" in confirm["body"]
    assert "/basvuru-takip/" in confirm["body"]
    _complete_interview(a, token, app_id)
    # Tamamlanan mülakat bağlantısı yeniden kullanılamaz
    assert a["client"].post("/api/interviews/" + token + "/start").status_code == 409
    assert hr_flow_decisions.list_center(a["actor"])["counts"]["awaiting_hr"] == 1
    detail = hr_flow_decisions.detail(a["actor"], app_id)
    result = hr_flow_decisions.decide(a["actor"], app_id, hr_flow_decisions.Decision(
        decision="reject", reason="Gizli iç gerekçe: vardiya uyumu yetersiz.",
        expected_revision=detail["application"]["revision"], expected_last_decision_id="",
        idempotency_key="auto-reject-1"))
    assert "kuyruğa alındı" in result["notice"]
    a["notices"]()
    completed = next(n for n in a["sent"] if n["subject"] == "ERNASA · Mülakatınız tamamlandı")
    survey = re.search(r"(/aday-deneyimi\?\S+)", completed["body"]).group(1)
    assert "değerlendirmenizi etkilemez" in completed["body"]
    assert a["client"].get(survey).status_code == 200  # mevcut anonim anket modülü
    a["notices"]()
    rejection = next(n for n in a["sent"] if n["subject"] == "ERNASA · Başvurunuzun sonucu")
    assert "olumsuz" in rejection["body"] and "Gizli iç gerekçe" not in rejection["body"]
    assert "yapay zekâ vermez" in rejection["body"]
    # Aday takip sayfası güncel
    tracking = re.search(r"/basvuru-takip/([A-Za-z0-9_-]{43})", rejection["body"]).group(1)
    page = unescape(a["client"].get("/basvuru-takip/" + tracking).text)
    assert "olumsuz sonuçlandı" in page and "Mülakat randevunuz — gönderildi" in page
    detail = hr_flow_decisions.detail(a["actor"], app_id)
    statuses = {n["event"]: n["status"] for n in detail["candidate_notices"]}
    assert statuses["hr_rejected"] == "provider_accepted" and statuses["interview_completed"] == "provider_accepted"
    # Tekrar işleme: aynı bildirimler ikinci kez gönderilmez
    count = len(a["sent"])
    a["notices"]()
    assert len(a["sent"]) == count


def test_reschedule_and_cancel_move_the_8_hour_window(automated):
    a = automated
    email = "burak.randevu@example.test"
    app_id = a["apply"]("Burak Randevu", email)
    a["screen"]()
    a["book"]()
    url = a["booking_url"](email)
    _, options = a["slots"](url)
    first, second = options[2], options[10]
    a["client"].post(url, data={"starts_at": first})
    a["book"]()
    token = _invitation_token(a, email)
    old_schedule = _one("SELECT schedule_id FROM flow_booking_jobs WHERE application_id=?", (app_id,))["schedule_id"]
    html, _ = a["slots"](url)
    assert "Randevuyu değiştir" in html and "Randevuyu iptal et" in html
    # Değiştir: eski pencere geçersiz, yeni saatten itibaren 8 saat
    assert a["client"].post(url, data={"starts_at": second}, follow_redirects=False).status_code == 303
    new_start = datetime.strptime(second, "%Y-%m-%dT%H:%M").replace(tzinfo=auto.LOCAL_ZONE)
    assert _one("SELECT status FROM ats_schedules WHERE id=?", (old_schedule,))["status"] == "cancelled"
    link = _one("SELECT expires_at FROM interview_links WHERE token=?", (token,))
    assert datetime.fromisoformat(link["expires_at"]) == new_start + timedelta(hours=8)
    import portal_interviews
    old_start = datetime.strptime(first, "%Y-%m-%dT%H:%M").replace(tzinfo=auto.LOCAL_ZONE)
    with _db() as db:
        window = portal_interviews.availability(db.cursor(), {"application_id": app_id, "token": token},
                                                a["actor"].company_id, now=old_start + timedelta(minutes=5))
    assert window["state"] == "scheduled" and datetime.fromisoformat(window["starts_at"]) == new_start
    # İptal: bağlantı kullanılamaz; aday yeni saat seçebilir; aynı jeton yeni pencereye taşınır
    assert a["client"].post(url, data={"cancel": "1"}, follow_redirects=False).status_code == 303
    _exec("UPDATE ats_schedules SET starts_at=? WHERE application_id=? AND status='cancelled'",
          (datetime.now(timezone.utc).isoformat(), app_id))
    assert a["client"].post("/api/interviews/" + token + "/start").status_code == 409
    html, options = a["slots"](url)
    assert "Gün ve saati onayla" in html
    third = options[5]
    a["client"].post(url, data={"starts_at": third})
    a["book"]()
    links = _all("SELECT token FROM interview_links WHERE application_id=?", (app_id,))
    assert [row["token"] for row in links] == [token]  # yeni jeton / ikinci kota tüketimi yok
    assert len([o for o in a["outgoing"] if o[0] == "invitation" and o[1] == email]) == 1
    third_start = datetime.strptime(third, "%Y-%m-%dT%H:%M").replace(tzinfo=auto.LOCAL_ZONE)
    assert datetime.fromisoformat(_one("SELECT expires_at FROM interview_links WHERE token=?",
                                       (token,))["expires_at"]) == third_start + timedelta(hours=8)
    a["notices"]()
    subjects = [n["subject"] for n in a["sent"]]
    assert "ERNASA · Mülakat randevunuz güncellendi" in subjects
    assert "ERNASA · Mülakat randevunuz iptal edildi" in subjects


def test_expired_window_closes_link_and_started_interview_cannot_be_rescheduled(automated):
    a = automated
    email = "cem.sure@example.test"
    app_id = a["apply"]("Cem Süre", email)
    a["screen"]()
    a["book"]()
    url = a["booking_url"](email)
    _, options = a["slots"](url)
    a["client"].post(url, data={"starts_at": options[1]})
    a["book"]()
    token = _invitation_token(a, email)
    past = datetime.now(timezone.utc) - timedelta(hours=8, minutes=1)
    _exec("UPDATE ats_schedules SET starts_at=?,ends_at=? WHERE application_id=? AND status='planned'",
          (past.isoformat(), (past + timedelta(minutes=30)).isoformat(), app_id))
    assert a["client"].post("/api/interviews/" + token + "/start").status_code == 410
    assert _one("SELECT status FROM interview_links WHERE token=?", (token,))["status"] == "expired"
    assert a["client"].post("/api/interviews/" + token + "/start").status_code == 410
    html, _ = a["slots"](url)
    assert "randevu değiştirilemez" in unescape(html)
    with pytest.raises(HTTPException) as error:
        auto.change(url.rsplit("/", 1)[1], "synthetic", options[4])
    assert error.value.status_code == 409


def test_conflicting_slot_and_unoffered_slot_are_rejected(automated):
    a = automated
    first_app = a["apply"]("Deniz Çakışma", "deniz.cakisma@example.test")
    second_app = a["apply"]("Ece Çakışma", "ece.cakisma@example.test")
    a["screen"]()
    a["book"]()
    url_1, url_2 = a["booking_url"]("deniz.cakisma@example.test"), a["booking_url"]("ece.cakisma@example.test")
    _, options = a["slots"](url_1)
    slot = options[6]
    a["client"].post(url_1, data={"starts_at": slot})
    _, options_2 = a["slots"](url_2)
    assert slot not in options_2  # dolu saat artık sunulmuyor
    response = a["client"].post(url_2, data={"starts_at": slot})
    assert response.status_code == 409 and "artık uygun değil" in unescape(response.text)
    assert _one("SELECT status FROM flow_booking_jobs WHERE application_id=?", (second_app,))["status"] == "waiting"
    assert _one("SELECT status FROM flow_booking_jobs WHERE application_id=?", (first_app,))["status"] == "booked"


def test_unprocessable_applications_go_to_review_without_invite(automated, monkeypatch):
    a = automated
    import ats_matching
    bad = a["apply"]("Fatma Bozuk", "fatma.bozuk@example.test")
    original = ats_matching.evidence_match
    monkeypatch.setattr(ats_matching, "evidence_match",
                        lambda *args, **kw: (_ for _ in ()).throw(ValueError("bozuk CV")))
    a["screen"]()
    monkeypatch.setattr(ats_matching, "evidence_match", original)
    invalid = a["apply"]("Gül Geçersiz", "gul.gecersiz@example.test")
    _exec("UPDATE ats_candidates SET email=? WHERE email=?", ("gecersiz-adres", "gul.gecersiz@example.test"))
    a["screen"]()
    a["book"]()
    assert a["availability"] == []  # inceleme gerektirenlere davet gitmez; otomatik ret de yok
    reviews = {(r["application_id"], r["reason_code"]) for r in _all("SELECT * FROM flow_automation_reviews")}
    assert (bad, "cv_unreadable") in reviews and (invalid, "email_invalid") in reviews
    for app_id in (bad, invalid):
        assert _one("SELECT closure FROM ats_applications WHERE id=?", (app_id,))["closure"] is None
        assert _one("SELECT 1 AS x FROM flow_interview_permissions WHERE application_id=?", (app_id,)) is None
    report = auto.reports(a["actor"])
    assert {r["reason_code"] for r in report["reviews"]} >= {"cv_unreadable", "email_invalid"}
    # Geçersiz alıcıya bildirim gönderilmez, gönderilmiş gibi gösterilmez
    a["notices"]()
    failed = _one("SELECT status,error_code FROM flow_candidate_notices WHERE application_id=? "
                  "AND event='application_received'", (invalid,))
    assert failed == {"status": "failed", "error_code": "invalid_recipient"}


def test_provider_failure_retries_then_fails_without_claiming_sent(automated):
    a = automated
    app_id = a["apply"]("Hakan Sağlayıcı", "hakan.saglayici@example.test")
    calls = []

    def broken(recipient, subject, body, *, idempotency_key):
        calls.append(idempotency_key)
        raise RuntimeError("sentetik sağlayıcı hatası")

    assert auto.process_notice(broken)
    row = _one("SELECT status,attempts FROM flow_candidate_notices WHERE application_id=?", (app_id,))
    assert row == {"status": "retry", "attempts": 1}
    for _ in range(3):
        _exec("UPDATE flow_candidate_notices SET next_attempt_at=? WHERE application_id=?",
              (datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat(), app_id))
        assert auto.process_notice(broken)
    row = _one("SELECT status,attempts FROM flow_candidate_notices WHERE application_id=?", (app_id,))
    assert row == {"status": "failed", "attempts": 4} and len(set(calls)) == 1  # aynı idempotency anahtarı
    # Gönderim sonucu bilinmeyen (yarıda kalan) kayıt otomatik tekrar gönderilmez
    _exec("UPDATE flow_candidate_notices SET status='sending',updated_at=? WHERE application_id=?",
          ((datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(), app_id))
    assert not auto.process_notice(a["sent"].append)
    assert _one("SELECT status FROM flow_candidate_notices WHERE application_id=?", (app_id,))["status"] == "unconfirmed"


def test_quota_exhaustion_blocks_link_without_consuming(automated, monkeypatch):
    a = automated
    main = a["main"]
    original = main.portal_access.control_call

    def no_quota(portal, operation, **kwargs):
        result = original(portal, operation, **kwargs)
        if operation == "session":
            result = {**result, "interview_quota": 0, "month_bounds": ["2000-01-01T00:00:00+00:00",
                                                                       "2100-01-01T00:00:00+00:00"]}
        return result

    monkeypatch.setattr(main.portal_access, "control_call", no_quota)
    import hr_flow_booking
    monkeypatch.setattr(hr_flow_booking.control, "control_call", no_quota)
    email = "irem.kota@example.test"
    app_id = a["apply"]("İrem Kota", email)
    a["screen"]()
    a["book"]()
    assert len(a["availability"]) == 1  # davet e-postası kota tüketmez
    url = a["booking_url"](email)
    _, options = a["slots"](url)
    a["client"].post(url, data={"starts_at": options[0]})
    a["book"]()
    assert _all("SELECT token FROM interview_links WHERE application_id=?", (app_id,)) == []
    job = _one("SELECT status,error_code FROM flow_booking_jobs WHERE application_id=?", (app_id,))
    assert job["status"] == "failed" and job["error_code"] == "HTTPException"
    assert [o for o in a["outgoing"] if o[0] == "invitation"] == []


def test_authorization_and_tenant_isolation(automated):
    a = automated
    main = a["main"]
    staff = main.portal_access.Actor(a["actor"].company_id, "hr_staff", a["portal"], 99, "revision-1",
                                     "Sentetik Firma", products=("ats", "ai"))
    with pytest.raises(HTTPException) as error:
        auto.configure(staff, a["position"], ALL_DAY)
    assert error.value.status_code == 403
    with pytest.raises(HTTPException) as error:
        main.flow_reports(staff)
    assert error.value.status_code == 403
    other = main.portal_access.Actor(5555, "hr_manager", a["portal"], 77, "revision-1", "Başka Firma",
                                     products=("ats", "ai"))
    with pytest.raises(HTTPException) as error:
        auto.get_config(other, a["position"])
    assert error.value.status_code == 404
    app_id = a["apply"]("Jale Kapsam", "jale.kapsam@example.test")
    assert all(c["application_id"] != app_id for c in hr_flow_decisions.list_center(other)["candidates"])
    assert a["client"].get("/basvuru-takip/" + "A" * 43).status_code == 404
    no_auth = main.TestClient(main.app) if hasattr(main, "TestClient") else None
    from fastapi.testclient import TestClient
    anonymous = TestClient(main.app, base_url=a["portal"].origin)
    assert anonymous.get("/api/ik/flow/reports").status_code == 401
    assert anonymous.put("/api/ik/flow/positions/" + a["position"] + "/automation",
                         json={"enabled": False}).status_code in (401, 403)
    del no_auth


def test_tracking_requests_reminders_and_status_updates(automated):
    a = automated
    email = "kaan.takip@example.test"
    app_id = a["apply"]("Kaan Takip", email)
    a["screen"]()
    a["book"]()
    url = a["booking_url"](email)
    _, options = a["slots"](url)
    a["client"].post(url, data={"starts_at": options[0]})
    a["notices"]()
    body = a["sent"][0]["body"]
    tracking = re.search(r"/basvuru-takip/([A-Za-z0-9_-]{43})", body).group(1)
    page = unescape(a["client"].get("/basvuru-takip/" + tracking).text)
    assert "Mülakat erişim penceresi" in page and "(8 saat, Türkiye saati)" in page
    # KVKK erişim/silme talebi mevcut ATS gizlilik akışına, teknik sorun ise İK talep listesine düşer
    for kind in ("deletion", "technical_issue"):
        reply = a["client"].post("/basvuru-takip/" + tracking, data={"kind": kind, "message": "Sentetik talep"})
        assert reply.status_code == 200
    assert _one("SELECT kind,status,requested_by FROM ats_privacy_requests WHERE application_id=?", (app_id,)) \
        == {"kind": "erase", "status": "requested", "requested_by": 0}
    report = auto.reports(a["actor"])
    assert [r["kind"] for r in report["requests"]] == ["technical_issue"]
    # Randevudan 24 saat önce hatırlatma; tekrar taramada mükerrer kayıt yok
    start = datetime.strptime(options[0], "%Y-%m-%dT%H:%M").replace(tzinfo=auto.LOCAL_ZONE)
    assert auto.sweep(now=start - timedelta(hours=3)) >= 1
    auto.sweep(now=start - timedelta(hours=2))
    assert len(_all("SELECT id FROM flow_candidate_notices WHERE application_id=? AND event='interview_reminder'",
                    (app_id,))) == 1
    # 7 gün yanıtsız kalan (mülakatı bitmiş, kararsız) adaya durum bilgilendirmesi
    a["book"]()
    token = _invitation_token(a, email)
    _complete_interview(a, token, app_id)
    a["notices"]()
    later = datetime.now(timezone.utc) + timedelta(days=8)
    _exec("UPDATE flow_candidate_notices SET created_at=? WHERE application_id=?",
          ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(), app_id))
    auto.sweep(now=later)
    assert _one("SELECT status FROM flow_candidate_notices WHERE application_id=? AND event='awaiting_hr_update'",
                (app_id,))["status"] == "prepared"
    # 14 gün: İK hatırlatma listesi
    _exec("UPDATE interview_links SET created_at=? WHERE token=?",
          ((datetime.now(timezone.utc) - timedelta(days=15)).isoformat(), token))
    overdue = auto.reports(a["actor"])["hr_overdue"]
    assert [item["application_id"] for item in overdue] == [app_id]
