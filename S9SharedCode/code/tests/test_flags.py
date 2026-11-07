"""Feature flags (Prefab cloud + local fallback).

The Apps > Flags board reads/writes through flags.py. Cloud mode needs a
real PREFAB_API_KEY, so these tests pin S9_STATE_DIR at tmp and exercise
the local path: defaults, typed overrides, persistence, unknown names.
`importlib.reload` rebinds the module-level STATE_DIR per test so suites
never touch live state/flags.json.

Run:  uv run python -m pytest tests/test_flags.py -q
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def flags(tmp_path, monkeypatch):
    monkeypatch.setenv("S9_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("PREFAB_API_KEY", raising=False)
    import flags as f
    importlib.reload(f)
    return f


def test_defaults_without_key(flags):
    assert flags.prefab_status() == {"configured": False, "live": False}
    assert flags.is_enabled("chat.tools") is True
    assert flags.is_enabled("chat.tools", False) is True  # definition wins
    assert flags.is_enabled("nope.unknown", False) is False
    rows = {r["name"]: r for r in flags.all_flags()}
    assert rows["chat.tools"]["source"] == "default"
    assert rows["chat.tools"]["value"] is True


def test_override_roundtrip_and_persistence(flags, tmp_path):
    row = flags.set_override("chat.tools", False)
    assert row == {"name": "chat.tools", "value": False}
    assert flags.is_enabled("chat.tools") is False
    assert (tmp_path / "state" / "flags.json").exists()
    # A fresh import sees the persisted override.
    import flags as f2
    importlib.reload(f2)
    assert f2.is_enabled("chat.tools") is False
    rows = {r["name"]: r for r in f2.all_flags()}
    assert rows["chat.tools"]["source"] == "override"
    assert f2.clear_override("chat.tools") is True
    assert f2.is_enabled("chat.tools") is True
    assert f2.clear_override("chat.tools") is False


def test_override_type_validation(flags):
    assert flags.set_override("chat.tools", "off")["value"] is False
    assert flags.set_override("chat.tools", 1)["value"] is True
    with pytest.raises(ValueError):
        flags.set_override("chat.tools", "maybe")
    with pytest.raises(ValueError):
        flags.set_override("unknown.flag", True)


def test_evaluation_never_raises_without_key(flags, monkeypatch):
    # Even a broken store falls back to the definition default.
    _sd = Path(os.environ["S9_STATE_DIR"])
    _sd.mkdir(parents=True, exist_ok=True)
    (_sd / "flags.json").write_text("{not json", encoding="utf-8")
    assert flags.is_enabled("chat.tools") is True
    assert flags.get_value("chat.tools") is True


def test_pane_hiding_flag_is_defined_and_default_on(flags):
    """The console's pane-hiding kill switch.

    Default-on matters: an operator who never hears of this feature must get
    today's console, and the frontend treats an absent row as enabled so an
    older backend cannot blank out panes it has no opinion about.
    """
    assert flags.is_enabled("ui.pane_hiding") is True
    rows = {r["name"]: r for r in flags.all_flags()}
    assert rows["ui.pane_hiding"]["source"] == "default"
    assert rows["ui.pane_hiding"]["type"] == "bool"


def test_pane_hiding_flag_can_be_switched_off_and_reverted(flags):
    off = flags.set_override("ui.pane_hiding", False)
    assert off["value"] is False
    assert flags.is_enabled("ui.pane_hiding") is False
    # Reverting removes the override rather than storing False, so the flag
    # follows the definition default again and state/flags.json stays sparse.
    assert flags.clear_override("ui.pane_hiding") is True
    assert flags.is_enabled("ui.pane_hiding") is True


def test_authoring_focus_flag_is_defined_and_default_on(flags):
    """Focus is its own page (`/authoring/focus`), and this is its switch.

    Default-on for the same reason as pane hiding: nobody who has never heard
    of the flag may get a different console. The frontend also fails OPEN on a
    flag read that throws, so an unreachable /api/flags cannot make a page the
    user navigated to silently fall back.
    """
    rows = {r["name"]: r for r in flags.all_flags()}
    assert "ui.authoring_focus" in rows, \
        "the console renders a Focus entry point with no switch behind it"
    assert rows["ui.authoring_focus"]["type"] == "bool"
    assert flags.is_enabled("ui.authoring_focus") is True


def test_authoring_focus_can_be_switched_off_and_reverted(flags):
    assert flags.set_override("ui.authoring_focus", False)["value"] is False
    assert flags.is_enabled("ui.authoring_focus") is False
    assert flags.clear_override("ui.authoring_focus") is True
    assert flags.is_enabled("ui.authoring_focus") is True


def test_chat_recall_gate_flag_is_defined_and_default_on(flags):
    """`chat.recall_gate` skips the memory-corpus search for a greeting.

    It is a behaviour change on the request path — a "hi" used to spend a
    35-second search — so it needs a switch that restores the old call exactly.
    Off must mean "recall on every turn", not "recall off".
    """
    rows = {r["name"]: r for r in flags.all_flags()}
    assert "chat.recall_gate" in rows
    assert flags.is_enabled("chat.recall_gate") is True
    assert flags.set_override("chat.recall_gate", False)["value"] is False
    assert flags.is_enabled("chat.recall_gate") is False
    assert flags.clear_override("chat.recall_gate") is True
