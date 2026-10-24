"""Dispatch regressions found by a live Notepad run.

Live evidence, before the fix:
  * `replace_text` traced `ok` while the document stayed empty (the driver
    had refused the call with `snapshot_id_required`, and the refusal body
    was ignored);
  * plain `type_text` against modern Notepad (a XAML host) answered
    `effect: unverifiable` + `escalation: foreground` and no text landed;
  * the judge repeated the same action until the loop-guard aborted.

These tests pin the three fixes: token-targeted SetValue, the XAML editable
fallback, and refusal-not-success.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from computer_use.engine import ComputerUseSkill
from computer_use.safety import SafetyGates


class _FakeDaemon:
    def __init__(self, state: dict, results: dict | None = None):
        self.state = state
        self.results = results or {}
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, tool, args=None, timeout=60):
        args = dict(args or {})
        self.calls.append((tool, args))
        if tool in self.results:
            r = self.results[tool]
            return r(args) if callable(r) else r
        if tool == "get_window_state":
            return self.state
        return {"ok": True}


def _skill(monkeypatch) -> ComputerUseSkill:
    monkeypatch.setenv("COMPUTER_USE_ENABLED", "true")
    monkeypatch.setenv("COMPUTER_USE_MODE", "live")
    return ComputerUseSkill(safety=SafetyGates())


def test_replace_text_writes_through_a_fresh_element_token(monkeypatch):
    """SetValue needs element_token; a bare element_index is refused by the
    driver, and dropping the refresh is what made the write a no-op."""
    import computer_use.daemon as D

    state = {"snapshot_id": "snap1", "elements": [
        {"element_index": 9, "element_token": "tok9", "role": "Document",
         "value": "old"}]}
    fd = _FakeDaemon(state, {"set_value": {
        "delivery": {"mode": "unknown"}, "effect": "unverifiable"}})
    monkeypatch.setattr(D, "call", fd)

    skill = _skill(monkeypatch)
    ok = skill._dispatch_action(
        1, 2, {"type": "replace_text", "value": "new text"}, "stale-snapshot")

    assert ok is True
    setv = [a for t, a in fd.calls if t == "set_value"][0]
    assert setv["element_token"] == "tok9"
    # The turn-opening snapshot is stale; the token carries its own.
    assert "snapshot_id" not in setv
    assert not [t for t, _ in fd.calls if t == "type_text"]


def test_type_retries_against_the_editable_element_on_a_xaml_host(monkeypatch):
    """Modern Notepad drops posted WM_CHAR. The driver says so via
    `escalation: foreground`; after that also fails, the engine must target
    the Document element's token instead of aborting the run."""
    import computer_use.daemon as D

    state = {"snapshot_id": "snap2", "elements": [
        {"element_index": 4, "element_token": "tok4", "role": "Document",
         "value": ""}]}
    seen: list[dict] = []

    def type_text(args):
        seen.append(dict(args))
        if "element_token" in args:
            return {"effect": "confirmed", "route": "accessibility",
                    "delivery": {"mode": "background"}}
        return {"effect": "unverifiable", "route": "synthetic_events",
                "escalation": {"target": "foreground"},
                "delivery": {"mode": "background"}}

    fd = _FakeDaemon(state, {"type_text": type_text})
    monkeypatch.setattr(D, "call", fd)

    skill = _skill(monkeypatch)
    ok = skill._dispatch_action(1, 2, {"type": "type", "value": "hello"}, None)

    assert ok is True
    assert "element_token" in seen[-1]
    assert seen[-1]["element_token"] == "tok4"


def test_a_refused_call_is_not_reported_as_success(monkeypatch):
    """A refusal body is an HTTP success. Treating it as ok is how a click
    that never fired still reached the audit log as `done`."""
    import computer_use.daemon as D

    state = {"snapshot_id": "snap3", "elements": []}
    fd = _FakeDaemon(state, {"click": {
        "status": "refused",
        "refusal": {"code": "snapshot_id_required",
                    "message": "snapshot_id_required: pass it"}}})
    monkeypatch.setattr(D, "call", fd)

    skill = _skill(monkeypatch)
    ok = skill._dispatch_action(
        1, 2, {"type": "click", "element_index": 3}, "snap3")

    assert ok is False
    assert "snapshot_id_required" in skill._last_dispatch_error

def _gates_for(monkeypatch, mode: str):
    monkeypatch.setenv("COMPUTER_USE_ENABLED", "true")
    monkeypatch.setenv("COMPUTER_USE_MODE", mode)
    import computer_use.safety as safety
    safety.reset_shared_gates()
    return safety.shared_gates()


def test_dry_run_open_app_never_mints_an_approval(monkeypatch):
    gates = _gates_for(monkeypatch, "dry-run")
    from computer_use.tool import tool_dispatch

    out = tool_dispatch("open_app", {"app": "Spotify"})

    assert out["status"] == "dry-run"
    assert "No approval" in out["message"]
    assert gates.list_approvals() == []


def test_dry_run_sensitive_command_never_mints_an_approval(monkeypatch):
    """A dry-run host describes even approval-worthy commands; a pending
    queue here would be a request the user can approve and still not run."""
    gates = _gates_for(monkeypatch, "dry-run")
    from computer_use.tool import tool_dispatch

    out = tool_dispatch("run_command", {"command": "taskkill /F /PID 1234"})

    assert out["status"] == "dry-run"
    assert gates.list_approvals() == []


def test_live_approval_is_visible_in_the_agent_process(monkeypatch):
    gates = _gates_for(monkeypatch, "live")
    from computer_use.tool import tool_dispatch

    out = tool_dispatch("run_command", {"command": "taskkill /F /PID 1234"})

    assert out["status"] == "pending"
    listed = gates.list_approvals()
    assert [a["id"] for a in listed] == [out["approval_id"]]
    gates.resolve(out["approval_id"], True)
    assert gates.already_approved("run_command",
                                  {"command": "taskkill /F /PID 1234"})


def test_skill_dispatch_owns_computer_action(monkeypatch):
    """Skills must not send computer_action to the MCP child: the approval
    store there is a different object from the one the console reads."""
    _gates_for(monkeypatch, "dry-run")
    import skills

    raw = asyncio.run(skills._computer_tool_dispatch(
        "computer_action", {"action": "open_app",
                            "params": {"app": "Spotify"}}))
    assert json.loads(raw)["status"] == "dry-run"
    assert asyncio.run(skills._computer_tool_dispatch("other", {})) is None


def test_vision_pixel_click_does_not_send_snapshot_id(monkeypatch):
    """The driver treats snapshot_id as an element-handle claim.

    L3 deliberately clicks by x/y, but the old call also passed the turn's
    snapshot_id. cua-driver then refused with
    "snapshot_id requires element_index", so a correct vision target failed.
    """
    import computer_use.engine as engine

    calls: list[tuple[str, dict]] = []

    class FakeSafety:
        mode = "live"

        def needs_approval(self, action, params):
            return False

        def already_approved(self, action, params):
            return True

        def create_approval(self, action, params):
            return "fake"

        def record_action(self, action, params, outcome, ref=""):
            pass

    def fake_call(tool, args=None, timeout=60):
        calls.append((tool, dict(args or {})))
        return {"ok": True}

    monkeypatch.setattr(engine.daemon, "call", fake_call)
    monkeypatch.setattr(engine.layers, "draw_set_of_marks",
                        lambda b64, elements: (b64, []))
    skill = engine.ComputerUseSkill(
        llm_chat=lambda *a, **k: None,
        llm_vision=lambda *a, **k: {
            "verdict": "act",
            "action": {"type": "click", "x": 10, "y": 20}},
        safety=FakeSafety(),
    )
    res = skill._layer_vision(
        "play the song", 1, 2,
        {"screenshot_png_b64": "abc", "snapshot_id": "snap1", "elements": []})

    assert res.success is True
    click = [a for t, a in calls if t == "click"][0]
    assert click["x"] == 10 and click["y"] == 20
    assert "snapshot_id" not in click


def test_drive_app_wires_the_a11y_and_vision_judges(monkeypatch):
    """A chat `drive_app` must get the same LLM judges the DAG path passes.

    Without them the a11y judge returns None, the engine escalates to L3,
    and a usable accessibility tree is misreported as "needs a vision model".
    """
    import skills
    import computer_use.tool as tool

    seen: dict = {}

    class FakeCU:
        def request(self, kind, payload):
            seen["kind"] = kind
            seen["payload"] = payload
            return {"status": "done", "layer": "L2b"}

    def fake_get(**kwargs):
        seen.update(kwargs)
        return FakeCU()

    monkeypatch.setattr(tool, "get_computer_use", fake_get)
    monkeypatch.setattr(tool, "_judge_callbacks",
                        lambda: (skills._v9_judge_chat, skills._v9_judge_vision))

    out = tool.tool_dispatch("drive_app", {"goal": "play a song in Spotify",
                                           "app": "Spotify"})

    assert out["status"] == "done"
    assert seen["kind"] == "drive_app"
    assert seen["payload"]["app"] == "Spotify"
    assert seen["llm_chat"] is skills._v9_judge_chat
    assert seen["llm_vision"] is skills._v9_judge_vision


def test_drive_app_dry_run_is_not_reported_as_done(monkeypatch):
    monkeypatch.setenv("COMPUTER_USE_ENABLED", "true")
    monkeypatch.setenv("COMPUTER_USE_MODE", "dry-run")
    import computer_use
    from computer_use.engine import ComputerResult
    import computer_use.safety as safety
    safety.reset_shared_gates()

    cu = computer_use.ComputerUse()
    cu.skill = type("FakeSkill", (), {
        "run": lambda self, goal, **kw: ComputerResult(
            True, "L2b-dry-run", output={"dry_run_plan": ["would open"]}),
    })()

    out = cu.request("drive_app", {"goal": "open Spotify and play a song"})

    assert out["status"] == "dry-run"
    assert out["layer"] == "L2b-dry-run"
