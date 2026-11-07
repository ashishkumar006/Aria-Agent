"""Recalled hits are previews, not whole chunks.

`_format_memory_hits`'s own docstring says each line carries "a 400-char
preview", and the code interpolated `chunk` verbatim. Document chunks in the
live store average 999 characters and the largest is 32,870 (4,774 records in
`llm_gatewayV9/state/memory/document/memory.json`), and the chat block asks for
six hits — so one recall could add a quarter of a megabyte of prompt to a turn
that needed none of it. This is the uncapped half of why a two-letter "hi"
cost 5,076 logged input tokens.

The cap restores the documented shape, and it is visible: a clipped chunk ends
with an ellipsis rather than silently losing its middle.
"""

from __future__ import annotations

import re


class _Hit:
    def __init__(self, chunk=None, raw=None, kind="document"):
        self.kind = kind
        self.descriptor = "a passage from the report"
        self.source = "test"
        self.keywords = ["test"]
        self.value = {"chunk": chunk} if chunk is not None else {"raw": raw}


def _chunk_text(rendered: str) -> str:
    m = re.search(r"chunk: (.*)", rendered, re.S)
    assert m, f"no chunk line in:\n{rendered}"
    return m.group(1)


def test_a_long_chunk_is_cut_to_a_preview():
    from skills import _format_memory_hits

    body = "word " * 4000          # 20,000 chars, well past any preview
    rendered = _format_memory_hits([_Hit(chunk=body)])
    line = _chunk_text(rendered)

    assert len(line) <= 460, f"preview is {len(line)} chars — the cap is gone"
    assert line.endswith("…"), "a truncated chunk must say so, not stop mid-word"
    assert "word word word" in line


def test_a_short_chunk_is_untouched():
    from skills import _format_memory_hits

    rendered = _format_memory_hits([_Hit(chunk="the user prefers metric units")])
    assert "the user prefers metric units" in rendered
    assert not _chunk_text(rendered).endswith("…")


def test_raw_facts_are_capped_too():
    from skills import _format_memory_hits

    rendered = _format_memory_hits([_Hit(raw="y" * 9000, kind="fact")])
    m = re.search(r"raw: (.*)", rendered, re.S)
    assert m and len(m.group(1)) <= 460


def test_six_large_chunks_cannot_blow_the_prompt():
    """The shape the chat block actually asks for: top_k=6."""
    from skills import _format_memory_hits

    hits = [_Hit(chunk=("sentence " * 4000)) for _ in range(6)]
    total = len(_format_memory_hits(hits))
    # Six verbatim chunks would be ~180,000 chars (~45,000 tokens).
    assert total < 3200, f"{total} chars for six previews"
