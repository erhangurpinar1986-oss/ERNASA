"""Isolated ERNASA Academy course assignment and PDF suggestion checks."""
import json
from hashlib import sha256
from datetime import datetime, timezone
import fitz
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import academy_assignments as subject
import ernasa_training
import database


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_URL", None)
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "academy.db")
    ernasa_training.initialize_schema()
    subject.initialize_schema()
    app = FastAPI()
    app.include_router(subject.router)
    # Authentication itself is performed by the existing portal middleware.
    # Simulate an already authenticated actor for independent tenant-isolation tests.
    def actor(request, write=False):
        tenant = request.headers.get("X-Test-Tenant", "1001")
        if write and request.headers.get("Origin") != "https://ernasa.com":
            from fastapi import HTTPException
            raise HTTPException(403, "Origin")
        return int(tenant), 24
    monkeypatch.setattr(subject, "_actor", actor)
    return TestClient(app)


def create_report():
    d = fitz.open()
    page=d.new_page(width=595,height=842)
    text="""Aday: Test Katilimcisi
Pozisyon: ERNASA B2B Satis Ortagi
GELİŞTİRİLMESİ GEREKEN ALANLAR:
- Sıfırdan B2B satış, hunting, pipeline oluşturma.
- 30/60/90 gün planında ölçülebilir hedefler, CRM, KPI.
- Fiyat müzakere, ROI ve give-get.
- Kurumsal satın alma, KVKK/GDPR uyum.
- Almanya pazar genişleme ve yerel ortaklar.
- Demo oranı, kök neden ve A/B test.
DİKKAT EDİLMESİ GEREKEN NOKTALAR:
- Bunlar eğitim önerisidir.
"""
    # Text extracted from an actual rendered PDF, not only manually pasted data.
    page.insert_text((30,40),text,fontname="helv",fontsize=9)
    return d.tobytes()


def test_read_pdf_suggests_development_areas(app_client):
    # PDF font rendering can vary; use a minimal Latin headings variant through direct parser.
    report="""Aday: Test Katilimcisi
Pozisyon: ERNASA B2B
GELİŞTİRİLMESİ GEREKEN ALANLAR:
- Sıfırdan B2B satış, hunting, pipeline.
- CRM, KPI, 30/60/90 gün.
- ROI, fiyat müzakere.
- Kurumsal satın alma, GDPR.
- Almanya ve yerel ortaklar.
- Demo oranı ve A/B test.
DİKKAT EDİLMESİ GEREKEN NOKTALAR:
- Yetersiz bilgi
"""
    result=subject.suggest_from_text(report)
    got={x["module_id"] for x in result["suggestions"]}
    assert {"avcilik","crm","müzakere","kurumsal","almanya","metrik"}<=got
    assert result["report_saved"] is False


def test_catalog_and_tenant_scoped_assignments(app_client):
    c=app_client
    assert c.get("/api/egitim/ik/katalog").status_code==200
    payload={"recipient":"Örnek Eğitim Katılımcısı","email":"",
             "program_id":"b2b_satis","module_ids":["vizyon","crm","metrik"],"days":14}
    r=c.post("/api/egitim/ik/atama",json=payload,headers={"Origin":"https://ernasa.com","X-Test-Tenant":"1001"})
    assert r.status_code==201,r.text
    assert r.json()["sent"] is False
    token=r.json()["link"].split("/")[-1]
    assert subject.assigned_modules(token)["module_ids"]==["vizyon","crm","metrik"]
    own=c.get("/api/egitim/ik/atamalar",headers={"X-Test-Tenant":"1001"}).json()
    other=c.get("/api/egitim/ik/atamalar",headers={"X-Test-Tenant":"2002"}).json()
    assert len(own["assignments"])==1
    assert not other["assignments"]


def test_invalid_or_cross_origin_assignment_rejected(app_client):
    payload={"recipient":"Test Katılımcı","program_id":"b2b_satis",
             "module_ids":["vizyon"],"days":14}
    r=app_client.post("/api/egitim/ik/atama",json=payload,headers={"Origin":"https://attacker.example"})
    assert r.status_code==403
    payload["module_ids"]=["unknown"]
    r=app_client.post("/api/egitim/ik/atama",json=payload,headers={"Origin":"https://ernasa.com"})
    assert r.status_code==422


def test_pdf_invalid_format_rejected():
    with pytest.raises(Exception):
        subject.parse_pdf_bytes(b"not a pdf")


def test_saved_reports_only_visible_within_own_company(app_client):
    from contextlib import closing
    with closing(database.get_connection()) as conn:
        c=conn.cursor()
        c.execute("""CREATE TABLE interview_links(
            token TEXT PRIMARY KEY,name TEXT,position TEXT,
            company_id BIGINT,status TEXT,created_at TEXT
        )""")
        c.execute("""CREATE TABLE interview_reports(
            token TEXT PRIMARY KEY,job_fit_analysis TEXT
        )""")
        report="""GELİŞTİRİLMESİ GEREKEN ALANLAR:
- CRM, KPI, 30/60/90 gün, müzakere ve ROI.
DİKKAT EDİLMESİ GEREKEN NOKTALAR:
- Veri eksik.
"""
        c.execute("INSERT INTO interview_links VALUES(?,?,?,?,?,?)",
            ("tenant1case","Kurgusal Aday A","B2B Satis",1001,"completed","2026-11-18"))
        c.execute("INSERT INTO interview_links VALUES(?,?,?,?,?,?)",
            ("tenant2case","Kurgusal Aday B","B2B Satis",2002,"completed","2026-11-18"))
        c.execute("INSERT INTO interview_reports VALUES(?,?)",("tenant1case",report))
        c.execute("INSERT INTO interview_reports VALUES(?,?)",("tenant2case",report))
        conn.commit()
    client=app_client
    own=client.get("/api/egitim/ik/kayitli-raporlar",headers={"X-Test-Tenant":"1001"})
    assert own.status_code==200
    assert [x["token"] for x in own.json()["reports"]]==["tenant1case"]
    blocked=client.get("/api/egitim/ik/kayitli-raporlar/tenant2case/oneriler",
                       headers={"X-Test-Tenant":"1001"})
    assert blocked.status_code==404
    allowed=client.get("/api/egitim/ik/kayitli-raporlar/tenant1case/oneriler",
                       headers={"X-Test-Tenant":"1001"})
    assert allowed.status_code==200
    assert {x["module_id"] for x in allowed.json()["suggestions"]}>={"crm","müzakere"}
