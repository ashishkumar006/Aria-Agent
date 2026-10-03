"""Layer B — perception interpretation (charter §10).

Biggest cost knob: pre-filter the AX markdown before it hits the LLM, and
summarise long trees. Also extract structured rows via regex when possible
so we can skip an LLM call entirely (Layer 1 / deterministic path).
"""
from __future__ import annotations

import re


def filter_ax_markdown(tree_markdown: str, query: str | None = None,
                       max_chars: int = 12000) -> str:
    """Pre-filter the AX tree markdown.

    - If `query` given, keyword filter (stopword-stripped, any keyword hits)
      instead of full-phrase substring which almost never matched.
    - Truncate to `max_chars` (default 12k ≈ 3k tokens, up from 6k which
      dropped controls) to bound the LLM context window.
    - Drop pure-whitespace / decorative lines.
    """
    import re as _re
    _STOP = {"the", "a", "an", "to", "of", "in", "on", "and", "or", "is",
             "what", "does", "do", "show", "me", "my", "please", "click",
             "open", "app", "window"}
    lines = [ln for ln in tree_markdown.splitlines() if ln.strip()]
    if query:
        kws = [w.lower() for w in _re.findall(r"[a-z0-9]+", query.lower())
               if w not in _STOP and len(w) > 2]
        if kws:
            scored = []
            for ln in lines:
                ll = ln.lower()
                hits = sum(1 for k in kws if k in ll)
                if hits:
                    scored.append((hits, ln))
            if scored:
                # Keep all hits sorted by relevance, then truncate.
                scored.sort(key=lambda x: -x[0])
                lines = [ln for _, ln in scored] + [
                    ln for ln in lines if ln not in {s[1] for s in scored}][:200]
            # else: fall back to full list (no keyword matched)
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
