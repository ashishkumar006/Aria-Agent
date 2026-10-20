"""The endpoints an agent loops against.

`parts` v1 shipped validate/repair only. These cover the optimise + geometry
surface, including the two things that silently broke during development:

  - STL is NON-INDEXED (every triangle carries its own copy of each vertex),
    and quadric decimation is defined on an indexed mesh. Without welding
    first, decimate reports "no change" for every real upload.
  - A pass can legitimately refuse (`target_faces >= faces`). That is an
    answer, not a failure, so it must come back 200 with an explanation rather
    than an error or, worse, a silent pass.

Everything here goes through the real FastAPI app, so the multipart handling
and the query-vs-form distinction are covered too. `target_faces` and
`pass_name` are QUERY params; sent as form fields they are ignored and the
endpoint falls back to defaults, which looks exactly like a broken optimiser.
"""
import io
import json
import math

import pytest
from fastapi import FastAPI, File, UploadFile
from starlette.testclient import TestClient

from parts.optimise_api import router


def stl_sphere(rings=10, seg=20, radius=12.0) -> bytes:
    """ASCII STL of a closed triangulated sphere - a real upload, not a fixture."""
    def pt(i, j):
        th, ph = math.pi * i / rings, 2 * math.pi * j / seg
        return (radius * math.sin(th) * math.cos(ph),
                radius * math.sin(th) * math.sin(ph),
                radius * math.cos(th))

    tris = []
    for i in range(rings):
        for j in range(seg):
            a, b, c, d = pt(i, j), pt(i + 1, j), pt(i + 1, j + 1), pt(i, j + 1)
            tris += [(a, b, c), (a, c, d)]
    out = ["solid s"]
    for a, b, c in tris:
        out.append("  facet normal 0 0 0\n    outer loop")
        for v in (a, b, c):
            out.append(f"      vertex {v[0]:.5f} {v[1]:.5f} {v[2]:.5f}")
        out.append("    endloop\n  endfacet")
    out.append("endsolid s")
    return "\n".join(out).encode()


@pytest.fixture(scope="module")
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


SPHERE = stl_sphere()


def test_the_agent_can_read_the_pass_registry(client):
    """An agent must not hardcode pass names or their parameters."""
    r = client.get("/v1/parts/optimise/passes")
    assert r.status_code == 200
    body = r.json()
    names = {p["name"] for p in body["passes"]}
    assert {"decimate", "smooth", "detect_overhang", "detect_thin_walls"} <= names
    # it must be able to tell a modifying pass from a measuring one
    by_name = {p["name"]: p for p in body["passes"]}
    assert by_name["decimate"]["modifies"] is True
    assert by_name["detect_overhang"]["modifies"] is False


def test_geometry_welds_a_non_indexed_upload(client):
    """The regression: a raw STL sphere has 3 vertices per triangle, and
    quadric decimation silently does nothing on that form."""
    r = client.post("/v1/parts/geometry",
                    files={"file": ("s.stl", SPHERE, "application/octet-stream")})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["metrics"]["faces"] > 0
    assert b["geometry"]["position_count"] == b["metrics"]["vertices"]
    assert len(b["geometry"]["positions"]) == b["metrics"]["vertices"] * 3
    assert len(b["geometry"]["indices"]) == b["metrics"]["faces"] * 3
    # welded: fewer vertices than 3x the triangle count
    assert b["metrics"]["vertices"] < b["metrics"]["faces"] * 3


def test_the_measured_volume_is_physically_right(client):
    """A 24mm-diameter sphere is 7238 mm3. This catches a silently broken
    cross-product or winding convention, which is the failure that only shows
    up as "the number looks plausible"."""
    r = client.post("/v1/parts/geometry",
                    files={"file": ("s.stl", SPHERE, "application/octet-stream")})
    m = r.json()["metrics"]
    exact = 4.0 / 3.0 * math.pi * 12.0 ** 3
    assert m["volume_mm3"] > 0, "volume must be positive for an outward solid"
    assert abs(m["volume_mm3"] - exact) / exact < 0.05, (
        f"volume {m['volume_mm3']} is not within 5% of {exact}")


def test_decimation_reduces_faces(client):
    r = client.post(
        "/v1/parts/optimise",
        files={"file": ("s.stl", SPHERE, "application/octet-stream")},
        params={"pass_name": "decimate", "target_faces": 100})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["changed"] is True, b["note"]
    assert b["faces_after"] < b["faces_before"]


def test_the_objective_is_not_just_triangle_count(client):
    """Fewer triangles is not automatically better, and the endpoint must be
    able to say so.

    Decimating hard creates short edges, and the objective penalises geometry
    below the feature size. So a big face reduction can legitimately make the
    score WORSE - and `improved` must report that rather than assume the
    triangle count is the whole story. This is the signal an agent uses to
    stop iterating, so it has to be honest.
    """
    geo = client.post(
        "/v1/parts/geometry",
        files={"file": ("s.stl", SPHERE, "application/octet-stream")}).json()
    faces = geo["metrics"]["faces"]

    obj_before = geo["objective"]
    r = client.post(
        "/v1/parts/optimise",
        files={"file": ("s.stl", SPHERE, "application/octet-stream")},
        params={"pass_name": "decimate", "target_faces": 20}).json()

    assert r["changed"] is True
    assert r["faces_after"] < faces
    # whatever it decided, the flag agrees with the two numbers it is derived
    # from. That is the contract an agent depends on.
    assert r["improved"] == (r["objective_after"] < r["objective_before"])
    assert r["objective_before"] == pytest.approx(obj_before, rel=0.01)


def test_a_refused_pass_is_200_with_an_explanation(client):
    """`target_faces >= faces` is refused on purpose. A caller needs to be told
    why, not handed an error or a silent no-op."""
    geo = client.post(
        "/v1/parts/geometry",
        files={"file": ("s.stl", SPHERE, "application/octet-stream")}).json()
    faces = geo["metrics"]["faces"]

    r = client.post(
        "/v1/parts/optimise",
        files={"file": ("s.stl", SPHERE, "application/octet-stream")},
        params={"pass_name": "decimate", "target_faces": faces + 10_000})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["changed"] is False
    assert "already at or below" in b["note"]
    assert b["faces_after"] == b["faces_before"]


def test_a_detection_pass_never_changes_the_mesh(client):
    r = client.post(
        "/v1/parts/optimise",
        files={"file": ("s.stl", SPHERE, "application/octet-stream")},
        params={"pass_name": "detect_overhang", "overhang_angle_deg": 45})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["changed"] is False
    assert b["faces_after"] == b["faces_before"]
    assert b["note"]
    # it must not ship a copy of the whole mesh for nothing
    assert b["result_stl_base64"] is None


def test_an_unknown_pass_is_404(client):
    r = client.post(
        "/v1/parts/optimise",
        files={"file": ("s.stl", SPHERE, "application/octet-stream")},
        params={"pass_name": "teleport"})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "unknown_pass"


def test_an_empty_upload_is_422_not_500(client):
    r = client.post(
        "/v1/parts/geometry",
        files={"file": ("s.stl", b"", "application/octet-stream")})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "empty_mesh"


def test_the_result_can_be_chained_into_the_next_iteration(client):
    """The whole point: an agent loops without holding a mesh itself.

    Each iteration halves the triangle count and the objective must fall until
    it does not - at which point the loop has converged and should stop.
    """
    import base64

    blob, prev, faces = SPHERE, None, None
    history = []
    for _ in range(8):
        cur = (client.post("/v1/parts/geometry",
                           files={"file": ("s.stl", blob,
                                           "application/octet-stream")})
               .json()["metrics"]["faces"])
        target = max(8, cur // 2)
        b = client.post(
            "/v1/parts/optimise",
            files={"file": ("s.stl", blob, "application/octet-stream")},
            params={"pass_name": "decimate", "target_faces": target}).json()
        history.append((b["faces_after"], b["objective_after"]))
        if prev is not None and b["objective_after"] >= prev:
            break
        if not b["changed"]:
            break
        prev = b["objective_after"]
        blob = base64.b64decode(b["result_stl_base64"])

    assert len(history) >= 2, "the loop never made progress"
    # strictly improving while it keeps going
    for (f0, o0), (f1, o1) in zip(history, history[1:]):
        if o1 >= o0:
            break
        assert f1 < f0, "faces must fall every accepted iteration"
    assert blob is not None