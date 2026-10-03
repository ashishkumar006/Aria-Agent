"""Aria's A2UI component catalog.

What this is for
----------------
A2UI agents emit `component: "Text"` / `"Button"` / `"Column"`. Which concrete
widget that becomes is the *client's* decision, and the client declares which
catalog it is speaking via `catalogId` in `createSurface`. So a catalog is the
thing that turns "the model said Button" into "the model said `Action` and we
rendered a real Aria button".

That makes the catalog the whole asset. Without one, an agent emitting Google's
generic `basic` catalog produces UI that looks foreign in Aria and cannot
express our design system at all - no `Pill`, no `Stat`, no `Empty`, none of
the components the console is actually built from.

It is also the security boundary. A *closed* set is the property that matters:
the model cannot invent a component, because anything outside the catalog is
rejected before it is rendered. An open-ended "describe whatever UI you like"
contract has neither property.

A2UI v0.9 shape
----------------
Components are a flat list with a `component` discriminator and children
referenced by id (an adjacency list). Data binds by JSON Pointer. This module
produces and validates that shape.

Deliberately NOT here
---------------------
- Anything that executes. No `eval`, no script components, no URLs to fetch.
  A2UI's own guidance is to treat agent output as untrusted, and the attacks
  it names are phishing (spoofing a legitimate-looking control), XSS via
  property values, and DoS via pathological nesting. The limits below are the
  mitigations, and each one is a test.
- `iframe` / web-view components. They would need a real sandbox story, which
  we do not have.
"""

from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = "v0.9"
CATALOG_ID = "aria/catalog/v1"

# ── limits ───────────────────────────────────────────────────────────────────
# These are the DoS mitigations, and they are not arbitrary.
MAX_COMPONENTS = 400
MAX_DEPTH = 12
MAX_NODES_PER_PARENT = 64
MAX_TEXT_LEN = 4000
MAX_TOTAL_TEXT = 60_000
MAX_ACTIONS_PER_COMPONENT = 1


class CatalogError(ValueError):
    """A payload that does not belong to this catalog."""


# ── the catalog ──────────────────────────────────────────────────────────────
# Every entry: the A2UI component name, the Aria widget it maps to, the
# properties it accepts, and which of those bind to the data model.

CATALOG: dict[str, dict[str, Any]] = {
    # ── layout ──────────────────────────────────────────────────────────────
    "Column": {
        "aria_widget": "stack",
        "doc": "Vertical stack. The usual root.",
        "props": {"children": "ids", "gap": "enum:tight|normal|loose"},
    },
    "Row": {
        "aria_widget": "stack-row",
        "doc": "Horizontal stack.",
        "props": {"children": "ids", "gap": "enum:tight|normal|loose",
                  "align": "enum:top|center|bottom"},
    },
    "Card": {
        "aria_widget": "glass-card",
        "doc": "Bordered surface. One per record in a list.",
        "props": {"child": "id", "title": "text"},
    },
    "Divider": {
        "aria_widget": "hairline",
        "doc": "A rule between sections.",
        "props": {"axis": "enum:horizontal|vertical"},
    },
    "Spacer": {
        "aria_widget": "spacer",
        "doc": "Flexible gap. `size` is a hint, not a pixel count.",
        "props": {"size": "enum:small|normal|large"},
    },

    # ── text ────────────────────────────────────────────────────────────────
    "Text": {
        "aria_widget": "prose",
        "doc": "Body text, or a heading with `variant`.",
        "props": {
            "text": "text|literal",
            "variant": "enum:body|caption|heading|mono",
        },
    },
    "KeyValue": {
        "aria_widget": "stat",
        "doc": "A label above a value. The console's `Stat`.",
        "props": {"label": "text|literal", "value": "text|literal",
                  "tone": "enum:neutral|good|warn|bad"},
    },

    # ── status ──────────────────────────────────────────────────────────────
    "Badge": {
        "aria_widget": "pill",
        "doc": "Small status pill. The console's `Pill`.",
        "props": {"text": "text|literal",
                  "tone": "enum:neutral|info|good|warn|bad"},
    },
    "StatusRow": {
        "aria_widget": "health-strip",
        "doc": "Several badges at once, for a summary line.",
        "props": {"items": "list<Badge>"},
    },
    "Empty": {
        "aria_widget": "empty-state",
        "doc": "The console's `Empty`. Explains why a list is empty.",
        "props": {"title": "text|literal", "sub": "text|literal"},
    },
    "Progress": {
        "aria_widget": "progress",
        "doc": "Determinate progress, 0..1.",
        "props": {"label": "text|literal", "value": "number"},
    },

    # ── data ────────────────────────────────────────────────────────────────
    "MetricRow": {
        "aria_widget": "metric-strip",
        "doc": "A row of KeyValues. Use instead of several loose ones.",
        "props": {"children": "ids"},
    },
    "CodeBlock": {
        "aria_widget": "code",
        "doc": "Preformatted text. Never interpolated as markup.",
        "props": {"text": "text|literal"},
    },
    "DataList": {
        "aria_widget": "record-list",
        "doc": "Rows of label/value pairs from the data model.",
        "props": {"rows": "list<{label,value}>", "emptyText": "text|literal"},
    },

    # ── interaction ─────────────────────────────────────────────────────────
    # Read-only catalog. There is no text input and no submit button, and that
    # is a decision rather than an omission: a generated form whose submission
    # performs an action is an approval flow wearing a disguise, and approval
    # flows are not something an agent should be able to synthesise.
    "Action": {
        "aria_widget": "button",
        "doc": "A button that emits ONE event. No form submission.",
        "props": {"label": "text|literal", "variant": "enum:primary|secondary",
                  "action": "event"},
    },
    "FilterChips": {
        "aria_widget": "chip-row",
        "doc": "Single-select filter. Emits an event; does not mutate state.",
        "props": {"options": "list<{id,label,selected}>", "action": "event"},
    },
    "Link": {
        "aria_widget": "link",
        "doc": "Navigates within the console. External URLs are rejected.",
        "props": {"label": "text|literal", "target": "enum:console"},
    },
}

# Components whose `children`/`child` property is a list of ids / one id.
_CHILD_PROPS = ("children", "child")

# Property names that must never be interpreted as markup or URLs.
FORBIDDEN_PROPS = frozenset({
    "html", "innerHTML", "dangerouslySetInnerHTML", "src", "href", "url",
    "script", "onClick", "onError", "style", "className", "dangerously",
})


def catalog_document() -> dict[str, Any]:
    """The catalog as served to an agent, and as referenced by `catalogId`."""
    return {
        "version": PROTOCOL_VERSION,
        "catalogId": CATALOG_ID,
        "description": (
            "Aria console components. Declarative only: nothing in a payload is "
            "executed, and any component outside this catalog is rejected."),
        "limits": {
            "maxComponents": MAX_COMPONENTS,
            "maxDepth": MAX_DEPTH,
            "maxNodesPerParent": MAX_NODES_PER_PARENT,
            "maxTextLen": MAX_TEXT_LEN,
            "maxTotalText": MAX_TOTAL_TEXT,
        },
        "rootComponent": "root",
        "components": {
            name: {
                "ariaWidget": spec["aria_widget"],
                "description": spec["doc"],
                "properties": dict(spec["props"]),
            }
            for name, spec in CATALOG.items()
        },
    }


def catalog_prompt() -> str:
    """A compact catalog description for a system prompt.

    Two parts: what each component is FOR, and what properties it ACCEPTS.
    The second part is not optional. Asked to show three numbers, a model
    reached for `MetricRow` and gave it `label` and `value` - which is exactly
    what `KeyValue` is for, and `MetricRow` takes neither. Every such mistake
    is a rejected surface and a wasted retry.
    """
    lines = [
        "AVAILABLE UI COMPONENTS (Aria console). Emit these names only; any "
        "other name is rejected.",
        "",
    ]
    for name in sorted(CATALOG):
        spec = CATALOG[name]
        props = ", ".join(sorted(spec["props"])) or "(none)"
        lines.append(f"- {name} ({props}): {spec['doc']}")
    lines += [
        "",
        "Rules:",
        f"  * The root component's id must be \"root\".",
        "  * Components form a flat list; refer to children by id.",
        f"  * At most {MAX_COMPONENTS} components, nesting depth {MAX_DEPTH}.",
        f"  * Text longer than {MAX_TEXT_LEN} characters is rejected.",
        "  * Using a property a component does not list is rejected. Check the "
        "parenthesised list before writing a component out.",
        "  * There is no text input and no form submit. Use Action for a "
        "single event.",
        "  * Never emit HTML, styles, class names, URLs, or event handler "
        "names. They are rejected.",
    ]
    return "\n".join(lines)


# ── validation ───────────────────────────────────────────────────────────────

def _as_text(value: Any) -> str | None:
    """A bound value: literal string, or a JSON-Pointer binding."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("path"), str):
        return None  # a binding, not literal text
    return None


def validate_components(components: Any) -> list[dict[str, Any]]:
    """Validate and normalise an A2UI `components` array.

    Returns the components with their ids and bindings resolved enough to
    render, or raises `CatalogError` naming the first problem. Failing loudly
    is the point: a rejected surface renders an error, whereas a half-accepted
    one renders something that looks authoritative and is not.
    """
    if not isinstance(components, list):
        raise CatalogError("components must be an array")
    if not components:
        raise CatalogError("components must not be empty")
    if len(components) > MAX_COMPONENTS:
        raise CatalogError(
            f"{len(components)} components exceeds the limit of {MAX_COMPONENTS}")

    seen_ids: set[str] = set()
    total_text = 0
    out: list[dict[str, Any]] = []

    for i, raw in enumerate(components):
        if not isinstance(raw, dict):
            raise CatalogError(f"components[{i}] must be an object")
        cid = raw.get("id")
        if not isinstance(cid, str) or not cid.strip():
            raise CatalogError(f"components[{i}].id must be a non-empty string")
        if len(cid) > 64:
            raise CatalogError(f"components[{i}].id is too long")
        if cid in seen_ids:
            raise CatalogError(f"duplicate component id {cid!r}")
        seen_ids.add(cid)

        name = raw.get("component")
        if not isinstance(name, str):
            raise CatalogError(f"components[{i}].component must be a string")
        spec = CATALOG.get(name)
        if spec is None:
            raise CatalogError(
                f"{cid}: unknown component {name!r}. This catalog has: "
                + ", ".join(sorted(CATALOG)))

        for key in raw:
            if key in ("id", "component"):
                continue
            if key in FORBIDDEN_PROPS:
                raise CatalogError(
                    f"{cid}: property {key!r} is not allowed. This catalog is "
                    f"declarative; markup, styles and handlers are refused.")
            if key not in spec["props"]:
                raise CatalogError(
                    f"{cid} ({name}): unknown property {key!r}. Allowed: "
                    + ", ".join(sorted(spec["props"])))

        # Text budget, counted across the whole surface.
        for key, value in raw.items():
            if key in ("id", "component"):
                continue
            text = _as_text(value)
            if text is None:
                continue
            if len(text) > MAX_TEXT_LEN:
                raise CatalogError(
                    f"{cid}.{key} is {len(text)} chars; the limit is "
                    f"{MAX_TEXT_LEN}. Split it or summarise.")
            total_text += len(text)
            if total_text > MAX_TOTAL_TEXT:
                raise CatalogError(
                    f"surface text exceeds {MAX_TOTAL_TEXT} chars in total")

        # Children: existence and cardinality.
        kids: list[str] = []
        for prop in _CHILD_PROPS:
            if prop not in raw:
                continue
            value = raw[prop]
            if prop == "child":
                if not isinstance(value, str):
                    raise CatalogError(f"{cid}.child must be a component id")
                kids.append(value)
            else:
                if not isinstance(value, list):
                    raise CatalogError(f"{cid}.children must be an array of ids")
                if len(value) > MAX_NODES_PER_PARENT:
                    raise CatalogError(
                        f"{cid} has {len(value)} children; the limit is "
                        f"{MAX_NODES_PER_PARENT}")
                for k in value:
                    if not isinstance(k, str):
                        raise CatalogError(
                            f"{cid}.children must contain only ids")
                    kids.append(k)

        actions = [k for k in ("action",) if k in raw]
        if len(actions) > MAX_ACTIONS_PER_COMPONENT:
            raise CatalogError(f"{cid} declares too many actions")

        out.append({"id": cid, "component": name,
                    "props": {k: v for k, v in raw.items()
                              if k not in ("id", "component")},
                    "_children": kids})

    # Second pass: every referenced child must exist. Done separately so the
    # error can name the dangling reference instead of the component index.
    for comp in out:
        for kid in comp["_children"]:
            if kid not in seen_ids:
                raise CatalogError(
                    f"{comp['id']} references unknown child {kid!r}")

    _check_depth(out)

    roots = [c["id"] for c in out if c["id"] == "root"]
    if not roots:
        raise CatalogError('exactly one component must have id "root"')

    # `_children` is working state for the depth check, derived from props.
    # Leaving it in the returned value shipped internal bookkeeping to the
    # client, which then had to know to ignore it.
    for comp in out:
        comp.pop("_children", None)
    return out


def _check_depth(components: list[dict[str, Any]]) -> None:
    """Reject pathological nesting.

    Iterative rather than recursive: a 400-component cycle or a very deep chain
    must produce a clean error, not a RecursionError from the validator.
    """
    by_id = {c["id"]: c for c in components}
    for start in by_id:
        depth = 0
        node = by_id[start]
        seen = {start}
        while node["_children"]:
            node = by_id.get(node["_children"][0])
            if node is None:
                break
            if node["id"] in seen:
                raise CatalogError(
                    f"component cycle detected at {node['id']!r}")
            seen.add(node["id"])
            depth += 1
            if depth > MAX_DEPTH:
                raise CatalogError(
                    f"nesting deeper than {MAX_DEPTH} levels near {start!r}")


def render_to_text(components: list[dict[str, Any]], data: Any = None) -> str:
    """A plain-text rendering of a surface.

    Not the console renderer - that is React's job. This exists so a surface
    can be logged, diffed in tests, and shown in the agent's own answer as a
    fallback when the client cannot render. A generated UI with no textual
    representation is one that cannot be reviewed, and an unreviewable UI is
    one nobody should trust.
    """
    out: list[str] = []
    for c in components:
        bits = [c["component"]]
        for key, value in sorted(c["props"].items()):
            if key in _CHILD_PROPS:
                continue
            if isinstance(value, dict) and "path" in value:
                resolved = _resolve(data, value["path"])
                bits.append(f"{key}={resolved!r}")
            elif isinstance(value, (str, int, float, bool)):
                bits.append(f"{key}={value!r}")
        out.append(f"<{c['id']} {' '.join(bits)}>")
    return "\n".join(out)


def _resolve(data: Any, pointer: str) -> Any:
    """Minimal JSON-Pointer read. Bounded so a crafted path cannot wander."""
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        return None
    node = data
    hops = 0
    for part in pointer.lstrip("/").split("/"):
        hops += 1
        if hops > 16:
            return None
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if node is None:
            return None
    return node


def validate_surface(payload: Any) -> dict[str, Any]:
    """Validate one `createSurface` + components + data bundle.

    This is the unit an agent's whole output is checked against, so it is also
    where the cheap total-size guard lives: a payload can be individually valid
    components and still be a denial of service.
    """
    if not isinstance(payload, dict):
        raise CatalogError("surface payload must be an object")
    try:
        encoded = json.dumps(payload)
    except (TypeError, ValueError) as e:
        raise CatalogError(f"surface is not JSON-serialisable: {e}")
    if len(encoded) > 256 * 1024:
        raise CatalogError(
            f"surface is {len(encoded)} bytes; the limit is 262144")

    surface_id = payload.get("surfaceId")
    if not isinstance(surface_id, str) or not surface_id.strip():
        raise CatalogError("surfaceId is required")
    if len(surface_id) > 64 or not all(
            ch.isalnum() or ch in "-_" for ch in surface_id):
        raise CatalogError(
            "surfaceId must be alphanumeric with - or _ (it appears in "
            "message envelopes and DOM ids)")

    catalog_id = payload.get("catalogId") or CATALOG_ID
    if catalog_id != CATALOG_ID:
        raise CatalogError(
            f"unknown catalogId {catalog_id!r}; this agent speaks {CATALOG_ID!r}")

    data = payload.get("data")
    if data is not None and not isinstance(data, dict):
        raise CatalogError("data must be an object")

    components = validate_components(payload.get("components"))
    return {"surfaceId": surface_id, "catalogId": catalog_id,
            "data": data or {}, "components": components,
            "text": render_to_text(components, data or {})}
