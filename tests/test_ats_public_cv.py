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
def intake(tmp_path,monkeypatch):
    monkeypatch.setattr(database,"DATABASE_URL",None)
    monkeypatch.setattr(database,"DATABASE_PATH",tmp_path/"public-cv.db")
    monkeypatch.setenv("ERNASA_PUBLIC_CV_ENABLED","1")
    monkeypatch.setenv("ERNASA_PUBLIC_CV_RATE_SECRET","synthetic-only-secret-key-abcdef0123456789")
    database.initialize_database()
    ats_public_cv.initialize_schema()
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
