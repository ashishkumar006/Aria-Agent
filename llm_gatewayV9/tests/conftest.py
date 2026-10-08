"""Test bootstrap for the gateway.

Auth
----
Every `/v1/*` route now requires `X-Gateway-Token`. That is the point of the
change - the gateway's own surface used to be reachable with no credential at
all, so an unauthenticated caller could wipe memory, reload policy and spend
real money - but it means each test client must present the token.

The header is injected centrally here rather than at ~40 call sites: the suite
builds its clients both with `TestClient(app)` and with
`httpx.ASGITransport(app=M.app)`, so patching the two constructors covers
everything. The dedicated auth tests in `test_gateway_auth.py` bypass this and
exercise the real behaviour, including the rejection paths.
"""
from __future__ import annotations

import os

TEST_TOKEN = "test-gateway-token"
os.environ["GATEWAY_V9_TOKEN"] = TEST_TOKEN

import httpx  # noqa: E402
import pytest  # noqa: E402
from starlette.testclient import TestClient as _TestClient  # noqa: E402

_HDR = "X-Gateway-Token"

# The auth tests must be able to build a client that deliberately does NOT send
# the token. Since the patching below is process-wide, they set this flag to
# suspend it; `_raw_client` restores the original constructor first.
_UNPATCHED: dict = {}
_SUSPEND = False


def _suspend() -> None:
    """Restore the real httpx/TestClient constructors."""
    global _SUSPEND
    _SUSPEND = True
    _TestClient.__init__ = _orig_tc_init
    httpx.AsyncClient.__init__ = _orig_ac_init
    httpx.Client.__init__ = _orig_c_init


def _resume() -> None:
    global _SUSPEND
    _SUSPEND = False
    _TestClient.__init__ = _tc_init
    httpx.AsyncClient.__init__ = _ac_init
    httpx.Client.__init__ = _c_init


# starlette's TestClient
_orig_tc_init = _TestClient.__init__


def _tc_init(self, *args, **kwargs):
    headers = dict(kwargs.pop("headers", None) or {})
    headers.setdefault(_HDR, TEST_TOKEN)
    kwargs["headers"] = headers
    _orig_tc_init(self, *args, **kwargs)


_TestClient.__init__ = _tc_init

# httpx.AsyncClient / httpx.Client built directly against an ASGI transport
_orig_ac_init = httpx.AsyncClient.__init__


def _ac_init(self, *args, **kwargs):
    headers = dict(kwargs.pop("headers", None) or {})
    headers.setdefault(_HDR, TEST_TOKEN)
    kwargs["headers"] = headers
    _orig_ac_init(self, *args, **kwargs)


httpx.AsyncClient.__init__ = _ac_init

_orig_c_init = httpx.Client.__init__


def _c_init(self, *args, **kwargs):
    headers = dict(kwargs.pop("headers", None) or {})
    headers.setdefault(_HDR, TEST_TOKEN)
    kwargs["headers"] = headers
    _orig_c_init(self, *args, **kwargs)


httpx.Client.__init__ = _c_init


@pytest.fixture(autouse=True)
def _gateway_auth_env(monkeypatch):
    """Keep the token stable even if a test mutates the environment."""
    monkeypatch.setenv("GATEWAY_V9_TOKEN", TEST_TOKEN)
    import gateway_auth

    gateway_auth._token = TEST_TOKEN
    _resume()
    yield
    _resume()