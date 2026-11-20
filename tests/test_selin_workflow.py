"""Synthetic tests of the SELİN CV → interview → PDF → academy chain."""
import json
from contextlib import closing
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import academy_assignments
import database
import ernasa_training
import selin_workflow
from services import invite_email


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_URL", None)
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "selin.db")
    monkeypatch.setenv("ERNASA_SELIN_SEND_ENABLED", "1")
    monkeypatch.delenv("ERNASA_SELIN_WORKER_ENABLED", raising=False)
    ernasa_training.initialize_schema()
    academy_assignments.initialize_schema()
    with closing(database.get_connection()) as db:
        c=db.cursor()
        c.execute("""CREATE TABLE interview_links (
            token TEXT PRIMARY KEY, company_id BIGINT NOT NULL, name TEXT, email TEXT,
            company TEXT, position TEXT, status TEXT NOT NULL
        )""")
        c.execute("CREATE TABLE interview_reports(token TEXT PRIMARY KEY,job_fit_analysis TEXT)")
        c.execute("""CREATE TABLE company_positions (
            id TEXT PRIMARY KEY,company_id BIGINT NOT NULL,name TEXT NOT NULL,open INTEGER NOT NULL
        )""")
        c.execute("INSERT INTO company_positions VALUES(?,?,?,?)",
                  ("position-1",1001,"Satış Temsilcisi",1))
        c.execute("""CREATE TABLE interview_answers (
            id TEXT PRIMARY KEY,token TEXT,question_number INTEGER,question TEXT,answer TEXT
        )""")
        db.commit()
    selin_workflow.initialize_schema()
    actor=SimpleNamespace(company_id=1001,role="hr_manager",products=("ats","ai"),user_id=24)
    monkeypatch.setattr(selin_workflow.ats, "application",
        lambda cursor,who,app_id: {"id":app_id,"closure":None,"position_id":"position-1",
                                    "email":"synthetic@example.test"} if who.company_id==1001
        else (_ for _ in ()).throw(HTTPException(404,"Unknown app")))
    # These workflow-state tests simulate a previously approved, confirmed
    # candidate; readiness itself is verified against the real ATS fixture below.
    monkeypatch.setattr(selin_workflow.hr_flow_contacts,"require_interview_ready",
        lambda tenant,app,doc: None)
    monkeypatch.setattr(selin_workflow.ats_matching, "evidence_match",
        lambda root,who,app,doc: {"match_score":81.2,"evidence_coverage":75,
                                   "source":"original_document"})
    monkeypatch.setattr(selin_workflow.ats_matching, "source_text",
        lambda root,who,app,doc: "Python, CRM ve 5 yıllık müşteri ilişkileri deneyimi")
    actions={"interview":0,"mail":0,"training":0}

    def create_interview(data, person, application, **kwargs):
        actions["interview"]+=1
        assert data["cv_text"] and kwargs["cv_document_id"]=="document-1"
        token="a"*43
        with closing(database.get_connection()) as db:
            db.execute("INSERT INTO interview_links VALUES(?,?,?,?,?,?,?)",
                (token,person.company_id,"Kurgusal Aday","synthetic@example.test",
                 "ERNASA Sentetik","Satış Temsilcisi","waiting"))
            db.commit()
        return {"interview_id":token}

    def send_interview(token, person):
        actions["mail"]+=1
        assert token=="a"*43 and person.company_id==1001
        return {"success":True}

    def send_training(**kwargs):
        actions["training"]+=1
        assert kwargs["recipient"]=="synthetic@example.test"
        assert kwargs["url"].startswith("https://ernasa.com/egitim/")
        return "fake-provider-confirmation"

    monkeypatch.setattr(invite_email, "send_training_invitation", send_training)
    return actor,actions,create_interview,send_interview,monkeypatch


def start_case(pipeline):
    actor,actions,create,send,_=pipeline
    return selin_workflow.begin(actor,"application-1","document-1","/unreferenced",create,send)


def test_full_chain_with_report_archive_and_training_review(pipeline):
    actor,actions,create,send,_=pipeline
    row=start_case(pipeline)
    assert row["status"]=="interview_invited"
    assert actions=={"interview":1,"mail":1,"training":0}
    again=start_case(pipeline)
    assert again["id"]==row["id"]
    assert actions["interview"]==1 and actions["mail"]==1

    with closing(database.get_connection()) as db:
        db.execute("UPDATE interview_links SET status='completed' WHERE token=?",("a"*43,))
        report=("GELİŞTİRİLMESİ GEREKEN ALANLAR:\n- CRM, KPI, 30/60/90 gün.\n"
                "DİKKAT EDİLMESİ GEREKEN NOKTALAR:\n- Ek gözlem gerekli.")
        db.execute("INSERT INTO interview_reports VALUES(?,?)",("a"*43,report))
        for n in range(1,11):
            db.execute("INSERT INTO interview_answers VALUES(?,?,?,?,?)",
                       (f"question-{n}","a"*43,n,f"Soru {n}",f"Yanıt {n}"))
        db.commit()
    assert selin_workflow.process_one() is True
    current=selin_workflow.get_case(actor.company_id,row["id"])
    assert current["status"]=="training_invited"
    assert actions["training"]==1
    assert current["training_token_hash"]
    pdf=selin_workflow.archived_pdf(actor.company_id,row["id"])
    assert pdf.startswith(b"%PDF-")
    with closing(database.get_connection()) as db:
        found=db.execute("SELECT COUNT(*) FROM selin_report_archives").fetchone()[0]
        assert found==1
        modules=json.loads(db.execute(
            "SELECT modules_json FROM ernasa_training_assignments WHERE token_hash=?",
            (current["training_token_hash"],)).fetchone()[0])
        assert "demo" in modules
        db.execute("INSERT INTO ernasa_training_v9_progress VALUES(?,?,?)",
                   (current["training_token_hash"],json.dumps({
                       "done":{m:True for m in modules},
                       "answers":{modules[0]:{"0":1}}
                   }),"2026-11-20T10:00:00+00:00"))
        db.commit()
    assert selin_workflow.process_one() is True
    final=selin_workflow.evaluation(actor.company_id,row["id"])
    assert final["status"]=="evaluation_ready"
    assert final["training"]["completed_lessons"]==len(modules)
    assert final["training"]["is_self_reported"] is True
    assert final["training"]["exam_success_verified"] is False
    assert "Kurucu" in final["decision"]
    assert actions["training"]==1
    assert selin_workflow.process_one() is False


def test_company_isolation_and_manager_gate(pipeline):
    actor,actions,create,send,_=pipeline
    row=start_case(pipeline)
    assert len(selin_workflow.list_cases(1001))==1
    assert selin_workflow.list_cases(2002)==[]
    with pytest.raises(HTTPException) as exc:
        selin_workflow.get_case(2002,row["id"])
    assert exc.value.status_code==404
    with pytest.raises(HTTPException) as exc:
        selin_workflow.archived_pdf(2002,row["id"])
    assert exc.value.status_code==404
    actor.role="hr_staff"
    with pytest.raises(HTTPException) as exc:
        selin_workflow.begin(actor,"application-2","document-1","/unused",create,send)
    assert exc.value.status_code==403


def test_disabled_send_creates_no_outbound_effect(pipeline):
    actor,actions,create,send,monkeypatch=pipeline
    monkeypatch.delenv("ERNASA_SELIN_SEND_ENABLED")
    row=start_case(pipeline)
    assert row["status"]=="interview_created"
    assert actions["interview"]==1 and actions["mail"]==0 and actions["training"]==0
    assert selin_workflow.process_one() is False


def test_no_auto_retries_after_ambiguous_email_failure(pipeline):
    actor,actions,create,send,_=pipeline
    def uncertain(token,person):
        actions["mail"]+=1
        raise TimeoutError("Do not retry blindly")
    with pytest.raises(TimeoutError):
        selin_workflow.begin(actor,"application-1","document-1","/unreferenced",
                             create,uncertain)
    assert actions["mail"]==1 and actions["interview"]==1
    row=selin_workflow.list_cases(1001)[0]
    assert row["status"]=="needs_attention"
    same=start_case(pipeline)
    assert same["id"]==row["id"]
    assert actions["mail"]==1 and actions["interview"]==1


def test_requires_both_products_and_rejects_unreadable_cv(pipeline):
    actor,actions,create,send,monkeypatch=pipeline
    actor.products=("ai",)
    with pytest.raises(HTTPException) as exc:
        start_case(pipeline)
    assert exc.value.status_code==403
    actor.products=("ats","ai")
    monkeypatch.setattr(selin_workflow.ats_matching,"source_text",lambda *_:"")
    with pytest.raises(HTTPException) as exc:
        start_case(pipeline)
    assert exc.value.status_code==422
    assert selin_workflow.list_cases(actor.company_id)==[]


def test_direct_selin_start_never_mints_interview_without_approval(pipeline):
    actor,actions,create,send,monkeypatch=pipeline
    def not_ready(tenant,application,document):
        assert (tenant,application,document)==(1001,"application-1","document-1")
        raise HTTPException(409,"Onay ve aday teyidi gerekli.")
    monkeypatch.setattr(selin_workflow.hr_flow_contacts,"require_interview_ready",not_ready)
    with pytest.raises(HTTPException) as exc:
        start_case(pipeline)
    assert exc.value.status_code==409
    assert actions=={"interview":0,"mail":0,"training":0}
    assert selin_workflow.list_cases(actor.company_id)==[]
