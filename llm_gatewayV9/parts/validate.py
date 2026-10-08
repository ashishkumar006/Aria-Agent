"""Read-only measurement of a mesh.

Nothing here mutates. `validate` and `repair` share this so the "after"
numbers in a report come from the same code as the "before" numbers - two
implementations of "is it watertight" would eventually disagree, and the report
would be comparing apples to oranges.

Every field is a measurement or a derived flag. Where a value is a heuristic
(`orientation_confidence`, `units.confidence`) the flag says so.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .geometry import Geometry, compute, duplicate_face_mask, inward_face_mask
from .mesh import Mesh


@dataclass(frozen=True)
class BoundingBox:
    min: list[float]
    max: list[float]
    extents: list[float]
    diagonal: float

    def to_dict(self) -> dict:
        return {
            "min": self.min,
            "max": self.max,
            "extents": self.extents,
            "diagonal": self.diagonal,
        }


@dataclass(frozen=True)
class Validation:
    """Everything `validate_mesh` measures, in one serialisable object."""

    source_format: str
    vertex_count: int
    face_count: int
    referenced_vertex_count: int
    unreferenced_vertex_count: int
    duplicate_vertex_count: int
    degenerate_face_count: int
    duplicate_face_count: int
    # topology
    watertight: bool
    manifold: bool
    boundary_edge_count: int
    nonmanifold_edge_count: int
    winding_conflict_edge_count: int
    winding_consistent: bool
    unique_edge_count: int
    euler_characteristic: int
    genus: int | None
    # normals
    inward_face_count: int
    normals_outward: bool
    orientation_confidence: str
    # geometry
    surface_area: float
    signed_volume: float
    volume: float
    bbox: BoundingBox
    min_face_area: float
    mean_face_area: float
    max_face_area: float
    # provenance
    geometry: Geometry = field(repr=False, compare=False)

    def to_dict(self) -> dict:
        """JSON-safe. `geometry` is deliberately excluded - it holds arrays."""
        return {
            "source_format": self.source_format,
            "counts": {
                "vertices": self.vertex_count,
                "referenced_vertices": self.referenced_vertex_count,
                "unreferenced_vertices": self.unreferenced_vertex_count,
                "duplicate_vertices": self.duplicate_vertex_count,
                "triangles": self.face_count,
                "degenerate_faces": self.degenerate_face_count,
                "duplicate_faces": self.duplicate_face_count,
                "edges": self.unique_edge_count,
            },
            "topology": {
                "watertight": self.watertight,
                "manifold": self.manifold,
                "boundary_edges": self.boundary_edge_count,
                "nonmanifold_edges": self.nonmanifold_edge_count,
                "winding_conflicts": self.winding_conflict_edge_count,
                "winding_consistent": self.winding_consistent,
                "euler_characteristic": self.euler_characteristic,
                "genus": self.genus,
            },
            "normals": {
                "inward_faces": self.inward_face_count,
                "outward": self.normals_outward,
                "confidence": self.orientation_confidence,
            },
            "geometry": {
                "surface_area": self.surface_area,
                "signed_volume": self.signed_volume,
                "volume": self.volume,
                "bbox": self.bbox.to_dict(),
                "face_area": {
                    "min": self.min_face_area,
                    "mean": self.mean_face_area,
                    "max": self.max_face_area,
                },
            },
        }


def validate_mesh(mesh: Mesh) -> Validation:
    """Measure `mesh`. Never modifies it."""
    geo = compute(mesh)
    dup_faces = int(duplicate_face_mask(mesh.faces).sum())
    inward = int(inward_face_mask(geo).sum())
    # A closed mesh can be oriented exactly (winding conflicts are a hard
    # error). An open one only gets the centroid heuristic, so say so rather
    # than presenting both at the same confidence.
    confidence = "high" if geo.watertight and geo.winding_consistent else "advisory"
    areas = geo.areas
    return Validation(
        source_format=mesh.source_format,
        vertex_count=geo.vertex_count,
        face_count=geo.face_count,
        referenced_vertex_count=int(geo.referenced_vertices.shape[0]),
        unreferenced_vertex_count=geo.unreferenced_vertex_count,
        duplicate_vertex_count=geo.duplicate_vertex_count,
        degenerate_face_count=int(geo.degenerate_face_mask.sum()),
        duplicate_face_count=dup_faces,
        watertight=geo.watertight,
        # "manifold" in the printable sense: closed *and* coherently wound.
        # A mesh with every edge shared twice but no consistent orientation is
        # not a solid, and calling it manifold would be the wrong word.
        manifold=geo.watertight and geo.winding_consistent,
        boundary_edge_count=geo.boundary_edge_count,
        nonmanifold_edge_count=geo.nonmanifold_edge_count,
        winding_conflict_edge_count=geo.winding_conflict_edge_count,
        winding_consistent=geo.winding_consistent,
        unique_edge_count=geo.unique_edge_count,
        euler_characteristic=geo.euler_characteristic,
        genus=geo.genus,
        inward_face_count=inward,
        normals_outward=inward == 0,
        orientation_confidence=confidence,
        surface_area=geo.surface_area,
        signed_volume=geo.signed_volume,
        volume=abs(geo.signed_volume),
        bbox=BoundingBox(
            min=[float(x) for x in geo.bbox_min],
            max=[float(x) for x in geo.bbox_max],
            extents=[float(x) for x in geo.bbox_extents],
            diagonal=geo.bbox_diagonal,
        ),
        min_face_area=float(areas.min()),
        mean_face_area=float(areas.mean()),
        max_face_area=float(areas.max()),
        geometry=geo,
    )


def volume_is_degenerate(validation: Validation, *, relative: float = 1e-9) -> bool:
    """True when a supposedly-solid mesh encloses essentially nothing.

    Compares against the bounding box, so it means "a solid this size should
    enclose a visible fraction of this much space". A zero-area shell (all
    faces degenerate, or a flat sheet folded onto itself) trips this without a
    hardcoded absolute threshold that would misfire on a 0.1 mm part.
    """
    box_volume = float(np.prod(validation.bbox.extents))
    if box_volume <= 0.0:
        return True
    return validation.volume < relative * box_volume


__all__ = ["BoundingBox", "Validation", "validate_mesh", "volume_is_degenerate"]