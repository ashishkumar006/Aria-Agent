"""An empty tool result must not kill the whole request.

Found live during a standard research run: all four Gemini aliases were cooling
down, the router fell back to Kilo (`cohere/north-mini-code:free`), and the
researcher node died with

    400 Provider returned error: invalid request: all elements in
    tool_results must have the 'outputs' property specified

Reproduced against the live gateway with a two-message tool turn: `content: ""`
is a 502, `content: "{}"` is a 200. The tool had simply returned nothing, so the
translation now says so instead of sending an empty result.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from providers import OpenAICompatProvider  # noqa: E402


def _provider():
    return OpenAICompatProvider.__new__(OpenAICompatProvider)


def _turn(content):
    return [
        {"role": "user", "content": "read"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "name": "read_artifact", "arguments": {"id": "a1"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": content},
    ]


def test_empty_tool_result_is_replaced():
    out = _provider()._translate_messages(_turn(""), "")
    tool = [m for m in out if m["role"] == "tool"]
    assert len(tool) == 1
    assert tool[0]["content"].strip(), tool


def test_real_tool_result_is_unchanged():
    out = _provider()._translate_messages(_turn('{"text":"hi"}'), "")
    tool = [m for m in out if m["role"] == "tool"]
    assert tool[0]["content"] == '{"text":"hi"}'
