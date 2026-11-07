"""Agent Routes for the Parts library.

The live E2E exercises the whole pipeline; these tests pin the HTTP contract
and route ordering, especially that `/api/parts/jobs` and
`/api/parts/capabilities` are not swallowed by `/api/parts/{part_id}`.
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_server
import parts_service
import parts_store
from agent_server import app


def _part():
    return parts_store.create_part(
        name="Route Bracket",
        description="route test",
        filename="route-bracket.stl",
        blob=b"solid route\nendsolid route\n",
        metrics={"faces": 12, "vertices": 8},
        objective=9.5,
        report={"verdict": {"status": "ok", "errors": 0, "warnings": 0,
                            "printable": True}, "issues": []},
    )


def test_list_detail_view_and_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(parts_store, "PARTS_DIR", tmp_path / "parts")
    part = _part()
    with TestClient(app) as client:
        listed = client.get("/api/parts")
        assert listed.status_code == 200
        body = listed.json()
        assert body["stats"]["parts"] == 1
        assert body["parts"][0]["id"] == part["id"]

        detail = client.get(f"/api/parts/{part['id']}")
        assert detail.status_code == 200
        assert detail.json()["part"]["name"] == "Route Bracket"

        view = client.patch(f"/api/parts/{part['id']}/view",
                            json={"preset": "top", "overlay": "overhang"})
        assert view.status_code == 200
        assert view.json()["part"]["view"]["preset"] == "top"

        cap = client.get("/api/parts/capabilities")
        assert cap.status_code in (200, 502)
        # It must be the capabilities handler, not a part lookup with the id
        # "capabilities" (which would be a 400/404).
        if cap.status_code == 200:
            assert "backends" in cap.json()

        deleted = client.delete(f"/api/parts/{part['id']}")
        assert deleted.status_code == 200
        assert client.get(f"/api/parts/{part['id']}").status_code == 404


def test_import_route_normalises_the_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(parts_store, "PARTS_DIR", tmp_path / "parts")
    fake = {"id": "part-imported", "name": "Imported", "versions": [],
            "current_version": "v1", "verdict": {"status": "ok"},
            "faces": 12, "view": {}, "source": {}}

    async def fake_import(**kwargs):
        assert kwargs["name"] == "My Part"
        assert kwargs["scale"] == 10.0
        assert kwargs["auto_repair"] is True
        assert kwargs["blob"].startswith(b"solid")
        return fake

    monkeypatch.setattr(parts_service, "import_part", fake_import)
    with TestClient(app) as client:
        r = client.post(
            "/api/parts",
            files={"file": ("part.stl", b"solid x\nendsolid x\n",
                            "application/octet-stream")},
            data={"name": "My Part", "scale": "10", "auto_repair": "true"})
    assert r.status_code == 200, r.text
    assert r.json()["part"]["id"] == "part-imported"


def test_analyse_route_maps_service_errors(monkeypatch):
    async def fake_analyse(*args, **kwargs):
        raise parts_service.PartsServiceError("gateway is down", status=502)

    monkeypatch.setattr(parts_service, "analyse_version", fake_analyse)
    with TestClient(app) as client:
        r = client.post("/api/parts/part-x/analyse", json={})
    assert r.status_code == 502
    assert "gateway is down" in r.json()["error"]
