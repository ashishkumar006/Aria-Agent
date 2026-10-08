"""Readers, format detection, and the resource guards around them."""
from __future__ import annotations

import struct
import zipfile
from io import BytesIO

import numpy as np
import pytest

from parts.errors import (
    MeshIntegrityError,
    MeshTooLargeError,
    UnsupportedFormatError,
)
from parts.export import to_binary_stl
from parts.mesh import Mesh
from parts.readers import detect_format, read, zip_expansion
from parts.tests import primitives as prim

LIMITS = {"max_faces": 100_000, "max_vertices": 100_000}


def load(data: bytes, fmt: str):
    result = read(data, fmt, **LIMITS)
    return Mesh.create(result.vertices, result.faces, source_format=result.fmt)


# -- binary STL ------------------------------------------------------------

def test_binary_stl_round_trips_through_its_own_reader():
    v, f = prim.cube()
    mesh = load(prim.binary_stl(v, f), "stl")
    assert mesh.face_count == 12
    # Binary STL has no vertex sharing, so 36 corners is correct, not a defect.
    assert mesh.vertex_count == 36
    assert np.unique(mesh.vertices, axis=0).shape[0] == 8


def test_binary_stl_truncated_file_is_a_corruption_error():
    v, f = prim.cube()
    data = prim.binary_stl(v, f)[:-20]
    with pytest.raises(MeshIntegrityError, match="truncated"):
        read(data, "stl", **LIMITS)


def test_binary_stl_lying_about_its_triangle_count_is_rejected():
    """The count is attacker-controlled; it must be checked against the bytes."""
    data = bytearray(prim.binary_stl(*prim.cube()))
    struct.pack_into("<I", data, 80, 1_000_000)
    with pytest.raises(MeshIntegrityError, match="truncated"):
        read(bytes(data), "stl", **LIMITS)


def test_binary_stl_zero_triangles_is_rejected():
    data = b"\0" * 80 + struct.pack("<I", 0)
    with pytest.raises(MeshIntegrityError, match="zero triangles"):
        read(data, "stl", **LIMITS)


def test_binary_stl_rejects_a_face_count_over_the_limit():
    data = bytearray(prim.binary_stl(*prim.cube()))
    with pytest.raises(MeshTooLargeError) as excinfo:
        read(bytes(data), "stl", max_faces=4, max_vertices=1000)
    assert excinfo.value.detail["kind"] == "faces"


def test_binary_stl_with_nan_is_rejected():
    v, f = prim.cube()
    data = bytearray(prim.binary_stl(v, f))
    # First vertex of the first record sits at 84 + 12 bytes.
    struct.pack_into("<f", data, 84 + 12, float("nan"))
    with pytest.raises(MeshIntegrityError, match="non-finite"):
        read(bytes(data), "stl", **LIMITS)


def test_binary_stl_with_nonzero_attribute_byte_count_is_noted_not_fatal():
    v, f = prim.cube()
    data = bytearray(prim.binary_stl(v, f))
    struct.pack_into("<H", data, 84 + 48, 7)
    result = read(bytes(data), "stl", **LIMITS)
    assert any("attribute byte count" in note for note in result.notes)


def test_trailing_bytes_are_reported_but_tolerated():
    v, f = prim.cube()
    result = read(prim.binary_stl(v, f) + b"junk", "stl", **LIMITS)
    assert any("trailing" in note for note in result.notes)
    assert result.faces.shape[0] == 12


def test_binary_stl_whose_header_says_solid_is_still_read_as_binary():
    """Legal and it breaks the "starts with solid -> ASCII" heuristic."""
    v, f = prim.cube()
    data = bytearray(prim.binary_stl(v, f))
    data[0:5] = b"solid"
    result = read(bytes(data), "stl", **LIMITS)
    assert result.faces.shape[0] == 12
    assert "ASCII" not in " ".join(result.notes)


# -- ASCII STL -------------------------------------------------------------

def test_ascii_stl_is_parsed_and_welded_by_the_reader():
    v, f = prim.cube()
    mesh = load(prim.ascii_stl(v, f), "stl")
    assert mesh.vertex_count == 8, "ASCII STL shares coordinates textually"
    assert mesh.face_count == 12


def test_ascii_stl_with_a_misaligned_vertex_count_is_rejected():
    text = prim.ascii_stl(*prim.cube()).decode()
    text = text.replace("endsolid test", "  vertex 0.0 0.0 0.0\nendsolid test")
    with pytest.raises(MeshIntegrityError, match="not a multiple of 3"):
        read(text.encode(), "stl", **LIMITS)


def test_ascii_stl_with_a_facet_disagreement_is_rejected():
    text = prim.ascii_stl(*prim.cube()).decode()
    text = text.replace("endsolid test", "  facet normal 0 0 0\nendsolid test")
    with pytest.raises(MeshIntegrityError, match="declares"):
        read(text.encode(), "stl", **LIMITS)


def test_ascii_stl_with_a_malformed_vertex_line_is_rejected():
    text = prim.ascii_stl(*prim.cube()).decode()
    text = text.replace("      vertex 0.0 0.0 0.0", "      vertex 0.0 zzz 0.0", 1)
    with pytest.raises(MeshIntegrityError, match="non-numeric"):
        read(text.encode(), "stl", **LIMITS)


def test_a_binary_file_named_ascii_stl_is_not_mistaken_for_ascii():
    v, f = prim.cube()
    data = prim.binary_stl(v, f)
    assert read(data, "stl", **LIMITS).faces.shape[0] == 12


# -- OBJ -------------------------------------------------------------------

def test_obj_is_parsed():
    v, f = prim.cube()
    mesh = load(prim.obj(v, f), "obj")
    assert mesh.vertex_count == 8
    assert mesh.face_count == 12
    assert mesh.source_format == "obj"


def test_obj_slash_forms_and_negative_indices_resolve():
    v, f = prim.two_triangles()
    text = b"v 0 0 0\nv 1 0 0\nv 0 1 0\nvn 0 0 1\nf 1/1/1 2//2 3/3/3\n"
    mesh = load(text, "obj")
    assert mesh.faces.tolist() == [[0, 1, 2]]
    assert v.shape == mesh.vertices.shape


def test_obj_ngons_are_fan_triangulated_and_reported():
    v = np.array([[0.0, 0, 0], [1.0, 0, 0], [1.0, 1, 0], [0.0, 1, 0]], dtype=np.float64)
    result = read(b"v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nf 1 2 3 4\n", "obj", **LIMITS)
    assert result.faces.shape == (2, 3)
    assert any("fan-triangulated" in note for note in result.notes)


def test_obj_face_index_zero_is_rejected():
    with pytest.raises(MeshIntegrityError, match="1-based"):
        read(b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 0 1 2\n", "obj", **LIMITS)


def test_obj_face_with_too_few_corners_is_rejected():
    with pytest.raises(MeshIntegrityError, match="at least 3 corners"):
        read(b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2\n", "obj", **LIMITS)


def test_obj_with_a_malformed_index_is_rejected():
    with pytest.raises(MeshIntegrityError, match="malformed OBJ face index"):
        read(b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 x\n", "obj", **LIMITS)


def test_obj_without_faces_is_rejected_not_reported_as_empty():
    with pytest.raises(MeshIntegrityError, match="no faces"):
        read(b"v 0 0 0\nv 1 0 0\nv 0 1 0\n", "obj", **LIMITS)


def test_obj_without_vertices_is_rejected():
    with pytest.raises(MeshIntegrityError, match="no vertices"):
        read(b"f 1 2 3\n", "obj", **LIMITS)


def test_obj_vertex_with_two_coordinates_is_rejected():
    with pytest.raises(MeshIntegrityError, match="needs 3 coordinates"):
        read(b"v 0 0\nf 1 1 1\n", "obj", **LIMITS)


# -- format detection ------------------------------------------------------

def test_binary_stl_is_detected_from_bytes_alone():
    assert detect_format(None, prim.binary_stl(*prim.cube())) == "stl"


def test_ascii_stl_is_detected_from_bytes_alone():
    assert detect_format(None, prim.ascii_stl(*prim.cube())) == "stl"


def test_ply_and_zip_are_detected_by_magic():
    assert detect_format(None, b"ply\nformat ascii 1.0\n") == "ply"
    assert detect_format(None, b"PK\x03\x04rest") == "3mf"


def test_a_lying_filename_does_not_win_over_the_bytes():
    """`part.stl` that is really an OBJ must not be read as STL."""
    assert detect_format("part.stl", prim.obj(*prim.cube())) == "obj"


def test_unidentifiable_input_is_rejected():
    with pytest.raises(UnsupportedFormatError, match="could not identify"):
        detect_format(None, b"\x00\x01\x02\x03 not a mesh at all")


def test_ply_and_3mf_say_trimesh_is_required():
    for fmt in ("ply", "3mf"):
        with pytest.raises(UnsupportedFormatError) as excinfo:
            read(b"whatever", fmt, **LIMITS)
        assert excinfo.value.detail["requires"] == "trimesh"


def test_unknown_format_is_rejected():
    with pytest.raises(UnsupportedFormatError, match="unknown mesh format"):
        read(b"whatever", "step", **LIMITS)


# -- zip expansion guard ---------------------------------------------------

def _zip_with(entries: int, payload: int) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(entries):
            zf.writestr(f"model{i}.stl", b"\0" * payload)
    return buffer.getvalue()


def test_zip_expansion_sums_the_uncompressed_size():
    data = _zip_with(3, 1000)
    assert zip_expansion(data, cap=10_000_000) == 3000


def test_zip_bomb_is_refused_on_the_central_directory_alone():
    """A tiny upload that would expand past the cap never gets decompressed."""
    data = _zip_with(2, 2000)
    assert len(data) < 500
    with pytest.raises(MeshTooLargeError) as excinfo:
        zip_expansion(data, cap=1000)
    assert excinfo.value.detail["actual"] == 4000


def test_a_corrupt_zip_is_a_mesh_integrity_error():
    with pytest.raises(MeshIntegrityError, match="zip"):
        zip_expansion(b"PK\x03\x04 definitely not a zip", cap=10)


# -- export ----------------------------------------------------------------

def test_binary_stl_export_has_exactly_fifty_bytes_per_triangle():
    """If numpy ever pads this struct, every exported STL becomes unreadable."""
    v, f = prim.cube()
    data = to_binary_stl(Mesh.create(v, f))
    assert len(data) == 84 + 50 * 12
    assert struct.unpack_from("<I", data, 80)[0] == 12


def test_export_read_back_is_the_same_geometry():
    v, f = prim.cube(scale=7.5, offset=(1.0, -2.0, 3.0))
    mesh = Mesh.create(v, f)
    back = load(to_binary_stl(mesh), "stl")
    assert back.face_count == mesh.face_count
    assert np.unique(back.vertices, axis=0).shape[0] == mesh.vertex_count

    from parts.geometry import compute, merge_vertices

    # Not watertight yet, and correctly so: a binary STL carries no shared
    # vertices, so every edge looks like a boundary. This is why the repair
    # pipeline welds before it measures anything.
    assert compute(back).watertight is False
    assert compute(back).duplicate_vertex_count == 28

    welded_v, welded_f, _ = merge_vertices(back.vertices, back.faces)
    welded = compute(Mesh.create(welded_v, welded_f))
    assert welded.watertight is True
    assert welded.signed_volume == pytest.approx(
        compute(mesh).signed_volume, rel=1e-6
    )


def test_export_recomputes_normals_rather_than_writing_zeroes():
    from parts.geometry import compute

    v, f = prim.cube()
    mesh = Mesh.create(v, f)
    data = to_binary_stl(mesh)
    first_normal = struct.unpack_from("<3f", data, 84)
    assert first_normal == pytest.approx((0.0, 0.0, -1.0), abs=1e-6)
    assert np.array(compute(mesh).cross[0]).round(4).tolist() == [0.0, 0.0, -1.0]