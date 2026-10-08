"""Validation: the measurement object and the findings derived from it."""
from __future__ import annotations

import json

import numpy as np
import pytest

from parts.mesh import Mesh
from parts.report import issues_for
from parts.tests import primitives as prim
from parts.validate import validate_mesh, volume_is_degenerate


def test_a_clean_cube_validates_as_solid(cube_mesh):
    v = validate_mesh(cube_mesh)
    assert v.vertex_count == 8
    assert v.face_count == 12
    assert v.referenced_vertex_count == 8
    assert v.unreferenced_vertex_count == 0
    assert v.duplicate_vertex_count == 0
    assert v.degenerate_face_count == 0
    assert v.duplicate_face_count == 0
    assert v.watertight is True
    assert v.manifold is True
    assert v.boundary_edge_count == 0
    assert v.nonmanifold_edge_count == 0
    assert v.winding_conflict_edge_count == 0
    assert v.inward_face_count == 0
    assert v.normals_outward is True
    assert v.orientation_confidence == "high"
    assert v.volume == pytest.approx(1.0, abs=1e-12)
    assert v.surface_area == pytest.approx(6.0, abs=1e-12)


def test_bounding_box_is_reported_per_axis(cube_mesh):
    v = validate_mesh(cube_mesh)
    assert v.bbox.min == [0.0, 0.0, 0.0]
    assert v.bbox.max == [1.0, 1.0, 1.0]
    assert v.bbox.extents == [1.0, 1.0, 1.0]
    assert v.bbox.diagonal == pytest.approx(np.sqrt(3.0))


def test_face_area_statistics_bracket_the_total(cube_mesh):
    v = validate_mesh(cube_mesh)
    assert v.min_face_area == pytest.approx(0.5)
    assert v.max_face_area == pytest.approx(0.5)
    assert v.mean_face_area == pytest.approx(0.5)
    assert v.mean_face_area * v.face_count == pytest.approx(v.surface_area)


def test_volume_is_absolute_but_the_sign_is_kept(cube_mesh):
    from parts.geometry import flip_faces

    inside_out = flip_faces(cube_mesh, np.ones(cube_mesh.face_count, dtype=bool))
    v = validate_mesh(inside_out)
    assert v.signed_volume < 0
    assert v.volume == pytest.approx(1.0)
    assert v.inward_face_count == 12
    assert v.normals_outward is False


def test_open_mesh_is_advisory_not_authoritative(open_mesh):
    """A heuristic on an unclosed surface must not claim high confidence."""
    v = validate_mesh(open_mesh)
    assert v.watertight is False
    assert v.manifold is False
    assert v.genus is None
    assert v.orientation_confidence == "advisory"


def test_a_duplicate_face_makes_three_edges_non_manifold():
    """A repeated triangle is not merely redundant - it is unsolidifiable.

    Doubling a triangle puts three faces on each of its three edges, which is
    the definition of a non-manifold edge. This is why the duplicate-face pass
    has to run before anything reports topology. The fixture adds nothing but
    the duplicate, so the count of three is derived rather than incidental.
    """
    v, f = prim.cube()
    result = validate_mesh(Mesh.create(v, np.vstack([f, f[[0]]])))
    assert result.duplicate_face_count == 1
    assert result.degenerate_face_count == 0
    assert result.nonmanifold_edge_count == 3
    assert result.watertight is False
    assert result.manifold is False


def test_validation_dict_is_json_serialisable(cube_mesh):
    body = validate_mesh(cube_mesh).to_dict()
    assert set(body) == {"source_format", "counts", "topology", "normals", "geometry"}
    json.dumps(body, allow_nan=False)
    assert body["counts"]["triangles"] == 12
    assert body["topology"]["watertight"] is True


def test_validation_dict_contains_no_arrays(cube_mesh):
    """`geometry` holds numpy arrays and must never reach the report."""

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        else:
            assert not isinstance(node, np.ndarray), f"leaked array: {node!r}"

    walk(validate_mesh(cube_mesh).to_dict())


def test_every_number_in_the_dict_is_a_plain_python_scalar(cube_mesh):
    def walk(node):
        if isinstance(node, dict):
            return all(walk(v) for v in node.values())
        if isinstance(node, list):
            return all(walk(v) for v in node)
        assert not isinstance(node, np.generic), f"numpy scalar leaked: {node!r}"
        return True

    assert walk(validate_mesh(cube_mesh).to_dict())


# -- derived findings ------------------------------------------------------

def test_a_clean_cube_raises_no_issues(cube_mesh):
    assert issues_for(validate_mesh(cube_mesh)) == []


def test_broken_cube_reports_each_defect_and_its_consequences(broken_mesh):
    found = {i.code: i for i in issues_for(validate_mesh(broken_mesh))}
    # The four planted defects.
    assert {"degenerate_faces", "duplicate_faces", "unreferenced_vertices",
            "inverted_normals"} <= set(found)
    # And the topology damage the duplicate face causes downstream of them.
    assert {"nonmanifold_edges", "not_watertight"} <= set(found)
    assert found["nonmanifold_edges"].severity == "error"
    assert found["degenerate_faces"].severity == "warn"
    # The messages carry the counts a user needs to act on, not just a label.
    assert "edge(s) are shared by three or more" in found["nonmanifold_edges"].message
    assert "boundary edge(s) belong to only one triangle" in found["not_watertight"].message


def test_open_mesh_is_an_error_not_a_warning(open_mesh):
    issues = issues_for(validate_mesh(open_mesh))
    codes = {i.code for i in issues}
    assert "not_watertight" in codes
    assert [i for i in issues if i.code == "not_watertight"][0].severity == "error"


def test_nonmanifold_edges_are_an_error():
    v = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    f = np.array([[0, 1, 2], [1, 0, 2], [0, 1, 3]], dtype=np.int64)
    issues = issues_for(validate_mesh(Mesh.create(v, f)))
    assert "nonmanifold_edges" in {i.code for i in issues}
    assert [i for i in issues if i.code == "nonmanifold_edges"][0].severity == "error"


def test_a_flat_sheet_is_flagged_as_zero_volume():
    v, f = prim.two_triangles()
    codes = {i.code for i in issues_for(validate_mesh(Mesh.create(v, f)))}
    assert "zero_volume" in codes
    assert "flat_mesh" in codes


def test_duplicate_vertices_are_informational_not_a_defect():
    """Binary STL duplicates every corner; calling that a warning is noise."""
    v, f = prim.cube()
    stl = Mesh.create(v[f].reshape(-1, 3), np.arange(36, dtype=np.int64).reshape(12, 3))
    issue = [i for i in issues_for(validate_mesh(stl)) if i.code == "duplicate_vertices"][0]
    assert issue.severity == "info"
    assert "STL" in issue.message


def test_the_orientation_heuristic_says_when_it_is_guessing(cube_mesh):
    """Closed and inverted: authoritative. Open and inverted: a guess."""
    from parts.geometry import flip_faces

    inverted = flip_faces(cube_mesh, np.ones(cube_mesh.face_count, dtype=bool))
    closed = validate_mesh(inverted)
    assert closed.watertight is True
    assert closed.orientation_confidence == "high"
    closed_issue = {i.code: i.message for i in issues_for(closed)}["inverted_normals"]
    assert "heuristic" not in closed_issue

    # Open *and* inverted: remove the top of the cube, then invert one of the
    # triangles that remain, so the heuristic has something to be unsure about.
    v, f = prim.open_cube()
    f = f.copy()
    f[0] = f[0][::-1]
    open_mesh = Mesh.create(v, f)
    open_issue = {i.code: i.message for i in issues_for(validate_mesh(open_mesh))}
    assert validate_mesh(open_mesh).orientation_confidence == "advisory"
    assert "the mesh is not closed" in open_issue["inverted_normals"]


# -- degenerate volume heuristic ------------------------------------------

def test_volume_is_degenerate_compares_against_the_bounding_box():
    assert volume_is_degenerate(validate_mesh(Mesh.create(*prim.two_triangles())))
    assert not volume_is_degenerate(validate_mesh(Mesh.create(*prim.cube())))


def test_the_degenerate_volume_test_is_scale_free():
    """A 0.05 mm cube is a real solid; a sheet of the same size is not.

    An absolute threshold would call the first one empty and pass the second
    only by luck. The test compares volume against the bounding box, so it has
    to be checked at wildly different scales to mean anything.
    """
    tiny = Mesh.create(*prim.cube(scale=0.05))
    big = Mesh.create(*prim.cube(scale=500.0))
    sheet = Mesh.create(*prim.two_triangles())
    assert not volume_is_degenerate(validate_mesh(tiny))
    assert not volume_is_degenerate(validate_mesh(big))
    assert volume_is_degenerate(validate_mesh(sheet))
    assert validate_mesh(tiny).volume == pytest.approx(0.05 ** 3, rel=1e-12)