"""Config-driven tracker apps (Apps page).

Specs validate loudly (→ HTTP 400/409), the runner filters + diffs, and
failures are recorded per-app instead of raised. Network is monkeypatched
— no live HTTP in tests. S9_STATE_DIR is pinned at tmp and the module
reloaded per test so suites never touch live state/apps/.

Run:  uv run python -m pytest tests/test_apps.py -q
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def apps(tmp_path, monkeypatch):
    monkeypatch.setenv("S9_STATE_DIR", str(tmp_path / "state"))
    import apps as a
    importlib.reload(a)
    return a


def _or_fixture():
    return {"data": [
        {"id": "a/free", "name": "A", "context_length": 100,
         "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "b/paid", "name": "B", "context_length": 200,
         "pricing": {"prompt": "0.001", "completion": "0.002"}},
        {"id": "c/noprice", "name": "C", "context_length": 300},
    ]}


def test_validate_rejects_bad_specs(apps):
    with pytest.raises(ValueError):
        apps.validate_spec({"name": "x", "kind": "nope"})
    with pytest.raises(ValueError):
        apps.validate_spec({"kind": "openrouter_free"})
    with pytest.raises(ValueError):
        apps.validate_spec({"name": "x", "kind": "openrouter_free",
                            "schedule": "sometimes"})
    with pytest.raises(ValueError):
        apps.validate_spec({"name": "x", "kind": "json_feed",
                            "url": "ftp://x"})
    with pytest.raises(ValueError):
        apps.validate_spec({"name": "x", "kind": "json_feed",
                            "url": "https://x", "match": {"field": "a"}})


def test_feed_url_guard_refuses_internal_targets(apps):
    """A spec URL is fetched server-side, so it must not reach
    loopback / link-local / private targets (the gateway, cloud
    metadata, the LAN) or carry embedded credentials."""
    from apps import _guard_feed_url
    for bad in (
        "http://127.0.0.1:8109/v1/providers",
        "http://localhost/feed",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5/feed",
        "http://192.168.1.1/feed",
        "http://[::1]/feed",
        "https://user:pass@example.com/feed",
        "ftp://example.com/feed",
    ):
        with pytest.raises(ValueError):
            _guard_feed_url(bad)
    # A public literal IP passes (no DNS involved).
    _guard_feed_url("https://93.184.216.34/feed")


def test_schedule_interval_floor(apps):
    """"every 30s" is a valid scheduler interval but would fetch a
    third-party feed every second of every day."""
    with pytest.raises(ValueError, match=">= 60s"):
        apps.validate_spec({"name": "Fast", "kind": "openrouter_free",
                            "schedule": "every 30s"})
    s = apps.validate_spec({"name": "OK", "kind": "openrouter_free",
                            "schedule": "every 30m"})
    assert s["schedule"] == "every 30m"


def test_app_count_quota(apps, monkeypatch):
    monkeypatch.setattr(apps, "_MAX_APPS", 2)
    apps.create_app({"name": "One", "kind": "openrouter_free"})
    apps.create_app({"name": "Two", "kind": "openrouter_free"})
    with pytest.raises(ValueError, match="app limit"):
        apps.create_app({"name": "Three", "kind": "openrouter_free"})


def test_create_duplicate_and_manual_next(apps):
    s = apps.create_app({"name": "T", "kind": "openrouter_free",
                         "schedule": "manual"})
    assert s["id"] == "t" and s["next_refresh"] is None
    with pytest.raises(ValueError, match="already exists"):
        apps.create_app({"name": "T", "kind": "openrouter_free"})
    d = apps.create_app({"name": "D", "kind": "openrouter_free",
                         "schedule": "daily@09:00"})
    assert d["next_refresh"] is not None and d["next_refresh"] > 0


def test_openrouter_filter_and_diff(apps, monkeypatch):
    monkeypatch.setattr(apps, "_http_get_json", lambda url: _or_fixture())
    apps.create_app({"name": "OR", "kind": "openrouter_free",
                     "schedule": "manual"})
    r1 = apps.refresh_app("or")
    assert r1["error"] is None and r1["count"] == 1
    det = apps.get_app("or")
    assert det["items"][0]["id"] == "a/free"
    assert det["added"] == 1 and det["removed"] == 0
    assert [i["id"] for i in det["added_items"]] == ["a/free"]
    # Second refresh with a changed feed: one added, none removed.
    fx = _or_fixture()
    fx["data"].append({"id": "d/new", "name": "D", "pricing": {"prompt": "0"}})
    monkeypatch.setattr(apps, "_http_get_json", lambda url: fx)
    r2 = apps.refresh_app("or")
    assert r2["count"] == 2
    det2 = apps.get_app("or")
    assert [i["id"] for i in det2["added_items"]] == ["d/new"]
    assert len(det2["history"]) == 2


def test_refresh_failure_recorded_not_raised(apps, monkeypatch):
    def _boom(url):
        raise ConnectionError("down")
    monkeypatch.setattr(apps, "_http_get_json", _boom)
    apps.create_app({"name": "OR", "kind": "openrouter_free",
                     "schedule": "manual"})
    r = apps.refresh_app("or")
    assert r["error"] and "ConnectionError" in r["error"]
    assert apps.get_app("or")["error"]


def test_ui_block_defaults_and_validation(apps):
    s = apps.validate_spec({"name": "U", "kind": "openrouter_free"})
    assert s["ui"] == {"view": "table",
                       "columns": [{"field": f, "label": f}
                                   for f in ["id", "name", "context_length"]],
                       "sort": "id", "highlight_new": True}
    s2 = apps.validate_spec({"name": "U2", "kind": "openrouter_free",
                             "ui": {"view": "cards",
                                    "columns": ["id", {"field": "name", "label": "Model"}],
                                    "sort": "name", "highlight_new": "off"}})
    assert s2["ui"]["view"] == "cards"
    assert s2["ui"]["columns"][1] == {"field": "name", "label": "Model"}
    assert s2["ui"]["highlight_new"] is False
    with pytest.raises(ValueError):
        apps.validate_spec({"name": "U3", "kind": "openrouter_free",
                            "ui": {"view": "timeline"}})
    with pytest.raises(ValueError):
        apps.validate_spec({"name": "U4", "kind": "openrouter_free",
                            "ui": {"columns": "id"}})


def test_json_feed_dotpath_and_match(apps, monkeypatch):
    feed = {"meta": {"ok": True},
            "items": [{"k": "x", "keep": True, "v": 1},
                      {"k": "y", "keep": False, "v": 2}]}
    monkeypatch.setattr(apps, "_http_get_json", lambda url: feed)
    apps.create_app({"name": "J", "kind": "json_feed", "schedule": "manual",
                     "url": "https://example.com/f.json", "items_path": "items",
                     "id_field": "k", "match": {"field": "keep", "equals": True},
                     "fields": ["k", "v"]})
    r = apps.refresh_app("j")
    assert r["count"] == 1
    assert apps.get_app("j")["items"] == [{"k": "x", "v": 1}]
