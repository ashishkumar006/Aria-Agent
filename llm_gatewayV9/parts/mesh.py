"""The mesh value object and the one place raw numbers become trusted.

Uploaded coordinates are untrusted input. Binary STL is a flat 50-bytes-per
triangle structure with no type tags, so a malformed or hostile file produces
plausible-looking floats: NaNs, 1e308 magnitudes, indices past the end of the
vertex array. All of that is rejected once, here, in `Mesh.create`, so that
every later module can assume finite float64 vertices and in-range int64 face
indices. Doing the check at the reader instead would mean N places to forget
it.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from .errors import EmptyMeshError, MeshIntegrityError, MeshTooLargeError

# Default resource budget. 2M triangles is ~24 MB of index data and analyses in
# a few seconds; beyond that the request is a denial-of-service vector against
# a single-process server, not a part.
DEFAULT_MAX_VERTICES = 2_000_000
DEFAULT_MAX_FACES = 2_000_000
DEFAULT_MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class UnitAssumption:
    """What the numbers are measured in, and how sure we are.

    STL carries no unit tag and neither do OBJ or most PLY files; 3MF declares
    one but only inside its archive. Nothing here reads a unit from a file, so
    this is an assumption derived from format convention and it is labelled
    `convention` rather than `declared`. Downstream consumers must not present
    a derived millimetre figure as a measurement.
    """

    assumed: str
    confidence: str
    note: str
    scale_to_mm: float = 1.0

    def to_mm(self, value: float | None) -> float | None:
        """Convert a length/area/volume in file units to the mm basis.

        Returns None when the assumption is not mm, so a caller cannot
        accidentally ship a number that was scaled by an unverified factor.
        """
        if value is None or self.assumed != "mm":
            return None
        return value * self.scale_to_mm

    def to_dict(self) -> dict:
        return {
            "assumed": self.assumed,
            "confidence": self.confidence,
            "note": self.note,
            "scale_to_mm": self.scale_to_mm,
        }


# STL/OBJ/PLY all carry no unit field that the readers below parse.
MM_CONVENTION = UnitAssumption(
    assumed="mm",
    confidence="convention",
    note=("File declares no unit. Millimetres assumed, which is the "
          "convention for STL and near-universal for 3D-print parts. Treat "
          "all derived lengths as +/- the exporter's intent."),
    scale_to_mm=1.0,
)


@dataclass(frozen=True)
class Mesh:
    """Vertices and triangles, plus provenance for the report."""

    vertices: np.ndarray
    faces: np.ndarray
    source_format: str = "unknown"
    units: UnitAssumption = MM_CONVENTION

    def __post_init__(self) -> None:
        if not isinstance(self.vertices, np.ndarray):
            object.__setattr__(self, "vertices", np.asarray(self.vertices))
        if not isinstance(self.faces, np.ndarray):
            object.__setattr__(self, "faces", np.asarray(self.faces))

    @property
    def vertex_count(self) -> int:
        return int(self.vertices.shape[0])

    @property
    def face_count(self) -> int:
        return int(self.faces.shape[0])

    def with_arrays(self, vertices: np.ndarray, faces: np.ndarray) -> "Mesh":
        """A sibling mesh after a repair, keeping provenance."""
        return replace(self, vertices=vertices, faces=faces)

    def to_dict(self) -> dict:
        """Counts only. The arrays themselves are never serialised."""
        return {
            "source_format": self.source_format,
            "vertex_count": self.vertex_count,
            "face_count": self.face_count,
            "units": self.units.to_dict(),
        }

    # -- construction ------------------------------------------------------
    @staticmethod
    def create(
        vertices,
        faces,
        *,
        source_format: str = "unknown",
        units: UnitAssumption = MM_CONVENTION,
        max_vertices: int = DEFAULT_MAX_VERTICES,
        max_faces: int = DEFAULT_MAX_FACES,
    ) -> "Mesh":
        """Coerce and validate raw arrays into a trusted `Mesh`.

        This is a hard gate: anything that does not describe a non-empty
        triangle soup with finite coordinates and in-range indices raises.
        """
        v = np.asarray(vertices)
        f = np.asarray(faces)

        if v.ndim != 2 or v.shape[1] != 3:
            raise MeshIntegrityError(
                f"vertices must be an (N, 3) array, got shape {v.shape}",
                shape=list(v.shape),
            )
        if f.ndim != 2 or f.shape[1] != 3:
            raise MeshIntegrityError(
                f"faces must be an (M, 3) array, got shape {f.shape}",
                shape=list(f.shape),
            )
        if v.shape[0] == 0 or f.shape[0] == 0:
            raise EmptyMeshError(
                "mesh has no surface",
                vertex_count=int(v.shape[0]),
                face_count=int(f.shape[0]),
            )
        if v.shape[0] > max_vertices:
            raise MeshTooLargeError(
                f"{int(v.shape[0])} vertices exceeds the limit of {max_vertices}",
                kind="vertices", limit=max_vertices, actual=int(v.shape[0]),
            )
        if f.shape[0] > max_faces:
            raise MeshTooLargeError(
                f"{int(f.shape[0])} faces exceeds the limit of {max_faces}",
                kind="faces", limit=max_faces, actual=int(f.shape[0]),
            )

        # float64 for geometry, int64 for indices. Integer index arrays that
        # arrive as float (OBJ text, some exporters) are accepted only when
        # they are exactly integral; a 2.5 index means the file is corrupt.
        if v.dtype != np.float64:
            if not (np.issubdtype(v.dtype, np.floating) or np.issubdtype(v.dtype, np.integer)):
                raise MeshIntegrityError(f"vertices must be numeric, got dtype {v.dtype}")
            v = v.astype(np.float64)
        if f.dtype != np.int64:
            if np.issubdtype(f.dtype, np.floating):
                if not np.isfinite(f).all() or not np.equal(f, np.floor(f)).all():
                    raise MeshIntegrityError(
                        "face indices must be whole numbers",
                        dtype=str(f.dtype),
                    )
                f = f.astype(np.int64)
            elif np.issubdtype(f.dtype, np.integer):
                f = f.astype(np.int64)
            else:
                raise MeshIntegrityError(
                    f"face indices must be numeric, got dtype {f.dtype}"
                )

        if not np.isfinite(v).all():
            bad = int((~np.isfinite(v)).sum())
            raise MeshIntegrityError(
                f"{bad} non-finite coordinate value(s) (NaN or infinity)",
                bad_values=bad,
            )
        n = int(v.shape[0])
        if f.min() < 0 or f.max() >= n:
            raise MeshIntegrityError(
                f"face index out of range: [{int(f.min())}, {int(f.max())}] "
                f"against {n} vertices",
                vertex_count=n,
                min_index=int(f.min()),
                max_index=int(f.max()),
            )

        return Mesh(
            vertices=np.ascontiguousarray(v, dtype=np.float64),
            faces=np.ascontiguousarray(f, dtype=np.int64),
            source_format=source_format,
            units=units,
        )


__all__ = [
    "Mesh",
    "UnitAssumption",
    "MM_CONVENTION",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_FACES",
    "DEFAULT_MAX_VERTICES",
]