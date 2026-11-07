"""Gemini streams.

`GeminiProvider` inherited `BaseProvider.stream`, which is a buffered fallback:
it runs the whole non-streaming call and yields the answer once. Every Gemini
turn therefore went dark until the model had finished and delivered one lump —
measured live at 11.8 s for a one-sentence question and 24.2 s for a two-hop
one, with exactly one `delta` frame at the end. The OpenAI-compatible providers
already streamed properly, which is why only the Gemini-backed surface felt
stuck.

These tests drive the provider against a canned SSE body so the contract is
pinned without a network call: answer text as it arrives, thinking on its own
channel, and a functionCall that keeps the thought signature the next hop
requires.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

GW = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GW))

import providers  # noqa: E402


class _CacheStore:
    def __init__(self, name=None):
        self._name = name

    async def get_or_create(self, api_key, model, text, base_url):
        return (self._name, 0 if self._name else 0)


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj)


def _text(t: str, thought: bool = False) -> dict:
    p = {"text": t}
    if thought:
        p["thought"] = True
    return {"candidates": [{"content": {"parts": [p], "role": "model"}}]}


def _call(name: str, args: dict, sig: str | None = None) -> dict:
    part = {"functionCall": {"name": name, "args": args}}
    if sig:
        part["thoughtSignature"] = sig
    return {"candidates": [{"content": {"parts": [part], "role": "model"},
                            "finishReason": "STOP"}]}


class _Resp:
    def __init__(self, lines=(), status=200, text=""):
        self._lines = list(lines)
        self.status_code = status
        self.text = text

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self):
        return self.text.encode()

    async def aclose(self):
        return None


class _CM:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _Client:
    """Records the streamed request bodies; hands back canned responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.bodies: list[dict] = []
        self.urls: list[str] = []

    def stream(self, method, url, json=None, headers=None):
        assert method == "POST"
        self.bodies.append(json or {})
        self.urls.append(url)
        return _CM(self.responses.pop(0))


def _drive(provider, client, **kw):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session(timeout=None):
        yield client

    old = providers.upstream_session
    providers.upstream_session = _session
    try:
        out = asyncio.run(_collect(provider, **kw))
    finally:
        providers.upstream_session = old
    return out


async def _collect(provider, **kw):
    return [c async for c in provider.stream(
        kw.pop("messages", [{"role": "user", "content": "hi"}]), **kw)]


def _provider():
    return providers.GeminiProvider("k", "gemini-3.5-flash-lite", _CacheStore())


# ── the behaviour that was missing ──────────────────────────────────────────

def test_answer_text_is_yielded_chunk_by_chunk_not_as_one_lump():
    client = _Client([_Resp([_sse(_text("Hel")), _sse(_text("lo the")),
                             _sse(_text("re."))])])
    out = _drive(_provider(), client)
    assert out == ["Hel", "lo the", "re."], out
    assert len(out) > 1, (
        "one lump is the buffered fallback this replaced, not a stream")


def test_the_streamed_endpoint_is_streamgeneratecontent():
    client = _Client([_Resp([_sse(_text("ok"))])])
    _drive(_provider(), client)
    assert ":streamGenerateContent" in client.urls[0], client.urls[0]
    assert "alt=sse" in client.urls[0], client.urls[0]


def test_thinking_text_is_not_part_of_the_answer():
    """A `thought: true` part is chain of thought. On the answer channel it
    would be quoted, spoken, and counted as the reply."""
    client = _Client([_Resp([_sse(_text("reasoning here", thought=True)),
                             _sse(_text("the answer"))])])
    out = _drive(_provider(), client)
    assert out[0].startswith("[[REASONING_DELTA]] ")
    assert json.loads(out[0][len("[[REASONING_DELTA]] "):]) == "reasoning here"
    assert out[1] == "the answer"
    assert "reasoning here" not in out[1]


def test_a_function_call_keeps_the_thought_signature_it_needs_next_hop():
    """Gemini 400s on a replayed functionCall whose parts lack a
    thought_signature, and the router surfaces that as a 502 that kills the
    node. The signature has no field in OpenAI's fragment shape, so it rides as
    a sibling key and the client files it under `provider_meta`."""
    client = _Client([_Resp([_sse(_call("search_web", {"q": "weather"},
                                       sig="sig-abc-123"))])])
    out = _drive(_provider(), client)
    frags = [o for o in out if o.startswith("[[TOOL_CALL_DELTA]] ")]
    assert len(frags) == 1, out
    frag = json.loads(frags[0][len("[[TOOL_CALL_DELTA]] ") :])[0]
    assert frag["function"]["name"] == "search_web"
    assert json.loads(frag["function"]["arguments"]) == {"q": "weather"}
    assert frag["thought_signature"] == "sig-abc-123", frag
    assert frag["index"] == 0 and frag["id"]


def test_two_parallel_calls_keep_separate_indexes():
    one = {"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "a", "args": {}}, "thoughtSignature": "s"},
        {"functionCall": {"name": "b", "args": {}}}]}}]}
    client = _Client([_Resp([_sse(one)])])
    out = _drive(_provider(), client)
    frags = [json.loads(o[len("[[TOOL_CALL_DELTA]] "):])[0]
             for o in out if o.startswith("[[TOOL_CALL_DELTA]]")]
    assert [f["index"] for f in frags] == [0, 1], frags
    assert [f["function"]["name"] for f in frags] == ["a", "b"]
    # A call the model unsigned must not invent a signature it never returned.
    assert "thought_signature" not in frags[1]


# ── failure and switch behaviour ────────────────────────────────────────────

def test_an_empty_stream_is_not_reported_as_a_completed_call():
    """The buffered path had no guard here and the route logged `ok` with zero
    output tokens 10,075 times — each one a turn the agent then paid for
    again."""
    from providers import ProviderError
    client = _Client([_Resp([])])
    with pytest.raises(ProviderError) as e:
        _drive(_provider(), client)
    assert e.value.retryable is True


def test_a_500_fails_without_retrying_the_body():
    from providers import ProviderError
    client = _Client([_Resp([], status=500, text="boom")])
    with pytest.raises(ProviderError):
        _drive(_provider(), client)
    assert len(client.urls) == 1, "the retry budget is for 400s, not 5xx"


def test_a_400_retries_once_with_the_same_simplifications_as_the_batched_call():
    client = _Client([_Resp([], status=400, text="thinking not supported"),
                      _Resp([_sse(_text("ok"))])])
    out = _drive(_provider(), client, reasoning="off")
    assert out == ["ok"], out
    assert len(client.bodies) == 2
    assert "thinkingConfig" not in (client.bodies[1].get("generationConfig") or {}), \
        "the retry must simplify, not re-send what just failed"


def test_the_kill_switch_restores_the_buffered_answer(monkeypatch):
    """Off means the provider behaves exactly as it did before: one chunk, from
    the non-streaming call, with no SSE request on the wire."""
    monkeypatch.setenv("GATEWAY_GEMINI_STREAM", "0")
    p = _provider()
    seen = {}

    async def _fake_chat(messages, **kw):
        seen["kw"] = kw
        return {"text": "one lump"}

    monkeypatch.setattr(p, "chat", _fake_chat)
    out = asyncio.run(_collect(p))
    assert out == ["one lump"], out
    assert seen, "the buffered fallback goes through the non-streaming call"


def test_inline_system_turns_survive_the_stream(monkeypatch):
    """`contents` cannot carry a system role, so the batched call folds inline
    system turns into `systemInstruction` — without which the persona and the
    retrieved document were discarded, billed, and answered from the model's
    own weights. The stream builds its request through the same code, and this
    is what proves it did not fork."""
    client = _Client([_Resp([_sse(_text("ok"))])])
    msgs = [{"role": "system", "content": "Aria is the assistant."},
            {"role": "user", "content": "what is on my menu?"}]
    _drive(_provider(), client, messages=msgs)
    body = client.bodies[0]
    instr = json.dumps(body.get("systemInstruction") or {})
    assert "Aria is the assistant." in instr, body.keys()
    assert not [c for c in body["contents"]
                if (c.get("parts") or [{}])[0].get("text", "").startswith(
                    "Aria is")], "the system turn must not also be a user turn"
