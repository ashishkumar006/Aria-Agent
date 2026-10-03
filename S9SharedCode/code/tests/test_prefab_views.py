"""PrefectHQ/prefab render layer for tracker apps (spike).

Builds component trees from fixture snapshots (no network): metrics,
datatable rows/columns, history sparkline gating. prefab_ui missing →
whole module skipped (spike must never fail the suite).

Run:  uv run python -m pytest tests/test_prefab_views.py -q
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

prefab_ui = pytest.importorskip("prefab_ui")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def pv(tmp_path, monkeypatch):
    monkeypatch.setenv("S9_STATE_DIR", str(tmp_path / "state"))
    import apps as a
    importlib.reload(a)
    import prefab_views as p
    importlib.reload(p)
    return p, a


def _seed(a, monkeypatch):
    a.create_app({"name": "T", "kind": "openrouter_free", "schedule": "manual"})
    payload = {"refreshed_at": 1.0,
               "items": [{"id": "a/1", "name": "A", "context_length": 100},
                         {"id": "b/2", "name": "B", "context_length": 200}],
               "count": 2,
               "added": [{"id": "b/2", "name": "B", "context_length": 200}],
               "removed": [], "error": None,
               "history": [{"ts": 1.0, "count": 1, "added": ["a/1"], "removed": []},
                           {"ts": 2.0, "count": 2, "added": ["b/2"], "removed": []}]}
    dp = Path(a.APPS_DIR) / "t.data.json"
    dp.parent.mkdir(parents=True, exist_ok=True)
    dp.write_text(json.dumps(payload), encoding="utf-8")


def test_tree_shape(pv, monkeypatch):
    p, a = pv
    _seed(a, monkeypatch)
    tree = p.tracker_prefab_tree("t")
    blob = json.dumps(tree)
    assert "DataTable" in blob or "data-table" in blob.lower() or "datatable" in blob.lower()
    assert "a/1" in blob and "b/2" in blob
    assert "Sparkline" in blob or "sparkline" in blob.lower()


def test_tree_unknown_app(pv):
    p, _ = pv
    with pytest.raises(ValueError):
        p.tracker_prefab_tree("nope")


def test_html_bundled(pv, monkeypatch):
    p, a = pv
    _seed(a, monkeypatch)
    html = p.tracker_prefab_html("t")
    assert "<html" in html.lower() and "b/2" in html
