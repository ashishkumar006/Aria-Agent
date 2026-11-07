"""P0 coding-agent upgrades: modes, read gate, brief, plan and verification."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_server as ag  # noqa: E402
import mcp_runner  # noqa: E402


def test_read_only_mode_hides_writes_and_exec_tools():
    full = set(ag._code_allowed_tools(False))
    read_only = set(ag._code_allowed_tools(True))
    assert "edit_file" in full and "run_command" in full
    assert "edit_file" not in read_only
    assert "run_command" not in read_only
    assert "read_file" in read_only
    assert "list_dir" in read_only


def test_read_before_write_guard():
    assert "read_file" in ag._code_write_guard("edit_file", "a.py", set())
    assert ag._code_write_guard("edit_file", "a.py", {"a.py"}) == ""
    assert ag._code_write_guard("create_file", "new.py", set()) == ""
    assert ag._code_write_guard("make_dir", "newdir", set()) == ""
    assert ag._code_write_guard("read_file", "a.py", set()) == ""


def test_plan_normalisation():
    steps = ag._normalise_plan([
        {"id": "1", "title": "Read the module", "status": "done"},
        "Add the endpoint",
        {"title": "Run checks", "status": "bogus"},
        {"status": "pending"},
    ])
    assert [s["title"] for s in steps] == [
        "Read the module", "Add the endpoint", "Run checks"]
    assert steps[0]["status"] == "done"
    assert steps[1]["status"] == "pending"
    assert steps[2]["status"] == "pending"
    assert ag._normalise_plan(None) == []
    assert ag.PLAN_TOOL_SCHEMA["input_schema"]["required"] == ["steps"]


def test_workspace_brief_includes_rules_scripts_and_editor_context(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "AGENTS.md").write_text(
        "# Rules\nAlways run pytest before claiming success.\n", encoding="utf-8")
    (root / "README.md").write_text("A sample project.", encoding="utf-8")
    (root / "package.json").write_text(json.dumps({
        "scripts": {"test": "vitest run", "build": "vite build"}}),
        encoding="utf-8")
    (root / "src").mkdir()
    brief = ag._code_workspace_brief(
        root, tabs=["ws/src/app.ts"],
        diagnostics=[{"path": "ws/src/app.ts", "line": 4,
                      "message": "unused variable"}])
    assert "Always run pytest" in brief
    assert "vitest run" in brief
    assert "src/" in brief
    assert "ws/src/app.ts" in brief
    assert "unused variable" in brief


def test_fast_verify_python_and_json(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "ok.py").write_text("x = 1\n", encoding="utf-8")
    (root / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    (root / "bad.json").write_text("{nope}", encoding="utf-8")
    (root / "notes.md").write_text("# notes\n", encoding="utf-8")

    good = ag._code_fast_verify(["ok.py"], root)
    assert good["ok"] is True and good["checked"] == 1

    py = ag._code_fast_verify(["bad.py"], root)
    assert py["ok"] is False and py["checked"] == 1
    assert "line" in py["error"].lower() or "syntax" in py["error"].lower()

    js = ag._code_fast_verify(["bad.json"], root)
    assert js["ok"] is False and js["checked"] == 1

    client_path = ag._code_fast_verify(["ws/ok.py"], root)
    assert client_path["ok"] is True

    md = ag._code_fast_verify(["notes.md"], root)
    assert md["checked"] == 0
    assert md["ok"] is False
    assert "not verified" in md["error"]


def test_approval_callback_can_state_a_refusal_reason():
    dispatched = []

    async def dispatch(name, args):
        dispatched.append(name)
        return "written"

    async def approve(name, args):
        return "read a.py first - this run has not read that file"

    seen = {"calls": 0}

    async def chat(messages):
        seen["calls"] += 1
        if seen["calls"] == 1:
            return {
                "text": "",
                "tool_calls": [{"id": "c1", "name": "edit_file",
                                "arguments": {"path": "a.py"}}],
                "provider": "test", "input_tokens": 0, "output_tokens": 0,
            }
        return {"text": "I will read it first.", "tool_calls": [],
                "provider": "test", "input_tokens": 0, "output_tokens": 0}

    messages = [{"role": "user", "content": "edit a.py"}]
    out = asyncio.run(mcp_runner.run_tool_loop(
        messages=messages, chat_fn=chat, dispatch_fn=dispatch,
        approve_fn=approve, max_hops=4))
    assert dispatched == []
    tool_text = "\n".join(
        str(m.get("content") or "") for m in messages if m.get("role") == "tool")
    assert "read a.py first" in tool_text
    assert out.get("text") == "I will read it first."
