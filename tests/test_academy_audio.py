"""Training voice-upload regression tests (synthetic data only)."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import database
import academy_audio
import ernasa_training


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "ernasa_training_test.db")
    monkeypatch.setattr(database, "DATABASE_URL", None)
    monkeypatch.setattr(academy_audio, "DATABASE_URL", None)
    ernasa_training.initialize_schema()
    academy_audio.initialize_schema()
    app = FastAPI()
    app.include_router(academy_audio.router)
    return TestClient(app)


def invite():
    return ernasa_training.create_invite(
        ernasa_training.InviteForm(recipient="Test Katılımcısı", days=14),
        "https://ernasa.com"
    )["link"].split("/")[-1]


def upload(client, token, consent="yes", module_id="demo", content=None):
    sample = content or (b"\x1a\x45\xdf\xa3" + b"0" * 256)
    return client.post(
        f"/api/egitim/{token}/ses",
        headers={"Origin": "https://ernasa.com"},
        data={"module_id":module_id, "consent":consent},
        files={"audio":("test.webm",sample,"audio/webm")}
    )


def test_voice_upload_needs_valid_token_and_consent(client):
    token = invite()
    response = upload(client, token, consent="no")
    assert response.status_code == 422
    assert upload(client, "bad-token").status_code == 404
    response = upload(client, token)
    assert response.status_code == 201
    assert response.json()["received"] is True
    result = client.get(f"/api/egitim/{token}/sesler").json()
    assert [x["module_id"] for x in result["recordings"]] == ["demo"]


def test_voice_upload_rejects_cross_origin_and_unexpected_media(client):
    token = invite()
    r = client.post(
        f"/api/egitim/{token}/ses",
        headers={"Origin":"https://attacker.example"},
        data={"module_id":"demo","consent":"yes"},
        files={"audio":("test.webm",b"\x1a\x45\xdf\xa3"+b"x"*256,"audio/webm")}
    )
    assert r.status_code == 403
    assert upload(client, token, content=b"x"*300).status_code == 415
    assert upload(client, token, module_id="invalid").status_code == 422


def test_voice_recording_is_private_to_owner(client, monkeypatch):
    token = invite()
    assert upload(client, token).status_code == 201
    assert client.get("/api/egitim/admin/kayitlar").status_code == 401
    monkeypatch.setattr(academy_audio, "verify_ik_session", lambda token: token=="owner-session")
    import control_integration
    monkeypatch.setattr(control_integration, "portal_for", lambda req: None)
    owner = {"ernasa_ik_session":"owner-session"}
    response = client.get("/api/egitim/admin/kayitlar", cookies=owner)
    assert response.status_code == 200
    assert len(response.json()["recordings"]) == 1
    rid = response.json()["recordings"][0]["id"]
    assert client.get(f"/api/egitim/admin/kayitlar/{rid}/dinle").status_code == 401
    audio = client.get(f"/api/egitim/admin/kayitlar/{rid}/dinle", cookies=owner)
    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/webm")
    assert audio.content.startswith(b"\x1a\x45\xdf\xa3")


def test_invite_not_dispatched_by_creation(client, monkeypatch):
    monkeypatch.setattr(academy_audio,"verify_ik_session",lambda token: token=="owner-session")
    import control_integration
    monkeypatch.setattr(control_integration, "portal_for", lambda req: None)
    r = client.post("/api/egitim/admin/davet",
        headers={"Origin":"https://ernasa.com"},
        cookies={"ernasa_ik_session":"owner-session"},
        json={"recipient":"Test Katılımcısı","days":14})
    assert r.status_code == 201
    assert r.json()["sent"] is False
    assert r.json()["link"].startswith("https://ernasa.com/egitim/")
