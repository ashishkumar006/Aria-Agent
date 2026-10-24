"""The per-result clip is a harness default, not a law.

The researcher reads evidence pages; a 32k clip cut long articles mid-sentence.
`max_tool_result_chars=0` must let a page through whole, while every other
caller keeps the old cap unless it opts out.
"""
import asyncio

import mcp_runner

BIG = "x" * 100_000


def _tool_call_reply():
    return {"text": "", "tool_calls": [
        {"id": "c1", "name": "fetch_url",
         "arguments": {"url": "https://example.com/long"}}],
        "provider": "test"}


def _run_with_cap(cap):
    seen = {}
    hop = {"n": 0}

    async def chat(messages):
        hop["n"] += 1
        if hop["n"] == 1:
            return _tool_call_reply()
        tool = [m for m in messages if m.get("role") == "tool"][-1]
        seen["content"] = tool["content"]
        return {"text": "done", "tool_calls": [], "provider": "test"}

    async def dispatch(name, args):
        assert name == "fetch_url"
        return BIG

    out = asyncio.run(mcp_runner.run_tool_loop(
        messages=[{"role": "user", "content": "read it"}],
        chat_fn=chat, dispatch_fn=dispatch,
        allowed_tools={"fetch_url"},
        max_tool_result_chars=cap))
    return out, seen["content"]


def test_zero_disables_the_harness_clip():
    out, body = _run_with_cap(0)
    assert out["text"] == "done"
    assert body == BIG, f"page was clipped to {len(body):,} chars"


def test_none_keeps_the_32k_clip():
    out, body = _run_with_cap(None)
    assert out["text"] == "done"
    assert mcp_runner.MAX_TOOL_RESULT_MESSAGE_CHARS < len(body) < 40_000
    assert "truncated" in body.lower()
