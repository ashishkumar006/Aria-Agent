"""The computer-use skill's TWO routes, and the gate they share.

The gated route exists because the approval queue was unreachable: the
cascade drives a GUI, and a GUI click is not approval-worthy, so nothing
the agent did could ever produce the PENDING approval the Computer page
resolves. A shell command CAN be approval-worthy, and it is the right
instrument for "run this command" anyway.

So:
  - no `metadata.action`  -> the cascade (GUI driving)
  - `metadata.action`     -> the gated shell, and a pending approval for
                             anything sensitive
"""
import asyncio
import sys
import pathlib

import pytest

AGENT = pathlib.Path(r"C:\Users\AISHWARYA\Downloads\project3\S9SharedCode\code")
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

import skills  # noqa: E402


class _Result:
    """A stand-in for AgentResult, so the test can call the branch
    directly without an orchestrator, a graph or a gateway."""

    def __init__(self, success=False, output=None, error="", **kw):
        self.success = success
        self.output = output or {}
        self.error = error
        self.agent_name = "computer"
        self.elapsed_s = 0.0
        self.cost = 0.0
        for k, v in kw.items():
            setattr(self, k, v)


def _run_computer_branch(monkeypatch, node_meta, request_result=None):
    """Run the computer branch of run_skill with a stubbed engine and a
    stubbed gated request, and return the AgentResult."""
    calls = {"request": [], "engine": 0}

    class _FakeEngine:
        def __init__(self, *a, **k):
            pass

        def run(self, goal, app_hint=None, max_turns=12, record=False):
            calls["engine"] += 1
            raise AssertionError("the cascade must not run for a gated action")

    import computer_use.engine as engine_mod
    monkeypatch.setattr(engine_mod, "ComputerUseSkill", _FakeEngine)

    def _fake_request(kind, payload):
        calls["request"].append((kind, payload))
        return dict(request_result or {"status": "pending",
                                       "approval_id": "cu-1-1",
                                       "message": "Action requires your approval."})

    import computer_use
    monkeypatch.setattr(computer_use, "get_computer_use",
                        lambda session_id="s8-default": type(
                            "C", (), {"request": staticmethod(_fake_request)})())

    skill = skills.SkillRegistry().get("computer")
    monkeypatch.setattr(skills, "_disabled_tools", lambda: set())
    monkeypatch.setattr(skills, "_computer_cost_reset", lambda: None)
    monkeypatch.setattr(skills, "_computer_cost_snapshot", lambda: {"total_tokens": 0})

    graph_nodes = {"n:1": {"skill": "computer", "inputs": [], "status": "running",
                           "metadata": node_meta}}
    out, _rendered = asyncio.run(skills.run_skill(
        skill, "n:1", graph_nodes, "s8-test",
        "run a command on my machine", None))
    return out, calls


def test_a_gated_action_uses_the_shell_and_not_the_cascade(monkeypatch):
    """The command route must not fall through to GUI driving.

    A cascade run for "run python --version" clicks into a terminal window
    and types the command - slow, fragile, and it never asks a human.
    """
    out, calls = _run_computer_branch(
        monkeypatch,
        {"goal": "run python --version",
         "action": "run_command",
         "args": {"command": "python --version"}})
    assert calls["engine"] == 0, "the GUI cascade ran for a shell action"
    assert calls["request"] == [("run_command", {"command": "python --version"})]
    assert out.output.get("layer") == "gated-run_command"
    assert out.output.get("status") == "pending"


def test_a_pending_approval_is_not_a_failure(monkeypatch):
    """`pending` means "waiting for a human", not "the run broke".

    The Computer page resolves the approval; a run that reported this as a
    failure would be retried by the recovery planner, and every retry
    mints another approval.
    """
    out, _calls = _run_computer_branch(
        monkeypatch,
        {"goal": "run python --version", "action": "run_command",
         "args": {"command": "python --version"}},
        request_result={"status": "pending", "approval_id": "cu-9-1",
                        "message": "Action requires your approval."})
    assert out.success is True
    assert out.output.get("approval_id") == "cu-9-1"
    assert "approval" in (out.output.get("message") or "").lower()


def test_a_blocked_action_is_reported_as_a_failure(monkeypatch):
    out, _calls = _run_computer_branch(
        monkeypatch,
        {"goal": "format the disk", "action": "run_command",
         "args": {"command": "format c:"}},
        request_result={"status": "blocked",
                        "message": "matches denylist pattern 'format'"})
    assert out.success is False
    assert out.output.get("status") == "blocked"
    assert "denylist" in (out.output.get("message") or "")


def test_a_node_without_an_action_still_uses_the_cascade(monkeypatch):
    """The GUI route is unchanged: no metadata.action -> the cascade."""
    seen = {}

    class _RealEngine:
        def __init__(self, *a, **k):
            pass

        def run(self, goal, app_hint=None, max_turns=12, record=False):
            seen["goal"] = goal
            seen["app"] = app_hint
            return type("R", (), {"success": True, "layer": "L2b",
                                  "output": {}, "trace": [], "error": ""})()

    import computer_use.engine as engine_mod
    monkeypatch.setattr(engine_mod, "ComputerUseSkill", _RealEngine)
    monkeypatch.setattr(skills, "_disabled_tools", lambda: set())
    monkeypatch.setattr(skills, "_computer_cost_reset", lambda: None)
    monkeypatch.setattr(skills, "_computer_cost_snapshot",
                        lambda: {"total_tokens": 5})
    monkeypatch.setattr(skills, "_v9_judge_chat", lambda *a, **k: None)
    monkeypatch.setattr(skills, "_v9_judge_vision", lambda *a, **k: None)

    skill = skills.SkillRegistry().get("computer")
    graph_nodes = {"n:1": {"skill": "computer", "inputs": [], "status": "running",
                           "metadata": {"goal": "open Notepad",
                                        "app": "Notepad"}}}
    out, _rendered = asyncio.run(skills.run_skill(
        skill, "n:1", graph_nodes, "s8-test", "open Notepad", None))
    assert seen.get("goal") == "open Notepad"
    assert seen.get("app") == "Notepad"
    assert out.output.get("layer") == "L2b"


def test_an_unknown_action_is_not_treated_as_a_gated_action(monkeypatch):
    """Only the four known actions take the shell route.

    An unknown action name must not reach the shell: it would be a way to
    call something else with the gate's authority.
    """
    seen = {"engine": 0}

    class _RealEngine:
        def __init__(self, *a, **k):
            pass

        def run(self, goal, app_hint=None, max_turns=12, record=False):
            seen["engine"] += 1
            return type("R", (), {"success": True, "layer": "L2b",
                                  "output": {}, "trace": [], "error": ""})()

    import computer_use.engine as engine_mod
    monkeypatch.setattr(engine_mod, "ComputerUseSkill", _RealEngine)
    monkeypatch.setattr(skills, "_disabled_tools", lambda: set())
    monkeypatch.setattr(skills, "_computer_cost_reset", lambda: None)
    monkeypatch.setattr(skills, "_computer_cost_snapshot",
                        lambda: {"total_tokens": 0})

    called = []

    class _FakeCU:
        def request(self, kind, payload):
            called.append((kind, payload))
            return {"status": "done"}

    import computer_use
    monkeypatch.setattr(computer_use, "get_computer_use",
                        lambda session_id="s8-default": _FakeCU())

    skill = skills.SkillRegistry().get("computer")
    graph_nodes = {"n:1": {"skill": "computer", "inputs": [], "status": "running",
                           "metadata": {"goal": "do something",
                                        "action": "exec_anything",
                                        "args": {"cmd": "rm -rf /"}}}}
    asyncio.run(skills.run_skill(
        skill, "n:1", graph_nodes, "s8-test", "do something", None))
    assert called == [], "an unknown action reached the gated shell"
    assert seen["engine"] == 1
