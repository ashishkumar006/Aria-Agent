"""Agent-generated A2UI surfaces, validated before they are ever rendered.

The flow, and why each step exists
----------------------------------
1. `generate_surface` asks the model for a surface, with the catalog injected
   into the prompt rather than trusted to memory.
2. Whatever comes back is parsed and run through `a2ui_catalog.validate_surface`
   IMMEDIATELY. Not at render time - at generation time, so a rejected
   surface never reaches the client at all.
3. A rejected surface produces a diagnostic error naming the offending
   component, and the model gets one retry with that error appended.
4. On success the client gets `{surfaceId, components, data, text}` - plus the
   plain-text rendering, because a generated UI with no textual form cannot be
   reviewed, and an unreviewable UI is one nobody should trust.

The untrusted-input rules, concretely
-------------------------------------
- Nothing executes. No markup, no styles, no URLs, no handlers: the catalog has
  no such properties and the validator rejects them by name.
- No text input and no submit action. A generated form whose submission performs
  an action is an approval flow wearing a disguise, and an agent must not be
  able to synthesise one.
- Every surface is bounded (component count, nesting, text, total bytes).
- The model gets no tool access here. Generating UI does not need to reach the
  filesystem, the network, or an account.
- The gateway call is capped, so a pathological request cannot cost unbounded
  money.
"""

from __future__ import annotations

import json
import re
from typing import Any

import a2ui_catalog as C

MAX_QUERY = 400
MAX_GENERATION_TOKENS = 2000
MAX_RETRIES = 1
GENERATION_TIMEOUT_S = 90.0

# The model is asked for JSON and nothing else. Anything else in the reply is
# discarded rather than half-parsed.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Pull one JSON object out of a model reply.

    Models wrap JSON in prose and fences no matter how firmly the prompt asks
    them not to. Rather than fail the turn, take the largest balanced object in
    the reply. Returns None when there is nothing object-shaped in it, which is
    a legitimate outcome and reported as such.
    """
    if not text:
        return None
    candidates: list[str] = []
    for m in _FENCE.finditer(text):
        candidates.append(m.group(1))
    candidates.append(text)

    best: dict[str, Any] | None = None
    for blob in candidates:
        # Walk the string tracking brace depth outside string literals.
        depth = 0
        start = -1
        in_str = False
        esc = False
        for i, ch in enumerate(blob):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    chunk = blob[start:i + 1]
                    try:
                        obj = json.loads(chunk)
                    except ValueError:
                        start = -1
                        continue
                    if isinstance(obj, dict) and (
                            best is None or len(chunk) > len(json.dumps(best))):
                        best = obj
                    start = -1
    return best


def generation_prompt(request: str,
                      context: dict[str, Any] | None = None) -> tuple[str, str]:
    """`(system, user)` for surface generation.

    Split in two, and the split matters. An earlier version put everything in
    the system message and a literal `"go"` in the user message; the model read
    that as "a persona has been assigned, nothing has been asked" and replied
    "I'm ready! What are we doing?" The request belongs in the user turn, where
    a chat model actually looks for it.

    The catalog description is in the system message. That is the whole
    mechanism by which the model learns what it may emit: there is no filter
    list it could reason around, only a set of names that will be refused.
    """
    system = "\n".join([
        "You generate Aria console user interfaces in A2UI v0.9.",
        "",
        C.catalog_prompt(),
        "",
        "Reply with ONE JSON object and nothing else. No prose, no markdown "
        "fence, no explanation, no preamble. Start with `{` and end with `}`.",
        "",
        "Shape:",
        json.dumps({
            "surfaceId": "kebab-case-id",
            "catalogId": C.CATALOG_ID,
            "components": [
                {"id": "root", "component": "Column", "children": ["h"]},
                {"id": "h", "component": "Text", "text": "…", "variant": "heading"},
            ],
            "data": {},
        }, indent=2),
    ])

    user = "\n".join([
        f"Build a view for this request: {request[:MAX_QUERY]}",
        "",
        "Available data (already summarised): "
        + (context.get("summary", "(no data)") if context else "(no data)"),
        "",
        "Reply with ONE JSON object. Its top-level keys must be EXACTLY these "
        "four, spelled exactly, and no others:",
        "",
        '  "surfaceId"  - a short kebab-case string, e.g. "runs_by_skill"',
        f'  "catalogId"  - exactly "{C.CATALOG_ID}"',
        '  "components" - an array of component objects, described below',
        '  "data"       - an object of the values your components bind to',
        "",
        "Every entry in \"components\" must be an object with EXACTLY these "
        "three kinds of key:",
        "",
        '  "id"        - a short unique name, e.g. "root", "h1", "stat_total"',
        '  "component" - one of: ' + ", ".join(sorted(C.CATALOG)),
        '  plus that component\'s own properties',
        "",
        "A component is NOT {\"type\": ...}. It is {\"id\": ..., "
        "\"component\": \"Name\", ...properties}. Children are referenced by "
        "id: use a \"children\" array for many, or \"child\" for one. "
        "Exactly one component must have the id \"root\".",
        "",
        "To show a value from \"data\", BIND it - do not interpolate it:",
        '  correct:   "value": {"path": "/total_runs"}',
        '  wrong:     "value": "{{total_runs}}"   and   "value": "42"',
        "A literal string is only for text that does not change.",
        "",
        "One number per component: a single number goes in a KeyValue "
        "(label + value). A row of numbers goes in a MetricRow whose "
        "\"children\" list holds one KeyValue per number.",
        "",
        "Do not invent a different top-level shape. Do not add a \"summary\", "
        "\"view_type\", \"data_points\" or similar wrapper - the data belongs in "
        "\"data\" and the layout belongs in \"components\".",
        "",
        "Reply with the JSON object now, starting with { and ending with }.",
    ])
    return system, user


class GenerationRejected(Exception):
    """The model's surface did not survive validation."""

    def __init__(self, message: str, detail: str | None = None):
        super().__init__(message)
        self.detail = detail


async def generate_surface(request: str,
                           context: dict[str, Any] | None = None,
                           call_model=None) -> dict[str, Any]:
    """Produce a validated A2UI surface for `request`.

    `call_model(system, user, max_tokens) -> str` is injected so this is
    testable without a provider, and so the caller decides the gateway/billing
    story rather than this module reaching for a global.
    """
    request = (request or "").strip()
    if not request:
        raise GenerationRejected("describe the view you want")
    if len(request) > MAX_QUERY:
        raise GenerationRejected(
            f"request too long (max {MAX_QUERY} chars)")

    if call_model is None:
        raise GenerationRejected("no model configured for surface generation")

    prompt = generation_prompt(request, context)
    system_prompt, user_prompt = prompt
    last_error: str | None = None

    for attempt in range(MAX_RETRIES + 1):
        # The retry prompt carries the diagnosis. Without this the second
        # attempt is byte-identical to the first, so a retry is a second wasted
        # call rather than a correction.
        system_ask = system_prompt if not last_error else (
            f"{system_prompt}\n\nIMPORTANT: your previous attempt was REJECTED "
            f"with: {last_error}\nDo not repeat it.")
        user_ask = user_prompt if not last_error else (
            f"{user_prompt}\n\nYour previous reply was rejected: {last_error}")
        text = await call_model(system_ask, user_ask, MAX_GENERATION_TOKENS)
        obj = extract_json_object(text or "")
        if obj is None:
            last_error = ("the reply contained no JSON object. Reply with the "
                          "object only.")
            # Log a bounded prefix. Without this, "the model did not comply" is
            # undiagnosable - the failure could be a refusal, prose, a code
            # fence the scanner missed, or an empty response from the provider,
            # and they need different fixes.
            print(f"[a2ui] no JSON in reply "
                  f"({len(text or '')} chars): {(text or '')[:300]!r}",
                  flush=True)
            continue
        try:
            surface = C.validate_surface(obj)
        except C.CatalogError as e:
            last_error = str(e)
            print(f"[a2ui] surface rejected: {e}", flush=True)
            continue
        return {
            "ok": True,
            "attempts": attempt + 1,
            "surface": surface,
            # The A2UI wire messages, so a standards client can consume this
            # without the console reshaping it.
            "messages": surface_messages(surface),
        }

    raise GenerationRejected(
        f"the model did not produce a usable surface after "
        f"{MAX_RETRIES + 1} attempt(s)", last_error)


def surface_messages(surface: dict[str, Any]) -> list[dict[str, Any]]:
    """The surface as A2UI's three messages: create, update, data.

    `createSurface` first (it fixes the surfaceId and catalogId), then the
    components, then the data model. The spec requires a surface to exist
    before it is updated, so this order is not negotiable.
    """
    sid = surface["surfaceId"]
    return [
        {"version": C.PROTOCOL_VERSION,
         "createSurface": {"surfaceId": sid, "catalogId": surface["catalogId"],
                           "sendDataModel": True}},
        {"version": C.PROTOCOL_VERSION,
         "updateComponents": {
             "surfaceId": sid,
             "components": [
                 {"id": c["id"], "component": c["component"],
                  **{k: v for k, v in c["props"].items()}}
                 for c in surface["components"]
             ]}},
        {"version": C.PROTOCOL_VERSION,
         "updateDataModel": {"surfaceId": sid, "path": "/",
                             "value": surface.get("data") or {}}},
    ]


def surface_summary(surface: dict[str, Any]) -> dict[str, Any]:
    """A compact, reviewable description of what was generated.

    Returned alongside the surface so a human (or the agent's own answer) can
    see the shape without rendering it. A generated UI that can only be
    inspected by looking at it is a generated UI nobody inspects.
    """
    kinds: dict[str, int] = {}
    for c in surface["components"]:
        kinds[c["component"]] = kinds.get(c["component"], 0) + 1
    return {
        "surfaceId": surface["surfaceId"],
        "componentCount": len(surface["components"]),
        "components": kinds,
        "dataKeys": sorted((surface.get("data") or {}).keys()),
        "text": surface.get("text", ""),
    }
