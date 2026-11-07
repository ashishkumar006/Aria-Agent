"""Contract tests for the agent-built app builder.

The prompt must carry the LIVE catalog (so adding a component never needs a
prompt edit), and the model's JSON must survive the same closed validator as
every other A2UI surface. No live model calls here.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import a2ui_catalog as C  # noqa: E402
import app_builder as B  # noqa: E402
import apps  # noqa: E402


def _surface() -> dict:
    return {
        "surfaceId": "test-app",
        "catalogId": C.CATALOG_ID,
        "rootComponent": "root",
        "components": [
            {"id": "root", "component": "Column",
             "children": ["title", "name", "go"]},
            {"id": "title", "component": "Text", "text": "Hello",
             "variant": "heading"},
            {"id": "name", "component": "Input", "name": "who",
             "label": "Your name", "placeholder": "type here"},
            {"id": "go", "component": "Action", "label": "Send",
             "variant": "primary", "action": "submit"},
        ],
    }


def _reply(**over) -> str:
    payload = {
        "name": "Greeting",
        "description": "A tiny greeting app",
        "schedule": "manual",
        "refresh_prompt": "",
        "state": {},
        "surface": _surface(),
    }
    payload.update(over)
    return json.dumps(payload)


def test_builder_prompt_contains_the_live_catalog_without_examples():
    prompt = B.system_prompt()
    for name in C.CATALOG:
        assert name in prompt, f"{name} missing from the builder prompt"
    assert "collect user input" in prompt.lower()
    # The user was explicit: no domain examples baked into the prompt.
    low = prompt.lower()
    for forbidden in ("stock", "quiz", "google"):
        assert forbidden not in low


def test_parse_reply_validates_and_returns_a_spec():
    spec = B.parse_reply(_reply())
    assert spec["kind"] == "a2ui"
    assert spec["name"] == "Greeting"
    assert spec["surface"]["catalogId"] == C.CATALOG_ID
    assert any(c["component"] == "Input" for c in spec["surface"]["components"])


def test_parse_flattens_inline_child_objects():
    surface = {
        "surfaceId": "nested",
        "catalogId": C.CATALOG_ID,
        "rootComponent": "root",
        "components": [
            {"id": "root", "component": "Column", "children": [
                {"id": "title", "component": "Text", "text": "Hi",
                 "variant": "heading"},
                {"id": "go", "component": "Action", "label": "Go",
                 "action": "go"},
            ]},
        ],
    }
    spec = B.parse_reply(_reply(surface=surface))
    ids = {c["id"] for c in spec["surface"]["components"]}
    assert {"root", "title", "go"} <= ids
    root = next(c for c in spec["surface"]["components"] if c["id"] == "root")
    assert root["props"]["children"] == ["title", "go"]
    assert spec["surface"]["rootComponent"] == "root"


def test_parse_accepts_the_type_alias_for_component():
    """Live failure behind this: `/api/apps/build` returned 422 with
    "components[0].component must be a string". The prompt's surface example
    showed `"components": []` and never named the key holding the component
    name, so the model wrote the near-universal `"type"` spelling and the whole
    paid builder run was thrown away. The alias is unambiguous, so normalise."""
    surface = {
        "surfaceId": "aliased",
        "catalogId": C.CATALOG_ID,
        "rootComponent": "root",
        "components": [
            {"id": "root", "type": "Column", "children": ["title"], "gap": "loose"},
            {"id": "title", "kind": "Text", "text": "Hi", "variant": "heading"},
        ],
    }
    spec = B.parse_reply(_reply(surface=surface))
    comps = spec["surface"]["components"]
    assert {c["component"] for c in comps} == {"Column", "Text"}
    assert all("type" not in c and "kind" not in c for c in comps)
    # A real `component` string must win over an alias, never be overwritten.
    assert next(c for c in comps if c["id"] == "root")["component"] == "Column"


def test_parse_still_rejects_a_component_that_is_not_a_string():
    surface = _surface()
    surface["components"][0]["component"] = {"nested": "not a name"}
    with pytest.raises(B.BuilderError):
        B.parse_reply(_reply(surface=surface))


def test_parse_rejects_unknown_components():
    surface = _surface()
    surface["components"].append(
        {"id": "bad", "component": "Script", "src": "https://evil"})
    with pytest.raises(B.BuilderError):
        B.parse_reply(_reply(surface=surface))


def test_parse_keeps_the_existing_identity_and_state():
    existing = {"id": "my-app", "name": "Mine", "schedule": "daily@09:00",
                "state": {"answer": "old"}}
    spec = B.parse_reply(_reply(name="Changed", schedule=""), existing=existing)
    assert spec["id"] == "my-app"
    assert spec["schedule"] == "daily@09:00"
    assert spec["state"] == {}


def test_apps_accepts_a_validated_a2ui_spec():
    spec = apps.validate_spec(_reply_json())
    assert spec["kind"] == "a2ui"
    assert spec["surface"]["surfaceId"] == "test-app"
    assert spec["schedule"] == "manual"


def test_apps_rejects_an_invalid_a2ui_surface():
    bad = _surface()
    bad["components"].append({"id": "x", "component": "Nope"})
    payload = json.loads(_reply())
    payload["surface"] = bad
    with pytest.raises(ValueError):
        apps.validate_spec(payload)


def _reply_json() -> dict:
    payload = json.loads(_reply())
    payload["kind"] = "a2ui"
    return payload
