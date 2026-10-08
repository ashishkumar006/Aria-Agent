"""The gateway's own surface must reject unauthenticated callers.

Motivation, measured by black-box probing of the running service before this
change: every `/v1/*` route was reachable with **no credential at all**. A
single unauthenticated request did real damage -

    POST /v1/memory/sweep        -> 200, wiped 6 working-memory rows
    POST /v1/policy/reload       -> 200, mutated policy state
    POST /v1/control/deploy-test -> 200, ran live LLM calls (spend)
    GET  /v1/config/keys         -> 200, disclosed which secrets are configured
    GET  /v1/calls, /v1/spend    -> 200, full call log and spend by agent

Loopback is the outer control, but this instance was running on
`GATEWAY_HOST=0.0.0.0`, which removes it entirely.

These tests drive the real middleware via a client that is NOT given the token
by `conftest.py`, so the rejection paths are exercised rather than assumed.
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gateway_auth as auth  # noqa: E402
import main as M  # noqa: E402

TOKEN = "test-gateway-token"

# Routes a black-box probe confirmed were reachable and harmful. Each must now
# refuse an unauthenticated caller.
PROTECTED = [
    ("POST", "/v1/memory/sweep", {}),
    ("POST", "/v1/policy/reload", {}),
    ("POST", "/v1/control/deploy-test", {}),
    ("GET", "/v1/config/keys", None),
    ("GET", "/v1/calls", None),
    ("GET", "/v1/spend", None),
    ("GET", "/v1/documents", None),
    ("POST", "/v1/documents/search", {"query": "x"}),
    ("DELETE", "/v1/memory", None),
    ("GET", "/v1/memory", None),
]


def _client(headers=None):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=M.app),
                             base_url="http://test",
                             headers=headers or {})


@asynccontextmanager
async def _live_client(headers=None):
    """Inside the app lifespan, so routes that build real services (the memory
    plane, the embedder) work. Several of the protected routes need it."""
    async with M.app.router.lifespan_context(M.app):
        async with _client(headers) as c:
            yield c


@asynccontextmanager
async def _anon_client():
    """A client that genuinely sends no token.

    `conftest.py` injects the token as a per-instance httpx default so the other
    ~480 tests keep working. Popping it off the instance is enough to be
    anonymous, and avoids unwinding a process-wide constructor patch.
    """
    async with M.app.router.lifespan_context(M.app):
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=M.app),
                base_url="http://test") as c:
            c.headers.pop("X-Gateway-Token", None)
            yield c


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", PROTECTED)
async def test_unauthenticated_call_is_refused(method, path, body):
    async with _anon_client() as c:
        r = await c.request(method, path, json=body)
    assert r.status_code == 401, (method, path, r.status_code, r.text[:120])
    assert "token" in r.text.lower(), r.text[:120]


@pytest.mark.asyncio
async def test_a_wrong_token_is_refused():
    async with _anon_client() as c:
        c.headers["X-Gateway-Token"] = "not-the-token"
        r = await c.get("/v1/documents")
    assert r.status_code == 401, r.status_code


@pytest.mark.asyncio
async def test_an_empty_token_is_refused():
    async with _anon_client() as c:
        c.headers["X-Gateway-Token"] = ""
        r = await c.get("/v1/documents")
    assert r.status_code == 401, r.status_code


@pytest.mark.asyncio
async def test_the_correct_token_is_accepted():
    async with _live_client({"X-Gateway-Token": TOKEN}) as c:
        r = await c.get("/v1/documents")
    assert r.status_code == 200, r.status_code


@pytest.mark.asyncio
async def test_the_401_advertises_how_to_authenticate():
    async with _anon_client() as c:
        r = await c.get("/v1/documents")
    assert r.headers.get("www-authenticate"), dict(r.headers)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/health"])
async def test_documentation_and_health_stay_open(path):
    """They describe the surface but change nothing, and leaving them open
    keeps the docs usable from a browser while the routes stay closed."""
    async with _client() as c:
        r = await c.get(path)
    assert r.status_code in (200, 307, 404), (path, r.status_code)


@pytest.mark.asyncio
async def test_static_assets_stay_open():
    async with _client() as c:
        r = await c.get("/static/help.html")
    assert r.status_code in (200, 404), r.status_code


# ── the bind guard ───────────────────────────────────────────────────────────
def test_loopback_is_always_allowed():
    for h in ("127.0.0.1", "localhost", "::1", None, ""):
        ok, _why = auth.assert_safe_bind(h)
        assert ok is True, h


def test_a_lan_bind_with_only_a_process_token_is_refused(monkeypatch):
    """This is the actual incident: 0.0.0.0 with a token nothing else knows."""
    monkeypatch.delenv("GATEWAY_V9_TOKEN", raising=False)
    ok, why = auth.assert_safe_bind("0.0.0.0")
    assert ok is False
    assert "GATEWAY_V9_TOKEN" in why


def test_a_lan_bind_with_a_configured_token_is_allowed(monkeypatch):
    monkeypatch.setenv("GATEWAY_V9_TOKEN", "a-real-secret")
    ok, _why = auth.assert_safe_bind("0.0.0.0")
    assert ok is True


# ── the token itself ─────────────────────────────────────────────────────────
def test_token_is_never_empty():
    assert auth.token()


def test_a_configured_token_wins(monkeypatch):
    monkeypatch.setenv("GATEWAY_V9_TOKEN", "from-env")
    monkeypatch.setattr(auth, "_token", None)
    assert auth.token() == "from-env"


def test_token_comparison_rejects_near_misses():
    t = auth.token()
    assert auth.check_header_value(t) is True
    assert auth.check_header_value(t + "x") is False
    assert auth.check_header_value(t[:-1]) is False
    assert auth.check_header_value("") is False
    assert auth.check_header_value(None) is False


def test_token_comparison_tolerates_surrounding_whitespace():
    assert auth.check_header_value(f"  {auth.token()}  ") is True


def test_enforcement_is_never_inert():
    """A missing token used to mean the guard was skipped, which is the failure
    mode where a security control silently does nothing."""
    assert auth.is_enabled() is True


def test_the_token_file_is_not_committed():
    """It is written into `state/`, which .gitignore excludes, and only at
    start-up - never at import. A committed token would hand every local
    process a gateway credential, so the LOCATION is what matters here, not
    whether the file happens to exist (it does whenever a gateway is running)."""
    gitignore = (ROOT.parent / ".gitignore").read_text(encoding="utf-8")
    assert "state/" in gitignore
    rel = auth.token_file().relative_to(ROOT).as_posix()
    assert rel.startswith("state/"), rel
    # And it must be one of the ignore patterns, not merely inside a directory
    # that happens to be listed.
    import subprocess
    out = subprocess.run(["git", "check-ignore", "-q", str(auth.token_file())],
                         cwd=str(ROOT.parent), capture_output=True)
    assert out.returncode == 0, (
        f"{auth.token_file()} is not git-ignored - a live gateway token would "
        f"be committable")