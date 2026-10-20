"""Gemini 3 requires a thoughtSignature on every replayed functionCall part.

Measured live against gemini-3.1/3.5-flash-lite during a standard research run:
a parallel batch of six fetch_url calls came back with a signature on the
first part only (and sometimes with none at all). Replaying that turn on the
next hop returned

    400 Function call is missing a thought_signature in functionCall parts

which the router surfaced as a 502, so the researcher/author node died at 0.0s
and the run degraded - twice in a row, on the same plan shape. Sending a prior
signature for the unsigned parts was accepted (200) on the exact request that
400'd as received, so the translation fills the gaps.

These tests pin that translation, not the network behaviour.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from providers import GeminiProvider  # noqa: E402


def _provider():
    return GeminiProvider.__new__(GeminiProvider)


def _call(cid, name, args=None, sig=None):
    tc = {"id": cid, "name": name, "arguments": args or {}}
    if sig:
        tc["provider_meta"] = {"thoughtSignature": sig}
    return tc


def _turn(calls):
    return {"role": "assistant", "content": "", "tool_calls": calls}


def _result(cid):
    return {"role": "tool", "tool_call_id": cid, "content": '{"ok":true}'}


def test_one_signature_covers_the_whole_parallel_batch():
    msgs = [
        {"role": "user", "content": "search"},
        _turn([_call("c1", "web_search", sig="SIG-A"),
               _call("c2", "web_search"),
               _call("c3", "web_search")]),
        _result("c1"), _result("c2"), _result("c3"),
        {"role": "user", "content": "continue"},
    ]
    contents = _provider()._translate_messages(msgs)
    calls = [p for c in contents for p in c["parts"] if "functionCall" in p]
    assert len(calls) == 3
    assert [p.get("thoughtSignature") for p in calls] == ["SIG-A"] * 3


def test_a_later_unsigned_turn_borrows_the_newest_prior_signature():
    msgs = [
        {"role": "user", "content": "search"},
        _turn([_call("c1", "web_search", sig="SIG-A")]),
        _result("c1"),
        _turn([_call("c2", "fetch_url"), _call("c3", "fetch_url")]),
        _result("c2"), _result("c3"),
        {"role": "user", "content": "answer"},
    ]
    contents = _provider()._translate_messages(msgs)
    calls = [p for c in contents for p in c["parts"] if "functionCall" in p]
    assert [p.get("thoughtSignature") for p in calls] == ["SIG-A"] * 3


def test_no_signature_anywhere_replays_the_calls_as_text():
    """The author's first tool batch can come back with no signature at all,
    and there is then nothing to borrow. An unsigned functionCall is a 400, so
    the call and its result become prose instead of being replayed as a call.
    """
    msgs = [
        {"role": "user", "content": "read it"},
        _turn([_call("c1", "read_artifact", {"id": "a1"})]),
        _result("c1"),
    ]
    contents = _provider()._translate_messages(msgs)
    assert not [p for c in contents for p in c["parts"] if "functionCall" in p]
    assert not [p for c in contents for p in c["parts"] if "functionResponse" in p]
    texts = "\n".join(p.get("text", "") for c in contents for p in c["parts"])
    assert "[tool call] read_artifact" in texts, texts
    assert "[tool result]" in texts, texts


def test_text_parts_and_tool_results_get_no_signature():
    msgs = [
        {"role": "user", "content": "hi"},
        _turn([_call("c1", "web_search", sig="SIG-A")]),
        _result("c1"),
    ]
    contents = _provider()._translate_messages(msgs)
    for c in contents:
        for p in c["parts"]:
            if "functionCall" not in p:
                assert "thoughtSignature" not in p, p
