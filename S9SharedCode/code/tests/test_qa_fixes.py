"""Regressions from the console QA pass: search relevance, vote clearing.

The Memory/Documents search endpoints used to return the vector index's
nearest neighbours for every query, so a nonsense query looked exactly like
a perfect match. The feedback endpoint rejected vote 0, so un-voting only
worked until the next reload.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_server  # noqa: E402
import memory  # noqa: E402
from agent_server import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


class _MemHit:
    def __init__(self, mid: str, descriptor: str, keywords: list[str] | None = None):
        self.id = mid
        self.kind = "fact"
        self.descriptor = descriptor
        self.keywords = keywords or []
        self.source = "qa"
        self.confidence = 1.0
        self.superseded_by = None
        self.drawer = "fact"


def test_search_word_filter_keeps_matching_and_drops_nonsense():
    assert agent_server._matches_search(
        ["Celonis process mining and execution management"], "celonis process")
    assert not agent_server._matches_search(
        ["Celonis process mining and execution management"],
        "zzzz-no-such-memory-qa")
    # A stopword-only query has nothing to match; it must not mean "all".
    assert not agent_server._matches_search(["anything at all"], "the")
    # Non-Latin text remains searchable.
    assert agent_server._matches_search(["京都の寺"], "京都")


def test_memory_search_returns_no_rows_for_an_unrelated_query(client, monkeypatch):
    hits = [
        _MemHit("mem:1", "Celonis process mining notes", ["celonis"]),
        _MemHit("mem:2", "Stretch reminder at noon", ["stretch"]),
    ]
    monkeypatch.setattr(memory, "read", lambda *a, **k: hits)

    matched = client.get("/api/memory", params={"q": "celonis"})
    assert matched.status_code == 200, matched.text
    assert [i["id"] for i in matched.json()["items"]] == ["mem:1"]

    none = client.get("/api/memory", params={"q": "zzzz-no-such-memory-qa"})
    assert none.status_code == 200, none.text
    assert none.json()["items"] == []


def test_document_search_filters_unrelated_chunks(client, monkeypatch):
    async def fake_gateway(path, *, method="GET", json=None, timeout=30.0):
        assert path == "/v1/documents/search"
        return {"hits": [
            {"chunk": "The secret word is ORANGE-LANTERN.",
             "filename": "qa.txt", "descriptor": "", "heading_path": []},
            {"chunk": "The secret word in this attachment is persimmon-42.",
             "filename": "other.txt", "descriptor": "", "heading_path": []},
        ]}

    monkeypatch.setattr(agent_server, "_gw_json", fake_gateway)
    r = client.post("/api/documents/search", json={"query": "ORANGE-LANTERN"})
    assert r.status_code == 200, r.text
    hits = r.json()["hits"]
    assert [h["filename"] for h in hits] == ["qa.txt"]

    noise = client.post("/api/documents/search", json={"query": "zzzz-no-such-doc"})
    assert noise.status_code == 200, noise.text
    assert noise.json()["hits"] == []


def test_feedback_vote_can_be_cleared(tmp_path, monkeypatch, client):
    monkeypatch.setattr(agent_server, "STATE_DIR", tmp_path)
    monkeypatch.setattr(agent_server, "_queue_feedback_learning", lambda: None)
    params = {"session_id": "sess-qa", "msg_id": "turn-qa"}

    up = client.post("/api/feedback", json={**params, "vote": 1})
    assert up.status_code == 200, up.text
    assert client.get("/api/feedback", params=params).json()["vote"] == 1

    clear = client.post("/api/feedback", json={**params, "vote": 0})
    assert clear.status_code == 200, clear.text
    assert clear.json()["vote"] == 0
    assert client.get("/api/feedback", params=params).json()["vote"] == 0
