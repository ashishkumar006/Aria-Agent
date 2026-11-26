"""Agent-routing is a SOFT preference: a 5xx on the preferred provider must
fail over to the ladder, not 502 the whole node.

Background: `agent_routing.yaml` maps `agent="<name>"` to a preferred provider.
The first cut treated that as a HARD pin, so when the preferred provider (e.g.
free-tier Gemini) returned a transient 502 the whole DAG node died with it —
even though a healthy sibling was wired and idle. The fix: the pin seeds the
candidate order (`preferred=`) but is dropped after the first attempt, so a
wedged provider cannot pin a node into an outage. A caller's EXPLICIT
`provider=` stays a hard binding (caller knows best).

These drive the real `/v1/chat` route with fake providers (offline), the same
way test_route_affinity does, so they fail if the soft-pin regresses to a hard
one or if failover stops happening.
"""
from __future__ import annotations

import asyncio

import pytest


class _Ok:
    def __init__(self, name):
        self.model = f"model-of-{name}"
        self.capabilities = {}
        self.name = name
        self.calls = 0

    async def chat(self, *_a, **_k):
        self.calls += 1
        return {"text": "ok", "tool_calls": [], "model": self.model,
                "input_tokens": 5, "output_tokens": 2,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0, "stop_reason": "end_turn",
                "reasoning": "", "reasoning_applied": False,
                "tool_call_dialect": "native"}


class _Dead(_Ok):
    async def chat(self, *_a, **_k):
        self.calls += 1
        import providers as P
        raise P.ProviderError("upstream 502", status=502, retryable=True)


def _clear_rate_state(r):
    for name in r.order:
        st = r.state[name]
        st.unavailable_until = 0.0
        st.unavailable_reason = ""
        st.calls_minute.clear()
        st.tokens_minute.clear()
        st.last_call = 0.0


def _drive(body_seq, *, left, right, agent_routing):
    """Run the real route against two fake providers.

    `left` / `right` are the provider names (left is the agent-routing target).
    `agent_routing` is patched onto main.AGENT_ROUTING for the duration.
    """
    from starlette.testclient import TestClient
    import main as gm
    from router import Router

    async def drive():
        async with gm.app.router.lifespan_context(gm.app):
            saved = (gm.app.state.router, gm.app.state.router_providers,
                     gm.AGENT_ROUTING)
            a = _Dead(left) if left == "kilo" and _drive.dead_left else _Ok(left)
            b = _Ok(right)
            r = Router({left: a, right: b}, [left, right])
            for n in r.order:
                r.state[n].calls_minute.clear()
                r.state[n].last_call = 0.0
            gm.app.state.router = r
            gm.AGENT_ROUTING = agent_routing
            try:
                c = TestClient(gm.app)
                out = []
                for body in body_seq:
                    _clear_rate_state(r)
                    resp = c.post("/v1/chat", json=body)
                    out.append((resp.status_code,
                                (resp.json() or {}).get("provider"),
                                resp.text[:200]))
                return out
            finally:
                (gm.app.state.router, gm.app.state.router_providers,
                 gm.AGENT_ROUTING) = saved

    return asyncio.run(drive())


# Default: left (kilo) healthy. Flip per-test for the failover case.
_drive.dead_left = False


def _body(**over):
    b = {"prompt": "hi", "max_tokens": 5}
    b.update(over)
    return b


# ── the 502 fix: a soft pin fails over ───────────────────────────────────────

def test_soft_pin_fails_over_when_the_preferred_provider_5xxs(monkeypatch):
    """THE regression: preferred provider is down, node must still answer.

    Before the fix this 502'd the whole node. Now the pin only seeds the order;
    the dead preferred provider is dropped after one attempt and the ladder
    answers.
    """
    _drive.dead_left = True
    try:
        out = _drive([_body(agent="writer")], left="kilo", right="gemini35lite",
                     agent_routing={"writer": "kilo"})
    finally:
        _drive.dead_left = False
    assert len(out) == 1
    status, provider, text = out[0]
    assert status == 200, f"soft pin 502'd instead of failing over: {text}"
    assert provider == "gemini35lite", (
        f"did not fail over off the dead preferred provider: {provider}")


# ── the pin is still honored when the preferred provider is healthy ──────────

def test_soft_pin_is_honored_when_the_preferred_provider_is_up(monkeypatch):
    _drive.dead_left = False
    out = _drive([_body(agent="writer")], left="kilo", right="gemini35lite",
                 agent_routing={"writer": "kilo"})
    status, provider, text = out[0]
    assert status == 200, text
    assert provider == "kilo", (
        f"the healthy preferred provider was skipped: {provider}")


# ── an explicit provider stays a HARD binding ────────────────────────────────

def test_explicit_provider_is_a_hard_pin_even_when_dead(monkeypatch):
    """A caller who pins provider= explicitly means it. A dead explicit
    provider must NOT silently fail over to a sibling — that would answer from
    a provider the caller forbade. It surfaces the error instead."""
    _drive.dead_left = True
    try:
        out = _drive([_body(provider="kilo", agent="writer")],
                     left="kilo", right="gemini35lite",
                     agent_routing={"writer": "kilo"})
    finally:
        _drive.dead_left = False
    status, provider, _text = out[0]
    assert status >= 500, f"explicit dead provider should surface an error: {out}"
    assert provider != "gemini35lite", (
        "an explicit provider= was silently failed over to a sibling")


# ── an agent with no pin behaves like an ordinary routed call ────────────────

def test_unpinned_agent_uses_the_normal_load_pick(monkeypatch):
    _drive.dead_left = False
    out = _drive([_body(agent="unknown-agent")], left="kilo",
                 right="gemini35lite", agent_routing={"writer": "kilo"})
    status, provider, text = out[0]
    assert status == 200, text
    assert provider in {"kilo", "gemini35lite"}


# ── a pin to an unwired provider is ignored, not a KeyError ──────────────────

def test_pin_to_an_unwired_provider_is_ignored(monkeypatch):
    """agent_routing names a provider that is not in this pool: the pin must be
    skipped (the `pinned in router.providers` guard), leaving the normal pick —
    never a 500 from a KeyError on the missing provider."""
    _drive.dead_left = False
    out = _drive([_body(agent="writer")], left="kilo", right="gemini35lite",
                 agent_routing={"writer": "not_wired"})
    status, provider, text = out[0]
    assert status == 200, text
    assert provider in {"kilo", "gemini35lite"}


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))