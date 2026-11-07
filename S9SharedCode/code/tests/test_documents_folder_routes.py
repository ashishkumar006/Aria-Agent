"""The documents-folder and reindex proxies — verb, path and status fidelity.

The document pipeline (parsing, chunking, embedding, the registry, folders)
lives entirely in the gateway. These agent routes are thin, and being thin is
exactly where they went wrong before:

  * a verb that did not survive the hop — `DELETE` arriving at the gateway as
    `GET` turns a folder deletion into a read that reports 200 to a button the
    user pressed, and the same class of bug wiped memory instead of listing it;
  * a status that did not survive the hop — every proxied route used to
    `return await _gw_json(...)`, which is a plain dict, and FastAPI renders a
    dict as HTTP 200. Measured on the running system: "no such folder" (404),
    "name taken" (409) and "query required" (400) were byte-indistinguishable
    from success at the transport layer, so `if (res.ok)` reported a rejected
    operation as done.

`httpx.MockTransport` is used rather than a stub client, so the header and verb
semantics being asserted are httpx's own — a fake that ignores `headers=` is how
the missing gateway token on the pooled client stayed invisible to its own test.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))            # for test_auth_guard.token_on
sys.path.insert(0, str(HERE.parent))     # for agent_server / auth

import agent_server  # noqa: E402
import auth  # noqa: E402
from test_auth_guard import token_on  # noqa: E402

GW_TOKEN = "zzfake-gateway-token"


@pytest.fixture()
def client(monkeypatch):
    """A guarded client, a pinned gateway credential, and a fresh client pool.

    `_gw_auth_headers` is pinned so the test never depends on the real
    `llm_gatewayV9/state/gateway.token`. `tests/conftest.py` already empties the
    pooled httpx clients per test — without that, the shared client built by an
    earlier test would serve these calls and the assertions below would describe
    a connection made before they started.
    """
    monkeypatch.setattr(agent_server, "_gw_auth_headers",
                        lambda extra=None: {"X-Gateway-Token": GW_TOKEN})
    guard = token_on()
    with guard as c:
        c.headers[auth.TOKEN_HEADER] = guard.token
        yield c


class _Gateway:
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
    def methods(self) -> list[str]:
        return [r.method for r in self.seen]

    @property
    def paths(self) -> list[str]:
        return [r.url.path for r in self.seen]

    @property
    def tokens(self) -> list:
        return [r.headers.get("X-Gateway-Token") for r in self.seen]

    @property
    def bodies(self) -> list:
        out = []
        for r in self.seen:
            try:
                out.append(json.loads(r.content or b"null"))
            except ValueError:
                out.append(None)
        return out


def _mount(monkeypatch, gw: _Gateway) -> _Gateway:
    """Route the module's pooled AsyncClient through a MockTransport.

    Captured before patching: the factory below calls `httpx.AsyncClient`, and
    a factory that calls the name it replaced recurses.
    """
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: real(*a,
                                             transport=httpx.MockTransport(gw),
                                             **k))
    return gw


def _json(status: int, payload):
    return httpx.Response(status, json=payload)


# ── the verb and the path survive the hop ────────────────────────────────

def test_listing_folders_is_a_get_on_the_gateway_path(client, monkeypatch):
    listing = {"folders": [{"id": "f1", "name": "Invoices"}]}
    gw = _mount(monkeypatch, _Gateway([_json(200, listing)]))
    r = client.get("/api/documents/folders")
    assert r.status_code == 200, r.text[:200]
    assert r.json()["folders"][0]["id"] == "f1"
    assert gw.methods == ["GET"], gw.methods
    assert gw.paths == ["/v1/documents/folders"], gw.paths


def test_creating_a_folder_is_a_post_that_carries_the_body(client, monkeypatch):
    gw = _mount(monkeypatch, _Gateway([_json(200, {"folder": {"id": "f2"}})]))
    r = client.post("/api/documents/folders", json={"name": "Contracts"})
    assert r.status_code == 200, r.text[:200]
    assert gw.methods == ["POST"], gw.methods
    assert gw.bodies == [{"name": "Contracts"}], (
        "the folder name did not travel; the gateway would make an unnamed folder")


def test_renaming_a_folder_stays_a_patch_and_keeps_the_id(client, monkeypatch):
    """The past bug class: a route that rebuilt the request as a GET. A PATCH
    sent as a GET is a read, and a read answers 200 while the name never moves."""
    gw = _mount(monkeypatch, _Gateway([_json(200, {"folder": {"name": "Tax"}})]))
    r = client.patch("/api/documents/folders/f1", json={"name": "Tax"})
    assert r.status_code == 200, r.text[:200]
    assert gw.methods == ["PATCH"], gw.methods
    assert gw.paths == ["/v1/documents/folders/f1"], gw.paths
    assert gw.bodies == [{"name": "Tax"}]


def test_deleting_a_folder_stays_a_delete(client, monkeypatch):
    """The one that matters most: `DELETE /v1/memory` once reached the gateway
    as a read, so a wipe button reported success having deleted nothing. A
    downgraded verb is silent, so this pins the method on the wire, not the
    agent's own status code."""
    gw = _mount(monkeypatch, _Gateway([_json(200, {"deleted": "f1"})]))
    r = client.delete("/api/documents/folders/f1")
    assert r.status_code == 200, r.text[:200]
    assert gw.methods == ["DELETE"], gw.methods
    assert gw.paths == ["/v1/documents/folders/f1"], gw.paths


def test_reindexing_a_document_is_a_post_on_its_own_path(client, monkeypatch):
    gw = _mount(monkeypatch, _Gateway([_json(200, {"status": "queued"})]))
    r = client.post("/api/documents/doc-7/reindex")
    assert r.status_code == 200, r.text[:200]
    assert gw.methods == ["POST"], gw.methods
    assert gw.paths == ["/v1/documents/doc-7/reindex"], gw.paths


# ── the credential is per request, never on the shared client ────────────

@pytest.mark.parametrize("verb,url", [
    ("get", "/api/documents/folders"),
    ("post", "/api/documents/folders"),
    ("patch", "/api/documents/folders/f1"),
    ("delete", "/api/documents/folders/f1"),
    ("post", "/api/documents/doc-7/reindex"),
])
def test_every_folder_call_presents_the_gateway_token(client, monkeypatch,
                                                      verb, url):
    """The gateway requires `X-Gateway-Token` on every `/v1/*` route, and the
    pooled client deliberately carries NO headers — so if the request-level
    header is ever dropped, every one of these calls 401s while `/api/health`
    still reads green."""
    gw = _mount(monkeypatch, _Gateway([_json(200, {"ok": True})]))
    getattr(client, verb)(url)
    assert gw.tokens == [GW_TOKEN], gw.tokens


def test_a_401_retries_once_with_a_refreshed_token_and_keeps_the_verb(
        client, monkeypatch):
    """The gateway regenerates its token when its state file goes missing, so a
    long-lived agent can hold one that stopped being valid. The retry must
    re-read the credential AND stay a DELETE — a retry that fell back to the
    default verb would turn the wipe into a read on exactly the error path
    nobody looks at."""
    issued = iter([GW_TOKEN, "zzfake-rotated"])
    monkeypatch.setattr(agent_server, "_gw_auth_headers",
                        lambda extra=None: {"X-Gateway-Token": next(issued)})
    gw = _mount(monkeypatch, _Gateway([
        _json(401, {"error": "missing or invalid x-gateway-token header"}),
        _json(200, {"deleted": "f1"}),
    ]))
    r = client.delete("/api/documents/folders/f1")
    assert r.status_code == 200, r.text[:200]
    assert gw.methods == ["DELETE", "DELETE"], gw.methods
    assert gw.tokens == [GW_TOKEN, "zzfake-rotated"], gw.tokens


# ── a gateway refusal is not laundered into a 200 ────────────────────────

@pytest.mark.parametrize("status", [400, 403, 404, 409, 500, 503])
@pytest.mark.parametrize("verb,url", [
    ("delete", "/api/documents/folders/f1"),
    ("patch", "/api/documents/folders/f1"),
    ("post", "/api/documents/doc-7/reindex"),
])
def test_a_gateway_failure_keeps_its_status_and_its_reason(client, monkeypatch,
                                                           status, verb, url):
    gw = _mount(monkeypatch, _Gateway([_json(status, {"error": f"nope {status}"})]))
    r = client.request(verb.upper(), url, json={"name": "x"})
    assert r.status_code == status, (status, verb, r.status_code, r.text[:200])
    assert r.json()["error"] == f"nope {status}", (
        "the reason was replaced by a generic one, so the user cannot tell "
        "'folder not found' from 'server is down'")
    # A FastMCP-shaped `{"detail": ...}` body is folded into `error` upstream;
    # nothing may arrive here as a bare success object.
    assert "status_code" not in r.json() or r.json()["status_code"] == status


@pytest.mark.parametrize("verb,url", [
    ("get", "/api/documents/folders"),
    ("post", "/api/documents/folders"),
])
def test_a_read_failure_is_not_reported_as_an_empty_success(client, monkeypatch,
                                                            verb, url):
    """"200 with an empty list" is indistinguishable from "you have no folders",
    which is how a gateway outage read as the user having deleted everything."""
    gw = _mount(monkeypatch, _Gateway([_json(500, {"error": "registry unreadable"})]))
    r = client.request(verb.upper(), url, json={"name": "x"})
    assert r.status_code == 500, (r.status_code, gw.methods)
    assert "registry unreadable" in r.json()["error"]


def test_a_gateway_error_page_still_yields_a_parseable_error(client, monkeypatch):
    """A proxy or ingress in front of the gateway answers 502 as HTML. `r.json()`
    raises, and the old code let that escape as the agent's own 500."""
    gw = _mount(monkeypatch, _Gateway([
        httpx.Response(502, text="<html>bad gateway</html>")]))
    r = client.delete("/api/documents/folders/f1")
    assert r.status_code >= 500, (r.status_code, gw.methods)
    assert "application/json" in r.headers.get("content-type", "")
    assert r.json()["error"]


def test_an_unreachable_gateway_is_a_5xx_with_the_reason(client, monkeypatch):
    """Nothing is listening: the user needs 'the gateway is down', said with a
    status that means a server problem — never a 200 and never a 4xx that tells
    them to fix an input that was fine."""
    def _refuse(request):
        raise httpx.ConnectError("connection refused", request=request)
    _mount(monkeypatch, _Gateway([_refuse]))
    for verb, url in (("get", "/api/documents/folders"),
                      ("delete", "/api/documents/folders/f1"),
                      ("post", "/api/documents/doc-7/reindex")):
        r = getattr(client, verb)(url)
        assert 500 <= r.status_code < 600, (url, r.status_code, r.text[:200])
        assert "unreachable" in r.json()["error"].lower(), r.json()


# ── malformed bodies ─────────────────────────────────────────────────────

@pytest.mark.parametrize("body", [[1, 2, 3], "a string", 7, True])
def test_a_non_object_folder_body_never_reaches_the_gateway_as_garbage(
        client, monkeypatch, body):
    """The route is deliberately permissive: a body it cannot use becomes `{}`
    and the gateway — which owns the schema — gives the real 400. What must not
    happen is the agent 500ing, or the malformed value being forwarded as if it
    were a folder definition."""
    gw = _mount(monkeypatch, _Gateway([_json(400, {"error": "name is required"})]))
    r = client.post("/api/documents/folders", json=body)
    assert r.status_code == 400, (body, r.status_code, r.text[:200])
    assert gw.bodies == [{}], (body, gw.bodies)


def test_an_unparseable_folder_body_is_answered_by_the_gateway_not_a_crash(
        client, monkeypatch):
    gw = _mount(monkeypatch, _Gateway([_json(400, {"error": "name is required"})]))
    r = client.post("/api/documents/folders", content=b"{not json")
    assert r.status_code == 400, (r.status_code, gw.methods, r.text[:200])
    assert gw.bodies == [{}]
    assert "application/json" in r.headers.get("content-type", "")


def test_a_rename_with_no_body_still_sends_an_object(client, monkeypatch):
    """`json=None` to httpx sends no body at all; the gateway then fails to
    parse rather than reporting "nothing to change". The proxy normalises it."""
    gw = _mount(monkeypatch, _Gateway([_json(200, {"folder": {"name": "f1"}})]))
    r = client.patch("/api/documents/folders/f1")
    assert r.status_code == 200, r.text[:200]
    assert gw.bodies == [{}], gw.bodies
