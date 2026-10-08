"""The trust gate: every way a mesh can be nonsense has to raise here."""
from __future__ import annotations

import numpy as np
import pytest

from parts.errors import (
    EmptyMeshError,
    MeshIntegrityError,
    MeshTooLargeError,
    PartError,
)
from parts.mesh import MM_CONVENTION, Mesh
from parts.tests import primitives as prim


def test_valid_arrays_are_coerced_to_float64_and_int64():
    v = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.int32)
    f = np.array([[0, 1, 2]], dtype=np.int32)
    mesh = Mesh.create(v, f)
    assert mesh.vertices.dtype == np.float64
    assert mesh.faces.dtype == np.int64


def test_float_face_indices_are_accepted_only_when_whole():
    v, _ = prim.cube()
    whole = prim.cube()[1].astype(np.float64)
    assert Mesh.create(v, whole).faces.dtype == np.int64

    fractional = whole.copy()
    fractional[0, 0] = 0.5
    with pytest.raises(MeshIntegrityError, match="whole numbers"):
        Mesh.create(v, fractional)


@pytest.mark.parametrize(
    "bad_vertices, bad_faces, match",
    [
        (np.zeros((4, 2)), np.array([[0, 1, 2]]), r"\(N, 3\)"),
        (np.zeros((4,)), np.array([[0, 1, 2]]), r"\(N, 3\)"),
        (np.zeros((4, 3)), np.zeros((1, 4), dtype=np.int64), r"\(M, 3\)"),
        (np.zeros((4, 3)), np.zeros(3, dtype=np.int64), r"\(M, 3\)"),
    ],
)
def test_wrong_shapes_are_rejected(bad_vertices, bad_faces, match):
    with pytest.raises(MeshIntegrityError, match=match):
        Mesh.create(bad_vertices, bad_faces)


def test_nan_coordinates_are_rejected():
    """Binary STL has no type tags, so a NaN reaches numpy as a real float."""
    v, f = prim.cube()
    v = v.copy()
    v[3, 1] = np.nan
    with pytest.raises(MeshIntegrityError, match="non-finite"):
        Mesh.create(v, f)


def test_infinite_coordinates_are_rejected():
    v, f = prim.cube()
    v = v.copy()
    v[0, 0] = np.inf
    with pytest.raises(MeshIntegrityError, match="non-finite"):
        Mesh.create(v, f)


def test_out_of_range_face_index_is_rejected():
    v, f = prim.cube()
    bad = f.copy()
    bad[0, 0] = 8  # the cube has 8 vertices, so 8 is one past the end
    with pytest.raises(MeshIntegrityError, match="out of range"):
        Mesh.create(v, bad)


def test_negative_face_index_is_rejected():
    v, f = prim.cube()
    bad = f.copy()
    bad[3, 1] = -1
    with pytest.raises(MeshIntegrityError, match="out of range"):
        Mesh.create(v, bad)


def test_empty_arrays_are_rejected_as_empty_not_as_corrupt():
    with pytest.raises(EmptyMeshError):
        Mesh.create(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64))
    v, _ = prim.cube()
    with pytest.raises(EmptyMeshError):
        Mesh.create(v, np.zeros((0, 3), dtype=np.int64))


def test_resource_limits_are_enforced_before_the_mesh_exists():
    v, f = prim.cube()
    with pytest.raises(MeshTooLargeError) as excinfo:
        Mesh.create(v, f, max_vertices=4)
    assert excinfo.value.detail["kind"] == "vertices"
    with pytest.raises(MeshTooLargeError) as excinfo:
        Mesh.create(v, f, max_faces=4)
    assert excinfo.value.detail["kind"] == "faces"


def test_every_error_is_a_part_error_with_a_code_and_dict_form():
    """The route maps `code` to a status, so every failure must carry one."""
    for exc in (
        EmptyMeshError("x"),
        MeshIntegrityError("x"),
        MeshTooLargeError("x"),
    ):
        assert isinstance(exc, PartError)
        assert isinstance(exc.code, str) and exc.code
        body = exc.to_dict()
        assert set(body) == {"code", "message", "detail"}
        assert body["message"] == "x"


def test_with_arrays_preserves_provenance():
    v, f = prim.cube()
    mesh = Mesh.create(v, f, source_format="obj", units=MM_CONVENTION)
    rebuilt = mesh.with_arrays(mesh.vertices, mesh.faces[:4])
    assert rebuilt.source_format == "obj"
    assert rebuilt.units is MM_CONVENTION
    assert rebuilt.face_count == 4


def test_to_dict_never_exposes_the_arrays():
    v, f = prim.cube()
    body = Mesh.create(v, f, source_format="stl").to_dict()
    assert body["source_format"] == "stl"
    assert body["vertex_count"] == 8
    assert body["face_count"] == 12
    assert not any(isinstance(value, np.ndarray) for value in body.values())


def test_unit_assumption_refuses_to_convert_a_non_mm_value():
    from parts.mesh import UnitAssumption

    inches = UnitAssumption(
        assumed="in", confidence="declared", note="test", scale_to_mm=25.4
    )
    assert inches.to_mm(1.0) is None, "a scaled number must not be presented as verified"
    assert MM_CONVENTION.to_mm(2.0) == 2.0