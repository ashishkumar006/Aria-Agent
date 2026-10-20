"""The research run contract: the TOPIC is the query, depth is planner-only.

Regression cover for the live DAG failure. The Research page used to post a
multi-role brief ("You are a research agent. Research this topic and deliver a
final report as Markdown...") as the run's USER_QUERY. Every node reads that
query, so each researcher answered by writing the final report instead of the
JSON the pipeline requires, failed with "returned no usable JSON" after up to
300s, and degraded the run. The topic now goes in verbatim and the operator's
depth is a note for the planner alone.

Also covers `run_kind`: the kind is declared by the caller and persisted at
run start, so a just-started topic appears in the sidebar immediately (and a
DAG-mode chat does not appear there at all).
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_server
from agent_server import _research_depth_note, app
from persistence import SESSIONS_ROOT, SessionStore


def test_depth_note_is_planner_only_and_never_impersonates_a_researcher():
    note = _research_depth_note("deep")
    assert "DEEP" in note.upper()
    # The old brief's exact sentences are what made a researcher write a report.
    assert "you are a research agent" not in note.lower()
    assert "deliver a final report as markdown" not in note.lower()
    assert "RUN PARAMETERS" in note
    # Quick/standard are budgets, not adjectives.
    assert "EXACTLY ONE researcher" in _research_depth_note("quick")
    assert "AT MOST 3" in _research_depth_note("standard")


def test_unknown_depth_falls_back_to_standard():
    assert "AT MOST 3" in _research_depth_note("banana")
    assert "AT MOST 3" in _research_depth_note("")


def _post_chat(client: TestClient, body: dict) -> None:
    """POST /api/chat and drain the (fake) stream."""
    with client.stream("POST", "/api/chat", json=body) as r:
        assert r.status_code == 200
        for _ in r.iter_lines():
            pass


def test_research_chat_sends_the_topic_and_a_planner_note(monkeypatch):
    seen: dict = {}

    def fake_stream(query, conversation_id, request=None, idem_key="",
                    doc_setup=None, run_note="", run_kind=""):
        seen.update(query=query, run_note=run_note, run_kind=run_kind)
        return StreamingResponse(iter([b"data: {\"type\": \"done\"}\n\n"]),
                                 media_type="text/event-stream")

    monkeypatch.setattr(agent_server, "_stream_run", fake_stream)
    monkeypatch.setattr(agent_server, "_capture_observation_bg",
                        lambda *a, **k: None)
    monkeypatch.setattr(agent_server, "_inflight_run_lookup",
                        lambda *a, **k: None)
    monkeypatch.setattr(agent_server, "_inflight_run_claim",
                        lambda *a, **k: None)
    monkeypatch.setattr(agent_server, "_inflight_run_release",
                        lambda *a, **k: None)

    topic = "solid-state battery supply chain: who leads"
    with TestClient(app) as client:
        _post_chat(client, {"query": topic, "research": True, "depth": "deep",
                            "run_kind": "research"})

    # The topic, untouched - not wrapped in a brief.
    assert seen["query"] == topic
    assert seen["run_kind"] == "research"
    assert "DEEP" in seen["run_note"].upper()
    assert "you are a research agent" not in seen["run_note"].lower()


def test_research_true_without_run_kind_still_declares_research(monkeypatch):
    """Back-compat: the console may be a build behind the server."""
    seen: dict = {}

    def fake_stream(query, conversation_id, request=None, idem_key="",
                    doc_setup=None, run_note="", run_kind=""):
        seen.update(run_note=run_note, run_kind=run_kind)
        return StreamingResponse(iter([b"data: {\"type\": \"done\"}\n\n"]),
                                 media_type="text/event-stream")

    monkeypatch.setattr(agent_server, "_stream_run", fake_stream)
    monkeypatch.setattr(agent_server, "_capture_observation_bg",
                        lambda *a, **k: None)
    monkeypatch.setattr(agent_server, "_inflight_run_lookup",
                        lambda *a, **k: None)
    monkeypatch.setattr(agent_server, "_inflight_run_claim",
                        lambda *a, **k: None)
    monkeypatch.setattr(agent_server, "_inflight_run_release",
                        lambda *a, **k: None)

    with TestClient(app) as client:
        _post_chat(client, {"query": "a topic", "research": True})
    assert seen["run_kind"] == "research"
    assert "AT MOST 3" in seen["run_note"]  # standard is the default


def test_chat_dag_run_declares_chat_not_research(monkeypatch):
    seen: dict = {}

    def fake_stream(query, conversation_id, request=None, idem_key="",
                    doc_setup=None, run_note="", run_kind=""):
        seen.update(run_note=run_note, run_kind=run_kind)
        return StreamingResponse(iter([b"data: {\"type\": \"done\"}\n\n"]),
                                 media_type="text/event-stream")

    monkeypatch.setattr(agent_server, "_stream_run", fake_stream)
    monkeypatch.setattr(agent_server, "_capture_observation_bg",
                        lambda *a, **k: None)
    monkeypatch.setattr(agent_server, "_inflight_run_lookup",
                        lambda *a, **k: None)
    monkeypatch.setattr(agent_server, "_inflight_run_claim",
                        lambda *a, **k: None)
    monkeypatch.setattr(agent_server, "_inflight_run_release",
                        lambda *a, **k: None)

    with TestClient(app) as client:
        _post_chat(client, {"query": "hello there", "run_kind": "chat"})
    assert seen["run_kind"] == "chat"
    assert seen["run_note"] == ""


def test_run_meta_roundtrip():
    sid = f"s8-testmeta-{uuid.uuid4().hex[:6]}"
    try:
        store = SessionStore(sid)
        assert store.read_run_meta() == {}
        store.write_run_meta({"kind": "research"})
        assert store.read_run_meta() == {"kind": "research"}
    finally:
        import shutil
        shutil.rmtree(SESSIONS_ROOT / sid, ignore_errors=True)


def test_sessions_list_prefers_the_declared_kind():
    """A chat run's graph looks exactly like a research run's (planner +
    researcher). Only the persisted declaration can keep it out of Research."""
    sid = f"s8-testmeta-{uuid.uuid4().hex[:6]}"
    try:
        store = SessionStore(sid)
        (SESSIONS_ROOT / sid / "query.txt").write_text("hello there",
                                                       encoding="utf-8")
        (SESSIONS_ROOT / sid / "graph.json").write_text(json.dumps({
            "directed": True, "multigraph": False,
            "nodes": [{"id": "n_001", "skill": "planner", "status": "completed"},
                      {"id": "n_002", "skill": "researcher", "status": "completed"}],
            "links": [],
        }), encoding="utf-8")
        store.write_run_meta({"kind": "chat"})
        with TestClient(app) as client:
            rows = client.get("/api/sessions").json()["sessions"]
        row = next(r for r in rows if r["session_id"] == sid)
        assert row["run_kind"] == "chat"
    finally:
        import shutil
        shutil.rmtree(SESSIONS_ROOT / sid, ignore_errors=True)
