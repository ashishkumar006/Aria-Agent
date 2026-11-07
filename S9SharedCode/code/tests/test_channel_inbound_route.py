"""POST /api/channel/inbound — the security invariant, tested at the route.

`channel_chat.handle_inbound` has module-level tests, and one seam test covers
the refusal. This file is the route: the request body arrives from a chat
platform's webhook (or anything else that can reach the agent), and the only
thing between that and a paid agent turn on the user's phone is the trust
lookup. Verified live on the running system, an unpaired sender gets:

    {"accepted": false, "reason": "sender is not paired on webui
     (trust=untrusted); no agent turn was started"}

and no turn runs. That is security invariant 2 — "every action must be checked
against the actual user" — holding, and it must not be able to regress quietly:
the failure mode if the gate moves or weakens is the agent answering strangers
and billing the user for it. The paired path is pinned in the same breath,
because a gate that refuses everyone is indistinguishable from a working one.

The gate reads the pairing store from the GATEWAY, never from the request, so
the tests below patch `notify._gw_get` (the gateway read itself) rather than
`trust_level` — that keeps `channel_chat.trust_level`'s own decisions in the
path being tested.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))            # for test_auth_guard.token_on
sys.path.insert(0, str(HERE.parent))     # for channel_chat / notify / auth

import ack_bridge  # noqa: E402
import auth  # noqa: E402
import channel_chat  # noqa: E402
import notify  # noqa: E402
from test_auth_guard import token_on  # noqa: E402


@pytest.fixture()
def client():
    """Enforcement on, and the guard's own token on every request.

    `token_on` mints the per-launch value; the comparison in `_origin_guard` is
    exact, so a made-up header would 403 the whole file rather than test the
    route. The header name comes from `auth` too, for the same reason."""
    guard = token_on()
    with guard as c:
        c.headers[auth.TOKEN_HEADER] = guard.token
        yield c


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """No real conversation map, and no in-flight entry leaking between tests.

    `_INFLIGHT` is process-global: a paired-sender test that left an entry
    behind would make the NEXT test's sender get "a turn for this sender is
    already running", so the order of the file would change its results.
    """
    monkeypatch.setattr(channel_chat, "MAP_PATH", tmp_path / "chan.json")
    for k in list(channel_chat._INFLIGHT):
        channel_chat._INFLIGHT.pop(k, None)
    yield
    for k in list(channel_chat._INFLIGHT):
        channel_chat._INFLIGHT.pop(k, None)


def _trust_from_gateway(monkeypatch, level):
    """Make the pairing store answer `level`, exactly as the gateway does.

    Patching the gateway read rather than `trust_level` keeps the real
    fall-through logic: an unreadable store must return "unknown", and
    "unknown" is not "allowed".
    """
    monkeypatch.setattr(notify, "_gw_get",
                        lambda path, timeout=15.0: {"trust": level})


def _record_turns(monkeypatch, release: threading.Event | None = None):
    """Replace the turn body with a recorder that signals it ran.

    The route hands the turn to a daemon thread and answers the webhook
    immediately — a chat platform retries a slow webhook and a reply that
    arrives twice is worse than one that arrives late — so the test has to wait
    for the thread rather than assume it already finished.
    """
    calls = []
    done = threading.Event()

    def _turn(channel, sender, text, conversation_id, attachments=None):
        calls.append({"channel": channel, "sender": sender, "text": text,
                      "conversation_id": conversation_id,
                      "attachments": attachments,
                      "thread": threading.current_thread().name})
        if release is not None:
            release.wait(5.0)
        done.set()
        return {"ok": True}

    monkeypatch.setattr(channel_chat, "_run_turn", _turn)
    return calls, done


def _await_inflight_gone(key: str, timeout: float = 5.0) -> bool:
    """`_safe_turn`'s `finally` clears the in-flight marker a moment after the
    recorder signals, so a test that reads it immediately would be racing a
    thread rather than asserting anything about the code."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if key not in channel_chat._INFLIGHT:
            return True
        time.sleep(0.02)
    return key not in channel_chat._INFLIGHT


# ── invariant 2: an unpaired sender gets nothing ─────────────────────────

def test_an_unpaired_sender_is_refused_and_starts_no_agent_turn(client,
                                                                monkeypatch):
    _trust_from_gateway(monkeypatch, "untrusted")
    calls, _done = _record_turns(monkeypatch)

    r = client.post("/api/channel/inbound",
                    json={"channel": "telegram", "sender": "999",
                          "text": "what's on my calendar today?"})

    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["accepted"] is False, body
    assert body["trust"] == "untrusted", body
    assert "sender is not paired" in body["reason"], body
    assert "trust=untrusted" in body["reason"], body
    assert "no agent turn was started" in body["reason"], body
    assert calls == [], "an unpaired sender started a paid agent turn"


@pytest.mark.parametrize("level", ["untrusted", "unknown", "", "stranger"])
def test_any_trust_level_short_of_owner_or_paired_is_refused(client, monkeypatch,
                                                             level):
    """The gate is an allow-list, not a deny-list. `unknown` is the case that
    matters: the pairing store could not be read, and "could not check" must
    not be read as "checked and fine"."""
    _trust_from_gateway(monkeypatch, level)
    calls, _done = _record_turns(monkeypatch)

    body = client.post("/api/channel/inbound",
                       json={"channel": "telegram", "sender": "999",
                             "text": "hello"}).json()

    assert body["accepted"] is False, (level, body)
    assert calls == [], (level, "a turn ran without a paired sender")


def test_a_gateway_read_that_raises_is_a_refusal_not_a_free_turn(client,
                                                                 monkeypatch):
    """"The pairing store is unreachable" and "the sender is not paired" must
    produce the same decision. A broad `except` that fell through to the chat
    turn would let an outage (or an attacker who can stall that one call) turn
    the agent into a public service."""
    def _boom(path, timeout=15.0):
        raise RuntimeError("gateway is down")
    monkeypatch.setattr(notify, "_gw_get", _boom)
    calls, _done = _record_turns(monkeypatch)

    body = client.post("/api/channel/inbound",
                       json={"channel": "telegram", "sender": "999",
                             "text": "hello"}).json()

    assert body["accepted"] is False and body["trust"] == "unknown", body
    assert calls == []


def test_the_sender_cannot_claim_its_own_trust_in_the_body(client, monkeypatch):
    """The request is untrusted input. If any part of the gate read the body,
    `{"trust": "owner"}` would be a skeleton key — and the caller already
    holds a valid agent token, which is exactly the position an SSRF or a
    stolen console session is in."""
    _trust_from_gateway(monkeypatch, "untrusted")
    calls, _done = _record_turns(monkeypatch)

    body = client.post("/api/channel/inbound", json={
        "channel": "telegram", "sender": "999", "text": "hello",
        "trust": "owner", "paired": True, "owner": True}).json()

    assert body["accepted"] is False, body
    assert body["trust"] == "untrusted", (
        "the trust level came from the request, not the pairing store")
    assert calls == []


# ── the path that must still work ────────────────────────────────────────

@pytest.mark.parametrize("level", ["paired", "owner"])
def test_a_paired_sender_does_start_a_turn(client, monkeypatch, level):
    """A gate that refuses everyone passes every test above. This is the other
    half: the paired person's message becomes a real chat conversation."""
    _trust_from_gateway(monkeypatch, level)
    calls, done = _record_turns(monkeypatch)

    r = client.post("/api/channel/inbound",
                    json={"channel": "telegram", "sender": "12345",
                          "text": "what is 2 + 2?"})

    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["accepted"] is True, body
    assert body["trust"] == level, body
    assert body["reason"] == "turn started", body
    assert body["conversation_id"], body
    assert done.wait(5.0), "the accepted message never reached a turn"
    assert calls[0]["channel"] == "telegram"
    assert calls[0]["sender"] == "12345"
    assert calls[0]["text"] == "what is 2 + 2?"
    assert calls[0]["conversation_id"] == body["conversation_id"]


def test_an_accepted_turn_runs_off_the_request_thread(client, monkeypatch):
    """The webhook has to answer before the model does. Running the turn inline
    means a slow answer is retried by the chat platform, and the user then gets
    the same reply two or three times."""
    _trust_from_gateway(monkeypatch, "paired")
    calls, done = _record_turns(monkeypatch)

    client.post("/api/channel/inbound",
                json={"channel": "telegram", "sender": "777", "text": "hi"})

    assert done.wait(5.0), "the turn never ran at all"
    assert calls[0]["thread"].startswith("chan-"), (
        f"the turn ran on {calls[0]['thread']!r}, i.e. the request thread")
    assert _await_inflight_gone("telegram:777"), (
        "a finished turn left its in-flight entry behind, blocking the sender")


def test_a_second_message_while_one_is_running_is_explained_not_swallowed(
        client, monkeypatch):
    """Two quick messages used to mean the second vanished with no trace. The
    refusal is correct — one turn at a time — but it has to be reported."""
    _trust_from_gateway(monkeypatch, "paired")
    told = []
    monkeypatch.setattr(notify, "send_channel_message",
                        lambda ch, text, **k: told.append(text) or
                        {"delivered": True})
    release = threading.Event()
    calls, done = _record_turns(monkeypatch, release=release)

    first = client.post("/api/channel/inbound",
                        json={"channel": "telegram", "sender": "777",
                              "text": "first"}).json()
    assert first["accepted"] is True, first
    # The recorder is parked on `release`, so this turn is genuinely in flight.
    second = client.post("/api/channel/inbound",
                         json={"channel": "telegram", "sender": "777",
                               "text": "second"}).json()
    release.set()
    assert done.wait(5.0)

    assert second["accepted"] is False, second
    assert "already running" in second["reason"], second
    assert [c["text"] for c in calls] == ["first"], "both messages ran a turn"
    assert told, "the sender was refused with no explanation at all"


def test_the_conversation_is_reused_so_history_survives(client, monkeypatch):
    """A returning Telegram user losing their thread is the same bug as the
    salted-hash id: every message starts an empty conversation."""
    _trust_from_gateway(monkeypatch, "paired")
    calls, done = _record_turns(monkeypatch)
    ids = []
    for text in ("first", "second"):
        body = client.post("/api/channel/inbound",
                           json={"channel": "telegram", "sender": "4242",
                                 "text": text}).json()
        assert body["accepted"] is True, body
        assert done.wait(5.0)
        assert _await_inflight_gone("telegram:4242")
        done.clear()
        ids.append(body["conversation_id"])

    assert ids[0] == ids[1], ids
    assert [c["text"] for c in calls] == ["first", "second"]


# ── malformed input, and the file-only message ───────────────────────────

@pytest.mark.parametrize("body", [
    {"channel": "telegram", "text": "no sender"},
    {"sender": "999", "text": ""},
    {"channel": "telegram", "sender": "999"},
    {},
])
def test_a_malformed_inbound_body_is_a_400_with_a_reason(client, monkeypatch,
                                                         body):
    """`sender`/`text` used to be read with `.strip()` on whatever arrived;
    a missing one raised and came back as a bare 500 `text/plain`, which the
    webhook handler could not parse and therefore retried forever."""
    _trust_from_gateway(monkeypatch, "paired")
    calls, _done = _record_turns(monkeypatch)

    r = client.post("/api/channel/inbound", json=body)

    assert r.status_code == 400, (body, r.status_code, r.text[:200])
    assert "application/json" in r.headers.get("content-type", "")
    assert r.json()["error"]
    assert calls == [], "a malformed request still reached the trust gate"


@pytest.mark.parametrize("body", [[1, 2, 3], "telegram", 7])
def test_a_body_that_is_not_an_object_is_a_parseable_error(client, monkeypatch,
                                                           body):
    _trust_from_gateway(monkeypatch, "paired")
    r = client.post("/api/channel/inbound", json=body)
    assert r.status_code in (400, 422), (body, r.status_code, r.text[:200])
    assert "application/json" in r.headers.get("content-type", "")


def test_a_file_only_message_is_not_refused_as_malformed(client, monkeypatch):
    """"sender and text are required" used to 400 a user's upload, because a
    document arrives with no caption. The file IS the request now, so it must
    get past validation — and still be stopped by the trust gate."""
    _trust_from_gateway(monkeypatch, "untrusted")
    calls, _done = _record_turns(monkeypatch)

    r = client.post("/api/channel/inbound", json={
        "channel": "telegram", "sender": "999",
        "attachments": [{"filename": "scan.pdf", "url": "http://x/scan.pdf"}]})

    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert "required" not in body["reason"], body
    assert "not paired" in body["reason"], body
    assert calls == [], "an unpaired sender's upload started a turn"


def test_an_attachment_from_a_paired_sender_reaches_the_turn(client, monkeypatch):
    _trust_from_gateway(monkeypatch, "paired")
    calls, done = _record_turns(monkeypatch)

    body = client.post("/api/channel/inbound", json={
        "channel": "telegram", "sender": "5150",
        "attachments": [{"filename": "scan.pdf", "url": "http://x/scan.pdf"}]}).json()

    assert body["accepted"] is True, body
    assert done.wait(5.0)
    assert calls[0]["attachments"][0]["filename"] == "scan.pdf", (
        "the upload was dropped between the route and the turn")


def test_an_upload_bypasses_the_ack_path(client, monkeypatch):
    """Someone who has just sent a file is not answering a reminder, and the
    classifier would see only the caption and mis-file the upload."""
    _trust_from_gateway(monkeypatch, "paired")
    consulted = []
    monkeypatch.setattr(
        ack_bridge, "handle_inbound",
        lambda *a, **k: consulted.append(a) or {"routed": False})
    calls, done = _record_turns(monkeypatch)

    client.post("/api/channel/inbound", json={
        "channel": "telegram", "sender": "5150", "text": "",
        "attachments": [{"filename": "scan.pdf"}]})

    assert done.wait(5.0)
    assert consulted == [], "an upload was handed to the reminder classifier"
    assert calls[0]["attachments"]


# ── a crash is still JSON ────────────────────────────────────────────────

def test_a_router_crash_answers_json_500_not_plain_text(client, monkeypatch):
    """The whole handler is wrapped so that an unexpected failure names itself
    in a body the webhook can log, instead of arriving as Starlette's
    undiagnosable `text/plain` 500."""
    def _boom(*a, **k):
        raise RuntimeError("the router exploded")
    monkeypatch.setattr(channel_chat, "route_inbound", _boom)

    r = client.post("/api/channel/inbound",
                    json={"channel": "telegram", "sender": "999",
                          "text": "hi"})

    assert r.status_code == 500, r.text[:200]
    assert "application/json" in r.headers.get("content-type", "")
    assert "the router exploded" in r.json()["error"]


def test_the_inbound_route_is_behind_the_token(monkeypatch):
    """The webhook is reachable by the console, and the console holds the
    per-launch token. Without the gate, any web page the user opens could POST
    a message claiming to be a paired sender — which is why the gate, not the
    body, is the thing that decides."""
    guard = token_on()
    with guard as c:
        bare = c.post("/api/channel/inbound",
                      json={"channel": "telegram", "sender": "999",
                            "text": "hi"})
        assert bare.status_code == 403, (bare.status_code, bare.text[:200])
        assert "X-Aria-Token" in bare.json()["error"]
        monkeypatch.setattr(notify, "_gw_get",
                            lambda path, timeout=15.0: {"trust": "untrusted"})
        c.headers[auth.TOKEN_HEADER] = guard.token
        assert c.post("/api/channel/inbound",
                      json={"channel": "telegram", "sender": "999",
                            "text": "hi"}).status_code == 200
