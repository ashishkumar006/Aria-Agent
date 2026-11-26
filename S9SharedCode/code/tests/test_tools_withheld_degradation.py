"""Graceful degradation of the operator tool guard (Skills page).

Two properties, both about failing SAFE rather than open:

  * `_disabled_tools()` returns the disabled set normally, but returns None
    (fail CLOSED) when the state file is corrupt/unreadable — a broken guard
    must withhold tools, not quietly re-enable everything.
  * `tool_payload()` offers nothing (None) when every requested tool is
    disabled, so a skill that lists tools but has them all switched off fails
    honestly ("tools withheld") instead of answering as if it had run them.

The state dir is redirected to a temp folder, so the real
`state/tools_disabled.json` is never read or written.
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import skills  # noqa: E402
import mcp_runner  # noqa: E402


def _state_dir(monkeypatch, tmp_path, payload):
    d = tmp_path / "state"
    d.mkdir()
    (d / "tools_disabled.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8")
    monkeypatch.setenv("S9_STATE_DIR", str(d))
    return d


class TestDisabledToolsGuard:
    def test_reads_the_disabled_set(self, monkeypatch, tmp_path):
        _state_dir(monkeypatch, tmp_path, {"tools": ["github_issue", "slack_message"]})
        assert skills._disabled_tools() == {"github_issue", "slack_message"}

    def test_no_file_means_nothing_disabled(self, monkeypatch, tmp_path):
        d = tmp_path / "state"
        d.mkdir()
        monkeypatch.setenv("S9_STATE_DIR", str(d))
        assert skills._disabled_tools() == set()

    def test_corrupt_file_fails_closed(self, monkeypatch, tmp_path):
        """A guard that cannot be read must withhold ALL tools (None), never
        fail open and re-enable everything."""
        _state_dir(monkeypatch, tmp_path, "{ this is not json ]")
        assert skills._disabled_tools() is None


class TestToolPayload:
    def test_all_disabled_yields_no_payload(self, monkeypatch, tmp_path):
        _state_dir(monkeypatch, tmp_path, {"tools": ["schedule_task"]})
        # All filtered out -> empty payload (falsy); the caller fails the node.
        assert not skills.tool_payload(["schedule_task"])

    def test_partially_disabled_keeps_the_enabled(self, monkeypatch, tmp_path):
        _state_dir(monkeypatch, tmp_path, {"tools": ["schedule_task"]})
        payload = skills.tool_payload(["schedule_task", "search_knowledge"])
        names = {t.get("name") for t in (payload or [])}
        assert "schedule_task" not in names
        assert "search_knowledge" in names

    def test_guard_unreadable_withholds_all(self, monkeypatch, tmp_path):
        _state_dir(monkeypatch, tmp_path, "not json at all")
        assert skills.tool_payload(["schedule_task", "search_knowledge"]) is None

    def test_empty_request_is_none(self, monkeypatch, tmp_path):
        _state_dir(monkeypatch, tmp_path, {"tools": []})
        assert skills.tool_payload([]) is None


def test_skill_with_all_tools_disabled_fails_honestly(monkeypatch, tmp_path):
    """End to end: the action skill lists tools, but every one is disabled, so
    the node must report an honest 'tools withheld' failure — not a plain
    no-tool answer that reads as a completed run, and not a network call."""
    import yaml
    cfg = yaml.safe_load((ROOT / "agent_config.yaml").read_text(encoding="utf-8"))
    action = skills.Skill("action", cfg["action"])
    # Disable EVERY tool the action skill is allowed to call.
    _state_dir(monkeypatch, tmp_path, {"tools": list(action.tools_allowed or [])})

    # If the guard works, the tool loop is never entered — so a run_with_tools
    # that raises proves no LLM/network call happened.
    async def _must_not_run(**_kw):
        raise AssertionError("run_with_tools was called despite all tools disabled")

    orig = mcp_runner.run_with_tools
    mcp_runner.run_with_tools = _must_not_run
    try:
        nodes = {"n:1": {"skill": "action", "inputs": {}, "status": "running"}}
        result, _ = asyncio.run(skills.run_skill(
            action, "n:1", nodes, "s8-t", "Post the summary to #general.", None))
    finally:
        mcp_runner.run_with_tools = orig

    assert result.success is False
    assert "tools withheld" in (result.error or "").lower() or \
           "no tool made it through" in (result.output or {}).get("error", "").lower()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))