"""The serialisable report, and the orchestration that produces it.

No prints anywhere: this is a library called from a FastAPI route, so the only
output channel is the returned object. Every field is a plain Python scalar so
`json.dumps(report.to_dict())` always works - a numpy float leaking into the
dict is the classic way an endpoint starts returning 500s on one machine only.

`analyse_part` is the one entry point: load, measure, repair, re-measure.
Both the before and after measurements come from `validate.validate_mesh`, so a
report never compares two different implementations of the same question.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from . import backends as backends_mod
from .errors import PartError
from .ingest import LoadResult, load
from .mesh import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_FACES,
    DEFAULT_MAX_VERTICES,
    Mesh,
)
from .repair import RepairOptions, RepairResult, repair_mesh
from .validate import Validation, validate_mesh, volume_is_degenerate

SCHEMA_VERSION = "1"

ERROR = "error"
WARN = "warn"
INFO = "info"


def _num(value, digits: int = 12):
    """JSON-safe float: numpy scalars become floats, non-finite becomes None.

    Non-finite coordinates cannot reach a mesh (rejected at load), but derived
    quantities can overflow for extreme extents, and `NaN` is not valid JSON -
    Python's encoder emits it anyway, which is worse.
    """
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return float(f"{number:.{digits}g}")


@dataclass(frozen=True)
class Issue:
    """One actionable finding, with a stable code a UI can switch on."""

    code: str
    severity: str
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "message": self.message}


def issues_for(validation: Validation) -> list[Issue]:
    """Derive findings from a measurement. Pure function of the numbers."""
    out: list[Issue] = []
    if validation.nonmanifold_edge_count:
        out.append(Issue(
            "nonmanifold_edges", ERROR,
            f"{validation.nonmanifold_edge_count} edge(s) are shared by three or more "
            "triangles. The mesh has no single inside, so no solid can be built "
            "from it without editing the geometry.",
        ))
    if validation.boundary_edge_count and not validation.watertight:
        # A boundary edge is either a real hole or an artefact of the format.
        # STL stores no shared vertices, so every exported STL looks open, and
        # a bare "the surface is open" sends the user hunting for a hole that
        # is not there. Say which of the two this most likely is, while still
        # reporting it as an error: on the raw arrays it genuinely is one.
        cause = ""
        if validation.duplicate_vertex_count:
            cause = (
                f" The mesh also has {validation.duplicate_vertex_count} coincident "
                "vertices, which is what an unwelded STL always looks like - the "
                "weld pass will usually clear this."
            )
        out.append(Issue(
            "not_watertight", ERROR,
            f"{validation.boundary_edge_count} boundary edge(s) belong to only one "
            "triangle, so the surface is open as stored. Slicers will either "
            f"repair it by guessing or refuse it.{cause}",
        ))
    if validation.winding_conflict_edge_count:
        out.append(Issue(
            "inconsistent_winding", ERROR,
            f"{validation.winding_conflict_edge_count} edge(s) are traversed the same "
            "way by both of their triangles, so the surface contradicts itself "
            "about which side is out.",
        ))
    if validation.degenerate_face_count:
        out.append(Issue(
            "degenerate_faces", WARN,
            f"{validation.degenerate_face_count} triangle(s) have a repeated corner "
            "or enclose no area.",
        ))
    if validation.duplicate_face_count:
        out.append(Issue(
            "duplicate_faces", WARN,
            f"{validation.duplicate_face_count} triangle(s) repeat a set of vertices "
            "already covered by an earlier triangle.",
        ))
    if validation.unreferenced_vertex_count:
        out.append(Issue(
            "unreferenced_vertices", WARN,
            f"{validation.unreferenced_vertex_count} vertex/vertices are not used by "
            "any triangle.",
        ))
    if validation.inward_face_count:
        out.append(Issue(
            "inverted_normals", WARN,
            f"{validation.inward_face_count} triangle(s) point inward"
            + ("" if validation.orientation_confidence == "high"
               else "; the mesh is not closed, so this is a heuristic"),
        ))
    if validation.face_count and volume_is_degenerate(validation):
        out.append(Issue(
            "zero_volume", ERROR,
            "the surface encloses no measurable volume, so there is nothing to "
            "manufacture even if it is closed.",
        ))
    if any(extent == 0.0 for extent in validation.bbox.extents):
        out.append(Issue(
            "flat_mesh", ERROR,
            "the bounding box has a zero dimension - the part is a sheet, not a solid.",
        ))
    if validation.duplicate_vertex_count:
        # Informational, not a defect: binary STL duplicates every corner, so a
        # clean export routinely arrives with thousands of "duplicates".
        out.append(Issue(
            "duplicate_vertices", INFO,
            f"{validation.duplicate_vertex_count} coincident vertex/vertices, which is "
            "expected for STL and harmless once welded.",
        ))
    return out


@dataclass(frozen=True)
class PartReport:
    """The whole result. `to_dict()` is what the HTTP layer returns."""

    schema_version: str = SCHEMA_VERSION
    source: dict = field(default_factory=dict)
    backends: dict = field(default_factory=dict)
    units: dict = field(default_factory=dict)
    before: dict | None = None
    analysis: dict = field(default_factory=dict)
    repair: dict | None = None
    issues: list[Issue] = field(default_factory=list)
    verdict: dict = field(default_factory=dict)
    slicer: dict | None = None

    @property
    def ok(self) -> bool:
        return self.verdict.get("status") == "ok"

    @property
    def usable(self) -> bool:
        return self.verdict.get("status") in ("ok", "warn")

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "backends": self.backends,
            "units": self.units,
            "before": self.before,
            "analysis": self.analysis,
            "repair": self.repair,
            "issues": [i.to_dict() for i in self.issues],
            "verdict": self.verdict,
            "slicer": self.slicer,
        }

    def to_json(self, **kwargs) -> str:
        """Strict JSON: `allow_nan=False`, so a NaN is a bug here, not output."""
        return json.dumps(self.to_dict(), allow_nan=False, **kwargs)


def _verdict(issues: list[Issue]) -> dict:
    errors = [i for i in issues if i.severity == ERROR]
    warns = [i for i in issues if i.severity == WARN]
    if errors:
        status = "unusable"
    elif warns:
        status = "warn"
    else:
        status = "ok"
    return {
        "status": status,
        "errors": len(errors),
        "warnings": len(warns),
        "printable": status != "unusable",
    }


def _units_block(mesh: Mesh, validation: Validation) -> dict:
    units = mesh.units
    return {
        **units.to_dict(),
        "surface_area_mm2": _num(units.to_mm(validation.surface_area)),
        "volume_mm3": _num(units.to_mm(validation.volume)),
        "bbox_mm": (
            [_num(x) for x in (units.to_mm(v) for v in validation.bbox.min)],
            [_num(x) for x in (units.to_mm(v) for v in validation.bbox.max)],
        ) if units.assumed == "mm" else None,
    }


def _geometry_block(validation: Validation) -> dict:
    block = validation.to_dict()["geometry"]
    return {
        "surface_area": _num(block["surface_area"]),
        "signed_volume": _num(block["signed_volume"]),
        "volume": _num(block["volume"]),
        "bbox": {
            "min": [_num(x) for x in block["bbox"]["min"]],
            "max": [_num(x) for x in block["bbox"]["max"]],
            "extents": [_num(x) for x in block["bbox"]["extents"]],
            "diagonal": _num(block["bbox"]["diagonal"]),
        },
        "face_area": {k: _num(v) for k, v in block["face_area"].items()},
    }


def _analysis_block(validation: Validation) -> dict:
    raw = validation.to_dict()
    return {
        "source_format": raw["source_format"],
        "counts": raw["counts"],
        "topology": raw["topology"],
        "normals": raw["normals"],
        "geometry": _geometry_block(validation),
    }


def analyse_part(
    source,
    *,
    filename: str | None = None,
    fmt: str | None = None,
    repair: bool = True,
    options: RepairOptions | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_faces: int = DEFAULT_MAX_FACES,
    max_vertices: int = DEFAULT_MAX_VERTICES,
    backends: backends_mod.Backends | None = None,
    scale: float = 1.0,
    slicer=None,
) -> PartReport:
    """Load, measure, optionally repair, re-measure, report.

    `slicer` is an optional callable taking the repaired mesh and returning a
    dict; see `slicer.check_meshes`. Kept as a parameter so the slicer stays
    optional *and* so a test can substitute a stub without patching a module.

    Raises `PartError` for anything rejected. There is no partial report: a
    caller that gets a report back has a mesh.
    """
    backends = backends or backends_mod.probe()
    loaded: LoadResult = load(
        source,
        filename=filename,
        fmt=fmt,
        max_bytes=max_bytes,
        max_faces=max_faces,
        max_vertices=max_vertices,
        backends=backends,
        scale=scale,
    )
    before_validation = validate_mesh(loaded.mesh)

    repair_dict = None
    final_validation = before_validation
    final_mesh = loaded.mesh
    if repair:
        result: RepairResult = repair_mesh(loaded.mesh, options, backends=backends)
        repair_dict = result.to_dict()
        final_mesh = result.mesh
        final_validation = validate_mesh(final_mesh)

    issues = issues_for(final_validation)
    slicer_block = None
    if slicer is not None:
        slicer_block = slicer(final_mesh)

    return PartReport(
        source=loaded.to_dict(),
        backends=backends.describe(),
        units=_units_block(final_mesh, final_validation),
        before=_analysis_block(before_validation),
        analysis=_analysis_block(final_validation),
        repair=repair_dict,
        issues=issues,
        verdict=_verdict(issues),
        slicer=slicer_block,
    )


def validate_part(source, **kwargs) -> PartReport:
    """Measure without touching anything."""
    kwargs["repair"] = False
    return analyse_part(source, **kwargs)


def error_report(exc: PartError) -> dict:
    """Uniform error body so the route has one shape to return."""
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "error": exc.to_dict(),
        "issues": [],
        "verdict": {"status": "error", "errors": 1, "warnings": 0, "printable": False},
    }


__all__ = [
    "SCHEMA_VERSION",
    "Issue",
    "PartReport",
    "analyse_part",
    "validate_part",
    "error_report",
    "issues_for",
]