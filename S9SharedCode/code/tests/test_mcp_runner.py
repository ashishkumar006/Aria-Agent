"""Tool-loop safety tests (deterministic, no gateway/MCP servers).

Covers the P0-2 guards: verifier-stall detection (same tool+args
repeating), hop-cap loud failure, and per-tool timeouts. The loop is
tested through run_tool_loop with scripted fakes — no subprocesses.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcp_runner as mr


def _tc(name, args=None, tid="t1"):
    return {"id": tid, "name": name, "arguments": args or {}}


class _Script:
    """Scripted chat_fn: pops replies in order, repeats the last forever."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    async def __call__(self, messages):
        self.calls += 1
        if len(self._replies) > 1:
            return self._replies.pop(0)
        return self._replies[0]


class TestRunToolLoop:
    def test_clean_finish_no_tools(self):
        chat = _Script([{"text": "done", "provider": "fake"}])
        out = asyncio.run(mr.run_tool_loop(
            messages=[], chat_fn=chat,
            dispatch_fn=lambda n, a: (_ for _ in ()).throw(
                AssertionError("must not dispatch"))))
        assert out["text"] == "done" and "error" not in out

    def test_tool_then_text(self):
        seen = []

        async def _dispatch(name, args):
            seen.append((name, args))
            return "tool-result"

        chat = _Script([
            {"text": "", "tool_calls": [_tc("list_scheduled")], "provider": "f"},
            {"text": "it is noon", "provider": "f"},
        ])
        out = asyncio.run(mr.run_tool_loop(
            messages=[{"role": "user", "content": "time?"}],
            chat_fn=chat, dispatch_fn=_dispatch))
        assert out["text"] == "it is noon"
        assert seen == [("list_scheduled", {})]

    def test_verifier_stall_aborts(self):
        """Same tool+args 3x → explicit stall error, third call never
        dispatched (no wasted work after the verdict)."""
        dispatched: list = []

        async def _dispatch(name, args):
            dispatched.append((name, args))
            return "same failure again"

        stall = {"text": "", "tool_calls": [_tc("verify_result")],
                 "provider": "f"}
        chat = _Script([stall])
        out = asyncio.run(mr.run_tool_loop(
            messages=[], chat_fn=chat, dispatch_fn=_dispatch))
        assert "error" in out and "verifier stall" in out["error"]
        assert len(dispatched) == mr.MAX_SAME_CALL_REPEATS
        assert chat.calls == mr.MAX_SAME_CALL_REPEATS + 1

    def test_varying_args_do_not_trip_stall(self):
        """Pagination-style varying args must keep working."""
        dispatched: list = []

        async def _dispatch(name, args):
            dispatched.append(args.get("page"))
            return "more"

        async def _chat(messages):
            n = len(dispatched)
            if n >= 4:
                return {"text": "done", "provider": "f"}
            return {"text": "", "tool_calls": [_tc("list", {"page": n})],
                    "provider": "f"}

        out = asyncio.run(mr.run_tool_loop(
            messages=[], chat_fn=_chat, dispatch_fn=_dispatch))
        assert out["text"] == "done" and "error" not in out
        assert dispatched == [0, 1, 2, 3]

    def test_hop_cap_fails_loud(self):
        chat = _Script([{"text": "", "tool_calls": [_tc("loop", {"i": 0})],
                         "provider": "f"}])

        async def _chat(messages):
            # Vary args so the stall guard does not fire first.
            n = len([m for m in messages if m.get("role") == "tool"])
            return {"text": "",
                    "tool_calls": [_tc("loop", {"i": n})], "provider": "f"}

        async def _dispatch(name, args):
            return "again"

        out = asyncio.run(mr.run_tool_loop(
            messages=[], chat_fn=_chat, dispatch_fn=_dispatch, max_hops=3))
        assert "error" in out and "hop cap (3)" in out["error"]

    def test_dispatch_timeout(self, monkeypatch):
        class _Slow:
            async def call_tool(self, name, arguments=None):
                await asyncio.sleep(30)
                return None

        monkeypatch.setattr(mr, "TOOL_CALL_TIMEOUT_S", 0.05)
        out = asyncio.run(mr._dispatch_tool(_Slow(), "slow_tool", {}))
        assert "timed out" in out

    def test_on_event_reports_tool_progress(self):
        """Live progress: one tool_call then one tool_result per call, in
        order, carrying the tool name — the chat SSE status frames."""
        events: list = []

        async def _dispatch(name, args):
            return "tool-result"

        chat = _Script([
            {"text": "", "tool_calls": [_tc("web_search")], "provider": "f"},
            {"text": "here you go", "provider": "f"},
        ])
        out = asyncio.run(mr.run_tool_loop(
            messages=[], chat_fn=chat, dispatch_fn=_dispatch,
            on_event=lambda k, p: events.append((k, p))))
        assert out["text"] == "here you go"
        assert events == [("tool_call", {"name": "web_search"}),
                          ("tool_result", {"name": "web_search", "ok": True})]

    def test_on_event_survives_callback_error(self):
        """A broken progress callback must never break the tool loop —
        streaming is best-effort, the answer still has to come back."""

        def _boom(kind, payload):
            raise RuntimeError("websocket gone")

        async def _dispatch(name, args):
            return "tool-result"

        chat = _Script([
            {"text": "", "tool_calls": [_tc("list_scheduled")], "provider": "f"},
            {"text": "noon", "provider": "f"},
        ])
        out = asyncio.run(mr.run_tool_loop(
            messages=[], chat_fn=chat, dispatch_fn=_dispatch, on_event=_boom))
        assert out["text"] == "noon" and "error" not in out

    def test_dispatch_failure_becomes_tool_text(self):
        """A dispatch that raises is fed back to the model as a tool error
        (the model can recover) instead of aborting the whole turn."""
        messages_seen: list = []

        async def _chat(messages):
            messages_seen.extend(messages)
            if len([m for m in messages if m.get("role") == "tool"]):
                return {"text": "recovered", "provider": "f"}
            return {"text": "", "tool_calls": [_tc("boom")], "provider": "f"}

        async def _dispatch(name, args):
            raise RuntimeError("kaboom")

        out = asyncio.run(mr.run_tool_loop(
            messages=[], chat_fn=_chat, dispatch_fn=_dispatch))
        assert out["text"] == "recovered"
        assert "tool error" in [m.get("content", "") for m in messages_seen
                                if m.get("role") == "tool"][0]


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
