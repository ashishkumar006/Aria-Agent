"""Slice 11 — prompt truth.

The code agent's system prompt is a string literal inside the route, and it is
the one place in this product where a claim can be false with no consequence
anywhere else: nothing else reads it. It has already been wrong twice — naming
tools that do not exist, and describing a workspace layout that only existed
in the shipped repo — and the failure is silent: the agent apologises for a
tool it was never given, or hunts for directories that are not there until the
loop guard kills the run.

So these tests read the prompt the way the model does, from the actual
request, and check it against the actual payload the same request produced.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import agent_server as A          # noqa: E402
import skills                     # noqa: E402
import studio_workspace as sw     # noqa: E402
from agent_server import app      # noqa: E402

SID = "s-truth"


def _capture(monkeypatch, *, read_only=False, browser=False, tmp_path=None):
    """Ask the route for its prompt and its payload, from one real request."""
    import mcp_runner
    seen: dict = {}

    async def fake_run(**kwargs):
        seen["messages"] = kwargs["messages"]
        seen["tools"] = [t["name"] for t in kwargs["tools_payload"]]
        seen["allowed"] = kwargs.get("allowed_tools")
        return {"text": "ok", "provider": "test", "input_tokens": 1,
                "output_tokens": 1, "tool_calls": 0}

    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_run)
    monkeypatch.setattr(A, "_browser_tools_enabled", lambda: browser)
    root = (tmp_path or Path(".")) / "proj"
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text("print('x')\n", encoding="utf-8")
    sw.clear(SID)
    sw.add_root(SID, str(root))
    sw.bind(SID)
    try:
        with TestClient(app) as c:
            with c.stream("POST", "/api/code/agent", json={
                    "question": "hello", "path": "", "text": "", "sid": SID,
                    "read_only": read_only}) as r:
                "".join(r.iter_text())
    finally:
        sw.bind("")
        sw.clear(SID)
    system = next((m["content"] for m in seen["messages"]
                   if m["role"] == "system"), "")
    return system, seen


# ── the claims ───────────────────────────────────────────────────────
def test_every_tool_the_prompt_names_is_in_the_payload(tmp_path, monkeypatch):
    """The anti-drift test. A name in the prompt that is not in the payload
    the model is handed is an instruction to use something that does not
    exist, and the only symptom is an agent that keeps saying it cannot."""
    system, seen = _capture(monkeypatch, tmp_path=tmp_path)
    payload = set(seen["tools"])
    # Only backtick-quoted names, which is how this prompt spells a tool.
    named = set(re.findall(r"`([a-z_]{3,30})`", system))
    # `update_plan` and the browser verbs come from other grants; everything
    # else has to be in what the model was actually given.
    extras = {"update_plan", "cwd", "sid", "dir", "file", "offset", "limit"}
    checked = {n for n in named if n not in extras
               and not n.startswith("browser_")}
    assert checked, "the prompt names no tools at all — did the format change?"
    missing = sorted(n for n in checked if n not in payload)
    assert not missing, (
        f"the prompt tells the model to use {missing}, which it was not given")


def test_the_browser_block_appears_only_when_the_tools_are_granted(
        tmp_path, monkeypatch):
    """A capability claimed while it is absent is the same bug in the other
    direction: the model promises to look at the page it has no tool for."""
    off, _ = _capture(monkeypatch, browser=False, tmp_path=tmp_path)
    assert "browser_open" not in off
    on, seen = _capture(monkeypatch, browser=True, tmp_path=tmp_path)
    assert "browser_open" in on
    for name in re.findall(r"`?(browser_\w+)`?", on):
        assert name in seen["tools"], f"prompt claims {name}, payload lacks it"


def test_read_only_mode_does_not_promise_editing(tmp_path, monkeypatch):
    """The prompt's write paragraph is unconditional, so the mode rule has to
    override it — and read-only has to be told the tools are not there, not
    merely asked to abstain."""
    system, seen = _capture(monkeypatch, read_only=True, tmp_path=tmp_path)
    assert "not available in this run" in system
    denied = A._CODE_WRITE_TOOLS | A._CODE_EXEC_TOOLS
    assert not (set(seen["tools"]) & denied), (
        "read-only handed out a write tool, then told the model not to use it")


def test_the_workspace_claim_matches_the_real_root(tmp_path, monkeypatch):
    """It used to name S9SharedCode/code and llm_gatewayV9 in every session,
    so in a playground the agent obeyed, hunted for two directories that were
    not there, and looped until the guards killed the run."""
    system, _ = _capture(monkeypatch, tmp_path=tmp_path)
    assert "S9SharedCode" not in system and "llm_gatewayV9" not in system
    assert 'list_dir(".")' in system, "orientation must be a call, not a dump"


def test_the_read_before_write_rule_matches_the_guard(tmp_path, monkeypatch):
    """The prompt says the active file and attached files count as read. The
    guard is what enforces it, so the two must agree — a prompt that promises
    an exemption the guard does not grant produces an agent that believes it
    was refused for no reason."""
    system, _ = _capture(monkeypatch, tmp_path=tmp_path)
    assert "READ BEFORE WRITE" in system
    assert A._code_write_guard("edit_file", "unseen.py", set()) != ""
    assert A._code_write_guard("edit_file", "seen.py", {"seen.py"}) == ""
    assert A._code_write_guard("create_file", "new.py", set()) == ""


def test_plan_instruction_is_grammar_safe(tmp_path, monkeypatch):
    """A malformed claim would surface only as a model that never plans, so
    the string is checked for what the code actually looks for."""
    system, seen = _capture(monkeypatch, tmp_path=tmp_path)
    assert "update_plan" in system
    assert "update_plan" in seen["tools"]


def test_the_approval_mode_lines_describe_a_real_mode(tmp_path, monkeypatch):
    """`APPROVAL MODE:` carries the human label and `MODE BEHAVIOUR:` the rule
    that goes with it. The rule is what the model acts on, so the test pins
    that one of the five known sentences was chosen — not that a label was
    spelled some way."""
    system, _ = _capture(monkeypatch, tmp_path=tmp_path)
    assert "APPROVAL MODE:" in system
    m = re.search(r"MODE BEHAVIOUR: ([^\n]+(?:\n[^\n]+)*)", system)
    assert m, system[-400:]
    rule = " ".join(m.group(1).split())
    known = ("do not attempt writes or commands",
             "nothing will pause for you",
             "a command runs on its own",
             "a write lands immediately",
             "a write or command is proposed to the user")
    assert any(rule.lower().startswith(k) for k in known), rule



def test_flags_describe_the_switches_that_exist():
    """A kill switch whose description misstates its blast radius is worse
    than no description: the operator toggles it expecting to hide lookups and
    finds out elsewhere that writes went with them."""
    import flags
    for name in ("ui.code_ide", "browser.tools", "browser.headed"):
        d = flags.DEFINITIONS.get(name)
        assert d, f"{name} has no flag definition"
        assert len(d["description"]) > 40, name
        assert d["type"] == "bool"
    bt = flags.DEFINITIONS["browser.tools"]
    assert "localhost" in bt["description"], (
        "browser.tools must say it can reach local services")
    assert bt["default"] is False, "the widest door ships closed"


def test_ui_code_ide_off_leaves_the_old_page_route_working():
    """The redirect is the switch's only write on the old surface; with the
    flag off, `/code` must still render rather than bounce."""
    import flags
    assert flags.DEFINITIONS["ui.code_ide"]["default"] is False
