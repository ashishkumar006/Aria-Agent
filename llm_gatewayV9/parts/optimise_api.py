"""Optimise + render endpoints for the parts module.

`parts` v1 was validate/repair only. `optimise.py` adds passes that measurably
improve a mesh; this adds the HTTP surface an agent loops against and a
viewer draws from.

Two shapes, deliberately:

* ``/v1/parts/optimise`` takes a PASS + params and returns the measured
  before/after plus the objective. An agent can call it repeatedly and compare
  two numbers, which is what makes a loop converge instead of wander.
* ``/v1/parts/geometry`` returns the triangle soup a viewer needs. The
  analyse/report endpoints return measurements but no vertices, so nothing
  could actually be rendered.

Both are stateless, like v1: the caller holds the file and re-uploads it per
call. No ``part_id``, no persistence, no queue. That keeps the agent loop
honest - every iteration is a real round trip through a real measurement.
"""
from __future__ import annotations

import base64
import json

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from . import optimise as O
from .export import to_binary_stl
from .mesh import DEFAULT_MAX_BYTES, Mesh
from .report import analyse_part
from .errors import PartError


def _stl_b64(mesh: Mesh) -> str:
    """Binary STL, base64'd, so an agent can chain iterations in one round
    trip instead of holding the mesh itself."""
    return base64.b64encode(to_binary_stl(mesh)).decode("ascii")

router = APIRouter()

#: Above this, a mesh is too heavy to ship as JSON to a viewer. The viewer
#: should decimate first rather than receive a multi-megabyte payload.
MAX_RENDER_FACES = 400_000


async def _load(request: Request, file: UploadFile, scale: float,
                 max_bytes: int = DEFAULT_MAX_BYTES, weld: bool = True):
    """Read the upload under the byte budget, then run the existing loader.

    The chunked read is v1's, and the reason matters: `await file.read()` with
    no limit turns one request into one arbitrary-sized allocation.
    """
    from .ingest import load
    import io

    buf = bytearray()
    total = 0
    while True:
        chunk = await file.read(1 << 20)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(413, detail={
                "code": "mesh_too_large",
                "message": f"upload exceeds the {max_bytes} byte budget"})
        buf.extend(chunk)
    if not buf:
        raise HTTPException(422, detail={
            "code": "empty_mesh",
            "message": "the upload was empty"})
    # `load` is SYNCHRONOUS, so `run_in_threadpool` must be awaited - leaving
    # it unawaited returns a coroutine and every downstream `mesh.vertices`
    # raises "'coroutine' object has no attribute 'vertices'" as a 500.
    loaded = await run_in_threadpool(load, bytes(buf), scale=scale)
    mesh = getattr(loaded, "mesh", None) or loaded

    if weld:
        # STL is a non-indexed format: every triangle carries its own copy of
        # each vertex, so a 2,304-triangle sphere arrives as 6,912 vertices.
        # Quadric decimation is defined on an INDEXED mesh and silently
        # returns the input untouched when handed the duplicated form - the
        # pass reports "no change" for every real upload. Welding first is what
        # `repair` does too; here it is a precondition for optimisation, not
        # an optional repair.
        from .repair import RepairOptions, repair_mesh
        try:
            fixed = repair_mesh(mesh, RepairOptions(
                weld=True, fix_normals=False))
            cand = getattr(fixed, "mesh", None)
            if cand is not None and cand.face_count > 0:
                mesh = cand
        except Exception:
            # A mesh that will not weld is still worth measuring and reporting;
            # only the optimising passes need the indexed form.
            pass
    return mesh


@router.get("/v1/parts/optimise/passes")
async def list_passes():
    """What the agent may choose between, with each pass's parameters.

    A client must read this rather than hardcoding pass names: whether a pass
    can run at all depends on optional backends being installed, and
    `capabilities` reports those.
    """
    return {
        "schema_version": "1",
        "passes": [
            {"name": p.name, "description": p.description,
             "params": p.params, "modifies": p.modifies}
            for p in O.PASSES
        ],
        "notes": [
            "detection passes report and never modify geometry",
            "a pass that would damage the part refuses and says why",
            "compare objective_before/objective_after to decide whether to keep "
            "a result; a pass can legitimately report 'no change'",
        ],
    }


@router.post("/v1/parts/optimise")
async def optimise_part(
    request: Request,
    file: UploadFile = File(...),
    pass_name: str = "decimate",
    scale: float = 1.0,
    target_faces: int | None = None,
    iterations: int = 1,
    strength: float = 0.4,
    max_bytes: int = DEFAULT_MAX_BYTES,
):
    """Run one optimisation pass and report the measured delta.

    The response always carries the objective before and after, so a caller
    can decide whether to keep the result. It is returned even when the pass
    refused, because "this will not work on this part" is an answer.
    """
    mesh = await _load(request, file, scale, max_bytes)
    print(f"[parts.optimise] loaded pass={pass_name} target={target_faces} "
          f"verts={mesh.vertex_count} faces={mesh.face_count}", flush=True)
    try:
        params = {
            "target_faces": target_faces if target_faces else 20000,
            "iterations": iterations,
            "strength": strength,
        }
        result = await run_in_threadpool(O.run_pass, pass_name, mesh, params)
    except KeyError as exc:
        raise HTTPException(404, detail={
            "code": "unknown_pass",
            "message": str(exc)}) from exc
    except PartError as exc:
        raise HTTPException(422, detail={
            "code": exc.code, "message": exc.message}) from exc

    obj_before = O.score(result.before)
    obj_after = O.score(result.after)
    body = result.to_dict()
    body.update({
        "pass": pass_name,
        "objective_before": round(obj_before, 6),
        "objective_after": round(obj_after, 6),
        # lower is better, so an improvement is a NEGATIVE delta
        "improved": obj_after < obj_before,
        # The optimised mesh, so the caller can CHAIN: feed this straight back
        # as the next request's upload. Without it an agent can only ever make
        # one change per upload, and cannot tell whether a second pass helped
        # or hurt - `objective_before` here is always THIS request's input.
        # Skipped for the detection passes, which never change anything and
        # would otherwise ship a copy of the whole mesh for nothing.
        "result_stl_base64": (
            None if not result.changed else _stl_b64(result.mesh)),
    })
    return body


@router.post("/v1/parts/geometry")
async def part_geometry(
    request: Request,
    file: UploadFile = File(...),
    scale: float = 1.0,
    max_faces: int | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
):
    """Triangle soup for a viewer, decimated to `max_faces` if asked.

    Returns interleaved positions and a flat index array rather than a blob
    or a base64 string: that is the form a WebGL buffer wants, and it keeps
    the payload inspectable in a client test.
    """
    mesh = await _load(request, file, scale, max_bytes)

    limit = int(max_faces or MAX_RENDER_FACES)
    metrics = O.measure(mesh)

    if metrics.faces > limit:
        res = await run_in_threadpool(O.decimate, mesh, limit)
        if res.changed:
            mesh = res.mesh
            metrics = O.measure(mesh)
            note = f"decimated from {res.before.faces} to {metrics.faces} faces"
        else:
            note = res.note
    else:
        note = "as uploaded"

    if metrics.faces > MAX_RENDER_FACES:
        raise HTTPException(413, detail={
            "code": "mesh_too_large",
            "message": (f"{metrics.faces} faces is above the "
                        f"{MAX_RENDER_FACES} render budget; decimate it first"),
        })

    verts = mesh.vertices
    faces = mesh.faces
    # interleaved float32 xyz triples
    positions = [round(float(x), 5) for row in verts for x in row]
    indices = [int(i) for i in faces.reshape(-1)]

    return {
        "schema_version": "1",
        "source_format": mesh.source_format,
        "metrics": metrics.to_dict(),
        "objective": round(O.score(metrics), 6),
        "geometry": {
            "positions": positions,
            "indices": indices,
            "position_count": int(len(verts)),
            "face_count": int(len(faces)),
        },
        "note": note,
    }