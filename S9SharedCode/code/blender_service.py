"""Parts <-> Blender lifecycle.

Blender is a separate process. This module owns three things:

* a persistent headless worker for versioned file operations;
* a live GUI session that can be placed over the Parts workspace;
* the sync step that turns a Blender save into an immutable part version.

The bridge itself is `blender_bridge/addon.py` inside Blender. Nothing here
imports bpy; every call goes through the token-protected loopback client.
"""
from __future__ import annotations

import asyncio
import atexit
import ctypes
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any

import parts_service
import parts_store as store
from blender_bridge.client import BlenderBridgeError, BlenderClient

ROOT = Path(__file__).parent
BLENDER_ROOT = Path(os.environ.get("S9_STATE_DIR") or (ROOT / "state")) / "blender"
BRIDGE_DIR = ROOT / "blender_bridge"
BOOTSTRAP = BRIDGE_DIR / "bootstrap.py"


class BlenderServiceError(RuntimeError):
    pass


def blender_executable() -> str | None:
    explicit = os.environ.get("ARIA_BLENDER_EXE")
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which("blender") or shutil.which("Blender")
    if found:
        return found
    candidates: list[Path] = []
    for base in (Path(r"C:\Program Files\Blender Foundation"),
                 Path(r"C:\Program Files (x86)\Blender Foundation")):
        if base.exists():
            candidates.extend(base.glob("Blender */blender.exe"))
    candidates.sort(reverse=True)
    return str(candidates[0]) if candidates else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_ping(client: BlenderClient, timeout: float = 30.0) -> dict:
    end = time.time() + timeout
    last: Exception | None = None
    while time.time() < end:
        try:
            return client.ping(timeout=2.0)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.25)
    raise BlenderServiceError(
        f"Blender did not become ready: {last or 'timeout'}")


# ── Windows window placement (not a web embed) ──────────────────────────────

def _find_window(pid: int) -> int:
    if os.name != "nt":
        return 0
    user32 = ctypes.windll.user32
    result: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
                if "Blender" in title and "splash" not in title.lower():
                    result.append(int(hwnd))
                    return False
        return True

    user32.EnumWindows(cb, 0)
    return result[0] if result else 0


def _place_window(pid: int, bounds: dict | None, *, topmost: bool = True) -> dict:
    if os.name != "nt":
        return {"supported": False}
    hwnd = _find_window(pid)
    if not hwnd:
        return {"supported": True, "found": False}
    user32 = ctypes.windll.user32
    if bounds:
        x = int(bounds.get("x", 0))
        y = int(bounds.get("y", 0))
        w = max(320, int(bounds.get("width", 1000)))
        h = max(240, int(bounds.get("height", 700)))
        flags = 0x0040 | 0x0004  # SWP_SHOWWINDOW | SWP_NOACTIVATE
        if topmost:
            user32.SetWindowPos(hwnd, -1, x, y, w, h, flags)
        else:
            user32.SetWindowPos(hwnd, 0, x, y, w, h, flags)
    elif topmost:
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004)
    return {"supported": True, "found": True, "hwnd": hwnd}


def _show_window(pid: int, show: bool) -> dict:
    if os.name != "nt":
        return {"supported": False}
    hwnd = _find_window(pid)
    if not hwnd:
        return {"supported": True, "found": False}
    ctypes.windll.user32.ShowWindow(hwnd, 5 if show else 0)  # SW_SHOW / SW_HIDE
    if show:
        ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004)
    return {"supported": True, "found": True, "visible": show}


# ── persistent headless worker ──────────────────────────────────────────────

class _Worker:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.token = ""
        self.workspace = BLENDER_ROOT / "worker"
        self.client: BlenderClient | None = None
        self.lock = threading.RLock()

    def ensure(self) -> BlenderClient:
        with self.lock:
            if self.client is not None:
                try:
                    self.client.ping(timeout=2.0)
                    return self.client
                except Exception:
                    self.stop()
            exe = blender_executable()
            if not exe:
                raise BlenderServiceError("Blender executable was not found")
            self.workspace.mkdir(parents=True, exist_ok=True)
            self.port = _free_port()
            self.token = uuid.uuid4().hex
            worker_args = [exe, "--background", "--factory-startup",
                           "--python", str(BOOTSTRAP), "--", "--headless",
                           "--port", str(self.port), "--token", self.token,
                           "--workspace", str(self.workspace)]
            if os.environ.get("ARIA_BLENDER_ALLOW_PYTHON") == "1":
                worker_args.append("--allow-python")
            self.proc = subprocess.Popen(
                worker_args,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL)
            self.client = BlenderClient(port=self.port, token=self.token,
                                        timeout=300.0)
            _wait_ping(self.client, timeout=30.0)
            return self.client

    def stop(self) -> None:
        with self.lock:
            if self.client is not None:
                try:
                    self.client.call("shutdown", timeout=4.0)
                except Exception:
                    pass
            if self.proc is not None and self.proc.poll() is None:
                try:
                    self.proc.wait(timeout=6.0)
                except Exception:
                    self.proc.kill()
            self.client = None
            self.proc = None

    def call(self, cmd: str, args: dict | None = None,
             timeout: float = 300.0) -> dict:
        with self.lock:
            client = self.ensure()
            return client.call(cmd, args, timeout=timeout)

    def job_dir(self, name: str) -> Path:
        path = self.workspace / name
        path.mkdir(parents=True, exist_ok=True)
        return path


WORKER = _Worker()
atexit.register(WORKER.stop)


# ── live GUI session ────────────────────────────────────────────────────────

class _Live:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.token = ""
        self.part_id = ""
        self.version = ""
        self.workspace: Path | None = None
        self.client: BlenderClient | None = None
        self.lock = threading.RLock()

    def status(self) -> dict:
        alive = bool(self.proc and self.proc.poll() is None)
        if alive and self.client is not None:
            try:
                info = self.client.ping(timeout=2.0)
                return {"live": True, "part_id": self.part_id,
                        "version": self.version, "pid": self.proc.pid,
                        "port": self.port, "info": info}
            except Exception:
                pass
        return {"live": False, "part_id": self.part_id,
                "version": self.version}

    def stop(self) -> None:
        with self.lock:
            if self.client is not None:
                try:
                    self.client.call("shutdown", timeout=3.0)
                except Exception:
                    pass
            if self.proc is not None and self.proc.poll() is None:
                try:
                    self.proc.wait(timeout=5.0)
                except Exception:
                    self.proc.kill()
            self.client = None
            self.proc = None
            self.part_id = ""
            self.version = ""

    def open(self, part_id: str, version: str, blob: bytes,
             filename: str, bounds: dict | None) -> dict:
        exe = blender_executable()
        if not exe:
            raise BlenderServiceError("Blender executable was not found")
        self.stop()
        work = BLENDER_ROOT / "parts" / part_id
        work.mkdir(parents=True, exist_ok=True)
        input_path = work / "input.stl"
        input_path.write_bytes(blob)
        self.port = _free_port()
        self.token = uuid.uuid4().hex
        self.workspace = work
        args = [exe]
        if bounds:
            args += ["--window-geometry",
                     str(int(bounds.get("x", 0))), str(int(bounds.get("y", 0))),
                     str(max(320, int(bounds.get("width", 1000)))),
                     str(max(240, int(bounds.get("height", 700))))]
        args += ["--python", str(BOOTSTRAP), "--",
                 "--port", str(self.port), "--token", self.token,
                 "--workspace", str(work)]
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self.client = BlenderClient(port=self.port, token=self.token,
                                    timeout=300.0)
        _wait_ping(self.client, timeout=45.0)
        self.client.call("import", {"path": str(input_path), "format": "stl",
                                    "clear": True})
        self.client.call("save", {"path": str(work / "working.blend")})
        self.part_id = part_id
        self.version = version
        self._placed = _place_window(self.proc.pid, bounds, topmost=True)
        return {"live": True, "part_id": part_id, "version": version,
                "pid": self.proc.pid, "port": self.port,
                "window": self._placed,
                "scene": self.client.call("scene")}


LIVE = _Live()
atexit.register(LIVE.stop)


# ── public service ──────────────────────────────────────────────────────────

def status() -> dict:
    exe = blender_executable()
    worker = {"alive": False}
    if WORKER.client is not None:
        try:
            worker = {"alive": True, "info": WORKER.client.ping(timeout=2.0)}
        except Exception:
            worker = {"alive": False}
    return {"available": bool(exe), "executable": exe,
            "live": LIVE.status(), "worker": worker,
            "root": str(BLENDER_ROOT)}


def catalog(query: str = "", limit: int = 40) -> dict:
    return WORKER.call("catalog", {"query": query, "limit": limit})


def _blob_for(part_id: str, version: str | None) -> tuple[bytes, dict, dict]:
    blob, v = store.read_version_bytes(part_id, version)
    part = store.get_part(part_id)
    return blob, v, part


def _write_input(job: Path, blob: bytes, filename: str) -> Path:
    path = job / (filename or "input.stl")
    path.write_bytes(blob)
    return path


def run_mesh_op(part_id: str, version: str | None, op: str,
                params: dict | None = None, *, active: str = "",
                mode: str = "") -> dict:
    """Apply one Blender operator headlessly and save the result as a version."""
    blob, v, _part = _blob_for(part_id, version)
    job = WORKER.job_dir(f"op-{uuid.uuid4().hex[:10]}")
    src = _write_input(job, blob, "input.stl")
    out = job / "result.stl"
    WORKER.call("import", {"path": str(src), "format": "stl", "clear": True})
    scene = WORKER.call("scene")
    meshes = [o for o in scene.get("objects", []) if o.get("type") == "MESH"]
    active_name = active or (meshes[0]["name"] if meshes else "")
    result = WORKER.call("op", {"name": op, "params": params or {},
                                "active": active_name, "mode": mode})
    # An operator that only adds a modifier has not changed the mesh yet.
    # Applying the stack is what makes "bevel/remesh/solidify" produce a real
    # version instead of an unchanged export.
    WORKER.call("apply_modifiers", {})
    WORKER.call("export", {"path": str(out), "format": "stl",
                           "selected": False})
    WORKER.call("checkpoint", {"path": str(job / "result.blend")})
    data = out.read_bytes()
    new_part = _commit(part_id, v["id"], data, f"{op}.stl", op,
                       {"operator": op, "params": params or {}},
                       f"Blender {op}", "blender")
    return {"part": new_part, "result": result, "job": str(job)}


def render(part_id: str, version: str | None, *,
           engine: str = "BLENDER_EEVEE", width: int = 1024,
           height: int = 1024, samples: int = 32,
           transparent: bool = False) -> dict:
    blob, _v, _part = _blob_for(part_id, version)
    job = WORKER.job_dir(f"render-{uuid.uuid4().hex[:10]}")
    src = _write_input(job, blob, "input.stl")
    out = job / "render.png"
    WORKER.call("import", {"path": str(src), "format": "stl", "clear": True})
    WORKER.call("render", {"path": str(out), "engine": engine, "width": width,
                           "height": height, "samples": samples,
                           "transparent": transparent, "frame_all": True})
    import base64
    return {"path": str(out), "bytes": out.stat().st_size,
            "filename": out.name, "job": str(job),
            "image_base64": base64.b64encode(out.read_bytes()).decode("ascii")}


def sync(part_id: str) -> dict:
    live = LIVE.status()
    if not live.get("live") or LIVE.client is None:
        raise BlenderServiceError("no live Blender session for this part")
    if LIVE.part_id != part_id:
        raise BlenderServiceError(
            f"Blender has part {LIVE.part_id!r} open, not {part_id!r}")
    job = LIVE.workspace / f"sync-{int(time.time())}.stl"
    LIVE.client.call("export", {"path": str(job), "format": "stl",
                                "selected": False})
    data = Path(job).read_bytes()
    part = _commit(part_id, LIVE.version, data, "blender-sync.stl",
                   "blender-sync", {"source": "live Blender"},
                   "saved from Blender", "blender")
    LIVE.version = str(part.get("current_version") or LIVE.version)
    return {"part": part, "bytes": len(data), "version": LIVE.version}


def _commit(part_id: str, parent: str, blob: bytes, filename: str,
            op: str, params: dict, note: str, origin: str) -> dict:
    if not blob:
        raise BlenderServiceError("Blender produced an empty mesh")
    measured = _sync_call(parts_service.metrics(blob, filename))
    report = _sync_call(parts_service.analyse(blob, filename, repair=False,
                                             weld_only=True))
    return store.add_version(
        part_id, blob=blob, filename=filename, op=op, params=params,
        metrics=measured.get("metrics") or {},
        objective=measured.get("objective"),
        report=report, parent=parent, kept=True, note=note,
        origin=origin, weld_only=True)


def _sync_call(coro):
    """Run an async parts_service call from a synchronous Blender operation."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # We are already inside an event loop (a FastAPI endpoint called this
    # synchronous helper via to_thread, so this branch is defensive).
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def open_part(part_id: str, version: str | None,
              bounds: dict | None = None) -> dict:
    blob, v, _part = _blob_for(part_id, version)
    return LIVE.open(part_id, v["id"], blob, v.get("filename") or "input.stl",
                     bounds)


def place(bounds: dict | None) -> dict:
    if LIVE.proc is None:
        return {"live": False}
    return _place_window(LIVE.proc.pid, bounds, topmost=True)


def show(visible: bool) -> dict:
    if LIVE.proc is None:
        return {"live": False}
    return _show_window(LIVE.proc.pid, visible)


def close_live() -> dict:
    LIVE.stop()
    return {"live": False}
