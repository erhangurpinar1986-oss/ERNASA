"""ERNASA HR FLOW · İK Karar Merkezi: sentetik adaylarla uçtan uca doğrulama.

Gerçek backend/main uygulaması, gerçek ATS / HR FLOW / takvim / Akademi tabloları
kullanılır. Yalnızca dış servisler (CONTROL oturumu, e-posta sağlayıcısı, OpenAI)
sentetik yanıtlarla değiştirilir. Üretim verisi, e-postası veya Render kullanılmaz.
"""
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from fastapi import HTTPException

import ats
import database
import hr_flow_decisions
from tests.test_ats_public_cv import intake  # noqa: F401  (paylaşılan sentetik fixture)

SYNTHETIC_REPORT = """Genel Pozisyon Uyumu: %72
Mesleki Deneyim Uyumu: %68
GÜÇLÜ YÖNLER:
- Depo düzeni ve WMS kullanımını somut örneklerle anlattı.
GELİŞTİRİLMESİ GEREKEN ALANLAR:
- Vardiya planlama deneyimi sınırlı.
DİKKAT EDİLMESİ GEREKEN NOKTALAR:
- Forklift belgesi CV'de belirtilmiş ancak mülakatta doğrulanmadı.
İK DEĞERLENDİRME ÖZETİ:
- Sentetik test raporu; nihai karar İK yöneticisine aittir.
"""


@pytest.fixture
def flow(intake, tmp_path, monkeypatch):  # noqa: F811
    """Başvuru → ATS → yönetici izni → uygunluk e-postası → aday saat seçimi → davet → mülakat → rapor."""
    import hr_flow_booking
    import hr_flow_screening
    import openai
    from fastapi.testclient import TestClient

    import ats_public_cv
    monkeypatch.setattr(openai, "OpenAI", lambda **kwargs: object())
    from backend import main
    from services import invite_email

    manager, position, job, pdf, applicant = intake
    import selin_workflow
    selin_workflow.initialize_schema()
    import academy_assignments
    import ernasa_training
    ernasa_training.initialize_schema()
    academy_assignments.initialize_schema()
    hr_flow_decisions.initialize_schema()
    monkeypatch.setattr(main, "UPLOAD_DIRECTORY", tmp_path)
    monkeypatch.setenv("ERNASA_IK_CONTROL_URL", "https://control.example.test")
    portal = main.portal_access.Portal("synthetic", "https://synthetic.ernasa.com")
    actor = main.portal_access.Actor(manager.company_id, "hr_manager", portal, manager.user_id,
                                     "revision-1", "Sentetik Firma", products=("ats", "ai"))

    def control_call(portal, operation, **kwargs):
        if operation == "session":
            return {"company_id": actor.company_id, "role": actor.role, "version": actor.access_version,
                    "company_name": actor.company_name, "products": actor.products}
        if operation == "resolve":
            return {"company_id": actor.company_id if portal.slug == "synthetic" else 4444,
                    "name": actor.company_name, "products": actor.products}
        return {}

    monkeypatch.setattr(main.portal_access, "control_call", control_call)
    monkeypatch.setattr(invite_email, "configured_sender", lambda: ("fake", "synthetic@example.test"))
    outgoing = []

    def send_availability(candidate, url, **kwargs):
        outgoing.append(("availability", candidate["email"], url))
        return "synthetic-availability-" + uuid4().hex[:6]

    def send_invitation(candidate, url, **kwargs):
        outgoing.append(("invitation", candidate["email"], url))
        return "synthetic-interview-" + uuid4().hex[:6]

    def forbidden_training_send(**kwargs):
        raise AssertionError("Karar Merkezi eğitim e-postası GÖNDERMEMELİ")

    monkeypatch.setattr(invite_email, "send_invitation", send_invitation)
    monkeypatch.setattr(invite_email, "send_training_invitation", forbidden_training_send)
    monkeypatch.setattr(main, "generate_interview_question",
                        lambda **kwargs: "Sentetik mülakat sorusu #" + str(kwargs.get("question_number")))
    client = TestClient(main.app, base_url=portal.origin, headers={"Origin": portal.origin})

    def new_candidate(name, email, complete=True):
        result = client.post("/api/ats/jobs/" + job + "/apply-with-cv",
            data={"name": name, "email": email, "notice_version": applicant.notice_version,
                  "acknowledge": "true", "source": "direct"},
            files={"cv": ("sentetik_cv.pdf", pdf, "application/pdf")}, headers={"Origin": portal.origin})
        assert result.status_code == 201, result.text
        entry = next(row for row in ats_public_cv.list_intake(actor.company_id) if row["email"] == email)
        app_id, doc_id = entry["application_id"], entry["document_id"]
        while hr_flow_screening.process_one(tmp_path):
            pass
        permission = hr_flow_screening.request_permission(actor, app_id, doc_id)
        main.flow_decide_interview_permission(permission["id"], main.FlowPermissionDecision(decision="approved"), actor)
        assert hr_flow_booking.process_one(main.start_booked_flow, send_availability)
        booking_url = urlsplit([o for o in outgoing if o[0] == "availability" and o[1] == email][-1][2]).path
        if not complete:
            return app_id, booking_url, None
        # Aday: uygunluk bağlantısını açar ve saati KENDİSİ seçer (gerçek HTTP uçları)
        assert client.get(booking_url).status_code == 200
        start = (datetime.now(hr_flow_booking.LOCAL_ZONE) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        assert "Görüşme saatiniz kaydedildi" in client.post(booking_url, data={"starts_at": start}).text
        assert hr_flow_booking.process_one(main.start_booked_flow, send_availability)
        token = urlsplit([o for o in outgoing if o[0] == "invitation" and o[1] == email][-1][2]).path.rsplit("/", 1)[1]
        # Aday mülakat bağlantısını randevudan önce açar: başlatamaz ama açılış kaydedilir
        assert client.post("/api/interviews/" + token + "/opened").json()["can_start"] is False
        # Sentetik test veritabanında randevu saatini şimdiye çekiyoruz (zamanı simüle etmek için)
        with closing(database.get_connection()) as db:
            now = datetime.now(timezone.utc)
            db.cursor().execute("UPDATE ats_schedules SET starts_at=?,ends_at=? WHERE application_id=?",
                                ((now - timedelta(minutes=1)).isoformat(), (now + timedelta(minutes=29)).isoformat(), app_id))
            db.commit()
        started = client.post("/api/interviews/" + token + "/start")
        assert started.status_code == 200, started.text
        question = started.json()
        for number in range(question["question_number"], 11):
            reply = client.post("/api/interviews/" + token + "/answer", json={
                "question_number": number, "answer": f"Sentetik cevap {number}: depo sayımını planlı yaparım."})
            assert reply.status_code == 200, reply.text
        with closing(database.get_connection()) as db:
            status = db.cursor().execute("SELECT status FROM interview_links WHERE token=?", (token,)).fetchone()["status"]
            assert status == "completed"
            # AI değerlendirici dış servistir; çıktısı sentetik rapor olarak yazılır
            db.cursor().execute("INSERT INTO interview_reports VALUES (?,?,?)", (token, SYNTHETIC_REPORT, ats.now()))
            db.commit()
        return app_id, booking_url, token

    return {"main": main, "actor": actor, "client": client, "new": new_candidate, "outgoing": outgoing,
            "manager": manager, "portal": portal}


def _decide(flow, app_id, decision, **extra):
    detail = hr_flow_decisions.detail(flow["actor"], app_id)
    body = hr_flow_decisions.Decision(decision=decision, expected_revision=detail["application"]["revision"],
                                      expected_last_decision_id=detail["last_decision_id"],
                                      idempotency_key=extra.pop("key", uuid4().hex), **extra)
    return flow["main"].flow_decision_center_decide(app_id, body, flow["actor"])


def _count(sql, params=()):
    with closing(database.get_connection()) as db:
        return db.cursor().execute(sql, params).fetchone()[0]


@pytest.mark.parametrize("intake", ["sqlite", "postgres"], indirect=True)
def test_full_flow_real_events_and_unified_dossier(flow):
    app_id, _, token = flow["new"]("Ayşe Sentetik", "ayse.sentetik@example.test")
    listing = hr_flow_decisions.list_center(flow["actor"])
    row = next(c for c in listing["candidates"] if c["application_id"] == app_id)
    assert row["state"] == "awaiting_hr" and row["state_label"] == "İK kararı bekliyor"
    assert row["interview_score"] == 72
    assert listing["can_decide"] is True

    detail = hr_flow_decisions.detail(flow["actor"], app_id)
    steps = {step["code"]: step for step in detail["timeline"]}
    for code, source in (("availability_link_opened", "flow_candidate_events"),
                         ("appointment_selected", "flow_candidate_appointment"),
                         ("interview_link_opened", "flow_candidate_events"),
                         ("interview_started", "flow_candidate_events"),
                         ("interview_completed", "flow_candidate_events"),
                         ("report", "interview_reports")):
        assert steps[code]["state"] == "done", (code, steps[code])
        assert steps[code]["at"], code
        assert steps[code]["source"] == source, (code, steps[code]["source"])
    order = [steps[c]["at"] for c in ("availability_link_opened", "interview_link_opened",
                                       "interview_started", "interview_completed")]
    assert order == sorted(order)
    assert len(detail["answers"]) == 10 and detail["answers"][0]["answer"].startswith("Sentetik cevap")
    assert detail["report"]["strengths"] == ["Depo düzeni ve WMS kullanımını somut örneklerle anlattı."]
    assert detail["report"]["attention"][0].startswith("Forklift belgesi")
    assert detail["documents"] and detail["ats"] and detail["scores"]["interview"] == 72
    assert "birleştirilmez" in detail["scores"]["note"]
    assert detail["can_decide"] is True
    # Bağlantı / e-posta değerleri karar ekranına sızmaz
    assert token not in str(detail)
    # Olaylar tekrar açılışta çoğalmaz
    flow["client"].post("/api/interviews/" + token + "/opened")
    assert _count("SELECT COUNT(*) FROM flow_candidate_events WHERE application_id=?", (app_id,)) == 5


@pytest.mark.parametrize("intake", ["sqlite", "postgres"], indirect=True)
def test_unperformed_steps_are_never_shown_as_done(flow):
    app_id, _, _ = flow["new"]("Bekleyen Aday", "bekleyen@example.test", complete=False)
    detail = hr_flow_decisions.detail(flow["actor"], app_id)
    steps = {step["code"]: step for step in detail["timeline"]}
    assert detail["state"] == "availability_sent"
    for code in ("availability_link_opened", "appointment_selected", "interview_link", "interview_link_opened",
                 "interview_started", "interview_completed", "report"):
        assert steps[code]["state"] == "missing", code
        assert steps[code]["at"] is None
    assert detail["can_decide"] is False and "tamamlanmış mülakat" in detail["decision_help"]
    with pytest.raises(HTTPException) as error:
        _decide(flow, app_id, "approve")
    assert error.value.status_code == 409


@pytest.mark.parametrize("intake", ["sqlite", "postgres"], indirect=True)
def test_four_decisions_use_existing_modules_and_send_no_email(flow):
    outgoing = flow["outgoing"]
    second, _, _ = flow["new"]("İkinci Görüşme Adayı", "ikinci@example.test")
    approve, _, _ = flow["new"]("Onay Adayı", "onay@example.test")
    reject, _, _ = flow["new"]("Ret Adayı", "ret@example.test")
    emails_before = len(outgoing)

    # İKİNCİ GÖRÜŞME: mevcut ATS aşaması + mevcut takvim kaydı
    starts = (datetime.now(timezone.utc) + timedelta(days=5)).replace(microsecond=0)
    result = _decide(flow, second, "second_interview", reason="Teknik ekip görüşmesi.",
                     schedule=hr_flow_decisions.SecondInterviewPlan(
                         interviewer_id=flow["actor"].user_id, starts_at=starts.isoformat(),
                         ends_at=(starts + timedelta(minutes=45)).isoformat(), kind="onsite"))
    assert result["decision"]["decision"] == "second_interview" and result["decision"]["schedule_id"]
    detail = hr_flow_decisions.detail(flow["actor"], second)
    assert detail["state"] == "second_interview" and detail["application"]["stage"] == "interview"
    planned = [s for s in detail["schedules"] if s["id"] == result["decision"]["schedule_id"]][0]
    assert planned["kind"] == "onsite" and planned["status"] == "planned"
    # İK'nın girdiği takvim kaydı aday seçimi sayılmaz: aday seçimi ayrı adımda kalır
    assert [s for s in detail["timeline"] if s["code"] == "appointment_selected"][0]["source"] == "flow_candidate_appointment"

    # EĞİTİM ATA: mevcut Akademi V9 tabloları, başvuruya bağlı, e-posta gönderilmeden
    training = _decide(flow, second, "assign_training", training=hr_flow_decisions.TrainingChoice(
        program_id="ernasa_v9", module_ids=["ernasa", "demo"], days=10))
    assert training["training_link"].startswith("https://ernasa.com/egitim/")
    detail = hr_flow_decisions.detail(flow["actor"], second)
    assert detail["state"] == "training_assigned"
    assert detail["trainings"][0]["total_lessons"] == 2 and detail["trainings"][0]["completed_lessons"] == 0
    assert _count("SELECT COUNT(*) FROM ernasa_training_assignments WHERE company_id=?", (flow["actor"].company_id,)) == 1

    # ONAYLA: mevcut ATS aşaması 'offer'
    _decide(flow, approve, "approve", reason="Pozisyon için uygun.")
    detail = hr_flow_decisions.detail(flow["actor"], approve)
    assert detail["state"] == "approved" and detail["application"]["stage"] == "offer"
    assert detail["can_decide"] is False
    with pytest.raises(HTTPException) as error:
        _decide(flow, approve, "reject", reason="İkinci karar denemesi")
    assert error.value.status_code == 409

    # REDDET: gerekçe zorunlu; mevcut olumsuz kapanış + gerekçe ATS notlarına
    with pytest.raises(HTTPException) as error:
        _decide(flow, reject, "reject", reason="")
    assert error.value.status_code == 422
    _decide(flow, reject, "reject", reason="Vardiya düzenine uygun değil.")
    detail = hr_flow_decisions.detail(flow["actor"], reject)
    assert detail["state"] == "rejected" and detail["application"]["closure"] == "negative"
    assert any("Vardiya düzenine uygun değil." in note["note"] for note in detail["notes"])
    assert detail["decisions"][-1]["decided_by"] == flow["actor"].user_id and detail["decisions"][-1]["decided_at"]

    # Hiçbir karar adaya e-posta göndermedi
    assert len(outgoing) == emails_before
    # Her karar ATS denetim kaydına da yazıldı
    assert _count("SELECT COUNT(*) FROM recruitment_audit WHERE event LIKE ?", ("hr_decision_%",)) == 4


@pytest.mark.parametrize("intake", ["sqlite", "postgres"], indirect=True)
def test_decision_security_idempotency_and_tenant_scope(flow):
    from backend import main
    app_id, _, _ = flow["new"]("Güvenlik Adayı", "guvenlik@example.test")
    staff = main.portal_access.Actor(flow["actor"].company_id, "hr_staff", flow["portal"], 99, "revision-1",
                                     "Sentetik Firma", products=("ats", "ai"))
    with pytest.raises(HTTPException) as error:
        _decide({**flow, "actor": staff}, app_id, "approve")
    assert error.value.status_code in (403, 404)
    other = main.portal_access.Actor(5555, "hr_manager", flow["portal"], 1, "r", "Başka Firma", products=("ats", "ai"))
    with pytest.raises(HTTPException) as error:
        hr_flow_decisions.detail(other, app_id)
    assert error.value.status_code == 404
    assert all(c["application_id"] != app_id for c in hr_flow_decisions.list_center(other)["candidates"])

    detail = hr_flow_decisions.detail(flow["actor"], app_id)
    stale = hr_flow_decisions.Decision(decision="approve", expected_revision=detail["application"]["revision"] + 1,
                                       expected_last_decision_id="", idempotency_key=uuid4().hex)
    with pytest.raises(HTTPException) as error:
        main.flow_decision_center_decide(app_id, stale, flow["actor"])
    assert error.value.status_code == 409

    key = uuid4().hex
    first = _decide(flow, app_id, "second_interview", key=key)
    replay = hr_flow_decisions.decide(flow["actor"], app_id, hr_flow_decisions.Decision(
        decision="second_interview", expected_revision=0, expected_last_decision_id="", idempotency_key=key))
    assert replay["replayed"] is True and replay["decision"]["id"] == first["decision"]["id"]
    assert _count("SELECT COUNT(*) FROM flow_hr_decisions WHERE application_id=?", (app_id,)) == 1

    # Eşzamanlı iki karar: yalnızca biri kaydedilir
    detail = hr_flow_decisions.detail(flow["actor"], app_id)
    results = []

    def attempt(kind):
        try:
            hr_flow_decisions.decide(flow["actor"], app_id, hr_flow_decisions.Decision(
                decision=kind, reason="Eşzamanlı test", expected_revision=detail["application"]["revision"],
                expected_last_decision_id=detail["last_decision_id"], idempotency_key=uuid4().hex))
            results.append("ok")
        except Exception as error:  # 409 veya SQLite kilidi: kaydedilmedi
            results.append(type(error).__name__)

    threads = [threading.Thread(target=attempt, args=(k,)) for k in ("approve", "reject", "approve")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("ok") == 1, results
    assert _count("SELECT COUNT(*) FROM flow_hr_decisions WHERE application_id=?", (app_id,)) == 2


@pytest.mark.parametrize("intake", ["sqlite", "postgres"], indirect=True)
def test_http_routes_require_session_and_legacy_status_is_labelled(flow):
    app_id, _, token = flow["new"]("Yetki Adayı", "yetki@example.test")
    anonymous = flow["client"]
    anonymous.cookies.clear()
    assert anonymous.get("/api/ik/flow/decision-center").status_code == 401
    assert anonymous.get("/api/ik/flow/decision-center/" + app_id).status_code == 401
    assert anonymous.post("/api/ik/flow/decision-center/" + app_id + "/decision", json={},
                          headers={"Origin": flow["portal"].origin}).status_code in (401, 422)
    # Güncelleme öncesi kayıt: durum 'started' ama olay zamanı yok → yapıldı, fakat zaman uydurulmaz
    with closing(database.get_connection()) as db:
        db.cursor().execute("DELETE FROM flow_candidate_events WHERE application_id=?", (app_id,))
        db.commit()
    steps = {s["code"]: s for s in hr_flow_decisions.detail(flow["actor"], app_id)["timeline"]}
    assert steps["interview_started"]["state"] == "done"
    assert steps["interview_started"]["source"] == "interview_links"
    assert "olay zamanı kaydı yok" in steps["interview_started"]["detail"]
    assert steps["availability_link_opened"]["state"] == "missing"


@pytest.mark.parametrize("intake", ["sqlite", "postgres"], indirect=True)
def test_candidate_flow_still_works_if_event_store_unavailable(flow):
    """Olay kaydı en iyi çabadır: olay tablosu yoksa bile randevu ve mülakat bozulmaz."""
    with closing(database.get_connection()) as db:
        db.cursor().execute("DROP TABLE flow_candidate_events")
        db.commit()
    app_id, _, token = flow["new"]("Dayanıklılık Adayı", "dayaniklilik@example.test")
    with closing(database.get_connection()) as db:
        row = db.cursor().execute("SELECT status FROM interview_links WHERE token=?", (token,)).fetchone()
        assert row["status"] == "completed"
        assert db.cursor().execute("SELECT confirmed_by FROM flow_candidate_appointment WHERE application_id=?",
                                   (app_id,)).fetchone()["confirmed_by"] == "candidate"
    hr_flow_decisions.initialize_schema()
    assert _count("SELECT COUNT(*) FROM flow_candidate_events WHERE application_id=?", (app_id,)) == 0
    steps = {s["code"]: s for s in hr_flow_decisions.detail(flow["actor"], app_id)["timeline"]}
    assert steps["interview_completed"]["state"] == "done" and steps["interview_completed"]["source"] == "interview_answers"
    assert steps["availability_link_opened"]["state"] == "missing"
