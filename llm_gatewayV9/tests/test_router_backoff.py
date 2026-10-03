"""Backoff behavior for dead providers (offline except DB writes to tmp).

Covers: HTTP 402 (payment/quota) backs off hard instead of burning a
round trip on every pick, and a hard-failing router-LLM is backed off
in the pool instead of retried first on every classify.
"""
from __future__ import annotations

import asyncio
import sys
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

    r = Router({"groq": _Dead()}, ["groq"])
    secs, reason = M._backoff_for(P.ProviderError("pay up", status=402))
    r.state["groq"].mark_unavailable(secs, reason)
    name, attempts = r.pick(100, ["groq"])
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

    r = Router({"groq": _P(), "cerebras": _P()}, ["groq", "cerebras"])
    first, _ = r.pick(100, ["groq", "cerebras"])
    assert first == "groq"  # tie → candidate order
    r.state[first].record(0)
    r.state[first].last_call -= 10  # out of cooldown, but still 1 recent call
    second, _ = r.pick(100, ["groq", "cerebras"])
    assert second == "cerebras"  # load moved off the just-used provider
    r.state[second].record(0)
    r.state[second].last_call -= 10
    third, _ = r.pick(100, ["groq", "cerebras"])
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
            self.providers = {"groq": _Raiser()}
            self.state = defaultdict(RateState)

        def candidates(self):
            return ["groq"]

    pool = _Pool()
    req = ChatRequest(prompt="hello there", agent="t", session="s")
    decision = asyncio.run(M._classify_tier(req, "memory", pool, "hello"))
    assert decision.fallback_used is True  # pool exhausted → count rule
    snap = pool.state["groq"].snapshot(LIMITS["groq"])
    assert snap["backoff_remaining"] > 0  # dead router backed off
