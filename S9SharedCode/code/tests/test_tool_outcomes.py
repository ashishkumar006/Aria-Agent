"""Deterministic tool-outcome recording.

`memory.record_outcome()` has existed since Session 7 and had no callers, so
outcomes were only recorded when the model chose to do it itself. These tests
pin the three properties that make the new wiring safe to have on the hot
path: it fires from the tool loop, it never blocks or breaks a run, and it
never persists a secret.
"""
import asyncio
import json

import pytest

import mcp_runner
import outcomes


class _Call:
    """run_tool_loop reads tool calls with .get(), so mirror that shape."""

    def __init__(self, name, args, cid="c1"):
        self._d = {"id": cid, "name": name, "arguments": args}

    def get(self, k, default=None):
        return self._d.get(k, default)


def _reply(*calls, text=""):
    return {"text": text, "tool_calls": list(calls), "provider": "test"}


def _drain_queue():
    while not outcomes._RECORDER.q.empty():
        try:
            outcomes._RECORDER.q.get_nowait()
        except Exception:
            break


# ── it fires from the single choke point ────────────────────────────────────
def test_outcome_callback_fires_once_per_tool_call():
    seen = []
    hops = {"n": 0}

    async def chat(_messages):
        # Tool call first, final text on the next hop.
        hops["n"] += 1
        if hops["n"] == 1:
            return _reply(_Call("web_search", {"query": "x"}))
        return _reply(text="done")

    async def dispatch(name, args):
        return "results"

    out = asyncio.run(mcp_runner.run_tool_loop(
        messages=[{"role": "user", "content": "go"}],
        chat_fn=chat, dispatch_fn=dispatch,
        on_outcome=lambda n, a, ok, t, lat: seen.append((n, ok, t, lat))))
    assert out["text"] == "done", out
    assert len(seen) == 1, seen
    name, ok, text, latency = seen[0]
    assert name == "web_search" and ok is True and text == "results"
    assert latency >= 0.0


def test_failed_tool_calls_are_recorded_as_failures():
    seen = []
    hops = {"n": 0}

    async def chat(_messages):
        hops["n"] += 1
        if hops["n"] == 1:
            return _reply(_Call("fetch_url", {"url": "https://x.example"}))
        return _reply(text="done")

    async def dispatch(name, args):
        raise RuntimeError("boom")

    asyncio.run(mcp_runner.run_tool_loop(
        messages=[{"role": "user", "content": "go"}],
        chat_fn=chat, dispatch_fn=dispatch,
        on_outcome=lambda n, a, ok, t, lat: seen.append((n, ok, t))))
    assert len(seen) == 1, seen
    name, ok, text = seen[0]
    assert ok is False
    assert "boom" in text


def test_a_raising_outcome_callback_cannot_break_the_loop():
    """The whole point of queueing elsewhere is that bookkeeping can never be
    the reason a research node fails."""
    hops = {"n": 0}

    async def chat(_messages):
        hops["n"] += 1
        if hops["n"] == 1:
            return _reply(_Call("list_scheduled", {}))
        return _reply(text="done")

    async def dispatch(name, args):
        return "12:00"

    def _bad(*_a, **_k):
        raise RuntimeError("outcome writer exploded")

    out = asyncio.run(mcp_runner.run_tool_loop(
        messages=[{"role": "user", "content": "go"}],
        chat_fn=chat, dispatch_fn=dispatch, on_outcome=_bad))
    assert out["text"] == "done"
    assert out.get("error") is None


def test_no_callback_is_still_supported():
    hops = {"n": 0}

    async def chat(_messages):
        hops["n"] += 1
        if hops["n"] == 1:
            return _reply(_Call("list_scheduled", {}))
        return _reply(text="ok")

    async def dispatch(name, args):
        return "t"

    out = asyncio.run(mcp_runner.run_tool_loop(
        messages=[], chat_fn=chat, dispatch_fn=dispatch))
    assert out["text"] == "ok"


# ── secrets never reach the store ───────────────────────────────────────────
def test_secret_argument_keys_are_redacted():
    out = outcomes._redact({
        "to": "a@b.com",
        "access_token": "abcdef123456",
        "refresh_token": "zzz",
        "api_key": "k",
        "password": "hunter2",
        "Authorization": "Bearer x",
        "nested": {"client_secret": "s3cr3tvalue", "keep": "yes"},
    })
    blob = json.dumps(out)
    # The KEY names stay (they are needed to interpret the record); the
    # VALUES must not.
    for leak in ("abcdef123456", "hunter2", '"zzz"', "s3cr3tvalue", "Bearer x"):
        assert leak not in blob, f"secret leaked: {leak} in {blob}"
    assert out["access_token"] == "[redacted]"
    assert out["refresh_token"] == "[redacted]"
    assert out["api_key"] == "[redacted]"
    assert out["password"] == "[redacted]"
    assert out["Authorization"] == "[redacted]"
    assert out["nested"]["client_secret"] == "[redacted]"
    assert out["nested"]["keep"] == "yes"       # non-secrets survive
    assert out["to"] == "a@b.com"


def test_token_shaped_values_are_redacted_even_under_a_benign_key():
    """A key called "note" is a classic exfil path: the secret is in the value."""
    out = outcomes._redact({
        "note": "here is my key ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
        "other": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
    })
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in json.dumps(out)
    assert "[redacted]" in out["note"] and "[redacted]" in out["other"]


def test_result_text_is_summarised_not_stored_verbatim():
    """A 20KB fetched page must not be persisted as a memory record; what is
    worth keeping is the shape and any error the tool reported."""
    big = "x" * 30_000
    s = outcomes._summarise_result("fetch_url", big)
    assert len(s) < 400
    assert "30000 chars" in s

    err = outcomes._summarise_result("web_search", '{"error": "all sources throttled"}')
    assert err == "error: all sources throttled"

    exc = outcomes._summarise_result("fetch_url", "tool error: TimeoutError: slow")
    assert "TimeoutError" in exc
    assert outcomes._summarise_result("x", "") == "empty result"


# ── queue behaviour ─────────────────────────────────────────────────────────
def test_record_never_raises_and_does_not_block():
    _drain_queue()
    for i in range(50):
        outcomes.record(tool="web_search", arguments={"query": f"q{i}"},
                        result_text="ok", ok=True, latency_s=0.01,
                        session_id="s1", run_id="s1")
    outcomes.record(tool="", arguments=None)          # empty tool: ignored
    outcomes.record(tool="x" * 5000, arguments={"a": 1})  # junk: swallowed
    st = outcomes.stats()
    assert st["dropped"] == 0, st
    _drain_queue()


def test_queue_drops_rather_than_blocking_when_saturated():
    _drain_queue()
    outcomes._RECORDER.dropped = 0
    # Fill past QUEUE_MAX synchronously; nothing should raise or block.
    for i in range(outcomes.QUEUE_MAX + 25):
        outcomes.record(tool="web_search", arguments={"i": i},
                        result_text="ok", ok=True, latency_s=0.0)
    st = outcomes.stats()
    assert st["dropped"] > 0, "a saturated queue must drop, not block"
    _drain_queue()
    outcomes._RECORDER.dropped = 0
