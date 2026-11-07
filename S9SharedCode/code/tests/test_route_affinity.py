"""Per-node provider affinity, and the removed authoring fan-out cap.

Two changes, one goal: stop re-paying a node's system prompt on every hop.

`gateway.node_scope("<session>:<node>")` binds a route-affinity id for the whole
node. Every gateway call made inside it — the tool loop's hops, an author's
empty-reply retry, a researcher's sectioned write-ups, the vision path — inherits
that id and the gateway keeps the node on one provider, where the cache lives.
It is a ContextVar rather than a parameter because `run_skill` reaches the
gateway from a dozen places, and threading one more argument through all of them
is how this file's sibling bug (a value read where it was never bound) happens.

The authoring wave cap at 2 concurrent nodes is removed. It existed because four
researchers landing on the SAME provider at once produced four empty replies.
Affinity makes that collision less likely, not more: a node now holds one
provider across its hops while DIFFERENT nodes are still spread by load across
the key pool, so eight concurrent researchers become eight separate rate
windows.
"""
from __future__ import annotations

import asyncio
import threading
from unittest import mock

import pytest

import flow
import gateway as G


# ── the context itself ──────────────────────────────────────────────────────

def test_no_affinity_is_bound_outside_a_node():
    assert G.current_node_affinity() is None


def test_node_scope_binds_and_always_unbinds():
    with G.node_scope("s8-a:n:2"):
        assert G.current_node_affinity() == "s8-a:n:2"
    assert G.current_node_affinity() is None, (
        "a leaked binding makes the next node inherit this node's provider "
        "history and get pinned somewhere it never chose")


def test_scopes_nest_and_restore_the_outer_binding():
    with G.node_scope("s8-a:n:1"):
        with G.node_scope("s8-a:n:2"):
            assert G.current_node_affinity() == "s8-a:n:2"
        assert G.current_node_affinity() == "s8-a:n:1"


def test_an_empty_affinity_is_a_no_op():
    """A node with no session or no id must not bind "" — every such node would
    then share one bucket and all be pinned to whichever provider ran first."""
    with G.node_scope(None):
        assert G.current_node_affinity() is None
    with G.node_scope(""):
        assert G.current_node_affinity() is None


def test_the_binding_reaches_a_worker_thread():
    """`mcp_runner._gateway_chat` runs the sync client on `asyncio.to_thread`.

    That is the whole reason this is a ContextVar and not a thread-local: the
    hops actually execute off-loop, so a binding that stopped at the thread
    boundary would silently do nothing on the path it exists for.
    """
    seen = {}

    async def main():
        with G.node_scope("s8-t:n:3"):
            seen["loop"] = G.current_node_affinity()
            return await asyncio.to_thread(G.current_node_affinity)

    in_thread = asyncio.run(main())
    assert seen["loop"] == "s8-t:n:3"
    assert in_thread == "s8-t:n:3", (
        "the affinity did not cross into to_thread, so tool-loop hops get no "
        "provider stickiness at all")


def test_concurrent_nodes_do_not_see_each_others_affinity():
    """Two nodes on one loop, interleaved — the same shape that leaked logs
    across runs, tested here before it can leak routing the same way."""
    order = ["s8-x:n:1", "s8-y:n:2"]
    observed = {}

    async def node(aff):
        with G.node_scope(aff):
            await asyncio.sleep(0.01)
            observed[aff] = G.current_node_affinity()
            await asyncio.sleep(0.01)
            observed[aff + ":after"] = G.current_node_affinity()

    async def main():
        await asyncio.gather(*(node(a) for a in order))

    asyncio.run(main())
    for aff in order:
        assert observed[aff] == aff, observed
        assert observed[aff + ":after"] == aff, observed


# ── the binding actually reaches the wire ────────────────────────────────────

class _RecordingRaw:
    """Stands in for the gateway client's LLM class, capturing chat bodies."""

    calls: list[dict] = []

    def __init__(self, *a, **kw):
        pass

    def chat(self, prompt=None, *, messages=None, system=None,
             agent=None, session=None, **kw):
        _RecordingRaw.calls.append(dict(kw, agent=agent, session=session))
        return {"text": "ok", "provider": "fake"}

    def vision(self, *a, **kw):
        return {"text": "ok", "provider": "fake"}


def _chat_kwargs(**kw):
    _RecordingRaw.calls = []
    with mock.patch.object(G, "_RawLLM", _RecordingRaw):
        G.LLM().chat(prompt="hi", agent="researcher", session="s8-a", **kw)
    return _RecordingRaw.calls[0]


def test_a_call_inside_a_node_sends_its_affinity():
    with G.node_scope("s8-a:n:2"):
        assert _chat_kwargs()["route_affinity"] == "s8-a:n:2"


def test_a_call_outside_a_node_sends_none():
    assert _chat_kwargs()["route_affinity"] is None


def test_an_explicit_affinity_wins_over_the_context():
    """A caller that names its own group is not overridden by an ambient one —
    the chat turn does this to keep a whole conversation on one provider."""
    with G.node_scope("s8-a:n:2"):
        got = _chat_kwargs(route_affinity="chat:thread-9")
    assert got["route_affinity"] == "chat:thread-9"


def test_every_hop_of_one_node_carries_the_same_affinity():
    """The property the whole change exists for, stated directly."""
    _RecordingRaw.calls = []
    with G.node_scope("s8-a:n:2"):
        with mock.patch.object(G, "_RawLLM", _RecordingRaw):
            for _ in range(6):          # the tool loop's hops
                G.LLM().chat(prompt="hi", agent="researcher", session="s8-a")
    affs = {c.get("route_affinity") for c in _RecordingRaw.calls}
    assert len(_RecordingRaw.calls) == 6
    assert affs == {"s8-a:n:2"}, (
        "hops of one node split across cache groups, which is the behaviour "
        "this exists to remove")


# ── the executor binds per node ─────────────────────────────────────────────

def test_the_executor_binds_session_and_node_for_each_node():
    """`flow` must actually open the scope, or all of the above is dead code."""
    import skills as S

    seen = []

    class _Spy(_RecordingRaw):
        def chat(self, prompt=None, *, messages=None, system=None,
                 agent=None, session=None, **kw):
            seen.append((agent, session, kw.get("route_affinity")))
            return {"text": "ok", "provider": "fake",
                    "input_tokens": 1, "output_tokens": 1}

    script_planner = {"nodes": [{"skill": "formatter", "inputs": ["USER_QUERY"],
                                 "metadata": {}}]}

    async def fake_run_skill(skill, nid, graph_nodes, sid, query, fr, **kw):
        # Mimic a real node: reach the gateway the way skills do.
        with mock.patch.object(G, "_RawLLM", _Spy):
            G.LLM().chat(prompt="hi", agent=skill.name, session=sid)
        from schemas import AgentResult
        return AgentResult(success=True, agent_name=skill.name,
                           output={"final_answer": "done"}), "prompt"

    calls = {"n": 0}

    def planner_chat(self, prompt=None, *, messages=None, system=None,
                     agent=None, session=None, **kw):
        calls["n"] += 1
        import json
        return {"text": json.dumps(script_planner if calls["n"] == 1
                                    else {"final_answer": "done"}),
                "provider": "fake"}

    with mock.patch.object(flow, "ensure_gateway", lambda: None), \
         mock.patch.object(S, "run_skill", fake_run_skill), \
         mock.patch.object(G, "_RawLLM", _Spy):
        asyncio.run(flow.Executor().run("make a thing",
                                        session_id="s8-scope-1"))

    affs = [a for (_ag, _s, a) in seen if a]
    assert affs, (
        "the executor never bound an affinity, so no node's hops share a "
        "provider")
    assert all(x.startswith("s8-scope-1:") for x in affs), affs


# ── the removed authoring cap ────────────────────────────────────────────────

def test_authoring_no_longer_defers_to_two_nodes():
    """An authoring wave runs as wide as any other run's.

    The cap was 2 because four researchers on the SAME provider produced four
    empty replies. Per-node affinity does not make that collision likelier — a
    node holds one provider across its hops while different nodes are still
    picked by load, so N concurrent nodes become N separate rate windows.
    """
    from schemas import AgentResult, NodeSpec

    running = {"now": 0, "peak": 0}
    n_nodes = 6

    async def fake_run_skill(skill, nid, graph_nodes, sid, query, fr, **kw):
        if skill.name == "planner":
            # The planner hands back the fan-out itself, so this test measures
            # the SCHEDULER's wave width and nothing else.
            return AgentResult(
                success=True, agent_name="planner",
                output={}, successors=[
                    NodeSpec(skill="author", inputs=["USER_QUERY"],
                             metadata={"label": f"s{i}"})
                    for i in range(n_nodes)]), "plan"
        running["now"] += 1
        running["peak"] = max(running["peak"], running["now"])
        await asyncio.sleep(0.02)
        running["now"] -= 1
        return AgentResult(success=True, agent_name=skill.name,
                           output={"final_answer": "sec"}), "p"

    with mock.patch.object(flow, "ensure_gateway", lambda: None), \
         mock.patch.object(flow, "run_skill", fake_run_skill):
        asyncio.run(flow.Executor().run(
            "write a long report", session_id="s8-wave-1",
            run_kind="authoring"))

    assert running["peak"] > 2, (
        f"authoring still serialises at {running['peak']} concurrent nodes; "
        "the removed cap was supposed to be gone")
    assert running["peak"] <= flow.MAX_CONCURRENCY, (
        "removing the authoring cap must not also remove the rate-limit bound")
    assert running["now"] == 0, "a node was left running at the end of the wave"


# ── what the tool loop asks the gateway for ─────────────────────────────────

class _ACM:
    """Minimal async context manager for the stdio session handshake.

    Both subclasses override `__aenter__`; `__aexit__` returning False means
    "did not swallow the exception", which is what the real one does too.
    """

    def __init__(self, *a, **kw):
        pass

    async def __aexit__(self, *exc):
        return False


class _Session(_ACM):
    async def __aenter__(self):
        return self

    async def initialize(self):
        return None


def _loop_kwargs(*, tools_payload, max_hops, session_id="conv-9",
                 route_affinity=None, in_node=None):
    """Run `run_with_tools` with the MCP child and the gateway both stubbed,
    and return the kwargs the first hop was sent with.

    The stdio session is faked rather than spawned: this test is about which
    affinity and cache flag the loop ASKS for, not about the tool transport.
    """
    import mcp_runner as MR

    sent = {}

    async def fake_loop(*, messages, chat_fn, dispatch_fn, **kw):
        await chat_fn(list(messages))
        return {"text": "done", "tool_calls": []}

    async def fake_chat(*, messages, tools, agent, session_id, provider_pin,
                        model=None, max_tokens, temperature,
                        route_affinity=None, cache_system=None):
        sent.update(route_affinity=route_affinity, cache_system=cache_system,
                    agent=agent, session=session_id)
        return {"text": "done", "tool_calls": []}

    async def go():
        ctx = G.node_scope(in_node) if in_node else None
        if ctx:
            ctx.__enter__()
        try:
            await MR.run_with_tools(
                prompt="hi", tools_payload=tools_payload, agent="chat",
                session_id=session_id, max_hops=max_hops,
                route_affinity=route_affinity)
        finally:
            if ctx:
                ctx.__exit__(None, None, None)

    class _Stdio(_ACM):
        async def __aenter__(self):
            return ("r", "w")

    with mock.patch.object(MR, "run_tool_loop", fake_loop), \
         mock.patch.object(MR, "_gateway_chat", fake_chat), \
         mock.patch.object(MR, "stdio_client", lambda *a, **k: _Stdio()), \
         mock.patch.object(MR, "ClientSession", lambda *a, **k: _Session()), \
         mock.patch.object(MR, "breaker_reset", lambda: None), \
         mock.patch.object(MR, "breaker_trip", lambda r: None):
        asyncio.run(go())
    return sent


def test_the_loop_groups_hops_by_conversation_when_not_in_a_node():
    """The chat page and the code agent never name an affinity. Deriving
    `agent:session` here means a conversation's turns and hops share one
    provider, which is where the growing-history prefix cache lives."""
    sent = _loop_kwargs(tools_payload=[{"name": "web_search"}], max_hops=0)
    assert sent["route_affinity"] == "chat:conv-9", sent


def test_a_bound_node_affinity_wins_over_the_derived_group():
    """Collapsing a run's nodes into `agent:session` would pin every node to
    one provider — the exact collision the authoring cap used to guard."""
    sent = _loop_kwargs(tools_payload=[{"name": "web_search"}], max_hops=0,
                        in_node="s8-a:n:4")
    assert sent["route_affinity"] == "s8-a:n:4", sent


def test_an_explicit_affinity_beats_both():
    sent = _loop_kwargs(tools_payload=[{"name": "x"}], max_hops=6,
                        route_affinity="explicit-1", in_node="s8-a:n:4")
    assert sent["route_affinity"] == "explicit-1", sent


def test_a_multi_hop_tool_loop_asks_for_explicit_caching():
    """Minting a cached system turn costs a call, so it is asked for only where
    the same prompt provably goes out again: a tool loop with more than one
    hop in play."""
    sent = _loop_kwargs(tools_payload=[{"name": "web_search"}], max_hops=6)
    assert sent["cache_system"] is True, sent


def test_a_single_hop_loop_does_not_pay_for_a_cache_it_wont_reuse():
    sent = _loop_kwargs(tools_payload=[{"name": "web_search"}], max_hops=1)
    assert sent["cache_system"] is False, sent


def test_a_call_with_no_tools_is_not_cached_explicitly():
    """No tools means no loop, which means one call and no reuse."""
    sent = _loop_kwargs(tools_payload=[], max_hops=6)
    assert sent["cache_system"] is False, sent


def test_the_streamed_loop_carries_the_same_affinity():
    """`stream=True` is a different call inside the same loop.

    The code agent always streams, and it has the longest, most repetitive
    prompt in the system — the single best cache case here. If affinity were
    wired only through `_gateway_chat`, that path would keep bouncing across
    providers and caching on none of them.
    """
    import mcp_runner as MR

    seen = {}

    class _StreamingLLM:
        def stream_chat(self, **kw):
            seen.update(kw)

            async def _gen():
                yield {"type": "text", "text": "ok"}
                yield {"type": "done", "provider": "fake"}
            return _gen()

    async def fake_loop(*, messages, chat_fn, **kw):
        # Drive the streaming chat fn the loop was given, if it was given one.
        stream_fn = kw.get("chat_stream_fn")
        if stream_fn is None:
            return {"text": "done", "tool_calls": []}
        await stream_fn(list(messages), on_text=lambda t: None)
        return {"text": "done", "tool_calls": []}

    class _Stdio:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return ("r", "w")

        async def __aexit__(self, *exc):
            return False

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            return None

    async def go():
        await MR.run_with_tools(
            prompt="hi", tools_payload=[{"name": "read_file"}], agent="code",
            session_id="code-main", max_hops=8, stream=True)

    with mock.patch.object(MR, "run_tool_loop", fake_loop), \
         mock.patch.object(MR, "LLM", lambda *a, **k: _StreamingLLM()), \
         mock.patch.object(MR, "stdio_client", lambda *a, **k: _Stdio()), \
         mock.patch.object(MR, "ClientSession", lambda *a, **k: _Session()), \
         mock.patch.object(MR, "breaker_reset", lambda: None), \
         mock.patch.object(MR, "breaker_trip", lambda r: None):
        asyncio.run(go())

    assert seen.get("route_affinity") == "code:code-main", seen
