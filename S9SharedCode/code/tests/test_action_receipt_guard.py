"""Receipt verification (P1): an action node that CLAIMS a tool call but
dispatches nothing must fail honestly, not report a fabricated success.

Run: uv run python -m pytest tests/test_action_receipt_guard.py -q
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import skills  # noqa: E402
import mcp_runner  # noqa: E402


def _run_action(fake_reply):
    skill = skills.SkillRegistry().get("action")
    nodes = {"n:1": {"skill": "action", "inputs": {}, "status": "running"}}
    orig = mcp_runner.run_with_tools
    mcp_runner.run_with_tools = fake_reply
    try:
        return asyncio.run(skills.run_skill(
            skill, "n:1", nodes, "s8-t",
            "Schedule yourself to wake in 45 seconds with a goal.", None))
    finally:
        mcp_runner.run_with_tools = orig


def test_narrated_tool_call_without_dispatch_fails_honestly():
    """The model writes {tool_used: schedule_self, result: {ok: true}} but the
    loop dispatches nothing (on_outcome never fires). The receipt guard must
    reject it rather than let the fake schedule id read as success."""

    async def fake_reply(**kw):
        # No on_outcome call => no tool was actually dispatched.
        return {"text": json.dumps({
            "action": "schedule a wake",
            "tool_used": "schedule_self",
            "result": {"ok": True, "scheduled_for": "in 45s"},
            "status": "done",
            "message_to_user": "scheduled"}),
            "provider": "t", "tool_calls": []}

    result, _ = _run_action(fake_reply)
    assert result.success is False
    err = (result.error or "").lower()
    assert "no tool call" in err or "nothing was" in err or "narrat" in err


def test_real_dispatch_is_accepted():
    """When a tool is actually dispatched (on_outcome fires), the guard stays
    out of the way and the node succeeds."""

    async def fake_reply(**kw):
        on_outcome = kw.get("on_outcome")
        if on_outcome:
            on_outcome("schedule_self", {},
                       True, json.dumps({"ok": True, "sleep_id": "sl-abc"}), 0.0)
        return {"text": json.dumps({
            "action": "schedule a wake",
            "tool_used": "schedule_self",
            "result": {"ok": True, "sleep_id": "sl-abc"},
            "status": "done",
            "message_to_user": "scheduled sl-abc"}),
            "provider": "t", "tool_calls": []}

    result, _ = _run_action(fake_reply)
    assert result.success is True, result.error