"""Comprehensive static test harness for phases 3-9."""
import sys, os, json, tempfile, shutil
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

results = []

def check(name, fn):
    try:
        fn()
        results.append((name, "PASS", ""))
        print(f"  [PASS] {name}")
    except Exception as e:
        results.append((name, "FAIL", str(e)))
        print(f"  [FAIL] {name}: {e}")

# ── Phase 3: Schema contracts ──────────────────────────────────────────────
print("\n=== Phase 3: Schema contracts ===")

from schemas import (AgentResult, NodeState, Goal, Observation, MemoryItem,
                     ToolCall, DecisionOutput, NodeSpec, BrowserOutput,
                     Artifact, ErrorCode, new_id)

def t_agentresult_roundtrip():
    r = AgentResult(success=True, output={"key": "value"}, agent_name="test")
    d = r.model_dump()
    r2 = AgentResult.model_validate(d)
    assert r2.success == True
    assert r2.output["key"] == "value"

def t_agentresult_rejects_bad():
    try:
        AgentResult.model_validate({"bad": "data"})
        raise AssertionError("should have failed")
    except Exception as e:
        if "validation error" in str(e).lower() or "missing" in str(e).lower():
            return
        raise

def test_memoryitem_embedding():
    m = MemoryItem(id="m1", kind="fact", descriptor="test", value={"raw": "hello"},
                   embedding=[0.1]*768, source="test", run_id="r1")
    d = m.model_dump(mode="json")
    m2 = MemoryItem.model_validate(d)
    assert len(m2.embedding) == 768

def test_goal_observation():
    g = Goal(id="g1", text="do thing", done=False)
    obs = Observation(goals=[g])
    assert obs.all_done == False
    g.done = True
    assert Observation(goals=[g]).all_done == True

def test_decision_output():
    d1 = DecisionOutput(answer="hello")
    assert d1.is_answer == True
    d2 = DecisionOutput(tool_call=ToolCall(id="t1", name="tool", arguments={}))
    assert d2.is_answer == False

def test_browser_output():
    b = BrowserOutput(path="extract", url="http://example.com", goal="test", content="test", method="extract")
    assert b.path == "extract"

check("AgentResult round-trip", t_agentresult_roundtrip)
check("AgentResult rejects bad", t_agentresult_rejects_bad)
check("MemoryItem embedding", test_memoryitem_embedding)
check("Goal/Observation", test_goal_observation)
check("DecisionOutput", test_decision_output)
check("BrowserOutput", test_browser_output)

# ── Phase 4: Persistence & artifacts ────────────────────────────────────────
print("\n=== Phase 4: Persistence & artifacts ===")

import networkx as nx
from persistence import SessionStore, SessionLoadError

def _session_root(tmp):
    """Root SessionStore at a tmp dir.

    Session ids are validated (persistence._ID_RE) so a crafted id can't
    escape SESSIONS_ROOT — a raw tmp path is therefore not a legal id. Point
    the root at the tmp dir and use a real id instead.
    """
    import persistence
    root = Path(tmp) / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    old = persistence.SESSIONS_ROOT
    persistence.SESSIONS_ROOT = root
    return old

def t_session_roundtrip():
    tmp = tempfile.mkdtemp()
    import persistence
    old = _session_root(tmp)
    try:
        store = SessionStore("t-sp-roundtrip")
        store.write_query("test query")
        g = nx.DiGraph()
        g.add_node("n:1", skill="planner", status="complete",
                   result=AgentResult(success=True, output={"a": 1}, agent_name="planner"))
        store.write_graph(g)
        g2 = store.read_graph()
        assert "n:1" in g2.nodes
        assert g2.nodes["n:1"]["result"].success == True
    finally:
        persistence.SESSIONS_ROOT = old
        shutil.rmtree(tmp)

def test_corrupt_graph():
    tmp = tempfile.mkdtemp()
    import persistence
    old = _session_root(tmp)
    try:
        store = SessionStore("t-sp-corrupt")
        # Write corrupt graph.json: _result_typed=true but result fails validation
        with open(store.graph_path, "w") as f:
            json.dump({"nodes": [{"id": "n:1", "result": {"bad": "data"}, "_result_typed": True, "skill": "planner", "status": "complete"}], "edges": []}, f)
        try:
            store.read_graph()
            raise AssertionError("should have raised SessionLoadError")
        except SessionLoadError:
            pass
    finally:
        persistence.SESSIONS_ROOT = old
        shutil.rmtree(tmp)

def test_artifacts_roundtrip():
    tmp = tempfile.mkdtemp()
    try:
        import artifacts as art
        art.STORE = Path(tmp) / "artifacts"
        art.STORE.mkdir(exist_ok=True)
        aid = art.put(b"hello world content", content_type="text/plain", source="test", descriptor="test")
        assert art.exists(aid)
        assert art.get_bytes(aid) == b"hello world content"
        # Dedup
        aid2 = art.put(b"hello world content", content_type="text/plain", source="test", descriptor="test")
        assert aid == aid2
    finally:
        shutil.rmtree(tmp)

check("SessionStore round-trip", t_session_roundtrip)
check("Corrupt graph handling", test_corrupt_graph)
check("Artifacts round-trip + dedup", test_artifacts_roundtrip)

# ── Phase 5: Memory service + vector index ──────────────────────────────────
print("\n=== Phase 5: Memory service + vector index ===")

from vector_index import VectorIndex

def test_vector_index():
    tmp = tempfile.mkdtemp()
    try:
        idx = VectorIndex(Path(tmp))
        idx.add("a", [1,0,0,0])
        idx.add("b", [0,1,0,0])
        idx.add("c", [0.9,0.1,0,0])
        results = idx.search([1,0,0,0], k=2)
        assert results[0][0] == "a"
        assert results[0][1] > 0.99
    finally:
        shutil.rmtree(tmp)

def test_keyword_search():
    # Retrieval ranking moved gateway-side (llm_gatewayV9/memory/service.py).
    # Agent-side retains only the keyword extractor for the classifier
    # fallback — pin its contract here.
    from memory import _tokens
    toks = _tokens("What is the capital of France?")
    assert "capital" in toks and "france" in toks
    assert "is" not in toks and "the" not in toks and "what" not in toks

check("VectorIndex add/search", test_vector_index)

# ── Phase 6: Recovery logic ─────────────────────────────────────────────────
print("\n=== Phase 6: Recovery logic ===")

from recovery import classify_failure, plan_recovery, handle_critic_verdict, RecoveryDecision

def test_classify_transient():
    assert classify_failure("503 Service Unavailable") == "transient"
    assert classify_failure("timeout") == "transient"
    assert classify_failure("connection error") == "transient"

def test_classify_environmental():
    assert classify_failure("computer-use is disabled") == "environmental"
    assert classify_failure("daemon-error") == "environmental"

def test_classify_validation():
    assert classify_failure("validation error: bad") == "validation_error"

def test_plan_recovery():
    # recovery.plan_recovery's decision table: transient → skip (the gateway
    # already exhausted its own retry before the failure reaches us).
    d = plan_recovery(failed_skill="researcher", error_text="503 timeout", failed_node_id="n:1")
    assert d.action == "skip", f"got {d.action}"
    d = plan_recovery(failed_skill="planner", error_text="503", failed_node_id="n:1")
    assert d.action == "skip", f"got {d.action}"
    # upstream_failure: planner skips (would loop), others replan
    d = plan_recovery(failed_skill="planner", error_text="model hallucinated", failed_node_id="n:1")
    assert d.action == "skip", f"got {d.action}"
    d = plan_recovery(failed_skill="researcher", error_text="model hallucinated", failed_node_id="n:1")
    assert d.action == "replan", f"got {d.action}"
    # environmental skips
    d = plan_recovery(failed_skill="researcher", error_text="computer-use is disabled", failed_node_id="n:1")
    assert d.action == "skip", f"got {d.action}"

check("classify_failure transient", test_classify_transient)
check("classify_failure environmental", test_classify_environmental)
check("classify_failure validation", test_classify_validation)
check("plan_recovery decision table", test_plan_recovery)

# ── Phase 9: Skills registry ────────────────────────────────────────────────
print("\n=== Phase 9: Skills registry ===")

from skills import SkillRegistry

def test_registry_loads():
    reg = SkillRegistry()
    names = reg.names()
    assert "planner" in names
    assert "formatter" in names
    assert "action" in names
    assert "browser" in names
    assert len(names) >= 12

def test_skill_prompts_exist():
    reg = SkillRegistry()
    for name in reg.names():
        sk = reg.get(name)
        assert sk.prompt_template() is not None

check("SkillRegistry loads", test_registry_loads)
check("Skill prompts exist", test_skill_prompts_exist)

# ── Summary ─────────────────────────────────────────────────────────────────
def _main():
    print("\n" + "="*60)
    passed = sum(1 for _,s,_ in results if s == "PASS")
    failed = sum(1 for _,s,_ in results if s == "FAIL")
    print(f"RESULTS: {passed} passed, {failed} failed, {len(results)} total")
    if failed:
        print("\nFailures:")
        for n, s, e in results:
            if s == "FAIL":
                print(f"  - {n}: {e}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _main()
