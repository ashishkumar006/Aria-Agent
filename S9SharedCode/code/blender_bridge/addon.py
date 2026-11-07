"""Aria's live Blender bridge.

This module runs inside Blender (GUI or `--background`). It owns a loopback
JSON-line server and executes every command on Blender's MAIN thread: `bpy` is
not thread-safe, and running an operator from the socket thread corrupts
context or silently does nothing.

The command surface is deliberately small and generic:

  ping / scene / catalog / op / rna_get / rna_set / export / import / render /
  checkpoint / save / undo / select / python / shutdown

`catalog` reads Blender's own RNA, so the agent can discover operators and
their parameters instead of guessing. `op` invokes any operator it names.
`python` is the universal escape hatch and is disabled unless the process was
started with `--allow-python`; the caller must obtain user approval.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import queue
import socket
import socketserver
import threading
import time
import traceback
from pathlib import Path
from typing import Any

PROTOCOL = "aria-blender/1"


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(str(v) for v in value)
    return str(value)


class Bridge:
    def __init__(self, *, token: str, workspace: str,
                 allow_python: bool = False):
        import bpy  # imported here so the module stays importable outside Blender
        self.bpy = bpy
        self.token = token
        self.workspace = Path(workspace).resolve()
        self.allow_python = bool(allow_python)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.queue: queue.Queue = queue.Queue()
        self.stop = threading.Event()
        self.server: socketserver.ThreadingTCPServer | None = None
        self._catalog_names: list[str] | None = None
        self._catalog_details: dict[str, dict] = {}

    # -- main-thread pump ---------------------------------------------------

    def pump(self) -> None:
        while True:
            try:
                request, done, box = self.queue.get_nowait()
            except queue.Empty:
                return
            try:
                box["response"] = self.handle(request)
            except Exception as exc:  # noqa: BLE001
                box["response"] = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}"[:800],
                    "trace": traceback.format_exc(limit=4)[-3000:],
                }
            finally:
                done.set()

    def submit(self, request: dict, timeout: float = 300.0) -> dict:
        done = threading.Event()
        box: dict = {}
        self.queue.put((request, done, box))
        if not done.wait(timeout):
            return {"ok": False, "error": f"Blender command timed out after {timeout:.0f}s"}
        return box.get("response") or {"ok": False, "error": "empty response"}

    def run_forever(self) -> None:
        while not self.stop.is_set():
            self.pump()
            time.sleep(0.02)

    # -- command handling ---------------------------------------------------

    def handle(self, request: dict) -> dict:
        if str(request.get("token") or "") != self.token:
            return {"ok": False, "error": "invalid bridge token"}
        cmd = str(request.get("cmd") or "").strip()
        args = request.get("args") if isinstance(request.get("args"), dict) else {}
        handler = getattr(self, f"cmd_{cmd}", None)
        if handler is None:
            return {"ok": False, "error": f"unknown command {cmd!r}"}
        data = handler(args)
        data.setdefault("ok", True)
        data["protocol"] = PROTOCOL
        return _jsonable(data)

    def _safe_path(self, raw: str, *, must_exist: bool = False) -> Path:
        path = Path(str(raw or "")).resolve()
        try:
            path.relative_to(self.workspace)
        except ValueError:
            raise ValueError(f"path escapes the Blender workspace: {path}")
        if must_exist and not path.exists():
            raise FileNotFoundError(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _mesh_objects(self):
        return [o for o in self.bpy.context.scene.objects if o.type == "MESH"]

    def _active(self, name: str | None = None):
        bpy = self.bpy
        if name:
            obj = bpy.data.objects.get(str(name))
            if obj is None:
                raise ValueError(f"no object named {name!r}")
            bpy.ops.object.select_all(action="DESELECT")
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
        return bpy.context.view_layer.objects.active

    def _set_mode(self, mode: str) -> None:
        bpy = self.bpy
        if not mode:
            return
        obj = bpy.context.view_layer.objects.active
        if obj is None:
            return
        if obj.mode != mode:
            bpy.ops.object.mode_set(mode=mode)

    def _override(self):
        bpy = self.bpy
        try:
            win = bpy.context.window_manager.windows[0]
            area = next((a for a in win.screen.areas if a.type == "VIEW_3D"), None)
            if area is None:
                return contextlib.nullcontext()
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            kwargs = {"window": win, "screen": win.screen, "area": area}
            if region is not None:
                kwargs["region"] = region
            return bpy.context.temp_override(**kwargs)
        except Exception:
            return contextlib.nullcontext()

    def cmd_ping(self, args: dict) -> dict:
        bpy = self.bpy
        return {
            "ok": True,
            "protocol": PROTOCOL,
            "blender": bpy.app.version_string,
            "pid": os.getpid(),
            "headless": bpy.app.background,
            "workspace": str(self.workspace),
            "allow_python": self.allow_python,
            "scene": bpy.context.scene.name if bpy.context.scene else "",
            "mode": bpy.context.mode,
            "active": bpy.context.view_layer.objects.active.name
            if bpy.context.view_layer.objects.active else "",
        }

    def cmd_scene(self, args: dict) -> dict:
        bpy = self.bpy
        objs = []
        for o in bpy.context.scene.objects:
            row = {"name": o.name, "type": o.type,
                   "selected": bool(o.select_get()),
                   "active": o == bpy.context.view_layer.objects.active}
            if o.type == "MESH":
                row.update({"vertices": len(o.data.vertices),
                            "faces": len(o.data.polygons),
                            "materials": [m.name if m else "" for m in o.data.materials]})
            objs.append(row)
        return {"ok": True, "objects": objs,
                "active": bpy.context.view_layer.objects.active.name
                if bpy.context.view_layer.objects.active else "",
                "mode": bpy.context.mode,
                "materials": [m.name for m in bpy.data.materials]}

    def cmd_select(self, args: dict) -> dict:
        bpy = self.bpy
        name = str(args.get("object") or "")
        if bool(args.get("all")):
            bpy.ops.object.select_all(action="SELECT")
        elif name:
            self._active(name)
        if args.get("mode"):
            self._set_mode(str(args["mode"]))
        return {"ok": True, "active": bpy.context.view_layer.objects.active.name
                if bpy.context.view_layer.objects.active else ""}

    def _operator_names(self) -> list[str]:
        if self._catalog_names is not None:
            return self._catalog_names
        bpy = self.bpy
        names: list[str] = []
        for group_name in dir(bpy.ops):
            if group_name.startswith("_"):
                continue
            try:
                group = getattr(bpy.ops, group_name)
            except Exception:
                continue
            for op_name in dir(group):
                if not op_name.startswith("_"):
                    names.append(f"{group_name}.{op_name}")
        self._catalog_names = names
        return names

    def _describe_op(self, ident: str) -> dict:
        cached = self._catalog_details.get(ident)
        if cached is not None:
            return cached
        bpy = self.bpy
        group_name, _, op_name = ident.partition(".")
        group = getattr(bpy.ops, group_name)
        row: dict = {"name": ident, "group": group_name, "op": op_name}
        try:
            rna = getattr(group, op_name).get_rna_type()
            row["label"] = str(getattr(rna, "name", "") or op_name)
            row["description"] = str(getattr(rna, "description", "") or "")
            props = []
            for p in getattr(rna, "properties", []):
                item = {
                    "name": str(getattr(p, "identifier", "")),
                    "type": str(getattr(p, "type", "")),
                    "label": str(getattr(p, "name", "") or ""),
                    "description": str(getattr(p, "description", "") or ""),
                }
                default = getattr(p, "default", None)
                if isinstance(default, (str, int, float, bool)):
                    item["default"] = default
                try:
                    if item["type"] == "ENUM" and getattr(p, "enum_items", None):
                        item["enum"] = [str(i.identifier) for i in p.enum_items][:100]
                except Exception:
                    pass
                props.append(item)
            row["params"] = props
        except Exception as exc:
            row["description"] = f"(introspection failed: {exc})"
            row["params"] = []
        self._catalog_details[ident] = row
        return row

    def cmd_catalog(self, args: dict) -> dict:
        query = str(args.get("query") or "").strip().lower()
        limit = max(1, min(int(args.get("limit") or 40), 200))
        names = self._operator_names()
        if query:
            matches = [n for n in names if query in n.lower()]
            # Also match operator labels/descriptions, but only for a bounded
            # candidate set: introspecting every operator is why the first
            # catalog call used to take minutes.
            if not matches:
                matches = [n for n in names if query in n.split(".")[0].lower()][:50]
        else:
            groups: dict[str, int] = {}
            for n in names:
                g = n.split(".", 1)[0]
                groups[g] = groups.get(g, 0) + 1
            return {"ok": True, "total": len(names), "groups": groups,
                    "operators": [self._describe_op(n) for n in names[:limit]],
                    "truncated": len(names) > limit,
                    "note": "Pass query to search operators; no query returns group counts and a sample."}
        rows = [self._describe_op(n) for n in matches[:limit]]
        return {"ok": True, "total": len(matches), "operators": rows,
                "truncated": len(matches) > limit}

    def cmd_op(self, args: dict) -> dict:
        bpy = self.bpy
        ident = str(args.get("name") or "").strip()
        if "." not in ident:
            raise ValueError("operator must be '<group>.<name>'")
        group_name, op_name = ident.split(".", 1)
        group = getattr(bpy.ops, group_name, None)
        op = getattr(group, op_name, None) if group is not None else None
        if op is None:
            raise ValueError(f"unknown Blender operator {ident!r}")
        params = args.get("params") if isinstance(args.get("params"), dict) else {}
        active = str(args.get("active") or "")
        mode = str(args.get("mode") or "")
        try:
            self._active(active or None)
            self._set_mode(mode)
            try:
                bpy.ops.ed.undo_push(message=f"aria: {ident}")
            except Exception:
                pass
            with self._override():
                result = op(**params)
            return {"ok": True, "operator": ident,
                    "result": _jsonable(result),
                    "scene": self.cmd_scene({}).get("objects", [])[:30]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "operator": ident,
                    "error": f"{type(exc).__name__}: {exc}"[:800],
                    "hint": "Check catalog params and the active object/mode."}

    def cmd_rna_get(self, args: dict) -> dict:
        path = str(args.get("path") or "").strip()
        if not path.startswith("bpy."):
            raise ValueError("RNA path must start with bpy.")
        value = eval(path, {"bpy": self.bpy}, {})  # noqa: S307 - gated by token
        return {"ok": True, "path": path, "value": _jsonable(value)}

    def cmd_rna_set(self, args: dict) -> dict:
        path = str(args.get("path") or "").strip()
        if not path.startswith("bpy."):
            raise ValueError("RNA path must start with bpy.")
        value = args.get("value")
        exec(compile(f"{path} = __aria_value__", "<aria-rna>", "exec"),
             {"bpy": self.bpy, "__aria_value__": value, "__builtins__": {}})
        return {"ok": True, "path": path, "value": _jsonable(value)}

    def cmd_python(self, args: dict) -> dict:
        if not self.allow_python:
            return {"ok": False,
                    "error": "Python execution is disabled; start Blender with --allow-python and obtain approval."}
        code = str(args.get("code") or "")
        out = io.StringIO()
        namespace = {"bpy": self.bpy, "__name__": "__aria__"}
        with contextlib.redirect_stdout(out):
            exec(compile(code, "<aria-python>", "exec"), namespace)  # noqa: S102
        return {"ok": True, "stdout": out.getvalue()[-20000:],
                "scene": self.cmd_scene({}).get("objects", [])[:30]}

    def cmd_import(self, args: dict) -> dict:
        bpy = self.bpy
        path = self._safe_path(args.get("path"), must_exist=True)
        fmt = str(args.get("format") or path.suffix.lstrip(".")).lower()
        if bool(args.get("clear", True)):
            bpy.ops.object.select_all(action="SELECT")
            bpy.ops.object.delete(use_global=False)
        if fmt == "stl":
            bpy.ops.wm.stl_import(filepath=str(path))
        elif fmt == "obj":
            bpy.ops.wm.obj_import(filepath=str(path))
        elif fmt in ("glb", "gltf"):
            bpy.ops.import_scene.gltf(filepath=str(path))
        elif fmt in ("ply",):
            bpy.ops.wm.ply_import(filepath=str(path))
        elif fmt in ("blend",):
            bpy.ops.wm.open_mainfile(filepath=str(path))
        else:
            raise ValueError(f"unsupported import format {fmt!r}")
        meshes = self._mesh_objects()
        if meshes:
            bpy.ops.object.select_all(action="DESELECT")
            for o in meshes:
                o.select_set(True)
            bpy.context.view_layer.objects.active = meshes[0]
        return {"ok": True, "scene": self.cmd_scene({})}

    def cmd_export(self, args: dict) -> dict:
        bpy = self.bpy
        path = self._safe_path(args.get("path"))
        fmt = str(args.get("format") or path.suffix.lstrip(".")).lower()
        selected = bool(args.get("selected", False))
        if selected:
            bpy.ops.object.select_all(action="DESELECT")
            for o in self._mesh_objects():
                o.select_set(True)
        if fmt in ("stl", "obj", "ply"):
            fn = getattr(bpy.ops.wm, f"{fmt}_export")
            kwargs = {"filepath": str(path)}
            if fmt == "stl":
                kwargs["export_selected_objects"] = selected
            elif fmt == "obj":
                kwargs["export_selected_objects"] = selected
            try:
                fn(**kwargs)
            except TypeError:
                fn(filepath=str(path))
        elif fmt in ("glb", "gltf"):
            bpy.ops.export_scene.gltf(filepath=str(path), export_format="GLB",
                                      use_selection=selected)
        elif fmt == "fbx":
            bpy.ops.export_scene.fbx(filepath=str(path), use_selection=selected)
        elif fmt == "blend":
            bpy.ops.wm.save_as_mainfile(filepath=str(path))
        else:
            raise ValueError(f"unsupported export format {fmt!r}")
        size = path.stat().st_size if path.exists() else 0
        return {"ok": True, "path": str(path), "bytes": size, "format": fmt}

    def cmd_save(self, args: dict) -> dict:
        path = self._safe_path(args.get("path"))
        self.bpy.ops.wm.save_as_mainfile(filepath=str(path))
        return {"ok": True, "path": str(path), "bytes": path.stat().st_size
                if path.exists() else 0}

    def cmd_checkpoint(self, args: dict) -> dict:
        path = self._safe_path(args.get("path") or
                               f"checkpoint-{int(time.time())}.blend")
        self.bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True)
        return {"ok": True, "path": str(path)}

    def cmd_apply_modifiers(self, args: dict) -> dict:
        bpy = self.bpy
        applied = []
        for obj in self._mesh_objects():
            bpy.ops.object.select_all(action="DESELECT")
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
            for mod in list(obj.modifiers):
                try:
                    bpy.ops.object.modifier_apply(modifier=mod.name)
                    applied.append(f"{obj.name}:{mod.name}")
                except Exception as exc:  # noqa: BLE001
                    applied.append(f"{obj.name}:{mod.name}:error:{exc}")
        return {"ok": True, "applied": applied,
                "scene": self.cmd_scene({}).get("objects", [])[:30]}

    def cmd_undo(self, args: dict) -> dict:
        self.bpy.ops.ed.undo()
        return {"ok": True, "scene": self.cmd_scene({})}

    def _ensure_camera(self):
        bpy = self.bpy
        if bpy.context.scene.camera:
            return bpy.context.scene.camera
        bpy.ops.object.camera_add(location=(0, -8, 4))
        cam = bpy.context.active_object
        bpy.context.scene.camera = cam
        return cam

    def cmd_render(self, args: dict) -> dict:
        bpy = self.bpy
        path = self._safe_path(args.get("path") or "render.png")
        scene = bpy.context.scene
        engine = str(args.get("engine") or "BLENDER_EEVEE")
        try:
            scene.render.engine = engine
        except Exception:
            pass
        scene.render.resolution_x = max(64, min(int(args.get("width") or 1024), 4096))
        scene.render.resolution_y = max(64, min(int(args.get("height") or 1024), 4096))
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = "PNG"
        scene.render.filepath = str(path)
        scene.render.film_transparent = bool(args.get("transparent", False))
        try:
            if hasattr(scene, "eevee") and hasattr(scene.eevee, "taa_render_samples"):
                scene.eevee.taa_render_samples = max(1, min(int(args.get("samples") or 32), 256))
        except Exception:
            pass
        cam = self._ensure_camera()
        if args.get("camera"):
            candidate = bpy.data.objects.get(str(args["camera"]))
            if candidate and candidate.type == "CAMERA":
                scene.camera = candidate
        if args.get("frame_all", True):
            try:
                bpy.ops.object.select_all(action="SELECT")
                bpy.ops.view3d.camera_to_view_selected()
            except Exception:
                pass
        bpy.ops.render.render(write_still=True)
        return {"ok": True, "path": str(path), "bytes": path.stat().st_size
                if path.exists() else 0, "engine": scene.render.engine,
                "camera": scene.camera.name if scene.camera else ""}

    def cmd_shutdown(self, args: dict) -> dict:
        self.stop.set()
        try:
            if self.server:
                self.server.shutdown()
        except Exception:
            pass
        try:
            self.bpy.ops.wm.quit_blender()
        except Exception:
            pass
        return {"ok": True, "stopping": True}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        bridge: Bridge = self.server.bridge  # type: ignore[attr-defined]
        while not bridge.stop.is_set():
            line = self.rfile.readline()
            if not line:
                return
            try:
                request = json.loads(line.decode("utf-8"))
                if not isinstance(request, dict):
                    raise ValueError("request must be an object")
                response = bridge.submit(request)
            except Exception as exc:  # noqa: BLE001
                response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            try:
                self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))
                self.wfile.flush()
            except OSError:
                return


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start(*, port: int, token: str, workspace: str,
          allow_python: bool = False) -> Bridge:
    bridge = Bridge(token=token, workspace=workspace, allow_python=allow_python)
    server = _Server(("127.0.0.1", int(port)), _Handler)
    server.bridge = bridge  # type: ignore[attr-defined]
    bridge.server = server
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="aria-blender-socket")
    thread.start()

    import bpy

    if bpy.app.background:
        # The bootstrap runs run_forever() on the main thread.
        return bridge

    def _pump():
        bridge.pump()
        return 0.05 if not bridge.stop.is_set() else None

    bpy.app.timers.register(_pump, first_interval=0.1, persistent=True)
    print(f"[aria.blender] bridge listening on 127.0.0.1:{port} "
          f"(workspace {workspace})", flush=True)
    return bridge
