"""The notification, channel-inventory and ack-thread ROUTES.

`test_notify_health.py` and `test_reminder_delivery.py` drive the `notify`
module directly; nothing in the suite ever reached these URLs over HTTP, so
deleting any one of them left the whole run green. They are the only thing the
console's Notify panel talks to, and each wraps its work in a `try/except` that
answers JSON on purpose: a route that raises instead falls through to Starlette's
bare `500 text/plain "Internal Server Error"`, which a `res.json()` client cannot
parse and therefore reports as "something went wrong" rather than what broke.

State-bearing paths are moved into `tmp_path`, so nothing here reads or writes
the real notifications file or the channel conversation map.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))            # for test_auth_guard.token_on
sys.path.insert(0, str(HERE.parent))     # for agent_server / notify / ack

import ack  # noqa: E402
import agent_server  # noqa: E402
import channel_chat  # noqa: E402
import notify  # noqa: E402
from test_auth_guard import token_on  # noqa: E402


@pytest.fixture()
def guarded():
    """A client with enforcement ON, plus its header.

    Reuses `token_on` rather than reinventing token plumbing: enforcement is
    process-global, and a test that turns it on and forgets to turn it off 403s
    every later test in the session.
    """
    guard = token_on()
    with guard as c:
        yield c, {"X-Aria-Token": guard.token}


@pytest.fixture()
def notif_file(tmp_path, monkeypatch):
    """The notifications file, pointed at tmp and seeded."""
    path = tmp_path / "notifications.json"
    monkeypatch.setattr(agent_server, "_NOTIF_PATH", path)
    return path


def _seed(path: Path, rows: list) -> None:
    path.write_text(json.dumps(rows), encoding="utf-8")


# ── GET /api/notifications ───────────────────────────────────────────────

def test_notifications_come_back_newest_first(guarded, notif_file):
    c, hdr = guarded
    _seed(notif_file, [
        {"ts": 1.0, "kind": "info", "text": "oldest"},
        {"ts": 2.0, "kind": "warn", "text": "newest"},
    ])
    r = c.get("/api/notifications", headers=hdr)
    assert r.status_code == 200, r.text[:200]
    rows = r.json()["notifications"]
    assert [n["text"] for n in rows] == ["newest", "oldest"], (
        "the panel showed the oldest notification first, so the one the user "
        "came to read was off the bottom of the list")


def test_the_limit_query_param_windows_the_list(guarded, notif_file):
    c, hdr = guarded
    _seed(notif_file, [{"ts": float(i), "kind": "info", "text": f"n{i}"}
                       for i in range(5)])
    r = c.get("/api/notifications?limit=2", headers=hdr)
    assert [n["text"] for n in r.json()["notifications"]] == ["n4", "n3"]


def test_a_negative_limit_does_not_drop_the_oldest_record(guarded, notif_file):
    """`items[-limit:]` with a negative limit is `items[1:]`, so `?limit=-1`
    quietly dropped the OLDEST entry and returned a short list that looked
    complete. A negative now means "everything"."""
    c, hdr = guarded
    _seed(notif_file, [{"ts": 1.0, "kind": "info", "text": "a"},
                       {"ts": 2.0, "kind": "info", "text": "b"}])
    r = c.get("/api/notifications?limit=-1", headers=hdr)
    assert r.status_code == 200, r.text[:200]
    assert [n["text"] for n in r.json()["notifications"]] == ["b", "a"]


def test_a_non_numeric_limit_is_a_parseable_client_error(guarded):
    """Not a 500 and not `text/plain`: the console must be able to say
    "your query string is wrong" rather than "the server is broken"."""
    c, hdr = guarded
    r = c.get("/api/notifications?limit=abc", headers=hdr)
    assert r.status_code == 422, (r.status_code, r.text[:200])
    assert "application/json" in r.headers.get("content-type", "")


def test_a_corrupt_notifications_file_is_not_a_500(guarded, notif_file):
    """Half-written state must degrade to an empty list; the panel opening is
    not allowed to depend on a file that a crash may have truncated."""
    c, hdr = guarded
    notif_file.write_text("{ this is not json", encoding="utf-8")
    r = c.get("/api/notifications", headers=hdr)
    assert r.status_code == 200, r.text[:200]
    assert r.json()["notifications"] == []


# ── POST /api/notifications ──────────────────────────────────────────────

def test_a_notification_is_appended_and_returned(guarded, notif_file):
    c, hdr = guarded
    _seed(notif_file, [])
    r = c.post("/api/notifications", headers=hdr,
               json={"kind": "info", "text": "built the report",
                     "conversation_id": "ct-abc"})
    assert r.status_code == 200, r.text[:200]
    assert r.json()["notification"]["text"] == "built the report"
    on_disk = json.loads(notif_file.read_text(encoding="utf-8"))
    assert len(on_disk) == 1 and on_disk[0]["conversation_id"] == "ct-abc", (
        "the route answered as if it had stored nothing")


def test_an_oversized_notification_text_is_capped(guarded, notif_file):
    """The body is not length-limited at the transport, so the cap has to be
    applied here: an uncapped 5 MB `text` would be written to state and then
    re-served in full to every later reader of the panel."""
    c, hdr = guarded
    _seed(notif_file, [])
    r = c.post("/api/notifications", headers=hdr,
               json={"kind": "info", "text": "x" * 200_000})
    assert r.status_code == 200, r.text[:200]
    assert len(r.json()["notification"]["text"]) == 500


@pytest.mark.parametrize("body", [[1, 2, 3], "a string", 7, True, None])
def test_a_body_that_is_not_an_object_is_a_400(guarded, notif_file, body):
    """`body.get(...)` on a list/str/None is an AttributeError, which surfaced
    as a bare 500 `text/plain`. A non-object body is the client's mistake."""
    c, hdr = guarded
    r = c.post("/api/notifications", headers=hdr, json=body)
    assert r.status_code == 400, (body, r.status_code, r.text[:200])
    assert "application/json" in r.headers.get("content-type", "")
    assert r.json()["error"]


def test_an_unparseable_body_is_a_400_not_a_500(guarded, notif_file):
    c, hdr = guarded
    r = c.post("/api/notifications", headers=hdr, content=b"{not json")
    assert r.status_code == 400, r.text[:200]
    assert "json" in r.json()["error"].lower()


@pytest.mark.parametrize("body", [
    {"kind": "info", "text": 123},          # a number where a string belongs
    {"kind": ["info"], "text": "hello"},    # a list
    {"kind": "info"},                       # missing `text`
    {"kind": "info", "text": ""},           # empty text
    {"kind": "info", "text": "   "},        # whitespace-only text
])
def test_a_field_that_is_missing_or_not_a_string_is_a_400(guarded, notif_file,
                                                          body):
    """`.strip()` on a truthy non-string raises, and an empty text used to be
    stored as a blank row the panel rendered as an empty card."""
    c, hdr = guarded
    r = c.post("/api/notifications", headers=hdr, json=body)
    assert r.status_code == 400, (body, r.status_code, r.text[:200])
    assert "application/json" in r.headers.get("content-type", "")
    assert r.json()["error"]
    assert notif_file.exists() is False or json.loads(
        notif_file.read_text(encoding="utf-8")) == [], "a rejected write landed"


def test_an_absent_kind_defaults_to_info_rather_than_being_refused(guarded,
                                                                   notif_file):
    """`text` is required; `kind` is not, and the route's own contract is that
    a caller may omit it. Pinned so a future "validate every field" sweep does
    not turn the panel's own writes into 400s."""
    c, hdr = guarded
    _seed(notif_file, [])
    r = c.post("/api/notifications", headers=hdr, json={"text": "just a note"})
    assert r.status_code == 200, r.text[:200]
    assert r.json()["notification"]["kind"] == "info"


# ── GET /api/notify/channels ─────────────────────────────────────────────

_HEALTH = {"channels": [{"name": "telegram", "configured": True,
                         "destination": True}]}


def test_the_channel_inventory_returns_health_and_recent_deliveries(guarded,
                                                                    monkeypatch):
    c, hdr = guarded
    monkeypatch.setattr(notify, "channel_health", lambda channel="": _HEALTH)
    monkeypatch.setattr(notify, "delivery_log",
                        lambda limit=50: [{"channel": "telegram", "ok": True}])
    r = c.get("/api/notify/channels", headers=hdr)
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["health"]["channels"][0]["name"] == "telegram"
    assert body["recent"][0]["channel"] == "telegram"


def test_the_channel_inventory_does_not_run_on_the_event_loop(guarded,
                                                              monkeypatch):
    """`channel_health()` is a blocking gateway round trip per configured
    channel. Run inline from an `async def` it held the loop for 9.6 s measured
    (22 s under load) and froze every chat stream in the process — opening the
    Notify settings was enough to stop the whole app.

    `asyncio.get_running_loop()` only succeeds on a loop thread, so raising
    here is the proof the work moved to a worker thread."""
    c, hdr = guarded
    seen = {}

    def _health(channel=""):
        try:
            seen["loop"] = asyncio.get_running_loop()
        except RuntimeError:
            seen["loop"] = None
        seen["thread"] = threading.current_thread().name
        return _HEALTH

    monkeypatch.setattr(notify, "channel_health", _health)
    monkeypatch.setattr(notify, "delivery_log", lambda limit=50: [])
    r = c.get("/api/notify/channels", headers=hdr)
    assert r.status_code == 200, r.text[:200]
    assert seen["loop"] is None, (
        f"channel_health ran on the event loop (thread {seen['thread']})")


def test_a_failing_health_probe_answers_json_500(guarded, monkeypatch):
    """A broken channel report is a real failure, so a 500 is right — but it
    has to be a JSON 500 with the reason, not the plain-text one."""
    c, hdr = guarded
    def _boom(channel=""):
        raise RuntimeError("gateway would not answer")
    monkeypatch.setattr(notify, "channel_health", _boom)
    r = c.get("/api/notify/channels", headers=hdr)
    assert r.status_code == 500, r.text[:200]
    assert "application/json" in r.headers.get("content-type", "")
    assert "gateway would not answer" in r.json()["error"]


# ── POST /api/notify/refresh ─────────────────────────────────────────────

def test_refresh_forwards_the_channel_and_the_pasted_token(guarded, monkeypatch):
    """Verification is the point of this route: it separates "the gateway holds
    a token" from "that token still works". A silently dropped `channel` would
    make every refresh report on the wrong transport."""
    c, hdr = guarded
    called = {}

    def _refresh(channel, token=""):
        called["channel"], called["token"] = channel, token
        return {"channel": channel, "ok": True, "verified": True}

    monkeypatch.setattr(notify, "refresh_channel_token", _refresh)
    r = c.post("/api/notify/refresh", headers=hdr,
               json={"channel": "telegram", "token": "zzfake-token"})
    assert r.status_code == 200, r.text[:200]
    assert called == {"channel": "telegram", "token": "zzfake-token"}
    assert r.json()["verified"] is True


def test_refresh_reports_an_unverified_channel_without_raising(guarded,
                                                               monkeypatch):
    """`ok: false` is a normal answer here, not an exception — the panel needs
    to show which env var to fix."""
    c, hdr = guarded
    monkeypatch.setattr(notify, "refresh_channel_token",
                        lambda channel, token="": {
                            "channel": channel, "ok": False, "verified": False,
                            "action_required": "TELEGRAM_BOT_TOKEN",
                            "detail": "not set in this process"})
    r = c.post("/api/notify/refresh", headers=hdr, json={"channel": "telegram"})
    assert r.status_code == 200, r.text[:200]
    assert r.json()["action_required"] == "TELEGRAM_BOT_TOKEN"


def test_refresh_refuses_a_body_that_is_not_an_object(guarded, monkeypatch):
    c, hdr = guarded
    monkeypatch.setattr(notify, "refresh_channel_token",
                        lambda channel, token="": {"channel": channel,
                                                   "ok": True, "verified": True})
    r = c.post("/api/notify/refresh", headers=hdr, json=["telegram"])
    assert r.status_code == 422, (r.status_code, r.text[:200])
    assert "application/json" in r.headers.get("content-type", "")


def test_a_refresh_failure_is_json_and_not_a_plain_500(guarded, monkeypatch):
    c, hdr = guarded
    def _boom(channel, token=""):
        raise RuntimeError("adaptor blew up")
    monkeypatch.setattr(notify, "refresh_channel_token", _boom)
    r = c.post("/api/notify/refresh", headers=hdr, json={"channel": "telegram"})
    assert r.status_code == 500
    assert "application/json" in r.headers.get("content-type", "")
    assert "adaptor blew up" in r.json()["error"]


# ── GET /api/ack/threads ─────────────────────────────────────────────────

def test_open_ack_threads_are_listed(guarded, monkeypatch):
    """A reminder waiting on a reply looked identical to one delivered and
    ignored until these records were exposed."""
    c, hdr = guarded
    monkeypatch.setattr(ack, "list_threads", lambda limit=50: [
        {"schedule_id": "s-1", "channel": "telegram", "status": "open"}])
    r = c.get("/api/ack/threads", headers=hdr)
    assert r.status_code == 200, r.text[:200]
    assert r.json()["threads"][0]["schedule_id"] == "s-1"


def test_the_ack_thread_list_is_bounded_by_the_limit(guarded, monkeypatch):
    c, hdr = guarded
    seen = {}

    def _list(limit=50):
        seen["limit"] = limit
        return [{"schedule_id": f"s-{i}"} for i in range(limit)]
    monkeypatch.setattr(ack, "list_threads", _list)
    r = c.get("/api/ack/threads?limit=3", headers=hdr)
    assert r.status_code == 200, r.text[:200]
    assert seen["limit"] == 3, "the limit was ignored, so the panel pulled all"
    assert len(r.json()["threads"]) == 3


def test_a_failing_ack_thread_read_is_a_json_500(guarded, monkeypatch):
    c, hdr = guarded
    def _boom(limit=50):
        raise RuntimeError("ack store is unreadable")
    monkeypatch.setattr(ack, "list_threads", _boom)
    r = c.get("/api/ack/threads", headers=hdr)
    assert r.status_code == 500, r.text[:200]
    assert "application/json" in r.headers.get("content-type", "")
    assert "unreadable" in r.json()["error"]


# ── GET /api/channel/threads ─────────────────────────────────────────────

@pytest.fixture()
def chan_map(tmp_path, monkeypatch):
    path = tmp_path / "channel_conversations.json"
    monkeypatch.setattr(channel_chat, "MAP_PATH", path)
    return path


def test_the_channel_conversation_map_is_exposed(guarded, chan_map):
    """A channel turn is a real conversation with its own history, and until
    this route existed there was no way to see which thread a sender maps to."""
    c, hdr = guarded
    chan_map.write_text(json.dumps({
        "telegram:111": {"conversation_id": "telegram-abc", "updated": 2.0},
    }), encoding="utf-8")
    r = c.get("/api/channel/threads", headers=hdr)
    assert r.status_code == 200, r.text[:200]
    convs = r.json()["conversations"]
    assert convs["telegram:111"]["conversation_id"] == "telegram-abc"


def test_a_missing_or_corrupt_map_reads_as_empty(guarded, chan_map):
    """Never opening the panel must be the same as nobody having talked yet."""
    c, hdr = guarded
    assert c.get("/api/channel/threads", headers=hdr).json() == {"conversations": {}}
    chan_map.write_text("not json at all", encoding="utf-8")
    r = c.get("/api/channel/threads", headers=hdr)
    assert r.status_code == 200, r.text[:200]
    assert r.json()["conversations"] == {}


def test_a_map_that_is_a_list_not_a_dict_reads_as_empty(guarded, chan_map):
    """`route_inbound` iterates this file's keys; a list-shaped file used to
    raise on the wrong type and 500 the panel."""
    c, hdr = guarded
    chan_map.write_text(json.dumps([{"conversation_id": "x"}]), encoding="utf-8")
    r = c.get("/api/channel/threads", headers=hdr)
    assert r.status_code == 200, r.text[:200]
    assert r.json() == {"conversations": {}}


# ── the guard actually covers these routes ───────────────────────────────

@pytest.mark.parametrize("method,path", [
    ("get", "/api/notifications"), ("post", "/api/notifications"),
    ("get", "/api/notify/channels"), ("post", "/api/notify/refresh"),
    ("get", "/api/ack/threads"), ("get", "/api/channel/threads"),
])
def test_every_route_in_this_family_is_behind_the_token(method, path):
    """A new `/api` route is not automatically gated by anything a developer
    writes later — the gate is this middleware. Without this test a route could
    be added (or exempted) and stay reachable from any web page."""
    guard = token_on()
    with guard as c:
        r = getattr(c, method)(path)
        assert r.status_code == 403, (path, r.status_code)
        assert r.json()["error"]
        ok = getattr(c, method)(path, headers={"X-Aria-Token": guard.token})
    assert ok.status_code != 403, (path, ok.status_code, ok.text[:200])
