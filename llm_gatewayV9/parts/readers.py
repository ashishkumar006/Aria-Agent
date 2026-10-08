"""Native readers for the two formats that need no dependency: STL and OBJ.

trimesh is the IO backend when it is installed and covers PLY, 3MF and
everything else. It is not installed in this venv, so STL and OBJ - the two
formats a part actually arrives in - are parsed here instead. A missing
optional dependency should narrow the feature, not switch it off.

Everything in this module treats its input as hostile: sizes come from the
file, so they are cross-checked against the byte length before any allocation
proportional to them, and a count that disagrees with the buffer is a
corruption error rather than an out-of-memory error.
"""
from __future__ import annotations

import re
import struct
import zipfile
from dataclasses import dataclass, field
from io import BytesIO

import numpy as np

from .errors import (
    MeshIntegrityError,
    MeshTooLargeError,
    UnsupportedFormatError,
)

# STL binary: 80-byte header, uint32 triangle count, then 50 bytes per triangle
# (12 float32 + 1 uint16). Fixed-size records are why this format is trivial to
# trust and trivial to corrupt.
_STL_HEADER = 80
_STL_COUNT = 4
_STL_RECORD = 50

_TRI_DTYPE = np.dtype(
    [("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attr", "<u2")]
)

SUPPORTED = ("stl", "obj")

# Formats we know about but cannot read without trimesh. Deliberately only the
# ones trimesh actually handles: STEP/IGES and Parasolid are *not* here,
# because trimesh does not read them either and naming them would send a user
# off to install a dependency that would not help.
_TRIMESH_ONLY = {"ply", "3mf", "off", "glb", "gltf", "dae", "amf"}


@dataclass
class ReadResult:
    """Raw arrays from a reader, before the trust gate in `Mesh.create`."""

    vertices: np.ndarray
    faces: np.ndarray
    fmt: str
    notes: list[str] = field(default_factory=list)


def detect_format(filename: str | None, data: bytes) -> str:
    """Identify the container from the filename, then the bytes.

    Filenames come from the client, so they are only a hint. Magic bytes win
    when they disagree - an upload named `part.stl` that is really a PLY is a
    thing that happens, and reading it as the wrong format yields garbage
    triangles rather than an error.
    """
    ext = ""
    if filename:
        stem = filename.replace("\\", "/").rsplit("/", 1)[-1]
        if "." in stem:
            ext = stem.rsplit(".", 1)[-1].strip().lower()

    if data[:2] == b"PK":
        return "3mf"
    head = data[:64].lstrip()
    if head[:3] == b"ply" or head[:4] == b"ply\n":
        return "ply"
    if head[:7].lower().startswith(b"solid ") or head[:6].lower() == b"solid\n":
        if _looks_like_ascii_stl(data):
            return "stl"
    if _looks_like_binary_stl(data):
        return "stl"
    if _looks_like_obj(data):
        return "obj"
    if ext:
        return ext
    raise UnsupportedFormatError(
        "could not identify the mesh format from its contents",
        filename=filename,
        first_bytes=repr(data[:16]),
    )


def read(data: bytes, fmt: str, *, max_faces: int, max_vertices: int) -> ReadResult:
    """Dispatch to a reader for a format already identified."""
    fmt = fmt.lower().lstrip(".")
    if fmt == "stl":
        return _read_stl(data, max_faces=max_faces, max_vertices=max_vertices)
    if fmt == "obj":
        return _read_obj(data, max_faces=max_faces, max_vertices=max_vertices)
    if fmt in _TRIMESH_ONLY:
        raise UnsupportedFormatError(
            f"reading {fmt.upper()} needs trimesh, which is not installed",
            format=fmt,
            requires="trimesh",
            natively_supported=list(SUPPORTED),
        )
    raise UnsupportedFormatError(
        f"unknown mesh format '{fmt}'",
        format=fmt,
        natively_supported=list(SUPPORTED),
        requires_trimesh_for=sorted(_TRIMESH_ONLY),
    )


# -- STL --------------------------------------------------------------------

def _looks_like_binary_stl(data: bytes) -> bool:
    if len(data) < _STL_HEADER + _STL_COUNT:
        return False
    count = struct.unpack_from("<I", data, _STL_HEADER)[0]
    return count > 0 and len(data) == _STL_HEADER + _STL_COUNT + count * _STL_RECORD


def _looks_like_ascii_stl(data: bytes) -> bool:
    """Distinguish ASCII STL from a binary one whose header says "solid".

    `outer loop` is the discriminator, not `facet`: four ASCII bytes turn up in
    32-bit float noise often enough to matter, whereas the keyword pair does
    not.
    """
    head = data[:8192].lower()
    return b"facet" in head and b"outer loop" in head


def _read_stl(data: bytes, *, max_faces: int, max_vertices: int) -> ReadResult:
    """Read binary or ASCII STL.

    Detection order matters: some exporters write a binary STL whose 80-byte
    header starts with the word "solid", which is legal and confuses the
    "starts with solid -> ASCII" heuristic. So ASCII is tried first and only
    accepted if it actually yields facets.
    """
    if data[:5].lower() == b"solid":
        # Only fall back to binary when the data carries no ASCII structure at
        # all. A file that does contain facets and fails to parse them is
        # corrupt, and reporting that as "truncated binary STL" would send the
        # user looking at the wrong problem.
        if _looks_like_ascii_stl(data):
            return _read_ascii_stl(
                data, max_faces=max_faces, max_vertices=max_vertices
            )

    if len(data) < _STL_HEADER + _STL_COUNT:
        raise MeshIntegrityError(
            f"STL is {len(data)} bytes, too short to hold a header and a triangle count",
            byte_length=len(data),
        )
    count = struct.unpack_from("<I", data, _STL_HEADER)[0]
    expected = _STL_HEADER + _STL_COUNT + count * _STL_RECORD
    notes: list[str] = []
    if expected > len(data):
        raise MeshIntegrityError(
            f"STL declares {count} triangles ({expected} bytes) but the file is "
            f"{len(data)} bytes - truncated",
            declared_faces=count,
            expected_bytes=expected,
            byte_length=len(data),
        )
    if expected < len(data):
        notes.append(
            f"{len(data) - expected} trailing byte(s) after the last triangle record were ignored"
        )
    if count == 0:
        raise MeshIntegrityError("STL declares zero triangles", declared_faces=0)
    if count > max_faces:
        raise MeshTooLargeError(
            f"{count} triangles exceeds the limit of {max_faces}",
            kind="faces", limit=max_faces, actual=count,
        )

    records = np.frombuffer(data, dtype=_TRI_DTYPE, count=count, offset=_STL_HEADER + _STL_COUNT)
    # Binary STL has no vertex sharing: every triangle carries three private
    # corners, so a unit cube arrives as 36 vertices. That is expected, not a
    # defect, and `validate` reports the duplicate count against which the
    # weld step is judged.
    vertices = records["vertices"].reshape(-1, 3).astype(np.float64)
    faces = np.arange(count * 3, dtype=np.int64).reshape(count, 3)
    attr = records["attr"]
    # Not "the first value is nonzero": only some records carrying a stray
    # attribute byte count is exactly the case worth reporting.
    nonstandard = np.unique(attr[attr != 0])
    if nonstandard.size:
        notes.append(
            f"STL attribute byte count is {int(nonstandard[0])} on "
            f"{int((attr != 0).sum())} of {count} triangles, not the specified 0"
        )
    if not np.isfinite(vertices).all():
        bad = int((~np.isfinite(vertices)).sum())
        raise MeshIntegrityError(
            f"{bad} non-finite coordinate value(s) in binary STL", bad_values=bad
        )
    return ReadResult(vertices=vertices, faces=faces, fmt="stl", notes=notes)


def _read_ascii_stl(data: bytes, *, max_faces: int, max_vertices: int) -> ReadResult:
    facets = 0
    rows: list[tuple[float, float, float]] = []
    tokenise = re.compile(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)")
    for line in data.splitlines():
        stripped = line.strip()
        if not stripped or stripped[:1] == b"#":
            continue
        head = stripped.split(None, 1)[0].lower()
        if head == b"facet":
            facets += 1
        elif head == b"vertex":
            m = tokenise.match(stripped)
            if m is None:
                raise MeshIntegrityError(
                    "malformed ASCII STL vertex line",
                    line=stripped[:80].decode("utf-8", "replace"),
                )
            try:
                rows.append(
                    (float(m.group(1)), float(m.group(2)), float(m.group(3)))
                )
            except ValueError as exc:
                raise MeshIntegrityError(
                    f"non-numeric ASCII STL vertex: {exc}",
                    line=stripped[:80].decode("utf-8", "replace"),
                ) from exc

    if facets == 0 or not rows:
        raise MeshIntegrityError(
            "ASCII STL contains no facets", facets=facets, vertices=len(rows)
        )
    if len(rows) % 3 != 0:
        raise MeshIntegrityError(
            f"ASCII STL has {len(rows)} vertex lines, not a multiple of 3",
            vertices=len(rows),
        )
    if len(rows) // 3 != facets:
        raise MeshIntegrityError(
            f"ASCII STL declares {facets} facets but carries {len(rows) // 3} triangles",
            declared_faces=facets,
            actual_faces=len(rows) // 3,
        )
    if facets > max_faces:
        raise MeshTooLargeError(
            f"{facets} triangles exceeds the limit of {max_faces}",
            kind="faces", limit=max_faces, actual=facets,
        )
    # An ASCII STL reuses coordinates textually, so unlike binary STL the
    # vertices are deduplicated here rather than left for the weld step. Two
    # spellings of the same number ("1" vs "1.0") still land as separate
    # rows, which the weld step cleans up.
    uniq, inverse = np.unique(
        np.asarray(rows, dtype=np.float64), axis=0, return_inverse=True
    )
    inverse = np.asarray(inverse).reshape(-1)
    if uniq.shape[0] > max_vertices:
        raise MeshTooLargeError(
            f"{uniq.shape[0]} vertices exceeds the limit of {max_vertices}",
            kind="vertices", limit=max_vertices, actual=int(uniq.shape[0]),
        )
    faces = inverse.reshape(-1, 3)
    return ReadResult(
        vertices=np.ascontiguousarray(uniq, dtype=np.float64),
        faces=np.ascontiguousarray(faces, dtype=np.int64),
        fmt="stl",
        notes=["read as ASCII STL"],
    )


# -- OBJ --------------------------------------------------------------------

_OBJ_INDEX = re.compile(r"^([+-]?\d+)(?:[/?].*)?$")


def _looks_like_obj(data: bytes) -> bool:
    return re.search(rb"(?m)^\s*(v|vn|vt|vp|f|o|g|usemtl|mtllib)\s", data[:4096]) is not None


def _read_obj(data: bytes, *, max_faces: int, max_vertices: int) -> ReadResult:
    """Read an OBJ's `v` and `f` records.

    Deliberately partial: normals, UVs, materials and multi-object groups are
    skipped, because validation only needs topology and position, and carrying
    UV seams would mean splitting vertices along them and changing the
    duplicate-vertex count the report is meant to state.
    """
    vertices: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []
    notes: list[str] = []
    ngons = 0
    lineno = 0

    for raw in data.splitlines():
        lineno += 1
        line = raw.strip()
        if not line or line[:1] == b"#":
            continue
        parts = line.split()
        tag = parts[0].lower()
        if tag == b"v":
            if len(parts) < 4:
                raise MeshIntegrityError(
                    f"OBJ vertex needs 3 coordinates, got {len(parts) - 1}",
                    line=lineno,
                )
            try:
                vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
            except ValueError as exc:
                raise MeshIntegrityError(
                    f"non-numeric OBJ vertex: {exc}", line=lineno
                ) from exc
            if len(vertices) > max_vertices:
                raise MeshTooLargeError(
                    f"more than {max_vertices} vertices", kind="vertices",
                    limit=max_vertices,
                )
        elif tag == b"f":
            if len(parts) < 4:
                raise MeshIntegrityError(
                    f"OBJ face needs at least 3 corners, got {len(parts) - 1}",
                    line=lineno,
                )
            idx: list[int] = []
            for token in parts[1:]:
                m = _OBJ_INDEX.match(token.decode("utf-8", "replace").strip())
                if m is None:
                    raise MeshIntegrityError(
                        f"malformed OBJ face index {token!r}", line=lineno
                    )
                raw_index = int(m.group(1))
                if raw_index == 0:
                    raise MeshIntegrityError(
                        "OBJ face index 0 is invalid - indices are 1-based", line=lineno
                    )
                # Negative indices count back from the vertices defined so far,
                # which is the only interpretation that works for a file that
                # declares a face before its own geometry.
                idx.append(raw_index - 1 if raw_index > 0 else len(vertices) + raw_index)
            if len(idx) > 3:
                ngons += 1
            for k in range(1, len(idx) - 1):  # fan triangulation
                triangles.append((idx[0], idx[k], idx[k + 1]))
            if len(triangles) > max_faces:
                raise MeshTooLargeError(
                    f"more than {max_faces} triangles", kind="faces", limit=max_faces
                )
        # every other record type is intentionally ignored

    if not vertices:
        raise MeshIntegrityError("OBJ contains no vertices", byte_length=len(data))
    if not triangles:
        raise MeshIntegrityError("OBJ contains no faces", vertices=len(vertices))
    if ngons:
        notes.append(f"{ngons} polygonal face(s) fan-triangulated; OBJ UV seams are not honoured")
    return ReadResult(
        vertices=np.asarray(vertices, dtype=np.float64),
        faces=np.asarray(triangles, dtype=np.int64),
        fmt="obj",
        notes=notes,
    )


# -- zip expansion guard ----------------------------------------------------

def zip_expansion(data: bytes, cap: int) -> int:
    """Total uncompressed size of a zip archive, refusing anything over `cap`.

    3MF is a zip. trimesh unzips it, so without this check a few kilobytes of
    upload can ask the server for gigabytes of memory. Only the central
    directory is parsed - no member is decompressed here.
    """
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            total = sum(max(0, int(info.file_size)) for info in zf.infolist())
    except zipfile.BadZipFile as exc:
        raise MeshIntegrityError(f"not a readable zip container: {exc}") from exc
    if total > cap:
        raise MeshTooLargeError(
            f"archive expands to {total} bytes, over the {cap} byte cap",
            kind="uncompressed_bytes", limit=cap, actual=total,
        )
    return total


__all__ = ["ReadResult", "SUPPORTED", "detect_format", "read", "zip_expansion"]