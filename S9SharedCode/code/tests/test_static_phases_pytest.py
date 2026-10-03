"""Pytest conversion of the phase-3..9 static checks.

The original `test_static_phases.py` runs the same logic but calls
`sys.exit()` at module level, which aborts pytest collection. This file
re-implements those exact checks as real pytest functions (assertions raise,
so failures are reported normally and the whole suite can run).

Covers: schema contracts, persistence/artifacts, vector index, recovery
decision table, and the skills registry.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import networkx as nx
import pytest

from artifacts import put, exists, get_bytes
from persistence import SessionStore, SessionLoadError
from recovery import classify_failure, plan_recovery
from schemas import (AgentResult, MemoryItem, Goal, Observation,
                     DecisionOutput, ToolCall, BrowserOutput)
from skills import SkillRegistry
from vector_index import VectorIndex


# ── Phase 3: schema contracts ────────────────────────────────────────────────
def test_agentresult_roundtrip():
    r = AgentResult(success=True, output={"key": "value"}, agent_name="test")
    d = r.model_dump()
    r2 = AgentResult.model_validate(d)
    assert r2.success is True
    assert r2.output["key"] == "value"


def test_agentresult_rejects_bad():
    with pytest.raises(Exception):
        AgentResult.model_validate({"bad": "data"})


def test_memoryitem_embedding():
    m = MemoryItem(id="m1", kind="fact", descriptor="test", value={"raw": "hello"},
                   embedding=[0.1] * 768, source="test", run_id="r1")
    d = m.model_dump(mode="json")
    m2 = MemoryItem.model_validate(d)
    assert len(m2.embedding) == 768


def test_goal_observation():
    g = Goal(id="g1", text="do thing", done=False)
    assert Observation(goals=[g]).all_done is False
    g.done = True
    assert Observation(goals=[g]).all_done is True


def test_decision_output():
    assert DecisionOutput(answer="hello").is_answer is True
    assert DecisionOutput(tool_call=ToolCall(id="t1", name="tool", arguments={})).is_answer is False


def test_browser_output():
    b = BrowserOutput(path="extract", url="http://example.com", goal="test",
                      content="test", method="extract")
    assert b.path == "extract"


# ── Phase 4: persistence & artifacts ────────────────────────────────────────
def _session_root(tmp: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Root the session store at a tmp dir.

    Session ids are validated (persistence._ID_RE) so a crafted id can't
    escape SESSIONS_ROOT — which means a raw tmp path is no longer a legal
    id. Point the root at the tmp dir instead and use a real id.
    """
    import persistence
    root = Path(tmp) / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(persistence, "SESSIONS_ROOT", root)
    return root


def test_session_roundtrip(tmp_path, monkeypatch):
    _session_root(str(tmp_path), monkeypatch)
    store = SessionStore("t-sp-roundtrip")
    store.write_query("test query")
    g = nx.DiGraph()
    g.add_node("n:1", skill="planner", status="complete",
               result=AgentResult(success=True, output={"a": 1}, agent_name="planner"))
    store.write_graph(g)
    g2 = store.read_graph()
    assert "n:1" in g2.nodes
    assert g2.nodes["n:1"]["result"].success is True


def test_corrupt_graph_handling(tmp_path, monkeypatch):
    _session_root(str(tmp_path), monkeypatch)
    store = SessionStore("t-sp-corrupt")
    with open(store.graph_path, "w", encoding="utf-8") as f:
        json.dump({"nodes": [{"id": "n:1", "result": {"bad": "data"},
                              "_result_typed": True, "skill": "planner",
                              "status": "complete"}], "edges": []}, f)
    with pytest.raises(SessionLoadError):
        store.read_graph()


def test_artifacts_roundtrip_dedup():
    tmp = tempfile.mkdtemp()
    try:
        import artifacts as art
        art.STORE = Path(tmp) / "artifacts"
        art.STORE.mkdir(exist_ok=True)
        aid = put(b"hello world content", content_type="text/plain",
                  source="test", descriptor="test")
        assert exists(aid)
        assert get_bytes(aid) == b"hello world content"
        aid2 = put(b"hello world content", content_type="text/plain",
                   source="test", descriptor="test")
        assert aid == aid2  # dedup
    finally:
        shutil.rmtree(tmp)


# ── Phase 5: vector index ────────────────────────────────────────────────────
def test_vector_index_add_search():
    tmp = tempfile.mkdtemp()
    try:
        idx = VectorIndex(Path(tmp))
        idx.add("a", [1, 0, 0, 0])
        idx.add("b", [0, 1, 0, 0])
        idx.add("c", [0.9, 0.1, 0, 0])
        results = idx.search([1, 0, 0, 0], k=2)
        assert results[0][0] == "a"
        assert results[0][1] > 0.99
    finally:
        shutil.rmtree(tmp)


# ── Phase 6: recovery decision table ────────────────────────────────────────
def test_classify_transient():
    assert classify_failure("503 Service Unavailable") == "transient"
    assert classify_failure("timeout") == "transient"
    assert classify_failure("connection error") == "transient"


def test_classify_environmental():
    assert classify_failure("computer-use is disabled") == "environmental"
    assert classify_failure("daemon-error") == "environmental"


def test_classify_validation():
    assert classify_failure("validation error: bad") == "validation_error"


def test_plan_recovery_table():
    # CONTRACT (updated): transient always SKIPS, even for planner — the
    # gateway owns retries; the orchestrator must not re-plan on a 503.
    assert plan_recovery(failed_skill="researcher", error_text="503 timeout",
                         failed_node_id="n:1").action == "skip"
    assert plan_recovery(failed_skill="planner", error_text="503",
                         failed_node_id="n:1").action == "skip"
    # upstream_failure: planner skips (would loop), others replan
    assert plan_recovery(failed_skill="planner", error_text="model hallucinated",
                         failed_node_id="n:1").action == "skip"
    assert plan_recovery(failed_skill="researcher", error_text="model hallucinated",
                         failed_node_id="n:1").action == "replan"
    # environmental skips
    assert plan_recovery(failed_skill="researcher", error_text="computer-use is disabled",
                         failed_node_id="n:1").action == "skip"


# ── Phase 9: skills registry ─────────────────────────────────────────────────
def test_registry_loads():
    reg = SkillRegistry()
    names = reg.names()
    for required in ("planner", "formatter", "action", "browser"):
        assert required in names
    assert len(names) >= 12


def test_skill_prompts_exist():
    reg = SkillRegistry()
    for name in reg.names():
        assert reg.get(name).prompt_template() is not None
