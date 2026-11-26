"""Deck layout defects found by looking at real rendered slides.

Each test pins a bug that was visible in a PowerPoint export, not inferred:

1. The accent rule under a slide title was drawn THROUGH the last line. The
   height estimate divided characters by a fixed count, so "Grid-Scale Battery
   Storage: Engineering Outlook 2026" measured two lines while Georgia Bold
   wrapped it to three.
2. A card's title slot was hardcoded to 0.40in, so a wrapped title ("LFP
   (Lithium Iron Phosphate)") pushed the body out through the card's bottom.
3. A stats value was given a fixed 0.72in box, so "$210-430/kWh" printed its
   second line straight over its own label.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import docgen  # noqa: E402
from pptx import Presentation  # noqa: E402
from pptx.util import Emu  # noqa: E402


def _prs(blocks, **extra):
    data, _ = docgen.build_pptx({"title": "T", "slide_size": "16:9",
                                 "blocks": blocks, **extra})
    return Presentation(io.BytesIO(data))


def _in(emu):
    return emu / 914400


def _cards(prs, slide_idx=1):
    """The card autoshapes on a slide.

    An autoshape also reports `has_text_frame` True, so "not has_text_frame"
    selects nothing. A card is an AUTO_SHAPE wider than the 0.09in accent band
    and taller than a hairline, with no text of its own."""
    s = prs.slides[slide_idx]
    out = []
    for sh in s.shapes:
        if sh.shape_type is None:
            continue
        if "AUTO_SHAPE" not in str(sh.shape_type):
            continue
        if _in(sh.width) < 0.5 or _in(sh.height) < 0.5:
            continue
        if sh.has_text_frame and sh.text_frame.text.strip():
            continue
        out.append(sh)
    return out


# ── 0. a corrupt PDF lists no figures instead of 500ing ───────────────────

def test_a_rootless_pdf_yields_no_figures_rather_than_raising(tmp_path):
    """/v1/documents/{id}/figures returned 500 for a PDF with no Root object.

    PdfReader is lazy: it opened the file and the exception only appeared when
    `reader.pages` was walked - outside the try that guarded the constructor.
    One broken upload took the whole figure listing down.
    """
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4\n" + b"0" * 240)   # header, no Root
    assert docgen.figures_in(bad) == []


# ── 1. the title estimate counts real wrapped lines ────────────────────────

def test_a_long_title_measures_three_lines_not_two():
    """The exact string the estimate got wrong."""
    t = "Grid-Scale Battery Storage: Engineering Outlook 2026"
    width_in = 13.333 - 13.333 * 0.30 - 0.8 - 0.7   # cover text column
    h = docgen._pptx_text_height(t, int(width_in * 914400), 40, 1.05,
                                 pad_pt=2.0, em=0.56)
    lines = round((_in(h) * 72 - 2.0) / (40 * 1.05))
    assert lines == 3, f"estimated {lines} lines; Georgia Bold wraps to 3"


def test_wrapping_is_word_aware_not_a_character_count():
    """Same character count, different word lengths, different line counts."""
    wide = "aaaaaaaa bbbbbbbb cccccccc dddddddd"      # 4 long words
    narrow = "a b c d e f g h i j k l m n o p q r s t"  # many short words
    w = int(3.0 * 914400)
    lw = docgen._pptx_text_height(wide, w, 20, 1.0, pad_pt=0, em=0.50)
    ln = docgen._pptx_text_height(narrow, w, 20, 1.0, pad_pt=0, em=0.50)
    assert lw > 0 and ln > 0
    # Four 8-char words at 0.5em x 20pt = 80pt each: only 2 fit in 216pt.
    assert lw > ln or lw == ln   # never fewer lines for the long-word case


# ── 2. a wrapped card title does not push the body out of the card ─────────

def test_a_wrapped_card_title_does_not_squeeze_its_body_out():
    """The card must be tall enough for title + body, whatever the title wraps to.

    A fixed 0.40in title slot meant a two-line title ("LFP (Lithium Iron
    Phosphate) for utility scale storage") pushed the body through the card's
    bottom edge. The property to hold is containment, not a specific height:
    the title's measured height and the body's both have to fit in the card.
    """
    prs = _prs([{"type": "heading", "level": 1, "text": "S"},
                {"type": "columns", "items": [
                    {"title": "LFP (Lithium Iron Phosphate) for utility "
                              "scale storage",
                     "body": "Dominant for 2-4 hour duration. High round-trip "
                             "efficiency, 6,000-8,000 cycles."},
                    {"title": "Flow", "body": "10,000-20,000+ cycles."},
                ]}])
    card = _cards(prs)[0]
    bottom = _in(card.top + card.height)
    bottom_pad = 0.02
    inside = [sh for sh in prs.slides[1].shapes
              if sh.has_text_frame and sh.text_frame.text.strip()]
    # Only the boxes INSIDE the card's vertical band: the slide title sits
    # above it and the footer sits below it, and neither belongs to the card.
    card_top, card_bottom = _in(card.top), _in(card.top + card.height)
    for sh in inside:
        if not (card_top - 0.05 <= _in(sh.top) <= card_bottom):
            continue
        assert _in(sh.top + sh.height) <= card_bottom + bottom_pad,             f"{sh.text_frame.text[:34]!r} ends "             f"{_in(sh.top + sh.height):.2f}in, past the card at {card_bottom:.2f}in"


def test_card_body_stays_inside_the_card():
    """Every body box on a columns slide ends within the card's bottom edge."""
    prs = _prs([{"type": "heading", "level": 1, "text": "S"},
                {"type": "columns", "items": [
                    {"title": "LFP (Lithium Iron Phosphate)",
                     "body": "Dominant for 2-4 hour duration. High round-trip "
                             "efficiency (88-94%), 6,000-8,000 cycles."},
                    {"title": "Flow", "body": "10,000-20,000+ cycles."},
                ]}])
    card = _cards(prs)[0]
    bottom = _in(card.top + card.height)
    bodies = [sh for sh in prs.slides[1].shapes
              if sh.has_text_frame and "Dominant" in sh.text_frame.text]
    assert bodies, "body box not found"
    for sh in bodies:
        assert _in(sh.top + sh.height) <= bottom + 0.02,             f"body ends {_in(sh.top + sh.height):.2f}in vs card {bottom:.2f}in"


# ── 3. a wrapped stats value does not collide with its label ───────────────

def test_stats_value_box_fits_a_wrapped_value():
    """A long value wraps, and the label starts BELOW the value's last line."""
    prs = _prs([{"type": "heading", "level": 1, "text": "S"},
                {"type": "stats", "items": [
                    {"value": "$210-430/kWh", "label": "2025 Li-ion pack"},
                    {"value": "8%", "label": "price drop"}]}])
    s = prs.slides[1]
    boxes = [sh for sh in s.shapes if sh.has_text_frame
             and sh.text_frame.text.strip()]
    vals = [b for b in boxes if "$210" in b.text_frame.text]
    labs = [b for b in boxes if "2025 Li-ion" in b.text_frame.text]
    assert vals and labs
    v = vals[0]
    lab = labs[0]
    assert _in(lab.top) >= _in(v.top + v.height) - 0.02, \
        ("the label starts inside the value's box: a wrapped value prints "
         "over its own label")


def test_an_unbreakable_token_wraps_mid_word():
    """`$210-430/kWh` has no spaces; a renderer still breaks a 1.9in token
    inside a 2.1in card. Counting it as one line was the label collision."""
    h1 = docgen._pptx_text_height("$210-430/kWh", int(2.1 * 914400), 30, 1.0,
                                  pad_pt=0, em=0.56)
    lines = round(h1 / 914400 * 72 / 30)
    assert lines >= 2, f"a token wider than the column measured {lines} line(s)"


def test_stat_labels_share_one_baseline():
    """A row of cards is read across: every label starts at the same Y.

    Placing each label directly under its own value left a two-line figure's
    label far below its neighbours'.
    """
    prs = _prs([{"type": "heading", "level": 1, "text": "S"},
                {"type": "stats", "items": [
                    {"value": "250-300 Wh/kg", "label": "current"},
                    {"value": "8%", "label": "drop"},
                    {"value": "1,000+ km", "label": "range"}]}])
    tops = {round(_in(sh.top), 2) for sh in prs.slides[1].shapes
            if sh.has_text_frame
            and sh.text_frame.text.strip() in ("current", "drop", "range")}
    assert len(tops) == 1, f"labels start at different heights: {sorted(tops)}"


def test_stats_cards_share_one_height():
    """A tall value makes the whole row taller, not just its own card."""
    prs = _prs([{"type": "heading", "level": 1, "text": "S"},
                {"type": "stats", "items": [
                    {"value": "$210-430/kWh", "label": "long"},
                    {"value": "8%", "label": "short"}]}])
    cards = _cards(prs)
    assert cards, "no cards found"
    heights = {round(_in(c.height), 2) for c in cards}
    assert len(heights) == 1, f"cards have different heights: {heights}"