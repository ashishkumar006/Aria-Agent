"""Coordinate-based PDF table reconstruction.

The failure this exists to prevent was invisible: the physical-grid path
produced a day-to-dish mapping that *looked* right while breakfast cells had
absorbed the top of the lunch band. It was only caught by printing the page's
own text lines with their y coordinates and comparing.

Two independent defects, both pinned here:

1. **Row labels are not vertically centred.** On the real menu, BREAKFAST sits
   mid-band but LUNCH, SNACKS and DINNER all sit at the *bottom* of theirs.
   "Assign to the nearest label" therefore pulled the first line of each band
   into the band above it - which is exactly the reported symptom, lunch items
   appearing under breakfast. Band cuts now come from the whitespace between
   bands, and the labels only supply their names and order.
2. **A band's own label leaked into its first cell.** Gutter words fell through
   the column assignment, so Monday's breakfast literally began "Brinjal
   BREAKFAST Sambar".

Plus: a cell's text must not contain a word that belongs to the gutter at all,
and words must stay whole (the grid path split "Chana" into "Ch ana" and
"100gm" into "1 00gm").
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from documents import parsers as P  # noqa: E402

HEADER_Y = 99.3
# Measured from the real file: label centres, and the three widest gaps between
# text lines, which fall at y=238.6, 372.2 and 406.9.
BREAKFAST_Y, LUNCH_Y, SNACKS_Y, DINNER_Y = 192.0, 352.7, 389.4, 507.4

COLS = {"Monday": 138.3, "Tuesday": 259.2, "Wednesday": 358.1,
        "Thursday": 480.0, "Friday": 604.9, "Saturday": 728.8,
        "Sunday": 833.7}


def _word(text, x0, y, x1=None):
    return {"text": text, "x0": float(x0), "x1": float(x1 if x1 is not None
                                                     else x0 + 6 * len(text)),
            "top": float(y) - 5, "bottom": float(y) + 5,
            "width": 0.0, "height": 10.0}


def _menu_words():
    """A miniature with the real geometry.

    Two properties of the source page are reproduced deliberately, because both
    broke the first implementation:

    * labels sit LOW in their band - LUNCH, SNACKS and DINNER are at the very
      bottom of theirs, so "nearest label" mis-assigns the band above;
    * leading is ~10pt inside a band and ~13-19pt between bands, so the widest
      gap inside a label window really is the separator. A fixture with airy
      internal spacing would test a document that does not exist.
    """
    ws = [_word("Protein Mess Menu", 404, 80)]
    for name, x in COLS.items():                       # header line
        ws.append(_word(name, x, HEADER_Y))

    # BREAKFAST band: content 124..220, label at 192 (near its end).
    for y in (128, 142, 153, 163, 173, 183, 193, 203, 213):
        for x in COLS.values():
            ws.append(_word("bf", x, y))
    ws.append(_word("BREAKFAST", 47, BREAKFAST_Y))

    # LUNCH band: content 238..356, label at 352.7 (at its end).
    for y in (240, 252, 262, 272, 282, 292, 302, 312, 322, 332, 342, 356):
        for x in COLS.values():
            ws.append(_word("lu", x, y))
    ws.append(_word("LUNCH", 47, LUNCH_Y))

    # SNACKS band: 372..384, label at 389.4 (just below it). The internal gap
    # is kept well under the 16pt that separates it from LUNCH, as on the real
    # page (16.2pt separator against a 13.5pt internal gap).
    for y in (372, 384):
        for x in COLS.values():
            ws.append(_word("sn", x, y))
    ws.append(_word("SNACKS", 47, SNACKS_Y))

    # DINNER band: 406..494, label at 507.4 (well below it).
    for y in (410, 420, 430, 440, 450, 460, 470, 480, 490):
        for x in COLS.values():
            ws.append(_word("dn", x, y))
    ws.append(_word("DINNER", 47, DINNER_Y))
    return ws


@pytest.fixture
def table():
    out = P._layout_table(_menu_words())
    assert out is not None, "the layout table was not recognised"
    return out


def test_header_is_the_row_of_short_names(table):
    header, _rows = table
    assert header == ["Meal"] + list(COLS), header


def test_every_meal_becomes_a_row(table):
    _header, rows = table
    assert [r[0] for r in rows] == ["BREAKFAST", "LUNCH", "SNACKS",
                                    "DINNER"], [r[0] for r in rows]


def test_a_label_sitting_low_in_its_band_does_not_steal_the_next_band(table):
    """The reported bug. LUNCH's label is at y=352.7, at the *end* of a band
    that runs 240..356, so a nearest-label rule hands 240-352 to LUNCH and
    leaves BREAKFAST with almost nothing - and pushes the rest of the table the
    wrong way across bands."""
    _header, rows = table
    cells = {r[0]: dict(zip(table[0][1:], r[1:])) for r in rows}
    assert cells["BREAKFAST"]["Monday"].count("bf") == 9, cells["BREAKFAST"]
    assert cells["LUNCH"]["Monday"].count("lu") == 12, cells["LUNCH"]
    assert cells["SNACKS"]["Monday"].count("sn") == 2, cells["SNACKS"]
    assert cells["DINNER"]["Monday"].count("dn") == 9, cells["DINNER"]


def test_no_content_leaks_across_a_band(table):
    _header, rows = table
    own = {"BREAKFAST": "bf", "LUNCH": "lu", "SNACKS": "sn", "DINNER": "dn"}
    for row in rows:
        # Tokens are whole words, so a leak shows up as a neighbour's token.
        foreign = {t for name, t in own.items() if name != row[0]}
        words = set(" ".join(row).split())
        for word in foreign & words:
            assert word not in words, f"{word} leaked into {row[0]}: {row}"


def test_the_gutter_label_never_appears_inside_a_cell(table):
    """It is already the row label; letting it fall through to the first column
    made Monday's breakfast read "Brinjal BREAKFAST Sambar"."""
    _header, rows = table
    flat = " ".join(" ".join(r) for r in rows)
    for label in ("BREAKFAST", "LUNCH", "SNACKS", "DINNER"):
        assert flat.count(label) == 1, (label, flat)


def test_each_day_gets_its_own_cell(table):
    _header, rows = table
    cells = {r[0]: dict(zip(_header[1:], r[1:])) for r in rows}
    for day in COLS:
        assert cells["BREAKFAST"][day].count("bf") == 9, (day, cells["BREAKFAST"])
        assert cells["DINNER"][day].count("dn") == 9, (day, cells["DINNER"])


def test_an_overlong_cell_may_overflow_its_own_column():
    """Monday's longest real entry reaches x=244 while Tuesday starts at 259.
    Cells are left-aligned under their header, so a word belongs to the last
    header at or before its own x0 - a cell that overruns must not be handed to
    the next day."""
    ws = _menu_words()
    ws.append(_word("overflowing", 240, 153))          # Monday's column
    out = P._layout_table(ws)
    assert out is not None
    header, rows = out
    cells = {r[0]: dict(zip(header[1:], r[1:])) for r in rows}
    assert "overflowing" in cells["BREAKFAST"]["Monday"], cells["BREAKFAST"]
    assert "overflowing" not in cells["BREAKFAST"]["Tuesday"], cells["BREAKFAST"]


def test_words_are_not_split_at_intra_word_gaps():
    """The grid path's text alignment cut "Chana" to "Ch ana" and "100gm" to
    "1 00gm", because a wide intra-word gap read as a column edge. Working from
    `extract_words` output keeps them whole."""
    ws = _menu_words()
    ws.append(_word("Chana", 140, 173))
    ws.append(_word("100gm", 150, 173))
    out = P._layout_table(ws)
    assert out is not None
    header, rows = out
    cells = {r[0]: dict(zip(header[1:], r[1:])) for r in rows}
    bf = cells["BREAKFAST"]["Monday"]
    assert "Chana" in bf, bf
    assert "100gm" in bf, bf
    assert "Ch ana" not in bf, bf
    assert "1 00gm" not in bf, bf


def test_the_title_is_not_mistaken_for_the_header():
    """"Protein Mess Menu" is above the day names and is not a row of short
    single words - it must not become the column header."""
    header, _rows = P._layout_table(_menu_words())
    assert "Protein" not in header, header
    assert header[1] == "Monday", header


def test_a_punctuation_free_title_is_not_mistaken_for_the_header():
    """A centred report title satisfies every shape test - three single alpha
    words, well separated - so it became the header, dragged the gutter boundary
    to the title's left edge, and silently deleted every real column to its
    left. On a five-day menu that lost 40% of the cells with no warning, and
    because `parse` then sets `table_only`, the text was indexed nowhere.

    The shipped file escapes only by luck: its title carries a colon and a
    parenthesis, so `_is_header_token` rejects it.
    """
    ws = _menu_words()
    # Drop the fixture's own title line so the only candidate above the real
    # header is the punctuation-free one, as in the reproduction.
    ws = [w for w in ws if w["text"] != "Protein Mess Menu"]
    ws = [_word("Weekly", 404, 70), _word("Mess", 449, 70),
          _word("Menu", 482, 70)] + ws
    out = P._layout_table(ws)
    assert out is not None, "the real header should still be found"
    header, rows = out
    assert header == ["Meal"] + list(COLS), header
    assert "Weekly" not in header, header
    # No column may be silently emptied.
    for row in rows:
        assert all(c for c in row[1:]), row


def test_a_title_with_no_data_under_it_is_declined():
    """If the only candidate is a title, there is no table to recover."""
    ws = [_word("Weekly", 404, 70), _word("Mess", 449, 70),
          _word("Menu", 482, 70)]
    ws += [_word("Some", 100 + 9 * i, 100 + 11 * i) for i in range(40)]
    assert P._layout_table(ws) is None


def test_a_band_separator_must_beat_the_next_widest_gap(table):
    """The widest gap in a label's window wins only if it is a clear outlier. A
    blank line inside a band can be as wide as the real separator, and the
    tightest genuine separator on the shipped menu beats its runner-up by just
    16% - so with no margin check an ordinary reflow would cut a band in half and
    serve lunch dishes as snacks. When the two are indistinguishable the honest
    answer is to decline.

    The base fixture separates LUNCH from SNACKS by 16pt against a 12pt
    internal gap (ratio 1.33). Widening that internal gap to 15pt makes the two
    indistinguishable (ratio 1.07), and the parser must refuse.
    """
    ws = [w for w in _menu_words() if not (w["text"] == "sn"
                                           and abs(w["top"] - 379.0) < 0.01)]
    for x in COLS.values():
        ws.append(_word("sn", x, 387))
    out = P._layout_table(ws)
    assert out is None, (
        "a separator indistinguishable from an internal gap must be declined, "
        "not guessed")


def test_prose_is_declined_rather_than_given_a_fake_grid():
    ws = [_word("Lorem", 100 + 8 * i, 100 + 14 * i) for i in range(60)]
    assert P._layout_table(ws) is None


def test_too_few_words_is_declined():
    assert P._layout_table([_word("hello", 10, 10)]) is None
    assert P._layout_table([]) is None


def test_a_table_with_no_row_labels_is_declined():
    """Header but no gutter labels means we cannot name the bands, and guessing
    would silently mislabel every row."""
    ws = []
    for name, x in COLS.items():
        ws.append(_word(name, x, HEADER_Y))
    for x in COLS.values():
        for y in (140, 250, 380):
            ws.append(_word("item", x, y))
    assert P._layout_table(ws) is None


def test_a_repeated_label_on_two_lines_is_one_row():
    ws = _menu_words()
    ws.append(_word("BREAKFAST", 47, 196))            # wrapped label
    header, rows = P._layout_table(ws)
    assert [r[0] for r in rows].count("BREAKFAST") == 1, [r[0] for r in rows]


def test_a_single_band_is_declined():
    ws = [_word("Protein Mess Menu", 404, 80)]
    for name, x in COLS.items():
        ws.append(_word(name, x, HEADER_Y))
    ws.append(_word("BREAKFAST", 47, BREAKFAST_Y))
    for x in COLS.values():
        ws.append(_word("only", x, 150))
    assert P._layout_table(ws) is None
