"""Build and update agent-authored A2UI apps.

The prompt is deliberately a file (`prompts/app_builder.md`), not a string in
this module: the catalog is appended at request time from
`a2ui_catalog.catalog_prompt()`, so adding a component never requires editing
the prompt by hand. The model gets real read-only tools through the caller
(news/search/fetch/knowledge), which is what lets a recurring app refresh with
current facts instead of remembered ones.

This module only composes prompts and validates/parses the model's JSON. The
agent server owns the tool loop and storage.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import a2ui_catalog as C

ROOT = Path(__file__).resolve().parent
PROMPT_PATH = ROOT / "prompts" / "app_builder.md"
MAX_DESCRIPTION = 4000
MAX_REPLY = 400_000
MAX_STATE_BYTES = 64 * 1024


class BuilderError(ValueError):
    """The model's reply was not a valid app spec."""


def _base_prompt() -> str:
    try:
        base = PROMPT_PATH.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise BuilderError(f"app builder prompt unavailable: {e}") from None
    return base.rstrip() + "\n\n" + C.catalog_prompt()


def system_prompt() -> str:
    return _base_prompt()


def user_prompt(description: str, *, existing: dict | None = None,
                event: dict | None = None, mode: str = "build") -> str:
    """The user turn. The catalog stays in the system turn; the task and the
    current app/action go here, where a chat model looks for the request."""
    description = (description or "").strip()[:MAX_DESCRIPTION]
    lines = [f"Task: {mode}."]
    if description:
        lines += ["User's app description:", description]
    else:
        lines += ["The user has asked for a change to the existing app."]
    if existing:
        # Bound the context. The surface is the useful part; do not echo the
        # entire data history back into the prompt.
        slim = {
            "name": existing.get("name"),
            "description": existing.get("description"),
            "schedule": existing.get("schedule"),
            "refresh_prompt": existing.get("refresh_prompt"),
            "state": existing.get("state") or {},
            "surface": existing.get("surface") or {},
        }
        lines += [
            "Existing app (update this, do not replace its identity):",
            json.dumps(slim, ensure_ascii=False)[:80_000],
        ]
    if event:
        lines += [
            "A user interaction just happened in this app. Its action and input "
            "values are:",
            json.dumps(event, ensure_ascii=False)[:20_000],
            "This is a state transition, not a request to rebuild the same form. "
            "The returned surface must show the result of the action using the "
            "submitted input. Keep the app's identity and layout recognizable.",
            "Return the updated surface and state.",
        ]
    lines += [
        "Return one JSON object with the app fields. No prose and no fence.",
    ]
    return "\n\n".join(lines)


def _json_object(text: str) -> dict:
    raw = (text or "").strip()
    if len(raw) > MAX_REPLY:
        raise BuilderError("app reply is too large")
    # Models still occasionally fence JSON despite the instruction. Strip the
    # outer fence, then take the first complete object. Never eval.
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise BuilderError("the app builder did not return a JSON object")
    try:
        obj = json.loads(raw[start:end + 1])
    except ValueError as e:
        raise BuilderError(f"app JSON was not parseable: {e}") from None
    if not isinstance(obj, dict):
        raise BuilderError("app JSON must be an object")
    return obj


def _normalise_component_keys(comp: dict) -> dict:
    """Accept the aliases models actually write for the component name.

    The catalog lists component NAMES but the prompt's `surface` example shows
    `"components": []`, so nothing ever showed the model the key that holds the
    name. It reliably guesses `type` (the convention in every other UI schema,
    OpenUI/A2UI-flavoured JSON included) and the build then dies on
    `components[0].component must be a string` — a whole paid builder run and a
    422 for the user, over a spelling. `kind` shows up too. Both are unambiguous,
    so rename rather than retry.
    """
    if not isinstance(comp.get("component"), str):
        for alias in ("type", "kind", "componentType", "widget"):
            value = comp.get(alias)
            if isinstance(value, str) and value.strip():
                comp["component"] = value.strip()
                comp.pop(alias, None)
                break
    return comp


def _flatten_inline_children(surface: dict) -> dict:
    """Repair the common model mistake `children: [{...component...}]`.

    The wire shape is an adjacency list: children are ids, not nested
    component objects. A model that nests them is not describing something
    unsafe, only a different (and unambiguous) shape, so flatten it rather
    than spending the user's retry on a format correction. Invalid entries
    are dropped and remain visible as a validation error if the tree then has
    no edge.
    """
    comps = surface.get("components")
    if isinstance(comps, dict):
        comps = [_normalise_component_keys({**v, "id": str(v.get("id") or k)})
                 for k, v in comps.items() if isinstance(v, dict)]
        surface["components"] = comps
    if not isinstance(comps, list):
        return surface
    out: list[dict] = []
    seen: set[str] = set()
    seq = [0]

    def emit(raw: Any) -> str | None:
        if not isinstance(raw, dict):
            return None
        comp = _normalise_component_keys(dict(raw))
        cid = str(comp.get("id") or "").strip()
        if not cid:
            seq[0] += 1
            cid = f"c{seq[0]}"
            comp["id"] = cid
        if cid in seen:
            return cid
        kids = comp.get("children")
        if isinstance(kids, list):
            ids = []
            for child in kids:
                if isinstance(child, str):
                    ids.append(child)
                elif isinstance(child, dict):
                    child_id = emit(child)
                    if child_id:
                        ids.append(child_id)
            comp["children"] = ids
        elif isinstance(kids, dict):
            child_id = emit(kids)
            comp["children"] = [child_id] if child_id else []
        child = comp.get("child")
        if isinstance(child, dict):
            comp["child"] = emit(child)
        seen.add(cid)
        out.append(comp)
        return cid

    for c in comps:
        emit(c)
    surface["components"] = out
    return surface


def parse_reply(reply: str, *, existing: dict | None = None) -> dict:
    """Validate a builder reply into a spec-shaped dict.

    Missing identity fields inherit from `existing`; a build with no name gets
    one from the description. The surface is validated against the same closed
    catalog every other A2UI surface uses.
    """
    obj = _json_object(reply)
    raw_surface = obj.get("surface")
    if not isinstance(raw_surface, dict):
        raise BuilderError("app reply is missing a surface object")
    surface = dict(raw_surface)
    surface.setdefault("catalogId", C.CATALOG_ID)
    surface.setdefault("surfaceId", "app")
    surface = _flatten_inline_children(surface)
    try:
        surface = C.validate_surface(surface)
    except C.CatalogError as e:
        raise BuilderError(f"surface rejected: {e}") from None

    name = str(obj.get("name") or (existing or {}).get("name") or "").strip()
    if not name:
        # A stable fallback; apps.validate_spec will slug it.
        name = "Aria app"
    description = str(
        obj.get("description") or (existing or {}).get("description") or ""
    ).strip()[:1000]
    schedule = str(
        obj.get("schedule") or (existing or {}).get("schedule") or "manual"
    ).strip()
    refresh_prompt = str(
        obj.get("refresh_prompt") or (existing or {}).get("refresh_prompt") or ""
    ).strip()[:4000]
    state = obj.get("state")
    if state is None:
        state = (existing or {}).get("state") or {}
    if not isinstance(state, dict):
        raise BuilderError("state must be a JSON object")
    try:
        encoded_state = json.dumps(state)
    except (TypeError, ValueError):
        raise BuilderError("state is not JSON-serialisable") from None
    if len(encoded_state.encode("utf-8")) > MAX_STATE_BYTES:
        raise BuilderError("state is larger than 64 KB")
    out = {
        "kind": "a2ui",
        "name": name,
        "description": description,
        "schedule": schedule,
        "refresh_prompt": refresh_prompt,
        "surface": surface,
        "state": state,
    }
    if existing and existing.get("id"):
        out["id"] = existing["id"]
    return out
