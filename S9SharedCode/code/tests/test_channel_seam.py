"""The channel → agent seam, which had no tests at all.

Every existing test either stopped at `handle_inbound` (with `_run_turn`
replaced by a recorder) or stubbed `trust_level` itself. Nothing between a
Telegram message and an answer on the user's phone was ever executed by the
suite. That matters because this is the path a real person uses: break the
SSE parser, the trust lookup, or the delivery, and every test stays green
while Telegram goes quiet.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import agent_server
import channel_chat

HDR = {"X-Aria-Token": "test-token"}


@pytest.fixture()
def client():
    return TestClient(agent_server.app)


# ── the SSE parser ───────────────────────────────────────────────────────

class _Stream:
    """Stands in for `httpx.Client.stream(...)`, emitting canned raw lines."""

    def __init__(self, lines, status=200, body=None):
        self._lines = lines
        self.status_code = status
        self._body = body or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self):
        return iter(self._lines)

    def json(self):
        return self._body


def _client_with(stream, monkeypatch):
    import httpx

    class _C:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, *a, **k):
            return stream

    monkeypatch.setattr(httpx, "Client", _C)
    monkeypatch.setattr(channel_chat, "_self_base", lambda: "http://x")


def _frame(kind, **kw):
    return "data: " + json.dumps({"type": kind, **kw})


def test_the_done_frame_wins_over_accumulated_deltas(monkeypatch):
    """`done` carries the complete answer. A stream cut mid-way leaves the
    deltas truncated, so trusting the accumulator alone would send a clipped
    reply and the user would never see the end of the sentence."""
    _client_with(_Stream([
        _frame("started", conversation_id="c1"),
        _frame("status", text="thinking…"),
        _frame("delta", text="Hello"),
        _frame("delta", text=" wor"),
        _frame("done", answer="Hello world", conversation_id="c1"),
    ]), monkeypatch)

    answer, err, _files = channel_chat._ask_agent("hi", "c1")

    assert err == ""
    assert answer == "Hello world"


def test_deltas_alone_still_produce_an_answer(monkeypatch):
    _client_with(_Stream([
        _frame("delta", text="partial "),
        _frame("delta", text="answer"),
    ]), monkeypatch)

    answer, err, _files = channel_chat._ask_agent("hi", "c1")

    assert (answer, err) == ("partial answer", "")


def test_an_error_frame_becomes_an_error_not_a_silent_blank(monkeypatch):
    """Before this, an `error` frame with no deltas produced ("", "") — the
    agent reported success while sending the user nothing."""
    _client_with(_Stream([_frame("error", text="model exploded")]), monkeypatch)

    answer, err, _files = channel_chat._ask_agent("hi", "c1")

    assert answer == ""
    assert "model exploded" in err


def test_a_non_200_carries_the_gateway_reason(monkeypatch):
    _client_with(_Stream([], status=502, body={"error": "gateway unavailable"}),
                 monkeypatch)

    answer, err, _files = channel_chat._ask_agent("hi", "c1")

    assert answer == ""
    assert "502" in err and "gateway unavailable" in err


def test_noise_and_malformed_frames_are_skipped(monkeypatch):
    """Comments, blank lines and unparseable JSON must not abort the answer."""
    _client_with(_Stream([
        ": keep-alive",
        "",
        "data: {not json",
        _frame("delta", text="still "),
        "event: message",
        _frame("delta", text="works"),
        _frame("done", answer="still works"),
    ]), monkeypatch)

    answer, err, _files = channel_chat._ask_agent("hi", "c1")

    assert (answer, err) == ("still works", "")


# ── the trust producer ───────────────────────────────────────────────────

def test_trust_fails_closed_when_the_pairing_store_is_unreachable(monkeypatch):
    """The single most security-relevant line in the feature. Every other test
    stubs `trust_level`, so flipping this to `return "paired"` would turn the
    agent into a free LLM for anyone who can reach it and nothing would fail."""
    import notify

    def _boom(path, timeout=15.0):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(notify, "_gw_get", _boom)

    assert channel_chat.trust_level("telegram", "111") == "unknown"


def test_a_gateway_error_body_is_not_mistaken_for_a_verdict(monkeypatch):
    import notify

    monkeypatch.setattr(notify, "_gw_get",
                        lambda path, timeout=15.0: {"error": "gateway down"})

    assert channel_chat.trust_level("telegram", "111") == "unknown"


def test_an_absent_sender_is_untrusted_before_any_lookup(monkeypatch):
    import notify

    called = []
    monkeypatch.setattr(notify, "_gw_get",
                        lambda *a, **k: called.append(a) or {"trust": "owner"})

    assert channel_chat.trust_level("telegram", "") == "untrusted"
    assert called == [], "trust was consulted for a message with no sender"


def test_a_real_owner_verdict_passes_through(monkeypatch):
    import notify

    monkeypatch.setattr(notify, "_gw_get",
                        lambda path, timeout=15.0: {"trust": "owner"})

    assert channel_chat.trust_level("telegram", "111") == "owner"


# ── delivery outcomes ───────────────────────────────────────────────────

def test_an_undelivered_reply_is_reported_as_an_error(monkeypatch):
    """The mirror of the nudge-worker test that already exists. `delivered:False`
    means the user got nothing; reporting success would hide it."""
    import notify

    monkeypatch.setattr(channel_chat, "_ask_agent", lambda q, c, a=None: ("hi", "", []))
    monkeypatch.setattr(
        notify, "send_channel_message",
        lambda *a, **k: {"delivered": False, "error": "chat not found"})

    out = channel_chat._run_turn("telegram", "111", "hello", "c1")

    assert out["ok"] is True
    assert out["error"] == "chat not found"


def test_an_agent_failure_is_told_to_the_user(monkeypatch):
    import notify

    told = []
    monkeypatch.setattr(channel_chat, "_ask_agent",
                        lambda q, c, a=None: ("", "agent returned HTTP 502", []))
    monkeypatch.setattr(notify, "send_channel_message",
                        lambda ch, text, **k: told.append(text) or {"delivered": True})

    out = channel_chat._run_turn("telegram", "111", "hello", "c1")

    assert out["ok"] is False
    assert told and "could not answer" in told[0]


def test_an_empty_answer_is_not_sent_as_silence(monkeypatch):
    import notify

    told = []
    monkeypatch.setattr(channel_chat, "_ask_agent", lambda q, c, a=None: ("", "", []))
    monkeypatch.setattr(notify, "send_channel_message",
                        lambda ch, text, **k: told.append(text) or {"delivered": True})

    out = channel_chat._run_turn("telegram", "111", "hello", "c1")

    assert told, "the user was sent nothing at all"
    assert out["error"]


def test_a_turn_that_raises_still_tells_the_user(monkeypatch):
    """`_safe_turn`'s apology branch. A crash used to be swallowed, leaving the
    user waiting on a reply that would never come."""
    import notify

    told = []
    monkeypatch.setattr(channel_chat, "_run_turn",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("boom")))
    monkeypatch.setattr(notify, "send_channel_message",
                        lambda ch, text, **k: told.append(text) or {"delivered": True})

    channel_chat._safe_turn("telegram", "111", "hi", "c1")

    assert told and "went wrong" in told[0]
    assert "telegram:111" not in channel_chat._INFLIGHT, (
        "a crashed turn left its in-flight entry behind, blocking the user")


# ── the HTTP routes ──────────────────────────────────────────────────────

def test_the_inbound_route_refuses_an_unpaired_sender(client, monkeypatch):
    """First test to reach the route at all. Deleting it left the suite green."""
    import notify

    monkeypatch.setattr(notify, "_gw_get",
                        lambda path, timeout=15.0: {"trust": "untrusted"})
    turns = []
    monkeypatch.setattr(channel_chat, "_run_turn",
                        lambda *a, **k: turns.append(a))

    r = client.post("/api/channel/inbound", headers=HDR,
                    json={"channel": "telegram", "sender": "999",
                          "text": "are you there?"})

    assert r.status_code == 200
    assert r.json()["accepted"] is False
    assert turns == []


def test_both_inbound_routes_are_the_same_router(client, monkeypatch):
    """Two names for one behaviour, so a fix to one cannot drift from the
    other — which is exactly how the ack and chat paths diverged before."""
    import notify

    monkeypatch.setattr(notify, "_gw_get",
                        lambda path, timeout=15.0: {"trust": "untrusted"})

    a = client.post("/api/channel/inbound", headers=HDR,
                    json={"channel": "telegram", "sender": "999", "text": "hi"})
    b = client.post("/api/ack/inbound", headers=HDR,
                    json={"channel": "telegram", "sender": "999", "text": "hi"})

    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()


def test_the_inbound_route_defaults_the_channel_and_aliases_the_sender(
        client, monkeypatch):
    import notify

    seen = []
    monkeypatch.setattr(notify, "_gw_get",
                        lambda path, timeout=15.0: seen.append(path)
                        or {"trust": "untrusted"})

    r = client.post("/api/channel/inbound", headers=HDR,
                    json={"sender_id": "999", "text": "hi"})

    assert r.status_code == 200
    assert any("telegram" in p for p in seen), (
        "the default channel was not used when the caller omitted it")


def test_a_malformed_inbound_request_is_a_400_not_a_500(client):
    r = client.post("/api/channel/inbound", headers=HDR,
                    json={"channel": "telegram", "text": "no sender"})
    assert r.status_code == 400
    assert "error" in r.json()