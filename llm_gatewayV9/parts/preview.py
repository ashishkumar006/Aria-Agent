"""Fast viewing LOD for very large meshes.

Quadric decimation is the right tool for an optimisation pass, but it is
expensive on a million-triangle scan and a viewer only needs a recognisable
silhouette. Vertex clustering is O(vertices + faces), produces a watertight-ish
preview, and is good enough for orbit/overlay work. The source mesh is never
modified; this only builds the buffers sent to the browser.
"""
from __future__ import annotations

import numpy as np

from .mesh import Mesh

# The first grid resolution tried is derived from the reduction ratio; these
# bounds keep a pathological target from making the loop huge.
_MIN_GRID = 8
_MAX_GRID = 512


def _cluster(mesh: Mesh, cells: int) -> Mesh:
    v = mesh.vertices
    f = mesh.faces
    lo = v.min(axis=0)
    hi = v.max(axis=0)
    span = float(np.max(hi - lo))
    if not np.isfinite(span) or span <= 0:
        return mesh
    cell = span / cells
    keys = np.floor((v - lo) / cell).astype(np.int64)
    # `return_index` gives one representative per cell; using the first
    # original vertex avoids moving the silhouette while clustering.
    _, first, inverse = np.unique(keys, axis=0, return_index=True,
                                  return_inverse=True)
    nf = inverse.reshape(-1)[f]
    # A cell collapse can turn a triangle into an edge or a point.
    good = ((nf[:, 0] != nf[:, 1])
            & (nf[:, 1] != nf[:, 2])
            & (nf[:, 0] != nf[:, 2]))
    nf = nf[good]
    if not len(nf):
        return mesh
    # Collapsing can also make two triangles share the same three cells.
    _, keep = np.unique(np.sort(nf, axis=1), axis=0, return_index=True)
    nf = nf[np.sort(keep)]
    if not len(nf):
        return mesh
    return Mesh.create(v[first], nf, source_format=mesh.source_format)


def fast_preview(mesh: Mesh, target_faces: int) -> tuple[Mesh, bool]:
    """Return a cheap viewable mesh at or near `target_faces`.

    One estimated cluster is usually enough; only a mesh whose surface is much
    denser than the estimate gets a second, coarser pass. A binary search over
    grid sizes cost nine full clustering passes on a 1.1M-face scan, which is
    exactly the wait this function exists to remove.
    """
    target = max(1, int(target_faces))
    if mesh.face_count <= target:
        return mesh, False
    ratio = max(target / max(1, mesh.face_count), 1e-6)
    cells = max(_MIN_GRID, min(_MAX_GRID, int(round(512 * np.sqrt(ratio)))))
    best = mesh
    candidate = _cluster(mesh, cells)
    if candidate.face_count < mesh.face_count:
        best = candidate
    if best.face_count <= target * 1.12:
        return best, True
    # The first grid was too fine; coarsen proportionally and try a few times.
    for _ in range(4):
        overshoot = max(1.05, (best.face_count / target) ** 0.5)
        cells = max(_MIN_GRID, int(cells / overshoot))
        candidate = _cluster(mesh, cells)
        if candidate.face_count < mesh.face_count:
            best = candidate
        if best.face_count <= target * 1.12:
            return best, True
        if cells <= _MIN_GRID:
            break
    if best.face_count < mesh.face_count:
        return best, True
    return mesh, False
