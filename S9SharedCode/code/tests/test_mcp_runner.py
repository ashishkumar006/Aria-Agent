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


def _ledger_hop_test(**kw):
    """Three hops: hop 0 and 1 call tools, hop 2 sees the ledger for both.
    Returns the messages each hop was handed (snapshotted)."""
    seen: list = []

    class _Spy:
        async def __call__(self, messages):
            seen.append(list(messages))
            n = len(seen)
            if n <= 2:
                return {"text": "", "reasoning": "SOME PRIVATE REASONING",
                        "tool_calls": [_tc(f"list_dir{n}", {"path": "."})],
                        "provider": "f"}
            return {"text": "all done", "provider": "f"}

    async def _dispatch(name, args):
        return "TOOL RESULT"

    out = asyncio.run(mr.run_tool_loop(
        messages=[{"role": "user", "content": "what is here?"}],
        chat_fn=_Spy(), dispatch_fn=_dispatch, max_hops=kw.pop("max_hops", 0), **kw))
    return out, seen


class TestProgressLedger:
    """The loop must hand the model a record of what it ALREADY did, or each hop
    re-derives its plan and repeats its own work. Observed live: four identical
    README reads in one turn, each preceded by the same reasoning paragraph."""

    def test_ledger_is_carried_into_the_next_assistant_turn(self):
        out, seen = _ledger_hop_test()
        assert out["text"] == "all done"
        assert len(seen) == 3
        # The FIRST assistant turn is written before any call has completed, so
        # it carries no ledger yet; the second one must.
        assistants = [m for m in seen[2] if m.get("role") == "assistant"]
        assert len(assistants) == 2
        content = assistants[-1]["content"]
        assert "already done this run" in content
        assert "list_dir1(.)" in content
        assert assistants[-1]["tool_calls"], "tool_calls were lost"

    def test_first_assistant_turn_has_no_ledger_yet(self):
        """Nothing has run at that point, so the note must not be invented."""
        _, seen = _ledger_hop_test()
        first = [m for m in seen[1] if m.get("role") == "assistant"][0]
        assert first["content"] == ""
        assert first["tool_calls"]

    def test_tool_result_still_follows_the_assistant_turn(self):
        _, seen = _ledger_hop_test()
        assert [m.get("role") for m in seen[1]] == ["user", "assistant", "tool"]

    def test_reasoning_is_NOT_echoed_back(self):
        """Regression guard. Echoing the free-form reasoning was tried first and
        the model copied it straight back out: every hop's thinking block became
        a superset of the last, all opening with the same sentence (252 -> 1138
        -> 1272 -> 4320 chars). The ledger must carry facts, never the prose."""
        _, seen = _ledger_hop_test()
        for msgs in seen[2:]:
            for m in msgs:
                if m.get("role") == "assistant":
                    assert "SOME PRIVATE REASONING" not in str(m.get("content"))

    def test_ledger_is_bounded(self):
        """A long run must not grow the ledger without limit."""

        class _Spy:
            def __init__(self):
                self.n = 0

            async def __call__(self, messages):
                self.n += 1
                return {"text": "", "provider": "f",
                        "tool_calls": [_tc(f"t{self.n}", {"n": self.n})]}

        seen = []
        spy = _Spy()

        async def _chat(messages):
            seen.append(list(messages))
            return await spy(messages)

        async def _dispatch(name, args):
            return "R"

        asyncio.run(mr.run_tool_loop(
            messages=[{"role": "user", "content": "hi"}],
            chat_fn=_chat, dispatch_fn=_dispatch, max_hops=40))
        last = [m for m in seen[-1] if m.get("role") == "assistant"][-1]
        entries = last["content"].split(";")
        assert len(entries) <= mr.PROGRESS_LEDGER_CALLS + 1
        assert spy.n > mr.PROGRESS_LEDGER_CALLS, "test did not exceed the bound"

    def test_truncated_read_is_reported_with_its_next_offset(self):
        """The whole point of the ledger's outcome column: a read that came back
        cut off must say so, or the model re-issues the identical call hoping for
        the rest (observed live: seven identical README reads, then a blank
        answer)."""

        class _Spy:
            def __init__(self):
                self.n = 0

            async def __call__(self, messages):
                self.n += 1
                return {"text": "", "provider": "f",
                        "tool_calls": [_tc(f"c{self.n}", {"path": "README.md"})]}

        async def _dispatch(name, args):
            return ('{"path": "README.md", "line_count": 663, '
                    '"end_line": 492, "truncated": true, '
                    '"next_offset": 492}')

        seen = []
        spy = _Spy()

        async def _chat(messages):
            seen.append(list(messages))
            return await spy(messages)

        asyncio.run(mr.run_tool_loop(
            messages=[{"role": "user", "content": "read it"}],
            chat_fn=_chat, dispatch_fn=_dispatch, max_hops=3))
        content = [m for m in seen[-1] if m.get("role") == "assistant"][-1]["content"]
        assert "continue at offset=492" in content

    def test_stopped_with_no_answer_says_why(self):
        """A run the guard stops carries its reason on `note`; the caller turns
        that into something the user can see rather than a blank reply."""

        class _Spy:
            async def __call__(self, messages):
                return {"text": "", "provider": "f",
                        "tool_calls": [_tc("read_file", {"path": "README.md"})]}

        async def _dispatch(name, args):
            return "SAME RESULT EVERY TIME"

        out = asyncio.run(mr.run_tool_loop(
            messages=[{"role": "user", "content": "read it"}],
            chat_fn=_Spy(), dispatch_fn=_dispatch, max_hops=0))
        assert "already-successful" in (out.get("note") or "")
        assert not out["text"].strip(), "the model produced no answer here"


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
        # A FAILING tool is a genuine stall and must still abort.
        async def _boom(name, args):
            dispatched.append((name, args))
            raise RuntimeError("nope")

        out = asyncio.run(mr.run_tool_loop(
            messages=[], chat_fn=chat, dispatch_fn=_boom))
        assert "error" in out and "verifier stall" in out["error"]
        assert len(dispatched) == mr.MAX_SAME_CALL_REPEATS
        assert chat.calls == mr.MAX_SAME_CALL_REPEATS + 1

    def test_repeating_a_successful_call_keeps_the_result(self):
        """Not a stall: the model already has what it needs.

        Observed live - render_document returned 200 and stored the PDF, the
        model called it again unchanged, and the old guard aborted the loop.
        The node then died holding a real document and the receipt reported
        nothing. Repeating a call that succeeded must stop the loop quietly and
        keep the result.
        """
        dispatched: list = []

        async def _dispatch(name, args):
            dispatched.append((name, args))
            return "rendered ok"

        stall = {"text": "", "tool_calls": [_tc("render_document")],
                 "provider": "f"}
        chat = _Script([stall])
        out = asyncio.run(mr.run_tool_loop(
            messages=[], chat_fn=chat, dispatch_fn=_dispatch))
        assert "error" not in out, out
        assert "already-successful" in (out.get("note") or "")
        assert len(dispatched) <= mr.MAX_SAME_CALL_REPEATS + 1

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
        order, carrying the tool name AND the call id — the chat SSE status
        frames. The id lets the panel update one activity row in place instead
        of rendering the call and its result as two."""
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
        assert events == [("tool_call", {"name": "web_search",
                                          "arguments": {}, "id": "t1"}),
                          ("tool_result", {"name": "web_search",
                                            "ok": True,
                                            "result": "tool-result",
                                            "id": "t1"})]

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

    def test_parallel_tool_calls_run_concurrently(self):
        """Several calls in ONE turn are dispatched together, not one by one.

        Reading matching files is the common case: the wall-clock cost must be
        the slowest read, not the sum. Three 50 ms reads finish in well under
        the 150 ms a serial loop would take.
        """
        import time

        async def _dispatch(name, args):
            await asyncio.sleep(0.05)
            return f"r:{args.get('path')}"

        chat = _Script([
            {"text": "", "provider": "f", "tool_calls": [
                _tc("read_file", {"path": "a"}, "1"),
                _tc("read_file", {"path": "b"}, "2"),
                _tc("read_file", {"path": "c"}, "3")]},
            {"text": "all read", "provider": "f"},
        ])
        msgs: list = []
        t0 = time.perf_counter()
        out = asyncio.run(mr.run_tool_loop(
            messages=msgs, chat_fn=chat, dispatch_fn=_dispatch))
        elapsed = time.perf_counter() - t0
        assert out["text"] == "all read"
        assert elapsed < 0.12, f"looked serial ({elapsed:.3f}s for 3x50ms)"
        # Results ride back in the assistant's call order, so the tool messages
        # line up with the tool_call ids the model sent.
        assert [m["content"] for m in msgs if m.get("role") == "tool"] == \
            ["r:a", "r:b", "r:c"]
        assert [m["tool_call_id"] for m in msgs if m.get("role") == "tool"] == \
            ["1", "2", "3"]

    def test_parallel_events_report_the_batch(self):
        """Both calls are announced before either result, so a parallel batch
        reads as a batch in the transcript."""
        events: list = []

        async def _dispatch(name, args):
            return "ok"

        chat = _Script([
            {"text": "", "provider": "f", "tool_calls": [
                _tc("read_file", {"path": "a"}, "1"),
                _tc("search_files", {"pattern": "x"}, "2")]},
            {"text": "done", "provider": "f"},
        ])
        out = asyncio.run(mr.run_tool_loop(
            messages=[], chat_fn=chat, dispatch_fn=_dispatch,
            on_event=lambda k, p: events.append((k, p.get("name")))))
        assert out["text"] == "done"
        assert events[:2] == [("tool_call", "read_file"),
                              ("tool_call", "search_files")]
        assert {e for e in events[2:]} == {("tool_result", "read_file"),
                                           ("tool_result", "search_files")}

    def test_unlimited_hops_run_to_completion(self):
        """max_hops=0 is no ceiling: a long, genuinely progressing run finishes
        with its answer instead of dying on a hop cap."""
        state = {"n": 0}

        async def _chat(messages):
            state["n"] += 1
            if state["n"] >= 15:
                return {"text": "finished", "provider": "f"}
            # Vary args so neither guard fires: real forward motion.
            return {"text": "", "provider": "f",
                    "tool_calls": [_tc("read_file", {"path": f"f{state['n']}"},
                                       str(state["n"]))]}

        async def _dispatch(name, args):
            return "body"

        out = asyncio.run(mr.run_tool_loop(
            messages=[], chat_fn=_chat, dispatch_fn=_dispatch, max_hops=0))
        assert out["text"] == "finished", out

    def test_no_progress_run_stops_instead_of_spinning_forever(self):
        """With no hop cap, a run that never gets a successful result must still
        stop: this is the backstop that replaces the fixed ceiling."""
        async def _chat(messages):
            # A DIFFERENT failing call each hop: varying args defeat the repeat
            # guard, so only the no-progress guard can end this.
            n = len([m for m in messages if m.get("role") == "tool"])
            return {"text": "", "provider": "f",
                    "tool_calls": [_tc("read_file", {"path": f"x{n}"}, str(n))]}

        async def _dispatch(name, args):
            raise RuntimeError("nope")

        out = asyncio.run(mr.run_tool_loop(
            messages=[], chat_fn=_chat, dispatch_fn=_dispatch, max_hops=0))
        assert "error" in out and "no successful tool result" in out["error"]


class TestTextReplayedToolCalls:
    """Gemini without a thoughtSignature gets its call replayed as prose.

    The gateway substitutes `[tool call] name({...})` for a functionCall it
    cannot sign, and a small model then copies that line back as its answer.
    The loop used to return it as the final text and never dispatch anything.
    """

    def test_prose_call_is_dispatched_and_not_returned_as_the_answer(self):
        replies = [
            {"text": '[tool call] computer_action({"action": "open_app", '
                     '"params": {"app": "Spotify"}})',
             "provider": "gemini-3", "tool_calls": []},
            {"text": "Spotify is open.", "provider": "gemini-3"},
        ]
        seen: list[tuple[str, dict]] = []

        async def _dispatch(name, args):
            seen.append((name, args))
            return '{"ok": true}'

        out = asyncio.run(mr.run_tool_loop(
            messages=[{"role": "user", "content": "play a song"}],
            chat_fn=_Script(replies), dispatch_fn=_dispatch, max_hops=4))
        assert seen == [("computer_action",
                         {"action": "open_app",
                          "params": {"app": "Spotify"}})]
        assert out["text"] == "Spotify is open."

    def test_empty_stream_is_retried_on_the_batched_path(self):
        """A stream can complete with zero text and zero calls; that is not an
        answer. The loop must retry once without streaming."""
        calls = {"stream": 0, "batched": 0}

        async def _stream(messages, on_text=None):
            calls["stream"] += 1
            return {"text": "", "tool_calls": [], "provider": "f"}

        async def _chat(messages):
            calls["batched"] += 1
            return {"text": "a real answer", "provider": "f"}

        out = asyncio.run(mr.run_tool_loop(
            messages=[], chat_fn=_chat, dispatch_fn=lambda n, a: "x",
            chat_stream_fn=_stream, max_hops=2))
        assert calls == {"stream": 1, "batched": 1}
        assert out["text"] == "a real answer"

    def test_empty_reply_twice_is_reported_not_dressed_up(self):
        async def _chat(messages):
            return {"text": "", "tool_calls": [], "provider": "f"}

        out = asyncio.run(mr.run_tool_loop(
            messages=[], chat_fn=_chat, dispatch_fn=lambda n, a: "x",
            max_hops=2))
        assert out["text"] == ""
        assert "empty reply twice" in out["error"]

    def test_marker_inside_ordinary_prose_is_not_a_call(self):
        text = 'Use [tool call] open_app({"app": "Spotify"}) to open it.'
        replies = [{"text": text, "provider": "f", "tool_calls": []}]
        seen: list[str] = []

        async def _dispatch(name, args):
            seen.append(name)
            return "{}"

        out = asyncio.run(mr.run_tool_loop(
            messages=[], chat_fn=_Script(replies), dispatch_fn=_dispatch,
            max_hops=2))
        assert seen == []
        assert out["text"] == text


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
