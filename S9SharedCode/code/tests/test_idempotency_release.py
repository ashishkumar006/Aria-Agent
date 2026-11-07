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

KEYS = ["k-abort-at-started", "k-gateway-down", "k-midbody", "k-normal",
        "k-h5-lock"]


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
    """Fail fast instead of spending real money on every test here.

    The document scope goes with it: it is one live read of the registry on the
    run's critical path, and this file is about claims and registrations. Left
    unpatched, these tests pass or hang depending on whether a gateway happens
    to be listening on :8109.
    """
    monkeypatch.setattr(
        gateway, "ensure_gateway",
        lambda: (_ for _ in ()).throw(RuntimeError("gateway unreachable")))
    monkeypatch.setattr(agent_server, "_shell_intent_offline",
                        lambda q: None, raising=False)
    monkeypatch.setattr(agent_server, "_conversation_doc_ids",
                        lambda *a, **k: set())


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


def test_no_run_stays_registered_after_an_abort(monkeypatch):
    """The phantom-run half of the same bug: Stop listing runs that never
    existed, one per aborted stream, until a restart.

    The wait is the point, not the convenience. An abandoned STREAM is not a
    stopped RUN: `test_the_worker_starts_before_the_first_frame_and_survives_abort`
    pins that navigating away must not destroy the work, so the worker outlives
    the client and its cancel event stays registered until the worker settles.
    What must never happen is the leak — one event per closed tab, counting as
    a live run forever.

    Asserted on this run's own key rather than a snapshot of the global count,
    because the abandoned workers of the tests above this one are still
    settling in their own threads; a total taken at the start of the run is not
    a stable baseline in a single-process suite.
    """
    import time

    monkeypatch.setattr(agent_server, "_GATEWAY_WARMUP_THREAD", None)
    monkeypatch.setattr(agent_server, "_EMBEDDER_WARMUP_THREAD", None)
    conv = "ct-abort-registry"

    resp = agent_server._stream_run("something ordinary", conv,
                                    idem_key="k-abort-at-started")

    async def _abort_at_first():
        async for _frame in resp.body_iterator:
            break
        await resp.body_iterator.aclose()

    asyncio.run(_abort_at_first())

    def _registered() -> bool:
        with agent_server._ACTIVE_RUNS_LOCK:
            return bool(agent_server._ACTIVE_RUNS.get(conv))

    deadline = time.time() + 20
    while _registered() and time.time() < deadline:
        time.sleep(0.05)
    assert not _registered(), (
        "the abandoned run never left the cancel registry — every closed tab "
        "would leave a phantom live run behind")



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

def test_the_worker_starts_before_the_first_frame_and_survives_abort(monkeypatch):
    """C5: navigating away at `started` must not destroy the run.

    The thread used to start only after `started` (and after gateway warmup),
    so Starlette closing the generator at that first yield left no worker.
    """
    import threading
    import time

    started = threading.Event()
    release = threading.Event()

    monkeypatch.setattr(agent_server, "_GATEWAY_WARMUP_THREAD", None)
    monkeypatch.setattr(agent_server, "_EMBEDDER_WARMUP_THREAD", None)
    monkeypatch.setattr(gateway, "ensure_gateway", lambda *a, **k: True)
    monkeypatch.setattr(agent_server, "_session_cost_breakdown",
                        lambda sid: {})
    monkeypatch.setattr(agent_server, "_session_cost_delta",
                        lambda sid, before: {})
    monkeypatch.setattr(agent_server, "_record_turn_cost",
                        lambda *a, **k: None)

    def _run(*_a, **_k):
        started.set()
        release.wait(3)
        return "finished after the client left"

    monkeypatch.setattr(agent_server, "_run_orchestrator", _run)

    key = "k-worker-before-frame"
    agent_server._inflight_run_release(key)
    assert agent_server._inflight_run_claim(key, "ct-c5-worker") == ""
    resp = agent_server._stream_run("hello", "ct-c5-worker",
                                    idem_key=key)

    async def _abort_at_first():
        async for _frame in resp.body_iterator:
            break
        await resp.body_iterator.aclose()

    asyncio.run(_abort_at_first())
    assert started.wait(1), "the worker did not start before the first frame"
    # The worker owns the claim while it is still running.
    assert agent_server._inflight_run_lookup(key) == "ct-c5-worker"
    release.set()
    deadline = time.time() + 3
    while agent_server._inflight_run_lookup(key) and time.time() < deadline:
        time.sleep(0.02)
    assert agent_server._inflight_run_lookup(key) == ""


def test_a_completed_run_releases_its_session_turn_lock(monkeypatch):
    """H5: the per-session lock must not survive the run.

    Cost deltas are exact only because a session runs one turn at a time.
    A leaked lock would hang every later chat/research turn on that session.
    """
    monkeypatch.setattr(agent_server, "_GATEWAY_WARMUP_THREAD", None)
    monkeypatch.setattr(agent_server, "_EMBEDDER_WARMUP_THREAD", None)
    monkeypatch.setattr(gateway, "ensure_gateway", lambda *a, **k: True)
    monkeypatch.setattr(agent_server, "_session_cost_breakdown",
                        lambda sid: {})
    monkeypatch.setattr(agent_server, "_session_cost_delta",
                        lambda sid, before: {})
    monkeypatch.setattr(agent_server, "_record_turn_cost",
                        lambda *a, **k: None)
    monkeypatch.setattr(agent_server, "_run_orchestrator",
                        lambda *_a, **_k: "done")

    sid = "s8-h5-lock"
    monkeypatch.setattr(agent_server, "resolve_session",
                        lambda _cid: sid)
    resp = agent_server._stream_run("hello", "ct-h5-lock",
                                    idem_key="k-h5-lock")
    frames = _drain(resp)
    assert frames, "stream produced no frames"
    lock = agent_server._turn_lock(sid)
    assert lock.acquire(blocking=False), "turn lock leaked after the run"
    lock.release()
