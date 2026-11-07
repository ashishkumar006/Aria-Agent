"""Parts service: gateway round trips, versions, and background jobs.

The gateway stays stateless. This module owns the part lifecycle around it:
import, measure, repair, optimise, cache viewer geometry, and persist each
result through `parts_store`. Both the HTTP routes and the DAG's local tools
call these functions, so a click and an agent plan produce the same versions
with the same provenance.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

import parts_store as store
from gateway import GATEWAY_URL, _gateway_token, async_session

MAX_UPLOAD_BYTES = 64 * 1024 * 1024
GEOMETRY_PREVIEW_FACES = 140_000
# Bump when the preview algorithm changes: compiled previews live beside the
# immutable STL versions, and a stale buffer would outlive the fix.
GEOMETRY_CACHE_VERSION = 2
_GW_TIMEOUT = 180.0


class PartsServiceError(RuntimeError):
    def __init__(self, message: str, status: int = 502, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


class DuplicatePartError(PartsServiceError):
    def __init__(self, existing: dict):
        super().__init__(
            f"this mesh is already in the library as '{existing.get('name')}'",
            status=409, existing=existing)
        self.existing = existing


def _headers() -> dict:
    try:
        token = _gateway_token()
    except Exception:
        token = ""
    return {"X-Gateway-Token": token} if token else {}


def _error_from(response: httpx.Response) -> PartsServiceError:
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = str(body.get("error") or body.get("detail")
                         or body.get("message") or "")
    except ValueError:
        detail = response.text[:240]
    code = response.status_code if response.status_code >= 400 else 502
    return PartsServiceError(detail or f"parts gateway HTTP {response.status_code}",
                             status=code)


async def _upload(path: str, blob: bytes, filename: str,
                  params: dict | None = None,
                  timeout: float = _GW_TIMEOUT) -> dict:
    if len(blob) > MAX_UPLOAD_BYTES:
        raise PartsServiceError(
            f"mesh is larger than the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit",
            status=413)
    try:
        with async_session(timeout) as c:
            # Auth is per request: the pooled client is shared across every
            # caller in the process, so a header set on it would outlive this
            # call (and a token set on it would ride along on someone else's).
            async def _send(h):
                return await c.post(
                    f"{GATEWAY_URL}{path}",
                    params={k: v for k, v in (params or {}).items()
                            if v is not None},
                    files={"file": (filename or "part.stl", blob,
                                    "application/octet-stream")},
                    headers=h,
                )

            r = await _send(_headers())
            if r.status_code == 401:
                # Re-read the token: the gateway regenerates it when its state
                # file is missing, and a long-lived agent would otherwise stay
                # broken until restart.
                r = await _send(_headers())
            if not r.is_success:
                raise _error_from(r)
            data = r.json()
            if not isinstance(data, dict):
                raise PartsServiceError("parts gateway returned a non-object body")
            return data
    except PartsServiceError:
        raise
    except Exception as exc:
        raise PartsServiceError(
            f"parts gateway unreachable: {type(exc).__name__}: {exc}") from None


async def _json(path: str, *, ttl_cache: dict | None = None,
                ttl: float = 0.0) -> dict:
    if ttl_cache is not None:
        cached = ttl_cache.get("value")
        if cached and time.time() - ttl_cache.get("at", 0) < ttl:
            return cached
    try:
        async with httpx.AsyncClient(timeout=30.0, headers=_headers()) as c:
            r = await c.get(f"{GATEWAY_URL}{path}")
            if r.status_code == 401:
                c.headers.update(_headers())
                r = await c.get(f"{GATEWAY_URL}{path}")
            if not r.is_success:
                raise _error_from(r)
            data = r.json()
            if not isinstance(data, dict):
                raise PartsServiceError("parts gateway returned a non-object body")
            if ttl_cache is not None:
                ttl_cache.update({"value": data, "at": time.time()})
            return data
    except PartsServiceError:
        raise
    except Exception as exc:
        raise PartsServiceError(
            f"parts gateway unreachable: {type(exc).__name__}: {exc}") from None


_CAP_CACHE: dict = {}


async def capabilities() -> dict:
    return await _json("/v1/parts/capabilities", ttl_cache=_CAP_CACHE, ttl=60.0)


async def passes() -> dict:
    return await _json("/v1/parts/optimise/passes")


async def metrics(blob: bytes, filename: str, *,
                  scale: float = 1.0) -> dict:
    """Measure the upload itself; no render buffers and no preview decimation."""
    return await _upload("/v1/parts/metrics", blob, filename, {"scale": scale})


async def geometry(blob: bytes, filename: str, *,
                   max_faces: int | None = None, scale: float = 1.0) -> dict:
    return await _upload("/v1/parts/geometry", blob, filename,
                         {"max_faces": max_faces, "scale": scale})


async def analyse(blob: bytes, filename: str, *,
                  repair: bool = False, slicer: bool = False,
                  scale: float = 1.0, weld_only: bool = True) -> dict:
    return await _upload("/v1/parts/analyse", blob, filename,
                         {"repair": str(bool(repair)).lower(),
                          "slicer": str(bool(slicer)).lower(),
                          "scale": scale,
                          "weld_only": str(bool(weld_only)).lower()},
                         timeout=300.0 if slicer else _GW_TIMEOUT)


async def repair(blob: bytes, filename: str, *,
                 watertight: bool = True, fix_normals: bool = True,
                 weld: bool = True, scale: float = 1.0) -> dict:
    return await _upload("/v1/parts/repair", blob, filename, {
        "watertight": str(bool(watertight)).lower(),
        "fix_normals": str(bool(fix_normals)).lower(),
        "weld": str(bool(weld)).lower(),
        "scale": scale,
    })


async def optimise(blob: bytes, filename: str, *, pass_name: str,
                   params: dict | None = None, scale: float = 1.0) -> dict:
    p = dict(params or {})
    query: dict[str, Any] = {"pass_name": pass_name, "scale": scale}
    for key in ("target_faces", "iterations", "strength", "overhang_angle_deg"):
        if p.get(key) is not None:
            query[key] = p[key]
    return await _upload("/v1/parts/optimise", blob, filename, query)


def _decode_stl(payload: dict) -> bytes | None:
    b64 = payload.get("result_stl_base64")
    if not b64:
        return None
    try:
        return base64.b64decode(b64)
    except Exception:
        return None


def _metrics_from_report(report: dict) -> dict:
    try:
        analysis = report.get("analysis") or {}
        counts = analysis.get("counts") or {}
        geometry = analysis.get("geometry") or {}
        bbox = geometry.get("bbox") or {}
        extents = bbox.get("extents") or []
        return {
            "vertices": int(counts.get("vertices") or 0),
            "faces": int(counts.get("triangles") or 0),
            "volume_mm3": geometry.get("volume") or 0,
            "surface_area_mm2": geometry.get("surface_area") or 0,
            "dimensions_mm": [round(float(x), 3) for x in extents],
            "bbox_min": [round(float(x), 3) for x in (bbox.get("min") or [])],
            "bbox_max": [round(float(x), 3) for x in (bbox.get("max") or [])],
            "min_edge_mm": 0,
            "median_edge_mm": 0,
            "overhang_faces": 0,
            "overhang_area_mm2": 0,
            "thin_regions": 0,
            "thinnest_wall_mm": 0,
        }
    except Exception:
        return {}


async def import_part(*, blob: bytes, filename: str, name: str,
                      description: str = "", tags: Any = None,
                      scale: float = 1.0, origin: str = "ui",
                      auto_repair: bool = True) -> dict:
    if not blob:
        raise PartsServiceError("the upload was empty", status=400)
    import hashlib
    digest = hashlib.sha256(blob).hexdigest()
    existing = store.find_by_sha(digest)
    if existing:
        raise DuplicatePartError(existing)
    try:
        measured, rep = await asyncio.gather(
            metrics(blob, filename, scale=scale),
            # When the user asked for an untouched import, measure it as
            # stored: the raw STL report is the honest one. Auto-repair
            # normalises the weld artefact before measuring.
            analyse(blob, filename, repair=False, scale=scale,
                    weld_only=bool(auto_repair)),
        )
    except PartsServiceError:
        raise
    metric_block = measured.get("metrics") or _metrics_from_report(rep)
    part = store.create_part(
        name=name or Path(filename).stem or "Part",
        description=description,
        filename=filename,
        blob=blob,
        metrics=metric_block,
        objective=measured.get("objective"),
        report=rep,
        origin=origin,
        tags=tags,
        sha256=digest,
        weld_only=bool(auto_repair),
    )
    if auto_repair:
        # Binary STL has no shared vertices, so a perfectly good export often
        # validates as open/inconsistent until it is welded. Auto-repairing on
        # import turns that artefact into a usable current version and keeps
        # the untouched upload as v1, so nothing is silently overwritten.
        try:
            repaired = await repair_version(
                part["id"], part["current_version"], origin=origin)
            part = repaired.get("part") or part
        except Exception:
            # A repair refusal is not an import failure: the raw upload is
            # still a real part and the report explains why it could not be
            # fixed. The UI offers Repair explicitly in that case.
            part = store.get_part(part["id"])
    # Warm the viewer LOD after the response, not before it: import should
    # return as soon as the version exists, and the first open should not pay
    # for a million-face preview a second time.
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(version_geometry(part["id"], part.get("current_version")))
    except RuntimeError:
        pass
    return part


async def analyse_version(part_id: str, version: str | None = None, *,
                          repair: bool = False, slicer: bool = False,
                          scale: float = 1.0,
                          weld_only: bool | None = None) -> dict:
    blob, v = store.read_version_bytes(part_id, version)
    # An untouched upload is measured as stored; a derived version is always
    # normalised (its binary STL would otherwise look open). The version
    # records which answer is correct.
    if weld_only is None:
        weld_only = bool(v.get("weld_only", True))
    rep = await analyse(blob, v.get("filename") or f"{v['id']}.stl",
                        repair=repair, slicer=slicer, scale=scale,
                        weld_only=weld_only)
    metrics = v.get("metrics") or _metrics_from_report(rep)
    return store.update_version(part_id, v["id"], report=rep, metrics=metrics)


async def repair_version(part_id: str, version: str | None = None, *,
                         watertight: bool = True, fix_normals: bool = True,
                         weld: bool = True, scale: float = 1.0,
                         origin: str = "ui") -> dict:
    blob, v = store.read_version_bytes(part_id, version)
    result = await repair(blob, v.get("filename") or f"{v['id']}.stl",
                          watertight=watertight, fix_normals=fix_normals,
                          weld=weld, scale=scale)
    new_blob = _decode_stl(result)
    if not new_blob:
        return {"part": store.get_part(part_id), "version": v,
                "result": result, "changed": False}
    rep = await analyse(new_blob, "repaired.stl", repair=False, scale=scale,
                        weld_only=True)
    metrics = result.get("metrics_after") or _metrics_from_report(rep)
    part = store.add_version(
        part_id, blob=new_blob, filename="repaired.stl", op="repair",
        params={"watertight": watertight, "fix_normals": fix_normals,
                "weld": weld},
        metrics=metrics, objective=result.get("objective_after"),
        report=rep, parent=v["id"], kept=True,
        note=" / ".join(str(s.get("name")) for s in result.get("steps") or []
                        if s.get("status") == "applied")[:300] or "repair",
        origin=origin,
    )
    new_v = max(
        (x for x in part["versions"]
         if x.get("id") != v["id"] and x.get("op") == "repair"
         and x.get("parent") == v["id"]),
        key=lambda x: x.get("created") or 0, default=None)
    return {"part": part, "version": new_v or part.get("current"),
            "result": result, "changed": True}


async def optimise_version(part_id: str, version: str | None = None, *,
                           pass_name: str, params: dict | None = None,
                           origin: str = "ui") -> dict:
    blob, v = store.read_version_bytes(part_id, version)
    result = await optimise(blob, v.get("filename") or f"{v['id']}.stl",
                            pass_name=pass_name, params=params)
    new_blob = _decode_stl(result)
    before = result.get("objective_before")
    after = result.get("objective_after")
    improved = bool(result.get("improved")) or (
        isinstance(before, (int, float)) and isinstance(after, (int, float))
        and after < before)
    if not new_blob:
        return {"part": store.get_part(part_id), "version": v,
                "result": result, "changed": False, "improved": False}
    metrics = result.get("metrics") or (result.get("after") or {})
    part = store.add_version(
        part_id, blob=new_blob, filename=f"{pass_name}.stl", op=pass_name,
        params=params or {}, metrics=metrics, objective=after,
        report=None, parent=v["id"], kept=improved,
        note=("kept" if improved else "reverted: objective got worse")[:300],
        origin=origin,
    )
    new_v = max(
        (x for x in part["versions"] if x.get("parent") == v["id"]
         and x.get("op") == pass_name),
        key=lambda x: x.get("created") or 0, default=None)
    if improved and new_v:
        # A kept result should arrive with a real verdict, not "unknown".
        # The weld-normalised analysis does not change the saved bytes; it
        # just measures the format-normalised mesh.
        try:
            part = await analyse_version(part_id, new_v["id"], weld_only=True)
            new_v = next((x for x in part["versions"]
                          if x.get("id") == new_v["id"]), new_v)
        except PartsServiceError:
            pass
    return {"part": part, "version": new_v, "result": result,
            "changed": True, "improved": improved}


# ── viewer geometry cache ───────────────────────────────────────────────────

async def version_geometry(part_id: str, version: str | None = None, *,
                           max_faces: int = GEOMETRY_PREVIEW_FACES) -> dict:
    blob, v = store.read_version_bytes(part_id, version)
    cache = (store._part_dir(part_id)
             / f"{v['id']}.geom.v{GEOMETRY_CACHE_VERSION}.{int(max_faces)}.json")
    cached = store._read_json(cache, None)
    if isinstance(cached, dict) and cached.get("geometry"):
        return cached
    data = await geometry(blob, v.get("filename") or f"{v['id']}.stl",
                         max_faces=max_faces)
    try:
        store._write_json(cache, data)
    except OSError:
        pass
    return data


# ── background optimisation jobs ────────────────────────────────────────────

_JOBS: dict[str, dict] = {}
_TASKS: dict[str, asyncio.Task] = {}


def _job_public(job: dict) -> dict:
    return {k: v for k, v in job.items() if k != "task"}


def list_jobs(part_id: str | None = None) -> list[dict]:
    rows = [j for j in _JOBS.values() if not part_id or j.get("part_id") == part_id]
    rows.sort(key=lambda j: j.get("created") or 0, reverse=True)
    return [_job_public(j) for j in rows[:30]]


def get_job(job_id: str) -> dict:
    job = _JOBS.get(job_id)
    if not job:
        raise PartsServiceError("unknown job", status=404)
    return _job_public(job)


def cancel_job(job_id: str) -> dict:
    job = _JOBS.get(job_id)
    if not job:
        raise PartsServiceError("unknown job", status=404)
    job["cancel"] = True
    if job["status"] in ("queued", "running"):
        job["status"] = "cancelling"
    job["updated"] = time.time()
    return _job_public(job)


def _append_log(job: dict, text: str) -> None:
    job.setdefault("log", []).append(str(text)[:500])
    job["log"] = job["log"][-80:]
    job["updated"] = time.time()


async def _run_loop(job: dict) -> None:
    job["status"] = "running"
    current_version = job["version"]
    try:
        for step in range(1, int(job.get("max_steps") or 6) + 1):
            if job.get("cancel"):
                job["status"] = "cancelled"
                _append_log(job, "stopped before the next step")
                break
            job["step"] = step
            blob, v = store.read_version_bytes(job["part_id"], current_version)
            result = await optimise(
                blob, v.get("filename") or f"{v['id']}.stl",
                pass_name=job["pass"], params=job.get("params") or {})
            before = result.get("objective_before")
            after = result.get("objective_after")
            improved = bool(result.get("improved")) or (
                isinstance(before, (int, float)) and isinstance(after, (int, float))
                and after < before)
            new_blob = _decode_stl(result)
            if not new_blob:
                _append_log(job, f"step {step}: {result.get('note') or 'no change'}")
                job["status"] = "done"
                job["note"] = "no further change"
                break
            metrics = result.get("metrics") or (result.get("after") or {})
            part = store.add_version(
                job["part_id"], blob=new_blob, filename=f"{job['pass']}.stl",
                op=job["pass"], params=job.get("params") or {},
                metrics=metrics, objective=after, report=None,
                parent=current_version, kept=improved,
                note=("kept" if improved else "reverted: objective got worse"),
                origin=job.get("origin") or "ui",
            )
            created = next((x for x in part["versions"]
                            if x.get("parent") == current_version
                            and x.get("op") == job["pass"]), None)
            if created:
                job.setdefault("versions", []).append(created["id"])
            _append_log(job, f"step {step}: {job['pass']} "
                             f"{before} → {after} "
                             f"({'kept' if improved else 'reverted'})")
            job["current_version"] = part.get("current_version")
            if not improved:
                job["status"] = "done"
                job["note"] = "objective stopped improving"
                break
            current_version = created["id"] if created else part["current_version"]
            job["version"] = current_version
        else:
            job["status"] = "done"
            job["note"] = "reached the step limit"
        if job["status"] in ("done", "cancelled"):
            try:
                await analyse_version(job["part_id"], job.get("current_version"))
            except Exception as exc:
                _append_log(job, f"final analysis failed: {type(exc).__name__}: {exc}")
    except PartsServiceError as exc:
        job["status"] = "error"
        job["error"] = str(exc)
        _append_log(job, f"error: {exc}")
    except Exception as exc:
        job["status"] = "error"
        job["error"] = f"{type(exc).__name__}: {exc}"
        _append_log(job, f"error: {job['error']}")
    finally:
        job["updated"] = time.time()
        job["finished"] = time.time()
        _TASKS.pop(job["id"], None)


def start_loop(part_id: str, version: str | None = None, *,
               pass_name: str, params: dict | None = None,
               max_steps: int = 6, origin: str = "ui") -> dict:
    store.get_part(part_id)
    v = store.get_version(part_id, version)
    steps = max(1, min(int(max_steps or 6), 12))
    active = [j for j in _JOBS.values()
              if j.get("part_id") == part_id
              and j.get("status") in ("queued", "running", "cancelling")]
    if active:
        raise PartsServiceError("this part already has a running job", status=409)
    job = {
        "id": f"job-{uuid.uuid4().hex[:10]}",
        "part_id": part_id,
        "version": v["id"],
        "base_version": v["id"],
        "current_version": v["id"],
        "pass": pass_name,
        "params": params or {},
        "max_steps": steps,
        "step": 0,
        "status": "queued",
        "log": [],
        "versions": [],
        "error": None,
        "note": "",
        "origin": origin,
        "created": time.time(),
        "updated": time.time(),
    }
    _JOBS[job["id"]] = job
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        job["status"] = "error"
        job["error"] = "no event loop for the parts job"
        return _job_public(job)
    _TASKS[job["id"]] = loop.create_task(_run_loop(job))
    return _job_public(job)


def export_version(part_id: str, version: str | None = None) -> tuple[bytes, dict]:
    return store.read_version_bytes(part_id, version)


def public_pass_list(payload: dict) -> list[dict]:
    rows = payload.get("passes") if isinstance(payload, dict) else None
    return rows if isinstance(rows, list) else []
