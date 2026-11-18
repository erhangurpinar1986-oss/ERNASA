"""Synthetic tests for ERNASA Academy camera permissions and HR media isolation."""
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import database
import ernasa_training
import academy_assignments
import academy_audio
import academy_video


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(database,"DATABASE_URL",None)
    monkeypatch.setattr(database,"DATABASE_PATH",tmp_path/"academy-video.db")
    monkeypatch.setattr(academy_audio,"DATABASE_URL",None)
    monkeypatch.setattr(academy_video,"DATABASE_URL",None)
    ernasa_training.initialize_schema()
    academy_assignments.initialize_schema()
    academy_audio.initialize_schema()
    academy_video.initialize_schema()
    def actor(req,write=False):
        company=int(req.headers.get("X-Test-Company","1001"))
        if write and req.headers.get("Origin")!="https://ernasa.com":
            raise HTTPException(403,"Origin")
        return company,99
    monkeypatch.setattr(academy_assignments,"_actor",actor)
    app=FastAPI()
    app.include_router(academy_assignments.router)
    app.include_router(academy_audio.router)
    app.include_router(academy_video.router)
    return TestClient(app)


def assign(client,video_allowed,company="1001"):
    response=client.post("/api/egitim/ik/atama",headers={
        "Origin":"https://ernasa.com","X-Test-Company":company},
        json={"recipient":"Sentetik Katılımcı","program_id":"b2b_satis",
              "module_ids":["vizyon","demo"],"video_allowed":video_allowed,"days":2})
    assert response.status_code==201,response.text
    return response.json()["link"].rsplit("/",1)[-1]


def video_upload(client,token,company=None,consent="yes",scenario="itiraz"):
    return client.post("/api/egitim/"+token+"/video",headers={"Origin":"https://ernasa.com"},
        data={"module_id":"demo","consent":consent,"scenario_id":scenario},
        files={"video":("test.webm",b"\x1a\x45\xdf\xa3"+b"0"*800,"video/webm")})


def test_video_disabled_unless_explicitly_assigned(client):
    disabled=assign(client,False)
    assert video_upload(client,disabled).status_code==403
    enabled=assign(client,True)
    assert video_upload(client,enabled,consent="no").status_code==422
    valid=video_upload(client,enabled)
    assert valid.status_code==201,valid.text
    assert valid.json()["scenario_id"]=="itiraz"


def test_video_requires_valid_origin_media_and_token(client):
    token=assign(client,True)
    cross=client.post("/api/egitim/"+token+"/video",
        headers={"Origin":"https://attacker.invalid"},
        data={"module_id":"demo","consent":"yes"},
        files={"video":("test.webm",b"\x1a\x45\xdf\xa3"+b"0"*800,"video/webm")})
    assert cross.status_code==403
    invalid=client.post("/api/egitim/"+token+"/video",
        headers={"Origin":"https://ernasa.com"},
        data={"module_id":"demo","consent":"yes"},
        files={"video":("test.webm",b"no camera data"*100,"video/webm")})
    assert invalid.status_code==415
    assert video_upload(client,"no-token").status_code==404


def test_tenant_can_only_review_its_own_voice_and_video(client):
    first=assign(client,True,company="1001")
    second=assign(client,True,company="2002")
    assert video_upload(client,first).status_code==201
    assert video_upload(client,second).status_code==201
    audio=client.post("/api/egitim/"+first+"/ses",
        headers={"Origin":"https://ernasa.com"},
        data={"module_id":"demo","consent":"yes","scenario_id":"tanitim"},
        files={"audio":("sample.webm",b"\x1a\x45\xdf\xa3"+b"0"*500,"audio/webm")})
    assert audio.status_code==201,audio.text
    result=client.get("/api/egitim/ik/kayitlar",headers={"X-Test-Company":"1001"})
    assert result.status_code==200,result.text
    values=result.json()["recordings"]
    assert len(values)==2
    assert {x["kind"] for x in values}=={"ses","video"}
    assert {x["scenario_id"] for x in values}=={"tanitim","itiraz"}
    other=client.get("/api/egitim/ik/kayitlar",headers={"X-Test-Company":"2002"}).json()
    assert len(other["recordings"])==1
    own_video=next(x for x in values if x["kind"]=="video")
    response=client.get(f'/api/egitim/ik/kayitlar/video/{own_video["id"]}',headers={"X-Test-Company":"1001"})
    assert response.status_code==200
    assert response.headers["content-type"].startswith("video/webm")
    assert client.get(f'/api/egitim/ik/kayitlar/video/{own_video["id"]}',headers={"X-Test-Company":"2002"}).status_code==404
    assert client.delete(f'/api/egitim/ik/kayitlar/video/{own_video["id"]}',headers={"X-Test-Company":"2002","Origin":"https://ernasa.com"}).status_code==204
    assert client.get(f'/api/egitim/ik/kayitlar/video/{own_video["id"]}',headers={"X-Test-Company":"1001"}).status_code==200
    assert client.delete(f'/api/egitim/ik/kayitlar/video/{own_video["id"]}',headers={"X-Test-Company":"1001","Origin":"https://ernasa.com"}).status_code==204
    assert client.get(f'/api/egitim/ik/kayitlar/video/{own_video["id"]}',headers={"X-Test-Company":"1001"}).status_code==404
