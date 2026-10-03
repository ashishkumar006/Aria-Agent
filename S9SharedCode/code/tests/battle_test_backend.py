"""Backend battle-test: drives the REAL agent_server HTTP surface.

Exercises /api/chat SSE streaming through the real orchestrator (skills
faked, gateway fully bypassed), plus schedule / template / approval /
cost / memory / audit endpoints. No network, no LLM, no Telegram.
"""
from __future__ import annotations

import json
import sys
import time as _time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

# ── neutralise the gateway BEFORE anything imports it ───────────────────────
import gateway
gateway.ensure_gateway = lambda *a, **k: None

import e2e_harness as h
import skills, flow, memory as _mem, persistence as _persist

_ORIG_ATOMIC = _persist._atomic_write


def _atomic_retry(path, data):
    for _attempt in range(8):
        try:
            return _ORIG_ATOMIC(path, data)
        except PermissionError:
            if _attempt == 7:
                raise
            _time.sleep(0.25)


def _fake_run_skill(skill, node_id, graph_nodes, session_id, query, fr,
                    memory_hits=None, **kwargs):
    return h.fake_run_skill(skill, node_id, graph_nodes, session_id, query, fr,
                            memory_hits)


def _noop(*a, **k):
    return None


skills.run_skill = _fake_run_skill
flow.run_skill = _fake_run_skill
flow.ensure_gateway = _noop
_mem.read = lambda q, top_k=5, **kw: []
flow._safe_remember = _noop
flow._safe_purge_working = _noop
_persist._atomic_write = _atomic_retry

import agent_server as ag
from agent_server import app
# Kill the import-time warmup threads (they'd block joins / try to launch uv)
# and the Telegram notifier (real bot token lives in .env).
ag._GATEWAY_WARMUP_THREAD = None
ag._EMBEDDER_WARMUP_THREAD = None
ag.notify_task_done = _noop

from fastapi.testclient import TestClient

PASS, FAIL = [], []


def check(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f"PASS  {name}")
    except Exception as e:
        FAIL.append((name, e))
        print(f"FAIL  {name}: {e}")


def parse_sse(text):
    frames = []
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                frames.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                pass
    return frames


def frame_types(frames):
    return [f.get("type") for f in frames]


# ── 1. health & static ──────────────────────────────────────────────────────

def t_health():
    with TestClient(app) as c:
        r = c.get("/api/health")
        assert r.status_code == 200
        b = r.json()
        assert b.get("agent") == "ready", b
        assert "gateway_up" in b, b


def t_index_no_500():
    with TestClient(app) as c:
        for route in ("/", "/app.js", "/style.css"):
            r = c.get(route)
            assert r.status_code in (200, 404), (route, r.status_code)


# ── 2. /api/chat SSE end-to-end (real orchestrator) ─────────────────────────

def t_chat_shortcircuit():
    h.SCENARIO.clear(); h.CALL_LOG.clear(); h.CALL_COUNTS.clear()
    h.SCENARIO["planner"] = {"answer": "The capital of France is Paris."}
    with TestClient(app) as c:
        r = c.post("/api/chat", json={"query": "What is the capital of France?"})
        assert r.status_code == 200, r.status_code
        assert "text/event-stream" in r.headers.get("content-type", ""), \
            r.headers.get("content-type")
        frames = parse_sse(r.text)
        types = frame_types(frames)
        assert "done" in types, types
        assert "meta" in types, types
        done = next(f for f in frames if f.get("type") == "done")
        assert "Paris" in done.get("answer", ""), done
        assert done.get("session_id", "").startswith("s8-"), done
        meta = next(f for f in frames if f.get("type") == "meta")
        assert "elapsed_s" in meta, meta
        assert "cost_usd" in meta and "cost_in_tokens" in meta, meta
        assert any(f.get("type") == "log" for f in frames), types


def t_chat_conversation_continuity():
    h.SCENARIO.clear(); h.CALL_LOG.clear(); h.CALL_COUNTS.clear()
    h.SCENARIO["planner"] = {"answer": "Answer A."}
    with TestClient(app) as c:
        r1 = c.post("/api/chat", json={
            "query": "q1", "conversation_id": "battle-conv-1"})
        r2 = c.post("/api/chat", json={
            "query": "q2", "conversation_id": "battle-conv-1"})
        d1 = next(f for f in parse_sse(r1.text) if f.get("type") == "done")
        d2 = next(f for f in parse_sse(r2.text) if f.get("type") == "done")
        assert d1.get("session_id") == d2.get("session_id"), (d1, d2)
        assert d1.get("conversation_id") == "battle-conv-1", d1


def t_chat_full_dag_streams_logs():
    h.SCENARIO.clear(); h.CALL_LOG.clear(); h.CALL_COUNTS.clear()
    h.SCENARIO["planner"] = {"nodes": [
        {"skill": "researcher", "inputs": ["USER_QUERY"],
         "metadata": {"label": "r1"}},
        {"skill": "formatter", "inputs": ["n:r1"], "metadata": {"label": "f1"}},
    ]}
    h.SCENARIO["researcher"] = {"text": "Paris is the capital of France."}
    h.SCENARIO["formatter"] = {"final_answer": "Paris is the capital of France."}
    with TestClient(app) as c:
        r = c.post("/api/chat", json={"query": "Tell me about France."})
        frames = parse_sse(r.text)
        logs = [f.get("text", "") for f in frames if f.get("type") == "log"]
        joined = "\n".join(logs)
        assert "planner" in joined, joined[:500]
        assert "researcher" in joined, joined[:500]
        assert "formatter" in joined, joined[:500]
        done = next(f for f in frames if f.get("type") == "done")
        assert "Paris" in done.get("answer", ""), done


def t_chat_planner_failure_graceful():
    h.SCENARIO.clear(); h.CALL_LOG.clear(); h.CALL_COUNTS.clear()
    h.SCENARIO["planner"] = {"rejected": ["BAD SPEC"]}
    with TestClient(app) as c:
        r = c.post("/api/chat", json={"query": "do something weird"})
        assert r.status_code == 200, r.status_code
        frames = parse_sse(r.text)
        assert "done" in frame_types(frames), frame_types(frames)


def t_chat_error_when_gateway_down():
    # Simulate total gateway failure: make ensure_gateway raise.
    orig = gateway.ensure_gateway
    flow_orig = flow.ensure_gateway
    gateway.ensure_gateway = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("simulated gateway down"))
    flow.ensure_gateway = gateway.ensure_gateway
    try:
        with TestClient(app) as c:
            r = c.post("/api/chat", json={"query": "hello there agent"})
            assert r.status_code == 200, r.status_code
            frames = parse_sse(r.text)
            types = frame_types(frames)
            assert "error" in types or "done" in types, types
    finally:
        gateway.ensure_gateway = orig
        flow.ensure_gateway = flow_orig


# ── 3. cost / memory / audit endpoints after real runs ──────────────────────

def t_cost_endpoint_after_run():
    h.SCENARIO.clear(); h.CALL_LOG.clear(); h.CALL_COUNTS.clear()
    h.SCENARIO["planner"] = {"answer": "cost probe answer"}
    with TestClient(app) as c:
        c.post("/api/chat", json={"query": "cost probe",
                                  "conversation_id": "battle-cost"})
        r = c.get("/api/cost", params={"session": "battle-cost"})
        assert r.status_code == 200
        b = r.json()
        assert "rows" in b and "totals" in b, b.keys()


def t_memory_endpoint():
    with TestClient(app) as c:
        r = c.get("/api/memory", params={"q": "test", "limit": 5})
        assert r.status_code == 200
        assert "items" in r.json(), r.json().keys()


def t_audit_endpoint():
    with TestClient(app) as c:
        r = c.get("/api/audit")
        assert r.status_code == 200
        assert "items" in r.json(), r.json().keys()


# ── 4. scheduler over HTTP ──────────────────────────────────────────────────

def t_schedule_crud_http():
    with TestClient(app) as c:
        r = c.post("/api/schedule", json={"query": "battle test task",
                                          "when": "in 30m", "notify": False})
        assert r.status_code == 200
        b = r.json()
        assert b.get("status") == "ok" and b.get("id"), b
        sid = b["id"]
        r = c.get("/api/schedule")
        assert r.status_code == 200
        ids = [s["id"] for s in r.json().get("schedules", [])]
        assert sid in ids, (sid, ids)
        r = c.delete(f"/api/schedule/{sid}")
        assert r.json().get("status") == "ok", r.json()
        r = c.delete("/api/schedule/sch-nonexistent")
        assert r.json().get("status") == "not_found", r.json()


def t_schedule_validation():
    with TestClient(app) as c:
        r = c.post("/api/schedule", json={"query": "", "when": ""})
        assert r.json().get("status") == "error", r.json()


# ── 5. templates over HTTP ──────────────────────────────────────────────────

def t_template_crud_and_run():
    with TestClient(app) as c:
        r = c.post("/api/templates", json={
            "name": "battle-tpl", "query": "Research {topic}",
            "vars": ["topic"]})
        assert r.json().get("status") == "ok", r.json()
        try:
            r = c.get("/api/templates")
            names = [t["name"] for t in r.json().get("templates", [])]
            assert "battle-tpl" in names, names
            r = c.get("/api/templates/battle-tpl")
            assert r.json().get("query") == "Research {topic}", r.json()
            r = c.get("/api/templates/nope-missing")
            assert r.json().get("status") == "not_found", r.json()
            # Template run streams SSE like chat (real orchestrator path).
            h.SCENARIO.clear(); h.CALL_LOG.clear(); h.CALL_COUNTS.clear()
            h.SCENARIO["planner"] = {"answer": "template ran"}
            r = c.post("/api/templates/battle-tpl/run",
                       json={"vars": {"topic": "cats"}})
            frames = parse_sse(r.text)
            done = next(f for f in frames if f.get("type") == "done")
            assert "template ran" in done.get("answer", ""), done
        finally:
            c.delete("/api/templates/battle-tpl")


# ── 6. computer-use approval surface ────────────────────────────────────────

def t_computer_approvals_surface():
    with TestClient(app) as c:
        r = c.get("/api/computer/approvals")
        assert r.status_code == 200
        assert "approvals" in r.json(), r.json().keys()
        r = c.get("/api/computer/runs")
        assert r.status_code == 200
        r = c.post("/api/computer/approvals/does-not-exist",
                   json={"approve": True})
        # Must be a graceful JSON response, never a 500.
        assert r.status_code < 500, r.status_code


# ── 7. artifacts path-traversal guard ───────────────────────────────────────

def t_artifacts_traversal_blocked():
    with TestClient(app) as c:
        r = c.get("/api/artifacts/s8-x/..%2F..%2F..%2F.env")
        # Must never leak the file: 4xx, or 200 with non-env content.
        if r.status_code == 200:
            assert "TAVILY" not in r.text and "TOKEN" not in r.text, \
                "path traversal leaked .env!"
        else:
            assert r.status_code in (400, 403, 404), r.status_code


if __name__ == "__main__":
    tests = [
        t_health, t_index_no_500,
        t_chat_shortcircuit, t_chat_conversation_continuity,
        t_chat_full_dag_streams_logs, t_chat_planner_failure_graceful,
        t_chat_error_when_gateway_down,
        t_cost_endpoint_after_run, t_memory_endpoint, t_audit_endpoint,
        t_schedule_crud_http, t_schedule_validation,
        t_template_crud_and_run,
        t_computer_approvals_surface,
        t_artifacts_traversal_blocked,
    ]
    for t in tests:
        check(t.__name__, t)
    print(f"\n{len(PASS)}/{len(tests)} passed")
    if FAIL:
        print("FAILURES:")
        for name, err in FAIL:
            print(f"  {name}: {err}")
        sys.exit(1)
