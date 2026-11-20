"""Offline synthetic tests for agent approval workflow; no live mail or AI."""
import sqlite3

import pytest

from control.agent_core import AGENTS, AgentError, AgentStore
from control.agent_runner import GENERAL_RULES, prepare_with


@pytest.fixture
def store(tmp_path):
    store = AgentStore(tmp_path / "agents.sqlite3")
    store.initialize()
    return store


def test_agents_and_durable_queue(store):
    assert {"LARA", "MERT", "SELIN"} <= set(AGENTS)
    identifier = store.create("LARA", "analysis", "Satış ekibini koordine et")
    assert store.get(identifier)["status"] == "queued"
    assert store.queue("queued")[0]["id"] == identifier
    # Opening a new store object must preserve the queue.
    second = AgentStore(store.path)
    assert second.get(identifier)["request"] == "Satış ekibini koordine et"
    assert [entry["event"] for entry in store.history(identifier)] == ["created"]


def test_draft_requires_matching_agent_and_human_approval(store):
    identifier = store.create("MERT", "development", "İK paneli için taslak hazırla")
    with pytest.raises(AgentError):
        store.prepare(identifier, "LARA", "Yanlış ajan")
    store.prepare(identifier, "MERT", "Kod değişikliği önerisi")
    assert store.get(identifier)["status"] == "draft"
    with pytest.raises(AgentError):
        store.decide(identifier, is_founder=True, approved=True, reason="Erken karar")
    store.submit(identifier, "MERT")
    with pytest.raises(AgentError):
        store.decide(identifier, is_founder=False, approved=True, reason="Yetkisiz")
    with pytest.raises(AgentError):
        store.decide(identifier, is_founder=True, approved="yes", reason="Hatalı tür")
    result = store.decide(identifier, is_founder=True, approved=True, reason="Kurucu inceleyip uygun buldu")
    assert result["status"] == "approved"
    assert result["founder_reason"] == "Kurucu inceleyip uygun buldu"
    with pytest.raises(AgentError):
        store.decide(identifier, is_founder=True, approved=False, reason="Yinelenen karar")
    assert [e["event"] for e in store.history(identifier)] == [
        "created", "draft_prepared", "submitted", "approved"
    ]


def test_rejection_is_terminal_and_does_not_execute(store):
    identifier = store.create("SELIN", "communication", "Adaya dönüş taslağı")
    store.prepare(identifier, "SELIN", "Merhaba, katılım durumunuzu öğrenebilir miyiz?")
    store.submit(identifier, "SELIN")
    store.decide(identifier, is_founder=True, approved=False, reason="Göndermeyeceğiz")
    assert store.get(identifier)["status"] == "rejected"
    with pytest.raises(AgentError):
        store.prepare(identifier, "SELIN", "Bir başka taslak")
    assert not hasattr(store, "send_email")
    assert not hasattr(store, "deploy")
    assert not hasattr(store, "pay")


def test_generator_can_only_prepare_text(store):
    identifier = store.create("LARA", "analysis", "Satış planını çıkar")
    seen = {}

    def generator(instructions, request):
        seen["instructions"] = instructions
        seen["request"] = request
        return "Öneri: 1) Öncelikleri belirle. 2) Kurucuya onaya sun."

    result = prepare_with(store, identifier, generator)
    assert result["status"] == "awaiting_approval"
    assert "Harici sistem işlemi yapamazsın" in seen["instructions"]
    assert GENERAL_RULES in seen["instructions"]
    assert seen["request"] == "Satış planını çıkar"
    assert result["draft"].startswith("Öneri:")
    with pytest.raises(AgentError):
        prepare_with(store, identifier, generator)
    assert store.queue("awaiting_approval")[0]["id"] == identifier


def test_invalid_requests_and_event_integrity(store):
    with pytest.raises(AgentError):
        store.create("UNKNOWN", "analysis", "Görev")
    with pytest.raises(AgentError):
        store.create("MERT", "shell", "Komut çalıştır")
    with pytest.raises(AgentError):
        store.create("MERT", "development", " ")
    identifier = store.create("MERT", "development", "Yazılım denetimi")
    with store.connect() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM agent_events WHERE task_id=?", (identifier,))
    assert len(store.history(identifier)) == 1


def test_approval_must_have_reason(store):
    identifier = store.create("SELIN", "interview", "Mülakat soruları")
    store.prepare(identifier, "SELIN", "Bir soru taslağı")
    store.submit(identifier, "SELIN")
    with pytest.raises(AgentError):
        store.decide(identifier, is_founder=True, approved=True, reason=" ")
    assert store.get(identifier)["status"] == "awaiting_approval"
