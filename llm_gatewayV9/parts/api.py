"""HTTP surface for the parts module. NOT REGISTERED - see module footer.

This router exists so the endpoint contract is reviewable as code and testable
in isolation, but nothing imports it into `main.py`. Registering it is a
one-line change the owner of this repo makes deliberately; see the footer for
exactly what to add.

Two things here are not decoration:

* the upload is read in chunks and abandoned the moment it passes the byte
  budget. `await file.read()` with no limit turns one request into one
  arbitrary-sized allocation, and Starlette's spool threshold only decides
  where the bytes land, not how many arrive.
* `analyse_part` runs in a threadpool. It is blocking numpy work, and calling
  it on the event loop stalls every other request for the length of the mesh.

Also deliberate: no `part_id`, no persistence, no queue. This endpoint is
stateless and does I/O only against the caller's upload.
"""
from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from .mesh import DEFAULT_MAX_BYTES, DEFAULT_MAX_FACES, DEFAULT_MAX_VERTICES
from .repair import RepairOptions
from .report import SCHEMA_VERSION, analyse_part
from .slicer import slicer_callable
from . import backends as backends_mod
from .errors import MeshTooLargeError, PartError

router = APIRouter()

#: PartError.code -> HTTP status. Anything unmapped is a 422: the request was
#: syntactically fine and the *mesh* was not, which is a client error.
_STATUS = {
    "part_error": 422,
    "unsupported_format": 415,
    "mesh_too_large": 413,
    "mesh_integrity": 422,
    "empty_mesh": 422,
    "repair_failed": 422,
    "missing_backend": 501,
}

_CHUNK = 1 << 20


async def _read_capped(upload: UploadFile, max_bytes: int) -> bytes:
    """Read an upload, refusing to buffer more than `max_bytes`."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise MeshTooLargeError(
                f"upload exceeds the {max_bytes} byte limit",
                kind="bytes", limit=max_bytes, actual=total,
                filename=upload.filename,
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _raise(exc: PartError) -> None:
    raise HTTPException(_STATUS.get(exc.code, 422), detail=exc.to_dict())


async def _analyse(
    request: Request,
    upload: UploadFile,
    *,
    repair: bool,
    slicer: bool,
    scale: float,
    max_bytes: int,
) -> dict:
    """Shared body for both endpoints."""
    from channels_api import _loopback_only

    _loopback_only(request)
    options = RepairOptions()
    slicer_fn = slicer_callable() if slicer else None
    try:
        # The read cap lives inside the try on purpose: it raises the same
        # typed errors as the pipeline, and outside the try it would surface as
        # an unhandled 500 instead of the 413 it is.
        data = await _read_capped(upload, max_bytes)
        report = await run_in_threadpool(
            analyse_part,
            data,
            filename=upload.filename,
            repair=repair,
            options=options,
            max_bytes=max_bytes,
            max_faces=DEFAULT_MAX_FACES,
            max_vertices=DEFAULT_MAX_VERTICES,
            slicer=slicer_fn,
            scale=scale,
        )
    except PartError as exc:
        # The status carries the reason: a 413 for an oversized upload and a 415
        # for an unreadable container are different problems and a client
        # branching on them should not have to parse a message. `error_report`
        # exists for callers that would rather have one uniform 200 body.
        _raise(exc)
    except Exception as exc:  # a bug in here is a 500, never a traceback
        raise HTTPException(
            500,
            detail={
                "code": "internal_error",
                "message": f"unhandled {type(exc).__name__} while analysing the mesh",
            },
        ) from exc
    return report.to_dict()


@router.post("/v1/parts/validate")
async def validate_part(
    request: Request,
    file: UploadFile = File(...),
    scale: float = 1.0,
    max_bytes: int = DEFAULT_MAX_BYTES,
):
    """Measure an uploaded mesh without modifying it."""
    return await _analyse(
        request, file, repair=False, slicer=False,
        scale=scale, max_bytes=max_bytes,
    )


@router.post("/v1/parts/analyse")
async def analyse(
    request: Request,
    file: UploadFile = File(...),
    repair: bool = True,
    slicer: bool = False,
    scale: float = 1.0,
    max_bytes: int = DEFAULT_MAX_BYTES,
):
    """Repair then measure. `slicer=true` adds a PrusaSlicer run (slow)."""
    return await _analyse(
        request, file, repair=repair, slicer=slicer,
        scale=scale, max_bytes=max_bytes,
    )


@router.get("/v1/parts/capabilities")
async def capabilities():
    """Which optional backends are present, and the declared budgets.

    A client should read this before promising a PLY upload or a watertightness
    verdict: with no trimesh, PLY and 3MF are refused rather than half-read.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "backends": backends_mod.probe().describe(),
        "natively_readable": ["stl", "obj"],
        "limits": {
            "max_bytes": DEFAULT_MAX_BYTES,
            "max_faces": DEFAULT_MAX_FACES,
            "max_vertices": DEFAULT_MAX_VERTICES,
        },
        "notes": [
            "Topology optimisation is out of scope for v1.",
            "Units are assumed, never declared: STL/OBJ carry no unit field.",
            "A non-watertight mesh is reported, never hole-filled. A bracket's "
            "through-hole is indistinguishable from a missing face by topology alone.",
        ],
    }


# ── registration (NOT applied) ────────────────────────────────────────────
#
# In llm_gatewayV9/main.py, alongside the other `app.include_router(...)` calls:
#
#     import parts.api as parts_api
#     app.include_router(parts_api.router)
#
# `parts` must be importable, which it is: llm_gatewayV9 is the working
# directory the gateway is started from, so it is already on sys.path.
# `parts.api` imports only fastapi and this package - it does not import main,
# so there is no circular import.