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


def test_view_flags_default_on(flags):
    assert flags.is_enabled("apps.views.table") is True
    assert flags.is_enabled("apps.views.cards") is True
    assert flags.is_enabled("apps.views.prefab") is True
    rows = {r["name"]: r for r in flags.all_flags()}
    assert rows["apps.views.cards"]["source"] == "default"


def test_evaluation_never_raises_without_key(flags, monkeypatch):
    # Even a broken store falls back to the definition default.
    _sd = Path(os.environ["S9_STATE_DIR"])
    _sd.mkdir(parents=True, exist_ok=True)
    (_sd / "flags.json").write_text("{not json", encoding="utf-8")
    assert flags.is_enabled("chat.tools") is True
    assert flags.get_value("chat.tools") is True
