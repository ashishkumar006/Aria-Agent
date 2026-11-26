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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))