"""Root conftest: the seams that make client pooling testable.

`adaptors/http.py` and `providers.py` now reuse one httpx client instead of
building a fresh one per call, because construction costs 230-590 ms on this
host and it was being paid on every provider call, every channel send and every
integration tool call.

That reuse is exactly what a monkeypatch of `httpx.Client` cannot see. Several
suites here patch httpx to record a request or to stub a transport; if a pooled
client was already built, the patch never runs and the test asserts against a
connection made by whichever test ran first. Order-dependent failures appeared
in `integrations/test_integrations.py` the moment pooling landed — green alone,
red only in a full run.

So every test starts with an empty pool. Production never calls these; the
clients are dropped, not closed, so no loop is required.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fresh_http_clients():
    try:
        from adaptors import http as _adaptor_http
        _adaptor_http.reset_pools()
    except Exception:
        pass
    try:
        import providers as _providers
        _providers.reset_upstream_pool()
    except Exception:
        pass
    yield
