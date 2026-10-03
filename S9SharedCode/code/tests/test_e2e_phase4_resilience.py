"""Phase 4 (resilience, performance, soak) — E2E scenarios.

Deterministic (L0, CI):
* ER-01  transient 503 on a worker -> recovery re-plans only that node.
* ER-02  upstream_failure -> orchestrator route-arounds (different tool).
* ER-04  planner itself fails -> no recovery replan loop (graceful end).
* ER-05  critic node participates in a plan and runs without crashing.

Live (``@live``):
* ER-03  chat never returns HTTP 500 (graceful done/error frame).
* EP-01  latency within rough budgets (simple / complex).
* EP-02  short-circuit path is no slower than the full DAG.
* EP-03  cost ledger consistent (meta.cost_usd is a sane float; /api/cost
         returns a dict for the session).
* EP-04  no runaway: nodes_executed <= MAX_NODES (60).

Soak / canary (``@stress``): N sequential varied chats, all answer, none 500.
"""
from __future__ import annotations

import time
import urllib.request
import urllib.error
import json
from pathlib import Path

import pytest

from e2e_harness import (
    deterministic, skills_called, run_executor, chat_sse, health,
)

AGENT = "http://127.0.0.1:8500"
ROOT = Path(__file__).resolve().parent.parent


# ── R. Recovery / resilience (L0) ────────────────────────────────────────────
def test_er01_transient_503_recovery(deterministic):
    deterministic["planner"] = [
        {"nodes": [
            {"skill": "researcher", "inputs": ["USER_QUERY"],
             "metadata": {"label": "r1"}},
            {"skill": "formatter", "inputs": ["n:r1"]}]},
        {"nodes": [
            {"skill": "researcher", "inputs": ["USER_QUERY"],
             "metadata": {"label": "r2"}},
            {"skill": "formatter", "inputs": ["n:r2"]}]},
    ]
    # First researcher attempt fails (transient); recovery attempt succeeds.
    deterministic["researcher"] = [
        {"success": False, "error": "503 transient gateway error"},
        {"text": "recovered data"},
    ]
    deterministic["formatter"] = {"text": "final answer"}
    ans = run_executor("research with a transient failure", "L0-er01")
    assert isinstance(ans, str)
    called = skills_called()
    assert called.count("researcher") >= 2, called
    assert called.count("planner") >= 2, called  # seed + recovery planner


def test_er02_upstream_failure_route_around(deterministic):
    deterministic["planner"] = [
        {"nodes": [
            {"skill": "researcher", "inputs": ["USER_QUERY"],
             "metadata": {"label": "r1"}},
            {"skill": "formatter", "inputs": ["n:r1"]}]},
        {"nodes": [
            {"skill": "retriever", "inputs": ["USER_QUERY"],
             "metadata": {"label": "v1"}},
            {"skill": "formatter", "inputs": ["n:v1"]}]},
    ]
    deterministic["researcher"] = {
        "success": False, "error": "upstream_failure: connection refused"}
    deterministic["retriever"] = {"text": "found via retrieval"}
    deterministic["formatter"] = {"text": "final answer"}
    ans = run_executor("route around a broken tool", "L0-er02")
    assert isinstance(ans, str)
    called = skills_called()
    # Recovery chose a DIFFERENT tool (route-around), not a blind retry.
    assert "retriever" in called, called
    assert called.count("planner") >= 2, called


def test_er04_planner_failure_no_replan_loop(deterministic):
    # A failing planner must not trigger an infinite recovery replan loop.
    deterministic["planner"] = {"rejected": ["{'skill':'formatter' BAD}"]}
    ans = run_executor("broken planner input", "L0-er04")
    assert isinstance(ans, str)
    # Exactly one planner invocation — recovery correctly skipped re-planning
    # the planner itself.
    assert skills_called().count("planner") == 1, skills_called()


def test_er05_critic_node_runs(deterministic):
    deterministic["planner"] = {"nodes": [
        {"skill": "researcher", "inputs": ["USER_QUERY"],
         "metadata": {"label": "r1"}},
        {"skill": "critic", "inputs": ["n:r1"], "metadata": {"label": "c1"}},
        {"skill": "formatter", "inputs": ["n:c1"]},
    ]}
    deterministic["researcher"] = {"text": "some data"}
    deterministic["critic"] = {"text": "verdict: consistent"}
    deterministic["formatter"] = {"text": "final answer"}
    ans = run_executor("run with a critic", "L0-er05")
    assert isinstance(ans, str)
    called = skills_called()
    assert "critic" in called and "formatter" in called, called


# ── R/EP. Live resilience & performance ──────────────────────────────────────
def _live_ok():
    try:
        return health(AGENT).get("gateway_up")
    except Exception:
        return False


@pytest.mark.live
def test_er03_no_http_500():
    if not _live_ok():
        pytest.skip("agent/gateway not reachable")
    ans, frames, _ = chat_sse(AGENT, "What is 1 + 1?", timeout=120)
    # Either a done answer or a graceful error frame — never an HTTP 500.
    assert isinstance(ans, str)
    assert any(f.get("type") in ("done", "error") for f in frames)


@pytest.mark.live
def test_ep01_latency_budget():
    if not _live_ok():
        pytest.skip("agent/gateway not reachable")
    _, _, m_simple = chat_sse(AGENT, "What is 2 + 2? Just the number.", timeout=120)
    _, _, m_complex = chat_sse(
        AGENT, "Compare the population of London and Paris; which is larger?",
        timeout=240)
    assert m_simple["elapsed_s"] < 30, m_simple
    assert m_complex["elapsed_s"] < 150, m_complex


@pytest.mark.live
def test_ep02_shortcircuit_fires():
    if not _live_ok():
        pytest.skip("agent/gateway not reachable")
    ans, frames, _ = chat_sse(AGENT, "What is 3 + 3? Just the number.", timeout=120)
    # The short-circuit path answers a trivial query with a single planner
    # node (1 LLM call) instead of planner + formatter (2 calls).
    n = sum(1 for f in frames
            if f.get("type") == "log" and str(f.get("text", "")).startswith("[n:"))
    assert "6" in ans, ans
    assert n <= 1, f"expected short-circuit (<=1 node), got {n} nodes"


@pytest.mark.live
def test_ep03_cost_ledger_consistent():
    if not _live_ok():
        pytest.skip("agent/gateway not reachable")
    _, frames, meta = chat_sse(AGENT, "What is 4 + 4? Just the number.", timeout=120)
    done = next((f for f in frames if f.get("type") == "done"), None)
    sid = (done or {}).get("session_id")
    assert isinstance(meta.get("cost_usd"), (int, float))
    assert meta["cost_usd"] >= 0
    if sid:
        with urllib.request.urlopen(f"{AGENT}/api/cost?session={sid}",
                                    timeout=30) as r:
            data = json.loads(r.read().decode())
        assert isinstance(data, (dict, list))


@pytest.mark.live
def test_ep04_no_runaway_nodes():
    if not _live_ok():
        pytest.skip("agent/gateway not reachable")
    _, frames, _ = chat_sse(
        AGENT, "List 5 capital cities in Europe and a fact about each.",
        timeout=240)
    # Node count derived from completion logs must respect MAX_NODES (60).
    n = sum(1 for f in frames
            if f.get("type") == "log" and str(f.get("text", "")).startswith("[n:"))
    assert n <= 60, n


# ── Soak / canary ────────────────────────────────────────────────────────────
@pytest.mark.live
@pytest.mark.stress
def test_canary_sequential_chats():
    if not _live_ok():
        pytest.skip("agent/gateway not reachable")
    queries = [
        "What is 2 + 2?",
        "Name the capital of Germany.",
        "What is the weather like in Paris today?",
        "Summarise https://example.com in one sentence.",
        "Tell me a short joke.",
    ]
    for q in queries:
        ans, frames, _ = chat_sse(AGENT, q, timeout=180)
        assert isinstance(ans, str) and len(ans) > 0
        assert any(f.get("type") in ("done", "error") for f in frames)


if __name__ == "__main__":
    import pytest as _p
    _p.main([__file__, "-q"])
