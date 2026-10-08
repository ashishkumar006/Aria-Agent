"""Pure-numpy geometry: the single source of truth for every number reported.

Deliberately *not* delegating the analysis to trimesh. trimesh is optional
here, and a measurement that changes depending on whether an optional import
succeeded is a measurement nobody can test. trimesh is used as an IO backend
and manifold3d as the authoritative topology oracle (see `repair`); the counts,
areas, volumes and topology flags below are computed the same way whether or
not either is installed.

Conventions
-----------
`signed_volume` is positive for a closed mesh whose triangles wind
counter-clockwise when viewed from outside. Volume is the divergence-theorem
sum, not a signed-tetrahedron decomposition, so an open mesh reports the volume
of whatever it currently encloses (usually meaninglessly small) rather than
erroring. Callers that care check `watertight` first.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import MeshIntegrityError
from .mesh import Mesh

# Bounds the quantiser in `merge_vertices` against int64 overflow. A bin index
# of 2**62 is already ~4.6e18 cells apart, far beyond any real coordinate.
_MAX_BIN = 2 ** 62


@dataclass(frozen=True)
class Geometry:
    """Derived per-face and per-edge arrays for one mesh.

    Everything is computed once. The analysis and both repair passes all need
    the cross products and the edge table, and recomputing them per consumer
    turned a 2M-triangle validation into several seconds of redundant numpy.
    """

    vertices: np.ndarray
    faces: np.ndarray
    # per-face
    corner_a: np.ndarray
    corner_b: np.ndarray
    corner_c: np.ndarray
    cross: np.ndarray        # (M,3) unnormalised face normal * 2 * area
    areas: np.ndarray        # (M,)
    centroids: np.ndarray    # (M,3)
    reference: np.ndarray    # (3,) orientation reference point, see below
    # per-edge
    edge_incidence: np.ndarray  # (K,) faces incident to each undirected edge
    edge_dir_sum: np.ndarray  # (K,) +1/-1 traversal-direction balance
    edge_group_start: np.ndarray  # (K,) offset of each edge's rows in edge_owner_by_edge
    edge_faces: np.ndarray   # (3M,) face id owning each directed edge, sorted by edge key
    # bookkeeping
    referenced_vertices: np.ndarray  # sorted unique vertex ids used by a face
    duplicate_vertex_count: int

    # -- derived scalars ---------------------------------------------------
    @property
    def vertex_count(self) -> int:
        return int(self.vertices.shape[0])

    @property
    def face_count(self) -> int:
        return int(self.faces.shape[0])

    @property
    def unique_edge_count(self) -> int:
        return int(self.edge_incidence.shape[0])

    @property
    def unreferenced_vertex_count(self) -> int:
        return self.vertex_count - int(self.referenced_vertices.shape[0])

    @property
    def surface_area(self) -> float:
        return float(self.areas.sum())

    @property
    def signed_volume(self) -> float:
        """Enclosed volume; positive when wound outward."""
        return float(np.einsum("ij,ij->i", self.corner_a, np.cross(self.corner_b, self.corner_c)).sum() / 6.0)

    @property
    def boundary_edge_count(self) -> int:
        """Edges with exactly one incident face - the holes."""
        return int((self.edge_incidence == 1).sum())

    @property
    def nonmanifold_edge_count(self) -> int:
        """Edges with three or more incident faces - unsolidifiable."""
        return int((self.edge_incidence > 2).sum())

    @property
    def winding_conflict_edge_count(self) -> int:
        """Edges whose two faces traverse it the same way.

        Watertight and still inside-out: a closed mesh can have every edge
        shared twice and still have no coherent outward direction. This is the
        local half of the normals check; `inward_face_count` is the global half.
        """
        return int(((self.edge_incidence == 2) & (np.abs(self.edge_dir_sum) == 2)).sum())

    @property
    def watertight(self) -> bool:
        """Every undirected edge shared by exactly two faces.

        This is the boundary/topology definition and needs no volume, so it is
        meaningful on an open sheet too (where it is simply False).
        """
        return bool(self.edge_incidence.size and (self.edge_incidence == 2).all())

    @property
    def winding_consistent(self) -> bool:
        return self.winding_conflict_edge_count == 0

    @property
    def euler_characteristic(self) -> int:
        """V - E + F over the *referenced* vertex set.

        Meaningful only on a closed mesh, where it equals 2-2g. On an open
        sheet it is reported for information and should not be interpreted.
        """
        v = int(self.referenced_vertices.shape[0])
        return v - self.unique_edge_count + self.face_count

    @property
    def genus(self) -> int | None:
        """Number of handles, or None when the mesh is not closed."""
        if not self.watertight:
            return None
        return int((2 - self.euler_characteristic) // 2)

    @property
    def bbox_min(self) -> np.ndarray:
        return self.vertices.min(axis=0)

    @property
    def bbox_max(self) -> np.ndarray:
        return self.vertices.max(axis=0)

    @property
    def bbox_extents(self) -> np.ndarray:
        return self.bbox_max - self.bbox_min

    @property
    def bbox_diagonal(self) -> float:
        return float(np.linalg.norm(self.bbox_extents))

    @property
    def degenerate_face_mask(self) -> np.ndarray:
        """Faces that enclose no area: a repeated corner, or a zero cross product."""
        f = self.faces
        repeated = (f[:, 0] == f[:, 1]) | (f[:, 1] == f[:, 2]) | (f[:, 2] == f[:, 0])
        return repeated | (self.areas == 0.0)




def compute(mesh: Mesh) -> Geometry:
    """Build the derived arrays for `mesh`."""
    v = mesh.vertices
    f = mesh.faces
    n_v = int(v.shape[0])
    n_f = int(f.shape[0])

    a = v[f[:, 0]]
    b = v[f[:, 1]]
    c = v[f[:, 2]]
    cross = np.cross(b - a, c - a)
    areas = 0.5 * np.linalg.norm(cross, axis=1)
    centroids = (a + b + c) / 3.0

    # -- edges ------------------------------------------------------------
    # Three directed edges per face: (0,1) (1,2) (2,0).
    #
    # The concatenation below stacks the three edge *columns*, so row i belongs
    # to face i % n_f, not i // 3 - hence `tile`, not `repeat`. Getting this
    # wrong is invisible in every aggregate this module reports (edge incidence
    # and traversal balance are computed from `key` alone) and shows up only in
    # `edge_faces`, which is to say only in the winding repair. `tile` is the
    # whole fix.
    directed = np.concatenate((f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]), axis=0)
    owner = np.tile(np.arange(n_f, dtype=np.int64), 3)
    lo = np.minimum(directed[:, 0], directed[:, 1])
    hi = np.maximum(directed[:, 0], directed[:, 1])
    # Single int64 key per undirected edge. lo*n_v+hi is collision-free because
    # 0 <= lo,hi < n_v, and stays under 2**63 while n_v < 3e9.
    key = lo.astype(np.int64) * np.int64(n_v) + hi.astype(np.int64)
    uniq_key, inverse, counts = np.unique(key, return_inverse=True, return_counts=True)
    inverse = np.asarray(inverse).reshape(-1)
    # +1 when the face walks lo->hi, -1 when hi->lo. Two faces on a closed
    # mesh must cancel; a sum of +/-2 is two faces walking the same way.
    direction = np.where(directed[:, 0] == lo, 1.0, -1.0)
    dir_sum = np.bincount(inverse, weights=direction, minlength=uniq_key.shape[0])

    order = np.argsort(inverse, kind="stable")
    faces_by_edge = owner[order]
    group_start = np.zeros(uniq_key.shape[0] + 1, dtype=np.int64)
    np.cumsum(counts, out=group_start[1:])

    total_area = float(areas.sum())
    if total_area > 0.0:
        # Area-weighted face centroid. The plain vertex mean is biased towards
        # densely tessellated regions, and binary STL duplicates every corner
        # per triangle, so a vertex mean tracks the tessellation rather than
        # the shape.
        reference = (centroids * areas[:, None]).sum(axis=0) / total_area
    else:
        reference = v.mean(axis=0)

    referenced = np.unique(f)
    duplicate_vertices = n_v - int(np.unique(v, axis=0).shape[0])

    return Geometry(
        vertices=v,
        faces=f,
        corner_a=a,
        corner_b=b,
        corner_c=c,
        cross=cross,
        areas=areas,
        centroids=centroids,
        reference=reference,
        edge_incidence=counts,
        edge_dir_sum=dir_sum,
        edge_group_start=group_start,
        edge_faces=faces_by_edge,
        referenced_vertices=referenced,
        duplicate_vertex_count=duplicate_vertices,
    )


# -- vertex welding ---------------------------------------------------------

def merge_vertices(
    vertices: np.ndarray, faces: np.ndarray, tolerance: float = 0.0
) -> tuple[np.ndarray, np.ndarray, int]:
    """Weld coincident vertices and rewrite the face indices.

    `tolerance == 0.0` welds only bit-identical coordinates, which is what
    binary STL needs: it stores every triangle's three corners separately, so
    a unit cube arrives as 36 vertices and only ever becomes 8.

    A positive tolerance bins coordinates onto a `tolerance` grid and takes the
    first original vertex in each bin. Grid binning is approximate in both
    directions and the error is worth stating: two points closer than
    `tolerance` can straddle a bin boundary and stay separate, and two points
    up to `sqrt(3) * tolerance` apart can share a bin and be wrongly welded.
    It never moves a vertex, so it cannot invent geometry - only lose it.

    Returns (vertices, faces, removed_count).
    """
    if tolerance < 0.0:
        raise MeshIntegrityError(f"merge tolerance must be >= 0, got {tolerance}")

    if tolerance == 0.0:
        unique, inverse = np.unique(vertices, axis=0, return_inverse=True)
        inverse = np.asarray(inverse).reshape(-1)
        removed = int(vertices.shape[0] - unique.shape[0])
        return (
            np.ascontiguousarray(unique, dtype=np.float64),
            np.ascontiguousarray(inverse[faces], dtype=np.int64),
            removed,
        )

    quantised = np.floor(vertices / tolerance)
    if not np.all(np.abs(quantised) < _MAX_BIN):
        raise MeshIntegrityError(
            "merge tolerance is too small for these coordinates to quantise",
            tolerance=tolerance,
            bbox_diagonal=float(np.linalg.norm(vertices.max(axis=0) - vertices.min(axis=0))),
        )
    unique, inverse = np.unique(quantised.astype(np.int64), axis=0, return_inverse=True)
    inverse = np.asarray(inverse).reshape(-1)
    n_bins = int(unique.shape[0])

    # Three index spaces, and conflating any two of them silently produces
    # out-of-range faces:
    #   bin        -> representative original vertex  (rep_of_bin)
    #   vertex     -> representative original vertex  (rep_of_vertex)
    #   original   -> dense output row                (dense_of_vertex)
    # First original vertex wins, so the surviving position is always one that
    # was actually in the file.
    rep_of_bin = np.full(n_bins, -1, dtype=np.int64)
    # Reversed so the lowest index is assigned last and therefore survives.
    rep_of_bin[inverse[::-1]] = np.arange(
        vertices.shape[0] - 1, -1, -1, dtype=np.int64
    )
    rep_of_vertex = rep_of_bin[inverse]
    dense_of_vertex = np.empty(vertices.shape[0], dtype=np.int64)
    dense_of_vertex[rep_of_bin] = np.arange(n_bins, dtype=np.int64)

    return (
        np.ascontiguousarray(vertices[rep_of_bin], dtype=np.float64),
        np.ascontiguousarray(dense_of_vertex[faces], dtype=np.int64),
        int(vertices.shape[0] - n_bins),
    )


# -- face classification ----------------------------------------------------

def duplicate_face_mask(faces: np.ndarray) -> np.ndarray:
    """Mark every face that repeats the vertex set of an earlier face.

    A face is identified by its sorted index triple, so (0,1,2) and (2,0,1)
    count as the same face - they are the same triangle with opposite winding,
    which is an error in its own right and certainly not two surfaces.

    Only catches *geometric* duplicates once vertices are welded: two faces
    covering the same patch of space with different vertex indices look
    identical and are not detected here. That is why `repair` welds first.
    """
    canonical = np.sort(faces, axis=1)
    # numpy returns (unique, indices, inverse, counts) - in the order the
    # keywords are declared, not the order they are asked for here.
    _, first_index, inverse, counts = np.unique(
        canonical, axis=0, return_index=True, return_counts=True, return_inverse=True
    )
    inverse = np.asarray(inverse).reshape(-1)
    repeated = counts[inverse] > 1
    keep_first = np.arange(faces.shape[0]) == first_index[inverse]
    return repeated & ~keep_first


def conflicting_face_pairs(geo: Geometry) -> tuple[np.ndarray, np.ndarray]:
    """Face ids of every same-direction shared edge, as parallel arrays."""
    two = geo.edge_incidence == 2
    conflict = two & (np.abs(geo.edge_dir_sum) == 2)
    idx = np.flatnonzero(conflict)
    starts = geo.edge_group_start[idx]
    first = geo.edge_faces[starts]
    second = geo.edge_faces[starts + 1]
    return first, second


def inward_face_mask(geo: Geometry) -> np.ndarray:
    """Faces whose normal points back toward the mesh interior.

    Heuristic, and only exact for a solid that is star-shaped about the
    reference point: an L-bracket's inner faces face the interior too, and
    this flags them. `validate` reports `orientation_confidence` so a caller
    knows when to trust the answer, and `fix_normals` combines this with the
    exact local check (`winding_conflict_edge_count`) rather than relying on
    it alone.
    """
    outward = np.einsum("ij,ij->i", geo.cross, geo.centroids - geo.reference)
    return outward < 0.0


# -- topology-preserving edits ---------------------------------------------

def drop_faces(mesh: Mesh, mask: np.ndarray) -> Mesh:
    """Remove the faces where `mask` is True."""
    return mesh.with_arrays(mesh.vertices, mesh.faces[~mask])


def drop_unreferenced_vertices(mesh: Mesh) -> tuple[Mesh, int]:
    """Compact the vertex array down to vertices a face still uses."""
    used = np.unique(mesh.faces)
    removed = mesh.vertex_count - int(used.shape[0])
    if removed == 0:
        return mesh, 0
    remap = np.full(mesh.vertex_count, -1, dtype=np.int64)
    remap[used] = np.arange(used.shape[0], dtype=np.int64)
    return mesh.with_arrays(mesh.vertices[used], remap[mesh.faces]), removed


def flip_faces(mesh: Mesh, mask: np.ndarray) -> Mesh:
    """Reverse the winding of the faces where `mask` is True."""
    faces = mesh.faces.copy()
    faces[mask] = faces[mask][:, [0, 2, 1]]
    return mesh.with_arrays(mesh.vertices, faces)


__all__ = [
    "Geometry",
    "compute",
    "merge_vertices",
    "duplicate_face_mask",
    "conflicting_face_pairs",
    "inward_face_mask",
    "drop_faces",
    "drop_unreferenced_vertices",
    "flip_faces",
]