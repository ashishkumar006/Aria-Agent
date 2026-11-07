"""Stop has to reach INSIDE a node.

The Executor has always honoured the operator's Stop between dispatch batches.
That boundary is the one that never helps on the runs people actually press
Stop on: a long document is a SINGLE author node making one model call per
section (up to 160 of them), and a deep researcher is a single node making
thirty tool hops. Neither reaches a batch boundary until it is finished, so the
console said "stopped", the run kept billing for minutes, and it kept holding
its idempotency claim — which is what made re-asking the same question answer
"an identical run is already in progress" and point at a run that had already
been cancelled.

These tests pin the boundaries that close that gap, and the claim release that
makes a re-ask work.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import flow                                    # noqa: E402
import gateway                                 # noqa: E402
import mcp_runner as mr                        # noqa: E402
import skills                                  # noqa: E402


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """In-node stopping is the behaviour under test. The last test turns it
    off explicitly to prove the switch still means the old behaviour."""
    import flags as f
    monkeypatch.setattr(f, "is_enabled",
                        lambda name, default=False: (
                            True if name == "agent.stop_in_nodes" else default),
                        raising=False)


# ── the scope itself ────────────────────────────────────────────────────────

def test_no_cancel_is_requested_outside_a_node():
    assert gateway.cancel_requested() is False


def test_the_scope_reads_the_predicate_live_not_at_entry():
    """A bool snapshotted at node entry would always say "keep going" — the
    event is set minutes later, from another thread."""
    ev = threading.Event()
    with gateway.cancel_scope(ev.is_set):
        assert gateway.cancel_requested() is False
        ev.set()
        assert gateway.cancel_requested() is True
    ev.set()
    assert gateway.cancel_requested() is False, "the scope leaked its token"


def test_a_predicate_that_raises_never_cancels_a_node():
    def _boom() -> bool:
        raise RuntimeError("registry went away")
    with gateway.cancel_scope(_boom):
        assert gateway.cancel_requested() is False


def test_the_scope_travels_into_a_to_thread_call():
    """Section writers run their model calls through `asyncio.to_thread`, so a
    context that did not travel into the thread would stop nothing."""
    ev = threading.Event()
    ev.set()

    async def _go():
        with gateway.cancel_scope(ev.is_set):
            return await asyncio.to_thread(gateway.cancel_requested)

    assert asyncio.run(_go()) is True


# ── the tool loop ───────────────────────────────────────────────────────────

class _ToolChat:
    """A model that asks for one more tool on every turn, forever."""

    def __init__(self):
        self.n = 0

    async def __call__(self, messages):
        self.n += 1
        return {"text": "", "tool_calls": [
            {"id": f"t{self.n}", "name": "search_web",
             "arguments": {"q": f"query {self.n}"}}],
            "provider": "fake", "input_tokens": 10, "output_tokens": 5}


def test_the_loop_stops_at_its_next_hop_instead_of_running_to_the_cap():
    """Without the hop check this runs all 40 hops; that is the whole bug."""
    chat = _ToolChat()
    dispatched = []

    async def _dispatch(name, args):
        dispatched.append(args)
        return json.dumps({"ok": True, "result": "a page"})

    ev = threading.Event()

    def _flip_after_two():
        if chat.n >= 2:
            ev.set()
        return ev.is_set()

    with gateway.cancel_scope(_flip_after_two):
        out = asyncio.run(mr.run_tool_loop(
            messages=[{"role": "user", "content": "research this"}],
            chat_fn=chat, dispatch_fn=_dispatch, max_hops=40))

    assert out.get("cancelled") is True
    assert out.get("error") == "stopped by the operator"
    assert chat.n == 2, f"kept hopping after Stop: {chat.n} calls"
    # One hop was in flight when Stop landed, so its tools still ran; the hop
    # Stop interrupted did not.
    assert len(dispatched) == 1


def test_a_stopped_loop_does_not_dispatch_the_tools_it_was_asked_for():
    """A tool call is a file write, a render, or a post to someone's API. None
    of those belong on a run the operator has already called off."""
    ev = threading.Event()
    ev.set()
    dispatched = []

    async def _dispatch(name, args):
        dispatched.append(name)
        return "{}"

    with gateway.cancel_scope(ev.is_set):
        out = asyncio.run(mr.run_tool_loop(
            messages=[{"role": "user", "content": "do the thing"}],
            chat_fn=_ToolChat(), dispatch_fn=_dispatch, max_hops=5))

    assert dispatched == [], "tools were fired after Stop"
    assert out.get("cancelled") is True


def test_a_stopped_loop_leaves_no_unanswered_tool_call_behind():
    """The chat path passes the SAME `messages` list back on the next turn. An
    assistant tool-call turn with no matching tool result is a malformed
    request, so the stop must land before that turn is written."""
    ev = threading.Event()
    ev.set()
    msgs = [{"role": "user", "content": "do the thing"}]

    async def _dispatch(name, args):
        return "{}"

    with gateway.cancel_scope(ev.is_set):
        asyncio.run(mr.run_tool_loop(messages=msgs, chat_fn=_ToolChat(),
                                     dispatch_fn=_dispatch, max_hops=5))

    assert not [m for m in msgs if m.get("tool_calls")], msgs


def test_a_run_that_was_never_stopped_is_untouched():
    chat = _ToolChat()

    async def _dispatch(name, args):
        return json.dumps({"ok": True})

    out = asyncio.run(mr.run_tool_loop(
        messages=[{"role": "user", "content": "go"}], chat_fn=chat,
        dispatch_fn=_dispatch, max_hops=3))
    assert out.get("cancelled") is None
    assert chat.n == 3


def test_the_switch_restores_the_batch_boundary_only_behaviour(monkeypatch):
    """Off must mean off, with no residue: the node is never even handed a
    predicate, so nothing inside it can see a Stop."""
    import networkx as nx
    from schemas import AgentResult

    monkeypatch.setattr(flow, "_stop_in_nodes", lambda: False)
    seen: dict[str, object] = {}

    async def _fake_run_skill(skill, nid, nodes, sid, query, fr, **kw):
        seen["inside"] = gateway.cancel_requested()
        return (AgentResult(success=True, agent_name=skill.name,
                            output={"final_answer": "x"}), "prompt")

    g = nx.DiGraph()
    g.add_node("n:1", skill="formatter", status="running", inputs=[],
               metadata={}, outputs=[])
    graph = SimpleNamespace(g=g)
    ev = threading.Event()
    ev.set()   # already stopped when the node starts
    store = mock.MagicMock()

    with mock.patch.object(flow, "run_skill", _fake_run_skill):
        asyncio.run(flow.Executor()._run_one(
            "n:1", graph, "s8-switch", "hi", store, [], None, None, "", None,
            ev.is_set))

    assert seen["inside"] is False, (
        "the flag is off but the node still carried a cancel predicate")

    # …and with it ON, the same node does see it, which is the other half of
    # the switch meaning something.
    monkeypatch.setattr(flow, "_stop_in_nodes", lambda: True)
    seen.clear()
    g.add_node("n:2", skill="formatter", status="running", inputs=[],
               metadata={}, outputs=[])
    with mock.patch.object(flow, "run_skill", _fake_run_skill):
        asyncio.run(flow.Executor()._run_one(
            "n:2", graph, "s8-switch", "hi", store, [], None, None, "", None,
            ev.is_set))
    assert seen["inside"] is True


def test_a_node_with_no_predicate_sees_no_stop(monkeypatch):
    """Every non-DAG caller (plain chat, the code agent) runs with no
    predicate at all, and must never be cancelled by an unrelated run."""
    from schemas import AgentResult
    import networkx as nx

    seen: dict[str, object] = {}

    async def _fake_run_skill(skill, nid, nodes, sid, query, fr, **kw):
        seen["inside"] = gateway.cancel_requested()
        return (AgentResult(success=True, agent_name=skill.name,
                            output={"final_answer": "x"}), "prompt")

    g = nx.DiGraph()
    g.add_node("n:1", skill="formatter", status="running", inputs=[],
               metadata={}, outputs=[])
    with mock.patch.object(flow, "run_skill", _fake_run_skill):
        asyncio.run(flow.Executor()._run_one(
            "n:1", SimpleNamespace(g=g), "s8-none", "hi", mock.MagicMock(),
            []))
    assert seen["inside"] is False



# ── the sectioned author ────────────────────────────────────────────────────

class _SectionLLM:
    """One paid-for section per call, so a stopped run can be measured by how
    many calls it made after the Stop."""

    def __init__(self):
        self.n = 0

    def chat(self, prompt=None, **kw):
        self.n += 1
        return {"text": json.dumps({"blocks": [
            {"type": "heading", "level": 1, "text": f"Section {self.n}"},
            {"type": "paragraph", "text": "word " * 300}]})}


def _sections(ev, titles, calls_stop_at=None):
    llm = _SectionLLM()

    async def _go():
        def _watch():
            if calls_stop_at is not None and llm.n >= calls_stop_at:
                ev.set()
            return ev.is_set()

        with mock.patch.object(skills, "LLM", lambda: llm), \
             mock.patch.object(skills, "_acquire_figures", lambda *a, **k: ""), \
             gateway.cancel_scope(_watch):
            return await skills._sectioned_document_blocks(
                SimpleNamespace(name="author", provider_pin=None,
                                max_tokens=4000, temperature=0.3),
                "rendered prompt", titles, [], "write a long document", "s8-x")

    return asyncio.run(_go()), llm


def test_a_stopped_document_keeps_its_sections_and_starts_no_more():
    ev = threading.Event()
    titles = [f"Chapter {i}" for i in range(12)]
    blocks, llm = _sections(ev, titles, calls_stop_at=3)
    assert llm.n == 3, f"wrote {llm.n} sections after Stop"
    # What the user paid for survives: three headings, not nothing.
    heads = [b for b in blocks if str(b.get("type")) == "heading"]
    assert len(heads) == 3, heads


def test_an_unstopped_document_writes_every_section():
    blocks, llm = _sections(threading.Event(), ["A", "B", "C"])
    assert llm.n == 3
    assert len([b for b in blocks if str(b.get("type")) == "heading"]) == 3


# ── the graph: a stopped node is not a failure to recover from ──────────────

def test_a_node_cut_short_is_skipped_and_recovery_is_never_asked():
    """A node whose call was interrupted comes back as a failure, and pass 2
    would answer a failure by consulting the recovery planner — which plans
    more work for the part the operator just called off, and marks the run
    `degraded` for a stop nobody considers a defect.

    `plan_recovery` is rigged to raise, so this fails the moment the guard in
    front of it is removed rather than quietly passing on a `skip` decision."""
    from schemas import AgentResult, NodeSpec
    from persistence import SessionStore

    ev = threading.Event()

    async def _fake_run_skill(skill, nid, nodes, sid, query, fr, **kw):
        if skill.name == "planner":
            return (AgentResult(
                success=True, agent_name="planner",
                output={"nodes": [{"skill": "formatter",
                                   "inputs": ["USER_QUERY"],
                                   "metadata": {}}]},
                successors=[NodeSpec(skill="formatter",
                                     inputs=["USER_QUERY"])]), "prompt")
        ev.set()      # the Stop lands while this node is mid-call
        return (AgentResult(success=False, agent_name=skill.name,
                            error="stopped by the operator"), "prompt")

    def _no_recovery(**kw):
        raise AssertionError("recovery was consulted for a stopped node")

    sid = "t-stop-node"
    with mock.patch.object(flow, "ensure_gateway", lambda: None), \
         mock.patch.object(flow, "run_skill", _fake_run_skill), \
         mock.patch.object(flow, "plan_recovery", _no_recovery):
        answer = asyncio.run(flow.Executor().run(
            "hi", session_id=sid, should_cancel=ev.is_set))

    g = SessionStore(sid, create=False).read_graph()
    assert g is not None
    by_skill = {d["skill"]: d["status"] for _, d in g.nodes(data=True)}
    assert by_skill["planner"] == "complete"
    assert by_skill["formatter"] == "skipped", by_skill
    assert "failed" not in by_skill.values(), by_skill
    assert len(g.nodes) == 2, f"a stopped run grew new nodes: {list(g.nodes)}"
    assert answer.strip() == "", answer


# ── the claim, which is what actually blocked a re-ask ───────────────────────

def test_stop_releases_the_claim_so_the_same_question_can_be_asked_again():
    import agent_server as ag
    from fastapi.testclient import TestClient

    client = TestClient(ag.app)
    key, cid = "idem-stop-resubmit", "ct-stop-resubmit"
    ag._inflight_run_release(key)
    assert ag._inflight_run_claim(key, cid) == ""
    ev = ag.threading.Event()
    ag._register_run(ev, "s8-stop-resubmit", cid)
    try:
        # Still refused while the run is live — that is the claim's job.
        assert ag._inflight_run_claim("other-key", "ct-x") == ""
        ag._inflight_run_release("other-key")

        r = client.post("/api/chat/cancel", json={"conversation_id": cid})
        assert r.json()["status"] == "cancelled"
        assert r.json()["claims_released"] >= 1
        assert ag._inflight_run_lookup(key) == "", (
            "Stop left the claim held: the re-ask would be refused for the "
            "whole 600s TTL")
    finally:
        ag._unregister_run("s8-stop-resubmit", cid, cancel_ev=ev)
        ag._inflight_run_release(key)
        ag._inflight_run_release("other-key")


def test_a_stopped_run_cannot_release_a_newer_runs_claim():
    """The stopped run's worker releases its key on the way out. By then a
    re-ask may already hold it, and dropping THAT claim would let a third
    submit start a second paid run beside the live one."""
    import agent_server as ag
    key = "idem-owner"
    ag._inflight_run_release(key)
    assert ag._inflight_run_claim(key, "ct-old") == ""
    # A fresh submit takes the key after Stop released it.
    ag._inflight_run_release(key, owner="ct-old")
    assert ag._inflight_run_claim(key, "ct-new") == ""
    # The old run finishes and tries to release what it once owned.
    ag._inflight_run_release(key, owner="ct-old")
    assert ag._inflight_run_lookup(key) == "ct-new", (
        "the stopped run released the claim it no longer owned")
    ag._inflight_run_release(key, owner="ct-new")
    assert ag._inflight_run_lookup(key) == ""


def test_release_without_an_owner_still_clears_any_claim():
    import agent_server as ag
    key = "idem-any"
    ag._inflight_run_claim(key, "ct-holder")
    ag._inflight_run_release(key)
    assert ag._inflight_run_lookup(key) == ""


def test_a_released_claim_is_matched_by_the_owner_key_set():
    import agent_server as ag
    for k, owner in (("idem-o1", "ct-one"), ("idem-o2", "ct-two"),
                     ("idem-o3", "ct-three")):
        ag._inflight_run_release(k)
        ag._inflight_run_claim(k, owner)
    try:
        assert ag._inflight_run_release_for_owners("ct-one", "ct-three") == 2
        assert ag._inflight_run_lookup("idem-o2") == "ct-two"
        assert ag._inflight_run_lookup("idem-o1") == ""
    finally:
        for k in ("idem-o1", "idem-o2", "idem-o3"):
            ag._inflight_run_release(k)


def test_the_lightweight_turn_releases_its_claim_when_the_turn_ends():
    """`/api/chat/simple/stream` claimed a key and never gave it back: the row
    sat until the 600s TTL expired, so a finished — or stopped — research
    submit refused the same question for ten minutes after it had ended."""
    src = (CODE / "agent_server.py").read_text(encoding="utf-8")
    start = src.index("async def chat_simple_stream")
    end = src.index("\n@app.", start + 1)
    body = src[start:end]
    assert "_inflight_run_claim(" in body, "the path claims a key"
    assert "_inflight_run_release(" in body, (
        "and must release it, or every submit blocks its own topic")


# ── the flag is one switch, read in one place ───────────────────────────────

def test_the_kill_switch_is_defined_and_read_once():
    import flags as f
    d = f.DEFINITIONS.get("agent.stop_in_nodes")
    assert d and d["type"] == "bool" and d["default"] is True, (
        "a new behaviour ships with one named master switch")

    flow_src = (CODE / "flow.py").read_text(encoding="utf-8")
    gates = [ln for ln in flow_src.splitlines()
             if "_stop_in_nodes()" in ln and not ln.strip().startswith("def ")]
    assert len(gates) == 1, (
        "the predicate is published in exactly one place, so off is one edit")
    run_one = flow_src[flow_src.index("async def _run_one"):
                       flow_src.index("# ── CLI")]
    assert "_stop_in_nodes()" in run_one, (
        "the single gate must sit where the cancel scope is opened, not at a "
        "call site that could forget it")


def test_the_hop_check_precedes_the_time_ceiling():
    """Stop is the operator's decision; a ceiling is the harness's. The receipt
    for a stopped run must not read as a run that ran out of budget."""
    src = (CODE / "mcp_runner.py").read_text(encoding="utf-8")
    loop = src[src.index("async def run_tool_loop"):
               src.index("def _context_node_affinity")]
    assert loop.index("cancel_requested()") < loop.index("max_seconds > 0"), (
        "the hop-boundary check must be the first thing the loop does")
