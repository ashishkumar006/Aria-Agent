"""Repair passes, each one reporting whether it did anything and why not.

Ordering is an invariant, not a preference. Welding must precede every face
test, because two faces covering the same patch of space are only recognisable
as duplicates once their corners have been given the same index - binary STL
ships a unit cube as 36 vertices, so without the weld first the duplicate-face
count is always zero and the step reports "nothing to do" on a mesh that is
full of duplicates. Unreferenced-vertex removal must come last of the three,
because welding and face removal both strand vertices.

Post-condition: when the pass returns, a mesh that still has faces has no
unreferenced vertices. If the passes leave nothing, that is a failure and not
a success - `repair_mesh` raises rather than handing back an empty mesh that
would analyse as "0 triangles, all clear".
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .backends import Backends, probe as probe_backends
from .errors import RepairFailedError
from .geometry import (
    compute,
    conflicting_face_pairs,
    drop_faces,
    drop_unreferenced_vertices,
    duplicate_face_mask,
    flip_faces,
    inward_face_mask,
    merge_vertices,
)
from .mesh import Mesh

APPLIED = "applied"
SKIPPED = "skipped"
FAILED = "failed"
UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class RepairStep:
    """One pass's outcome. `detail` is always populated, including for skips."""

    name: str
    status: str
    changed: int
    detail: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "changed": self.changed,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RepairOptions:
    """Which passes to run and how hard to try. Defaults are the v1 behaviour."""

    weld: bool = True
    merge_tolerance: float = 0.0
    drop_degenerate_faces: bool = True
    drop_duplicate_faces: bool = True
    drop_unreferenced_vertices: bool = True
    fix_normals: bool = True
    #: Resolve same-direction shared edges by iteration. Bounded because a
    #: non-orientable surface cannot be fixed by winding alone and would
    #: otherwise loop forever.
    max_normal_passes: int = 8
    #: Try the manifold3d pass. It canonicalises; see the note in
    #: `_manifold3d_watertight` about what it cannot do.
    watertight: bool = True


@dataclass(frozen=True)
class RepairResult:
    mesh: Mesh
    steps: list[RepairStep] = field(default_factory=list)

    def step(self, name: str) -> RepairStep:
        for s in self.steps:
            if s.name == name:
                return s
        raise KeyError(name)

    def by_status(self, status: str) -> list[RepairStep]:
        return [s for s in self.steps if s.status == status]

    @property
    def applied(self) -> list[RepairStep]:
        return self.by_status(APPLIED)

    @property
    def skipped(self) -> list[RepairStep]:
        return self.by_status(SKIPPED)

    @property
    def failed(self) -> list[RepairStep]:
        return self.by_status(FAILED)

    @property
    def unavailable(self) -> list[RepairStep]:
        return self.by_status(UNAVAILABLE)

    def to_dict(self) -> dict:
        return {
            "steps": [s.to_dict() for s in self.steps],
            "summary": {
                status: len(self.by_status(status))
                for status in (APPLIED, SKIPPED, FAILED, UNAVAILABLE)
            },
        }


def repair_mesh(
    mesh: Mesh,
    options: RepairOptions | None = None,
    *,
    backends: Backends | None = None,
) -> RepairResult:
    """Run the repair passes and report each one."""
    opts = options or RepairOptions()
    backends = backends or probe_backends()
    steps: list[RepairStep] = []
    current = mesh

    if opts.weld:
        current, step = _weld(current, opts.merge_tolerance)
        steps.append(step)
    else:
        steps.append(RepairStep("weld_vertices", SKIPPED, 0, "disabled by options"))

    if opts.drop_degenerate_faces:
        current, step = _drop_degenerate(current)
        steps.append(step)
    else:
        steps.append(RepairStep("drop_degenerate_faces", SKIPPED, 0, "disabled by options"))

    if opts.drop_duplicate_faces:
        current, step = _drop_duplicate_faces(current)
        steps.append(step)
    else:
        steps.append(RepairStep("drop_duplicate_faces", SKIPPED, 0, "disabled by options"))

    if opts.drop_unreferenced_vertices:
        current, step = _drop_unreferenced(current)
        steps.append(step)
    else:
        steps.append(
            RepairStep("drop_unreferenced_vertices", SKIPPED, 0, "disabled by options")
        )

    _guard_non_empty(current, steps)

    if opts.fix_normals:
        current, step = _fix_normals(current, opts.max_normal_passes)
        steps.append(step)
    else:
        steps.append(RepairStep("fix_normals", SKIPPED, 0, "disabled by options"))

    if opts.watertight:
        current, step = _manifold3d_watertight(current, backends)
        steps.append(step)
    else:
        steps.append(
            RepairStep("manifold3d_watertight", SKIPPED, 0, "disabled by options")
        )

    return RepairResult(mesh=current, steps=steps)


# -- passes -----------------------------------------------------------------

def _weld(mesh: Mesh, tolerance: float) -> tuple[Mesh, RepairStep]:
    before = mesh.vertex_count
    vertices, faces, removed = merge_vertices(mesh.vertices, mesh.faces, tolerance)
    welded = mesh.with_arrays(vertices, faces)
    if removed == 0:
        how = "exact coordinate match" if tolerance == 0.0 else f"{tolerance} grid"
        return mesh, RepairStep(
            "weld_vertices", SKIPPED, 0,
            f"no coincident vertices at {how}; nothing to weld",
        )
    return welded, RepairStep(
        "weld_vertices", APPLIED, removed,
        f"{removed} of {before} vertices were coincident and merged",
    )


def _drop_degenerate(mesh: Mesh) -> tuple[Mesh, RepairStep]:
    mask = compute(mesh).degenerate_face_mask
    count = int(mask.sum())
    if count == 0:
        return mesh, RepairStep(
            "drop_degenerate_faces", SKIPPED, 0,
            "every triangle encloses area and has three distinct corners",
        )
    return drop_faces(mesh, mask), RepairStep(
        "drop_degenerate_faces", APPLIED, count,
        f"{count} zero-area or repeated-corner triangle(s) removed",
    )


def _drop_duplicate_faces(mesh: Mesh) -> tuple[Mesh, RepairStep]:
    mask = duplicate_face_mask(mesh.faces)
    count = int(mask.sum())
    if count == 0:
        return mesh, RepairStep(
            "drop_duplicate_faces", SKIPPED, 0,
            "no two triangles share the same three vertices",
        )
    return drop_faces(mesh, mask), RepairStep(
        "drop_duplicate_faces", APPLIED, count,
        f"{count} repeated triangle(s) removed, keeping the first of each set",
    )


def _drop_unreferenced(mesh: Mesh) -> tuple[Mesh, RepairStep]:
    compacted, removed = drop_unreferenced_vertices(mesh)
    if removed == 0:
        return mesh, RepairStep(
            "drop_unreferenced_vertices", SKIPPED, 0,
            "every vertex is used by at least one triangle",
        )
    return compacted, RepairStep(
        "drop_unreferenced_vertices", APPLIED, removed,
        f"{removed} vertex/vertices not referenced by any triangle removed",
    )


def _fix_normals(mesh: Mesh, max_passes: int) -> tuple[Mesh, RepairStep]:
    """Orient faces outward, then settle same-direction shared edges.

    Two phases, because they answer different questions. The inward-face test
    is global ("is this solid inside-out?") but only exact for a star-shaped
    solid. The shared-edge test is local and exact ("do these two triangles
    agree?") but says nothing about which way the whole solid points. Doing
    only the first mis-flips the inner faces of an L-bracket; doing only the
    second leaves a correctly-wound but inside-out part.

    Phase two flips the higher face id of each conflicting pair, which is
    deterministic but not guaranteed to converge on a non-orientable surface;
    `max_passes` bounds it and the step reports what was left over rather than
    claiming success.
    """
    geo = compute(mesh)
    inward = inward_face_mask(geo)
    flipped = int(inward.sum())
    current = flip_faces(mesh, inward) if flipped else mesh

    passes = 0
    for _ in range(max(1, max_passes)):
        geo = compute(current)
        first, second = conflicting_face_pairs(geo)
        if first.size == 0:
            break
        mask = np.zeros(current.face_count, dtype=bool)
        mask[np.maximum(first, second)] = True
        current = flip_faces(current, mask)
        passes += 1

    geo = compute(current)
    remaining = int(conflicting_face_pairs(geo)[0].size)
    still_inward = int(inward_face_mask(geo).sum())

    if flipped == 0 and remaining == 0:
        return mesh, RepairStep(
            "fix_normals", SKIPPED, 0,
            "winding already consistent and faces already point outward",
        )
    detail_parts = [f"{flipped} inward face(s) flipped"]
    if passes:
        detail_parts.append(f"{passes} conflict-resolution pass(es)")
    if remaining:
        detail_parts.append(
            f"{remaining} same-direction edge(s) unresolved after {max_passes} passes - "
            "surface is not consistently orientable by winding alone"
        )
    status = APPLIED if remaining == 0 else FAILED
    if remaining == 0 and still_inward:
        status = APPLIED  # reported in detail; a heuristic, not a hard failure
        detail_parts.append(f"{still_inward} face(s) still read as inward")
    return current, RepairStep("fix_normals", status, flipped, "; ".join(detail_parts))


def _manifold3d_watertight(mesh: Mesh, backends: Backends) -> tuple[Mesh, RepairStep]:
    """Canonicalise through manifold3d, which is the strict oracle here.

    What manifold3d can do: collapse degenerate triangles, merge vertices and
    resolve non-manifold edges, because its constructor only accepts an
    oriented 2-manifold and returns empty with an error status otherwise.

    What it cannot do: close a hole. A missing face is a boundary, and no
    boolean kernel invents one. That is deliberate on our side too - a bracket's
    through-hole is a boundary edge, and auto-filling boundaries would silently
    destroy exactly the features this module is supposed to help with. So a
    mesh with holes is reported as not watertight, and this step's detail says
    how many boundaries are left rather than pretending to have fixed them.

    Unverified against a live manifold3d: the wheel is not installed in this
    venv, so this branch is exercised by a stub implementing the documented
    API (Manifold/Error/MeshGL from bindings/python/examples/all_apis.py).
    """
    if not backends.has("manifold3d"):
        return mesh, RepairStep(
            "manifold3d_watertight", UNAVAILABLE, 0,
            "manifold3d is not installed; watertightness is reported, not repaired",
        )
    module = backends.require("manifold3d")
    before = compute(mesh)
    try:
        source_mesh, note = _to_meshgl(module, mesh.vertices, mesh.faces)
        merged = bool(source_mesh.merge())
        solid = module.Manifold(source_mesh)
        status = solid.status()
        no_error = getattr(getattr(module, "Error", None), "NoError", 0)
        if status != no_error:
            return mesh, RepairStep(
                "manifold3d_watertight", FAILED, 0,
                f"manifold3d rejected the mesh: status={status}. It only accepts an "
                "oriented 2-manifold, so a mesh with holes or non-manifold edges "
                "cannot be canonicalised.",
            )
        if solid.is_empty():
            return mesh, RepairStep(
                "manifold3d_watertight", FAILED, 0,
                "manifold3d consumed the mesh and produced an empty solid; "
                "kept the input rather than returning nothing",
            )
        out = solid.to_mesh()
        vertices, faces = _from_meshgl(out)
    except Exception as exc:  # a C++ boundary can raise anything
        return mesh, RepairStep(
            "manifold3d_watertight", FAILED, 0,
            f"{type(exc).__name__}: {exc}",
        )

    try:
        repaired = Mesh.create(
            vertices, faces, source_format=mesh.source_format, units=mesh.units
        )
    except Exception as exc:
        return mesh, RepairStep(
            "manifold3d_watertight", FAILED, 0,
            f"manifold3d output failed the mesh integrity check: {type(exc).__name__}: {exc}",
        )

    after = compute(repaired)
    parts = [note]
    if merged:
        parts.append("merge vectors applied before construction")
    parts.append(
        f"{before.vertex_count} -> {after.vertex_count} vertices, "
        f"{before.face_count} -> {after.face_count} triangles"
    )
    # `changed` counts what the pass removed, so it is 0 for a genuine no-op and
    # APPLIED/SKIPPED mean the same thing here as in every other pass.
    removed = max(
        0,
        (before.vertex_count - after.vertex_count)
        + (before.face_count - after.face_count),
    )
    if not after.watertight:
        return repaired, RepairStep(
            "manifold3d_watertight", APPLIED if removed else SKIPPED, removed,
            "; ".join(parts)
            + f"; still not watertight - {after.boundary_edge_count} boundary edge(s) "
              "remain, and manifold3d does not fill holes",
        )
    if before.watertight and removed == 0:
        return repaired, RepairStep(
            "manifold3d_watertight", SKIPPED, 0,
            "; ".join(parts) + "; already watertight",
        )
    return repaired, RepairStep(
        "manifold3d_watertight", APPLIED, removed,
        "; ".join(parts)
        + ("; now watertight" if not before.watertight else "; already watertight"),
    )


def _to_meshgl(module, vertices: np.ndarray, faces: np.ndarray):
    """Build a manifold3d `MeshGL`, trying the index dtypes it accepts.

    The bindings take `tri_verts` as a uint32 buffer in the JS build and an
    int buffer in others, and the Python type caster is strict, so the dtype is
    discovered rather than guessed. The winning combination is reported in the
    step detail so the output records what actually ran.
    """
    vp = np.ascontiguousarray(vertices, dtype=np.float32).reshape(-1)
    for index_dtype in (np.uint32, np.int32, np.int64):
        tv = np.ascontiguousarray(faces, dtype=index_dtype).reshape(-1)
        try:
            built = module.MeshGL(vp, tv)
        except TypeError:
            continue
        return built, f"MeshGL(float32, {np.dtype(index_dtype).name})"
    raise TypeError(
        "manifold3d.MeshGL rejected every index dtype tried "
        "(uint32, int32, int64)"
    )


def _from_meshgl(out) -> tuple[np.ndarray, np.ndarray]:
    """Pull (vertices, faces) back out of a manifold3d mesh, or raise."""
    num_prop = int(out.num_prop)
    if num_prop < 3:
        raise ValueError(f"manifold3d returned {num_prop} vertex properties, need >= 3")
    vp = np.asarray(out.vert_properties, dtype=np.float64)
    if vp.size % num_prop:
        raise ValueError(
            f"vert_properties has {vp.size} values, not a multiple of num_prop={num_prop}"
        )
    vertices = vp.reshape(-1, num_prop)[:, :3]
    faces = np.asarray(out.tri_verts, dtype=np.int64).reshape(-1, 3)
    return np.ascontiguousarray(vertices), np.ascontiguousarray(faces)


def _guard_non_empty(mesh: Mesh, steps: list[RepairStep]) -> None:
    """Raise if the passes have consumed every triangle.

    This is the difference between a failed repair and a clean bill of health
    for a mesh that is now nothing. Reporting "0 triangles, no problems" would
    be a lie a caller has no way to detect.
    """
    if mesh.face_count == 0 or mesh.vertex_count == 0:
        removed = [s.name for s in steps if s.status == APPLIED]
        raise RepairFailedError(
            "repair removed every triangle; the input had no valid surface",
            steps_that_ran=removed,
        )


__all__ = [
    "APPLIED",
    "SKIPPED",
    "FAILED",
    "UNAVAILABLE",
    "RepairOptions",
    "RepairResult",
    "RepairStep",
    "repair_mesh",
]