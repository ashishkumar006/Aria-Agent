"""Parts library: immutable versions, caps, local tools and the DAG app node.

These are deliberately not gateway integration tests. The gateway's own parts
suite covers geometry; this file pins the agent-side contract the DAG and the
console both rely on: a version is never mutated, a current version survives
pruning, and a local tool sees the same store the HTTP routes do.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import parts_store as store


@pytest.fixture
def parts_dir(tmp_path, monkeypatch):
    root = tmp_path / "parts"
    monkeypatch.setattr(store, "PARTS_DIR", root)
    return root


def _create(name="QA Bracket", blob=b"solid cube\nendsolid cube\n", fmt="stl"):
    return store.create_part(
        name=name,
        description="test part",
        filename=f"{name.lower().replace(' ', '-')}.{fmt}",
        blob=blob,
        metrics={"faces": 12, "vertices": 8, "dimensions_mm": [10, 10, 10]},
        objective=10.0,
        report={"verdict": {"status": "ok", "errors": 0, "warnings": 0,
                            "printable": True},
                "issues": [], "repair": {"steps": []}},
        origin="ui",
        tags=["qa"],
    )


def _add(part_id, *, parent="v1", op="decimate", kept=True, name="v"):
    return store.add_version(
        part_id,
        blob=(f"{name}-{op}-{parent}".encode() * 4),
        filename=f"{name}-{op}.stl",
        op=op,
        params={"target_faces": 100},
        metrics={"faces": 8, "vertices": 6},
        objective=5.0,
        report={"verdict": {"status": "ok"}, "issues": []},
        parent=parent,
        kept=kept,
        note="test",
        origin="dag",
    )


def test_versions_are_immutable_and_the_current_pointer_moves(parts_dir):
    part = _create()
    assert part["current_version"] == "v1"
    assert part["versions"][0]["op"] == "upload"
    updated = _add(part["id"], parent="v1")
    assert updated["current_version"] == "v2"
    assert len(updated["versions"]) == 2
    # The parent's bytes are still exactly the upload.
    blob, version = store.read_version_bytes(part["id"], "v1")
    assert blob == b"solid cube\nendsolid cube\n"
    assert version["op"] == "upload"
    # A reverted version does not move the pointer.
    reverted = _add(part["id"], parent="v2", op="smooth", kept=False)
    assert reverted["current_version"] == "v2"
    assert any(v["id"] == "v3" and v["kept"] is False
               for v in reverted["versions"])


def test_current_version_survives_pruning_and_old_files_are_removed(parts_dir):
    part = _create()
    store.set_current(part["id"], "v1")
    for i in range(34):
        part = _add(part["id"], parent="v1", op=f"decimate", kept=False,
                    name=f"a{i}")
    assert len(part["versions"]) == store.MAX_VERSIONS
    assert any(v["id"] == "v1" for v in part["versions"])
    # A dropped version's file must not be left behind.
    dropped = [v for v in part["versions"] if v["id"] == "v2"]
    assert not dropped
    # v2 was the first added version, whose file was a0-decimate.stl.
    assert not (parts_dir / part["id"] / "a0-decimate.stl").exists()


def test_duplicate_and_view_and_delete(parts_dir):
    part = _create()
    copy = store.duplicate_part(part["id"], "QA Bracket copy")
    assert copy["id"] != part["id"]
    assert copy["name"] == "QA Bracket copy"
    assert len(store.list_parts()) == 2
    viewed = store.set_view(part["id"], {"preset": "top", "mode": "wireframe",
                                         "overlay": "overhang"})
    assert viewed["view"]["preset"] == "top"
    assert viewed["view"]["mode"] == "wireframe"
    bad = store.set_view(part["id"], {"preset": "nonsense", "mode": "x",
                                      "overlay": "y"})
    assert bad["view"]["preset"] == "iso"
    assert bad["view"]["mode"] == "shaded"
    assert bad["view"]["overlay"] == "none"
    assert store.delete_part(copy["id"]) is True
    assert store.delete_part(copy["id"]) is False
    assert len(store.list_parts()) == 1


def test_duplicate_hash_is_found_and_used_by_import(parts_dir):
    blob = b"unique-mesh-bytes"
    part = _create(blob=blob)
    found = store.find_by_sha(__import__("hashlib").sha256(blob).hexdigest())
    assert found and found["id"] == part["id"]
    assert store.find_by_sha("0" * 64) is None


def test_local_tools_read_the_same_store(parts_dir):
    import local_tools
    part = _create()
    listed = json.loads(asyncio.run(local_tools.dispatch("parts_list", {})))
    assert listed["ok"] is True
    assert listed["parts"][0]["id"] == part["id"]
    got = json.loads(asyncio.run(local_tools.dispatch(
        "part_get", {"part": part["name"]})))
    assert got["ok"] is True and got["id"] == part["id"]
    export = json.loads(asyncio.run(local_tools.dispatch(
        "part_export", {"part": part["id"]})))
    assert export["ok"] is True
    assert export["download"].endswith("/versions/v1/stl")
    view = json.loads(asyncio.run(local_tools.dispatch(
        "part_set_view", {"part": part["id"], "preset": "front",
                          "overlay": "issues"})))
    assert view["view"]["preset"] == "front"
    assert view["view"]["overlay"] == "issues"


def test_ambiguous_name_is_refused(parts_dir):
    import local_tools
    _create("QA Bracket one")
    _create("QA Bracket two", blob=b"other")
    out = json.loads(asyncio.run(local_tools.dispatch(
        "part_get", {"part": "QA Bracket"})))
    assert out["ok"] is False
    assert "matches more than one" in out["error"]


def test_the_app_builder_skill_is_deterministic_and_uses_metadata(monkeypatch):
    """The Apps endpoints go through a DAG node, but that node must not pay
    for a second model call just to call the builder."""
    import agent_server
    import apps
    import skills

    calls = {}

    async def fake_build(description, *, existing=None, event=None, mode="build"):
        calls.update({"description": description, "existing": existing,
                      "event": event, "mode": mode})
        return {"name": "QA Built", "description": description, "kind": "a2ui",
                "schedule": "manual",
                "surface": {"surfaceId": "s", "rootComponent": "root",
                            "components": [{"id": "root", "component": "Text",
                                            "text": "hi"}]},
                "state": {}}

    created = {}

    def fake_create(spec):
        created.update(spec)
        return {**spec, "id": "qa-built-app"}

    monkeypatch.setattr(agent_server, "_app_builder_generate", fake_build)
    monkeypatch.setattr(apps, "create_app", fake_create)
    registry = skills.SkillRegistry()
    skill = registry.get("app_builder")
    nodes = {"n:1": {"inputs": ["USER_QUERY"],
                     "metadata": {"description": "a tiny status app",
                                  "mode": "build", "name": "QA Built"}}}
    result, _ = asyncio.run(skills.run_skill(
        skill, "n:1", nodes, "test-app-skill", "a tiny status app", None))
    assert result.success is True
    assert result.output["app_id"] == "qa-built-app"
    assert calls["description"] == "a tiny status app"
    assert calls["mode"] == "build"
    assert created["name"] == "QA Built"


def test_the_app_builder_action_node_saves_the_surface(monkeypatch):
    import agent_server
    import apps
    import skills

    spec = {"id": "qa-action-app", "kind": "a2ui", "name": "QA Action",
            "description": "sampler", "surface": {"surfaceId": "old",
                                                  "components": []}}
    saved = {}

    async def fake_build(description, *, existing=None, event=None, mode="build"):
        return {"surface": {"surfaceId": "new", "components": [
                    {"id": "root", "component": "Text", "text": "updated"}]},
                "state": {}}

    def fake_save(app_id, surface, state=None, event=None):
        saved.update({"app_id": app_id, "surface": surface, "event": event})
        return {"id": app_id, "surface": surface, "state": state}

    monkeypatch.setattr(agent_server, "_app_builder_generate", fake_build)
    monkeypatch.setattr(apps, "save_a2ui_surface", fake_save)
    skill = skills.SkillRegistry().get("app_builder")
    event = {"action": "apply", "input": {"x": "1"}}
    nodes = {"n:1": {"inputs": ["USER_QUERY"],
                     "metadata": {"existing": spec, "event": event,
                                  "mode": "action"}}}
    result, _ = asyncio.run(skills.run_skill(
        skill, "n:1", nodes, "test-app-action", "sampler", None))
    assert result.success is True
    assert saved["app_id"] == "qa-action-app"
    assert saved["surface"]["surfaceId"] == "new"
    assert saved["event"] == event
    assert result.output["action"] == "apply"


def test_section_tools_are_local_and_not_advertised_as_mcp_tools():
    """The MCP reachability test asserts every _TOOL_CATALOG entry is a real
    mcp_server tool. Section tools share the agent's store and must stay on the
    local dispatch path, so they belong in local_tools.SCHEMAS, not that dict."""
    import skills
    assert skills._local_tools is not None
    assert not (skills._local_tools.LOCAL_TOOL_NAMES & set(skills._TOOL_CATALOG))
    payload = skills.tool_payload(["parts_list", "part_get", "web_search"])
    names = {t["name"] for t in (payload or [])}
    assert {"parts_list", "part_get", "web_search"} <= names


def test_import_measures_as_stored_when_auto_repair_is_off(parts_dir, monkeypatch):
    import parts_service
    seen = {}

    async def fake_metrics(blob, filename, **kwargs):
        return {"metrics": {"faces": 12, "vertices": 8}, "objective": 1.0}

    async def fake_analyse(blob, filename, **kwargs):
        seen.update(kwargs)
        return {"verdict": {"status": "unusable"}, "issues": [], "repair": None}

    monkeypatch.setattr(parts_service, "metrics", fake_metrics)
    monkeypatch.setattr(parts_service, "analyse", fake_analyse)
    part = asyncio.run(parts_service.import_part(
        blob=b"solid raw", filename="raw.stl",
        name="Raw", auto_repair=False))
    assert seen["weld_only"] is False
    assert part["current_version"] == "v1"
    assert part["versions"][0]["weld_only"] is False


def test_import_auto_repair_normalises_before_measuring(parts_dir, monkeypatch):
    import parts_service
    seen = {}
    called = {}

    async def fake_metrics(blob, filename, **kwargs):
        return {"metrics": {"faces": 12, "vertices": 8}, "objective": 1.0}

    async def fake_analyse(blob, filename, **kwargs):
        seen.update(kwargs)
        return {"verdict": {"status": "ok"}, "issues": [], "repair": None}

    async def fake_repair(part_id, version=None, **kwargs):
        called["repair"] = (part_id, version)
        return {"part": store.get_part(part_id), "version": None, "changed": False}

    monkeypatch.setattr(parts_service, "metrics", fake_metrics)
    monkeypatch.setattr(parts_service, "analyse", fake_analyse)
    monkeypatch.setattr(parts_service, "repair_version", fake_repair)
    part = asyncio.run(parts_service.import_part(
        blob=b"solid auto", filename="auto.stl",
        name="Auto", auto_repair=True))
    assert seen["weld_only"] is True
    assert called["repair"][0] == part["id"]
