"""Public ERNASA HR CV application: synthetic security and durability tests."""
from contextlib import closing
from types import SimpleNamespace

import pymupdf
import pytest
from fastapi import HTTPException

import ats
import ats_public_cv
import database
import recruitment


@pytest.fixture
def intake(tmp_path,monkeypatch,request):
    import os
    from uuid import uuid4

    dsn = None
    if getattr(request, "param", None) == "postgres":
        import psycopg2
        from psycopg2.extensions import make_dsn
        dsn = os.getenv("HR_FLOW_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Isolated PostgreSQL DSN required")
        schema = "flow_test_" + uuid4().hex
        with closing(psycopg2.connect(dsn)) as connection, connection:
            connection.cursor().execute("CREATE SCHEMA " + schema)
        dsn = make_dsn(dsn, options="-csearch_path=" + schema)
    monkeypatch.setattr(database,"DATABASE_URL",dsn)
    monkeypatch.setattr(database,"DATABASE_PATH",tmp_path/"public-cv.db")
    monkeypatch.setenv("ERNASA_PUBLIC_CV_ENABLED","1")
    monkeypatch.setenv("ERNASA_PUBLIC_CV_RATE_SECRET","synthetic-only-secret-key-abcdef0123456789")
    database.initialize_database()
    ats_public_cv.initialize_schema()
    import hr_flow_contacts
    hr_flow_contacts.initialize_schema()
    import hr_flow_screening
    hr_flow_screening.initialize_schema()
    manager=SimpleNamespace(company_id=1234,user_id=21,role="hr_manager",products=("ats",))
    dept=recruitment.add_department(manager,"Lojistik")
    position=recruitment.save_position(manager,recruitment.Position(
        name="Depo Personeli",department_id=dept["id"]))
    job=ats.save_job(manager,ats.Job(
        position_id=position["id"],title="Depo Personeli",
        summary="Lojistik depo",state="published",confirm_publish=True,
        notice="Aydınlatma: Başvuru ve CV değerlendirilecektir.",
        notice_version="v1"
    ))
    doc=pymupdf.open()
    page=doc.new_page()
    page.insert_text((72,100),"Sentetik aday: lojistik depo ve WMS deneyimi.")
    pdf=doc.tobytes()
    doc.close()
    applicant=ats.PublicApplication(name="Test Basvuran",
                                    email="synthetic-applicant@example.test",phone="",
                                    acknowledge=True,notice_version="v1")
    return manager,position["id"],job["id"],pdf,applicant


def test_public_cv_attaches_to_exact_published_position_and_persists_source(intake):
    manager,position,job,pdf,applicant=intake
    result=ats_public_cv.submit(manager.company_id,job,applicant,"cv.pdf",pdf,"linkedin","203.0.113.14")
    with closing(database.get_connection()) as db:
        c=db.cursor()
        app=c.execute("SELECT * FROM ats_applications WHERE id=?",
                      (result["application_id"],)).fetchone()
        assert app["position_id"]==position and app["company_id"]==manager.company_id
        assert app["job_id"]==job
        source=c.execute("SELECT * FROM ats_application_sources WHERE application_id=?",
                         (app["id"],)).fetchone()
        assert source["source"]=="linkedin"
        assert c.execute("SELECT COUNT(*) FROM ats_notices WHERE application_id=?",
                         (app["id"],)).fetchone()[0]==1
        document=c.execute("SELECT * FROM ats_documents WHERE id=?",
                           (result["document_id"],)).fetchone()
        assert document["company_id"]==manager.company_id
        blob=c.execute("SELECT content_base64 FROM ats_document_blobs WHERE document_id=?",
                       (result["document_id"],)).fetchone()
        from base64 import b64decode
        assert b64decode(blob["content_base64"])==pdf


def test_eleman_net_source_preserved_for_published_job(intake):
    """Channel is attributed without claiming publication on third-party sites."""
    manager,position,job,pdf,applicant=intake
    result=ats_public_cv.submit(manager.company_id,job,applicant,
                                "cv.pdf",pdf,"eleman_net","203.0.113.30")
    with closing(database.get_connection()) as db:
        row=db.execute(
            "SELECT source FROM ats_application_sources WHERE application_id=?",
            (result["application_id"],)).fetchone()
    assert row["source"]=="eleman_net"


def test_invalid_consent_source_document_and_duplicate_are_rejected(intake):
    manager,position,job,pdf,applicant=intake
    for source in ("http://evil.example","", "LinkedIn.com"):
        with pytest.raises(HTTPException) as err:
            ats_public_cv.submit(manager.company_id,job,applicant,"cv.pdf",pdf,source,"203.0.113.14")
        assert err.value.status_code==422
    with pytest.raises(HTTPException):
        ats_public_cv.submit(manager.company_id,job,applicant,"cv.pdf",b"not a pdf",
                             "direct","203.0.113.14")
    wrong=applicant.model_copy(update={"acknowledge":False})
    with pytest.raises(HTTPException) as err:
        ats_public_cv.submit(manager.company_id,job,wrong,"cv.pdf",pdf,"direct","203.0.113.14")
    assert err.value.status_code==400
    ats_public_cv.submit(manager.company_id,job,applicant,"cv.pdf",pdf,"direct","203.0.113.14")
    with pytest.raises(HTTPException) as err:
        ats_public_cv.submit(manager.company_id,job,applicant,"cv.pdf",pdf,"direct","203.0.113.14")
    assert err.value.status_code==409
    with closing(database.get_connection()) as db:
        assert db.execute("SELECT COUNT(*) FROM ats_documents").fetchone()[0]==1


def test_cross_tenant_and_disabled_public_cv_fail_closed(intake,monkeypatch):
    manager,position,job,pdf,applicant=intake
    with pytest.raises(HTTPException) as err:
        ats_public_cv.submit(9999,job,applicant,"cv.pdf",pdf,"direct","203.0.113.15")
    assert err.value.status_code==404
    monkeypatch.delenv("ERNASA_PUBLIC_CV_ENABLED")
    with pytest.raises(HTTPException) as err:
        ats_public_cv.submit(manager.company_id,job,applicant,"cv.pdf",pdf,"direct","203.0.113.15")
    assert err.value.status_code==404


def test_source_tracker_and_rate_limit_do_not_store_raw_visitor_ip(intake):
    manager,position,job,pdf,applicant=intake
    for i in range(8):
        with closing(database.get_connection()) as db:
            c=db.cursor()
            now=ats.now()
            from uuid import uuid4
            from hashlib import sha256
            secret="synthetic-only-secret-key-abcdef0123456789"
            visitor=sha256((secret+"|"+"203.0.113.15").encode()).hexdigest()
            c.execute("INSERT INTO ats_public_cv_attempts VALUES(?,?,?,?)",
                      (uuid4().hex,manager.company_id,visitor,now))
            db.commit()
    with pytest.raises(HTTPException) as err:
        ats_public_cv.submit(manager.company_id,job,applicant,"cv.pdf",pdf,"direct","203.0.113.15")
    assert err.value.status_code==429
    with closing(database.get_connection()) as db:
        rows=db.execute("SELECT visitor_hash FROM ats_public_cv_attempts").fetchall()
        assert all("203.0.113" not in row["visitor_hash"] for row in rows)


def test_unpublished_job_cannot_accept_cv(intake):
    manager,position,job,pdf,applicant=intake
    with recruitment.write(manager.company_id) as db:
        db.execute("UPDATE ats_jobs SET state='closed' WHERE id=?",(job,))
    with pytest.raises(HTTPException) as err:
        ats_public_cv.submit(manager.company_id,job,applicant,"cv.pdf",pdf,"direct","203.0.113.19")
    assert err.value.status_code==404


def approve_synthetic_interview(manager,position,application_id,document_id,root):
    import json
    import hr_flow_screening
    hr_flow_screening.initialize_schema()
    with closing(database.get_connection()) as db:
        c=db.cursor()
        fields=json.loads(c.execute(
            "SELECT fields FROM ats_position_profiles WHERE position_id=?",
            (position,)).fetchone()["fields"])
        fields["matching_criteria"]="Yetkinlik | Depo deneyimi | 80 | depo"
        c.execute("UPDATE ats_position_profiles SET fields=? WHERE position_id=?",
                  (json.dumps(fields),position))
        db.commit()
    if root is None:
        root=database.DATABASE_PATH.parent / "synthetic_cv"
    result=hr_flow_screening.screen_position(manager,position,root)
    assert result["candidates"][0]["analysis_state"]=="analyzed"
    permission=hr_flow_screening.request_permission(manager,application_id,document_id)
    assert permission["status"]=="pending"
    assert hr_flow_screening.decide_permission(manager,permission["id"],"approved")["status"]=="approved"


def confirm_synthetic_appointment(company_id,application_id):
    with closing(database.get_connection()) as db:
        db.execute("""INSERT INTO flow_candidate_appointment
            (application_id,company_id,proposed_at,confirmed_at,confirmed_by,confirmed_on,updated_at)
            VALUES(?,?,?,?,?,?,?)""",
            (application_id,company_id,None,"2026-11-24T10:00:00+03:00",
             "candidate","2026-11-20T15:00:00+03:00",ats.now()))
        db.commit()


def test_selin_public_intake_queue_approval_scoped_and_once(intake,monkeypatch):
    import selin_workflow
    manager,position,job,pdf,applicant=intake
    manager.products=("ats","ai")
    result=ats_public_cv.submit(manager.company_id,job,applicant,"cv.pdf",pdf,"linkedin","203.0.113.33")
    app_id=result["application_id"]
    q=ats_public_cv.list_intake(manager.company_id)
    assert len(q)==1 and q[0]["position"]=="Depo Personeli"
    assert q[0]["status"]=="pending_review" and q[0]["source"]=="linkedin"
    assert ats_public_cv.list_intake(2222)==[]
    with pytest.raises(HTTPException) as unconfirmed:
        ats_public_cv.decide_intake(manager,app_id,"start",None,None,None)
    assert unconfirmed.value.status_code==409
    confirm_synthetic_appointment(manager.company_id,app_id)
    approve_synthetic_interview(manager,position,app_id,result["document_id"],None)
    calls=[]
    def fake_begin(actor,app,doc,*args):
        calls.append((actor.company_id,app,doc))
        return {"id":"case-01","status":"interview_created"}
    monkeypatch.setattr(selin_workflow,"begin",fake_begin)
    staff=SimpleNamespace(company_id=manager.company_id,user_id=33,role="hr_staff",products=("ats","ai"))
    with pytest.raises(HTTPException) as e:
        ats_public_cv.decide_intake(staff,app_id,"start",None,None,None)
    assert e.value.status_code==403
    outsider=SimpleNamespace(company_id=2222,user_id=99,role="hr_manager",products=("ats","ai"))
    with pytest.raises(HTTPException) as e:
        ats_public_cv.decide_intake(outsider,app_id,"start",None,None,None)
    assert e.value.status_code==404
    response=ats_public_cv.decide_intake(manager,app_id,"start",None,None,None)
    assert response["status"]=="assigned"
    assert calls==[(manager.company_id,app_id,result["document_id"])]
    assert ats_public_cv.list_intake(manager.company_id)[0]["case_id"]=="case-01"
    with pytest.raises(HTTPException) as e:
        ats_public_cv.decide_intake(manager,app_id,"start",None,None,None)
    assert e.value.status_code==409
    assert len(calls)==1


def test_selin_intake_skip_keeps_ats_record(intake):
    manager,position,job,pdf,applicant=intake
    manager.products=("ats","ai")
    result=ats_public_cv.submit(manager.company_id,job,applicant,"cv.pdf",pdf,"indeed","203.0.113.35")
    response=ats_public_cv.decide_intake(manager,result["application_id"],"skip",None,None,None)
    assert response["status"]=="skipped"
    with closing(database.get_connection()) as db:
        assert db.execute("SELECT COUNT(*) FROM ats_applications WHERE id=?",
                          (result["application_id"],)).fetchone()[0]==1
        assert db.execute("SELECT COUNT(*) FROM ats_documents WHERE id=?",
                          (result["document_id"],)).fetchone()[0]==1


def test_selin_intake_failed_processing_never_automatically_retries(intake,monkeypatch):
    import selin_workflow
    manager,position,job,pdf,applicant=intake
    manager.products=("ats","ai")
    result=ats_public_cv.submit(manager.company_id,job,applicant,"cv.pdf",pdf,"social","203.0.113.45")
    confirm_synthetic_appointment(manager.company_id,result["application_id"])
    approve_synthetic_interview(manager,position,result["application_id"],result["document_id"],None)
    calls=[]
    def fail(*args):
        calls.append(1)
        raise RuntimeError("synthetic")
    monkeypatch.setattr(selin_workflow,"begin",fail)
    with pytest.raises(RuntimeError):
        ats_public_cv.decide_intake(manager,result["application_id"],"start",None,None,None)
    assert ats_public_cv.list_intake(manager.company_id)[0]["status"]=="needs_attention"
    with pytest.raises(HTTPException) as e:
        ats_public_cv.decide_intake(manager,result["application_id"],"start",None,None,None)
    assert e.value.status_code==409 and len(calls)==1


def test_hr_flow_evidence_only_marks_durable_records_verified(intake):
    import hr_flow_evidence
    import selin_workflow
    from uuid import uuid4
    manager,position,job,pdf,applicant=intake
    import ernasa_training
    import academy_assignments
    ernasa_training.initialize_schema()
    academy_assignments.initialize_schema()
    selin_workflow.initialize_schema()
    entry=ats_public_cv.submit(manager.company_id,job,applicant,
                               "cv.pdf",pdf,"linkedin","203.0.113.100")
    case_id=uuid4().hex
    with closing(database.get_connection()) as db:
        db.execute("""INSERT INTO selin_cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (case_id,manager.company_id,entry["application_id"],
                    entry["document_id"],21,14,None,None,"created","{}",None,ats.now(),ats.now()))
        db.commit()
    proof=hr_flow_evidence.case_evidence(manager.company_id,case_id)
    steps={row["code"]:row for row in proof["steps"]}
    assert proof["product"]=="ERNASA HR FLOW"
    assert steps["application"]["state"]=="verified"
    assert steps["cv"]["state"]=="verified"
    assert steps["approval"]["state"]=="not_recorded"
    assert steps["interview"]["state"]=="not_recorded"
    assert steps["invite_email"]["state"]=="unverified"
    assert steps["pdf"]["state"]=="unverified"
    assert steps["training_email"]["state"]=="not_recorded"
    assert all("content_base64" not in row for row in proof["steps"])
    with pytest.raises(HTTPException) as error:
        hr_flow_evidence.case_evidence(9999,case_id)
    assert error.value.status_code==404
    with closing(database.get_connection()) as db:
        db.execute("UPDATE ats_document_blobs SET digest=? WHERE document_id=?",
                   ("0"*64,entry["document_id"]))
        db.commit()
    tampered=hr_flow_evidence.case_evidence(manager.company_id,case_id)
    assert next(row for row in tampered["steps"] if row["code"]=="cv")["state"]=="unverified"


def test_flow_contact_channels_are_prepared_but_no_delivery_claims(intake):
    import hr_flow_contacts
    manager,position,job,pdf,applicant=intake
    result=ats_public_cv.submit(manager.company_id,job,applicant,
                                "cv.pdf",pdf,"linkedin","203.0.113.89")
    app_id=result["application_id"]
    status=hr_flow_contacts.contact_status(manager.company_id,app_id)
    assert len(status["messages"])==1
    assert status["messages"][0]["channel"]=="email"
    assert status["messages"][0]["purpose"]=="availability_request"
    assert status["messages"][0]["status"]=="prepared"
    assert status["messages"][0]["provider_message_id"] is None
    assert status["linkedin"]["status"]=="integration_required"
    assert status["whatsapp"]["status"]=="future_opt_in"
    assert hr_flow_contacts.may_create_interview_link(manager.company_id,app_id,result["document_id"]) is False
    assert hr_flow_contacts.contact_status(9999,app_id)["messages"]==[]
    with closing(database.get_connection()) as db:
        db.execute("""INSERT INTO flow_candidate_appointment
            (application_id,company_id,proposed_at,confirmed_at,confirmed_by,
             confirmed_on,updated_at) VALUES(?,?,?,?,?,?,?)""",
            (app_id,manager.company_id,None,"2026-11-24T10:00:00+03:00",
             "candidate","2026-11-20T15:00:00+03:00",ats.now()))
        db.commit()
    # A confirmed appointment alone never bypasses the manager's ATS approval.
    assert hr_flow_contacts.may_create_interview_link(manager.company_id,app_id,result["document_id"]) is False
    approve_synthetic_interview(manager,position,app_id,result["document_id"],None)
    assert hr_flow_contacts.may_create_interview_link(manager.company_id,app_id,result["document_id"]) is True
    assert hr_flow_contacts.may_create_interview_link(9999,app_id,result["document_id"]) is False


def test_flow_position_screening_and_permission_full_human_gate(intake,tmp_path):
    import json
    import hr_flow_screening
    manager,position,job,pdf,applicant=intake
    hr_flow_screening.initialize_schema()
    entry=ats_public_cv.submit(manager.company_id,job,applicant,
                               "cv.pdf",pdf,"direct","203.0.113.51")
    with pytest.raises(HTTPException) as exc:
        hr_flow_screening.request_permission(manager,entry["application_id"],entry["document_id"])
    assert exc.value.status_code==409
    assert hr_flow_screening.process_one(tmp_path)
    assert not hr_flow_screening.process_one(tmp_path)
    with closing(database.get_connection()) as db:
        row = db.execute("SELECT analysis_state,match_score FROM flow_screening_results").fetchone()
        assert row["analysis_state"] == "criteria_missing"
        assert row["match_score"] is None
    request = hr_flow_screening.request_permission(manager,entry["application_id"],entry["document_id"])
    assert request["status"] == "pending"
    with closing(database.get_connection()) as db:
        c=db.cursor()
        c.execute("SELECT fields FROM ats_position_profiles WHERE position_id=?",(position,))
        fields=json.loads(c.fetchone()["fields"])
        fields["matching_criteria"]="Yetkinlik | WMS deneyimi | 80 | wms"
        c.execute("UPDATE ats_position_profiles SET fields=? WHERE position_id=?",
                  (json.dumps(fields),position))
        db.commit()
    screened=hr_flow_screening.screen_position(manager,position,tmp_path)
    row=screened["candidates"][0]
    assert row["analysis_state"]=="analyzed"
    assert row["match_score"] is not None
    assert row["coverage"] is not None
    assert row["document_id"]==entry["document_id"]
    request=hr_flow_screening.request_permission(manager,entry["application_id"],entry["document_id"])
    assert request["status"]=="pending"
    repeated=hr_flow_screening.request_permission(manager,entry["application_id"],entry["document_id"])
    assert repeated["id"]==request["id"]
    with closing(database.get_connection()) as db:
        assert db.execute("SELECT COUNT(*) FROM interview_links").fetchone()[0]==0
        assert db.execute("SELECT COUNT(*) FROM ik_invitation_email_sends").fetchone()[0]==0
    staff=SimpleNamespace(company_id=manager.company_id,user_id=44,
                          role="hr_staff",products=("ats",))
    with pytest.raises(HTTPException) as exc:
        hr_flow_screening.decide_permission(staff,request["id"],"approved")
    assert exc.value.status_code==403
    stranger=SimpleNamespace(company_id=3333,user_id=10,
                             role="hr_manager",products=("ats",))
    with pytest.raises(HTTPException) as exc:
        hr_flow_screening.decide_permission(stranger,request["id"],"approved")
    assert exc.value.status_code==404
    outcome=hr_flow_screening.decide_permission(manager,request["id"],"approved")
    assert outcome["status"]=="approved"
    with pytest.raises(HTTPException) as exc:
        hr_flow_screening.decide_permission(manager,request["id"],"approved")
    assert exc.value.status_code==409
    assert hr_flow_screening.screen_position(manager,position,tmp_path)["candidates"][0]["permission"]["status"]=="approved"
    with closing(database.get_connection()) as db:
        assert db.execute("SELECT COUNT(*) FROM interview_links").fetchone()[0]==0


def test_queued_cv_upload_scores_synthetic_wms_cv(intake, tmp_path, monkeypatch):
    import json

    import ats_upload_queue
    import hr_flow_screening

    manager, position, job, pdf, applicant = intake
    entry = ats_public_cv.submit(manager.company_id, job, applicant,
                                 "original.pdf", pdf, "direct", "203.0.113.52")
    assert hr_flow_screening.process_one(tmp_path)
    with closing(database.get_connection()) as db:
        profile = db.execute("SELECT fields FROM ats_position_profiles WHERE position_id=?",
                             (position,)).fetchone()
        fields = json.loads(profile["fields"])
        fields["matching_criteria"] = "Yetkinlik | WMS deneyimi | 80 | wms"
        db.execute("UPDATE ats_position_profiles SET fields=? WHERE position_id=?",
                   (json.dumps(fields), position))
        db.commit()

    manager.portal = ats_upload_queue.control.Portal("synthetic", "")
    manager.access_version = "test"
    monkeypatch.setattr(ats_upload_queue.control, "control_call", lambda *args, **kwargs: {
        "company_id": manager.company_id, "version": manager.access_version,
        "role": manager.role, "products": manager.products,
    })
    # Generate a distinct PDF for the existing application.
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 100), "Warehouse experience: WMS and inventory tracking.")
        replacement = doc.tobytes()
    upload = ats_upload_queue.enqueue(manager, entry["application_id"], "replacement.pdf", replacement)
    assert ats_upload_queue.process_one(tmp_path)
    assert ats_upload_queue.jobs(manager, entry["application_id"])[0]["status"] == "ready"
    assert hr_flow_screening.process_one(tmp_path)
    assert not hr_flow_screening.process_one(tmp_path)
    with closing(database.get_connection()) as db:
        row = db.execute("SELECT * FROM flow_screening_results WHERE document_id=?",
                         (upload["id"],)).fetchone()
        assert row["company_id"] == manager.company_id
        assert row["position_id"] == position
        assert row["analysis_state"] == "analyzed"
        assert row["match_score"] == 100
        assert db.execute("SELECT COUNT(*) FROM interview_links").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM flow_interview_permissions").fetchone()[0] == 0


def test_flow_screening_honors_staff_visibility_and_stale_criteria(intake,tmp_path):
    import json
    import hr_flow_screening
    manager,position,job,pdf,applicant=intake
    hr_flow_screening.initialize_schema()
    entry=ats_public_cv.submit(manager.company_id,job,applicant,
                               "cv.pdf",pdf,"direct","203.0.113.52")
    with closing(database.get_connection()) as db:
        c=db.cursor()
        fields=json.loads(c.execute("SELECT fields FROM ats_position_profiles WHERE position_id=?",
                                    (position,)).fetchone()["fields"])
        fields["matching_criteria"]="Yetkinlik | Depo deneyimi | 70 | depo"
        c.execute("UPDATE ats_position_profiles SET fields=? WHERE position_id=?",
                  (json.dumps(fields),position))
        db.commit()
    staff=SimpleNamespace(company_id=manager.company_id,user_id=44,
                          role="hr_staff",products=("ats",))
    with pytest.raises(HTTPException) as unauthorized_position:
        hr_flow_screening.screen_position(staff,position,tmp_path)
    assert unauthorized_position.value.status_code==404
    with closing(database.get_connection()) as db:
        db.execute("INSERT INTO ats_position_members VALUES(?,?,?)",
                   (manager.company_id,position,staff.user_id))
        db.commit()
    assert hr_flow_screening.screen_position(staff,position,tmp_path)["total"]==0
    with pytest.raises(HTTPException) as exc:
        hr_flow_screening.request_permission(staff,entry["application_id"],entry["document_id"])
    assert exc.value.status_code==404
    result=hr_flow_screening.screen_position(manager,position,tmp_path)
    assert result["candidates"][0]["analysis_state"]=="analyzed"
    with closing(database.get_connection()) as db:
        c=db.cursor()
        fields["matching_criteria"]="Yetkinlik | CRM deneyimi | 70 | crm"
        c.execute("UPDATE ats_position_profiles SET fields=? WHERE position_id=?",
                  (json.dumps(fields),position))
        db.commit()
    with pytest.raises(HTTPException) as exc:
        hr_flow_screening.request_permission(manager,entry["application_id"],entry["document_id"])
    assert exc.value.status_code==409


def test_interview_readiness_fails_when_source_or_approval_changes(intake):
    import hr_flow_contacts
    manager,position,job,pdf,applicant=intake
    result=ats_public_cv.submit(manager.company_id,job,applicant,"cv.pdf",pdf,"direct","203.0.113.84")
    app_id,doc=result["application_id"],result["document_id"]
    assert hr_flow_contacts.may_create_interview_link(manager.company_id,app_id,doc) is False
    confirm_synthetic_appointment(manager.company_id,app_id)
    assert hr_flow_contacts.may_create_interview_link(manager.company_id,app_id,doc) is False
    approve_synthetic_interview(manager,position,app_id,doc,None)
    assert hr_flow_contacts.may_create_interview_link(manager.company_id,app_id,doc) is True
    assert hr_flow_contacts.may_create_interview_link(manager.company_id,app_id,"wrong-document") is False
    with closing(database.get_connection()) as db:
        db.execute("UPDATE flow_candidate_appointment SET confirmed_by='staff' WHERE application_id=?",(app_id,))
        db.commit()
    assert hr_flow_contacts.may_create_interview_link(manager.company_id,app_id,doc) is False
    with closing(database.get_connection()) as db:
        db.execute("UPDATE flow_candidate_appointment SET confirmed_by='candidate' WHERE application_id=?",(app_id,))
        db.execute("UPDATE flow_interview_permissions SET status='declined' WHERE application_id=?",(app_id,))
        db.commit()
    assert hr_flow_contacts.may_create_interview_link(manager.company_id,app_id,doc) is False


@pytest.mark.parametrize("intake", ["sqlite", "postgres"], indirect=True)
def test_synthetic_flow_approval_booking_invite_and_pdf(intake, tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from urllib.parse import urlsplit

    from fastapi.testclient import TestClient

    import hr_flow_booking
    import hr_flow_contacts
    import hr_flow_screening
    import selin_workflow
    import openai

    monkeypatch.setattr(openai,"OpenAI",lambda **kwargs: object())
    from backend import main
    from services import invite_email

    manager, position, job, pdf, applicant = intake
    selin_workflow.initialize_schema()
    monkeypatch.setattr(main, "UPLOAD_DIRECTORY", tmp_path)
    monkeypatch.setenv("ERNASA_IK_CONTROL_URL", "https://control.example.test")
    portal = main.portal_access.Portal("synthetic", "https://synthetic.ernasa.com")
    actor = main.portal_access.Actor(manager.company_id, manager.role, portal, manager.user_id,
                                     "revision-1", "Sentetik Firma", products=("ats", "ai"))

    def control_call(portal, operation, **kwargs):
        if operation == "session":
            return {"company_id": actor.company_id, "role": actor.role, "version": actor.access_version,
                    "company_name": actor.company_name, "products": actor.products}
        if operation == "resolve":
            return {"company_id": actor.company_id if portal.slug == "synthetic" else 4444,
                    "name": actor.company_name, "products": actor.products}
        assert operation == "interview_template"
        return {}

    monkeypatch.setattr(main.portal_access, "control_call", control_call)
    monkeypatch.setattr(invite_email, "configured_sender", lambda: ("fake", "synthetic@example.test"))
    outgoing = []

    def send_availability(candidate, url, **kwargs):
        assert candidate["email"] == applicant.email
        outgoing.append(("availability", url))
        return "synthetic-availability-id"

    def send_invitation(candidate, url, **kwargs):
        assert candidate["email"] == applicant.email
        assert candidate["scheduled_at"]
        assert "Türkiye saati" in invite_email.invitation_message(candidate, url)[2]
        outgoing.append(("invitation", url))
        return "synthetic-interview-id"

    monkeypatch.setattr(invite_email, "send_invitation", send_invitation)
    client = TestClient(main.app, base_url=portal.origin)
    foreign = TestClient(main.app, base_url="https://other.ernasa.com")
    assert client.get("/basvuru/"+job).status_code == 200
    assert foreign.get("/basvuru/"+job).status_code == 404
    result = client.post("/api/ats/jobs/"+job+"/apply-with-cv",
        data={"name":applicant.name,"email":applicant.email,"notice_version":applicant.notice_version,
              "acknowledge":"true","source":"direct"},
        files={"cv":("cv.pdf",pdf,"application/pdf")}, headers={"Origin":portal.origin})
    assert result.status_code == 201, result.text
    entry = ats_public_cv.list_intake(actor.company_id)[0]
    app_id, doc_id = entry["application_id"], entry["document_id"]
    assert hr_flow_screening.process_one(tmp_path)
    assert not hr_flow_booking.process_one(main.start_booked_flow, send_availability)
    permission = hr_flow_screening.request_permission(actor, app_id, doc_id)
    main.flow_decide_interview_permission(permission["id"], main.FlowPermissionDecision(decision="approved"), actor)
    assert hr_flow_booking.process_one(main.start_booked_flow, send_availability)
    assert not hr_flow_booking.process_one(main.start_booked_flow, send_availability)
    assert [kind for kind, _ in outgoing] == ["availability"]
    url = urlsplit(outgoing[0][1]).path
    start = (datetime.now(hr_flow_booking.LOCAL_ZONE) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    assert client.get(url).status_code == 200
    assert foreign.get(url).status_code == 404
    assert client.post(url, data={"starts_at":"2020-01-01T09:00"}).status_code == 422
    assert "Görüşme saatiniz kaydedildi" in client.post(url, data={"starts_at":start}).text
    assert client.post(url, data={"starts_at":start}).status_code == 200
    assert hr_flow_contacts.may_create_interview_link(actor.company_id, app_id, doc_id)
    assert hr_flow_booking.process_one(main.start_booked_flow, send_availability)
    assert not hr_flow_booking.process_one(main.start_booked_flow, send_availability)
    assert [kind for kind, _ in outgoing] == ["availability", "invitation"]
    token = urlsplit(outgoing[1][1]).path.rsplit("/",1)[1]
    early = main.portal_interviews.start
    with pytest.raises(HTTPException) as ex:
        early(token, actor.company_id, main._link_deadline_passed, main._intro_question, lambda *args: None)
    assert ex.value.status_code == 409
    info = main.get_interview_link(token,actor.company_id)
    assert info["schedule_state"] == "scheduled" and info["can_start"] is False
    assert main.mark_interview_opened(token,actor.company_id)["can_start"] is False
    starts_at = datetime.fromisoformat(info["scheduled_at"])
    assert datetime.fromisoformat(info["expires_at"]) - starts_at == timedelta(hours=8)
    with closing(database.get_connection()) as db:
        assert db.cursor().execute("SELECT COUNT(*) FROM ats_schedules").fetchone()[0] == 1
        assert db.cursor().execute("SELECT COUNT(*) FROM interview_links").fetchone()[0] == 1
        record = db.cursor().execute("SELECT expires_at FROM interview_links WHERE token=?", (token,)).fetchone()
        assert datetime.fromisoformat(record["expires_at"]) > datetime.now(timezone.utc) + timedelta(days=1)
        app = db.cursor().execute("SELECT closure FROM ats_applications WHERE id=?", (app_id,)).fetchone()
        assert app["closure"] is None
        db.cursor().execute("UPDATE interview_links SET status='completed' WHERE token=?", (token,))
        db.cursor().execute("INSERT INTO interview_reports VALUES (?,?,?)", (token, "Sentetik mülakat değerlendirmesi", ats.now()))
        db.commit()
    assert selin_workflow.process_one()
    case = selin_workflow.list_cases(actor.company_id)[0]
    assert case["report_available"] == 1
    assert main.selin_case_pdf(case["id"], actor).body.startswith(b"%PDF-")


@pytest.mark.parametrize("intake", ["sqlite", "postgres"], indirect=True)
def test_import_same_email_different_candidates_and_latest_cv(intake,tmp_path):
    from io import BytesIO
    from uuid import uuid4

    from docx import Document

    import ats_cv_import
    import hr_flow_screening

    manager, position, _, _, _ = intake
    email = "shared@example.test"

    def cv(name, phone, skill):
        document = Document()
        for line in (f"Ad Soyad: {name}", email, phone, "Eğitim", "Lojistik ön lisans", "Deneyim", skill):
            document.add_paragraph(line)
        output = BytesIO()
        document.save(output)
        return output.getvalue()

    def save(name, phone, skill):
        content = cv(name, phone, skill)
        preview = ats_cv_import.preview("candidate.docx",content,manager)
        assert preview["name"] == name
        assert preview["education"] == ["Lojistik ön lisans"]
        assert preview["experience"] == [skill]
        assert preview["suggested_position_id"] == position
        fields = ats_cv_import.Selection(import_id=uuid4(),position_id=position,name=name,email=email,phone=phone)
        return ats_cv_import.save(tmp_path,manager,fields,"candidate.docx",content)

    first = save("Zeynep Demir","0531 111 22 33","Depo yönetimi")
    second = save("Deniz Karaca","0531 444 55 66","Stok takibi")
    updated = save("Deniz Karaca","0531 444 55 66","Stok ve WMS")
    assert first["application_id"] != second["application_id"]
    assert second["identity_conflict"]
    assert updated["existing_application"]
    assert updated["application_id"] == second["application_id"]
    assert updated["document_id"] != second["document_id"]
    for item in (first,second,updated):
        assert hr_flow_screening.process_one(tmp_path)
        assert ats_cv_import.screening_status(manager,item["application_id"],item["document_id"])["state"] != "pending"
    assert not hr_flow_screening.process_one(tmp_path)
    assert ats.list_applications(manager,email,limit=1,name_order="asc")["applications"][0]["name"] == "Deniz Karaca"
    assert ats.list_applications(manager,email,limit=1,offset=1,name_order="asc")["applications"][0]["name"] == "Zeynep Demir"
    rows = ats.list_applications(manager,email,limit=1,name_order="desc")
    assert rows["total"] == 2 and rows["applications"][0]["name"] == "Zeynep Demir"
    stranger = SimpleNamespace(company_id=4444,user_id=41,role="hr_manager",products=("ats",))
    assert ats.list_applications(stranger,email)["total"] == 0
    with pytest.raises(HTTPException) as error:
        ats_cv_import.screening_status(stranger,updated["application_id"],updated["document_id"])
    assert error.value.status_code == 404
    second_app = ats.details(manager,second["application_id"])
    assert not second_app["application"]["email"]
    assert len(second_app["documents"]) == 2


@pytest.mark.parametrize("intake", ["sqlite", "postgres"], indirect=True)
def test_legacy_calendar_blocks_early_and_expired_starts(intake):
    from datetime import datetime, timedelta, timezone

    import ats_workflow
    import portal_interviews

    actor, _, job, pdf, applicant = intake
    entry = ats_public_cv.submit(actor.company_id,job,applicant,"cv.pdf",pdf,"direct","203.0.113.123")
    token = "synthetic-interview"
    now = datetime.now(timezone.utc)
    starts = now + timedelta(hours=1)
    schedule = ats_workflow.save_schedule(actor,entry["application_id"],ats_workflow.Schedule(
        interviewer_id=actor.user_id,starts_at=starts.isoformat(),
        ends_at=(starts + timedelta(minutes=30)).isoformat(),kind="video"))
    with closing(database.get_connection()) as db:
        db.cursor().execute("""INSERT INTO interview_links
            (token,company_id,application_id,status,expires_at,created_at)
            VALUES(?,?,?,'waiting',?,?)""",(token,actor.company_id,entry["application_id"],
                                          (now+timedelta(days=1)).isoformat(),now.isoformat()))
        db.commit()
    def start():
        return portal_interviews.start(token,actor.company_id,lambda row:False,lambda:"Intro",lambda *args:"Soru?")
    with pytest.raises(HTTPException) as error:
        start()
    assert error.value.status_code == 409 and "Türkiye saati" in error.value.detail
    with closing(database.get_connection()) as db:
        cursor = db.cursor()
        interview = portal_interviews.load_interview(cursor,token,actor.company_id)
        assert portal_interviews.availability(cursor,interview,actor.company_id,now=starts)["state"] == "available"
        assert portal_interviews.availability(cursor,interview,actor.company_id,now=starts+timedelta(hours=8))["state"] == "expired"
        cursor.execute("UPDATE ats_schedules SET starts_at=? WHERE id=?",((now-timedelta(hours=9)).isoformat(),schedule["id"]))
        db.commit()
    with pytest.raises(HTTPException) as error:
        start()
    assert error.value.status_code == 410


@pytest.mark.parametrize("intake", ["sqlite", "postgres"], indirect=True)
def test_public_ats_groups_and_expired_job_preserve_applications(intake,tmp_path):
    import json
    from datetime import datetime,timedelta,timezone
    import hr_flow_screening

    actor, position, job, _, applicant = intake
    with closing(database.get_connection()) as db:
        db.cursor().execute("UPDATE ats_position_profiles SET fields=? WHERE position_id=?",
            (json.dumps({"matching_criteria":"Yetkinlik | WMS | 60 | WMS\nYetkinlik | CRM | 40 | CRM"}),position))
        db.commit()
    scenarios = [("WMS ve CRM kullandim", "suitable"), ("WMS kullandim; CRM bilgim yok", "development"),
                 ("Lojistik depo operasyonu", "review")]
    for index, (skills, group) in enumerate(scenarios):
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((72,100),f"Experience\n{skills}")
            content = pdf.tobytes()
        candidate = applicant.model_copy(update={"name":f"Sentetik Aday {index}", "email":f"candidate{index}@example.test"})
        entry = ats_public_cv.submit(actor.company_id,job,candidate,"cv.pdf",content,"direct",f"203.0.113.{10+index}")
        assert hr_flow_screening.process_one(tmp_path)
        result = ats.list_applications(actor,classification=group)
        assert result["total"] == 1
        assert result["applications"][0]["id"] == entry["application_id"]
        assert hr_flow_screening.request_permission(actor,entry["application_id"],entry["document_id"])["status"] == "pending"
    with closing(database.get_connection()) as db:
        db.cursor().execute("UPDATE ats_job_deadlines SET closes_at=? WHERE job_id=?",
                            ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),job))
        db.commit()
    with pytest.raises(HTTPException) as error:
        ats_public_cv.submit(actor.company_id,job,applicant,"cv.pdf",content,"direct","203.0.113.241")
    assert error.value.status_code == 404
    assert ats.list_applications(actor,position_id=position)["total"] == 3
