"""Layer B — perception interpretation (charter §10).

Biggest cost knob: pre-filter the AX markdown before it hits the LLM, and
summarise long trees. Also extract structured rows via regex when possible
so we can skip an LLM call entirely (Layer 1 / deterministic path).
"""
from __future__ import annotations

import re


def filter_ax_markdown(tree_markdown: str, query: str | None = None,
                       max_chars: int = 6000) -> str:
    """Pre-filter the AX tree markdown.

    - If `query` given, keep only lines mentioning it (case-insensitive).
    - Truncate to `max_chars` to bound the LLM context window.
    - Drop pure-whitespace / decorative lines.
    """
    lines = [ln for ln in tree_markdown.splitlines() if ln.strip()]
    if query:
        q = query.lower()
        kept = [ln for ln in lines if q in ln.lower()]
        lines = kept or lines  # fall back to full if query matches nothing
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…(truncated)"
    return text


def extract_rows(tree_markdown: str, pattern: str) -> list[dict]:
    """Regex-extract structured rows (e.g. name + figure) without an LLM.

    `pattern` should have named groups. Returns list of dicts. Used when the
    goal is a simple extraction ("what's in cell B3", "list the 5 prices").
    """
    try:
        rx = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    except re.error:
        return []
    return [m.groupdict() for m in rx.finditer(tree_markdown)]


def element_count(tree_markdown: str) -> int:
    """Count indexed elements — the trap-table signal.

    The driver's markdown tags elements as `[N] Role "Label"` (observed on
    Windows 0.19.3); some docs show `[element_index N]`. Match both.
    """
    return len(re.findall(r"\[(\d+)\]\s+\w+", tree_markdown))
