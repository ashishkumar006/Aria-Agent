"""Geometry: every claim checked against an analytically known answer.

The fixtures are hand-built solids with known volume and Euler characteristic,
so a sign error in the winding convention or an off-by-one in the edge table
shows up as a wrong number rather than as a plausible one.
"""
from __future__ import annotations

import numpy as np
import pytest

from parts.geometry import (
    compute,
    conflicting_face_pairs,
    drop_faces,
    drop_unreferenced_vertices,
    duplicate_face_mask,
    flip_faces,
    inward_face_mask,
    merge_vertices,
)
from parts.mesh import Mesh
from parts.tests import primitives as prim


# -- the numbers that pin the conventions ---------------------------------

def test_cube_volume_matches_the_analytic_value(cube_mesh):
    """Positive means the fixtures really are wound outward."""
    geo = compute(cube_mesh)
    assert geo.signed_volume == pytest.approx(1.0, abs=1e-12)
    assert geo.surface_area == pytest.approx(6.0, abs=1e-12)


def test_cube_topology(cube_mesh):
    geo = compute(cube_mesh)
    assert geo.watertight is True
    assert geo.boundary_edge_count == 0
    assert geo.nonmanifold_edge_count == 0
    assert geo.winding_conflict_edge_count == 0
    assert geo.winding_consistent is True
    # 12 triangles x 3 edges / 2 faces per edge.
    assert geo.unique_edge_count == 18
    # V - E + F for a cube is 2, and genus (2 - chi) / 2 is 0.
    assert geo.euler_characteristic == 2
    assert geo.genus == 0


def test_tetrahedron_volume_is_one_sixth(tetra_mesh):
    geo = compute(tetra_mesh)
    assert geo.signed_volume == pytest.approx(1.0 / 6.0, rel=1e-12)
    assert geo.unique_edge_count == 6
    assert geo.euler_characteristic == 2


def test_torus_is_genus_one(torus_mesh):
    """Separates the Euler arithmetic from the cube case: chi = 0, not 2."""
    geo = compute(torus_mesh)
    assert geo.watertight is True
    assert geo.euler_characteristic == 0
    assert geo.genus == 1
    assert geo.signed_volume > 0.0


def test_inverted_solid_reports_negative_signed_volume(cube_mesh):
    """The sign is what makes `normals_outward` meaningful."""
    flipped = flip_faces(cube_mesh, np.ones(cube_mesh.face_count, dtype=bool))
    geo = compute(flipped)
    assert geo.signed_volume == pytest.approx(-1.0, abs=1e-12)
    assert geo.surface_area == pytest.approx(6.0, abs=1e-12)
    assert abs(geo.signed_volume) == pytest.approx(1.0, abs=1e-12)


def test_open_cube_reports_exactly_its_missing_top(open_mesh):
    geo = compute(open_mesh)
    assert geo.watertight is False
    # The removed top was two triangles sharing one diagonal; the four outer
    # edges of that square are what is left open.
    assert geo.boundary_edge_count == 4
    assert geo.nonmanifold_edge_count == 0
    assert geo.genus is None, "genus is undefined on an open surface"
    assert geo.signed_volume < 1.0


def test_scaling_is_linear():
    v, f = prim.cube(scale=2.5)
    geo = compute(Mesh.create(v, f))
    assert geo.signed_volume == pytest.approx(2.5 ** 3, rel=1e-12)
    assert geo.surface_area == pytest.approx(6 * 2.5 ** 2, rel=1e-12)
    assert geo.bbox_diagonal == pytest.approx(2.5 * np.sqrt(3), rel=1e-12)

# -- defect detection ------------------------------------------------------

def test_degenerate_faces_are_the_zero_area_and_repeated_corner_ones(broken_mesh):
    geo = compute(broken_mesh)
    mask = geo.degenerate_face_mask
    # Exactly one: the [0, 1, 1] face. The duplicate is not degenerate.
    assert int(mask.sum()) == 1
    assert geo.faces[mask].tolist() == [[0, 1, 1]]


def test_degenerate_faces_sum_to_zero_area():
    """A triangle with three collinear corners encloses nothing."""
    v = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]], dtype=np.float64)
    f = np.array([[0, 1, 2]], dtype=np.int64)
    geo = compute(Mesh.create(v, f))
    assert geo.surface_area == 0.0
    assert bool(geo.degenerate_face_mask[0])


def test_duplicate_faces_ignore_winding_order(broken_mesh):
    """(0,3,2) and (2,3,0) are the same triangle, not two surfaces."""
    mask = duplicate_face_mask(broken_mesh.faces)
    assert int(mask.sum()) == 1
    assert broken_mesh.faces[mask].tolist() == [[0, 3, 2]]


def test_duplicate_face_mask_keeps_the_first_of_each_set():
    faces = np.array([[0, 1, 2], [2, 1, 0], [0, 1, 2]], dtype=np.int64)
    mask = duplicate_face_mask(faces)
    assert list(mask) == [False, True, True]


def test_unreferenced_vertices_are_counted(broken_mesh):
    geo = compute(broken_mesh)
    assert geo.vertex_count == 9
    assert geo.unreferenced_vertex_count == 1
    assert 8 not in set(geo.referenced_vertices.tolist())


def test_inward_faces_are_detected(broken_mesh):
    """One triangle was deliberately reversed in the fixture."""
    geo = compute(broken_mesh)
    inward = inward_face_mask(geo)
    assert int(inward.sum()) == 1
    assert geo.faces[inward].tolist() == [[4, 6, 5]]


def test_a_solid_has_no_inward_faces(cube_mesh):
    assert not inward_face_mask(compute(cube_mesh)).any()


def test_nonmanifold_edges_are_counted():
    """Three triangles on one edge is the classic non-manifold bowtie."""
    v = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    f = np.array([[0, 1, 2], [1, 0, 2], [0, 1, 3]], dtype=np.int64)
    geo = compute(Mesh.create(v, f))
    assert geo.nonmanifold_edge_count == 1
    assert geo.watertight is False


def test_winding_conflicts_are_local_not_global():
    """Two triangles sharing an edge the same way contradict each other.

    Note the fixture: two triangles over the *same three* vertices always
    traverse their shared edges in opposite directions, whatever the winding.
    A conflict needs a genuine shared edge between different vertex triples -
    a fan of two triangles off one edge.
    """
    v = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0], [0.0, 1.0, 0.0]],
        dtype=np.float64,
    )
    fan = np.array([[0, 1, 2], [0, 1, 3]], dtype=np.int64)
    opposed = np.array([[0, 1, 2], [0, 3, 1]], dtype=np.int64)

    wrong = compute(Mesh.create(v, fan))
    assert wrong.winding_conflict_edge_count == 1
    assert wrong.winding_consistent is False

    right = compute(Mesh.create(v, opposed))
    assert right.winding_conflict_edge_count == 0
    assert right.winding_consistent is True


def test_same_vertex_triple_never_conflicts_regardless_of_winding():
    """Reverse order is an inverted duplicate, not a winding conflict."""
    v = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0]], dtype=np.float64)
    for order in ([0, 1, 2], [0, 2, 1], [2, 1, 0]):
        geo = compute(Mesh.create(v, np.array([order], dtype=np.int64)))
        assert geo.winding_conflict_edge_count == 0


def test_conflicting_face_pairs_names_both_faces():
    v = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0], [0.0, 1.0, 0.0]],
        dtype=np.float64,
    )
    geo = compute(Mesh.create(v, np.array([[0, 1, 2], [0, 1, 3]], dtype=np.int64)))
    first, second = conflicting_face_pairs(geo)
    assert sorted(first.tolist() + second.tolist()) == [0, 1]


def test_duplicate_vertex_count_is_zero_for_a_shared_vertex_box(cube_mesh):
    assert compute(cube_mesh).duplicate_vertex_count == 0

# -- edits -----------------------------------------------------------------

def test_exact_weld_collapses_a_binary_stl_cube():
    """A binary STL has no vertex sharing, so a cube arrives as 36 corners."""
    v, f = prim.cube()
    stl_vertices = v[f].reshape(-1, 3)
    stl_faces = np.arange(36, dtype=np.int64).reshape(12, 3)
    assert np.unique(stl_vertices, axis=0).shape[0] == 8

    welded_v, welded_f, removed = merge_vertices(stl_vertices, stl_faces)
    assert welded_v.shape[0] == 8
    assert removed == 28
    geo = compute(Mesh.create(welded_v, welded_f))
    assert geo.watertight is True
    assert geo.signed_volume == pytest.approx(1.0, abs=1e-12)


def test_weld_preserves_geometry_exactly():
    v, f = prim.cube(scale=3.0, offset=(1.5, -2.0, 0.25))
    stl_vertices = v[f].reshape(-1, 3)
    stl_faces = np.arange(3 * len(f), dtype=np.int64).reshape(len(f), 3)
    welded_v, welded_f, _ = merge_vertices(stl_vertices, stl_faces)
    assert welded_v.shape[0] == len(v)
    # Every original corner must still be present, bit for bit.
    original = set(map(tuple, np.unique(stl_vertices, axis=0).tolist()))
    assert original == set(map(tuple, welded_v.tolist()))
    geo = compute(Mesh.create(welded_v, welded_f))
    assert geo.signed_volume == pytest.approx(27.0, rel=1e-12)


def test_tolerance_weld_merges_near_but_distinct_corners():
    v = np.array([[0.0, 0.0, 0.0], [1e-7, 0.0, 0.0], [1.0, 0.0, 0.0]], dtype=np.float64)
    f = np.array([[0, 1, 2]], dtype=np.int64)
    _, _, exact_removed = merge_vertices(v, f)
    assert exact_removed == 0

    near_v, _, near_removed = merge_vertices(v, f, tolerance=1e-5)
    assert near_removed == 1
    assert near_v.shape[0] == 2


def test_tolerance_weld_never_invents_a_position():
    """The surviving coordinate is one that was in the input, not an average."""
    v = np.array([[0.0, 0.0, 0.0], [1e-7, 0.0, 0.0]], dtype=np.float64)
    welded, _, _ = merge_vertices(v, np.array([[0, 1, 1]], dtype=np.int64), tolerance=1e-5)
    assert list(welded[0]) in (list(v[0]), list(v[1]))


def test_negative_merge_tolerance_is_rejected():
    from parts.errors import MeshIntegrityError

    v, f = prim.cube()
    with pytest.raises(MeshIntegrityError):
        merge_vertices(v, f, tolerance=-1.0)


def test_absurdly_small_tolerance_is_rejected_not_overflowed():
    """An int64 quantiser overflow would silently produce nonsense indices."""
    from parts.errors import MeshIntegrityError

    v, f = prim.cube()
    with pytest.raises(MeshIntegrityError, match="quantise"):
        merge_vertices(v, f, tolerance=1e-30)


def test_drop_unreferenced_compacts_and_remaps(broken_mesh):
    compacted, removed = drop_unreferenced_vertices(broken_mesh)
    assert removed == 1
    assert compacted.vertex_count == 8
    assert int(compacted.faces.max()) == 7
    geo = compute(compacted)
    assert geo.unreferenced_vertex_count == 0
    # Removing an unused corner must not move the solid. The volume is 2/3, not
    # 1: the fixture's inverted face contributes -1/6 in place of its +1/6.
    assert geo.signed_volume == pytest.approx(2.0 / 3.0, abs=1e-12)


def test_drop_faces_leaves_the_rest_untouched(cube_mesh):
    keep = np.zeros(cube_mesh.face_count, dtype=bool)
    keep[:6] = True
    trimmed = drop_faces(cube_mesh, ~keep)
    assert trimmed.face_count == 6
    assert trimmed.vertex_count == cube_mesh.vertex_count


def test_flip_faces_reverses_winding_only_for_the_masked_faces(cube_mesh):
    mask = np.zeros(cube_mesh.face_count, dtype=bool)
    mask[2] = True
    flipped = flip_faces(cube_mesh, mask)
    assert list(flipped.faces[2]) == [4, 6, 5]
    assert list(flipped.faces[0]) == list(cube_mesh.faces[0])
    # Flipping a triangle negates its contribution, so the total drops by
    # exactly twice that contribution: 1 - 2*(1/6) = 2/3.
    assert compute(flipped).signed_volume == pytest.approx(2.0 / 3.0, abs=1e-12)
    assert compute(flipped).surface_area == pytest.approx(6.0, abs=1e-12)


def test_flipping_a_face_touching_the_origin_leaves_the_volume_alone(cube_mesh):
    """Face 0 is (0, 3, 2) and v0 is the origin, so its contribution is zero.

    Worth pinning down because it is the reason `signed_volume` cannot be used
    to detect a single inverted face on a mesh whose origin sits on it.
    """
    mask = np.zeros(cube_mesh.face_count, dtype=bool)
    mask[0] = True
    assert compute(flip_faces(cube_mesh, mask)).signed_volume == pytest.approx(1.0, abs=1e-12)