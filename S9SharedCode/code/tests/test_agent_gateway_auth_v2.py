"""The agent console's shell, and the gateway token the agent proxies with.

Two separate outages looked identical in the browser - an empty console - and
had two different causes:

1. The SPA fallback served `dist/index.html` raw. That file is byte-identical
   to the build, so it has no `aria-token` meta tag: every route WITHOUT its
   own `@app.get` handler (`/documents`, `/code`, any deep link, any refresh)
   loaded a console that could not authenticate, so each `/api` call 403'd and
   the page rendered nothing. Only the ~10 explicitly declared routes worked,
   which made it read as a broken Documents page rather than a broken shell.

2. The gateway started requiring `X-Gateway-Token` on `/v1/*`. The async
   proxies in `agent_server.py` built bare `httpx` clients, so every document
   list/search/upload, every TTS/STT call and the cost snapshot came back 401
   - while `/api/health` stayed green, because that probe is deliberately
   unauthenticated.

The shell tests use the app's own TestClient and `token_on` from
`test_auth_guard.py` (reused, not reimplemented: enforcement is process-global
and a leaked token 403s every later test). The gateway-proxy tests use
`httpx.MockTransport`, so no server on :8109 is ever contacted.

Two tests here were xfail against real holes these tests found; both are fixed
and the markers removed, so a regression now fails the suite loudly. The two
remaining xfails name the production bug that would fix them.
    """
from __future__ import annotations

import itertools
import re
import sys
from pathlib import Path

import httpx
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))          # for test_auth_guard.token_on
sys.path.insert(0, str(HERE.parent))   # for agent_server / auth / gateway

import agent_server  # noqa: E402
import auth  # noqa: E402
import gateway  # noqa: E402
from test_auth_guard import token_on  # noqa: E402

# Captured before any patching: `_mount` replaces `httpx.AsyncClient` with a
# factory, and a factory that calls the name it replaced recurses.
_RealAsyncClient = httpx.AsyncClient

SPA_ROUTES = ["/", "/ledger", "/documents", "/code",
              "/no/such/page", "/documents/42/detail"]
# Real `/api`-ish paths a broken client asks for. Note what is NOT here:
# `/api/documents/../../etc` - httpx normalises that to `/etc` before it leaves
# the client, so it would test the client's URL handling, not the server.
JSON_404_PATHS = ["/api/", "/api/nope", "/api/documents/search/extra",
                  "/api/a/b/c", "/v1/nope", "/assets/nope.js"]


def _client():
    """No lifespan: every route below reads a file or a constant, and the
    startup handlers reconcile state directories and start the scheduler."""
    from fastapi.testclient import TestClient
    return TestClient(agent_server.app)


def _token_meta(html: str) -> int:
    return html.count(agent_server.TOKEN_META_NAME)


# ── 7/8. every console route serves the token-carrying shell ────────────────

def test_the_raw_build_has_no_token_of_its_own():
    """The premise of every test below. If the built shell started shipping a
    meta tag, `contains meta` would pass for a file that was never injected
    and the fallback could go back to serving it raw unnoticed."""
    raw = agent_server._SPA_INDEX.read_text(encoding="utf-8")
    assert agent_server.TOKEN_META_NAME not in raw
    assert "</head>" in raw.lower(), "no head to inject into"


@pytest.mark.parametrize("path", SPA_ROUTES)
def test_every_console_route_carries_the_token(path):
    with token_on() as c:
        r = c.get(path)
        tok = auth.token()
    assert r.status_code == 200, (path, r.status_code)
    assert "text/html" in r.headers.get("content-type", ""), path
    assert _token_meta(r.text) == 1, (
        f"{path}: {_token_meta(r.text)} token metas in the served shell")
    assert f'content="{tok}"' in r.text, f"{path} did not get the live token"


def test_the_spa_fallback_serves_the_injected_shell_not_the_raw_file():
    """The regression itself, on a route with no handler of its own. The
    response must differ from `dist/index.html` in exactly the injected tag."""
    raw = agent_server._SPA_INDEX.read_text(encoding="utf-8")
    with token_on() as c:
        r = c.get("/documents")
        snippet = auth.token_meta_snippet()
    assert _token_meta(r.text) == 1
    assert r.text.replace(f"  {snippet}\n", "", 1) == raw, (
        "the served shell differs from the build by more than the token")


def test_a_declared_route_and_a_fallback_route_serve_the_same_shell():
    """`/ledger` has an `@app.get`; `/documents` does not. Both must serve the
    same shell, or the console is a different app depending on the URL."""
    with token_on() as c:
        declared, fallback = c.get("/ledger"), c.get("/documents")
    assert declared.status_code == fallback.status_code == 200
    assert declared.content == fallback.content


def test_the_injection_leaves_a_working_document():
    """The shell is the only thing the console has; mangling it is the same
    outage as not injecting at all. `#root` and the hashed asset references
    are what the bundle needs to boot."""
    raw = agent_server._SPA_INDEX.read_text(encoding="utf-8")
    with token_on() as c:
        html = c.get("/documents").text
    assert '<div id="root">' in html, html[:300]
    for ref in re.findall(r'(?:src|href)="(\./assets/[^"]+)"', raw):
        assert ref in html, f"injection lost the asset reference {ref}"
    # And the tag is a head child, not appended past </head>.
    assert html.index("<head") < html.index(agent_server.TOKEN_META_NAME)
    assert html.index(agent_server.TOKEN_META_NAME) < html.index("</head>")


def test_a_rotated_token_is_served_on_the_next_request():
    """The shell is cached on (mtime, size, meta). Drop the token from that key
    and every already-loaded tab keeps sending a token the gateway no longer
    accepts until it is hard-refreshed - while `/api/health` still says up."""
    with token_on() as c:
        first = c.get("/documents").text
        old = auth.token()
        new = auth.configure(force=True)
        assert new != old
        second = c.get("/documents").text
    assert f'content="{new}"' in second
    assert old not in second, "the cached shell still carries the old token"
    assert first != second


def test_a_missing_token_does_not_break_the_page():
    """No token configured means no meta and no enforcement - the non-launch
    path (a TestClient, an embedder). It must still serve the shell: serving
    the raw file would be a page that silently cannot call /api."""
    auth.disable()
    r = _client().get("/documents")
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert _token_meta(r.text) == 0, r.text[:200]
    assert '<div id="root">' in r.text


# ── 9. an unknown API path is JSON, never the shell ────────────────────────

@pytest.mark.parametrize("path", JSON_404_PATHS)
def test_an_unknown_api_path_answers_json_not_the_html_shell(path):
    """A broken client has to see an error it can parse. Handing it HTML - or
    a 200 - is how a dead endpoint reads as a working page."""
    auth.disable()                      # so the request reaches spa_fallback
    r = _client().get(path)
    assert r.status_code == 404, (path, r.status_code, r.text[:120])
    assert "application/json" in r.headers.get("content-type", ""), path
    assert "<" not in r.text[:1], path
    assert _token_meta(r.text) == 0, path


def test_an_unknown_api_path_is_still_json_with_enforcement_on():
    with token_on() as c:
        r = c.get("/api/nope")
    assert "application/json" in r.headers.get("content-type", "")
    assert _token_meta(r.text) == 0
    assert r.status_code in (403, 404), r.status_code


# ── 10. _gw_auth_headers never raises and never guesses ────────────────────

@pytest.fixture
def token_file(monkeypatch, tmp_path):
    """Where the real `gateway._gateway_token()` will look.

    It resolves the file from `__file__` (`parents[2] / llm_gatewayV9/state/
    gateway.token`), so pointing `gateway.__file__` at `tmp/a/b/gateway.py`
    moves that lookup to `tmp/llm_gatewayV9/state/gateway.token` - inside tmp,
    never next to the live gateway's real token.
    """
    p = tmp_path / "llm_gatewayV9" / "state" / "gateway.token"
    p.parent.mkdir(parents=True)
    monkeypatch.setattr(gateway, "__file__", str(tmp_path / "a" / "b" / "gateway.py"))
    monkeypatch.delenv("GATEWAY_V9_TOKEN", raising=False)
    return p


def test_the_gateway_token_is_read_from_the_token_file(token_file):
    token_file.write_text("token-from-the-file\n", encoding="utf-8")
    h = agent_server._gw_auth_headers()
    assert h.get("X-Gateway-Token") == "token-from-the-file", h


def test_an_empty_token_file_is_not_sent_as_an_empty_credential(token_file):
    """An empty header value is a credential that looks present. The gateway
    treats it as a wrong token; a client that omitted it would get the same
    401 for a different reason, and neither says why."""
    token_file.write_text("   \n", encoding="utf-8")
    assert agent_server._gw_auth_headers() == {}


def test_the_gateway_token_is_read_from_the_environment(monkeypatch, token_file):
    monkeypatch.setenv("GATEWAY_V9_TOKEN", "token-from-the-env")
    assert agent_server._gw_auth_headers() == {"X-Gateway-Token": "token-from-the-env"}
    # And the env wins over the file, which is the documented order: an
    # operator who configures both means the env one.
    token_file.write_text("token-from-the-file", encoding="utf-8")
    assert agent_server._gw_auth_headers()["X-Gateway-Token"] == "token-from-the-env"


def test_an_unreadable_token_file_yields_a_usable_empty_header_set(token_file):
    """Before the gateway has ever published one - or after it was rotated -
    the proxies must still be able to make the call, and must fail with the
    gateway's own 401 rather than a TypeError from a None header."""
    assert not token_file.exists()
    h = agent_server._gw_auth_headers()
    assert isinstance(h, dict), type(h)
    assert h == {}, h


def test_a_token_lookup_that_raises_is_swallowed(monkeypatch, token_file):
    """`gateway._gateway_token()` documents itself as never raising, so this
    is the belt to its braces: a broken gateway module must not turn every
    /api route into a 500."""
    def _boom():
        raise RuntimeError("gateway module is broken")
    monkeypatch.setattr(gateway, "_gateway_token", _boom)
    assert agent_server._gw_auth_headers() == {}


def test_a_missing_gateway_module_is_swallowed(monkeypatch):
    monkeypatch.setitem(sys.modules, "gateway", None)
    assert agent_server._gw_auth_headers() == {}


def test_caller_headers_survive_alongside_the_token(monkeypatch, token_file):
    token_file.write_text("tok-file", encoding="utf-8")
    h = agent_server._gw_auth_headers({"Content-Type": "text/csv"})
    assert h["Content-Type"] == "text/csv", h
    assert h["X-Gateway-Token"] == "tok-file", h


# ── 11. _gw_json: refresh once on 401, then tell the truth ──────────────────

class _Stub:
    """A scripted gateway, and a record of every request that reached it."""

    def __init__(self, responses):
        # One callable = the same reply to every request; a list = a script,
        # with the last entry repeated once it runs out.
        self._responses = [responses] if callable(responses) else list(responses)
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        nxt = self._responses[min(len(self.seen) - 1, len(self._responses) - 1)]
        if callable(nxt):
            return nxt(request)
        # Re-issue rather than replay: a Response is single-use once sent.
        return httpx.Response(nxt.status_code, content=nxt.content,
                              headers=dict(nxt.headers))

    @property
    def tokens(self) -> list:
        return [r.headers.get("X-Gateway-Token") for r in self.seen]

    @property
    def methods(self) -> list[str]:
        return [r.method for r in self.seen]

    @property
    def count(self) -> int:
        return len(self.seen)


def _mount(monkeypatch, stub: _Stub) -> _Stub:
    """Route the module's httpx.AsyncClient at a MockTransport.

    A real httpx client keeps real header semantics, so the retry's
    `c.headers.update(...)` is exercised for what it is - not faked by a stub
    client that ignores headers, which is precisely how the missing header in
    `_doc_context` stayed invisible to its own test.
    """
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **k: _RealAsyncClient(*a, transport=httpx.MockTransport(stub), **k))
    return stub


def _json(status, payload):
    return httpx.Response(status, json=payload)


def _refresh_to(*tokens):
    """`_gw_auth_headers` handing these tokens out in turn, then repeating.

    Cycling rather than iterating: an exhausted iterator raises StopIteration,
    which `_gw_json` swallows into an "unreachable" error, so a test that
    under-counted its own calls would pass for the wrong reason.
    """
    issued = itertools.cycle(tokens)

    def _headers(*_a, **_k):
        return {"X-Gateway-Token": next(issued)}
    return _headers


async def test_a_401_is_retried_once_with_a_refreshed_token(monkeypatch):
    """The gateway regenerates its token when its state file is missing or
    unreadable. An agent that has been up for hours holds a token that stopped
    being valid, and every proxy call then fails until the agent restarts."""
    stub = _mount(monkeypatch, _Stub([
        _json(401, {"error": "missing or invalid x-gateway-token header"}),
        _json(200, {"hits": [{"doc_id": "d1"}]}),
    ]))
    monkeypatch.setattr(agent_server, "_gw_auth_headers", _refresh_to("t1", "t2"))

    out = await agent_server._gw_json("/v1/documents/search", method="POST")

    assert out == {"hits": [{"doc_id": "d1"}]}, out
    assert stub.count == 2, [str(r.url) for r in stub.seen]
    assert stub.methods == ["POST", "POST"]
    assert stub.tokens == ["t1", "t2"], (
        "the retry replayed the stale token, so it cannot have been a refresh")


async def test_a_persistent_401_is_reported_as_401_and_retried_only_once(monkeypatch):
    stub = _mount(monkeypatch, _Stub([_json(401, {"error": "invalid token"})]))
    monkeypatch.setattr(agent_server, "_gw_auth_headers", _refresh_to("t1", "t2"))
    out = await agent_server._gw_json("/v1/documents")
    assert stub.count == 2, "expected exactly one retry, not a loop"
    assert out["status_code"] == 401, out
    assert "invalid token" in out["error"], out


@pytest.mark.parametrize("status", [400, 403, 404, 409, 500, 503])
async def test_a_non_401_failure_keeps_its_own_status_and_is_not_retried(
        monkeypatch, status):
    """`/v1/documents/{id}` deleting something already gone must reach the
    browser as 404, not as the 200 every proxied route used to return. And a
    non-401 must not cost a second upstream call."""
    stub = _mount(monkeypatch, _Stub([_json(status, {"error": f"upstream {status}"})]))
    monkeypatch.setattr(agent_server, "_gw_auth_headers", _refresh_to("t1"))
    out = await agent_server._gw_json("/v1/documents/missing", method="DELETE")
    assert stub.count == 1, [str(r.url) for r in stub.seen]
    assert out["status_code"] == status, out
    assert out["error"] == f"upstream {status}", out


async def test_a_non_json_error_body_still_yields_the_status(monkeypatch):
    stub = _mount(monkeypatch, _Stub([
        httpx.Response(502, text="<html>proxy error</html>")]))
    monkeypatch.setattr(agent_server, "_gw_auth_headers", _refresh_to("t1"))
    out = await agent_server._gw_json("/v1/documents")
    assert out["status_code"] == 502, out
    assert "proxy error" in out["error"], out


async def test_a_200_that_is_not_an_object_is_not_passed_on_as_success(monkeypatch):
    stub = _mount(monkeypatch, _Stub([_json(200, ["not", "an", "object"])]))
    monkeypatch.setattr(agent_server, "_gw_auth_headers", _refresh_to("t1"))
    out = await agent_server._gw_json("/v1/documents")
    assert out.get("error"), out


async def test_an_unreachable_gateway_is_reported_as_503_not_raised(monkeypatch):
    def _refuse(request):
        raise httpx.ConnectError("connection refused", request=request)
    _mount(monkeypatch, _Stub([_refuse]))
    monkeypatch.setattr(agent_server, "_gw_auth_headers", _refresh_to("t1"))
    out = await agent_server._gw_json("/v1/documents")
    assert out["status_code"] == 503, out
    assert "unreachable" in out["error"], out


async def test_every_proxy_call_carries_the_token(monkeypatch):
    """The whole point of `_gw_auth_headers`: whatever `_gw_json` is asked for,
    the upstream request presents the credential."""
    stub = _mount(monkeypatch, _Stub([_json(200, {"ok": True})]))
    monkeypatch.setattr(agent_server, "_gw_auth_headers", _refresh_to("t1"))
    for method, path in (("GET", "/v1/documents"), ("POST", "/v1/documents"),
                         ("DELETE", "/v1/documents/1"),
                         ("PATCH", "/v1/documents/1"),
                         ("POST", "/v1/memory/remember")):
        await agent_server._gw_json(path, method=method)
    assert stub.tokens == ["t1"] * 5, stub.tokens
    assert stub.methods == ["GET", "POST", "DELETE", "PATCH", "POST"]


async def test_a_delete_is_never_downgraded_to_a_get(monkeypatch):
    """`DELETE /v1/memory` became a read when the verb was dropped - and a read
    that reports 200 to a wipe button."""
    stub = _mount(monkeypatch, _Stub([_json(200, {"ok": True})]))
    monkeypatch.setattr(agent_server, "_gw_auth_headers", _refresh_to("t1"))
    await agent_server._gw_json("/v1/memory?confirm=wipe", method="DELETE")
    assert stub.methods == ["DELETE"], stub.seen


# ── the call sites, which is where this actually broke ─────────────────────

def test_no_gateway_call_site_builds_a_client_without_the_token():
    """Five call sites in `agent_server.py` talk to `:8109`. One of them builds
    a bare client, and its 401 is not an exception, so nothing surfaces it.
    A grep cannot be right about everything, but it can refuse to let a sixth
    call site be added unauthenticated - and it names the offender."""
    src = (HERE.parent / "agent_server.py").read_text(encoding="utf-8")
    offenders = []
    for m in re.finditer(r"(?:httpx|_hx)\.(?:AsyncClient|Client)\(", src):
        # Only the constructor's own arguments: a `headers=` on the request
        # that follows it does not authenticate the client.
        window = src[m.end(): m.end() + 300].split(") as ", 1)[0]
        if "_gw_auth_headers" not in window:
            offenders.append(src[:m.start()].count("\n") + 1)
    assert not offenders, (
        f"agent_server.py line(s) {offenders} build a client with no "
        f"_gw_auth_headers(); their calls to /v1/* will 401")


async def test_chat_document_context_search_sends_the_gateway_token(monkeypatch):
    seen: list[httpx.Request] = []

    def _handler(request):
        seen.append(request)
        return _json(200, {"hits": [
            {"doc_id": "d1", "chunk": "sourdough starter", "chunk_index": 0,
             "page": 1, "heading_path": [], "descriptor": "starter"}]})

    _mount(monkeypatch, _Stub(_handler))
    monkeypatch.setattr(agent_server, "_gw_auth_headers", _refresh_to("t1"))

    text, hits = await agent_server._doc_context("how do I feed the starter?", None)

    assert seen, "the search was never attempted"
    assert seen[0].headers.get("X-Gateway-Token") == "t1", (
        "the document search reached the gateway unauthenticated")
    assert hits == 1 and "sourdough starter" in text, (hits, text)