"""Cancellable runs (console plan §4.7).

The console's Stop button used to abort only the CLIENT stream: the server
kept running the DAG (and the gateway kept billing). These tests pin the
real contract now that `POST /api/chat/cancel` exists:

  - Executor honours `should_cancel` at a node boundary: no further node is
    dispatched, pending nodes are persisted as `skipped`, completed work is
    kept, and the run returns without raising.
  - The cancel endpoint finds a registered run (by session or conversation
    id), flips its event, and reports found=false (not an error) for an id
    that isn't running.

Run:  pytest tests/test_cancel.py -q
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import flow
from persistence import SessionStore


# ── scripted LLM (same shape as test_orchestrator_integration) ───────────────

class _FakeLLM:
    def __init__(self, script: dict):
        self._script = script
        self.calls: list[str] = []

    def chat(self, prompt=None, *, messages=None, system=None, agent=None,
             session=None, **kw):
        skill = agent or "formatter"
        self.calls.append(skill)
        payload = self._script.get(skill, {"final_answer": f"answer from {skill}"})
        return {"text": json.dumps(payload), "provider": "fake",
                "input_tokens": 10, "output_tokens": 10}


def _execute(query: str, script: dict, *, session_id: str,
             should_cancel=None, fake: _FakeLLM | None = None
             ) -> tuple[str, _FakeLLM]:
    """Run the real Executor with a scripted LLM and an optional cancel
    predicate. Returns (answer, fake_llm) so tests can inspect call counts."""
    import mcp_runner
    fake = fake or _FakeLLM(script)

    async def _fake_run_with_tools(*, prompt=None, tools_payload=None,
                                   agent=None, session_id=None, **kw):
        reply = fake.chat(prompt=prompt, agent=agent, session=session_id)
        return {**reply, "tool_calls": []}

    with mock.patch.object(flow, "ensure_gateway", lambda: None), \
         mock.patch("skills.LLM", lambda: fake), \
         mock.patch.object(mcp_runner, "run_with_tools", _fake_run_with_tools):
        answer = asyncio.run(
            flow.Executor().run(query, session_id=session_id,
                                should_cancel=should_cancel)
        )
    return answer, fake


_MINIMAL = {
    "planner": {"nodes": [
        {"skill": "formatter", "inputs": ["USER_QUERY"], "metadata": {}}]},
    "formatter": {"final_answer": "done"},
}


# ── 1. cancel before the first dispatch ──────────────────────────────────────

def test_cancel_before_dispatch_runs_nothing(tmp_path):
    answer, fake = _execute("hi", _MINIMAL, session_id="t-cancel-1",
                            should_cancel=lambda: True)
    assert fake.calls == []          # nothing was ever dispatched
    assert answer.strip() == ""      # no formatter answer to report

    g = SessionStore("t-cancel-1", create=False).read_graph()
    assert g is not None
    statuses = {d["status"] for _, d in g.nodes(data=True)}
    assert statuses == {"skipped"}   # persisted, not left pending/running


# ── 2. cancel after the planner: partial work survives ───────────────────────

def test_cancel_mid_run_keeps_completed_nodes(tmp_path):
    fake = _FakeLLM(_MINIMAL)
    # Evaluated at the TOP of every dispatch batch: before the first batch the
    # planner hasn't run, so this returns False and the planner executes; the
    # next batch sees it and stops — leaving the formatter skipped.
    answer, fake = _execute("hi", _MINIMAL, session_id="t-cancel-2",
                            should_cancel=lambda: "planner" in fake.calls,
                            fake=fake)

    # Planner ran; formatter (next batch) was skipped.
    assert fake.calls == ["planner"]
    assert answer.strip() == ""

    g = SessionStore("t-cancel-2", create=False).read_graph()
    assert g is not None
    status = {n: g.nodes[n]["status"] for n in g.nodes}
    assert "complete" in status.values()       # finished work is preserved
    assert "skipped" in status.values()        # the rest is abandoned


# ── 3. the cancel endpoint ───────────────────────────────────────────────────

def test_cancel_endpoint_reports_and_fires():
    import agent_server as ag
    from fastapi.testclient import TestClient

    client = TestClient(ag.app)

    # Unknown id: 200 + found=false (stopping an idle run is not an error).
    r = client.post("/api/chat/cancel", json={"session_id": "s8-not-a-run"})
    assert r.status_code == 200
    assert r.json()["found"] is False

    # Register a live run (as _stream_run does) and cancel it by session id.
    ev = ag.threading.Event()
    ag._register_run(ev, "t-cancel-sid", "c-cancel-cid")
    try:
        r = client.post("/api/chat/cancel", json={"session_id": "t-cancel-sid"})
        assert r.status_code == 200
        assert r.json()["found"] is True
        assert ev.is_set()

        # …and by conversation id, with a fresh event.
        ev2 = ag.threading.Event()
        ag._register_run(ev2, "t-cancel-sid-2", "c-cancel-cid-2")
        r = client.post("/api/chat/cancel", json={"conversation_id": "c-cancel-cid-2"})
        assert r.json()["found"] is True
        assert ev2.is_set()
    finally:
        ag._unregister_run("t-cancel-sid", "c-cancel-cid",
                           "t-cancel-sid-2", "c-cancel-cid-2")

    # After unregistering, the run is no longer findable.
    r = client.post("/api/chat/cancel", json={"session_id": "t-cancel-sid"})
    assert r.json()["found"] is False


class TestCancelWiring:
    """The predicate handed to the orchestrator must be `Event.is_set`.

    `Event.set()` returns None, so `if should_cancel():` inside Executor.run
    is always falsy — the Stop button would report success while the DAG kept
    dispatching nodes. Caught by live E2E, not by the unit tests (which pass
    a correct predicate directly), so pin the source wiring here.
    """

    def test_stream_run_passes_is_set(self):
        src = (ROOT / "agent_server.py").read_text(encoding="utf-8")
        assert "cancel_ev.is_set" in src, (
            "_stream_run must pass cancel_ev.is_set (a bool-returning "
            "predicate), not cancel_ev.set (returns None => never cancels)"
        )
        # Every use of the event must read state, never mutate it: `.set()`
        # returns None (falsy) so the executor never cancels, the notify gate
        # always fires, and `done.cancelled` is always null.
        bare = src.replace("cancel_ev.is_set", "cancel_ev.__ok__")
        assert "cancel_ev.set" not in bare, (
            "cancel_ev.set returns None; replace it with cancel_ev.is_set"
        )
