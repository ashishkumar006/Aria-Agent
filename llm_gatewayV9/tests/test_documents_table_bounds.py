"""Regression tests for two defects introduced by labelled table rendering.

Both were found by an adversarial audit of the change, and both are silent:
content vanishes from the index, or a vector is computed over text the embedder
never saw whole.

1. **Cells past the header width were dropped.** The labelled renderer iterated
   `range(1, len(names))`, so a row with more cells than the header never had
   its extras read. `parse_csv`/`parse_xlsx`/`parse_docx` pad rows to a common
   width but `parse_html` and `parse_markdown` do not, so a ragged row from
   either of those lost data that the previous pipe-delimited form had indexed.

2. **Chunks could exceed the embedder's ceiling.** `rows_per = target // 12`
   encodes "a row is a handful of words", true for an 8-column menu and false
   for a 49-column export. A P&L CSV produced 12,443-char chunks against an
   8000-char limit; the embed path calls the embedder in-process and bypasses
   `/v1/embed`'s 413 guard, so the vectors were computed over silently
   truncated text.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from documents.chunker import (  # noqa: E402
    MAX_CHUNK_CHARS, Block, chunk_blocks,
)


def _text(block, **kw):
    return "\n".join(c.text for c in chunk_blocks([block], **kw))


def test_a_ragged_row_keeps_the_cells_past_the_header_width():
    """HTML/Markdown tables do not pad rows, so a row may be wider than the
    header. The extras used to be dropped from the index entirely."""
    b = Block(kind="table", header=["Day", "Dish", "Price"],
              rows=[["Day", "Dish", "Price"],
                    ["Tuesday", "Poha", "55", "extra", "more"]],
              text="ignored")
    text = _text(b, target_words=150)
    assert "- Dish: Poha" in text, text
    assert "- Price: 55" in text, text
    assert "extra" in text, text
    assert "more" in text, text


def test_ragged_cells_get_a_stable_placeholder_name():
    b = Block(kind="table", header=["Day", "Dish", "Price"],
              rows=[["Day", "Dish", "Price"], ["Tuesday", "Poha", "55", "extra"]],
              text="ignored")
    text = _text(b, target_words=150)
    assert "- col3: extra" in text, text


def test_a_duplicate_column_name_is_disambiguated():
    """Two columns called `Monday` would render as two identical `- Monday:`
    lines, which is the positional ambiguity the labelling was added to remove
    - just relocated."""
    b = Block(kind="table",
              header=["Day", "Monday", "Monday", "Tuesday"],
              rows=[["Day", "Monday", "Monday", "Tuesday"],
                    ["BREAKFAST", "Idli", "Dosa", "Upma"]],
              text="ignored")
    text = _text(b, target_words=150)
    assert "- Monday: Idli" in text, text
    assert "- Monday (2): Dosa" in text, text


def test_an_empty_column_still_appears_in_the_chunk():
    """A column empty in every row would otherwise have no lexical anchor, so
    "what is the menu on Wednesday" has nothing to match on a menu where
    Wednesday is closed."""
    b = Block(kind="table", header=["Meal", "Monday", "Tuesday", "Wednesday"],
              rows=[["Meal", "Monday", "Tuesday", "Wednesday"],
                    ["LUNCH", "Rice", "Dal", ""],
                    ["DINNER", "Dal", "Roti", ""]],
              text="ignored")
    text = _text(b, target_words=150)
    assert "Wednesday" in text, text
    assert "Day" not in text and "Meal | Monday" in text, text


def test_a_none_header_cell_is_not_named_None():
    b = Block(kind="table", header=["Meal", None, "Tuesday"],
              rows=[["Meal", None, "Tuesday"], ["LUNCH", "Rice", "Dal"]],
              text="ignored")
    text = _text(b, target_words=150)
    assert "- None:" not in text, text
    assert "Tuesday" in text, text


def test_a_wide_table_never_exceeds_the_embed_ceiling():
    """The regression: 49 columns x 80 rows produced 12,443-char chunks."""
    cols = ["CostCentre"] + [f"m{i}" for i in range(48)]
    header = cols
    rows = [header]
    for r in range(80):
        rows.append([f"CC{r:03d}"] + [f"{r * 100 + i}" for i in range(48)])
    b = Block(kind="table", header=header, rows=rows,
              text="ignored" * 100)
    chunks = chunk_blocks([b], target_words=150)
    assert chunks, "no chunks produced"
    over = [len(c.text) for c in chunks if len(c.text) > MAX_CHUNK_CHARS]
    assert not over, f"chunks over the embed ceiling: {over}"


def test_splitting_preserves_every_row():
    """The point of splitting on characters is to stay inside the ceiling, not
    to lose data."""
    cols = ["A", "B", "C", "D", "E", "F", "G", "H"]
    header = cols
    rows = [header]
    for r in range(60):
        rows.append([f"row{r:03d}"] + [f"value-{r}-{i}" * 6 for i in range(7)])
    b = Block(kind="table", header=header, rows=rows, text="ignored")
    text = _text(b, target_words=150)
    for r in (0, 7, 23, 59):
        assert f"row{r:03d}" in text, f"row{r} lost"


def test_every_split_chunk_repeats_the_header():
    """A chunk without its column names is the exact failure the header
    repetition exists to prevent."""
    cols = ["A", "B", "C", "D", "E", "F", "G", "H"]
    rows = [cols]
    for r in range(60):
        rows.append([f"row{r:03d}"] + [f"value-{r}-{i}" * 6 for i in range(7)])
    b = Block(kind="table", header=cols, rows=rows, text="ignored")
    chunks = chunk_blocks([b], target_words=150)
    assert len(chunks) > 1, "expected the table to split"
    for c in chunks:
        assert " | ".join(cols) in c.text, c.text[:120]


def test_a_single_oversized_line_is_not_cut_in_half():
    """A half-written number is worse than a long chunk: it reads as a
    different, wrong value. An un-splittable line therefore stays whole, and the
    chunk exceeds the ceiling rather than corrupting the value."""
    huge = "x" * (MAX_CHUNK_CHARS * 2)
    b = Block(kind="table", header=["A", "B", "C"],
              rows=[["A", "B", "C"], ["r", huge, "y"]],
              text="ignored")
    chunks = chunk_blocks([b], target_words=150)
    assert chunks
    joined = "\n".join(c.text for c in chunks)
    assert huge in joined, "the oversized cell was cut"
    assert "- C: y" in joined, joined[-200:]


def test_the_ceiling_constant_is_not_dead_code():
    """It documents the embedder limit; before this it was referenced only by a
    test that permitted twice its value."""
    src = (ROOT / "documents" / "chunker.py").read_text(encoding="utf-8")
    assert "MAX_CHUNK_CHARS" in src
    assert MAX_CHUNK_CHARS <= 8000, MAX_CHUNK_CHARS
