"""Gateway proxy calls reuse one connection pool, and carry auth per request.

Measured on this host, constructing an `httpx.Client` / `httpx.AsyncClient`
costs 230-590 ms (fresh SSL context and pool every time) while a warm client
request takes 2-8 ms. Every agent->gateway call built its own, so the local tax
alone explained the numbers the operator kept seeing: `/api/documents/folders`
answered 14 bytes in 520 ms, and `/api/notify/channels` — sixteen sequential
blocking round trips, inside an `async def` — took 9.6 s (22 s under load) while
holding the event loop, which stalled every chat stream in the process.

These tests pin both halves of the fix, and the second one is a security
property, not a performance one: the shared client must hold NO headers, so a
caller's authorization can never outlive its own request.
"""
from __future__ import annotations

import asyncio


def test_the_same_client_is_reused_for_a_given_timeout():
    import gateway

    gateway.reset_client_pools()
    built = []
    real = gateway.httpx.Client
    gateway.httpx.Client = lambda *a, **k: built.append(1) or real(*a, **k)
    try:
        for _ in range(5):
            with gateway.sync_pool(8.0) as c:
                assert c is not None
    finally:
        gateway.httpx.Client = real
    assert len(built) == 1, f"built {len(built)} clients for 5 calls"


def test_a_different_timeout_gets_its_own_client():
    """The notify resolver's 8 s and an upload's 120 s must not share a budget."""
    import gateway

    gateway.reset_client_pools()
    with gateway.sync_pool(8.0) as fast, gateway.sync_pool(120.0) as slow:
        assert fast is not slow
        assert fast.timeout.read == 8.0
        assert slow.timeout.read == 120.0
    with gateway.sync_pool(8.0) as again:
        assert again is fast


def test_pooled_clients_carry_no_headers():
    """Security property. The pool is shared by every caller in the process.

    A token set on the client would be sent on requests made by a different
    user, a different session, and — once the agent serves several tenants — a
    different tenant. Authorization is per request or it is not authorization.
    """
    import gateway

    gateway.reset_client_pools()
    with gateway.sync_pool(30.0) as c:
        leaked = [k for k in c.headers
                  if k.lower() in ("x-gateway-token", "x-aria-user-token",
                                   "authorization", "cookie")]
        assert not leaked, f"authorization sits on a shared client: {leaked}"
        # httpx installs its own accept/user-agent defaults; those are the
        # library's, not a caller's, so they are allowed. Anything this module
        # adds would be shared.
        assert set(c.headers) <= {"accept", "accept-encoding", "connection",
                                  "user-agent"}, dict(c.headers)


def test_async_pool_is_scoped_to_the_running_loop():
    """A client bound to a closed loop raises, so loops must not share one."""
    import gateway

    gateway.reset_client_pools()

    async def grab():
        return gateway.async_client(30.0)

    first = asyncio.run(grab())
    second = asyncio.run(grab())
    assert first is not second, (
        "the client survived a loop swap; the second caller would raise "
        "'Event loop is closed'")


def test_a_dead_loop_lets_go_of_its_client():
    """The mechanism that makes the test above reliable rather than lucky.

    The pool used to be keyed on `id(loop)`. An id is a memory address, and
    CPython hands a finished loop's address to the next one, so two
    `asyncio.run()` calls could produce the same key and hand back a client
    bound to a dead loop — which fails at USE time with "Event loop is closed",
    far from the cause.

    So assert both halves of the weak-key contract: while the loop lives its
    entry is there and reused, and once it dies the entry is GONE. The second
    is what makes address reuse harmless.
    """
    import gc
    import asyncio

    import gateway
    gateway.reset_client_pools()

    inside = {}

    async def grab():
        inside["first"] = gateway.async_client(30.0)
        inside["again"] = gateway.async_client(30.0)
        assert len(gateway._ASYNC_POOL) == 1, (
            "the live loop should hold exactly one entry")
        assert inside["first"] is inside["again"], (
            "a live loop rebuilt its client, which is the cost pooling removes")

    asyncio.run(grab())
    gc.collect()

    assert len(gateway._ASYNC_POOL) == 0, (
        f"the finished loop kept {len(gateway._ASYNC_POOL)} pool entries — "
        "its address can now be recycled into a stale client")

    # And the next loop gets a fresh, working client rather than the dead one.
    async def grab_again():
        c = gateway.async_client(30.0)
        assert c is not inside["first"]
        assert not getattr(c, "is_closed", False)
        return c

    asyncio.run(grab_again())


def test_pool_keys_survive_repeated_loop_creation():
    """Many short-lived loops in a row — the shape that exposed the id reuse.

    Each must get its own client and none may be handed to a later loop, so
    every client collected here must still work on the loop it was built for.
    """
    import gateway
    import asyncio as _a

    gateway.reset_client_pools()
    seen = []

    async def grab():
        c = gateway.async_client(15.0)
        # Touch it on this loop: a client inherited from a dead loop raises here.
        assert not getattr(c, "is_closed", False)
        return c

    for _ in range(6):
        seen.append(_a.run(grab()))
    assert all(s is not t for i, s in enumerate(seen) for t in seen[i + 1:]), (
        "two different loops were handed the same client")


def test_async_client_is_reused_within_one_loop():
    import gateway

    gateway.reset_client_pools()
    built = []
    real = gateway.httpx.AsyncClient

    def counting(*a, **k):
        built.append(1)
        return real(*a, **k)

    gateway.httpx.AsyncClient = counting
    try:
        async def main():
            for _ in range(6):
                with gateway.async_session(45.0) as c:
                    assert c is not None
        asyncio.run(main())
    finally:
        gateway.httpx.AsyncClient = real
    assert len(built) == 1, f"built {len(built)} async clients for 6 calls"


def test_reset_client_pools_leaves_no_stale_entry(monkeypatch):
    """The test seam `tests/conftest.py` uses: suites that patch httpx must see
    a fresh construction, or they assert against a connection made earlier."""
    import gateway

    with gateway.sync_pool(9.5):
        pass
    assert gateway._SYNC_POOL
    gateway.reset_client_pools()
    assert gateway._SYNC_POOL == {}
    assert gateway._ASYNC_POOL == {}


def test_notify_and_gw_json_send_the_token_on_every_request(monkeypatch):
    """The pooling change had to move auth onto the request; this is the proof.

    Before, `_gw_auth_headers()` was passed once to the client constructor and
    every request inherited it. Now it is computed per call, which ALSO keeps
    the token-rotation self-heal working (the retry re-reads the file rather
    than reusing a stale client header).
    """
    import agent_server
    import gateway

    gateway.reset_client_pools()
    seen: list[dict] = []

    class _Resp:
        status_code = 200
        is_success = True
        content = b"{}"
        headers = {"content-type": "application/json"}

        def json(self):
            return {}

    class _Client:
        def __init__(self, *a, **k):
            self.constructor_headers = dict(k.get("headers") or {})

        async def get(self, url, **kw):
            seen.append(dict(kw.get("headers") or {}))
            return _Resp()

        async def post(self, url, **kw):
            seen.append(dict(kw.get("headers") or {}))
            return _Resp()

        async def request(self, method, url, **kw):
            seen.append(dict(kw.get("headers") or {}))
            return _Resp()

    real = gateway.httpx.AsyncClient
    gateway.httpx.AsyncClient = lambda *a, **k: _Client(*a, **k)
    monkeypatch.setattr(agent_server, "_gw_auth_headers",
                        lambda extra=None: {"X-Gateway-Token": "tok-1"})
    try:
        asyncio.run(agent_server._gw_json("/v1/things"))
    finally:
        gateway.httpx.AsyncClient = real
    assert len(seen) == 1, seen
    assert seen[0].get("X-Gateway-Token") == "tok-1", (
        "the token never reached the request — pooling silently dropped it")
