"""A completed tool loop must survive an MCP teardown fault.

Live research runs failed nodes with

    ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)

from the anyio task group inside the MCP stdio session - at `elapsed_s 0.0`,
alongside a tool loop that had already produced its answer. The session's
`async with` closes AFTER the loop returns, so a teardown fault used to
discard a successful answer and fail the node. An answer in hand outranks a
teardown fault; a failure with no answer is still a failure.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcp_runner
from flow import _readable_exc


class _FakeStdio:
    async def __aenter__(self):
        return object(), object()

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        raise BaseExceptionGroup("unhandled errors in a TaskGroup",
                                 [RuntimeError("transport closed")])

    async def initialize(self):
        return None


def _install(monkeypatch, loop):
    monkeypatch.setattr(mcp_runner, "stdio_client", lambda *a, **k: _FakeStdio())
    monkeypatch.setattr(mcp_runner, "ClientSession", _FakeSession)
    monkeypatch.setattr(mcp_runner, "run_tool_loop", loop)
    monkeypatch.setattr(mcp_runner, "breaker_check", lambda: None)
    monkeypatch.setattr(mcp_runner, "breaker_reset", lambda: None)


def test_readable_exc_unwraps_the_taskgroup_and_skips_cancellation():
    inner = RuntimeError("stdio child exited")
    group = BaseExceptionGroup(
        "unhandled errors in a TaskGroup",
        [asyncio.CancelledError(), BaseExceptionGroup("nested", [inner])])
    text = _readable_exc(group)
    assert "RuntimeError: stdio child exited" in text
    assert "TaskGroup" not in text


def test_teardown_fault_after_a_completed_loop_returns_the_answer(monkeypatch):
    async def loop(**kwargs):
        return {"text": "the answer", "tool_calls": []}

    _install(monkeypatch, loop)
    result = asyncio.run(mcp_runner.run_with_tools(
        prompt="hi", tools_payload=[], agent="researcher", session_id="s"))
    assert result["text"] == "the answer"


def test_a_model_failure_does_not_condemn_the_mcp_child(monkeypatch):
    """A gateway/model error is not evidence the tool child is broken. Counting
    it opened the breaker and turned one bad answer into "tools unavailable"
    for every run in the next 30 seconds."""
    tripped: list[str] = []

    async def loop(**kwargs):
        raise RuntimeError("gateway refused")

    _install(monkeypatch, loop)
    monkeypatch.setattr(mcp_runner, "breaker_trip", lambda why: tripped.append(why))
    with pytest.raises(RuntimeError):
        asyncio.run(mcp_runner.run_with_tools(
            prompt="hi", tools_payload=[], agent="researcher", session_id="s"))
    assert tripped == [], tripped


def test_a_failed_handshake_still_trips_the_breaker(monkeypatch):
    """The breaker exists for exactly this: a child that cannot start."""
    tripped: list[str] = []

    class _DeadSession(_FakeSession):
        async def initialize(self):
            raise RuntimeError("handshake failed")

    monkeypatch.setattr(mcp_runner, "stdio_client", lambda *a, **k: _FakeStdio())
    monkeypatch.setattr(mcp_runner, "ClientSession", _DeadSession)

    async def loop(**kwargs):                 # pragma: no cover - never reached
        return {"text": "unreachable"}

    monkeypatch.setattr(mcp_runner, "run_tool_loop", loop)
    monkeypatch.setattr(mcp_runner, "breaker_check", lambda: None)
    monkeypatch.setattr(mcp_runner, "breaker_reset", lambda: None)
    monkeypatch.setattr(mcp_runner, "breaker_trip", lambda why: tripped.append(why))

    with pytest.raises(RuntimeError):
        asyncio.run(mcp_runner.run_with_tools(
            prompt="hi", tools_payload=[], agent="researcher", session_id="s"))
    assert tripped and "handshake failed" in tripped[0], tripped
