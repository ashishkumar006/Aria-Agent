"""The gateway's own console must stay reachable - and must stay *tokened*.

`/v1/*` was closed behind `X-Gateway-Token` (see `gateway_auth.py` and
`test_gateway_auth.py`, which pin the rejection paths). Closing it broke the
gateway's own UI in a way nobody could see from the API side: the console pages
live under `/static/*.html`, fetch `/v1/*` from inline scripts, and had no way
to present a credential - so every panel came back 401 and rendered empty.

The fix had two halves and each half has its own failure mode:

  * `/static/*` must be served WITHOUT a token. The middleware condition was
    inverted at one point (`not path.startswith("/static")` - a path had to be
    non-static to skip the check), which put the gateway's own CSS and JS
    behind auth. That reads in the browser as "unstyled panels full of failed
    requests", not as an auth bug, so it is pinned here asset by asset.

  * Every served HTML page must carry the token the loader reads, exactly once.
    A missing meta tag is silent: the page loads fine and then 401s on data.

Also pinned, because they are the same code path and just as exploitable:
non-HTML assets must pass through untouched (no token written into a .js/.css
file), the static route must not escape its directory, and `inject_into_html`
must be idempotent - it runs on every request, so a second injection would put
two meta tags in the page and two shims on `window.fetch`.

Every test here uses the app's own ASGI transport. No server is started and no
token is needed for the console routes, which is the point of them.

Two tests are marked xfail, each against a hole these tests found rather than
against a known-broken build; the reason on each says what would fix it. The two
   that were xfail are fixed and their markers removed.
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

HTML_PAGES = ["dashboard.html", "connections.html", "ledger.html",
              "system.html", "architecture.html", "help.html"]
NON_HTML = ["console-theme.css", "gateway.js", "console-token.js"]
ALL_ASSETS = HTML_PAGES + NON_HTML

# The escaping forms a real client can produce. Starlette hands the *raw* path
# segment to the route, so the handler has to reject each of these itself;
# `/static/../main.py` is additionally normalised away by the router, which is
# why most of these are asserted at the handler, not over HTTP.
ESCAPES = [
    "../main.py",
    "../../main.py",
    "..%2fmain.py",
    "%2e%2e/main.py",
    "%2e%2e%2fmain.py",
    "....//main.py",
    "..\\main.py",
    "subdir/../../main.py",
    "../gateway_auth.py",
    "..%2fgateway_auth.py",
]


@asynccontextmanager
async def _anon():
    """A client that genuinely sends no token.

    `conftest.py` injects the token as an httpx default so the other ~480 tests
    keep working; popping it off the instance is enough to be anonymous and
    avoids unwinding a process-wide constructor patch. Same approach as
    `test_gateway_auth.py`.
    """
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=M.app),
                                 base_url="http://test") as c:
        c.headers.pop("X-Gateway-Token", None)
        yield c


def _meta(tag: str) -> int:
    """Count *injected* meta tags: occurrences carrying the live token.

    Counting `name="gateway-token"` alone also counts a page that merely
    documents the meta in its prose, which is exactly the case the loader
    test below needs to be able to assert against.
    """
    return tag.count(f'name="{auth.META_NAME}" content="{auth.token()}"')


# ── 1. the console is not behind the credential it exists to serve ──────────

@pytest.mark.asyncio
@pytest.mark.parametrize("name", ALL_ASSETS)
async def test_every_console_asset_is_served_without_a_token(name):
    """The regression itself: the middleware demanded a token for its own
    static files, so the whole UI lost its CSS and its JS."""
    async with _anon() as c:
        r = await c.get(f"/static/{name}")
    assert r.status_code == 200, (name, r.status_code, r.text[:120])
    assert r.text, f"{name} served an empty body"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", HTML_PAGES)
async def test_a_console_page_still_renders_when_a_wrong_token_is_sent(name):
    """Static is skipped by the guard entirely, not merely exempted for the
    anonymous case, so a stale token left in a browser cannot blank the UI."""
    async with _anon() as c:
        c.headers["X-Gateway-Token"] = "stale-token-from-a-previous-launch"
        r = await c.get(f"/static/{name}")
    assert r.status_code == 200, (name, r.status_code)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/", "/help"])
async def test_the_html_routes_outside_static_are_served_too(path):
    """`/` and `/help` are the same console pages by another route. They use
    the same injector, so they need the same guarantee."""
    async with _anon() as c:
        r = await c.get(path)
    assert r.status_code == 200, (path, r.status_code)
    assert _meta(r.text) == 1, (path, r.text[:200])


# ── 2. and /v1/* is still behind it ─────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/documents", "/v1/providers", "/v1/status",
                                  "/v1/calls", "/v1/spend", "/v1/memory"])
async def test_v1_is_still_refused_with_no_token(path):
    """The point of the exemption is not "the gateway has no auth"."""
    async with _anon() as c:
        r = await c.get(path)
    assert r.status_code == 401, (path, r.status_code, r.text[:120])


@pytest.mark.asyncio
async def test_a_prefix_of_the_token_is_refused_on_v1():
    async with _anon() as c:
        c.headers["X-Gateway-Token"] = auth.token()[:6]
        r = await c.get("/v1/documents")
    assert r.status_code == 401, r.status_code


@pytest.mark.asyncio
async def test_the_guard_runs_before_the_router_so_unknown_v1_paths_401():
    """Any path under `/v1/` is closed, including ones that match no route. If
    the guard moved into route handling, a path a future route adds - or a
    typo'd one - would fall through unauthenticated."""
    async with _anon() as c:
        r = await c.get("/v1/no/such/route/at/all")
    assert r.status_code == 401, r.status_code


def test_no_open_path_can_shadow_a_v1_route():
    """`_OPEN_PATHS` is consulted before the `/v1/` prefix, so a member of it
    that happens to be a real `/v1/` route silently unprotects it. Cheap to
    assert, and impossible to notice by reading either list."""
    v1_routes = {r.path for r in M.app.routes if r.path.startswith("/v1")}
    assert v1_routes, "the /v1 route table is empty - the guard is untestable"
    assert not (v1_routes & M._OPEN_PATHS), (
        f"unprotected by _OPEN_PATHS: {sorted(v1_routes & M._OPEN_PATHS)}")


def test_no_route_lives_at_bare_v1():
    """The guard matches `/v1/`, with the slash. A route registered at exactly
    `/v1` sits outside the guard: it 404s today and would start answering in
    cleartext the moment it were implemented."""
    bare = [r.path for r in M.app.routes if r.path.rstrip("/") == "/v1"]
    assert not bare, f"a route sits outside the guard's '/v1/' prefix: {bare}"


# ── 3. every served page carries the token, once ────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("name", HTML_PAGES)
async def test_a_console_page_declares_the_token_exactly_once(name):
    async with _anon() as c:
        r = await c.get(f"/static/{name}")
    assert r.status_code == 200, name
    assert _meta(r.text) == 1, (
        f"{name}: {_meta(r.text)} gateway-token meta tags; the loader reads "
        f"the first, and a second one means double injection")
    assert r.text.count("/static/console-token.js") == 1, (
        f"{name}: the fetch shim is not referenced exactly once")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", HTML_PAGES)
async def test_the_injected_token_is_the_real_one(name):
    """Present-but-empty, or a copy from a previous launch, both render a page
    that loads and then 401s on every panel. The value has to be what the guard
    will actually compare against."""
    async with _anon() as c:
        r = await c.get(f"/static/{name}")
    assert f'content="{auth.token()}"' in r.text, (
        f"{name} does not carry the live token value")
    assert auth.check_header_value(auth.token()) is True


def test_the_pages_themselves_do_not_hardcode_the_snippet():
    """Injection is the server's job. A committed `name="gateway-token"` or
    `console-token.js` reference makes `inject_into_html` treat the file as
    already done and add nothing - which would silently disarm the page it was
    added to protect."""
    for name in HTML_PAGES:
        src = (M._STATIC_DIR / name).read_text(encoding="utf-8")
        assert f'name="{auth.META_NAME}"' not in src, (
            f"static/{name} already contains a gateway-token meta")
        assert "console-token.js" not in src, (
            f"static/{name} already references console-token.js")


# ── 4. non-HTML is passed through untouched ─────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("name", NON_HTML)
async def test_a_non_html_asset_is_byte_identical_to_the_file(name):
    async with _anon() as c:
        r = await c.get(f"/static/{name}")
    assert r.status_code == 200, (name, r.status_code)
    assert r.content == (M._STATIC_DIR / name).read_bytes(), (
        f"{name} was rewritten in transit")
    assert b"<meta" not in r.content, f"{name} had HTML injected into it"
    assert auth.token().encode() not in r.content, (
        f"{name} now carries the gateway token, and a static asset is served "
        f"to anyone who can reach the port with no credential at all")


@pytest.mark.asyncio
@pytest.mark.parametrize("name,ctype", [("console-theme.css", "text/css"),
                                        ("gateway.js", "javascript"),
                                        ("console-token.js", "javascript")])
async def test_a_non_html_asset_keeps_its_content_type(name, ctype):
    """A shim served as text/plain is ignored by the browser: the page would
    load, look styled, and still have no fetch wrapper."""
    async with _anon() as c:
        r = await c.get(f"/static/{name}")
    assert ctype in r.headers.get("content-type", "").lower(), (
        name, r.headers.get("content-type"))


# ── 5. the static route cannot be walked out of ─────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("name", ESCAPES)
async def test_traversal_over_http_leaks_nothing(name):
    async with _anon() as c:
        r = await c.get(f"/static/{name}")
    assert r.status_code in (400, 403, 404), (name, r.status_code)
    assert "FastAPI(" not in r.text, (name, r.text[:200])
    assert "hmac" not in r.text, (name, r.text[:200])


@pytest.mark.parametrize("name", ESCAPES)
def test_traversal_is_refused_at_the_handler(name):
    """Asserted on the handler because most of these never reach it: httpx and
    Starlette normalise `..` in the request path, so the route 404s before
    `_console_asset` is called. This is the only place the guard itself is
    exercised, so it is also the only place a rewritten guard would show."""
    r = M._console_asset(name)
    assert getattr(r, "status_code", None) == 404, (name, type(r).__name__)
    assert b"FastAPI(" not in getattr(r, "body", b""), name


def test_a_directory_is_not_served_as_a_file(tmp_path, monkeypatch):
    """`name` may name a directory. Reading one raises IsADirectoryError, which
    unhandled is a 500 - a fingerprint rather than a leak, but still a 500."""
    real = tmp_path / "static"
    (real / "sub").mkdir(parents=True)
    monkeypatch.setattr(M, "_STATIC_DIR", real)
    r = M._console_asset("sub")
    assert getattr(r, "status_code", None) == 404, type(r).__name__


def test_a_sibling_directory_sharing_the_static_prefix_is_not_served(monkeypatch,
                                                                     tmp_path):
    """It needs a sibling directory to exist, which the repo does not have
    today, so this pins the hole in the guard rather than a live exposure -
    the guard is one `mkdir` away from being one."""
    real = tmp_path / "static"
    real.mkdir()
    (real / "ok.html").write_text("<html><head></head><body>ok</body></html>",
                                  encoding="utf-8")
    sibling = tmp_path / "static_secrets"
    sibling.mkdir()
    (sibling / "creds.txt").write_text("SECRET-KEY-MATERIAL", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("OUTSIDE", encoding="utf-8")
    monkeypatch.setattr(M, "_STATIC_DIR", real)

    assert M._console_asset("ok.html").status_code == 200
    assert M._console_asset("../outside.txt").status_code == 404
    escaped = M._console_asset("../static_secrets/creds.txt")
    assert getattr(escaped, "status_code", None) == 404, (
        "a sibling directory sharing the static prefix was served")


# ── 6. inject_into_html: once, and never worse than before ─────────────────

_PAGE = "<html><head><title>t</title></head><body>keep me</body></html>"


def _strip_snippet(out: str) -> str:
    """`out` with the injected block removed, so the page can be compared."""
    snippet = auth.html_head_injection()
    return out.replace("  " + snippet + "\n", "", 1).replace(snippet, "", 1)


def test_injection_is_idempotent():
    once = auth.inject_into_html(_PAGE)
    assert _meta(once) == 1
    assert once.count("/static/console-token.js") == 1
    assert auth.inject_into_html(once) == once, (
        "a second pass changed the page - this runs on every request")
    assert auth.inject_into_html(auth.inject_into_html(once)) == once


def test_injection_preserves_the_page():
    out = auth.inject_into_html(_PAGE)
    assert _strip_snippet(out) == _PAGE
    assert "<title>t</title>" in out and "keep me" in out
    assert out.rstrip().endswith("</html>")


def test_injection_lands_before_the_closing_head_tag():
    """After the head it is still in the document, but a page with an inline
    `<script>` in the head can run before the shim exists - which is the race
    the shim's own comment says it is placed to win."""
    out = auth.inject_into_html(_PAGE)
    assert out.index("console-token.js") < out.index("</head>")


@pytest.mark.parametrize("page", [
    "<html><HEAD></HEAD></html>",               # uppercase tags
    "<html><head></head>",                       # no body
    "<html><body></body></html>",               # no head
    "<p>bare fragment</p>",                      # neither
    "not html at all",
    "<html><head></head><!-- </head> --></html>",   # a tag inside a comment
])
def test_injection_survives_odd_documents(page):
    out = auth.inject_into_html(page)
    assert _meta(out) == 1, (page, out)
    assert out.count("/static/console-token.js") == 1
    assert _strip_snippet(out) == page, "the page itself was altered"


@pytest.mark.parametrize("junk", [None, "", 0, []])
def test_injection_tolerates_missing_input(junk):
    """Falsy input is returned as-is rather than turned into a page. Anything
    that raises here takes the whole console page down."""
    out = auth.inject_into_html(junk)
    assert not out, (junk, out)


def test_injection_is_a_no_op_when_there_is_no_token(monkeypatch):
    """No token means no snippet. Handing out `content=""` produces a page that
    loads and then 401s - strictly worse than no page, because it looks fine."""
    monkeypatch.setattr(auth, "token", lambda: "")
    out = auth.inject_into_html(_PAGE)
    assert out == _PAGE, out
    assert auth.META_NAME not in out


def test_injection_survives_a_token_lookup_that_raises(monkeypatch):
    def _boom():
        raise auth.AuthNotConfigured("no token")
    monkeypatch.setattr(auth, "token", _boom)
    out = auth.inject_into_html(_PAGE)
    assert out == _PAGE, out


def test_a_page_that_only_documents_the_meta_still_gets_the_loader():
    doc = ('<html><head><title>Auth</title></head><body>'
           'Add <meta name="gateway-token" content="..."> to your page.'
           '</body></html>')
    out = auth.inject_into_html(doc)
    assert "console-token.js" in out, (
        "a page that mentions the meta in prose got no loader script")
    assert _meta(out) == 1
