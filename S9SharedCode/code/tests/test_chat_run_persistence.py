"""A DAG chat turn belongs to the server-owned worker, not the SSE viewer.

Closing the stream used to lose the answer: the worker kept running, but the
sidebar turn was written by the browser on `done`, so a refresh before that
frame left no row at all. The worker now settles its own pending row.
"""
from __future__ import annotations

import shutil
import time
import uuid

from fastapi.testclient import TestClient

import agent_server
import gateway
from persistence import SESSIONS_ROOT


def test_dag_chat_answer_survives_a_closed_stream(monkeypatch):
    monkeypatch.setattr(agent_server, "_GATEWAY_WARMUP_THREAD", None)
    monkeypatch.setattr(agent_server, "_EMBEDDER_WARMUP_THREAD", None)
    monkeypatch.setattr(gateway, "ensure_gateway", lambda *a, **k: True)
    monkeypatch.setattr(agent_server, "_capture_observation_bg",
                        lambda *a, **k: None)
    monkeypatch.setattr(agent_server, "_session_cost_breakdown",
                        lambda *a, **k: None)
    monkeypatch.setattr(agent_server, "_session_cost_delta",
                        lambda *a, **k: None)
    monkeypatch.setattr(agent_server, "_record_turn_cost",
                        lambda *a, **k: None)
    monkeypatch.setattr(agent_server, "notify_task_done",
                        lambda *a, **k: None)

    def _slow_orchestrator(*a, **k):
        time.sleep(0.35)
        return "the DAG answer survived the refresh"

    monkeypatch.setattr(agent_server, "_run_orchestrator", _slow_orchestrator)

    cid = f"c-dagpersist-{uuid.uuid4().hex[:8]}"
    sid = agent_server.resolve_session(cid)
    try:
        with TestClient(agent_server.app) as client:
            with client.stream("POST", "/api/chat", json={
                    "query": "keep working after I leave",
                    "conversation_id": cid, "run_kind": "chat"}) as r:
                assert r.status_code == 200
                for line in r.iter_lines():
                    if line:
                        break
            # The viewer is gone. The worker still has to settle the row.
            deadline = time.time() + 4
            messages = []
            while time.time() < deadline:
                d = client.get(f"/api/chat/threads/{cid}").json()
                messages = d.get("messages") or []
                if any(m.get("role") == "assistant"
                       and not m.get("pending") for m in messages):
                    break
                time.sleep(0.1)
        answer = [m for m in messages
                  if m.get("role") == "assistant" and not m.get("pending")]
        assert answer, f"the DAG turn was never settled; stored={messages!r}"
        assert answer[-1].get("content") == "the DAG answer survived the refresh"
    finally:
        shutil.rmtree(SESSIONS_ROOT / sid, ignore_errors=True)
