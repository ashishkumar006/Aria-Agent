"""Persistent parts library for the Parts section.

A part is a named mesh with immutable versions. Every upload, repair and
optimisation pass writes a new version; the part's `current_version` pointer
selects what the viewer opens. Mesh bytes live beside the metadata as opaque
files, never inside JSON, so a 2M-face upload does not turn the library index
into a multi-megabyte read on every sidebar render.

The store is deliberately agent-side. The gateway's parts service is
stateless (v1: caller holds the file), and keeping that contract means every
derived version is explicitly written by the agent after a measured round
trip. The DAG tools and the HTTP routes share this module, so there is one
writer and one version history.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
STATE_DIR = Path(os.environ.get("S9_STATE_DIR") or (ROOT / "state"))
PARTS_DIR = STATE_DIR / "parts"

MAX_PARTS = 100
MAX_VERSIONS = 30
MAX_NAME = 80
MAX_DESCRIPTION = 2000
MAX_TAGS = 8
MAX_TAG = 24
MAX_REPORT_JSON = 256 * 1024

_ID_RE = re.compile(r"^part-[a-z0-9][a-z0-9-]{3,48}$")
_VERSION_RE = re.compile(r"^v[0-9]{1,4}$")
_FORMATS = {"stl", "obj", "ply", "3mf", "off", "glb", "gltf", "dae", "amf"}

_lock = threading.RLock()


class PartsStoreError(ValueError):
    """A request the library refuses (bad id, cap reached, unknown version)."""


def _now() -> float:
    return time.time()


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _part_dir(part_id: str) -> Path:
    return PARTS_DIR / part_id


def _meta_path(part_id: str) -> Path:
    return _part_dir(part_id) / "meta.json"


def _safe_id(part_id: str) -> str:
    value = (part_id or "").strip().lower()
    if not _ID_RE.match(value):
        raise PartsStoreError("invalid part id")
    return value


def _safe_version(version: str) -> str:
    value = (version or "").strip().lower()
    if not _VERSION_RE.match(value):
        raise PartsStoreError("invalid version id")
    return value


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return (slug[:28] or "part").strip("-")


def _new_id(name: str) -> str:
    # The random suffix is the identity; the slug only makes the id readable
    # in a URL and in logs. Renaming a part therefore never changes its id.
    return f"part-{_slug(name)}-{uuid.uuid4().hex[:6]}"


def _load(part_id: str) -> dict:
    part_id = _safe_id(part_id)
    meta = _read_json(_meta_path(part_id), None)
    if not isinstance(meta, dict) or meta.get("id") != part_id:
        raise PartsStoreError(f"unknown part '{part_id}'")
    return meta


def _save(meta: dict) -> dict:
    meta["updated"] = _now()
    meta["schema"] = 1
    _write_json(_meta_path(meta["id"]), meta)
    return meta


def _clean_name(name: str) -> str:
    value = " ".join(str(name or "").split())
    if not value:
        raise PartsStoreError("name is required")
    if len(value) > MAX_NAME:
        raise PartsStoreError(f"name is longer than {MAX_NAME} characters")
    return value


def _clean_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if not isinstance(tags, list):
        raise PartsStoreError("tags must be a list")
    out: list[str] = []
    for raw in tags[:MAX_TAGS]:
        value = " ".join(str(raw).split())[:MAX_TAG]
        if value and value not in out:
            out.append(value)
    return out


def _clean_format(filename: str) -> str:
    ext = Path(str(filename or "")).suffix.lower().lstrip(".")
    if ext not in _FORMATS:
        allowed = ", ".join(sorted(_FORMATS))
        raise PartsStoreError(f"unsupported mesh extension; expected one of {allowed}")
    return ext


def _version_path(meta: dict, version: dict) -> Path:
    return _part_dir(meta["id"]) / version["file"]


def _version_by_id(meta: dict, version: str | None) -> dict:
    wanted = _safe_version(version) if version else str(meta.get("current_version") or "")
    for v in meta.get("versions", []):
        if v.get("id") == wanted:
            return v
    raise PartsStoreError(f"unknown version '{wanted}'")


def _summary(meta: dict) -> dict:
    current = None
    try:
        current = _version_by_id(meta, meta.get("current_version"))
    except PartsStoreError:
        pass
    current = current or (meta.get("versions") or [{}])[-1]
    return {
        "id": meta["id"],
        "name": meta.get("name") or "Untitled part",
        "description": meta.get("description") or "",
        "tags": meta.get("tags") or [],
        "created": meta.get("created"),
        "updated": meta.get("updated"),
        "source": meta.get("source") or {},
        "current_version": meta.get("current_version"),
        "versions": len(meta.get("versions") or []),
        "faces": (current.get("metrics") or {}).get("faces") or 0,
        "vertices": (current.get("metrics") or {}).get("vertices") or 0,
        "objective": current.get("objective"),
        "verdict": current.get("verdict") or {},
        "op": current.get("op"),
        "kept": current.get("kept", True),
        "bytes": current.get("bytes") or 0,
        "format": current.get("format") or "",
        "view": meta.get("view") or {},
    }


def _report_summary(report: Any) -> tuple[dict, list, dict]:
    """Return (verdict, issues, report) with a size ceiling.

    A report is useful history, but it is not worth letting one pathological
    validation dump grow the metadata file without bound. The summary fields
    survive even when the full report is dropped.
    """
    if not isinstance(report, dict):
        return {}, [], {}
    verdict = report.get("verdict") or {}
    issues = report.get("issues") or []
    if not isinstance(verdict, dict):
        verdict = {}
    if not isinstance(issues, list):
        issues = []
    try:
        encoded = json.dumps(report, ensure_ascii=False)
    except (TypeError, ValueError):
        return verdict, issues, {}
    if len(encoded.encode("utf-8")) > MAX_REPORT_JSON:
        return verdict, issues[:50], {}
    return verdict, issues[:100], report


def list_parts() -> list[dict]:
    if not PARTS_DIR.exists():
        return []
    out: list[dict] = []
    for d in PARTS_DIR.iterdir():
        if not d.is_dir() or not _ID_RE.match(d.name):
            continue
        meta = _read_json(d / "meta.json", None)
        if isinstance(meta, dict) and meta.get("id") == d.name:
            out.append(_summary(meta))
    out.sort(key=lambda p: p.get("updated") or 0, reverse=True)
    return out


def get_part(part_id: str) -> dict:
    """Full detail: metadata, versions, current report and view."""
    meta = _load(part_id)
    versions = []
    for v in meta.get("versions") or []:
        if not isinstance(v, dict):
            continue
        versions.append({k: val for k, val in v.items() if k != "report"})
        # The full report belongs to the version's own endpoint, not to every
        # sidebar read. Keep the summary inline; `report` is added when the
        # caller asks for the current version below.
    current = None
    try:
        current = dict(_version_by_id(meta, meta.get("current_version")))
    except PartsStoreError:
        pass
    return {
        **_summary(meta),
        "versions": versions,
        "current": current,
        "report": (current or {}).get("report") or {},
        "issues": (current or {}).get("issues") or [],
        "view": meta.get("view") or {},
        "source": meta.get("source") or {},
    }


def get_version(part_id: str, version: str | None = None) -> dict:
    meta = _load(part_id)
    return dict(_version_by_id(meta, version))


def read_version_bytes(part_id: str, version: str | None = None) -> tuple[bytes, dict]:
    meta = _load(part_id)
    v = _version_by_id(meta, version)
    path = _version_path(meta, v)
    try:
        blob = path.read_bytes()
    except OSError as exc:
        raise PartsStoreError(f"version file is missing: {exc}") from None
    return blob, v


def find_by_sha(sha256: str) -> dict | None:
    wanted = (sha256 or "").lower()
    if not wanted:
        return None
    for summary in list_parts():
        meta = _read_json(_meta_path(summary["id"]), None)
        if isinstance(meta, dict) and str((meta.get("source") or {}).get("sha256") or "").lower() == wanted:
            return _summary(meta)
    return None


def create_part(*, name: str, description: str, filename: str,
                blob: bytes, metrics: dict, objective: float | None,
                report: dict | None, origin: str = "ui",
                tags: Any = None,
                sha256: str | None = None,
                weld_only: bool = True) -> dict:
    if not blob:
        raise PartsStoreError("the uploaded mesh is empty")
    fmt = _clean_format(filename)
    clean_name = _clean_name(name or Path(filename).stem)
    clean_desc = str(description or "")[:MAX_DESCRIPTION]
    digest = sha256 or hashlib.sha256(blob).hexdigest()
    with _lock:
        if len(list_parts()) >= MAX_PARTS:
            raise PartsStoreError(f"part limit is {MAX_PARTS}; delete one first")
        part_id = _new_id(clean_name)
        while _meta_path(part_id).exists():
            part_id = _new_id(clean_name)
        now = _now()
        meta = {
            "schema": 1,
            "id": part_id,
            "name": clean_name,
            "description": clean_desc,
            "tags": _clean_tags(tags),
            "created": now,
            "updated": now,
            "source": {
                "filename": Path(str(filename or "part.stl")).name[:160],
                "format": fmt,
                "bytes": len(blob),
                "sha256": digest,
                "uploaded_at": now,
            },
            "current_version": "v1",
            "next_version": 2,
            "versions": [],
            "view": {"preset": "iso", "mode": "shaded", "overlay": "none"},
            "archived": False,
        }
        path = _part_dir(part_id) / f"v1.{fmt}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        verdict, issues, stored_report = _report_summary(report)
        meta["versions"].append({
            "id": "v1",
            "parent": None,
            "op": "upload",
            "params": {},
            "created": now,
            "file": path.name,
            "filename": meta["source"]["filename"],
            "format": fmt,
            "bytes": len(blob),
            "metrics": metrics or {},
            "objective": objective,
            "verdict": verdict,
            "issues": issues,
            "report": stored_report,
            "kept": True,
            "note": "uploaded",
            "origin": origin,
            "weld_only": bool(weld_only),
        })
        _save(meta)
        return get_part(part_id)


def add_version(part_id: str, *, blob: bytes, filename: str,
                op: str, params: dict | None, metrics: dict,
                objective: float | None, report: dict | None,
                parent: str | None, kept: bool, note: str,
                origin: str = "ui", weld_only: bool = True) -> dict:
    if not blob:
        raise PartsStoreError("a derived version needs mesh bytes")
    fmt = _clean_format(filename)
    with _lock:
        meta = _load(part_id)
        parent = _safe_version(parent) if parent else str(meta.get("current_version") or "v1")
        _version_by_id(meta, parent)
        number = int(meta.get("next_version") or (len(meta.get("versions") or []) + 1))
        version = f"v{number}"
        meta["next_version"] = number + 1
        path = _part_dir(part_id) / f"{version}.{fmt}"
        path.write_bytes(blob)
        verdict, issues, stored_report = _report_summary(report)
        meta.setdefault("versions", []).append({
            "id": version,
            "parent": parent,
            "op": str(op or "edit")[:40],
            "params": params if isinstance(params, dict) else {},
            "created": _now(),
            "file": path.name,
            "filename": Path(str(filename or f"{version}.stl")).name[:160],
            "format": fmt,
            "bytes": len(blob),
            "metrics": metrics or {},
            "objective": objective,
            "verdict": verdict,
            "issues": issues,
            "report": stored_report,
            "kept": bool(kept),
            "note": str(note or "")[:500],
            "origin": origin,
            "weld_only": bool(weld_only),
        })
        if kept:
            meta["current_version"] = version
        _trim_versions(meta)
        _save(meta)
        return get_part(part_id)


def _trim_versions(meta: dict) -> None:
    versions = meta.get("versions") or []
    if len(versions) <= MAX_VERSIONS:
        return
    current = str(meta.get("current_version") or "")
    keep = versions[-MAX_VERSIONS:]
    # The current version must survive even if it is older than the window.
    if current and all(v.get("id") != current for v in keep):
        current_v = next((v for v in versions if v.get("id") == current), None)
        if current_v is not None:
            keep = [current_v, *keep[-(MAX_VERSIONS - 1):]]
    dropped = [v for v in versions if v not in keep]
    for v in dropped:
        try:
            (_part_dir(meta["id"]) / v.get("file", "")).unlink()
        except OSError:
            pass
        for p in _part_dir(meta["id"]).glob(f"{v.get('id')}.geom.*"):
            try:
                p.unlink()
            except OSError:
                pass
    meta["versions"] = keep


def update_version(part_id: str, version: str, *,
                   metrics: dict | None = None,
                   objective: float | None = None,
                   report: dict | None = None,
                   note: str | None = None) -> dict:
    with _lock:
        meta = _load(part_id)
        v = _version_by_id(meta, version)
        if metrics is not None:
            v["metrics"] = metrics
        if objective is not None:
            v["objective"] = objective
        if report is not None:
            verdict, issues, stored = _report_summary(report)
            v["verdict"] = verdict
            v["issues"] = issues
            v["report"] = stored
        if note is not None:
            v["note"] = str(note)[:500]
        _save(meta)
        return get_part(part_id)


def set_current(part_id: str, version: str) -> dict:
    with _lock:
        meta = _load(part_id)
        v = _version_by_id(meta, version)
        meta["current_version"] = v["id"]
        v["kept"] = True
        _save(meta)
        return get_part(part_id)


def update_meta(part_id: str, *, name: str | None = None,
                description: str | None = None,
                tags: Any = None) -> dict:
    with _lock:
        meta = _load(part_id)
        if name is not None:
            meta["name"] = _clean_name(name)
        if description is not None:
            meta["description"] = str(description or "")[:MAX_DESCRIPTION]
        if tags is not None:
            meta["tags"] = _clean_tags(tags)
        _save(meta)
        return get_part(part_id)


def set_view(part_id: str, view: dict) -> dict:
    allowed_presets = {"iso", "front", "back", "left", "right", "top", "bottom", "fit"}
    allowed_modes = {"shaded", "wireframe"}
    allowed_overlays = {"none", "overhang", "thin_walls", "issues"}
    if not isinstance(view, dict):
        raise PartsStoreError("view must be an object")
    clean = {
        "preset": str(view.get("preset") or "iso").lower(),
        "mode": str(view.get("mode") or "shaded").lower(),
        "overlay": str(view.get("overlay") or "none").lower(),
        "compare": view.get("compare") if isinstance(view.get("compare"), dict) else None,
    }
    if clean["preset"] not in allowed_presets:
        clean["preset"] = "iso"
    if clean["mode"] not in allowed_modes:
        clean["mode"] = "shaded"
    if clean["overlay"] not in allowed_overlays:
        clean["overlay"] = "none"
    with _lock:
        meta = _load(part_id)
        meta["view"] = clean
        _save(meta)
        return get_part(part_id)


def delete_part(part_id: str) -> bool:
    with _lock:
        part_id = _safe_id(part_id)
        path = _part_dir(part_id)
        if not path.exists():
            return False
        shutil.rmtree(path, ignore_errors=True)
        return True


def duplicate_part(part_id: str, name: str | None = None) -> dict:
    with _lock:
        meta = _load(part_id)
        if len(list_parts()) >= MAX_PARTS:
            raise PartsStoreError(f"part limit is {MAX_PARTS}; delete one first")
        new_id = _new_id(name or f"{meta.get('name')} copy")
        while _meta_path(new_id).exists():
            new_id = _new_id(name or f"{meta.get('name')} copy")
        src = _part_dir(part_id)
        dst = _part_dir(new_id)
        shutil.copytree(src, dst)
        # The copied meta.json still says the old id. Read it directly:
        # `_load` correctly refuses a file whose id does not match its path,
        # and the rewrite below is exactly what makes the new path valid.
        copied = _read_json(_meta_path(new_id), None)
        if not isinstance(copied, dict):
            raise PartsStoreError("the copied part metadata is unreadable")
        copied["id"] = new_id
        copied["name"] = _clean_name(name or f"{meta.get('name')} copy")
        copied["created"] = copied["updated"] = _now()
        _write_json(_meta_path(new_id), copied)
        return get_part(new_id)


def stats() -> dict:
    parts = list_parts()
    total_bytes = 0
    total_versions = 0
    for p in parts:
        meta = _read_json(_meta_path(p["id"]), {}) or {}
        total_versions += len(meta.get("versions") or [])
        for v in meta.get("versions") or []:
            total_bytes += int(v.get("bytes") or 0)
    return {
        "parts": len(parts),
        "versions": total_versions,
        "bytes": total_bytes,
        "max_parts": MAX_PARTS,
        "max_versions": MAX_VERSIONS,
    }


def brief(part_id: str, version: str | None = None) -> str:
    """A compact, model-facing description of one part and version.

    The DAG planner and section skills get this instead of raw geometry. It is
    deliberately a few lines: the tools can fetch the detailed report, but the
    planner only needs to know which part it is being asked about.
    """
    meta = _load(part_id)
    v = _version_by_id(meta, version)
    m = v.get("metrics") or {}
    verdict = v.get("verdict") or {}
    return (
        f"part id: {meta['id']}\n"
        f"name: {meta.get('name')}\n"
        f"current version: {meta.get('current_version')} "
        f"(requested: {v.get('id')}, operation: {v.get('op')})\n"
        f"source: {meta.get('source', {}).get('filename')} "
        f"({meta.get('source', {}).get('format')})\n"
        f"faces: {m.get('faces')}, vertices: {m.get('vertices')}\n"
        f"dimensions mm: {m.get('dimensions_mm')}\n"
        f"volume mm3: {m.get('volume_mm3')}, objective: {v.get('objective')}\n"
        f"verdict: {verdict.get('status')} "
        f"(errors {verdict.get('errors', 0)}, warnings {verdict.get('warnings', 0)}, "
        f"printable {verdict.get('printable')})\n"
        f"versions: {', '.join(x.get('id', '') for x in meta.get('versions') or [])}"
    )
