"""3D part validation, repair and analysis. v1.

Public API
----------
    analyse_part(source)          load, measure, repair, re-measure, report
    validate_part(source)         the same without the repair passes
    load(source)                  bytes/path -> trusted Mesh
    validate_mesh(mesh)           Mesh -> Validation
    repair_mesh(mesh, options)    Mesh -> RepairResult
    to_binary_stl(mesh)           the only native writer
    capabilities()                which optional backends are installed
    errors                        the typed failures the API maps to statuses

Out of scope for v1, deliberately: topology optimisation, mesh decimation,
thin-wall or overhang analysis, multi-body handling, and hole filling. The last
one is a design decision rather than an omission - see `repair` for why filling
a boundary would be worse than reporting it.

Optional dependencies: `trimesh` (wider format IO) and `manifold3d`
(topology oracle). Neither is required to import this package; both are probed
through `parts.backends` and their absence is reported, never raised at import.
"""
from __future__ import annotations

from . import backends, errors
from .backends import Backends, probe as capabilities
from .errors import (
    EmptyMeshError,
    MeshIntegrityError,
    MeshTooLargeError,
    MissingBackendError,
    PartError,
    RepairFailedError,
    UnsupportedFormatError,
)
from .export import to_binary_stl
from .geometry import compute as compute_geometry
from .ingest import LoadResult, load
from .mesh import MM_CONVENTION, Mesh, UnitAssumption
from .readers import SUPPORTED as NATIVELY_READABLE
from .repair import (
    APPLIED,
    FAILED,
    SKIPPED,
    UNAVAILABLE,
    RepairOptions,
    RepairResult,
    RepairStep,
    repair_mesh,
)
from .report import (
    SCHEMA_VERSION,
    Issue,
    PartReport,
    analyse_part,
    error_report,
    issues_for,
    validate_part,
)
from .slicer import SlicerResult, check_mesh, find_prusa_slicer
from .validate import BoundingBox, Validation, validate_mesh, volume_is_degenerate

__all__ = [
    # entry points
    "analyse_part",
    "validate_part",
    "load",
    "LoadResult",
    "validate_mesh",
    "Validation",
    "repair_mesh",
    "RepairResult",
    "RepairOptions",
    "RepairStep",
    "to_binary_stl",
    "check_mesh",
    "SlicerResult",
    "find_prusa_slicer",
    # containers
    "Mesh",
    "UnitAssumption",
    "MM_CONVENTION",
    "BoundingBox",
    "PartReport",
    "Issue",
    "Backends",
    # capabilities
    "capabilities",
    "backends",
    "NATIVELY_READABLE",
    "SCHEMA_VERSION",
    "compute_geometry",
    "volume_is_degenerate",
    "issues_for",
    "error_report",
    # statuses
    "APPLIED",
    "SKIPPED",
    "FAILED",
    "UNAVAILABLE",
    # errors
    "errors",
    "PartError",
    "UnsupportedFormatError",
    "MeshTooLargeError",
    "MeshIntegrityError",
    "EmptyMeshError",
    "RepairFailedError",
    "MissingBackendError",
]