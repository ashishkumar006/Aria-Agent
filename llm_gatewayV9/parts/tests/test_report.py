"""The report: shape, serialisability, verdict, and the absence of stdout."""
from __future__ import annotations

import json

import numpy as np
import pytest

from parts.backends import Backends
from parts.errors import UnsupportedFormatError
from parts.report import (
    SCHEMA_VERSION,
    analyse_part,
    error_report,
    validate_part,
)
from parts.tests import primitives as prim

NONE = Backends(trimesh=None, manifold3d=None,
                errors={"trimesh": "absent", "manifold3d": "absent"})


def stl_cube(**kwargs):
    return prim.binary_stl(*prim.cube(scale=5.0, offset=(2.0, 0.0, -1.0)))


# -- shape -----------------------------------------------------------------

def test_report_has_the_documented_top_level_keys():
    report = analyse_part(stl_cube(), filename="p.stl", backends=NONE)
    assert set(report.to_dict()) == {
        "schema_version", "source", "backends", "units", "before", "analysis",
        "repair", "issues", "verdict", "slicer",
    }
    assert report.schema_version == SCHEMA_VERSION


def test_a_clean_stl_is_ok_with_every_number_populated():
    report = analyse_part(stl_cube(), filename="p.stl", backends=NONE)
    assert report.verdict == {"status": "ok", "errors": 0, "warnings": 0, "printable": True}
    assert report.ok is True
    assert report.usable is True
    assert report.analysis["topology"]["watertight"] is True
    assert report.analysis["topology"]["manifold"] is True
    assert report.analysis["geometry"]["volume"] == pytest.approx(125.0, rel=1e-6)
    assert report.units["volume_mm3"] == pytest.approx(125.0, rel=1e-6)
    assert report.units["bbox_mm"] is not None


def test_before_and_after_differ_only_where_the_repair_worked():
    report = analyse_part(stl_cube(), filename="p.stl", backends=NONE)
    assert report.before["counts"]["duplicate_vertices"] == 28
    assert report.analysis["counts"]["duplicate_vertices"] == 0
    assert report.before["counts"]["triangles"] == report.analysis["counts"]["triangles"]
    assert report.before["topology"]["watertight"] is False, (
        "an unwelded STL has no shared edges, so it is genuinely open before repair"
    )
    assert report.analysis["topology"]["watertight"] is True


def test_validate_part_changes_nothing():
    report = validate_part(stl_cube(), filename="p.stl", backends=NONE)
    assert report.repair is None
    assert report.before == report.analysis
    assert report.analysis["counts"]["duplicate_vertices"] == 28


def test_an_unwelded_stl_reports_open_and_says_why():
    """The most common false alarm in this module, and it must self-explain.

    STL stores no shared vertices, so every exported STL has boundary edges on
    all of its faces and measures as not watertight. Reporting that as a bare
    error sends users hunting for a hole that is not there; reporting it as a
    warning would under-report real holes. So: error, with the cause named.
    """
    report = validate_part(stl_cube(), filename="p.stl", backends=NONE)
    assert report.verdict["status"] == "unusable"
    message = [i.message for i in report.issues if i.code == "not_watertight"][0]
    assert "28 coincident vertices" in message
    assert "36 boundary edge(s)" in message
    assert "the weld pass will usually clear this" in message

    # A genuinely open mesh gets no such excuse. ASCII STL is used here because
    # the reader welds on parse, so the mesh reaches validate with zero
    # coincident vertices and the boundary edges are real.
    hole = validate_part(prim.ascii_stl(*prim.open_cube()), filename="p.stl",
                         backends=NONE)
    assert hole.analysis["counts"]["duplicate_vertices"] == 0
    assert hole.analysis["topology"]["boundary_edges"] == 4
    hole_message = [i.message for i in hole.issues if i.code == "not_watertight"][0]
    assert "coincident" not in hole_message


def test_repair_block_lists_every_step_with_its_reason():
    report = analyse_part(stl_cube(), filename="p.stl", backends=NONE)
    block = report.repair
    assert set(block) == {"steps", "summary"}
    assert sum(block["summary"].values()) == 6
    applied = [s for s in block["steps"] if s["status"] == "applied"]
    assert {s["name"] for s in applied} >= {"weld_vertices"}
    for step in block["steps"]:
        assert step["detail"], step["name"]


def test_a_broken_part_is_unusable_with_errors_listed():
    v, f = prim.broken_cube()
    report = analyse_part(prim.obj(v, f), filename="p.obj", backends=NONE)
    assert report.verdict["status"] == "ok", "repair fixes every planted defect"
    assert report.repair["summary"]["applied"] >= 4

    # Validate-only on the same file is a different, worse answer, and both
    # have to be reported honestly.
    broken = validate_part(prim.obj(v, f), filename="p.obj", backends=NONE)
    assert broken.verdict["status"] == "unusable"
    assert broken.verdict["printable"] is False
    assert {i.code for i in broken.issues} >= {"nonmanifold_edges", "not_watertight"}


def test_issue_codes_and_severities_are_stable_strings():
    report = analyse_part(prim.binary_stl(*prim.open_cube()), filename="p.stl",
                          backends=NONE)
    for issue in report.issues:
        assert isinstance(issue.code, str) and issue.code.islower()
        assert issue.severity in ("error", "warn", "info")
        assert set(issue.to_dict()) == {"code", "severity", "message"}


# -- serialisability -------------------------------------------------------

def test_report_is_strict_json():
    report = analyse_part(stl_cube(), filename="p.stl", backends=NONE)
    text = report.to_json()
    assert json.loads(text)["schema_version"] == SCHEMA_VERSION


def test_no_numpy_scalars_reach_the_serialised_report():
    """A numpy float in a dict is a 500 on some machines and not others."""
    def walk(node, path="root"):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")
        else:
            assert not isinstance(node, np.generic), f"{path}: {node!r}"

    walk(analyse_part(stl_cube(), filename="p.stl", backends=NONE).to_dict())


def test_nothing_is_printed(capsys):
    """Called from a FastAPI route, stdout is not an output channel."""
    analyse_part(stl_cube(), filename="p.stl", backends=NONE)
    analyse_part(stl_cube(), filename="p.stl", repair=False, backends=NONE)
    try:
        analyse_part(b"nonsense", filename="p.stl", backends=NONE)
    except Exception:
        pass
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_backends_block_says_what_produced_the_numbers():
    report = analyse_part(stl_cube(), filename="p.stl", backends=NONE)
    assert report.backends["manifold3d"]["available"] is False
    assert report.backends["trimesh"]["available"] is False
    assert "manifold3d is not installed" in (
        [s for s in report.repair["steps"] if s["name"] == "manifold3d_watertight"][0]["detail"]
    )


# -- errors ----------------------------------------------------------------

def test_error_report_has_one_shape_for_every_failure():
    body = error_report(UnsupportedFormatError("no such format", format="step"))
    assert body["ok"] is False
    assert body["error"]["code"] == "unsupported_format"
    assert body["error"]["detail"] == {"format": "step"}
    assert body["verdict"]["status"] == "error"
    assert body["verdict"]["printable"] is False
    assert body["issues"] == []
    json.dumps(body, allow_nan=False)


def test_a_rejected_upload_produces_no_report_at_all():
    """Fail loudly: an empty analysis reads as a clean bill of health."""
    with pytest.raises(UnsupportedFormatError):
        analyse_part(b"ply\nformat ascii 1.0\n", filename="p.ply", backends=NONE)


# -- the slicer seam -------------------------------------------------------

def test_the_slicer_seam_receives_the_repaired_mesh():
    seen = {}

    def slicer(mesh):
        seen["vertex_count"] = mesh.vertex_count
        seen["face_count"] = mesh.face_count
        return {"status": "ok", "detail": "stub"}

    report = analyse_part(stl_cube(), filename="p.stl", backends=NONE, slicer=slicer)
    assert seen == {"vertex_count": 8, "face_count": 12}, "must slice what was repaired"
    assert report.slicer == {"status": "ok", "detail": "stub"}


def test_slicer_defaults_to_absent():
    assert analyse_part(stl_cube(), filename="p.stl", backends=NONE).slicer is None