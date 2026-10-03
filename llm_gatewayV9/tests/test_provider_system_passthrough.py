"""Inline system turns must reach the model on EVERY provider.

The original defect was found on Gemini: `_translate_messages` dropped every
inline `role:"system"` message, because Gemini's `contents` only accepts
user/model roles. Aria's persona and its retrieved-document block both arrive
as inline system turns, so on Gemini the document was logged, billed and
counted in `prompt_chars` - and the model answered "I don't have access to your
menu" to a question whose answer was in the same prompt.

A follow-up audit found the SAME conditional drop in the OpenAI-compatible and
Ollama adapters (`if not system_text: append(...)` then `continue`), plus a
third instance: the Gemini cache-strip retry rebuilt `systemInstruction` from
`system_blocks` alone, so the first provider that 400s on a cache silently
restored the original bug and still returned 200.

These tests assert on the OUTBOUND body, not on the request that was sent.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import providers as P  # noqa: E402

PERSONA = "You are Aria."
RETRIEVED = (
    "REFERENCE MATERIAL RETRIEVED FROM THE USER'S UPLOADED DOCUMENTS\n"
    "[1] menu.pdf (p. 1)\n- Monday: Idli, Dosa")
QUESTION = "what is the menu on monday"

MESSAGES = [
    {"role": "system", "content": PERSONA},
    {"role": "system", "content": RETRIEVED},
    {"role": "user", "content": QUESTION},
]


def _compat():
    return P.OpenAICompatProvider.__new__(P.OpenAICompatProvider)


def _ollama():
    return P.OllamaProvider.__new__(P.OllamaProvider)


def _gemini():
    return P.GeminiProvider.__new__(P.GeminiProvider)


# ── the shared helper ────────────────────────────────────────────────────────
def test_helper_collects_system_turns_in_order():
    assert P._inline_system_turns(MESSAGES) == [PERSONA, RETRIEVED]


def test_helper_drops_empty_and_non_dict_entries():
    msgs = [{"role": "system", "content": "  "},
            {"role": "system", "content": None},
            {"role": "system", "content": PERSONA},
            {"role": "user", "content": "hi"},
            "not a dict",
            None]
    assert P._inline_system_turns(msgs) == [PERSONA]


def test_helper_flattens_multimodal_system_content():
    msgs = [{"role": "system",
             "content": [{"type": "text", "text": PERSONA}]}]
    assert P._inline_system_turns(msgs) == [PERSONA]


# ── OpenAI-compatible ────────────────────────────────────────────────────────
def test_compat_keeps_inline_system_when_there_is_no_system_blocks():
    out = _compat()._translate_messages(MESSAGES, "")
    joined = " ".join(m["content"] for m in out if m["role"] == "system")
    assert PERSONA in joined, out
    assert RETRIEVED in joined, out


def test_compat_keeps_inline_system_when_system_blocks_is_also_present():
    """The exact shape that used to lose both the persona and the document."""
    out = _compat()._translate_messages(MESSAGES, "TOP-LEVEL SYSTEM")
    joined = " ".join(m["content"] for m in out if m["role"] == "system")
    assert "TOP-LEVEL SYSTEM" in joined, out
    assert PERSONA in joined, out
    assert RETRIEVED in joined, out
    assert [m["role"] for m in out] == ["system", "user"], out


def test_compat_system_appears_before_the_question():
    out = _compat()._translate_messages(MESSAGES, "TOP-LEVEL")
    assert out[0]["role"] == "system"
    assert out[-1]["content"] == QUESTION


def test_compat_adds_no_system_message_when_there_is_none():
    out = _compat()._translate_messages([{"role": "user", "content": "hi"}], "")
    assert [m["role"] for m in out] == ["user"], out


# ── Ollama ───────────────────────────────────────────────────────────────────
def test_ollama_keeps_inline_system_when_system_blocks_is_also_present():
    out = _ollama()._translate_messages(MESSAGES, "TOP-LEVEL SYSTEM",
                                        prompted_fallback=False)
    joined = " ".join(m["content"] for m in out if m["role"] == "system")
    assert "TOP-LEVEL SYSTEM" in joined, out
    assert PERSONA in joined, out
    assert RETRIEVED in joined, out


def test_ollama_keeps_inline_system_without_system_blocks():
    out = _ollama()._translate_messages(MESSAGES, "",
                                        prompted_fallback=False)
    joined = " ".join(m["content"] for m in out if m["role"] == "system")
    assert PERSONA in joined and RETRIEVED in joined, out


# ── Gemini ───────────────────────────────────────────────────────────────────
def test_gemini_helper_matches_the_shared_one():
    assert _gemini()._inline_system(MESSAGES) == [PERSONA, RETRIEVED]


def test_gemini_contents_never_carries_a_system_role():
    contents = _gemini()._translate_messages(MESSAGES)
    assert [c["role"] for c in contents] == ["user"], contents


# ── the Gemini cache-strip retry ─────────────────────────────────────────────
def _gemini_with_cache(cache_name="cachedContents/abc", create=1200):
    g = _gemini()
    g.model = "gemini-3.5-flash-lite"
    g.api_key = "k"
    g.base_url = "https://example.invalid"
    g.name = "gemini-test"
    g._headers = lambda: {}

    class _Store:
        async def get_or_create(self, key, model, text, base_url):
            return cache_name, create

    g.cache_store = _Store()
    return g


@pytest.mark.asyncio
async def test_the_cache_strip_retry_keeps_the_inline_system(monkeypatch):
    """The retry that removed `cachedContent` rebuilt the instruction from
    `system_blocks` alone, so the persona and the retrieved document vanished on
    exactly the requests that hit a cache error - and the retry still returned
    200, so nothing was logged."""
    sent = []

    class _Resp:
        def __init__(self, status, payload=None, text=""):
            self.status_code = status
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            sent.append(copy.deepcopy(json))
            if len(sent) == 1:
                # 400 mentioning the cache: forces the strip-and-retry path.
                return _Resp(400, text="cachedContent is not supported")
            return _Resp(200, {"candidates": [
                {"content": {"parts": [{"text": "ok"}], "role": "model"}}]})

    monkeypatch.setattr(P, "httpx", type("H", (), {"AsyncClient": _Client}))
    g = _gemini_with_cache()

    await g.chat(MESSAGES, max_tokens=32,
                 system_blocks=[{"text": "S" * 4000, "cache": True}])

    assert len(sent) == 2, sent
    for i, body in enumerate(sent):
        parts = (body.get("systemInstruction") or {}).get("parts") or []
        instr = "\n\n".join(p.get("text", "") for p in parts)
        assert PERSONA in instr, f"attempt {i + 1}: {instr[:200]}"
        assert RETRIEVED in instr, f"attempt {i + 1}: {instr[:200]}"
    assert "cachedContent" in sent[0]
    assert "cachedContent" not in sent[1], sent[1].keys()


# ── Gemini tool round-trip ───────────────────────────────────────────────────
def test_gemini_tool_result_is_labelled_with_the_function_that_was_called():
    """`functionResponse.name` has to match the preceding `functionCall`.
    The canonical tool message carries only `tool_call_id` + `content`, so
    without this lookup Gemini received `name: "tool"` next to a call for
    `web_search` and rejected the turn."""
    msgs = [
        {"role": "user", "content": "find it"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_abc", "name": "web_search", "arguments": {"q": "x"}}]},
        {"role": "tool", "tool_call_id": "call_abc", "content": "RESULT"},
    ]
    contents = _gemini()._translate_messages(msgs)
    resp = contents[-1]["parts"][0]["function_response"]
    assert resp["name"] == "web_search", resp
    assert resp["response"] == {"text": "RESULT"}, resp


def test_gemini_tool_result_prefers_an_explicit_name():
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_abc", "name": "web_search", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "call_abc", "tool_name": "override",
         "content": "R"},
    ]
    contents = _gemini()._translate_messages(msgs)
    assert contents[-1]["parts"][0]["function_response"]["name"] == "override"


def test_gemini_tool_result_without_any_name_still_sends_something():
    msgs = [{"role": "tool", "tool_call_id": "unknown", "content": "R"}]
    contents = _gemini()._translate_messages(msgs)
    assert contents[-1]["parts"][0]["function_response"]["name"] == "tool"


@pytest.mark.parametrize("raw,want_obj", [
    ("42", True), ('"hello"', True), ("true", True), ("[1,2]", True),
    ("plain text", True), ({"a": 1}, True),
])
def test_gemini_tool_response_is_always_an_object(raw, want_obj):
    """`functionResponse.response` must be a Struct. A bare number, boolean or
    array used to pass through and produce an invalid body -> 400 -> 502, while
    the same result worked everywhere else."""
    obj = P._coerce_obj(raw)
    assert isinstance(obj, dict), (raw, obj)
    assert want_obj


def test_gemini_tool_response_preserves_a_real_object():
    assert P._coerce_obj('{"a": 1}') == {"a": 1}
    assert P._coerce_obj({"a": 1}) == {"a": 1}


def test_gemini_tool_response_wraps_rather_than_drops():
    assert P._coerce_obj("42") == {"result": 42}
    assert P._coerce_obj("[1,2]") == {"result": [1, 2]}
    assert P._coerce_obj("plain text") == {"text": "plain text"}
