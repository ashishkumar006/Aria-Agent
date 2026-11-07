"""Route affinity: one DAG node stays on one provider so its cache survives.

The gateway deliberately picks the LEAST-LOADED provider, which is right across
nodes and wrong within one. A node is not one call: the tool loop iterates, an
author retries on an empty reply, a researcher writes pages up in several calls.
Each of those hops re-picked by load, so the node could land on a different
provider every time — and caches are per provider. Gemini mints a `cachedContent`
under one API key and cannot read another's; the implicit prefix cache is per
provider too. A bouncing node re-pays its whole (large, tool-grant-laden) system
prompt on every hop and gets nothing back.

`route_affinity` is the caller saying "this hop belongs to the same conversation
as the last one". Affinity must never become an outage, so it is a preference
checked against the same usability guards, not a bypass.
"""
from __future__ import annotations

import time

import pytest

import router as R
from router import Router


class _P:
    model = "x"
    capabilities = {}


def _router(names=("kilo", "gemini35lite")):
    r = Router({n: _P() for n in names}, list(names))
    # Fresh the rate state so ordering is deterministic regardless of what
    # `limits_for` thinks the minute holds.
    for n in names:
        r.state[n].calls_minute.clear()
        r.state[n].last_call = 0.0
    return r


# ── pick() honours a preference without weakening any guard ─────────────────

def test_a_preferred_provider_wins_over_the_load_pick():
    r = _router()
    # Load says kilo was just used; affinity says stay on kilo.
    r.state["kilo"].record(0)
    r.state["kilo"].last_call -= 10
    name, _ = r.pick(100, ["kilo", "gemini35lite"], preferred="kilo")
    assert name == "kilo", (
        "the preference was ignored, so the node's next hop lands elsewhere "
        "and its provider cache is thrown away")


def test_without_a_preference_the_spread_behaviour_is_unchanged():
    """The existing fan-out property must survive this change untouched."""
    r = _router()
    first, _ = r.pick(100, ["kilo", "gemini35lite"])
    assert first == "kilo"
    r.state[first].record(0)
    r.state[first].last_call -= 10
    second, _ = r.pick(100, ["kilo", "gemini35lite"])
    assert second == "gemini35lite"


def test_affinity_never_overrides_a_capability_requirement():
    r = _router()
    name, attempts = r.pick(100, ["kilo", "gemini35lite"],
                            required_caps=["tools"], preferred="kilo")
    assert name != "kilo" or name is None
    if name is None:
        assert any("no_tools" in a["reason"] for a in attempts), attempts


def test_affinity_crosses_pacing_but_never_a_failure_backoff():
    """Two different gates share the word "cooldown"; only one is a problem.

    `limits["cooldown"]` is routine PACING — "don't hit this key twice within
    N seconds" — which exists to spread independent callers. A node's own next
    hop is not an independent caller: it is the same conversation, and its
    prompt cache is on that provider. Measured live before this distinction
    existed, a four-hop node landed on four different siblings because each hop
    found the previous provider 1-4s into pacing — the exact behaviour the whole
    feature was meant to remove.

    `unavailable_until` is the other gate, set only when a provider FAILED.
    Crossing that would pin a node onto a live outage, so it stays closed.
    """
    r = _router()
    limits = R.limits_for("kilo")
    r.state["kilo"].record(0)                     # pacing: just used
    r.state["kilo"].last_call = time.time()
    ok, why = r.state["kilo"].can_use(limits)
    assert not ok and "cooldown" in why, why      # a stranger is still spaced
    ok, _ = r.state["kilo"].can_use(limits, ignore_pacing=True)
    assert ok, "the node's own next hop should reach its cached provider"

    r.state["kilo"].mark_unavailable(60, "upstream 503")   # a real failure
    ok, why = r.state["kilo"].can_use(limits, ignore_pacing=True)
    assert not ok and "backoff" in why, (
        "pacing relief leaked onto the failure backoff — a wedged provider "
        "would keep the node down instead of failing over")
    assert why

    name, _ = r.pick(100, ["kilo", "gemini35lite"], preferred="kilo")
    assert name == "gemini35lite"


def test_affinity_still_respects_rpm_rpd_and_tpm():
    """Pacing relief must not become rate-limit relief."""
    r = _router()
    limits = R.limits_for("kilo")
    for _ in range(limits["rpm"]):
        r.state["kilo"].calls_minute.append(time.time())
    ok, why = r.state["kilo"].can_use(limits, ignore_pacing=True)
    assert not ok and "RPM" in why, why
    name, _ = r.pick(100, ["kilo", "gemini35lite"], preferred="kilo")
    assert name == "gemini35lite"


def test_a_four_hop_node_reaches_one_provider_despite_pacing():
    """The property, end to end, without resetting the rate state.

    The other route tests clear cooldown between requests to isolate affinity
    from the limiter. This one deliberately does NOT: consecutive hops land
    inside the pacing window on purpose, because that is what a real node does.
    """
    from starlette.testclient import TestClient
    import asyncio
    import main as gm
    from router import Router

    async def drive():
        async with gm.app.router.lifespan_context(gm.app):
            saved = (gm.app.state.router, gm.app.state.router_providers)
            a, b = _Ok("kilo"), _Ok("gemini35lite")
            r = Router({"kilo": a, "gemini35lite": b}, ["kilo", "gemini35lite"])
            gm.app.state.router = r
            try:
                c = TestClient(gm.app)
                out = [c.post("/v1/chat", json=_body("s8-hop:n:1")).json()["provider"]
                       for _ in range(4)]
                other = c.post("/v1/chat", json=_body("s8-hop:n:2")).json()["provider"]
                return out, other
            finally:
                gm.app.state.router, gm.app.state.router_providers = saved

    hops, second_node = asyncio.run(drive())
    assert len(set(hops)) == 1, f"one node bounced across {set(hops)}"
    # The other node was free to take the other provider rather than queue on
    # the first node's choice.
    assert second_node in {"kilo", "gemini35lite"}, second_node


def test_affinity_never_overrides_a_context_limit():
    r = _router()
    big = R.limits_for("kilo")["max_ctx"] + 1
    name, _ = r.pick(big, ["kilo", "gemini35lite"], preferred="kilo")
    assert name != "kilo"


def test_a_preference_outside_the_candidate_set_is_ignored():
    r = _router(names=("kilo",))
    name, _ = r.pick(100, ["kilo"], preferred="gemini35lite")
    assert name == "kilo"


# ── the affinity store itself ───────────────────────────────────────────────

def test_note_and_lookup_round_trip():
    r = _router()
    assert r.provider_for("s8-a:n:2") is None
    r.note_affinity("s8-a:n:2", "kilo")
    assert r.provider_for("s8-a:n:2") == "kilo"


def test_no_affinity_id_is_a_no_op():
    """A blank id must not create a shared "" bucket that every unaffinitised
    caller then pins to whichever provider last touched it."""
    r = _router()
    r.note_affinity(None, "kilo")
    r.note_affinity("", "gemini35lite")
    assert r.affinity == {}
    assert r.provider_for(None) is None
    assert r.provider_for("") is None


def test_an_affinity_cannot_name_a_provider_that_is_not_wired():
    """The id comes from the network. Recording an unknown provider must not
    make the next pick hand out a KeyError or an object that does not exist."""
    r = _router()
    r.note_affinity("s8-a:n:1", "not_a_provider")
    assert r.affinity == {}
    assert r.provider_for("s8-a:n:1") is None


def test_affinity_expires_so_a_finished_node_does_not_pin_forever(monkeypatch):
    r = _router()
    r.note_affinity("s8-a:n:2", "kilo")
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + R.AFFINITY_TTL_S + 1)
    try:
        assert r.provider_for("s8-a:n:2") is None
        assert "s8-a:n:2" not in r.affinity, (
            "the stale entry was left in the map instead of reaped")
    finally:
        monkeypatch.setattr(time, "time", real)


def test_the_store_is_bounded(monkeypatch):
    """`session:node` is unique per DAG node, so an unbounded dict is a slow
    memory leak in a long-lived gateway — one entry per node ever run."""
    r = _router()
    real = time.time
    for i in range(R.AFFINITY_MAX + 500):
        # Advance a hair so eviction has a strict order to work with.
        monkeypatch.setattr(time, "time", lambda base=real, k=i: base() + k * 1e-3)
        r.note_affinity(f"s8-x:n:{i}", "kilo")
    monkeypatch.setattr(time, "time", real)
    assert len(r.affinity) <= R.AFFINITY_MAX, len(r.affinity)
    # The oldest entries go, the newest — the nodes actually in flight — stay.
    assert r.provider_for(f"s8-x:n:{R.AFFINITY_MAX + 499}") == "kilo"
    assert r.provider_for("s8-x:n:0") is None


def test_two_routers_do_not_share_affinity():
    """Per-user pools are separate Router objects (user_pools.py). Affinity on
    one user's router must not pin another user's node to a provider chosen
    under the first user's keys."""
    a, b = _router(), _router()
    a.note_affinity("shared-id", "kilo")
    assert b.provider_for("shared-id") is None
    assert b.affinity == {}


# ── the request field ───────────────────────────────────────────────────────

def test_the_field_is_optional_and_absent_from_older_clients():
    """Every existing caller sends no `route_affinity`. An omitted field must
    mean "no preference", never a validation error on /v1/chat."""
    from schemas import ChatRequest

    plain = ChatRequest(messages=[{"role": "user", "content": "hi"}])
    assert plain.route_affinity is None
    assert "route_affinity" not in plain.model_dump(exclude_none=True)

    withaff = ChatRequest(messages=[{"role": "user", "content": "hi"}],
                          route_affinity="s8-a:n:2")
    assert withaff.route_affinity == "s8-a:n:2"


# ── the main.py wiring, end to end through /v1/chat ─────────────────────────
# Everything above tests `Router` in isolation. These drive the real route, so
# they fail if `preferred` is never passed, if affinity is recorded on a FAILED
# call, or if a provider that went into cooldown outranks failover.

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


def _clear_rate_state(r):
    """Undo the post-call cooldown before the next hop.

    Every successful call sets `unavailable_until` by that provider's LIMITS
    cooldown (kilo 1s, gemini35lite 4s), so a second request issued 5ms later
    would find every candidate in cooldown and 503. That behaviour is the rate
    limiter's, and it is covered by test_router_backoff and the RPM tests here;
    these tests are about which provider a node gets pinned to, so the limiter is
    held still rather than worked around in production code.
    """
    for name in r.order:
        st = r.state[name]
        st.unavailable_until = 0.0
        st.unavailable_reason = ""
        st.calls_minute.clear()
        st.tokens_minute.clear()
        st.last_call = 0.0


def _two_provider_app(monkeypatch):
    """Run the lifespan, then swap in two fake providers.

    Swapping rather than reusing the real pool keeps this offline and makes the
    assertion exact: with only two candidates, "which one answered" is not
    luck. The rate limits are the real ones for `kilo` (the widest), so no
    guard is weakened — the pick still has to pass `can_use`.
    """
    from starlette.testclient import TestClient
    import asyncio
    import main as gm
    from router import Router

    ctx = gm.app.router.lifespan_context(gm.app)

    async def drive(body_seq):
        async with ctx:
            saved = (gm.app.state.router, gm.app.state.router_providers)
            a, b = _Ok("kilo"), _Ok("gemini35lite")
            r = Router({"kilo": a, "gemini35lite": b}, ["kilo", "gemini35lite"])
            for n in r.order:
                r.state[n].calls_minute.clear()
                r.state[n].last_call = 0.0
            gm.app.state.router = r
            try:
                c = TestClient(gm.app)
                out = []
                for body in body_seq:
                    _clear_rate_state(r)
                    resp = c.post("/v1/chat", json=body)
                    out.append((resp.status_code,
                                (resp.json() or {}).get("provider"),
                                resp.text[:200]))
                return out, a, b, r
            finally:
                gm.app.state.router, gm.app.state.router_providers = saved

    return lambda body_seq: asyncio.run(drive(body_seq))


def _body(affinity=None, **over):
    b = {"prompt": "hi", "max_tokens": 5}
    if affinity:
        b["route_affinity"] = affinity
    b.update(over)
    return b


def test_the_second_hop_of_a_node_stays_on_the_firsts_provider(monkeypatch):
    """Without affinity, the load pick deliberately moves hop 2 to the other
    provider (see test_pick_spreads_load_across_providers). With it, it must not.
    """
    out, a, b, r = _two_provider_app(monkeypatch)(
        [_body("s8-n:n:2"), _body("s8-n:n:2")])
    assert [o[0] for o in out] == [200, 200], out
    first, second = out[0][1], out[1][1]
    assert first == second, (
        f"hop 1 answered on {first} and hop 2 on {second}; the node's cache is "
        "on {first} and it is now asking {second} to rebuild it")
    assert r.provider_for("s8-n:n:2") == first


def test_different_affinities_are_not_pinned_to_the_same_bucket(monkeypatch):
    """Distinct nodes get distinct, independent affinity records.

    Asserted on the map, not on which providers answered: `_clear_rate_state`
    zeroes both providers before every request, so the load pick sees a tie and
    deterministically takes candidate order. Whether distinct providers get used
    IS covered — by `test_pick_spreads_load_across_providers` in the existing
    suite, which drives the same `pick()` with real load deltas. What only an
    end-to-end route test can show is that six nodes did not collapse into one
    shared bucket, which is what would pin an 8-node authoring wave onto a
    single key.
    """
    out, a, b, r = _two_provider_app(monkeypatch)(
        [_body(f"s8-n:n:{i}") for i in range(6)])
    assert all(o[0] == 200 for o in out), out
    assert len(r.affinity) == 6, r.affinity
    assert set(r.affinity) == {f"s8-n:n:{i}" for i in range(6)}, r.affinity
    # One node moving must not drag the others with it.
    r.note_affinity("s8-n:n:0", "gemini35lite")
    assert r.provider_for("s8-n:n:1") == "kilo"
    assert r.provider_for("s8-n:n:0") == "gemini35lite"


def test_no_affinity_means_no_state_is_stored(monkeypatch):
    out, a, b, r = _two_provider_app(monkeypatch)([_body(), _body()])
    assert all(o[0] == 200 for o in out), out
    assert r.affinity == {}, r.affinity


def test_a_failed_hop_does_not_pin_the_node_to_the_broken_provider(monkeypatch):
    """Recording the attempt rather than the success would pin a node to the
    provider that just failed it."""
    class _Dead(_Ok):
        async def chat(self, *_a, **_k):
            self.calls += 1
            import providers as P
            raise P.ProviderError("upstream 503", status=503, retryable=True)

    from starlette.testclient import TestClient
    import asyncio
    import main as gm
    from router import Router

    async def drive():
        async with gm.app.router.lifespan_context(gm.app):
            saved = (gm.app.state.router, gm.app.state.router_providers)
            d, ok = _Dead("kilo"), _Ok("gemini35lite")
            r = Router({"kilo": d, "gemini35lite": ok}, ["kilo", "gemini35lite"])
            for n in r.order:
                r.state[n].calls_minute.clear()
                r.state[n].last_call = 0.0
            gm.app.state.router = r
            try:
                c = TestClient(gm.app)
                res = []
                for _ in range(3):
                    _clear_rate_state(r)
                    resp = c.post("/v1/chat", json=_body("s8-f:n:9"))
                    res.append((resp.status_code, (resp.json() or {}).get("provider")))
                return res, r
            finally:
                gm.app.state.router, gm.app.state.router_providers = saved

    res, r = asyncio.run(drive())
    assert [x[1] for x in res] == ["gemini35lite"] * len(res), res
    assert r.provider_for("s8-f:n:9") == "gemini35lite", (
        "the node is pinned to the provider that failed it")
