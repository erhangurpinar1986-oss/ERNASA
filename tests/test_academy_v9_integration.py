"""Regression checks for the additive ERNASA Academy V9 integration."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import database
import ernasa_training
import academy_assignments
import academy_audio


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_URL", None)
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "academy_v9.db")
    ernasa_training.initialize_schema()
    academy_assignments.initialize_schema()
    app = FastAPI()
    app.include_router(academy_assignments.router)
    app.include_router(academy_audio.router)

    def actor(request, write=False):
        if write and request.headers.get("Origin") != "https://ernasa.com":
            from fastapi import HTTPException
            raise HTTPException(403, "Geçersiz kaynak")
        return 1001, 24

    monkeypatch.setattr(academy_assignments, "_actor", actor)
    return TestClient(app)


def test_v9_catalog_and_curriculum_count(client):
    assert len(academy_assignments.PROGRAMS["ernasa_v9"]["modules"]) == 53
    catalog = client.get("/api/egitim/ik/katalog").json()
    assert any(p["id"] == "ernasa_v9" for p in catalog["programs"])
    assert any(p["id"] == "b2b_satis" for p in catalog["programs"])


def test_personal_link_opens_v9_and_keeps_legacy(client):
    common = {"recipient": "Sentetik Katılımcı", "email": "",
              "days": 14, "video_allowed": True}
    headers = {"Origin": "https://ernasa.com"}
    modern = client.post("/api/egitim/ik/atama", json={
        **common, "program_id": "ernasa_v9",
        "module_ids": list(academy_assignments.V9_MODULE_TITLES),
    }, headers=headers)
    assert modern.status_code == 201, modern.text
    token = modern.json()["link"].split("/")[-1]
    opened = client.get("/egitim/" + token)
    assert opened.status_code == 200, opened.text[:200]
    assert "ERNASA_V9_CONTEXT" in opened.text
    assert "v9SendAudio" in opened.text
    assert "Sentetik Katılımcı" in opened.text
    legacy = client.post("/api/egitim/ik/atama", json={
        **common, "program_id": "b2b_satis", "module_ids": ["vizyon", "demo"]
    }, headers=headers)
    assert legacy.status_code == 201, legacy.text
    old_token = legacy.json()["link"].split("/")[-1]
    old = client.get("/egitim/" + old_token)
    assert old.status_code == 200
    assert "const ACADEMY_DATA=" in old.text
    assert "ERNASA_V9_CONTEXT" not in old.text


def test_v9_progress_saved_and_shown_in_hr(client):
    headers={"Origin":"https://ernasa.com"}
    created=client.post("/api/egitim/ik/atama",json={
        "recipient":"Sentetik Eğitim Kullanıcısı","program_id":"ernasa_v9",
        "module_ids":["ernasa","demo"],"days":7
    },headers=headers)
    assert created.status_code == 201,created.text
    token=created.json()["link"].split("/")[-1]
    url="/api/egitim/"+token+"/v9/ilerleme"
    saved=client.post(url,json={"done":{"ernasa":True},"answers":{
        "ernasa":{"0":1,"1":1}}})
    assert saved.status_code == 200,saved.text
    read=client.get(url)
    assert read.json()["done"]=={"ernasa":True}
    assignments=client.get("/api/egitim/ik/atamalar").json()["assignments"]
    assert assignments[0]["completed_lessons"]==1
    assert assignments[0]["total_lessons"]==2
    invalid=client.post(url,json={"done":{"not-a-course":True},"answers":{}})
    assert invalid.status_code == 422


def test_v9_hr_tracking_open_start_and_tenant_isolation(client, monkeypatch):
    headers = {"Origin": "https://ernasa.com"}
    created = client.post("/api/egitim/ik/atama", json={
        "recipient": "Sentetik Takip Katılımcısı", "email": "test@example.invalid",
        "program_id": "ernasa_v9", "module_ids": ["ernasa", "demo"], "days": 7,
    }, headers=headers)
    assert created.status_code == 201, created.text
    token = created.json()["link"].split("/")[-1]
    tracking_url = "/api/egitim/ik/takip"

    initial = client.get(tracking_url)
    assert initial.status_code == 200
    row = initial.json()["participants"][0]
    assert row["status"] == "not_started"
    assert row["opened_at"] is None
    assert row["started_at"] is None
    assert row["percent"] == 0
    assert row["total_lessons"] == 2
    assert "token" not in row and "link" not in row

    event_url = "/api/egitim/" + token + "/v9/etkinlik"
    denied = client.post(event_url, json={"event":"opened"}, headers={"Origin":"https://evil.invalid"})
    assert denied.status_code == 403
    opened = client.post(event_url, json={"event":"opened"}, headers=headers)
    assert opened.status_code == 200, opened.text
    row = client.get(tracking_url).json()["participants"][0]
    assert row["status"] == "opened"
    assert row["opened_at"] and not row["started_at"]

    started = client.post(event_url, json={"event":"started"}, headers=headers)
    assert started.status_code == 200, started.text
    row = client.get(tracking_url).json()["participants"][0]
    assert row["status"] == "in_progress"
    assert row["started_at"] and row["last_activity_at"]

    progress = client.post("/api/egitim/"+token+"/v9/ilerleme", json={
        "done":{"ernasa":True, "demo":True},
        "answers":{"ernasa":{"0":1}},
    })
    assert progress.status_code == 200, progress.text
    row = client.get(tracking_url).json()["participants"][0]
    assert row["status"] == "completed"
    assert row["percent"] == 100
    assert all(x["completed"] for x in row["lessons"])

    monkeypatch.setattr(academy_assignments, "_actor", lambda request, write=False: (2002, 31))
    assert client.get(tracking_url).json()["participants"] == []
