"""Optimisation passes for a part, with a measured objective.

`parts` v1 deliberately stopped at validate/repair: "topology optimisation and
mesh decimation" are named as out of scope in its docstring. That was the right
call for a validation tool, but an agentic optimiser needs something to
actually DO, not just something to report on.

What is here is deliberately modest and, more importantly, **measurable**. Each
pass takes a mesh and a set of parameters and returns a new mesh plus the
numbers that justify it. Nothing here invents geometry:

  - decimate      : reduce triangles toward a budget (trimesh)
  - smooth        : Laplacian relaxation, bounds-checked
  - thin_walls    : DETECT thin regions, never fill them
  - overhangs      : DETECT faces past the build angle, never trim them

The two detect passes do not modify the mesh at all. That follows the same
principle v1 used when it refused to hole-fill a boundary: a through-hole and a
missing face are indistinguishable by topology alone, so guessing is worse than
reporting. An agent gets the measurement and decides.

The objective function is the part that makes a loop possible. `score` is
low-is-better and combines the things a printability request actually cares
about, so "is this iteration better than the last one" is one comparison rather
than a judgement call.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .mesh import Mesh

# ---------------------------------------------------------------------------
# measurable properties
# ---------------------------------------------------------------------------


def _volume(mesh: Mesh) -> float:
    """Signed volume via the divergence theorem over every triangle.

    Signed, so an inside-out mesh reports negative volume and gets flagged by
    `score` rather than silently optimising a flipped solid.

    Measured from the area-weighted centroid, not the world origin. The two
    agree for a closed mesh, but an open mesh's origin-based sum grows with the
    coordinate offset (a mesh a million mm away reported a million-scale
    "volume"). The reference cancels that offset. This is the same definition
    `geometry.Geometry.signed_volume` uses.
    """
    v = mesh.vertices
    f = mesh.faces
    if len(f) == 0:
        return 0.0
    a = v[f[:, 0]]
    b = v[f[:, 1]]
    c = v[f[:, 2]]
    cross = np.cross(b - a, c - a)
    areas = 0.5 * np.linalg.norm(cross, axis=1)
    total_area = float(areas.sum())
    if total_area > 1e-12:
        reference = ((a + b + c) / 3.0 * areas[:, None]).sum(axis=0) / total_area
    elif len(v):
        reference = v.mean(axis=0)
    else:
        return 0.0
    a = a - reference
    b = b - reference
    c = c - reference
    return float(np.sum(np.einsum("ij,ij->i", a, np.cross(b, c))) / 6.0)


def _surface_area(mesh: Mesh) -> float:
    v, f = mesh.vertices, mesh.faces
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    return float(np.sum(0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)))


def _edge_lengths(mesh: Mesh) -> np.ndarray:
    v, f = mesh.vertices, mesh.faces
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    return np.concatenate([
        np.linalg.norm(b - a, axis=1),
        np.linalg.norm(c - b, axis=1),
        np.linalg.norm(a - c, axis=1),
    ])


@dataclass
class Metrics:
    """Everything the agent and the UI need to judge a mesh."""

    vertices: int
    faces: int
    volume_mm3: float
    surface_area_mm2: float
    bbox_min: list[float]
    bbox_max: list[float]
    dimensions_mm: list[float]
    min_edge_mm: float
    median_edge_mm: float
    # detection results; 0.0 when the check did not run
    overhang_faces: int = 0
    overhang_area_mm2: float = 0.0
    thin_regions: int = 0
    thinnest_wall_mm: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "vertices": self.vertices,
            "faces": self.faces,
            "volume_mm3": round(self.volume_mm3, 3),
            "surface_area_mm2": round(self.surface_area_mm2, 3),
            "bbox_min": [round(x, 3) for x in self.bbox_min],
            "bbox_max": [round(x, 3) for x in self.bbox_max],
            "dimensions_mm": [round(x, 3) for x in self.dimensions_mm],
            "min_edge_mm": round(self.min_edge_mm, 4),
            "median_edge_mm": round(self.median_edge_mm, 4),
            "overhang_faces": self.overhang_faces,
            "overhang_area_mm2": round(self.overhang_area_mm2, 3),
            "thin_regions": self.thin_regions,
            "thinnest_wall_mm": round(self.thinnest_wall_mm, 4),
        }


def measure(mesh: Mesh, *, overhang_angle_deg: float = 45.0,
            wall_mm: float | None = None) -> Metrics:
    """Full measurement pass. Deterministic and side-effect free."""
    v, f = mesh.vertices, mesh.faces
    edges = _edge_lengths(mesh)
    lo = v.min(axis=0) if len(v) else np.zeros(3)
    hi = v.max(axis=0) if len(v) else np.zeros(3)

    m = Metrics(
        vertices=int(len(v)),
        faces=int(len(f)),
        volume_mm3=_volume(mesh),
        surface_area_mm2=_surface_area(mesh),
        bbox_min=[float(x) for x in lo],
        bbox_max=[float(x) for x in hi],
        dimensions_mm=[float(x) for x in (hi - lo)],
        min_edge_mm=float(edges.min()) if edges.size else 0.0,
        median_edge_mm=float(np.median(edges)) if edges.size else 0.0,
    )

    # Overhangs: a face whose normal points downward more steeply than the
    # build angle. Measured, never trimmed - trimming a support feature is a
    # geometry decision an operator has to make.
    if len(f):
        a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
        n = np.cross(b - a, c - a)
        norm = np.linalg.norm(n, axis=1)
        good = norm > 1e-12
        if np.any(good):
            n[good] /= norm[good][:, None]
            # build direction is +Z; cos(90 + angle) is the threshold
            thresh = math.cos(math.radians(90.0 + overhang_angle_deg))
            bad = good & (n[:, 2] < thresh)
            m.overhang_faces = int(bad.sum())
            m.overhang_area_mm2 = float(0.5 * norm[bad].sum())

    # Thin walls: local thickness implied by the mesh scale. Uses the median
    # edge as the reference, because that is what a nozzle layer actually is.
    if edges.size:
        med = float(np.median(edges))
        if wall_mm is None:
            wall_mm = max(med * 0.5, 1e-3)
        m.thinnest_wall_mm = float(edges.min())
        m.thin_regions = int((edges < wall_mm).sum())

    return m


def score(m: Metrics, *, target_faces: int | None = None) -> float:
    """A single lower-is-better number, so "did this iteration help?" is a
    comparison rather than a judgement.

    Weights reflect what a printability request asks for: fewer triangles to
    slice, no inverted solid, no geometry below the feature size. Detection
    findings (overhangs, thin regions) enter as a penalty so an agent can see
    that a change which halved the triangle count but doubled the overhangs is
    a bad trade.
    """
    s = 0.0
    # triangle budget, relative to the target when one is known
    if target_faces:
        s += 4.0 * abs(m.faces - target_faces) / max(1.0, target_faces)
    else:
        s += 0.004 * m.faces
    # An inverted or empty solid is a hard failure, so the penalty must
    # dominate every normal objective - including a huge target_faces miss -
    # or an inside-out mesh can rank above a valid one (C6).
    if m.volume_mm3 <= 0:
        s += 1e12
    # features thinner than the reference
    s += 0.02 * m.thin_regions
    # unbuildable overhangs, per cm^2
    s += 8.0 * (m.overhang_area_mm2 / 100.0)
    return float(s)


# ---------------------------------------------------------------------------
# the passes
# ---------------------------------------------------------------------------


@dataclass
class PassResult:
    mesh: Mesh
    before: Metrics
    after: Metrics
    changed: bool
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "note": self.note,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "faces_before": self.before.faces,
            "faces_after": self.after.faces,
        }


def decimate(mesh: Mesh, target_faces: int, *,
             before: Metrics | None = None) -> PassResult:
    """Reduce triangles toward `target_faces`.

    Requires trimesh. Refuses rather than approximating when the mesh is
    already at or below the target, and refuses a target above the current
    count (decimation only ever removes).
    """
    before = before or measure(mesh)
    if target_faces >= before.faces:
        return PassResult(mesh, before, before, False,
                          f"already at or below {target_faces} faces")
    try:
        import trimesh as _tm
    except ImportError:
        return PassResult(mesh, before, before, False,
                          "trimesh is not installed; cannot decimate")
    try:
        out = _tm.Trimesh(vertices=mesh.vertices, faces=mesh.faces,
                           process=False)
        # trimesh 5.x exposes ONE decimation entry point,
        # `simplify_quadric_decimation(percent=None, face_count=None,
        # aggression=None)`. There is no `simplify` on Trimesh and the kwarg is
        # `face_count`, not `target_faces`/`target_reduction` - guessing either
        # raises and the pass reports "decimation failed" for every input.
        out = out.simplify_quadric_decimation(face_count=max(1, int(target_faces)))
        new = mesh.with_arrays(np.asarray(out.vertices, dtype=np.float64),
                               np.asarray(out.faces, dtype=np.int64))
    except Exception as exc:                      # noqa: BLE001
        return PassResult(mesh, before, before, False,
                          f"decimation failed: {type(exc).__name__}: {exc}")
    after = measure(new)
    return PassResult(new, before, after, after.faces != before.faces,
                      f"{before.faces} -> {after.faces} faces")


def smooth(mesh: Mesh, iterations: int = 1, strength: float = 0.5, *,
           before: Metrics | None = None) -> PassResult:
    """Laplacian relaxation along mesh edges.

    `strength` is clamped so a single pass cannot collapse geometry, and the
    result is rejected outright if the volume moves by more than 2%: smoothing
    that visibly changes the part is not smoothing, it is damage.
    """
    before = before or measure(mesh)
    strength = max(0.0, min(1.0, float(strength)))
    if strength == 0.0 or iterations <= 0:
        return PassResult(mesh, before, before, False, "nothing to do")

    v = mesh.vertices.astype(np.float64).copy()
    f = mesh.faces
    n = len(v)
    if n == 0 or len(f) == 0:
        return PassResult(mesh, before, before, False, "empty mesh")

    edges = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]], axis=0)
    lo = np.minimum(edges[:, 0], edges[:, 1])
    hi = np.maximum(edges[:, 0], edges[:, 1])

    for _ in range(max(1, int(iterations))):
        acc = np.zeros_like(v)
        cnt = np.zeros(n, dtype=np.float64)
        for a, b in ((lo, hi), (hi, lo)):
            # `np.add.at` is required: plain `acc[a] += v[b]` silently drops
            # repeated indices, which is every shared vertex in a closed mesh.
            np.add.at(acc, a, v[b])
            np.add.at(cnt, a, 1.0)
        movable = cnt > 0
        avg = np.zeros_like(v)
        avg[movable] = acc[movable] / cnt[movable][:, None]
        v = np.where(movable[:, None], v + (avg - v) * strength, v)

    new = mesh.with_arrays(v, f)
    after = measure(new)
    if before.volume_mm3 and abs(after.volume_mm3 - before.volume_mm3) \
            / abs(before.volume_mm3) > 0.02:
        return PassResult(mesh, before, before, False,
                          f"rejected: smoothing moved the volume "
                          f"{(after.volume_mm3 - before.volume_mm3) / before.volume_mm3 * 100:.1f}%")
    return PassResult(new, before, after, True,
                      f"volume {before.volume_mm3:.1f} -> {after.volume_mm3:.1f} mm3")


# ---------------------------------------------------------------------------
# the registry an agent chooses from
# ---------------------------------------------------------------------------

@dataclass
class Pass:
    """One optimiser the agent may select, with its parameters."""

    name: str
    description: str
    params: dict[str, Any] = field(default_factory=dict)
    modifies: bool = True


PASSES: list[Pass] = [
    Pass("decimate",
         "Cut triangles toward a budget. Needs trimesh. Never adds faces.",
         {"target_faces": 20000}),
    Pass("smooth",
         "Laplacian relaxation. Rejected if the volume moves more than 2%.",
         {"iterations": 1, "strength": 0.4}),
    Pass("detect_overhang",
         "Report faces past the build angle. Does not modify the mesh.",
         {"overhang_angle_deg": 45.0}, modifies=False),
    Pass("detect_thin_walls",
         "Report edges below the feature size. Does not modify the mesh.",
         {}, modifies=False),
]


def run_pass(name: str, mesh: Mesh, params: dict[str, Any] | None = None
             ) -> PassResult:
    """Dispatch one pass by name. Unknown names fail loudly."""
    p = dict(params or {})
    if name == "decimate":
        return decimate(mesh, int(p.get("target_faces", 20000)))
    if name == "smooth":
        return smooth(mesh, int(p.get("iterations", 1)),
                      float(p.get("strength", 0.4)))
    if name == "detect_overhang":
        m = measure(mesh, overhang_angle_deg=float(p.get("overhang_angle_deg", 45.0)))
        return PassResult(mesh, m, m, False,
                          f"{m.overhang_faces} faces over the build angle")
    if name == "detect_thin_walls":
        m = measure(mesh)
        return PassResult(mesh, m, m, False,
                          f"{m.thin_regions} edges below the feature size")
    raise KeyError(f"unknown pass: {name!r}")