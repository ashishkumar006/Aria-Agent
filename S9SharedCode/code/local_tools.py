"""Tools owned by the console sections, not by the MCP child.

Parts and Apps need the agent's in-process store, version history and job
registry. Shipping those calls through a separate MCP process would create a
second writer and a second source of truth for the same JSON files. These
tools are therefore dispatched by `mcp_runner`'s `extra_dispatch` hook, in the
same process as the DAG executor.

The schemas are merged into `skills._TOOL_CATALOG` so a skill's
`tools_allowed` grant is still the only way to expose one to a model. The
dispatch is also fenced by the calling skill's allowlist in `skills.run_skill`.
"""
from __future__ import annotations

import json
from typing import Any

LOCAL_TOOL_NAMES = {
    "parts_list", "part_get", "part_analyse", "part_repair", "part_optimise",
    "part_start_loop", "part_job_status", "part_cancel_job", "part_export",
    "part_set_view", "app_list", "app_get", "app_build", "app_action",
}


def _tool(name: str, description: str, properties: dict,
          required: list[str] | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    }


SCHEMAS: dict[str, dict] = {
    "parts_list": _tool(
        "parts_list",
        "List the user's saved parts with id, name, current version, face "
        "count, verdict and updated time. Use this to resolve a name the user "
        "said into a part id before calling another parts tool.",
        {"query": {"type": "string", "description": "Optional name/id filter."},
         "limit": {"type": "integer", "description": "Max rows (1-100)."}},
    ),
    "part_get": _tool(
        "part_get",
        "Read one saved part: versions, current version, metrics, verdict, "
        "issues and the last report summary. Use the exact part id or name "
        "returned by parts_list.",
        {"part": {"type": "string", "description": "Part id or exact name."},
         "version": {"type": "string",
                     "description": "Optional version id such as v2."}},
        ["part"],
    ),
    "part_analyse": _tool(
        "part_analyse",
        "Measure one version and return its verdict, issues and repair steps. "
        "Read-only: it never changes the mesh or the current version. Set "
        "repair=true only to see what the repair passes would change.",
        {"part": {"type": "string"},
         "version": {"type": "string"},
         "repair": {"type": "boolean"},
         "slicer": {"type": "boolean",
                    "description": "Run the slicer probe if installed (slow)."}},
        ["part"],
    ),
    "part_repair": _tool(
        "part_repair",
        "Apply the mesh repair passes and, if anything changed, save the result "
        "as a NEW version and make it current. Never destructive: the parent "
        "version stays available. Returns the new version id and the steps.",
        {"part": {"type": "string"},
         "version": {"type": "string"},
         "watertight": {"type": "boolean"},
         "fix_normals": {"type": "boolean"},
         "weld": {"type": "boolean"}},
        ["part"],
    ),
    "part_optimise": _tool(
        "part_optimise",
        "Run ONE optimisation pass on a version and save the measured result as "
        "a new version. If the objective did not improve the version is stored "
        "as reverted and the current version does not move. Returns before/after "
        "objectives, face counts and the created version id.",
        {"part": {"type": "string"},
         "version": {"type": "string"},
         "pass_name": {"type": "string",
                       "enum": ["decimate", "smooth", "detect_overhang",
                                "detect_thin_walls"]},
         "params": {"type": "object",
                    "description": ("Pass parameters, e.g. "
                                    "{\"target_faces\": 20000}, "
                                    "{\"iterations\": 1, \"strength\": 0.4}, "
                                    "{\"overhang_angle_deg\": 45}.")}},
        ["part", "pass_name"],
    ),
    "part_start_loop": _tool(
        "part_start_loop",
        "Start a bounded background loop of one optimisation pass. The loop "
        "keeps a step only when the objective falls and stops when it stops "
        "improving. Returns a job id; poll part_job_status for progress.",
        {"part": {"type": "string"},
         "version": {"type": "string"},
         "pass_name": {"type": "string",
                       "enum": ["decimate", "smooth", "detect_overhang",
                                "detect_thin_walls"]},
         "params": {"type": "object"},
         "max_steps": {"type": "integer", "description": "1-12, default 6."}},
        ["part", "pass_name"],
    ),
    "part_job_status": _tool(
        "part_job_status",
        "Read a parts job's status, step log, versions it created and error.",
        {"job_id": {"type": "string"}},
        ["job_id"],
    ),
    "part_cancel_job": _tool(
        "part_cancel_job",
        "Ask a parts job to stop after the current gateway call.",
        {"job_id": {"type": "string"}},
        ["job_id"],
    ),
    "part_export": _tool(
        "part_export",
        "Return a download link for one version (current by default). The file "
        "is served by the agent; do not paste mesh bytes into the answer.",
        {"part": {"type": "string"},
         "version": {"type": "string"}},
        ["part"],
    ),
    "part_set_view": _tool(
        "part_set_view",
        "Set the saved viewer state for a part so the console opens it with the "
        "requested camera and overlay. This is a preference, not a mesh change.",
        {"part": {"type": "string"},
         "preset": {"type": "string",
                    "enum": ["iso", "front", "back", "left", "right", "top",
                             "bottom", "fit"]},
         "mode": {"type": "string", "enum": ["shaded", "wireframe"]},
         "overlay": {"type": "string",
                     "enum": ["none", "overhang", "thin_walls", "issues"]}},
        ["part"],
    ),
    "app_list": _tool(
        "app_list",
        "List saved console apps with id, name, kind, schedule and counts.",
        {"limit": {"type": "integer"}},
    ),
    "app_get": _tool(
        "app_get",
        "Read one saved app's spec summary and current surface metadata.",
        {"app": {"type": "string", "description": "App id or exact name."}},
        ["app"],
    ),
    "app_build": _tool(
        "app_build",
        "Build a new agent-designed A2UI app from a description and save it. "
        "Returns the created app id and summary. Do not invent a surface; this "
        "tool owns the builder call and validation.",
        {"description": {"type": "string"},
         "name": {"type": "string"}},
        ["description"],
    ),
    "app_action": _tool(
        "app_action",
        "Send an action and optional input to an existing A2UI app. The app "
        "builder updates the surface and the new version is saved. Use for "
        "filter/submit/next actions the user asks the assistant to perform.",
        {"app": {"type": "string"},
         "action": {"type": "string"},
         "input": {"type": "object"}},
        ["app", "action"],
    ),
}


def _summary(part: dict) -> dict:
    return {k: part.get(k) for k in (
        "id", "name", "current_version", "faces", "vertices", "objective",
        "verdict", "versions", "updated", "tags", "format", "op", "kept")}


def _resolve_part(ref: str) -> dict:
    import parts_store as store
    text = str(ref or "").strip()
    if not text:
        raise ValueError("part is required")
    parts = store.list_parts()
    by_id = next((p for p in parts if p.get("id") == text), None)
    if by_id:
        return by_id
    low = text.lower()
    exact = [p for p in parts if str(p.get("name") or "").lower() == low]
    if len(exact) == 1:
        return exact[0]
    matches = [p for p in parts
               if low in str(p.get("name") or "").lower()
               or low in str(p.get("id") or "").lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"no saved part matches {text!r}; call parts_list")
    names = ", ".join(f"{p['name']} ({p['id']})" for p in matches[:8])
    raise ValueError(f"{text!r} matches more than one part: {names}")


def _resolve_app(ref: str) -> dict:
    import apps as store
    text = str(ref or "").strip()
    if not text:
        raise ValueError("app is required")
    rows = store.list_apps()
    row = next((a for a in rows if a.get("id") == text), None)
    if row:
        return row
    low = text.lower()
    exact = [a for a in rows if str(a.get("name") or "").lower() == low]
    if len(exact) == 1:
        return exact[0]
    matches = [a for a in rows
               if low in str(a.get("name") or "").lower()
               or low in str(a.get("id") or "").lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"no app matches {text!r}")
    names = ", ".join(f"{a['name']} ({a['id']})" for a in matches[:8])
    raise ValueError(f"{text!r} matches more than one app: {names}")


async def _app_build(args: dict) -> dict:
    # Lazy: agent_server imports skills, which imports this module.
    import agent_server as srv
    import apps as store
    description = str(args.get("description") or "").strip()
    if not description:
        raise ValueError("description is required")
    spec = await srv._app_builder_generate(description, mode="build")
    if args.get("name"):
        spec["name"] = str(args["name"])[:80]
    created = store.create_app(spec)
    return {"ok": True, "app_id": created["id"], "name": created["name"],
            "kind": created["kind"], "schedule": created.get("schedule"),
            "surface_components": len((created.get("surface") or {}).get("components") or [])}


async def _app_action(args: dict) -> dict:
    import agent_server as srv
    import apps as store
    app = _resolve_app(str(args.get("app") or ""))
    spec = store.get_spec(app["id"])
    if not spec or spec.get("kind") != "a2ui":
        raise ValueError("only an A2UI app can receive actions")
    action = str(args.get("action") or "").strip()
    if not action:
        raise ValueError("action is required")
    raw_input = args.get("input") if isinstance(args.get("input"), dict) else {}
    event = {"action": action[:80],
             "input": {str(k)[:80]: str(v)[:4000]
                       for k, v in list(raw_input.items())[:40]}}
    updated = await srv._app_builder_generate(
        str(spec.get("description") or spec.get("name") or ""),
        existing=spec, event=event, mode="action")
    saved = store.save_a2ui_surface(app["id"], updated["surface"],
                                    updated.get("state"), event)
    if saved is None:
        raise ValueError("app disappeared while the action ran")
    return {"ok": True, "app_id": app["id"], "action": action,
            "surface_components": len((saved.get("surface") or {}).get("components") or [])}


async def dispatch(name: str, args: dict | None) -> str | None:
    """Answer a local tool call, or None for a tool this module does not own."""
    if name not in LOCAL_TOOL_NAMES:
        return None
    a = args if isinstance(args, dict) else {}
    try:
        import parts_service as svc
        import parts_store as store
        if name == "parts_list":
            limit = max(1, min(int(a.get("limit") or 20), 100))
            query = str(a.get("query") or "").strip().lower()
            rows = store.list_parts()
            if query:
                rows = [p for p in rows
                        if query in str(p.get("name") or "").lower()
                        or query in str(p.get("id") or "").lower()]
            return json.dumps({"ok": True, "parts": [_summary(p) for p in rows[:limit]]})
        if name == "part_get":
            part = _resolve_part(str(a.get("part") or ""))
            detail = store.get_part(part["id"])
            version = a.get("version")
            v = store.get_version(part["id"], version) if version else detail.get("current") or {}
            return json.dumps({
                "ok": True,
                "id": detail["id"], "name": detail["name"],
                "current_version": detail.get("current_version"),
                "versions": [_summary({"id": x.get("id"), "name": detail["name"],
                                        "current_version": x.get("id"),
                                        "faces": (x.get("metrics") or {}).get("faces"),
                                        "objective": x.get("objective"),
                                        "verdict": x.get("verdict") or {},
                                        "op": x.get("op"), "kept": x.get("kept"),
                                        "updated": x.get("created"), "tags": [],
                                        "format": x.get("format"),
                                        "vertices": (x.get("metrics") or {}).get("vertices"),
                                        "versions": 0})
                           for x in detail.get("versions") or []],
                "version": _summary({"id": v.get("id"), "name": detail["name"],
                                     "current_version": v.get("id"),
                                     "faces": (v.get("metrics") or {}).get("faces"),
                                     "vertices": (v.get("metrics") or {}).get("vertices"),
                                     "objective": v.get("objective"),
                                     "verdict": v.get("verdict") or {},
                                     "op": v.get("op"), "kept": v.get("kept"),
                                     "updated": v.get("created"), "tags": [],
                                     "format": v.get("format"), "versions": 0}),
                "metrics": v.get("metrics") or {},
                "issues": (v.get("issues") or [])[:30],
                "view": detail.get("view") or {},
            })
        if name == "part_analyse":
            part = _resolve_part(str(a.get("part") or ""))
            updated = await svc.analyse_version(
                part["id"], a.get("version"), repair=bool(a.get("repair")),
                slicer=bool(a.get("slicer")))
            v = updated.get("current") or {}
            return json.dumps({
                "ok": True, "part": _summary(updated),
                "version": v.get("id"),
                "verdict": v.get("verdict") or {},
                "issues": (v.get("issues") or [])[:30],
                "metrics": v.get("metrics") or {},
                "repair_steps": ((v.get("report") or {}).get("repair") or {}).get("steps") or [],
            })
        if name == "part_repair":
            part = _resolve_part(str(a.get("part") or ""))
            out = await svc.repair_version(
                part["id"], a.get("version"),
                watertight=bool(a.get("watertight", True)),
                fix_normals=bool(a.get("fix_normals", True)),
                weld=bool(a.get("weld", True)),
                origin="dag")
            new = out.get("version") or {}
            return json.dumps({
                "ok": True, "changed": out.get("changed"),
                "part": _summary(out.get("part") or {}),
                "version": new.get("id"),
                "metrics_before": (out.get("result") or {}).get("metrics_before"),
                "metrics_after": (out.get("result") or {}).get("metrics_after"),
                "steps": (out.get("result") or {}).get("steps") or [],
            })
        if name == "part_optimise":
            part = _resolve_part(str(a.get("part") or ""))
            out = await svc.optimise_version(
                part["id"], a.get("version"),
                pass_name=str(a.get("pass_name") or "decimate"),
                params=a.get("params") if isinstance(a.get("params"), dict) else {},
                origin="dag")
            res = out.get("result") or {}
            return json.dumps({
                "ok": True, "changed": out.get("changed"),
                "improved": out.get("improved"),
                "part": _summary(out.get("part") or {}),
                "version": (out.get("version") or {}).get("id"),
                "objective_before": res.get("objective_before"),
                "objective_after": res.get("objective_after"),
                "faces_before": res.get("faces_before"),
                "faces_after": res.get("faces_after"),
                "note": res.get("note"),
            })
        if name == "part_start_loop":
            part = _resolve_part(str(a.get("part") or ""))
            job = svc.start_loop(
                part["id"], a.get("version"),
                pass_name=str(a.get("pass_name") or "decimate"),
                params=a.get("params") if isinstance(a.get("params"), dict) else {},
                max_steps=int(a.get("max_steps") or 6), origin="dag")
            return json.dumps({"ok": True, "job": job})
        if name == "part_job_status":
            return json.dumps({"ok": True, "job": svc.get_job(str(a.get("job_id") or ""))})
        if name == "part_cancel_job":
            return json.dumps({"ok": True, "job": svc.cancel_job(str(a.get("job_id") or ""))})
        if name == "part_export":
            part = _resolve_part(str(a.get("part") or ""))
            v = store.get_version(part["id"], a.get("version"))
            return json.dumps({
                "ok": True, "part_id": part["id"], "version": v["id"],
                "download": f"/api/parts/{part['id']}/versions/{v['id']}/stl",
                "filename": v.get("filename"),
            })
        if name == "part_set_view":
            part = _resolve_part(str(a.get("part") or ""))
            updated = store.set_view(part["id"], {
                "preset": a.get("preset"), "mode": a.get("mode"),
                "overlay": a.get("overlay"),
            })
            return json.dumps({"ok": True, "part": _summary(updated),
                               "view": updated.get("view")})
        if name == "app_list":
            import apps as app_store
            limit = max(1, min(int(a.get("limit") or 30), 100))
            rows = app_store.list_apps()[:limit]
            return json.dumps({"ok": True, "apps": [
                {k: r.get(k) for k in ("id", "name", "kind", "schedule",
                                       "count", "refreshed_at", "error")}
                for r in rows]})
        if name == "app_get":
            import apps as app_store
            row = _resolve_app(str(a.get("app") or ""))
            detail = app_store.get_app(row["id"]) or {}
            surface = detail.get("surface") or {}
            return json.dumps({
                "ok": True, "id": row["id"], "name": row["name"],
                "kind": row.get("kind"), "schedule": row.get("schedule"),
                "description": detail.get("spec", {}).get("description", ""),
                "surface_components": len(surface.get("components") or []),
                "surface_id": surface.get("surfaceId"),
                "refresh_prompt": detail.get("spec", {}).get("refresh_prompt", ""),
            })
        if name == "app_build":
            return json.dumps(await _app_build(a))
        if name == "app_action":
            return json.dumps(await _app_action(a))
        return json.dumps({"ok": False, "error": f"unknown local tool {name}"})
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:600]})
