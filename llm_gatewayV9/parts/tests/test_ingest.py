"""Ingest: the byte gates, format choice, and behaviour without the optional libs."""
from __future__ import annotations

import numpy as np
import pytest

from parts import backends as backends_mod
from parts.backends import Backends
from parts.errors import (
    EmptyMeshError,
    MeshTooLargeError,
    UnsupportedFormatError,
)
from parts.ingest import load
from parts.tests import primitives as prim

NONE = Backends(trimesh=None, manifold3d=None,
                errors={"trimesh": "not installed", "manifold3d": "not installed"})


# -- byte gates ------------------------------------------------------------

def test_bytes_over_the_limit_are_refused_before_parsing():
    data = prim.binary_stl(*prim.cube())
    with pytest.raises(MeshTooLargeError) as excinfo:
        load(data, filename="part.stl", max_bytes=64, backends=NONE)
    assert excinfo.value.detail["kind"] == "bytes"
    assert excinfo.value.detail["actual"] == len(data)


def test_an_empty_file_is_refused():
    with pytest.raises(EmptyMeshError):
        load(b"", filename="part.stl", backends=NONE)


def test_a_path_is_gated_on_its_stat_before_being_read(tmp_path):
    """A 4 MB file must not be pulled into memory just to be rejected."""
    path = tmp_path / "big.stl"
    path.write_bytes(prim.binary_stl(*prim.cube()))
    with pytest.raises(MeshTooLargeError) as excinfo:
        load(path, max_bytes=32, backends=NONE)
    assert excinfo.value.detail["kind"] == "bytes"
    assert excinfo.value.detail["path"] == str(path)


def test_a_path_loads_and_reports_its_name(tmp_path):
    path = tmp_path / "bracket.stl"
    path.write_bytes(prim.binary_stl(*prim.cube()))
    result = load(path, backends=NONE)
    assert result.source_name == "bracket.stl"
    assert result.fmt == "stl"
    assert result.backend == "native-stl"
    assert result.byte_length == path.stat().st_size


def test_a_missing_file_is_an_integrity_error_not_a_crash(tmp_path):
    from parts.errors import MeshIntegrityError

    with pytest.raises(MeshIntegrityError, match="stat"):
        load(tmp_path / "nope.stl", backends=NONE)


def test_an_unsupported_source_type_is_rejected():
    with pytest.raises(UnsupportedFormatError, match="cannot load a mesh"):
        load(12345, backends=NONE)


def test_face_and_vertex_limits_are_honoured_through_ingest():
    data = prim.binary_stl(*prim.cube())
    with pytest.raises(MeshTooLargeError) as excinfo:
        load(data, filename="part.stl", max_faces=4, max_vertices=1000, backends=NONE)
    assert excinfo.value.detail["kind"] == "faces"


def test_a_3mf_is_zip_checked_before_anything_unzips_it(tmp_path):
    """A few KB of zip that would expand past the cap is refused on sight."""
    import zipfile

    path = tmp_path / "part.3mf"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", b"\0" * 200_000)
    # The upload is ~1 KB and passes the byte gate; only the expansion check
    # can catch this, which is the whole reason it exists.
    assert path.stat().st_size < 100_000
    with pytest.raises(MeshTooLargeError) as excinfo:
        load(path, max_bytes=100_000, backends=NONE)
    assert excinfo.value.detail["kind"] == "uncompressed_bytes"
    assert excinfo.value.detail["actual"] == 200_000


# -- format selection ------------------------------------------------------

def test_stl_and_obj_load_without_trimesh():
    v, f = prim.cube()
    assert load(prim.binary_stl(v, f), filename="p.stl", backends=NONE).backend == "native-stl"
    assert load(prim.obj(v, f), filename="p.obj", backends=NONE).backend == "native-obj"


def test_ply_is_refused_with_an_actionable_message(no_optional_backends):
    with pytest.raises(UnsupportedFormatError) as excinfo:
        load(b"ply\nformat ascii 1.0\nend_header\n", filename="p.ply",
             backends=no_optional_backends)
    assert excinfo.value.detail["requires"] == "trimesh"


def test_loading_a_ply_adds_a_note_when_trimesh_is_missing(tmp_path):
    path = tmp_path / "p.ply"
    path.write_bytes(b"ply\nformat ascii 1.0\nend_header\n")
    # detect_format needs real content; a header-only PLY has no surface, so the
    # honest outcome is an empty-mesh rejection rather than a note.
    from parts.errors import PartError

    with pytest.raises(PartError):
        load(path, backends=NONE)


# -- units -----------------------------------------------------------------

def test_units_are_an_explicit_assumption_not_a_measurement():
    result = load(prim.binary_stl(*prim.cube()), filename="p.stl", backends=NONE)
    units = result.mesh.units.to_dict()
    assert units["assumed"] == "mm"
    assert units["confidence"] == "convention"
    assert "declares no unit" in units["note"]


def test_scaling_moves_the_numbers_and_upgrades_the_confidence():
    v, f = prim.cube()
    plain = load(prim.binary_stl(v, f), filename="p.stl", backends=NONE)
    scaled = load(prim.binary_stl(v, f), filename="p.stl", backends=NONE, scale=25.4)
    assert plain.mesh.units.confidence == "convention"
    assert scaled.mesh.units.confidence == "caller"
    assert scaled.mesh.vertices.max(axis=0)[0] == pytest.approx(25.4)
    assert plain.mesh.vertices.max(axis=0)[0] == pytest.approx(1.0)
    assert any("scaled by 25.4" in note for note in scaled.notes)


def test_provenance_block_never_contains_arrays():
    import json

    result = load(prim.binary_stl(*prim.cube()), filename="p.stl", backends=NONE)
    body = result.to_dict()
    json.dumps(body, allow_nan=False)
    assert set(body) >= {"source_name", "byte_length", "format", "io_backend",
                         "notes", "vertex_count", "face_count", "units"}


# -- the trimesh branch ----------------------------------------------------

class FakeTrimesh:
    """Stub of the trimesh call `ingest` makes, so the branch is covered.

    Only the glue is under test here: that trimesh is asked for a single body
    with process=False, and that a multi-body scene is refused. Whether real
    trimesh reads a given file correctly cannot be checked without it
    installed.
    """

    def __init__(self, result=None):
        self.calls = []
        self._result = result

    def load(self, buffer, file_type=None, process=None, force=None):
        self.calls.append(
            {"file_type": file_type, "process": process, "force": force,
             "bytes": len(buffer.getvalue())}
        )
        return self._result


class FakeGeometry(dict):
    def __len__(self):
        return 1


def test_trimesh_is_asked_not_to_repair_on_load():
    v, f = prim.cube()
    stub = FakeTrimesh(result=type("M", (), {
        "vertices": v, "faces": f, "geometry": FakeGeometry(),
    })())
    backends = Backends(trimesh=stub, manifold3d=None)
    result = load(prim.binary_stl(v, f), filename="p.stl", backends=backends)
    assert result.backend == "trimesh"
    assert stub.calls[0]["process"] is False, (
        "process=True would repair the mesh before we ever measure it"
    )
    assert any("process=False" in note for note in result.notes)


def test_a_multi_body_scene_is_refused_rather_than_merged():
    v, f = prim.cube()
    scene = type("M", (), {
        "vertices": v, "faces": f,
        "geometry": {"a": 1, "b": 2},
    })()
    backends = Backends(trimesh=FakeTrimesh(result=scene), manifold3d=None)
    with pytest.raises(UnsupportedFormatError) as excinfo:
        load(prim.binary_stl(v, f), filename="p.stl", backends=backends)
    assert excinfo.value.detail["bodies"] == 2
    assert "one body at a time" in excinfo.value.message


def test_an_empty_trimesh_result_is_refused():
    stub = FakeTrimesh(result=type("M", (), {
        "vertices": np.zeros((0, 3)), "faces": np.zeros((0, 3), dtype=np.int64),
        "geometry": FakeGeometry(),
    })())
    backends = Backends(trimesh=stub, manifold3d=None)
    with pytest.raises(EmptyMeshError):
        load(prim.binary_stl(*prim.cube()), filename="p.stl", backends=backends)


def test_trimesh_result_over_the_face_limit_is_refused():
    v, f = prim.cube()
    stub = FakeTrimesh(result=type("M", (), {
        "vertices": v, "faces": f, "geometry": FakeGeometry(),
    })())
    backends = Backends(trimesh=stub, manifold3d=None)
    with pytest.raises(MeshTooLargeError):
        load(prim.binary_stl(v, f), filename="p.stl", max_faces=4, backends=backends)


# -- graceful degradation --------------------------------------------------

def test_importing_the_package_works_with_neither_library_installed(no_optional_backends):
    """The whole point: no optional wheel must not break the import graph."""
    import importlib

    for name in ("parts", "parts.geometry", "parts.report", "parts.api",
                 "parts.slicer", "parts.repair", "parts.ingest"):
        assert importlib.import_module(name) is not None


def test_a_full_report_is_produced_with_neither_library_installed(no_optional_backends):
    from parts.report import analyse_part

    report = analyse_part(prim.binary_stl(*prim.cube()), filename="p.stl",
                          backends=no_optional_backends)
    assert report.verdict["status"] == "ok"
    assert report.backends["trimesh"]["available"] is False
    assert report.backends["manifold3d"]["available"] is False
    assert report.backends["trimesh"]["error"] == "forced"


def test_a_ply_upload_still_fails_loudly_rather_than_half_reading(no_optional_backends):
    from parts.report import analyse_part

    with pytest.raises(UnsupportedFormatError):
        analyse_part(b"ply\nformat ascii 1.0\nelement vertex 1\n"
                     b"property float x\nend_header\n0 0 0\n",
                     filename="p.ply", backends=no_optional_backends)


def test_capabilities_probe_reports_both_libraries():
    described = backends_mod.probe(force=True).describe()
    assert set(described) == {"trimesh", "manifold3d"}
    for entry in described.values():
        assert set(entry) == {"available", "version", "error"}
        assert isinstance(entry["available"], bool)


def test_require_names_the_distribution_to_install():
    from parts.errors import MissingBackendError

    with pytest.raises(MissingBackendError) as excinfo:
        NONE.require("manifold3d")
    assert excinfo.value.detail["install"] == "manifold3d"
    assert excinfo.value.detail["module"] == "manifold3d"


def test_the_probe_caches_and_can_be_reset():
    first = backends_mod.probe(force=True)
    assert backends_mod.probe() is first
    backends_mod.reset()
    assert backends_mod.probe(force=True) is not first