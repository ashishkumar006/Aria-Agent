"""Pooled upstream clients: one per (loop, timeout), never one per call.

Building an `httpx.AsyncClient` costs 230-590 ms on this host (fresh SSL
context + connection pool). Every provider call used to build one, so every
gateway request paid roughly half a second of local overhead before sending a
byte — and the operator's `latency_ms` on a trivial completion read "over a
thousand ms" even when the model answered in 200 ms.

These tests exist because the first version of the pool keyed on
`timeout.timeout`, which `httpx.Timeout` does not have: every chat call came
back 502 with "'Timeout' object has no attribute 'timeout'". A float and a
Timeout must both work, and neither may be introspected.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import providers


@pytest.fixture(autouse=True)
def _clean_pool():
    providers._CLIENTS.clear()
    yield
    providers._CLIENTS.clear()


def test_a_float_timeout_and_an_httpx_timeout_both_work():
    """The regression. `httpx.Timeout` has no `.timeout` attribute."""
    async def main():
        a = providers.upstream_client(60.0)
        b = providers.upstream_client(httpx.Timeout(60.0, connect=10.0))
        assert a is not b, "two different budgets must not share one client"
        assert b.timeout.connect == 10.0
        await a.aclose()
        await b.aclose()
    asyncio.run(main())


def test_the_client_is_reused_within_one_loop():
    built = []
    real = httpx.AsyncClient

    def counting(*a, **k):
        built.append(1)
        return real(*a, **k)

    providers.httpx.AsyncClient = counting
    try:
        async def main():
            for _ in range(6):
                c = providers.upstream_session(providers._UPSTREAM_TIMEOUT)
                async with c as _s:
                    pass
        asyncio.run(main())
    finally:
        providers.httpx.AsyncClient = real
    assert len(built) == 1, f"built {len(built)} clients for 6 calls"


def test_upstream_session_does_not_close_the_shared_client():
    """`async with` on the real client closes it; exiting ours must not.

    If it did, the pooled instance would be handed to the next caller already
    closed and every second call in a run would fail.
    """
    async def main():
        async with providers.upstream_session() as c1:
            pass
        assert not c1.is_closed, "exiting the session closed the pooled client"
        async with providers.upstream_session() as c2:
            assert c2 is c1
    asyncio.run(main())


def test_a_pool_client_carries_no_headers():
    """Authorization for upstream providers lives per request, never on the
    shared client — the pool is reused across every provider key and every
    user, so a header set here would ride along on someone else's call."""
    async def main():
        c = providers.upstream_client()
        leaked = [k for k in c.headers
                  if k.lower() in ("authorization", "x-goog-api-key",
                                   "api-key", "cookie")]
        assert not leaked, leaked
        await c.aclose()
    asyncio.run(main())


def test_the_pool_is_scoped_to_the_running_loop():
    """A client rebound across loops raises 'Event loop is closed'."""
    async def grab():
        return providers.upstream_client(30.0)

    a = asyncio.run(grab())
    b = asyncio.run(grab())
    assert a is not b
    asyncio.run(a.aclose())
    asyncio.run(b.aclose())


def test_cache_mint_failure_does_not_take_the_pool_down_with_it():
    """`cache.GeminiCache` used to write `async with upstream_client(60.0)`.

    That is the pooled object, and `httpx.AsyncClient.__aexit__` closes it — so
    a single unreachable cache mint killed the connection pool for every
    provider call sharing the loop, and the `except Exception: return None, 0`
    around it meant the ledger showed nothing but `cache_read_tokens=0`, the
    same value a merely-small prompt produces. A dead port exercises all three
    legs at once: the client must still be open, and the reason must be
    reported rather than swallowed.
    """
    import cache

    async def main():
        shared = providers.upstream_client(60.0)
        cache.reset_failure_report()
        name, created = await cache.GeminiCache(ttl_seconds=300).get_or_create(
            "not-a-real-key", "gemini-3.5-flash-lite", "x" * 2000,
            "http://127.0.0.1:9")
        assert name is None and created == 0, "an unreachable host minted a cache"
        assert not shared.is_closed, (
            "the cache mint closed the shared pooled client")
        report = cache.failure_report()
        assert report, "a failed mint was reported nowhere — the silent-zero case"
        assert any(r["model"] == "gemini-3.5-flash-lite" for r in report), report
    asyncio.run(main())


def test_no_call_site_wraps_a_pooled_client_in_a_context_manager():
    """Structural guard for the mistake above, wherever it is repeated.

    `upstream_client()` hands out a shared object; `with`-ing it closes it.
    `upstream_session()` is the wrapper whose exit is deliberately a no-op, so
    the call sites keep their shape without taking the pool with them.
    """
    import pathlib
    import re

    root = pathlib.Path(providers.__file__).parent
    bad = []
    for py in sorted(root.rglob("*.py")):
        if ".venv" in py.parts or py.name == "test_client_pooling.py":
            continue
        text = py.read_text(encoding="utf-8", errors="replace")
        if re.search(r"\bwith\s+upstream_client\(", text):
            bad.append(py.name)
    assert not bad, f"{bad} close the shared client by entering it"
