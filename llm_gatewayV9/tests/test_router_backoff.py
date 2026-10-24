"""Backoff behavior for dead providers (offline except DB writes to tmp).

Covers: HTTP 402 (payment/quota) backs off hard instead of burning a
round trip on every pick, and a hard-failing router-LLM is backed off
in the pool instead of retried first on every classify.
"""
from __future__ import annotations

import asyncio
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

import providers as P
from router import LIMITS, RateState


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    db.init()
    return db


def test_402_backs_off_hard():
    import main as M

    secs, reason = M._backoff_for(P.ProviderError("pay up", status=402))
    assert secs == 300
    assert "payment" in reason


def test_402_backoff_skips_provider_in_pick():
    import main as M
    from router import Router

    class _Dead:
        model = "x"
        capabilities = {}

    r = Router({"kilo": _Dead()}, ["kilo"])
    secs, reason = M._backoff_for(P.ProviderError("pay up", status=402))
    r.state["kilo"].mark_unavailable(secs, reason)
    name, attempts = r.pick(100, ["kilo"])
    assert name is None
    assert any("backoff" in a["reason"] for a in attempts)


def test_pick_spreads_load_across_providers():
    # No strict priority order: among usable providers pick the least
    # recently loaded, so concurrent calls fan out across providers in
    # parallel instead of queueing on provider #1 until it cools down.
    from router import Router

    class _P:
        model = "x"
        capabilities = {}

    r = Router({"kilo": _P(), "gemini35lite": _P()}, ["kilo", "gemini35lite"])
    first, _ = r.pick(100, ["kilo", "gemini35lite"])
    assert first == "kilo"  # tie → candidate order
    r.state[first].record(0)
    r.state[first].last_call -= 10  # out of cooldown, but still 1 recent call
    second, _ = r.pick(100, ["kilo", "gemini35lite"])
    assert second == "gemini35lite"  # load moved off the just-used provider
    r.state[second].record(0)
    r.state[second].last_call -= 10
    third, _ = r.pick(100, ["kilo", "gemini35lite"])
    assert third == first  # both used once → earliest last_call wins


def test_router_exception_backs_off_pool_member(tmp_db):
    import main as M
    from schemas import ChatRequest

    class _Raiser:
        model = "dead-model"

        async def chat(self, **kw):
            raise P.ProviderError("boom", status=402)

    class _Pool:
        def __init__(self):
            self.providers = {"kilo": _Raiser()}
            self.state = defaultdict(RateState)
            self.parse_strikes = defaultdict(int)

        def candidates(self):
            return ["kilo"]

    pool = _Pool()
    req = ChatRequest(prompt="hello there", agent="t", session="s")
    # Big enough to still reach the router: small prompts are answered by
    # the deterministic rule without asking a router at all.
    big = "word " * 800
    decision = asyncio.run(M._classify_tier(req, "memory", pool, big))
    assert decision.fallback_used is True  # pool exhausted → count rule
    snap = pool.state["kilo"].snapshot(LIMITS["kilo"])
    assert snap["backoff_remaining"] > 0  # dead router backed off


def test_a_small_prompt_never_asks_the_router(tmp_db):
    """The memory subsystem's prompts are tiny. Asking a router about them
    bought nothing - the reply was discarded and the token-count rule
    decided anyway - but it cost 1.5-4s per call (217 failed router calls
    in 24h, every one of them `router_memory` on a small prompt)."""
    import main as M
    from schemas import ChatRequest

    class _Never:
        model = "never-called"
        calls = 0

        async def chat(self, **kw):
            _Never.calls += 1
            return {"text": "TINY", "input_tokens": 1, "output_tokens": 1}

    class _Pool:
        def __init__(self):
            self.providers = {"kilo": _Never()}
            self.state = defaultdict(RateState)
            self.parse_strikes = defaultdict(int)

        def candidates(self):
            return ["kilo"]

    pool = _Pool()
    req = ChatRequest(prompt="small", agent=None, session=None)
    decision = asyncio.run(M._classify_tier(req, "memory", pool, "hello there"))
    assert _Never.calls == 0, "a small prompt must not spend a router call"
    assert decision.tier == "TINY"
    assert decision.fallback_used is True


def test_a_router_that_cannot_follow_the_instruction_is_benched(tmp_db):
    """Three mangled replies and the router is benched, so the next request
    is answered by the deterministic rule instead of paying for another
    useless call."""
    import main as M
    from schemas import ChatRequest

    class _Mangler:
        model = "kilo-auto/free"

        def __init__(self):
            self.calls = 0

        async def chat(self, **kw):
            self.calls += 1
            # What the live free router model actually answered.
            return {"text": "We need answer routing classifier",
                    "input_tokens": 300, "output_tokens": 8}

    class _AlwaysUsable:
        """A rate state that never rate-limits, so the test measures the
        strike counter and not the per-provider cooldown."""

        def __init__(self):
            self.unavailable_until = 0.0
            self.unavailable_reason = ""
            self.tokens_today = 0
            self.tokens_minute: list = []
            self.last_call = 0.0
            self.calls_minute: list = []

        def can_use(self, limits, est_tokens=0):
            if self.unavailable_until > time.time():
                return False, "backoff"
            return True, ""

        def record(self, tokens=0):
            pass

        def mark_unavailable(self, secs, reason=""):
            self.unavailable_until = time.time() + secs
            self.unavailable_reason = reason

        def snapshot(self, limits):
            return {"backoff_remaining": max(
                0, int(self.unavailable_until - time.time()))}

    class _Pool:
        def __init__(self):
            self.mangler = _Mangler()
            self.providers = {"kilo": self.mangler}
            self.state = {"kilo": _AlwaysUsable()}
            self.parse_strikes = defaultdict(int)

        def candidates(self):
            return ["kilo"]

    pool = _Pool()
    req = ChatRequest(prompt="big", agent=None, session=None)
    big = "word " * 800
    for _ in range(M.ROUTER_PARSE_STRIKES):
        asyncio.run(M._classify_tier(req, "memory", pool, big))
    assert pool.mangler.calls == M.ROUTER_PARSE_STRIKES
    assert pool.state["kilo"].snapshot(LIMITS["kilo"])["backoff_remaining"] > 0
    # Benched: the next classification does not call it again.
    asyncio.run(M._classify_tier(req, "memory", pool, big))
    assert pool.mangler.calls == M.ROUTER_PARSE_STRIKES


def test_an_echoed_tier_list_is_not_read_as_huge():
    """The free router model echoed the option list. Reading the first match
    in a fixed order answered HUGE, which was then clamped to LARGE - a
    small request routed to a big model."""
    import main as M

    assert M._parse_tier("TINY") == "TINY"
    assert M._parse_tier("The answer is LARGE.") == "LARGE"
    assert M._parse_tier("TINY, LARGE, or HUGE") is None
    assert M._parse_tier("We need answer routing classifier") is None
