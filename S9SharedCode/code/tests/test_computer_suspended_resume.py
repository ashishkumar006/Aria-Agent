"""Suspended-target recovery: empty AX tree -> resume the TARGET pid once.

WHY this exists (observed live): Windows suspends background UWP apps to save
resources. A suspended process still appears in the window list, so the engine
reads it as "running" and L2 escalates — but its accessibility tree is EMPTY, so
it fails with "no clickable target" even though the app is fine, merely
suspended (seen with Settings).

The fix (engine.run, `count == 0` branch): if the target's AX tree is empty,
call `_resume_suspended(pid)` ONCE, re-scan, and only escalate if it is still
empty. It must NEVER resume a different pid (other suspended windows, e.g.
GlobalDesktopSpotlight, are suspended for a reason).

These tests drive the real `ComputerUseSkill.run` loop with a fake daemon and a
fake `_resume_suspended`, so they are daemon-free and deterministic. The
"suspended signature" is an empty `tree_markdown` that still carries a
`snapshot_id` — that is what distinguishes "suspended" from "stale window"
(which has no snapshot_id and takes the re-acquire path instead).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import computer_use.engine as E
from computer_use.engine import ComputerUseSkill, ComputerResult

_POPULATED = "\n".join(f'[{i}] Button "b{i}"' for i in range(8))


def _enable(monkeypatch):
    import os
    os.environ["COMPUTER_USE_ENABLED"] = "true"
    os.environ["COMPUTER_USE_MODE"] = "live"
    import computer_use.safety as safety
    safety.reset_shared_gates()
    E.check_permissions = lambda: E.safety.permissions.PermissionReport(
        binary_present=True, daemon_running=True, ax_ok=True,
        screenshot_ok=True, elevated=False, platform="win32", apps=[])
    # Keep the test fast: the recovery path sleeps 1.0s x3 (settle) + 1.5s.
    monkeypatch.setattr(E.time, "sleep", lambda *_a, **_k: None)


def _judge_click_then_done():
    calls = {"n": 0}

    def judge(system, user, schema):
        calls["n"] += 1
        if calls["n"] > 1:
            return {"verdict": "done"}
        return {"verdict": "act",
                "action": {"type": "click", "element_index": 2}}
    return judge


class _Daemon:
    """get_window_state returns `empty()` until resume flips `populated`."""

    def __init__(self, empty_state, *, populated_after_resume=True):
        self.empty_state = empty_state
        self.populated_after_resume = populated_after_resume
        self.populated = False
        self.calls = []

    def __call__(self, tool, args=None, timeout=60):
        self.calls.append((tool, args))
        if tool == "get_accessibility_tree":
            return {"windows": [{"pid": 1, "window_id": 99,
                                 "title": "Settings"}]}
        if tool == "get_window_state":
            if self.populated and self.populated_after_resume:
                return {"snapshot_id": "s1", "tree_markdown": _POPULATED,
                        "elements": []}
            return dict(self.empty_state)
        if tool == "click":
            return {"ok": True}
        return {"ok": True}


def _run(monkeypatch, daemon, max_turns=4):
    orig = E.daemon.call
    E.daemon.call = daemon
    try:
        skill = ComputerUseSkill(llm_chat=_judge_click_then_done(),
                                 llm_vision=None)
        return skill.run("open settings", app_hint="Settings",
                         max_turns=max_turns)
    finally:
        E.daemon.call = orig


def test_suspended_target_is_resumed_once_and_recovers(monkeypatch):
    """Empty AX (with snapshot_id) -> resume the target once -> re-scan succeeds."""
    _enable(monkeypatch)
    # Empty tree WITH a snapshot_id: passes the stale-window guard, so the
    # engine reaches the count==0 suspended-recovery branch (not re-acquire).
    empty = {"snapshot_id": "s1", "tree_markdown": "", "elements": []}
    daemon = _Daemon(empty)

    resumed: list[int] = []

    def fake_resume(pid):
        resumed.append(pid)
        daemon.populated = True  # the real resume makes the tree populate

    monkeypatch.setattr(ComputerUseSkill, "_resume_suspended",
                        staticmethod(fake_resume))

    res = _run(monkeypatch, daemon)

    assert isinstance(res, ComputerResult)
    # Resumed EXACTLY once, and only the target pid (1) — never a bystander.
    assert resumed == [1], resumed
    # And it actually recovered: the run completed rather than escalating.
    assert res.success is True
    assert res.layer in ("L2b", "L2a", "L1")


def test_resume_is_not_repeated_when_tree_stays_empty(monkeypatch):
    """If the tree is STILL empty after resume, do not resume again — escalate.

    A second resume would mean the engine keeps poking a genuinely-empty (or
    hung) target every turn instead of giving up to L3.
    """
    _enable(monkeypatch)
    empty = {"snapshot_id": "s1", "tree_markdown": "", "elements": []}
    # populated_after_resume=False: resume fires but the tree never fills.
    daemon = _Daemon(empty, populated_after_resume=False)

    resumed: list[int] = []

    def fake_resume(pid):
        resumed.append(pid)

    monkeypatch.setattr(ComputerUseSkill, "_resume_suspended",
                        staticmethod(fake_resume))

    res = _run(monkeypatch, daemon, max_turns=3)

    # The whole point: resume is attempted once, not once per turn.
    assert resumed == [1], resumed
    # The run still terminates (escalates / gives up) rather than hanging.
    assert isinstance(res, ComputerResult)


def test_resume_suspended_is_safe_on_a_bogus_pid(monkeypatch):
    """_resume_suspended must fail soft: a pid with no threads is a no-op.

    It enumerates the target's threads and resumes them; an unknown pid owns no
    threads, so it must return quietly rather than raise into the run loop.
    (On non-Windows there is no kernel32; the blanket except covers that too.)
    """
    # Must not raise for a pid that cannot exist.
    assert ComputerUseSkill._resume_suspended(999_999) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))