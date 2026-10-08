"""Turning bytes into a trusted `Mesh`.

Order of operations is the security-relevant part of this module:

1. size gate, before any allocation proportional to the file's claims;
2. zip expansion gate, because 3MF is a zip and a few KB can otherwise ask
   for gigabytes;
3. format identification from bytes, not from the client's filename;
4. read;
5. `Mesh.create`, the trust gate.

trimesh is used as the IO backend when present, always with `process=False`.
That flag matters: trimesh's default load merges duplicate vertices and drops
degenerate faces, so a `process=True` load makes the central question of this
module - what is wrong with the file as uploaded - unanswerable by
construction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

import numpy as np

from . import readers
from .backends import Backends, probe as probe_backends
from .errors import (
    EmptyMeshError,
    MeshIntegrityError,
    MeshTooLargeError,
    UnsupportedFormatError,
)
from .mesh import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_FACES,
    DEFAULT_MAX_VERTICES,
    MM_CONVENTION,
    Mesh,
    UnitAssumption,
)


@dataclass
class LoadResult:
    """A loaded mesh plus the provenance the report needs to be honest."""

    mesh: Mesh
    byte_length: int
    backend: str
    fmt: str
    source_name: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source_name": self.source_name,
            "byte_length": self.byte_length,
            "format": self.fmt,
            "io_backend": self.backend,
            "notes": list(self.notes),
            **self.mesh.to_dict(),
        }


def load(
    source,
    *,
    filename: str | None = None,
    fmt: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_faces: int = DEFAULT_MAX_FACES,
    max_vertices: int = DEFAULT_MAX_VERTICES,
    backends: Backends | None = None,
    scale: float = 1.0,
) -> LoadResult:
    """Load a mesh from a path or from bytes.

    `source` is bytes-like, or a str/Path to read from disk. `filename` is
    only a hint for format detection; pass it when loading from bytes.
    `scale` multiplies every coordinate, for files exported in inches. It
    changes the unit claim from a convention to a caller assertion.
    """
    data, name = _resolve(source, filename, max_bytes)

    if len(data) > max_bytes:
        raise MeshTooLargeError(
            f"file is {len(data)} bytes, over the {max_bytes} byte limit",
            kind="bytes", limit=max_bytes, actual=len(data),
        )
    if not data:
        raise EmptyMeshError("file is empty", byte_length=0)

    detected = (fmt or readers.detect_format(name, data)).lower().lstrip(".")
    if detected in ("3mf", "zip"):
        # Guard before handing the archive to anything that will unzip it.
        readers.zip_expansion(data, cap=max_bytes)

    backends = backends or probe_backends()
    notes: list[str] = []
    if backends.has("trimesh"):
        vertices, faces, backend, tm_notes = _load_with_trimesh(
            backends, data, detected, max_faces=max_faces, max_vertices=max_vertices
        )
        notes.extend(tm_notes)
    else:
        result = readers.read(data, detected, max_faces=max_faces, max_vertices=max_vertices)
        vertices, faces, backend = result.vertices, result.faces, f"native-{result.fmt}"
        notes.extend(result.notes)
        if detected in ("ply", "3mf"):
            notes.append(
                "trimesh is not installed, so only STL and OBJ can be read natively; "
                "PLY/3MF support needs the trimesh dependency"
            )

    units = MM_CONVENTION
    if scale != 1.0:
        vertices = vertices * float(scale)
        units = UnitAssumption(
            assumed="mm",
            confidence="caller",
            note=f"Caller scaled coordinates by {scale} on ingest.",
            scale_to_mm=1.0,
        )
        notes.append(f"coordinates scaled by {scale} on ingest")

    mesh = Mesh.create(
        vertices,
        faces,
        source_format=detected,
        units=units,
        max_vertices=max_vertices,
        max_faces=max_faces,
    )
    return LoadResult(
        mesh=mesh,
        byte_length=len(data),
        backend=backend,
        fmt=detected,
        source_name=name,
        notes=notes,
    )


def _resolve(source, filename: str | None, max_bytes: int) -> tuple[bytes, str | None]:
    """Normalise the input to (bytes, filename) with the size gate applied."""
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source), filename
    if isinstance(source, (str, Path)):
        path = Path(source)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise MeshIntegrityError(f"cannot stat mesh file: {exc}", path=str(path)) from exc
        # Gate on the stat before reading, so an oversized file is never
        # pulled into memory just to be rejected afterwards.
        if size > max_bytes:
            raise MeshTooLargeError(
                f"file is {size} bytes, over the {max_bytes} byte limit",
                kind="bytes", limit=max_bytes, actual=size, path=str(path),
            )
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise MeshIntegrityError(f"cannot read mesh file: {exc}", path=str(path)) from exc
        return data, filename or path.name
    raise UnsupportedFormatError(
        f"cannot load a mesh from {type(source).__name__}; pass bytes or a path",
        source_type=type(source).__name__,
    )


def _load_with_trimesh(
    backends: Backends,
    data: bytes,
    fmt: str,
    *,
    max_faces: int,
    max_vertices: int,
) -> tuple[np.ndarray, np.ndarray, str, list[str]]:
    """Read through trimesh without letting it repair the file on the way in.

    Unverified against a live trimesh: this venv has no trimesh installed, so
    the branch is exercised only by a stub in the test suite.
    """
    trimesh = backends.require("trimesh")
    notes: list[str] = []
    loaded = trimesh.load(BytesIO(data), file_type=fmt, process=False)
    geometry = getattr(loaded, "geometry", None)
    if geometry is not None and len(geometry) != 1:
        # Concatenating separate bodies produces a mesh whose edges are shared
        # by surfaces that do not meet, so every topology number below becomes
        # a lie. v1 refuses instead.
        raise UnsupportedFormatError(
            f"file contains {len(geometry)} separate bodies; v1 validates one body at a time",
            bodies=len(geometry),
        )
    vertices = np.asarray(loaded.vertices, dtype=np.float64)
    faces = np.asarray(loaded.faces, dtype=np.int64)
    if vertices.size == 0 or faces.size == 0:
        raise EmptyMeshError(
            "trimesh returned an empty mesh",
            vertex_count=int(vertices.size), face_count=int(faces.size),
        )
    if faces.shape[0] > max_faces or vertices.shape[0] > max_vertices:
        raise MeshTooLargeError(
            "mesh exceeds the configured face or vertex limit",
            kind="faces" if faces.shape[0] > max_faces else "vertices",
            limit=max_faces if faces.shape[0] > max_faces else max_vertices,
            actual=int(max(faces.shape[0], vertices.shape[0])),
        )
    notes.append("read with trimesh (process=False: no repair at load time)")
    return vertices, faces, "trimesh", notes



__all__ = ["LoadResult", "load"]