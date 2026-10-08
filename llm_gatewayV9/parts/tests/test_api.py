"""The HTTP surface, exercised on a locally-built app.

`parts.api` is deliberately not registered on the gateway. These tests mount
its router on a throwaway `FastAPI()` so the contract is verified without
touching `main.py` - and `test_the_router_does_not_pull_in_main` pins the
property that makes that safe.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

from parts.api import _read_capped, router
from parts.errors import MeshTooLargeError, PartError
from parts.tests import primitives as prim

GATEWAY_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def app(monkeypatch):
    """A throwaway app with the parts router mounted, loopback check stubbed."""
    import channels_api

    calls = []
    monkeypatch.setattr(
        channels_api, "_loopback_only", lambda request: calls.append(request)
    )
    application = FastAPI()
    application.include_router(router)
    application.state.loopback_calls = calls
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def upload(name: str, data: bytes):
    return {"file": (name, data, "application/octet-stream")}


# -- the routes ------------------------------------------------------------

def test_the_router_declares_the_three_documented_routes():
    routes = {(r.path, tuple(sorted(r.methods))) for r in router.routes}
    assert routes == {
        ("/v1/parts/validate", ("POST",)),
        ("/v1/parts/analyse", ("POST",)),
        ("/v1/parts/capabilities", ("GET",)),
    }


def test_the_openapi_schema_builds():
    """If FastAPI cannot serialise the signatures, main.py would fail to start."""
    schema = FastAPI().openapi()
    application = FastAPI()
    application.include_router(router)
    paths = application.openapi()["paths"]
    assert set(paths) == {"/v1/parts/validate", "/v1/parts/analyse", "/v1/parts/capabilities"}
    assert "multipart/form-data" in str(paths["/v1/parts/validate"]["post"]["requestBody"])
    assert schema["openapi"].startswith("3.")


def test_the_router_does_not_pull_in_main():
    """The reason it can sit in its own package with no circular import."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys, parts.api; print('main' in sys.modules)"],
        cwd=str(GATEWAY_ROOT), capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_capabilities_advertises_the_limits_and_the_optional_backends(client):
    body = client.get("/v1/parts/capabilities").json()
    assert body["schema_version"] == "1"
    assert set(body["backends"]) == {"trimesh", "manifold3d"}
    assert body["natively_readable"] == ["stl", "obj"]
    assert body["limits"]["max_bytes"] == 64 * 1024 * 1024
    assert any("Topology optimisation is out of scope" in n for n in body["notes"])
    assert any("through-hole" in n for n in body["notes"])


# -- happy paths -----------------------------------------------------------

def test_validate_returns_a_report_without_repairing(client):
    data = prim.binary_stl(*prim.cube())
    response = client.post("/v1/parts/validate", files=upload("part.stl", data))
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "1"
    assert body["repair"] is None
    assert body["source"]["format"] == "stl"
    assert body["analysis"]["topology"]["watertight"] is False, "unwelded STL"
    assert body["verdict"]["printable"] is False


def test_analyse_repairs_by_default(client):
    data = prim.binary_stl(*prim.cube())
    body = client.post("/v1/parts/analyse", files=upload("part.stl", data)).json()
    assert body["repair"] is not None
    assert body["analysis"]["topology"]["watertight"] is True
    assert body["verdict"]["status"] == "ok"
    assert body["verdict"]["printable"] is True


def test_repair_false_matches_validate(client):
    data = prim.binary_stl(*prim.cube())
    body = client.post("/v1/parts/analyse?repair=false",
                       files=upload("part.stl", data)).json()
    assert body["repair"] is None


def test_an_obj_upload_is_read(client):
    v, f = prim.cube()
    body = client.post("/v1/parts/analyse", files=upload("part.obj", prim.obj(v, f))).json()
    assert body["source"]["format"] == "obj"
    assert body["source"]["io_backend"] == "native-obj"
    assert body["verdict"]["status"] == "ok"


def test_scale_moves_the_reported_size(client):
    v, f = prim.cube()
    data = prim.binary_stl(v, f)
    body = client.post("/v1/parts/analyse?scale=25.4",
                       files=upload("part.stl", data)).json()
    assert body["units"]["confidence"] == "caller"
    assert body["analysis"]["geometry"]["bbox"]["extents"][0] == pytest.approx(25.4)


def test_the_loopback_check_is_called(app, client):
    client.post("/v1/parts/validate", files=upload("part.stl", prim.binary_stl(*prim.cube())))
    assert len(app.state.loopback_calls) == 1


# -- error mapping ---------------------------------------------------------

def test_an_oversized_upload_is_a_413(client):
    data = prim.binary_stl(*prim.cube())
    response = client.post("/v1/parts/validate?max_bytes=100",
                           files=upload("part.stl", data))
    assert response.status_code == 413
    detail = response.json()["detail"]
    assert detail["code"] == "mesh_too_large"
    assert detail["detail"]["kind"] == "bytes"


def test_a_format_needing_trimesh_is_a_415(client):
    response = client.post("/v1/parts/validate",
                           files=upload("part.ply", b"ply\nformat ascii 1.0\nend_header\n"))
    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "unsupported_format"


def test_a_truncated_mesh_is_a_422(client):
    data = prim.binary_stl(*prim.cube())[:-20]
    response = client.post("/v1/parts/validate", files=upload("part.stl", data))
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "mesh_integrity"
    assert "truncated" in response.json()["detail"]["message"]


def test_a_junk_header_on_a_valid_stl_is_still_read(client):
    """The 80-byte STL header is not content; mangling it must not matter."""
    data = bytearray(prim.binary_stl(*prim.cube()))
    data[0:5] = b"xxxxx"
    response = client.post("/v1/parts/validate", files=upload("part.stl", bytes(data)))
    assert response.status_code == 200


def test_an_unknown_extension_is_a_415(client):
    """An extension we recognise as neither mesh nor known-3D is a media-type
    problem, not a corrupt-payload problem."""
    response = client.post("/v1/parts/validate",
                           files=upload("part.bin", b"\x01\x02\x03 not a mesh"))
    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "unsupported_format"


def test_content_that_identifies_as_nothing_is_a_415(client):
    """No magic bytes and no usable extension: there is nothing to go on.

    Shares the 415 with "known format, no reader" - both are a media-type
    problem - and is told apart by the message, not by a second status.
    """
    response = client.post("/v1/parts/validate", files=upload("part", b"\x01\x02\x03 junk"))
    assert response.status_code == 415
    assert "could not identify" in response.json()["detail"]["message"]


def test_every_mapped_code_has_a_sensible_status():
    from parts.api import _STATUS

    for code in ("unsupported_format", "mesh_too_large", "mesh_integrity",
                 "empty_mesh", "repair_failed", "missing_backend", "part_error"):
        assert 400 <= _STATUS[code] < 600, code


def test_an_unmapped_code_still_produces_a_client_error():
    from parts.api import _raise

    with pytest.raises(HTTPException) as excinfo:
        _raise(PartError("something new"))
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "part_error"


def test_an_internal_bug_is_a_500_with_no_traceback(client, monkeypatch):
    """A 500 that leaks the exception message is a 500 that leaks the schema."""
    import parts.api

    def boom(*args, **kwargs):
        raise RuntimeError("SECRET_TABLE_NAME=widgets")

    monkeypatch.setattr(parts.api, "analyse_part", boom)
    response = client.post("/v1/parts/validate",
                           files=upload("part.stl", prim.binary_stl(*prim.cube())))
    assert response.status_code == 500
    body = response.json()["detail"]
    assert body["code"] == "internal_error"
    assert "SECRET_TABLE_NAME" not in str(body)
    assert "RuntimeError" in body["message"]


# -- the read cap in isolation -------------------------------------------

class FakeUpload:
    def __init__(self, data: bytes, name="part.stl"):
        self.data = data
        self.filename = name

    async def read(self, size=-1):
        if size is None or size < 0:
            chunk, self.data = self.data, b""
            return chunk
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk


async def test_the_cap_returns_everything_under_the_limit():
    assert await _read_capped(FakeUpload(b"x" * 10), 100) == b"x" * 10


async def test_the_cap_abandons_a_stream_that_runs_over():
    with pytest.raises(MeshTooLargeError) as excinfo:
        await _read_capped(FakeUpload(b"x" * 10), 4)
    assert excinfo.value.detail["limit"] == 4


async def test_the_cap_reads_in_chunks_rather_than_asking_for_everything():
    """`await upload.read()` with no size is how one request eats all the RAM."""
    seen = []

    class Chunky(FakeUpload):
        async def read(self, size=-1):
            seen.append(size)
            return await super().read(size)

    await _read_capped(Chunky(b"x" * 40), 100)
    assert seen == [1 << 20] * 2
    assert 40 not in seen