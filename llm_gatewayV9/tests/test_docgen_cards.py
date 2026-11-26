"""The two designed-deck layouts: `stats` (big-number cards) and `columns`
(side-by-side cards).

They exist because a deck of bullets reads as a converted report. These tests
pin the contract: the blocks survive normalisation, the numbers and titles
reach the pptx file, and every format that cannot lay out cards still prints
the WORDS rather than dropping them.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import docgen  # noqa: E402


def _deck(blocks, title="Deck"):
    data, _ = docgen.build_pptx({"title": title, "blocks": blocks,
                                 "slide_size": "16:9"})
    from pptx import Presentation
    return Presentation(io.BytesIO(data))


def _text(prs) -> str:
    out = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                out.append(shape.text_frame.text)
    return "\n".join(out)


# ── normalisation ───────────────────────────────────────────────────────────

def test_stats_block_survives_normalisation():
    got = docgen._norm_blocks([
        {"type": "stats", "items": [
            {"value": "401", "label": "copper k"},
            {"value": "5.67e-8", "label": "sigma"}]}])
    assert len(got) == 1 and got[0]["type"] == "stats"
    assert [i["value"] for i in got[0]["items"]] == ["401", "5.67e-8"]
    assert got[0]["items"][0]["label"] == "copper k"


def test_columns_block_survives_normalisation():
    got = docgen._norm_blocks([
        {"type": "columns", "items": [
            {"title": "A", "body": "first"},
            {"title": "B", "body": "second"}]}])
    assert len(got) == 1 and got[0]["type"] == "columns"
    assert [c["title"] for c in got[0]["items"]] == ["A", "B"]


def test_stats_caps_at_four_and_columns_at_three():
    """More than four figures in a row is a table, not a callout."""
    many = [{"value": str(i), "label": "x"} for i in range(9)]
    got = docgen._norm_blocks([{"type": "stats", "items": many}])
    assert len(got[0]["items"]) == 4
    cols = [{"title": str(i), "body": "b"} for i in range(9)]
    got = docgen._norm_blocks([{"type": "columns", "items": cols}])
    assert len(got[0]["items"]) == 3


def test_kpi_alias_reaches_the_same_block():
    got = docgen._norm_blocks([
        {"type": "kpi", "items": [{"value": "1", "label": "one"}]}])
    assert got and got[0]["type"] == "stats"


def test_empty_stats_is_dropped_not_rendered():
    assert docgen._norm_blocks([{"type": "stats", "items": []}]) == []


# ── pptx rendering ──────────────────────────────────────────────────────────

def test_stats_render_their_values_and_labels():
    prs = _deck([{"type": "stats", "items": [
        {"value": "401", "label": "copper k"},
        {"value": "0.026", "label": "air k"}]}])
    body = _text(prs)
    assert "401" in body and "copper k" in body
    assert "0.026" in body and "air k" in body


def test_columns_render_each_title_and_body():
    prs = _deck([{"type": "columns", "items": [
        {"title": "Conduction", "body": "through solids"},
        {"title": "Convection", "body": "with a fluid"}]}])
    body = _text(prs)
    for want in ("Conduction", "through solids", "Convection", "with a fluid"):
        assert want in body


def test_stats_and_columns_do_not_raise_on_a_tiny_canvas():
    """A square/large-paper slide must clamp rather than overflow."""
    data, _ = docgen.build_pptx({"title": "T", "slide_size": "4:3", "blocks": [
        {"type": "stats", "items": [{"value": "9", "label": "nine"}]},
        {"type": "columns", "items": [{"title": "A", "body": "b"}]},
    ]})
    assert data[:2] == b"PK"


# ── degradation on paper ────────────────────────────────────────────────────

def test_pdf_prints_stats_and_columns_text():
    pdf, _ = docgen.build_pdf({"title": "T", "blocks": [
        {"type": "stats", "items": [{"value": "401", "label": "copper"}]},
        {"type": "columns", "items": [{"title": "Alpha", "body": "beta"}]},
    ]})
    import fitz
    doc = fitz.open(stream=pdf, filetype="pdf")
    text = "".join(p.get_text() for p in doc)
    assert "401" in text and "copper" in text
    assert "Alpha" in text and "beta" in text


# ── the glyph hazard ────────────────────────────────────────────────────────

def test_superscript_digits_beyond_three_do_not_draw_as_blocks():
    """cp1252 has ¹²³ only; ⁴-⁹ used to render as a solid block in the PDF."""
    out = docgen._font_safe("W/m²·K⁴ and T⁴")
    assert "⁴" not in out, f"unmapped superscript survived: {out!r}"
    assert "²" in out, "a supported superscript should be kept"
    assert "K^4" in out and "T^4" in out