"""Binary STL export - the only native writer.

Needed because the slicability check has to run against the *repaired* mesh,
not the upload: the interesting question is whether the fixed part prints, and
running PrusaSlicer on the original only tells us what we already knew. STL is
the one format that can be both read and written without a dependency.

The output is binary STL, which has no index sharing and no attributes, so a
round trip inflates the vertex count. That is a property of the format, not a
defect in the mesh, and `ingest` says so on read.
"""
from __future__ import annotations

import struct

import numpy as np

from .mesh import Mesh

_HEADER = b"aria-parts 1.0 binary stl"


def to_binary_stl(mesh: Mesh) -> bytes:
    """Serialise `mesh` as binary STL.

    Face normals are recomputed rather than taken from `mesh`, because there
    are none stored: the vertex normals a file may have carried were dropped at
    load, and writing stale zeros into a format where the reader trusts them is
    how slicers end up with inverted shading.
    """
    v = mesh.vertices
    f = mesh.faces
    tri = v[f]                                   # (M, 3, 3)
    normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])

    header = _HEADER.ljust(80, b"\0")[:80]
    count = struct.pack("<I", int(f.shape[0]))
    # One 50-byte record per triangle: 12 floats (normal xyz, then the three
    # corners flattened) followed by the uint16 attribute count. The record is
    # a flat 12-float view, not a (3, 4): itemsize is 12*4 + 2 = 50, which
    # `test_binary_stl_export_has_exactly_fifty_bytes_per_triangle` asserts,
    # because a padded struct would silently produce unreadable files.
    flat = np.concatenate([normals, tri.reshape(f.shape[0], 9)], axis=1)
    record = np.dtype([("f", "<f4", (12,)), ("a", "<u2")])
    body = np.zeros(f.shape[0], dtype=record)
    body["f"] = flat.astype("<f4")
    body["a"] = 0
    return header + count + body.tobytes()


__all__ = ["to_binary_stl"]