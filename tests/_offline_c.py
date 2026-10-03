"""OFFLINE Suite C: recovery classification + flow graph topology (no LLM)."""
import sys, os
sys.path.insert(0, ".")
os.environ.setdefault("S9_LLM_PROVIDER", "")

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail if not cond else ""))

from recovery import classify_failure, plan_recovery

# ── failure classification ──
check("C.cls.503", classify_failure("HTTP 503 Service Unavailable") == "transient")
check("C.cls.502", classify_failure("502 Bad Gateway") == "transient")
check("C.cls.timeout", classify_failure("request timed out after 30s") == "transient")
check("C.cls.conn", classify_failure("ConnectionError refused") == "transient")
check("C.cls.validation", classify_failure("ValidationError: bad field") == "validation_error")
check("C.cls.malformed", classify_failure("malformed NodeSpec emitted") == "validation_error")
check("C.cls.env_daemon", classify_failure("cua-driver daemon unavailable") == "environmental")
check("C.cls.env_approval", classify_failure("action requires your approval") == "environmental")
# F7: deterministic sandbox timeout must be environmental (skip), NOT transient
check("C.cls.det_timeout", classify_failure("sandbox timeout: code exceeded 30s wall-clock limit [deterministic-timeout]") == "environmental",
      f"got {classify_failure('sandbox timeout: code exceeded 30s wall-clock limit [deterministic-timeout]')}")
check("C.cls.upstream", classify_failure("weird unknown error") == "upstream_failure")
check("C.cls.empty", classify_failure("") == "upstream_failure")

# ── plan_recovery decision table ──
# CONTRACT (updated): transient failures SKIP — the gateway owns retries;
# the orchestrator must not re-plan on top of a 503.
d = plan_recovery(failed_skill="researcher", error_text="503 service unavailable", failed_node_id="n:2")
check("C.dec.transient_skip", d.action == "skip" and d.reason == "transient")

d = plan_recovery(failed_skill="sandbox_executor",
                  error_text="sandbox timeout: code exceeded 30s wall-clock limit [deterministic-timeout]",
                  failed_node_id="n:3")
check("C.dec.det_timeout_skip", d.action == "skip", f"got {d.action}")

d = plan_recovery(failed_skill="planner", error_text="some upstream crash", failed_node_id="n:1")
check("C.dec.planner_skip", d.action == "skip")

d = plan_recovery(failed_skill="browser", error_text="gateway_blocked marker seen", failed_node_id="n:5")
check("C.dec.upstream_replan", d.action == "replan" and d.failure_report is not None)

d = plan_recovery(failed_skill="computer", error_text="computer-use is disabled", failed_node_id="n:9")
check("C.dec.environmental_skip", d.action == "skip")

# ── flow Graph topology ──
from flow import Graph, MAX_NODES

g = Graph()
n1 = g.add_node("planner", inputs=["USER_QUERY"])
n2 = g.add_node("researcher", inputs=[n1])
n3 = g.add_node("formatter", inputs=[n2])
check("C.graph.ids", n1 == "n:1" and n2 == "n:2" and n3 == "n:3")
check("C.graph.ready_initial", g.ready_nodes() == [n1])
g.mark(n1, "complete")
check("C.graph.ready_after_planner", g.ready_nodes() == [n2])
g.mark(n2, "running")
check("C.graph.no_ready_while_running", n3 not in g.ready_nodes())
check("C.graph.has_running", g.has_running() is True)
g.mark(n2, "complete")
check("C.graph.ready_after_researcher", g.ready_nodes() == [n3])
g.mark(n3, "complete")
check("C.graph.done", g.ready_nodes() == [] and not g.has_running())

# skipped predecessor satisfies children (critic-fail path)
g2 = Graph()
a = g2.add_node("summariser", inputs=["USER_QUERY"])
b = g2.add_node("critic", inputs=[a])
c = g2.add_node("formatter", inputs=[b])
g2.mark(a, "complete")
g2.mark(b, "skipped")   # critic fail → child skipped
check("C.graph.skipped_satisfies", c in g2.ready_nodes())

# MAX_NODES constant sane
check("C.flow.max_nodes", MAX_NODES == 60)

print("\n=== SUITE C: recovery + graph ===")
print(f"pass={len(PASS)} fail={len(FAIL)}")
for name, err in FAIL:
    print(f"  FAIL {name}: {err}")
