"""Phase 3 (schemas) + Phase 4 (persistence/artifacts) logic checks."""
from __future__ import annotations
import sys, json, tempfile, os
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent / "S9SharedCode" / "code"
sys.path.insert(0, str(ROOT))

import schemas as S
from schemas import (MemoryItem, AgentResult, BrowserOutput, NodeState, Goal,
                     Observation, DecisionOutput, ToolCall, Artifact, NodeSpec)
from persistence import SessionStore, SessionLoadError
import artifacts as ART

fails = []
def check(name, cond, detail=""):
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name}  {detail}")
        fails.append(name)

# ── Phase 3: schemas ──
mi = MemoryItem(id="m:1", kind="fact", descriptor="d", source="s", run_id="r")
check("MemoryItem default embedding None", mi.embedding is None)
rt = mi.model_dump(); mr = MemoryItem.model_validate(rt)
check("MemoryItem round-trip", mr.id == mi.id and mr.embedding is None)

# AgentResult error_code literals
for code in ("gateway_blocked","extraction_failed","interaction_failed","timeout","vlm_unavailable"):
    ar = AgentResult(success=False, agent_name="browser", error_code=code)
    check(f"AgentResult error_code={code}", AgentResult.model_validate(ar.model_dump()).error_code == code)
try:
    AgentResult(success=False, agent_name="x", error_code="bogus")
    check("AgentResult rejects bad error_code", False, "should raise")
except Exception:
    check("AgentResult rejects bad error_code", True)

# BrowserOutput path literal
for p in ("extract","deterministic","a11y","vision"):
    bo = BrowserOutput(url="u", goal="g", path=p)
    check(f"BrowserOutput path={p}", BrowserOutput.model_validate(bo.model_dump()).path == p)
try:
    BrowserOutput(url="u", goal="g", path="bogus")
    check("BrowserOutput rejects bad path", False)
except Exception:
    check("BrowserOutput rejects bad path", True)

# NodeState nested AgentResult stays typed
ns = NodeState(node_id="n:1", skill="planner", status="complete",
               result=AgentResult(success=True, agent_name="planner", output={"x":1}))
ns2 = NodeState.model_validate_json(ns.model_dump_json())
check("NodeState result stays AgentResult", isinstance(ns2.result, AgentResult), type(ns2.result))

# Observation properties
obs = Observation(goals=[Goal(id="g:1", text="a", done=False), Goal(id="g:2", text="b", done=True)])
check("Observation.all_done false", obs.all_done is False)
check("Observation.next_unfinished", obs.next_unfinished().id == "g:1")
obs2 = Observation(goals=[Goal(id="g:1", text="a", done=True)])
check("Observation.all_done true", obs2.all_done is True)

# DecisionOutput
check("DecisionOutput is_answer (answer)", DecisionOutput(answer="hi").is_answer is True)
check("DecisionOutput is_answer (tool)", DecisionOutput(tool_call=ToolCall(name="x", arguments={})).is_answer is False)
check("DecisionOutput both None", DecisionOutput().is_answer is False)

# ── Phase 4: artifacts ──
tmp = Path(tempfile.mkdtemp())
os.chdir(ROOT)  # artifacts store is ROOT/state/artifacts
bid = ART.put(b"hello world", content_type="text/plain", source="t", descriptor="d")
check("artifacts.put returns art:id", bid.startswith("art:"))
bid2 = ART.put(b"hello world", content_type="text/plain", source="t", descriptor="d")
check("artifacts dedup same bytes", bid == bid2)
check("artifacts.get_bytes", ART.get_bytes(bid) == b"hello world")
check("artifacts.exists", ART.exists(bid) is True)
meta = ART.get_meta(bid)
check("artifacts.get_meta", meta.id == bid and meta.size_bytes == 11)

# ── Phase 4: persistence ──
sid = "test_session_xyz"
store = SessionStore(sid)
store.write_query("what is 2+2?")
check("persistence query round-trip", store.read_query() == "what is 2+2?")
ns3 = NodeState(node_id="n:1", skill="planner", status="complete",
               result=AgentResult(success=True, agent_name="planner", output={"final_answer":"4"}))
store.write_node(ns3)
got = store.read_node("n:1")
check("persistence node round-trip", got is not None and got.skill == "planner")
check("persistence node result typed", isinstance(got.result, AgentResult))

# graph JSON round-trip with typed result
import networkx as nx
g = nx.DiGraph(); g.add_node("n:1", skill="planner", status="complete",
                             result=AgentResult(success=True, agent_name="planner", output={"a":1}))
store.write_graph(g)
g2 = store.read_graph()
check("persistence graph revive typed result",
      isinstance(g2.nodes["n:1"]["result"], AgentResult))

# corrupt graph -> SessionLoadError
bad = ROOT / "state" / "sessions" / sid / "graph.json"
# result dict missing required 'agent_name' -> model_validate must fail
bad.write_text(json.dumps({"nodes":[{"id":"n:1","skill":"planner","status":"complete",
    "_result_typed":True,"result":{"success":True,"output":{"a":1}}}],
    "links":[]}))
try:
    store.read_graph()
    check("persistence corrupt graph raises", False, "no error")
except SessionLoadError:
    check("persistence corrupt graph raises", True)
except Exception as e:
    check("persistence corrupt graph raises", False, f"wrong exc: {e}")

# corrupt node file skipped
import shutil
nd = ROOT / "state" / "sessions" / sid / "nodes"
(nd / "n_002.json").write_text("{ this is not json")
states = store.read_all_nodes()
check("persistence skips corrupt node", all(isinstance(s, NodeState) for s in states))

# cleanup
shutil.rmtree(ROOT / "state" / "sessions" / sid, ignore_errors=True)

print(f"\nPHASE3+4: {len(fails)} failures: {fails}")
