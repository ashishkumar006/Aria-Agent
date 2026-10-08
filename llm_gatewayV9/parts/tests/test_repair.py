"""Repair passes: what each one did, what it skipped, and what it refuses to do."""
from __future__ import annotations

import numpy as np
import pytest

from parts.backends import Backends
from parts.errors import RepairFailedError
from parts.geometry import compute
from parts.mesh import Mesh
from parts.repair import (
    APPLIED,
    FAILED,
    SKIPPED,
    UNAVAILABLE,
    RepairOptions,
    repair_mesh,
)
from parts.tests import primitives as prim

NO_BACKENDS = Backends(trimesh=None, manifold3d=None,
                       errors={"trimesh": "not installed", "manifold3d": "not installed"})


# -- what the passes do ----------------------------------------------------

def test_full_repair_of_a_binary_stl_cube_produces_a_printable_solid():
    """The end-to-end claim: STL in, watertight and manifold out."""
    v, f = prim.cube(scale=4.0, offset=(10.0, -3.0, 2.0))
    stl = Mesh.create(v[f].reshape(-1, 3),
                      np.arange(36, dtype=np.int64).reshape(12, 3),
                      source_format="stl")
    result = repair_mesh(stl, backends=NO_BACKENDS)
    geo = compute(result.mesh)

    assert geo.watertight is True
    assert geo.winding_consistent is True
    assert geo.duplicate_vertex_count == 0
    assert geo.unreferenced_vertex_count == 0
    from parts.geometry import inward_face_mask

    assert not inward_face_mask(geo).any()
    assert geo.signed_volume == pytest.approx(64.0, rel=1e-6)
    assert geo.vertex_count == 8
    assert geo.face_count == 12


def test_every_step_reports_a_status_and_a_reason():
    v, f = prim.broken_cube()
    result = repair_mesh(Mesh.create(v, f), backends=NO_BACKENDS)
    names = {s.name for s in result.steps}
    assert names == {
        "weld_vertices", "drop_degenerate_faces", "drop_duplicate_faces",
        "drop_unreferenced_vertices", "fix_normals", "manifold3d_watertight",
    }
    for step in result.steps:
        assert step.status in (APPLIED, SKIPPED, FAILED, UNAVAILABLE)
        # A skip without a reason is the thing the spec rules out.
        assert step.detail, f"{step.name} reported {step.status} with no detail"
        assert set(step.to_dict()) == {"name", "status", "changed", "detail"}


def test_each_defect_is_caught_by_its_own_pass(broken_mesh):
    result = repair_mesh(broken_mesh, backends=NO_BACKENDS)
    applied = {s.name: s.changed for s in result.applied}
    assert applied["drop_degenerate_faces"] == 1
    assert applied["drop_duplicate_faces"] == 1
    assert applied["drop_unreferenced_vertices"] == 1
    assert applied["fix_normals"] == 1
    assert result.step("drop_degenerate_faces").detail.startswith("1 zero-area")


def test_repair_is_idempotent(cube_mesh):
    """Running it twice must report every pass as skipped, not redo work."""
    first = repair_mesh(cube_mesh, backends=NO_BACKENDS)
    second = repair_mesh(first.mesh, backends=NO_BACKENDS)
    for name in ("weld_vertices", "drop_degenerate_faces", "drop_duplicate_faces",
                 "drop_unreferenced_vertices", "fix_normals"):
        step = second.step(name)
        assert step.status == SKIPPED, f"{name} ran again on a clean mesh"
        assert step.changed == 0
    assert second.mesh.face_count == first.mesh.face_count
    assert compute(second.mesh).signed_volume == pytest.approx(1.0, abs=1e-12)


def test_inverted_solid_is_turned_the_right_way_round(cube_mesh):
    from parts.geometry import flip_faces

    inside_out = flip_faces(cube_mesh, np.ones(cube_mesh.face_count, dtype=bool))
    assert compute(inside_out).signed_volume < 0
    result = repair_mesh(inside_out, backends=NO_BACKENDS)
    assert compute(result.mesh).signed_volume == pytest.approx(1.0, abs=1e-12)
    assert result.step("fix_normals").status == APPLIED
    assert result.step("fix_normals").changed == 12


def test_one_inverted_face_among_twelve_is_found(broken_mesh):
    result = repair_mesh(broken_mesh, backends=NO_BACKENDS)
    assert compute(result.mesh).signed_volume == pytest.approx(1.0, abs=1e-12)


def test_open_mesh_is_reported_not_closed(open_mesh):
    """Deliberate: filling a boundary would delete a bracket's through-hole."""
    result = repair_mesh(open_mesh, backends=NO_BACKENDS)
    geo = compute(result.mesh)
    assert geo.watertight is False
    assert geo.boundary_edge_count == 4


def test_disabled_passes_report_that_they_were_disabled(cube_mesh):
    options = RepairOptions(weld=False, drop_degenerate_faces=False,
                            drop_duplicate_faces=False,
                            drop_unreferenced_vertices=False, fix_normals=False,
                            watertight=False)
    result = repair_mesh(cube_mesh, options, backends=NO_BACKENDS)
    assert len(result.skipped) == 6
    for step in result.steps:
        assert step.detail == "disabled by options"
    assert result.mesh.face_count == cube_mesh.face_count


def test_unreferenced_vertices_are_gone_after_repair(broken_mesh):
    result = repair_mesh(broken_mesh, backends=NO_BACKENDS)
    assert compute(result.mesh).unreferenced_vertex_count == 0
    assert int(result.mesh.faces.max()) < result.mesh.vertex_count


def test_summary_counts_add_up(cube_mesh):
    summary = repair_mesh(cube_mesh, backends=NO_BACKENDS).to_dict()["summary"]
    assert sum(summary.values()) == 6
    assert summary[UNAVAILABLE] == 1


# -- refusing to destroy the mesh -----------------------------------------

def test_a_mesh_of_only_degenerate_faces_is_a_failure_not_an_empty_success():
    """An empty report reads as a clean bill of health. It must never be one."""
    v = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]], dtype=np.float64)
    f = np.array([[0, 1, 2], [0, 0, 0]], dtype=np.int64)
    with pytest.raises(RepairFailedError) as excinfo:
        repair_mesh(Mesh.create(v, f), backends=NO_BACKENDS)
    assert excinfo.value.code == "repair_failed"
    assert "drop_degenerate_faces" in excinfo.value.detail["steps_that_ran"]


def test_a_mesh_that_is_already_empty_never_reaches_the_repair():
    from parts.errors import EmptyMeshError

    with pytest.raises(EmptyMeshError):
        Mesh.create(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64))


# -- the manifold3d pass ---------------------------------------------------

class FakeError:
    NoError = 0
    NotManifold = 7


class FakeMeshGL:
    """Stands in for manifold3d.MeshGL, per bindings/python/examples/all_apis.py."""

    def __init__(self, vert_properties, tri_verts):
        self.vert_properties = np.asarray(vert_properties, dtype=np.float32).copy()
        self.tri_verts = np.asarray(tri_verts).copy()
        self.num_prop = 3
        self.merge_called = False

    def merge(self) -> bool:
        self.merge_called = True
        return True


class FakeManifold:
    """Canonicalises like the real constructor: weld, then drop degenerates.

    This is a stub of the *documented* API, not the library. It proves the glue
    in `_manifold3d_watertight` is wired correctly; it cannot prove the C++
    behaves this way, because manifold3d is not installed here.
    """

    def __init__(self, mesh, status=FakeError.NoError, empty=False):
        self._mesh = mesh
        self._status = status
        self._empty = empty

    def status(self):
        return self._status

    def is_empty(self):
        return self._empty

    def to_mesh(self):
        from parts.geometry import merge_vertices

        tri = self._mesh.tri_verts.reshape(-1, 3)
        verts = self._mesh.vert_properties.reshape(-1, self._mesh.num_prop)[:, :3]
        v, f, _ = merge_vertices(np.asarray(verts, dtype=np.float64), tri)
        out = FakeMeshGL(v.astype(np.float32).reshape(-1), f.astype(np.uint32).reshape(-1))
        return out


def fake_backend(*, status=FakeError.NoError, empty=False, raises=None):
    class Module:
        Error = FakeError
        MeshGL = FakeMeshGL

        def Manifold(self, mesh):  # noqa: N802 - mirrors the real name
            if raises is not None:
                raise raises
            return FakeManifold(mesh, status=status, empty=empty)

    module = Module()
    return Backends(trimesh=None, manifold3d=module, errors={})


def test_manifold_pass_is_unavailable_and_says_so(cube_mesh):
    result = repair_mesh(cube_mesh, backends=NO_BACKENDS)
    step = result.step("manifold3d_watertight")
    assert step.status == UNAVAILABLE
    assert "manifold3d is not installed" in step.detail
    assert result.unavailable == [step]


def test_manifold_pass_canonicalises_an_unwelded_stl():
    """The applied path, isolated: an STL-style mesh with no shared vertices.

    Every other pass is switched off so the only thing that can turn 36 corners
    into a watertight 8-vertex solid is the manifold3d glue.
    """
    v, f = prim.cube()
    stl = Mesh.create(
        v[f].reshape(-1, 3), np.arange(36, dtype=np.int64).reshape(12, 3),
        source_format="stl",
    )
    options = RepairOptions(
        weld=False, drop_degenerate_faces=False, drop_duplicate_faces=False,
        drop_unreferenced_vertices=False, fix_normals=False, watertight=True,
    )
    result = repair_mesh(stl, options, backends=fake_backend())
    step = result.step("manifold3d_watertight")
    assert step.status == APPLIED
    assert "MeshGL(float32, uint32)" in step.detail
    assert "merge vectors applied before construction" in step.detail
    assert "36 -> 8 vertices, 12 -> 12 triangles" in step.detail
    assert step.changed == 28, "the pass is only useful if it says what it removed"
    assert "now watertight" in step.detail
    assert compute(result.mesh).watertight is True
    assert compute(result.mesh).duplicate_vertex_count == 0


def test_manifold_pass_reports_a_non_manifold_status_without_touching_the_mesh():
    result = repair_mesh(Mesh.create(*prim.open_cube()), backends=fake_backend(status=FakeError.NotManifold))
    step = result.step("manifold3d_watertight")
    assert step.status == FAILED
    assert "status=7" in step.detail
    assert "oriented 2-manifold" in step.detail
    assert result.mesh.face_count == 10


def test_manifold_pass_refuses_an_empty_result():
    result = repair_mesh(Mesh.create(*prim.cube()), backends=fake_backend(empty=True))
    step = result.step("manifold3d_watertight")
    assert step.status == FAILED
    assert "empty solid" in step.detail
    assert result.mesh.face_count == 12, "input must be kept, not replaced by nothing"


def test_manifold_pass_survives_an_exception_from_the_extension():
    result = repair_mesh(Mesh.create(*prim.cube()), backends=fake_backend(raises=RuntimeError("segfault-ish")))
    step = result.step("manifold3d_watertight")
    assert step.status == FAILED
    assert "RuntimeError" in step.detail
    assert result.mesh.face_count == 12


def test_manifold_pass_reports_an_already_clean_mesh_as_skipped(cube_mesh):
    result = repair_mesh(cube_mesh, backends=fake_backend())
    step = result.step("manifold3d_watertight")
    assert step.status == SKIPPED
    assert "already watertight" in step.detail


def test_manifold_pass_states_that_holes_are_not_filled(open_mesh):
    result = repair_mesh(open_mesh, backends=fake_backend())
    step = result.step("manifold3d_watertight")
    # Nothing was removed, so this is a skip, not an apply - and the detail is
    # the whole point: the mesh stayed open and the pass says why.
    assert step.status == SKIPPED
    assert step.changed == 0
    assert "does not fill holes" in step.detail
    assert "boundary edge" in step.detail
    assert compute(result.mesh).boundary_edge_count == 4


def test_from_meshgl_rejects_a_short_property_buffer():
    from parts.repair import _from_meshgl

    class Broken:
        num_prop = 3
        vert_properties = np.zeros(8, dtype=np.float32)
        tri_verts = np.zeros(3, dtype=np.int64)

    with pytest.raises(ValueError, match="not a multiple"):
        _from_meshgl(Broken())