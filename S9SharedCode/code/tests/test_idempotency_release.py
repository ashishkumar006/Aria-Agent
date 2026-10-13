"""An idempotency claim must not outlive the generator that holds it.

Structure: `_stream_run` wraps its real body in a `finally` that releases the
claim. The failure mode being pinned is a leak, and a leak is invisible to a
test that only checks the happy path - so these tests cover exits that are easy
to miss when reading the code (abort at the first frame, exception mid-body,
gateway down) and assert on the registry afterwards.

Why this matters concretely: the claim is what makes a second identical submit
refuse instead of starting a second paid run. A leaked key therefore does not
double-bill, it *blocks* the user - re-submitting the same topic answers
"an identical run is already in progress" and points at /runs, where no run
exists, for the full 600s TTL.
"""

from __future__ import annotations

import asyncio

import pytest

import agent_server
import gateway

KEYS = ["k-abort-at-started", "k-gateway-down", "k-midbody", "k-normal"]


@pytest.fixture(autouse=True)
def clean_registry():
    """The registry is module-global; do not leak claims between tests."""
    for k in KEYS:
        agent_server._inflight_run_release(k)
    yield
    for k in KEYS:
        agent_server._inflight_run_release(k)


@pytest.fixture(autouse=True)
def gateway_down(monkeypatch):
    """Fail fast instead of spending real money on every test here."""
    monkeypatch.setattr(
        gateway, "ensure_gateway",
        lambda: (_ for _ in ()).throw(RuntimeError("gateway unreachable")))
    monkeypatch.setattr(agent_server, "_shell_intent_offline",
                        lambda q: None, raising=False)


def _drain(resp):
    async def _go():
        return [f async for f in resp.body_iterator]

    return asyncio.run(_go())


def test_key_released_when_client_aborts_at_the_started_frame():
    """Stop pressed during gateway warmup, or the tab closing.

    The worker thread does not exist yet at this point, so the thread's own
    release never runs - only a scope-wide `finally` can cover it.
    """
    resp = agent_server._stream_run("hello", None, idem_key="k-abort-at-started")

    async def _abort_at_first():
        async for _frame in resp.body_iterator:
            break
        await resp.body_iterator.aclose()

    asyncio.run(_abort_at_first())

    assert agent_server._inflight_run_lookup("k-abort-at-started") == ""


def test_key_released_when_the_gateway_is_unavailable():
    resp = agent_server._stream_run("hello", None, idem_key="k-gateway-down")
    frames = _drain(resp)

    assert frames, "stream produced no frames"
    assert "gateway unavailable" in frames[-1], frames[-1]
    assert agent_server._inflight_run_lookup("k-gateway-down") == ""


def test_key_released_when_the_body_raises_mid_stream(monkeypatch):
    """An unexpected failure after the thread starts must not strand the key."""

    def _explode(*_a, **_k):
        raise RuntimeError("boom during drain")

    monkeypatch.setattr(agent_server, "_session_cost_breakdown", _explode)

    resp = agent_server._stream_run("hello", None, idem_key="k-midbody")
    try:
        _drain(resp)
    except RuntimeError:
        pass

    assert agent_server._inflight_run_lookup("k-midbody") == ""


def test_no_run_stays_registered_after_an_abort():
    """The phantom-run half of the same bug: Stop listing runs that never
    existed, one per aborted stream, until a restart."""
    before = agent_server._active_run_count()

    resp = agent_server._stream_run("something ordinary", None,
                                    idem_key="k-abort-at-started")

    async def _abort_at_first():
        async for _frame in resp.body_iterator:
            break
        await resp.body_iterator.aclose()

    asyncio.run(_abort_at_first())

    assert agent_server._active_run_count() == before


def test_a_completed_stream_also_releases_the_key():
    """The normal path must not regress into a double release that frees a
    claim another run legitimately holds."""
    resp = agent_server._stream_run("hello", None, idem_key="k-normal")
    frames = _drain(resp)
    assert "gateway unavailable" in frames[-1], frames[-1]
    assert agent_server._inflight_run_lookup("k-normal") == ""


def test_an_empty_idem_key_is_harmless():
    """Templated runs pass no key; the finally must tolerate that."""
    resp = agent_server._stream_run("hello", None, idem_key="")
    _drain(resp)   # must not raise
