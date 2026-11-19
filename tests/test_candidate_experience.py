"""Synthetic tests: anonymous candidate survey, CONTROL aggregates, clickable mail."""
from contextlib import closing
from hashlib import sha256
import re
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import database
import ernasa_training
import candidate_experience
import academy_assignments
from services import invite_email


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(database,"DATABASE_URL",None)
    monkeypatch.setattr(database,"DATABASE_PATH",tmp_path/"survey.db")
    monkeypatch.setenv("IK_SESSION_SECRET","synthetic-test-secret-"+("A"*48))
    ernasa_training.initialize_schema()
    academy_assignments.initialize_schema()
    candidate_experience.initialize_schema()
    def actor(req,write=False):
        tenant=req.headers.get("X-Tenant","1001")
        if write and req.headers.get("Origin")!="https://ernasa.com":
            raise HTTPException(403,"Origin")
        return int(tenant),42
    monkeypatch.setattr(academy_assignments,"_actor",actor)
    app=FastAPI()
    app.include_router(academy_assignments.router)
    app.include_router(candidate_experience.router)
    return TestClient(app)


def assigned(client):
    r=client.post("/api/egitim/ik/atama",
      headers={"Origin":"https://ernasa.com"},
      json={"recipient":"Sentetik Katılımcı","email":"synthetic@example.com",
            "module_ids":["vizyon"],"program_id":"b2b_satis","days":14})
    assert r.status_code==201,r.text
    return r.json()["link"].split("/")[-1]


def payload(token,source="egitim"):
    return {"source":source,"token":token,"ease":4,"clarity":3,"overall":5,
            "training":5 if source=="egitim" else None,
            "interview":4 if source=="mulakat" else None,"voluntary":True,
            "comment":"Arayüzün daha anlaşılır olmasını isterim."}


def test_training_survey_once_pseudonymous_and_aggregated(client):
    token=assigned(client)
    r=client.post("/api/aday-deneyimi",json=payload(token),headers={"Origin":"https://ernasa.com"})
    assert r.status_code==200,r.text
    assert r.json()=={"received":True,"already_submitted":False}
    again=client.post("/api/aday-deneyimi",json=payload(token),headers={"Origin":"https://ernasa.com"})
    assert again.json()["already_submitted"] is True
    with closing(database.get_connection()) as db:
        rows=db.cursor().execute("SELECT * FROM ernasa_candidate_experience").fetchall()
    assert len(rows)==1
    assert token not in str(dict(rows[0]))
    assert "synthetic@example.com" not in str(dict(rows[0]))
    summary=candidate_experience.summary()
    assert summary["egitim"]["count"]==1
    assert summary["egitim"]["ratings"]["overall"]["average"]==5
    assert summary["egitim"]["comments"]==[]


def test_survey_rejects_cross_origin_and_no_consent(client):
    token=assigned(client)
    r=client.post("/api/aday-deneyimi",json=payload(token),
       headers={"Origin":"https://example.invalid"})
    assert r.status_code==403
    data=payload(token);data["voluntary"]=False
    r=client.post("/api/aday-deneyimi",json=data,headers={"Origin":"https://ernasa.com"})
    assert r.status_code==422


def test_interview_requires_completed_status(client):
    with closing(database.get_connection()) as db:
        db.cursor().execute("CREATE TABLE interview_links(token TEXT PRIMARY KEY,status TEXT NOT NULL)")
        db.cursor().execute("INSERT INTO interview_links(token,status) VALUES(?,?)",("synthetic-interview-token-0001","started"))
        db.commit()
    data=payload("synthetic-interview-token-0001","mulakat")
    r=client.post("/api/aday-deneyimi",json=data,headers={"Origin":"https://ernasa.com"})
    assert r.status_code==404
    with closing(database.get_connection()) as db:
        db.cursor().execute("UPDATE interview_links SET status='completed'")
        db.commit()
    r=client.post("/api/aday-deneyimi",json=data,headers={"Origin":"https://ernasa.com"})
    assert r.status_code==200,r.text
    assert candidate_experience.summary()["mulakat"]["count"]==1


def test_email_send_is_explicit_and_company_scoped(client,monkeypatch):
    token=assigned(client)
    calls=[]
    monkeypatch.setattr(invite_email,"send_training_invitation",lambda **kwargs: calls.append(kwargs) or "mock-provider-id")
    assert client.post("/api/egitim/ik/davet/"+token+"/gonder",headers={
       "Origin":"https://ernasa.com","X-Tenant":"2002"}).status_code==404
    assert calls==[]
    r=client.post("/api/egitim/ik/davet/"+token+"/gonder",headers={"Origin":"https://ernasa.com"})
    assert r.status_code==200,r.text
    assert r.json()["sent"] is True
    assert len(calls)==1
    assert calls[0]["url"]=="https://ernasa.com/egitim/"+token
    assert calls[0]["recipient"]=="synthetic@example.com"


def test_provider_builds_real_html_button_not_plain_mailto(monkeypatch):
    class DummyResponse:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,n):return b'{"id":"sent-mock"}'
    seen=[]
    def fake_urlopen(req,timeout):
        seen.append(__import__("json").loads(req.data))
        return DummyResponse()
    monkeypatch.setenv("ERNASA_RESEND_API_KEY","test-secret")
    monkeypatch.setenv("ERNASA_EMAIL_FROM","iletisim@ernasa.com")
    monkeypatch.setattr(invite_email,"urlopen",fake_urlopen)
    link="https://ernasa.com/egitim/"+("a"*43)
    result=invite_email.send_training_invitation(
        recipient="test@example.com",name="Test",url=link,
        expires_at="2026-12-02T18:20:21+00:00",idempotency_key="test")
    assert result=="sent-mock"
    assert '<a href="'+link+'"' in seen[0]["html"]
    assert "Eğitime Başla" in seen[0]["html"]
    assert link in seen[0]["text"]
    assert "mailto:" not in seen[0]["html"]


def test_postgres_cursor_wrapper_supports_sqlite_style_chained_fetch(monkeypatch):
    """psycopg2 execute returns None, but existing app code chains .fetchall()."""
    class DriverCursor:
        def __init__(self):
            self.query = None
            self.params = None
        def execute(self, query, params=None):
            self.query, self.params = query, params
            return None  # psycopg2 behavior
        def executemany(self, query, params):
            self.query, self.params = query, params
            return None
        def fetchall(self):
            return [{"source": "egitim", "overall": 5}]
        def fetchone(self):
            return {"source": "egitim", "overall": 5}
    raw = DriverCursor()
    wrapped = database.PostgresCursorWrapper(raw)
    assert wrapped.execute("SELECT * FROM ernasa_candidate_experience WHERE source=?", ("egitim",)) is wrapped
    assert raw.query.endswith("source=%s")
    assert raw.params == ("egitim",)
    assert wrapped.fetchall()[0]["overall"] == 5
    assert wrapped.execute("SELECT 1").fetchone()["source"] == "egitim"
    assert wrapped.executemany("INSERT INTO survey VALUES(?)", [("egitim",)]) is wrapped


def test_candidate_summary_supports_postgres_none_returning_execute(monkeypatch):
    """CONTROL founder summary must work against a psycopg2-like cursor."""
    sample = {"source": "mulakat", "ease": 4, "clarity": 5, "interview": 3,
              "training": None, "overall": 5, "comment": "Test"}
    class DriverCursor:
        def execute(self, sql, params=None):
            assert "ernasa_candidate_experience" in sql
            return None
        def fetchall(self):
            return [sample]
        def close(self):
            pass
    class Connection:
        def __init__(self):
            self.raw = DriverCursor()
        def cursor(self):
            return database.PostgresCursorWrapper(self.raw)
        def close(self):
            pass
    monkeypatch.setattr(candidate_experience, "get_connection", Connection)
    report = candidate_experience.summary()
    assert report["mulakat"]["count"] == 1
    assert report["mulakat"]["ratings"]["overall"]["average"] == 5
    assert report["egitim"]["count"] == 0
    assert report["mulakat"]["comments"] == []


def test_feedback_form_requires_only_visible_questions_and_shows_confirmation():
    from pathlib import Path
    html = (Path(candidate_experience.__file__).parent / "candidate_experience.html").read_text(encoding="utf-8")
    assert "input.disabled=!applies;" in html
    assert "input.required=applies;" in html
    assert "name===\'interview\'&&source===\'mulakat\'" in html
    assert "name===\'training\'&&source===\'egitim\'" in html
    assert '</form><p id="status" role="status"' in html
    assert "Değerlendirmeniz alındı." in html
    assert "Değerlendirmeyi atladınız." in html


def test_demo_cleanup_refuses_to_delete_if_snapshot_changed(monkeypatch):
    import ik_demo_cleanup_20261120 as maintenance
    import psycopg2
    class DummyCursor:
        def __init__(self):
            self.rows = []
            self.executed = []
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query, args=None):
            self.executed.append(query)
            if 'FROM public.interview_links' in query:
                # A new 19th record must stop all writes.
                self.rows = [(str(i), 'Deneme', 'expired') for i in range(19)]
            else:
                self.rows = []
        def fetchall(self): return self.rows
    class DummyConnection:
        def __init__(self): self.c = DummyCursor()
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def cursor(self): return self.c
        def close(self): pass
    c=DummyConnection()
    monkeypatch.setattr(psycopg2, 'connect', lambda *a,**kw: c)
    with pytest.raises(RuntimeError, match='Beklenen 18 aday'):
        maintenance.cleanup('mock_maintenance_only')
    assert not any('DELETE FROM' in sql or 'CREATE TABLE' in sql
                   or 'INSERT INTO' in sql for sql in c.c.executed)


def test_demo_cleanup_is_idempotent_for_only_completed_merve(monkeypatch):
    import ik_demo_cleanup_20261120 as maintenance
    import psycopg2
    class DummyCursor:
        def __init__(self): self.executed=[]
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query, args=None): self.executed.append(query)
        def fetchall(self):
            return [('known-protected-token', maintenance.KEEP_NAME, 'completed')]
    class DummyConnection:
        def __init__(self): self.c=DummyCursor()
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def cursor(self): return self.c
        def close(self): pass
    c=DummyConnection()
    monkeypatch.setattr(psycopg2, 'connect', lambda *a,**kw: c)
    assert maintenance.cleanup('mock_maintenance_only') == {
        'already_clean': True, 'kept':1, 'removed':0
    }
    assert not any('DELETE FROM' in sql or 'CREATE TABLE' in sql
                   or 'INSERT INTO' in sql for sql in c.c.executed)


def test_ats_demo_cleanup_aborts_when_four_names_do_not_match(monkeypatch):
    import ats_demo_cleanup_20261120 as maintenance
    import psycopg2
    class DummyCursor:
        def __init__(self): self.sql=[]; self.rows=[]
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,sql,params=None):
            self.sql.append(sql)
            if 'count(*) FROM public.ats_candidates' in sql: self.rows=[(24,)]
            elif 'count(*) FROM public.ats_applications' in sql: self.rows=[(24,)]
            elif 'SELECT id,name FROM public.ats_candidates' in sql:
                self.rows=[('only-one','Deniz Arman')]
            else: self.rows=[]
        def fetchone(self): return self.rows[0]
        def fetchall(self): return self.rows
    class DummyConnection:
        def __init__(self): self.c=DummyCursor()
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def cursor(self): return self.c
        def close(self): pass
    conn=DummyConnection()
    monkeypatch.setattr(psycopg2,'connect',lambda *a,**kw: conn)
    with pytest.raises(RuntimeError, match='Dört aday'):
        maintenance.cleanup('synthetic-only')
    assert not any('INSERT INTO' in x or 'DELETE FROM' in x or 'CREATE TABLE' in x for x in conn.c.sql)


def test_ats_demo_cleanup_is_idempotent_after_four_removed(monkeypatch):
    import ats_demo_cleanup_20261120 as maintenance
    import psycopg2
    class DummyCursor:
        def __init__(self): self.sql=[];self.rows=[]
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,sql,params=None):
            self.sql.append(sql)
            if 'count(*) FROM public.ats_candidates' in sql: self.rows=[(20,)]
            elif 'count(*) FROM public.ats_applications' in sql: self.rows=[(20,)]
            else: self.rows=[]
        def fetchone(self): return self.rows[0]
        def fetchall(self): return self.rows
    class DummyConnection:
        def __init__(self):self.c=DummyCursor()
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def cursor(self):return self.c
        def close(self):pass
    conn=DummyConnection()
    monkeypatch.setattr(psycopg2,'connect',lambda *a,**kw:conn)
    assert maintenance.cleanup('synthetic-only') == {'already_clean':True,'removed':0,'remaining':20}
    assert not any('INSERT INTO' in x or 'DELETE FROM' in x or 'CREATE TABLE' in x for x in conn.c.sql)


def test_training_cleanup_aborts_if_invite_snapshot_changed(monkeypatch):
    import training_demo_cleanup_20261120 as maintenance
    import psycopg2
    class DummyCursor:
        def __init__(self):self.executed=[];self.rows=[]
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,sql,params=None):
            self.executed.append(sql)
            if 'JOIN public.ernasa_training_assignments a' in sql:
                self.rows=[('one', 'Merve Saye Aydoğdu Sipahi', 2, 'ernasa_v9'),
                           ('two', 'Erhan Gürpınar', 2, 'ernasa_v9')]
            else:self.rows=[]
        def fetchall(self):return self.rows
    class Conn:
        def __init__(self):self.c=DummyCursor()
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def cursor(self):return self.c
        def close(self):pass
    conn=Conn()
    monkeypatch.setattr(psycopg2,'connect',lambda *a,**kw:conn)
    with pytest.raises(RuntimeError,match='sekiz eğitim daveti'):
        maintenance.cleanup('synthetic-only')
    assert not any('DELETE FROM' in x or 'INSERT INTO' in x or 'CREATE TABLE' in x for x in conn.c.executed)


def test_training_cleanup_noops_if_only_merve_remains(monkeypatch):
    import training_demo_cleanup_20261120 as maintenance
    import psycopg2
    class DummyCursor:
        def __init__(self):self.executed=[];self.rows=[]
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,sql,params=None):
            self.executed.append(sql)
            if 'JOIN public.ernasa_training_assignments a' in sql:
                self.rows=[('protected',maintenance.KEEP_RECIPIENT,2,'ernasa_v9')]
            else:self.rows=[]
        def fetchall(self):return self.rows
    class Conn:
        def __init__(self):self.c=DummyCursor()
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def cursor(self):return self.c
        def close(self):pass
    conn=Conn()
    monkeypatch.setattr(psycopg2,'connect',lambda *a,**kw:conn)
    assert maintenance.cleanup('synthetic-only') == {
        'already_clean':True,'removed':0,'remaining':1
    }
    assert not any('DELETE FROM' in x or 'INSERT INTO' in x or 'CREATE TABLE' in x for x in conn.c.executed)
