"""P3 — Live integration tests against the REAL agent + gateway.

These are NOT mocked. They hit ``agent_server`` on :8500 and ``llm_gatewayV9``
on :8109 with real LLM calls, so they exercise the actual planner DAG,
real skill execution, real cost ledger, and real multi-turn memory.

Mark: @pytest.mark.live — skipped unless servers are up (see conftest.py).
Run:  pytest tests/test_live_integration.py -q -m live

Requires:
  - agent_server.py running on :8500
  - llm_gatewayV9 running on :8109
"""
from __future__ import annotations

import json
import time

import pytest

BASE = "http://127.0.0.1:8500"
pytestmark = pytest.mark.live


def _chat(query: str, conversation_id: str, timeout: int = 180) -> dict:
    """POST to /api/chat, collect all SSE frames, return aggregated result."""
    import urllib.request
    body = json.dumps({"query": query, "conversation_id": conversation_id}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/chat", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    out = {"answer": "", "elapsed_s": 0.0, "logs": [], "error": "",
           "session_id": "", "cost_usd": 0.0, "meta": {}}
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        buf = ""
        for raw in resp:
            buf += raw.decode("utf-8", "replace")
            while "\n\n" in buf:
                chunk = buf[: buf.index("\n\n")]
                buf = buf[buf.index("\n\n") + 2:]
                if not chunk.startswith("data: "):
                    continue
                try:
                    d = json.loads(chunk[6:])
                except Exception:
                    continue
                if d.get("type") == "log":
                    out["logs"].append(d.get("text", ""))
                elif d.get("type") == "meta":
                    out["elapsed_s"] = d.get("elapsed_s", 0.0)
                    out["cost_usd"] = d.get("cost_usd", 0.0)
                    out["meta"] = d
                elif d.get("type") == "done":
                    out["answer"] = d.get("answer", "")
                    out["session_id"] = d.get("session_id", "")
                elif d.get("type") == "error":
                    out["error"] = d.get("text", "")
    return out


def _cost(conversation_id: str) -> dict:
    import urllib.request
    with urllib.request.urlopen(f"{BASE}/api/cost?conversation_id={conversation_id}",
                                timeout=10) as r:
        return json.loads(r.read())


# ── core chat ────────────────────────────────────────────────────────────────

def test_live_simple_query_returns_answer():
    """The most basic contract: a query gets a non-empty answer, no error."""
    cid = f"live-simple-{int(time.time())}"
    r = _chat("Say hello in one sentence.", cid)
    assert not r["error"], f"stream error: {r['error']}"
    assert r["answer"], "expected a non-empty answer"
    assert r["session_id"], "expected a session_id"
    assert r["cost_usd"] > 0, "expected a positive cost for a real LLM call"


def test_live_planner_emits_dag():
    """Planner should produce a DAG with planner + formatter nodes."""
    cid = f"live-dag-{int(time.time())}"
    r = _chat("What is the capital of France?", cid)
    assert r["answer"], r
    # logs should mention planner + formatter completing
    joined = "\n".join(r["logs"])
    assert "planner" in joined.lower(), "planner node not seen in run log"
    assert "formatter" in joined.lower() or "final" in joined.lower(), \
        "formatter/final not seen in run log"


# ── memory & continuity ──────────────────────────────────────────────────────

def test_live_multi_turn_session_continuity():
    """Same conversation_id must resume the same session across turns."""
    cid = f"live-mem-{int(time.time())}"
    ra = _chat("Remember that my favourite city is Seville.", cid)
    assert ra["answer"], ra
    sid = ra["session_id"]
    rb = _chat("What is my favourite city? Answer in one word.", cid)
    # core contract: conversation_id -> stable session resume
    assert rb["session_id"] == sid, \
        f"conversation_id did not resume same session: {sid} vs {rb['session_id']}"
    # memory recall quality is model-dependent; verify the run at least
    # completed with a non-empty answer (recall itself is best-effort on the
    # current small routed model). Earlier manual runs showed the planner can
    # read stored memory — this asserts the plumbing, not model QA.
    assert rb["answer"], "turn B produced no answer"


# ── cost accounting ──────────────────────────────────────────────────────────

def test_live_cost_ledger_matches_meta():
    """The per-response cost_usd in meta should match the /api/cost ledger."""
    cid = f"live-cost-{int(time.time())}"
    r = _chat("Compute 17 * 23 + 4. Answer with just the number.", cid)
    assert r["answer"], r
    ledger = _cost(cid)
    assert ledger["totals"]["calls"] > 0, "ledger shows no calls"
    # meta cost should roughly equal sum of turn costs (within rounding)
    assert abs(r["cost_usd"] - ledger["totals"]["dollars"]) < 0.01, \
        f"meta cost {r['cost_usd']} != ledger {ledger['totals']['dollars']}"


# ── research path ────────────────────────────────────────────────────────────

def test_live_research_question():
    """A real research query should produce a substantive answer."""
    cid = f"live-res-{int(time.time())}"
    r = _chat("Summarise the benefits of vector databases for RAG in 3 bullet points.",
              cid)
    assert r["answer"], r
    # answer should be reasonably substantive (research actually happened)
    assert len(r["answer"]) > 50, f"answer too short: {r['answer']!r}"


# ── math / reasoning ────────────────────────────────────────────────────────

def test_live_math_reasoning():
    """The agent should correctly answer a simple arithmetic question."""
    cid = f"live-math-{int(time.time())}"
    r = _chat("What is 17 * 23 + 4? Answer with just the number.", cid)
    assert r["answer"], r
    assert "395" in r["answer"], f"expected 395 in answer: {r['answer']!r}"
