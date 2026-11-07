"""The streamed tool call has to arrive whole, with its signature.

`/api/chat/simple/stream` and every DAG node that streams go through
`gateway.stream_chat`, which rebuilds OpenAI-shaped `tool_call_delta` fragments
into complete calls before the loop ever sees one. Gemini 3 puts a
`thoughtSignature` on the functionCall parts it returns and 400s on any turn
replayed without one — a 502 that kills the node — so the signature has to
survive the same trip. It has no field in OpenAI's fragment shape, so the
gateway sends it as a sibling key and the client files it under
`provider_meta`, which is where the provider's `_translate_messages` already
looks.

Before the Gemini provider had a real `stream`, none of this was reachable: the
buffered path returned calls whole from `chat()`, signatures included.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import gateway  # noqa: E402


class _Resp:
    def __init__(self, lines, status=200):
        self._lines = lines
        self.status_code = status

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self):
        return b"upstream said no"


class _CM:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _Client:
    def __init__(self, resp):
        self._resp = resp

    def stream(self, method, url, json=None, headers=None):
        return _CM(self._resp)


def _events(lines):
    import contextlib

    @contextlib.contextmanager
    def _session(timeout=30.0):
        yield _Client(_Resp(lines))

    old = gateway.async_session
    gateway.async_session = _session
    try:
        async def _go():
            return [e async for e in gateway.LLM().stream_chat(
                messages=[{"role": "user", "content": "look it up"}],
                tools=[{"name": "search_web"}], agent="chat",
                session="ct-stream-sig")]
        return asyncio.run(_go())
    finally:
        gateway.async_session = old


def _frame(**kw):
    return "data: " + json.dumps(kw)


def test_answer_text_streams_through_as_text_events():
    ev = _events([_frame(delta="Hel"), _frame(delta="lo"), _frame(done=True,
                                                                  provider="gemini")])
    assert [e["type"] for e in ev] == ["text", "text", "done"]
    assert "".join(e["text"] for e in ev if e["type"] == "text") == "Hello"


def test_a_streamed_call_is_handed_over_whole_not_in_fragments():
    """The loop must never see a half-built call: `arguments` arrives as
    partial JSON keyed by index, and a dispatch on the first fragment would run
    a tool with a truncated payload."""
    frag1 = json.dumps([{"index": 0, "id": "c1", "type": "function",
                         "function": {"name": "search_web",
                                      "arguments": '{"q":'}}])
    frag2 = json.dumps([{"index": 0, "type": "function",
                         "function": {"arguments": '"weather"}'}}])
    ev = _events([_frame(tool_call_delta=frag1), _frame(tool_call_delta=frag2),
                  _frame(done=True, provider="gemini")])
    calls = [e for e in ev if e["type"] == "tool_calls"]
    assert len(calls) == 1, [e["type"] for e in ev]
    tc = calls[0]["tool_calls"][0]
    assert tc["name"] == "search_web"
    assert tc["arguments"] == {"q": "weather"}, tc
    # Two fragments, one complete call, handed out only at the end.
    assert len(ev) == 2, ev


def test_the_gemini_thought_signature_survives_the_stream():
    """Without this the NEXT hop is a functionCall with no signature, which
    Gemini 400s on, and the run dies with a 502 blaming the provider."""
    ev = _events([
        _frame(tool_call_delta=json.dumps(
            [{"index": 0, "id": "c1", "type": "function",
              "function": {"name": "read_url", "arguments": '{"u":"x"}'},
              "thought_signature": "sig-9f2c"}])),
        _frame(done=True, provider="gemini")])
    tc = [e for e in ev if e["type"] == "tool_calls"][0]["tool_calls"][0]
    assert tc["provider_meta"] == {"thoughtSignature": "sig-9f2c"}, tc


def test_an_unsigned_call_keeps_provider_meta_empty():
    """The OpenAI-compatible providers send no signature. Inventing one there
    would be a lie the next provider has to honour."""
    ev = _events([
        _frame(tool_call_delta=json.dumps(
            [{"index": 0, "id": "c1", "type": "function",
              "function": {"name": "search_web", "arguments": "{}"}}])),
        _frame(done=True)])
    tc = [e for e in ev if e["type"] == "tool_calls"][0]["tool_calls"][0]
    assert tc["provider_meta"] is None, tc


def test_parallel_calls_keep_their_own_ids_indexes_and_signatures():
    ev = _events([
        _frame(tool_call_delta=json.dumps([
            {"index": 0, "id": "a", "type": "function",
             "function": {"name": "one", "arguments": "{}"},
             "thought_signature": "sig-a"},
            {"index": 1, "id": "b", "type": "function",
             "function": {"name": "two", "arguments": "{}"}}])),
        _frame(done=True)])
    calls = [e for e in ev if e["type"] == "tool_calls"][0]["tool_calls"]
    assert [c["name"] for c in calls] == ["one", "two"], calls
    assert calls[0]["provider_meta"] == {"thoughtSignature": "sig-a"}
    assert calls[1]["provider_meta"] is None


def test_an_error_frame_raises_rather_than_yielding_a_short_answer():
    import pytest
    with pytest.raises(RuntimeError):
        _events([_frame(delta="par"), _frame(error="gemini 503")])
