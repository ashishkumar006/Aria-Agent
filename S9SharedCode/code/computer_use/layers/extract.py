"""Layer 1 — extract (charter §6).

Read content directly from the AX tree, clipboard, or file — NO LLM call.
For "what does this email say" or "what is in cell B3" no click is needed.
The text is already in the tree. Zero LLM cost. Try this first.

Key insight from the session: the AX tree exposes element `value` for
input fields (Edit/TextBox/Document) but NOT the rendered document text of
a Notepad window. To read a Notepad document we must use the clipboard
trick: select-all (Ctrl+A), copy (Ctrl+C), then read clipboard.
"""
from __future__ import annotations

import time
from typing import Callable

from computer_use.core.daemon import call, DaemonError


def read_clipboard() -> str | None:
    """Return the current system clipboard text, or None if unavailable."""
    try:
        r = call("clipboard_read", {"include_text": True}, timeout=10)
        # cua-driver returns {"text": "..."} or a content list.
        if isinstance(r, dict):
            return r.get("text") or r.get("content")
        return None
    except Exception:
        return None


def read_field_value(tree_markdown: str, label_substr: str) -> str | None:
    """Extract the `value` of a field whose label contains `label_substr`.

    The driver markdown tags elements as `[N] Role "Label" value="..."`.
    We parse the line for the label and pull the value out.
    """
    for line in tree_markdown.splitlines():
        if label_substr.lower() in line.lower() and "value=" in line:
            # value="..." or value='...'
            start = line.index("value=") + len("value=")
            quote = line[start]
            end = line.find(quote, start + 1)
            if end > start:
                return line[start + 1:end]
    return None


def read_document_text(pid: int, window_id, *, timeout: float = 20.0) -> str | None:
    """Read the full text of a document (Notepad, etc.) via clipboard.

    AX tree does NOT expose Notepad's document text reliably, so we:
      1. Focus the document (click it)
      2. Select all (Ctrl+A)
      3. Copy (Ctrl+C)
      4. Read clipboard
    Returns the text, or None on failure.
    """
    try:
        # Focus the document element (first Document/Edit/TextBox).
        st = call("get_window_state",
                  {"pid": pid, "window_id": window_id,
                   "include_screenshot": False}, timeout=timeout)
        els = (st.get("elements")
               or (st.get("structuredContent") or {}).get("elements") or [])
        doc_idx = None
        for e in els:
            if (e.get("role") or "").lower() in ("document", "edit", "textbox"):
                doc_idx = e.get("element_index")
                break
        if doc_idx is None:
            return None
        snap = st.get("snapshot_id")
        # Click into the document to focus it.
        args = {"pid": pid, "window_id": window_id, "element_index": doc_idx}
        if snap:
            args["snapshot_id"] = snap
        call("click", args, timeout=15)
        time.sleep(0.3)
        # Select all + copy. Foreground chords are window-scoped and need no
        # snapshot_id — and reusing the pre-click snapshot risks refusal
        # after the click expired it, so it is deliberately omitted here.
        call("press_key", {"pid": pid, "window_id": window_id, "key": "a",
                           "modifiers": ["ctrl"], "delivery_mode": "foreground"},
             timeout=15)
        time.sleep(0.3)
        call("press_key", {"pid": pid, "window_id": window_id, "key": "c",
                           "modifiers": ["ctrl"], "delivery_mode": "foreground"},
             timeout=15)
        time.sleep(0.3)
        return read_clipboard()
    except Exception:
        return None


def extract_structured_rows(tree_markdown: str, pattern: str) -> list[dict]:
    """Regex-extract structured rows (e.g. name + figure) without an LLM.

    `pattern` should have named groups. Returns list of dicts. Used when the
    goal is a simple extraction ("what's in cell B3", "list the 5 prices").
    """
    import re
    try:
        rx = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    except re.error:
        return []
    return [m.groupdict() for m in rx.finditer(tree_markdown)]


def try_extract(goal: str, tree_markdown: str, pid: int | None = None,
                window_id=None) -> dict | None:
    """Attempt a zero-LLM extraction for read-only goals.

    Returns {"content": str, "method": str} if successful, else None.
    The engine calls this BEFORE the L2b LLM judge for read goals.
    """
    g = goal.lower()
    # Pattern 1: "what does X say" / "read X" → document text via clipboard.
    if any(k in g for k in ("what does", "what is in", "read the", "show me the text", "contents of")):
        if pid is not None and window_id is not None:
            text = read_document_text(pid, window_id)
            if text:
                return {"content": text, "method": "document_clipboard"}
    # Pattern 2: "what is in cell B3" → regex row extraction from AX tree.
    import re
    cell_m = re.search(r"cell\s+([a-z]+\d+)", g)
    if cell_m:
        col, row = cell_m.group(1)[0], cell_m.group(1)[1:]
        # Excel-like: find the row, then the column header.
        rows = extract_structured_rows(
            tree_markdown,
            rf"\[(\d+)\]\s+Text\s+\"({col.upper()}{row}|.*?{col}.*?)\"")
        if rows:
            return {"content": str(rows[0]), "method": "ax_regex"}
    # Pattern 3: "list the N prices" → extract all price-like values.
    if "price" in g or "cost" in g:
        import re
        rx = re.compile(r"\$\s*([\d,]+\.?\d*)")
        prices = [m.group(1) for m in rx.finditer(tree_markdown)]
        if prices:
            return {"content": "\n".join(prices), "method": "ax_regex_prices"}
    return None
