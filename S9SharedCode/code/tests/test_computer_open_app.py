"""open_app: a deterministic app launch the computer-use agent can call.

The L2b/L3 cascade used to open an app by hunting the Start menu through the
accessibility tree / vision; when the vision call returned no clickable target
the whole run dead-ended (observed live: "open Notepad" -> L3 -> "vision
returned no clickable target"). `open_app` gives the agent a direct, gated
launch so opening an app never depends on vision.

These tests pin: the schema exposes it, the dispatcher routes it to the gated
shell, a missing app name fails, and dry-run never launches.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from computer_use.prompts import ACTION_SCHEMA_JSON
from computer_use.engine import ComputerUseSkill, _app_from_goal


def _skill(monkeypatch, *, mode="live"):
    import computer_use.safety as safety
    monkeypatch.setenv("COMPUTER_USE_ENABLED", "true")
    monkeypatch.setenv("COMPUTER_USE_MODE", mode)
    safety.reset_shared_gates()
    sk = ComputerUseSkill(llm_chat=lambda *a, **k: None, llm_vision=None)
    sk.safety.mode = mode
    # open_app is not a gated action, but keep the approval check out of the way.
    sk.safety.needs_approval = lambda *a, **k: False
    return sk


def test_schema_exposes_open_app():
    verb = ACTION_SCHEMA_JSON["properties"]["action"]["properties"]["type"]
    assert "open_app" in verb["enum"]
    props = ACTION_SCHEMA_JSON["properties"]["action"]["properties"]
    assert "app" in props, "open_app needs an app-name field"


def test_dispatch_launches_the_app(monkeypatch):
    sk = _skill(monkeypatch)
    calls = []
    monkeypatch.setattr(sk, "shell_open_app",
                        lambda app, **k: calls.append(app) or {"status": "done"})
    ok = sk._dispatch_action(1, 99, {"type": "open_app", "app": "notepad"})
    assert ok is True
    assert calls == ["notepad"]


def test_missing_app_name_fails(monkeypatch):
    sk = _skill(monkeypatch)
    monkeypatch.setattr(sk, "shell_open_app",
                        lambda app, **k: pytest.fail("must not launch with no app"))
    assert sk._dispatch_action(1, 99, {"type": "open_app"}) is False


def test_failed_launch_returns_false(monkeypatch):
    sk = _skill(monkeypatch)
    monkeypatch.setattr(sk, "shell_open_app",
                        lambda app, **k: {"status": "blocked", "message": "denied"})
    assert sk._dispatch_action(1, 99, {"type": "open_app", "app": "x"}) is False


def test_dry_run_describes_without_launching(monkeypatch):
    sk = _skill(monkeypatch, mode="dry-run")
    launched = []
    monkeypatch.setattr(sk, "shell_open_app",
                        lambda app, **k: launched.append(app) or {"status": "done"})
    ok = sk._dispatch_action(1, 99, {"type": "open_app", "app": "notepad"})
    assert ok is True
    assert launched == [], "dry-run must not actually launch the app"


# ── app-name recovery from the goal (when metadata.app is missing) ───────────

class TestAppFromGoal:
    def test_open_notepad(self):
        assert _app_from_goal("Open Notepad, type a proof and save it") == "Notepad"

    def test_open_spotify_lowercase(self):
        assert _app_from_goal("open spotify and play a song") == "spotify"

    def test_launch_two_words_takes_first(self):
        # Conservative: single word only (the launch cascade also title-matches).
        assert _app_from_goal("launch Google Chrome and search") == "Google"

    def test_article_is_not_an_app(self):
        assert _app_from_goal("open a new chrome tab and search") is None

    def test_no_verb_is_none(self):
        assert _app_from_goal("what is 2 + 2?") is None
        assert _app_from_goal("") is None


# ── the judge gets a factual action history (it is otherwise stateless) ──────

class TestJudgeHistory:
    def _capture(self, history):
        seen = {}

        def fake_chat(system, user, schema):
            seen["user"] = user
            return {"verdict": "done"}

        sk = ComputerUseSkill(llm_chat=fake_chat, llm_vision=None)
        sk._judge_a11y("goal", "[0] Button x", 1, "app", None, history=history)
        return seen["user"]

    def test_history_is_included(self):
        u = self._capture(["click 5 -> ok", "type 12 -> ok"])
        assert "HISTORY" in u
        assert "click 5 -> ok" in u
        assert "type 12 -> ok" in u

    def test_no_history_no_block(self):
        assert "HISTORY" not in self._capture([])


# ── a mid-run re-acquire must NOT relaunch an already-running app ────────────

class TestAcquireNoRelaunch:
    def test_disallowed_launch_does_not_relaunch(self, monkeypatch):
        import computer_use.engine as E
        calls = []

        class _FD:
            def __call__(self, tool, args=None, timeout=60):
                calls.append(tool)
                if tool == "get_accessibility_tree":
                    return {"windows": [{"pid": 1, "window_id": 99,
                                         "title": "Calculator"}]}
                return {"ok": True, "apps": []}

        orig = E.daemon.call
        E.daemon.call = _FD()
        try:
            sk = ComputerUseSkill(llm_chat=lambda *a, **k: None, llm_vision=None)
            sk.safety.mode = "live"
            sk.safety.needs_approval = lambda *a, **k: False
            pid, _wid = sk._acquire_target("notepad", allow_launch=False)
        finally:
            E.daemon.call = orig
        # The point: it must NOT relaunch. (It may fall back to an existing
        # window, but it must not spawn a new one.)
        assert "launch_app" not in calls, "relaunched despite allow_launch=False"


# ── change-detection settle (wait for the tree to update, not a fixed sleep) ──

class TestWaitForTreeChange:
    def test_returns_after_change_and_stabilize(self, monkeypatch):
        import computer_use.engine as E
        seq = [{"tree_markdown": "OLD"}, {"tree_markdown": "NEW"},
               {"tree_markdown": "NEW"}]
        calls = {"n": 0}

        class _FD:
            def __call__(self, tool, args=None, timeout=60):
                if tool == "get_window_state":
                    i = min(calls["n"], len(seq) - 1)
                    calls["n"] += 1
                    return seq[i]
                return {"ok": True}

        orig = E.daemon.call
        E.daemon.call = _FD()
        monkeypatch.setattr(E.time, "sleep", lambda *a, **k: None)
        try:
            sk = ComputerUseSkill(llm_chat=lambda *a, **k: None, llm_vision=None)
            sk._wait_for_tree_change(1, 99, {"tree_markdown": "OLD"}, cap_polls=10)
        finally:
            E.daemon.call = orig
        assert calls["n"] >= 3, calls["n"]  # old, new, new(stable)

    def test_caps_when_no_change(self, monkeypatch):
        import computer_use.engine as E
        calls = {"n": 0}

        class _FD:
            def __call__(self, tool, args=None, timeout=60):
                if tool == "get_window_state":
                    calls["n"] += 1
                    return {"tree_markdown": "SAME"}
                return {"ok": True}

        orig = E.daemon.call
        E.daemon.call = _FD()
        monkeypatch.setattr(E.time, "sleep", lambda *a, **k: None)
        try:
            sk = ComputerUseSkill(llm_chat=lambda *a, **k: None, llm_vision=None)
            sk._wait_for_tree_change(1, 99, {"tree_markdown": "SAME"}, cap_polls=5)
        finally:
            E.daemon.call = orig
        assert calls["n"] == 5, "should poll to the cap then give up"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))