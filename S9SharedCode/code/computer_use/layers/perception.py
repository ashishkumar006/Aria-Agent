"""Layer B — perception interpretation (charter §10).

Biggest cost knob: pre-filter the AX markdown before it hits the LLM, and
summarise long trees. Also extract structured rows via regex when possible
so we can skip an LLM call entirely (Layer 1 / deterministic path).
"""
from __future__ import annotations

import re


def filter_ax_markdown(tree_markdown: str, query: str | None = None,
                       max_chars: int = 60000) -> str:
    """Reduce the AX tree WITHOUT hiding anything important.

    - Collapses the deep leading indentation (tens of thousands of spaces that
      the model does not need — the [N] order carries the structure).
    - Drops the repetitive `actions=[...]` attribute (the role implies it).
    - Trims long value/help attributes to ~120 chars.
    - TRUNCATES any line over 300 chars (keeps the beginning, so a long error /
      result / confirmation is still readable) instead of dropping it.
    - De-dupes consecutive identical lines.

    Nothing is removed outright — long content is shortened, not discarded — so
    important static information (errors, results, status) survives.
    """
    import re as _re
    if not tree_markdown:
        return ""

    def _trim_attr(m):
        val = m.group(2)
        if len(val) > 120:
            val = val[:117] + "..."
        return m.group(1) + '="' + val + '"'

    out: list[str] = []
    prev = None
    for ln in tree_markdown.splitlines():
        if not ln.strip():
            continue
        ln = ln.strip()                                   # collapse indentation
        ln = _re.sub(r'(value|help)="([^"]*)"', _trim_attr, ln)
        ln = _re.sub(r"\s*\[actions=\[[^\]]*\]\]", "", ln)  # drop actions attr
        if len(ln) > 300:
            ln = ln[:297] + "..."                         # truncate long content
        if ln == prev:
            continue
        out.append(ln)
        prev = ln
    text = "\n".join(out)
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
    if not tree_markdown:
        return 0
    n1 = len(re.findall(r"\[(\d+)\]\s+\w+", tree_markdown))
    n2 = len(re.findall(r"\[element_index\s+(\d+)\]", tree_markdown, re.IGNORECASE))
    # Avoid double-counting lines containing both forms.
    if n1 and n2:
        lines = tree_markdown.splitlines()
        seen = set()
        for ln in lines:
            if re.search(r"\[(\d+)\]\s+\w+", ln) or re.search(r"\[element_index\s+(\d+)\]", ln, re.IGNORECASE):
                seen.add(ln.strip())
        return len(seen)
    return max(n1, n2)
