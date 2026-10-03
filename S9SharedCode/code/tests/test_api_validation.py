"""Request-validation regressions found by the dynamic route sweep.

The sweep probed all 85 agent routes with empty, malformed and out-of-range
bodies. Four endpoints answered a request they should have rejected; these
tests pin the corrected behaviour so the sweep result cannot regress.

Every case is a non-LLM endpoint, so the suite stays fast and token-free.
"""
from __future__ import annotations

import importlib
import inspect
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_server  # noqa: E402
import persistence  # noqa: E402
from agent_server import app  # noqa: E402


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


# --- POST /api/templates -------------------------------------------------
# An empty body used to reach `body.get(...)` on a None and raise, surfacing as
# a 500 "Internal Server Error" with no detail.

def test_templates_empty_body_is_400_not_500(client):
    r = client.post("/api/templates")
    assert r.status_code == 400, r.text
    assert "error" in r.json()


def test_templates_malformed_json_is_400(client):
    r = client.post("/api/templates", content=b"{not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert "error" in r.json()


def test_templates_non_object_body_is_400(client):
    r = client.post("/api/templates", json=[1, 2, 3])
    assert r.status_code == 400


@pytest.mark.parametrize("body", [{}, {"name": "x"}, {"query": "x"},
                                  {"name": "  ", "query": "x"},
                                  {"name": "x", "query": "  "}])
def test_templates_missing_fields_is_400(client, body):
    r = client.post("/api/templates", json=body)
    assert r.status_code == 400, body
    assert "error" in r.json()


def test_templates_valid_body_still_saves(client, tmp_path, monkeypatch):
    """The happy path must survive the stricter status codes."""
    monkeypatch.setattr(agent_server, "_TEMPLATES_FILE", None, raising=False)
    saved = {}

    def _save(name, query, variables):
        saved.update(name=name, query=query, vars=variables)
        return {"name": name, "query": query, "vars": variables or []}

    monkeypatch.setattr("templates.save", _save, raising=False)
    r = client.post("/api/templates",
                    json={"name": "research", "query": "Research {topic}",
                          "vars": ["topic"]})
    assert r.status_code == 200, r.text
    assert r.json()["template"]["name"] == "research"
    assert saved["query"] == "Research {topic}"


# --- POST /api/conversations/adopt --------------------------------------
# This endpoint writes the conversation->session mapping that
# resolve_session() consults. It previously adopted any string, persisting a
# dangling entry and answering `adopted: true` for a session that does not
# exist, so a caller believed a nonexistent run had become resumable.

def test_adopt_unknown_session_is_404(client):
    r = client.post("/api/conversations/adopt",
                    json={"session_id": "s8-does-not-exist-xyz"})
    assert r.status_code == 404, r.text
    assert "error" in r.json()


def test_adopt_missing_session_id_is_400(client):
    r = client.post("/api/conversations/adopt", json={})
    assert r.status_code == 400


def test_adopt_does_not_persist_unknown_session(client):
    """The mapping must not gain an entry for a session that was never there."""
    before = json.dumps(agent_server._conv_load(), sort_keys=True)
    client.post("/api/conversations/adopt",
                json={"session_id": "s8-never-existed-abc"})
    after = json.dumps(agent_server._conv_load(), sort_keys=True)
    assert before == after


def test_dedup_stream_ends_in_a_terminal_frame():
    """The client throws when a stream stops without done/error.

    A successful dedup used to emit only `started` + `status`, so a
    *correctly deduplicated* submit rendered as a red "connection closed
    before the run finished" error with an empty canvas.
    """
    import asyncio
    import inspect
    src = inspect.getsource(agent_server._dedup_stream)
    assert 'yield _sse("error"' in src or 'yield _sse("done"' in src, \
        "the dedup stream must end in a terminal frame"
    # And it must actually be reachable: run the generator to exhaustion.
    async def _drain():
        return [f async for f in agent_server._dedup_stream("s8-x")]
    frames = asyncio.new_event_loop().run_until_complete(_drain())
    kinds = [f.split('"type": "')[1].split('"')[0] if '"type": "' in f else "?"
             for f in frames]
    assert kinds[-1] in ("error", "done"), kinds
    assert "started" in kinds


def test_inflight_key_is_released_on_disconnect():
    """The disconnect path returns before the release below it.

    A key left claimed for the full 600s TTL meant a later submit with the
    same key was swallowed as a duplicate of an already-finished run.
    """
    src = inspect.getsource(agent_server._stream_run)
    # Every exit from the generator must release the key.
    assert "_inflight_run_release(idem_key)" in src
    disconnect_at = src.find("if disconnected:")
    assert disconnect_at != -1, "disconnect branch missing"
    tail = src[disconnect_at:disconnect_at + 1400]
    assert "_inflight_run_release(idem_key)" in tail, \
        "the disconnect branch must release the key before returning"


def test_chat_stream_sends_an_idempotency_key():
    """chat/simple/stream had no key at all, so a double-click started and
    billed N identical runs."""
    import inspect
    src = inspect.getsource(agent_server.chat_simple_stream)
    assert "_inflight_run_claim" in src


def test_adopt_existing_session_succeeds(client, tmp_path, monkeypatch):
    """A real session still gets adopted."""
    sid = "s8-adopt-probe-real"
    sessions_root = persistence.SESSIONS_ROOT
    (sessions_root / sid).mkdir(parents=True, exist_ok=True)
    try:
        r = client.post("/api/conversations/adopt", json={"session_id": sid})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["session_id"] == sid
        assert body["conversation_id"].startswith("c-")
        # Idempotent: a second call returns the same conversation.
        r2 = client.post("/api/conversations/adopt", json={"session_id": sid})
        assert r2.json()["conversation_id"] == body["conversation_id"]
        assert r2.json()["adopted"] is False
    finally:
        data = agent_server._conv_load()
        for cid, mapped in list(data.items()):
            if mapped == sid:
                data.pop(cid, None)
        agent_server._conv_save(data)
        import shutil
        shutil.rmtree(sessions_root / sid, ignore_errors=True)

# --- the trailing-slash global wipe --------------------------------------
# `DELETE /api/memory/` drew a 307 that PRESERVED the method, so it landed on
# the wipe handler: a request meant to touch nothing erased every drawer and
# still answered 200. Found during adversarial testing, which lost real data.

def test_trailing_slash_delete_is_not_redirected_to_the_wipe(client):
    r = client.delete("/api/memory/", follow_redirects=False)
    assert r.status_code not in (301, 302, 307, 308), \
        f"a redirect preserves DELETE and would reach the wipe: {r.status_code}"
    # 405: the SPA fallback serves GET/HEAD only, so a stray DELETE lands on a
    # method that does not exist. Either way it must not wipe anything.
    assert r.status_code in (404, 405), r.text
    assert "wiped" not in r.text.lower()


def test_app_disables_redirect_slashes():
    """A 307 preserves the HTTP method, so it can silently widen a request
    into the destructive one. Belt and braces with the confirm guard."""
    assert agent_server.app.router.redirect_slashes is False


def test_unscoped_wipe_requires_explicit_confirm(client):
    r = client.delete("/api/memory")
    assert r.status_code == 400, r.text
    assert "confirm" in r.json()["message"]


@pytest.mark.parametrize("bad", ["yes", "true", "1", "delete", "", "WIPE "])
def test_wipe_with_wrong_confirm_is_refused(client, bad):
    r = client.delete(f"/api/memory?confirm={bad}")
    assert r.status_code == 400, f"confirm={bad!r} -> {r.status_code}"


def test_scoped_wipe_with_confirm_is_allowed(client):
    """A session-scoped purge is narrow and must keep working."""
    r = client.delete("/api/memory?session_id=s8-nope&confirm=wipe")
    # Needs the gateway; a connection failure is a 502, not a 400. Either is
    # fine here -- the assertion is that the CONFIRM GUARD passed.
    assert r.status_code in (200, 502), r.text


# --- the search branch must honour the kind filter -----------------------
# The q branch dropped kinds/hide_superseded, so selecting "tool outcomes" in
# the UI still returned facts and preferences as soon as a search was active.

def test_search_honours_kinds_filter(monkeypatch):
    captured = {}

    def fake_read(query, history=None, *, kinds=None, top_k=8,
                  session_id=None, drawers=None, include_stale=False):
        captured.update(query=query, kinds=kinds, include_stale=include_stale)
        return []

    monkeypatch.setattr(importlib.import_module("memory"), "read", fake_read)
    with TestClient(app) as c:
        c.get("/api/memory?q=anything&kinds=tool_outcome")
    assert captured.get("kinds") == ["tool_outcome"], captured


def test_search_passes_hide_superseded(monkeypatch):
    captured = {}

    def fake_read(query, history=None, *, kinds=None, top_k=8,
                  session_id=None, drawers=None, include_stale=False):
        captured.update(include_stale=include_stale)
        return []

    monkeypatch.setattr(importlib.import_module("memory"), "read", fake_read)
    with TestClient(app) as c:
        c.get("/api/memory?q=x&hide_superseded=true")
    assert captured.get("include_stale") is False


# --- a failing backend must not look like an empty store ------------------

def test_memory_list_backend_failure_is_not_a_silent_empty_list(monkeypatch):
    """Was `return {"items": [], "error": ...}` with a 200, which the UI
    rendered as "Nothing stored here" -- so a gateway outage looked exactly
    like memory having been deleted."""

    def boom(*a, **k):
        raise importlib.import_module("memory").MemoryBackendError("gateway 500", 500)

    monkeypatch.setattr(importlib.import_module("memory"), "list_recent", boom)
    with TestClient(app) as c:
        r = c.get("/api/memory")
    assert r.status_code >= 400, r.text
    assert "items" not in r.json()


def test_memory_list_client_error_keeps_its_status(monkeypatch):
    def bad_kind(*a, **k):
        raise importlib.import_module("memory").MemoryBackendError("unknown kind", 400)

    monkeypatch.setattr(importlib.import_module("memory"), "list_recent", bad_kind)
    with TestClient(app) as c:
        r = c.get("/api/memory?kinds=nonsense")
    assert r.status_code == 400, r.text


# --- remember bridge input validation -----------------------------------

@pytest.mark.parametrize("body", [
    [1, 2, 3],
    "a string",
    5,
    {"kind": ["fact"], "descriptor": "x"},
    {"kind": "fact", "descriptor": {"a": 1}},
    {"kind": "fact", "descriptor": 12345},
    {"kind": "fact", "descriptor": "x", "keywords": "notalist"},
    {"kind": "fact", "descriptor": "x", "value": ["notanobject"]},
])
def test_remember_rejects_bad_types_with_400(client, body):
    """All of these raised AttributeError/TypeError and returned a bare 500."""
    r = client.post("/api/memory/remember", json=body)
    assert r.status_code == 400, f"{body!r} -> {r.status_code} {r.text}"


def test_remember_rejects_pathologically_nested_value(client):
    """A >100-deep value made pydantic raise on save, and because the store
    kept it in its cache, one such record bricked every later read and write
    in that drawer."""
    deep: dict = {}
    cur = deep
    for _ in range(400):
        cur["n"] = {}
        cur = cur["n"]
    r = client.post("/api/memory/remember",
                    json={"kind": "fact", "descriptor": "zzdeep", "value": deep})
    assert r.status_code == 400, r.text
    assert "nest" in r.json()["error"].lower()


def test_value_depth_counter_is_not_off_by_one():
    assert agent_server._json_depth({"a": {"b": {"c": 1}}}) == 3
    shallow = {"a": 1, "b": [1, 2], "c": {"d": "x"}}
    assert agent_server._json_depth(shallow) < agent_server._MAX_VALUE_DEPTH


# --- gateway client errors must keep their status ------------------------

def test_gateway_4xx_is_not_flattened_to_502():
    assert agent_server._gw_status({"status_code": 400}) == 400
    assert agent_server._gw_status({"status_code": 413}) == 413
    assert agent_server._gw_status({"status_code": 500}) == 502
    assert agent_server._gw_status({}) == 502
    assert agent_server._gw_status({"status_code": "junk"}) == 502
