"""Tests for the orchestrator recovery pipeline (recovery.py).

These are deterministic (no LLM calls):
  * failure classification buckets
  * the plan_recovery decision table
  * handle_critic_verdict: splicing a recovery Planner on a critic `fail`,
    skipping the child, capping re-recovery of the same target.
"""
from __future__ import annotations

from flow import Graph
from recovery import classify_failure, plan_recovery, handle_critic_verdict
from schemas import AgentResult


def test_classify_empty_is_upstream_failure():
    assert classify_failure("") == "upstream_failure"


def test_classify_environmental_markers():
    for text in ("permission denied", "action requires your approval",
                 "no suitable target app", "screenshot capture failed"):
        assert classify_failure(text) == "environmental"


def test_classify_transient_markers():
    for text in ("502 Bad Gateway", "504 gateway timeout", "connectionerror",
                 "httpstatuserror"):
        assert classify_failure(text) == "transient"


def test_classify_validation_marker():
    assert classify_failure("ValidationError: bad node") == "validation_error"


def test_plan_recovery_validation_skips():
    d = plan_recovery(failed_skill="researcher",
                      error_text="validation error: malformed nodespec",
                      failed_node_id="n:1")
    assert d.action == "skip"
    assert d.reason == "validation_error"


def test_plan_recovery_transient_skips_even_planner():
    """CONTRACT (updated): transient failures SKIP — the gateway owns
    retries; the orchestrator must not re-plan on top of a 503. This holds
    even for the planner (a planner 503 is a provider problem, not a plan
    bug), and skipping is what unblocks downstream children."""
    d = plan_recovery(failed_skill="planner", error_text="503",
                      failed_node_id="n:1")
    assert d.action == "skip"
    assert d.reason == "transient"


def test_plan_recovery_upstream_other_replans():
    d = plan_recovery(failed_skill="browser", error_text="model drift",
                      failed_node_id="n:2")
    assert d.action == "replan"
    assert d.failure_report is not None


def test_handle_critic_verdict_pass_returns_false():
    g = Graph()
    nid = g.add_node("critic", inputs=["USER_QUERY"])
    g.g.nodes[nid]["metadata"] = {"target": "n:1", "child": "n:2"}
    result = AgentResult(success=True, output={"verdict": "pass"}, agent_name="critic")
    assert handle_critic_verdict(nid, result, g, {}, []) is False


def test_handle_critic_verdict_fail_splices_recovery():
    g = Graph()
    target = g.add_node("distiller", inputs=["USER_QUERY"])
    child = g.add_node("formatter", inputs=[target])
    critic = g.add_node("critic", inputs=["USER_QUERY", target])
    g.g.nodes[critic]["metadata"] = {"target": target, "child": child}
    recovered: dict[str, bool] = {}
    cap_hit: list[str] = []
    result = AgentResult(success=True, output={"verdict": "fail",
                                               "rationale": "wrong units"},
                         agent_name="critic")
    assert handle_critic_verdict(critic, result, g, recovered, cap_hit) is True
    # child removed from critical path
    assert g.g.nodes[child]["status"] == "skipped"
    # target marked as recovered
    assert recovered.get(target) is True
    # a recovery planner node was spliced in
    planner_nodes = [n for n, d in g.g.nodes(data=True) if d["skill"] == "planner"]
    assert planner_nodes, "expected a recovery planner node"
    rec = g.g.nodes[planner_nodes[0]]
    assert rec["metadata"].get("recovery_reason") == "critic_fail"
    assert cap_hit == []


def test_handle_critic_verdict_fail_caps_rerecovery():
    g = Graph()
    target = g.add_node("distiller", inputs=["USER_QUERY"])
    child = g.add_node("formatter", inputs=[target])
    critic = g.add_node("critic", inputs=["USER_QUERY", target])
    g.g.nodes[critic]["metadata"] = {"target": target, "child": child}
    recovered: dict[str, bool] = {}
    cap_hit: list[str] = []
    fail = AgentResult(success=True, output={"verdict": "fail",
                                             "rationale": "still wrong"},
                       agent_name="critic")
    handle_critic_verdict(critic, fail, g, recovered, cap_hit)
    # second critic-fail on the SAME target -> cap hit, no new planner
    before = sum(1 for _, d in g.g.nodes(data=True) if d["skill"] == "planner")
    handle_critic_verdict(critic, fail, g, recovered, cap_hit)
    after = sum(1 for _, d in g.g.nodes(data=True) if d["skill"] == "planner")
    assert after == before
    assert target in cap_hit
