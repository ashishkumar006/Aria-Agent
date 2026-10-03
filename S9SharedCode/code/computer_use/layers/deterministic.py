"""Layer 2a — deterministic (charter §6).

When the goal is "compute 42 × 18 in Calculator" and you know the app's
hotkeys, the whole interaction is a sequence of press_key calls. No LLM in
the loop. This is the path students skip because writing hotkey sequences
is boring; it is also the path that keeps assignments cheap.

This module holds a registry of app-specific hotkey sequences. The engine
calls `try_deterministic(goal, app_hint)` BEFORE the L2b LLM judge. If a
match is found, the engine dispatches the sequence directly (0 LLM cost).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable


@dataclass
class DeterministicPlan:
    """A fixed sequence of actions to perform without LLM intervention."""
    app: str
    description: str
    actions: list[dict]  # each is a cua-driver action dict (see ACTION_SCHEMA)


# ── Calculator: click buttons by accessibility-tree element_index ──
# The button indices are DISCOVERED from the live AX tree at plan time (see
# _find_calc_button), NOT hardcoded. Hardcoding indices is brittle: they
# shift across Windows/Calculator versions, locales, and driver builds. The
# engine re-fetches a fresh snapshot before each click so the discovered
# indices stay valid even if the tree reflows between calls. Blind keyboard
# input does NOT reach Calculator reliably (UIA focus quirk), so we click.
#
# Fallback table is ONLY used if the live tree can't be scanned (no daemon /
# no elements) — it is a last resort, not the primary path.
_CALC_BUTTONS_FALLBACK = {
    "0": 43, "1": 44, "2": 45, "3": 46, "4": 47, "5": 48,
    "6": 49, "7": 50, "8": 51, "9": 52,
    "+": 40, "-": 39, "*": 38, "/": 37, "=": 41,
    "clear": 22,
}


def _find_calc_button(elements: list[dict], ch: str) -> int | None:
    """Resolve a calculator character to its live element_index.

    Matches the button whose label/name/id contains the digit or operator
    word. Covers en-US labels; the id= attributes (num0Button, plusButton,
    equalButton, clearButton, …) are locale-stable so we prefer those.
    """
    # Map char → candidate substrings to look for in label/name/id.
    # NOTE: keep these TIGHT. "add"/"subtract" are deliberately excluded from
    # the + / - entries because the Calculator's memory row has "Memory add"
    # (idx ~11) and "Memory subtract" (idx ~12) which contain those substrings
    # and would match BEFORE the real "Plus"/"Minus" operator buttons (idx
    # ~40/~39). "plus"/"minus" match both the operator label and its stable
    # id (plusButton/minusButton), so the looser terms are unnecessary.
    want = {
        "0": ("zero", "num0"), "1": ("one", "num1"), "2": ("two", "num2"),
        "3": ("three", "num3"), "4": ("four", "num4"), "5": ("five", "num5"),
        "6": ("six", "num6"), "7": ("seven", "num7"), "8": ("eight", "num8"),
        "9": ("nine", "num9"),
        "+": ("plus",), "-": ("minus",),
        "*": ("multiply", "times"), "/": ("divide", "divide by"),
        "=": ("equal",), "clear": ("clear",),
    }.get(ch, ())
    for e in elements:
        label = (e.get("label") or "").lower()
        name = (e.get("name") or "").lower()
        eid = (e.get("id") or e.get("automation_id") or "").lower()
        # Skip the Memory-row buttons (Memory add/subtract/store/recall): they
        # share substrings with the operator buttons and sit earlier in the
        # tree, so matching them first would drive the wrong button.
        if "memory" in label or "memory" in name or "memory" in eid:
            continue
        blob = f"{label} {name} {eid}"
        for w in want:
            if w in blob:
                return e.get("element_index")
    return None


def resolve_index(elements: list[dict], action: dict) -> int | None:
    """Re-resolve an element_index from a fresh AX tree.

    The Calculator's AX tree can reflow between plan time and dispatch time,
    invalidating indices that were resolved from an earlier scan. Plan actions
    carry a ``match`` field (the character/role used to find the button); this
    function re-finds the button in the *current* elements so a multi-click
    plan never hits a stale index. Returns None only if the button truly isn't
    present in the fresh tree.
    """
    match = action.get("match")
    if match is None:
        # No match key — fall back to the plan-time index (non-deterministic
        # actions shouldn't normally reach here).
        return action.get("element_index")
    if match in ("document",):
        # Notepad-style: resolve by role.
        for e in elements:
            if (e.get("role") or "").lower() in ("document", "edit", "textbox"):
                return e.get("element_index")
        return action.get("element_index")
    # Calculator-style: resolve by character.
    return _find_calc_button(elements, match)


def _calc_plan(expression: str, elements: list[dict] | None = None) -> DeterministicPlan:
    """Build a plan that drives the Calculator via button clicks.

    Button indices are resolved from the live AX ``elements`` when available;
    otherwise we fall back to the hardcoded table as a last resort. We click
    Clear first to wipe any stale display, then the digits/operators, then
    Equals. Verified live: 234 × 567 → display "1,32,678".
    """
    # Normalize × and ÷ to * and /, drop spaces.
    expr = expression.replace("×", "*").replace("÷", "/").replace("x", "*").replace(" ", "")
    table = elements if elements else []

    def idx(ch: str) -> int | None:
        live = _find_calc_button(table, ch) if table else None
        return live if live is not None else _CALC_BUTTONS_FALLBACK.get(ch)

    clear_idx = idx("clear")
    actions = []
    if clear_idx is not None:
        # Carry the "match" key so the engine can re-resolve this index from a
        # fresh AX tree at dispatch time (the tree reflows after each click).
        actions.append({"type": "click", "element_index": clear_idx, "match": "clear"})
    for ch in expr:
        i = idx(ch)
        if i is not None:
            actions.append({"type": "click", "element_index": i, "match": ch})
    eq = idx("=")
    if eq is not None and not expr.endswith("="):
        actions.append({"type": "click", "element_index": eq, "match": "="})
    return DeterministicPlan(
        app="calculator",
        description=f"Click buttons for '{expr}' in Calculator",
        actions=actions,
    )


# ── Notepad: write text, save (Ctrl+S) ──
def _notepad_write_plan(text: str, filename: str,
                        elements: list[dict] | None = None) -> DeterministicPlan:
    """Write `text` into Notepad and save via Ctrl+S.

    cua-driver has no `replace_text` tool (it is rejected as unclassified),
    so we click into the document and type the text, then press Ctrl+S to
    save. The document element is DISCOVERED from the live AX tree (first
    Document/Edit/TextBox role) rather than hardcoded to index 0 — index 0
    is brittle across Notepad versions/locales.
    """
    doc_idx = None
    if elements:
        for e in elements:
            if (e.get("role") or "").lower() in ("document", "edit", "textbox"):
                doc_idx = e.get("element_index")
                break
    if doc_idx is None:
        doc_idx = 0  # last-resort fallback
    return DeterministicPlan(
        app="notepad",
        description=f"Write '{text[:30]}...' and save as {filename}",
        actions=[
            {"type": "click", "element_index": doc_idx, "match": "document"},  # focus document
            {"type": "type", "value": text},
            {"type": "press_key", "value": "s", "modifiers": ["ctrl"]},
        ],
    )


# ── Generic: select-all + copy (read document to clipboard) ──
def _select_all_copy_plan() -> DeterministicPlan:
    return DeterministicPlan(
        app="*",
        description="Select all + copy to clipboard",
        actions=[
            {"type": "press_key", "value": "a", "modifiers": ["ctrl"]},
            {"type": "press_key", "value": "c", "modifiers": ["ctrl"]},
        ],
    )


# ── Registry: ordered list of (matcher, builder) ──
# Each matcher is (app_hint, regex_on_goal). First match wins.
# Builders receive the regex match AND the live AX elements (when available)
# so they can discover element_index values instead of hardcoding them.
_REGISTRY: list[tuple[str | None, re.Pattern, Callable]] = [
    # Calculator arithmetic: "compute 234 * 567", "calculate 2+2", "42 × 18"
    (None, re.compile(r"(compute|calculate|eval|what is|solve)\s*([\d\.\s\+\-\*/×÷x]+)", re.I),
     lambda m, els: _calc_plan(m.group(2).strip().replace(" ", ""), elements=els)),
    # Notepad write: "write 'hello' in notepad", "save 'text' to file X"
    ("notepad", re.compile(r"(write|type|save)\s+[\"'](.+?)[\"']", re.I),
     lambda m, els: _notepad_write_plan(m.group(2).strip(), "untitled.txt", elements=els)),
    # Select-all + copy for any app (read goal)
    (None, re.compile(r"(copy|select all|select everything)", re.I),
     lambda m, els: _select_all_copy_plan()),
]


def try_deterministic(goal: str, app_hint: str | None = None,
                      state: dict | None = None) -> DeterministicPlan | None:
    """Return a DeterministicPlan if the goal matches a known pattern.

    `app_hint` narrows the match (e.g. only try Notepad plans when the
    target is Notepad). `state` is the live window state (elements / tree);
    its AX elements are passed to builders so they can DISCOVER element_index
    values from the real tree instead of relying on hardcoded tables.
    Returns None if no deterministic path applies.
    """
    elements = (state or {}).get("elements") or [] if state else []
    for reg_app, rx, builder in _REGISTRY:
        # If the registry entry is app-specific, skip unless hint matches.
        if reg_app is not None and app_hint is None:
            continue
        if reg_app is not None and app_hint is not None:
            if reg_app.lower() not in app_hint.lower():
                continue
        m = rx.search(goal)
        if m:
            try:
                return builder(m, elements)
            except Exception:
                return None
    return None
