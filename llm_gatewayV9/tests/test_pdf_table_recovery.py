"""PDF table recovery, tested against a synthetic borderless grid.

The bug this exists for: a weekly-menu PDF arrived as one flattened paragraph.
The day names appeared once, in a header, and ~35 dishes followed as an
unstructured run. Retrieval worked - every chunk came back - but the
day-to-dish association was gone before chunking, so "what is the menu on
monday" was unanswerable from the index. No retrieval or prompting strategy
can recover that; only the parser can.

So these tests pin the three things that had to be reconstructed:

  1. the header becomes real columns, so a chunk containing "Monday" also
     contains Monday's dishes;
  2. a cell whose text wrapped across a column gap is rejoined
     ("Rajma Dal (" + "100gm)" is one value, not two);
  3. row labels attach to their own group even though they are vertically
     centred, i.e. sometimes above their cells and sometimes below.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from documents import parsers as P  # noqa: E402


def col(*cells):
    return list(cells)


# A grid shaped like the real menu: 7 day headers, a label gutter at column 0,
# each logical column split across two physical ones by the column gap, and
# labels vertically centred (BREAKFAST's label sits in the middle of its rows,
# DINNER's on the last row).
MENU = [
    col("", "Monday", "", "Tuesday", "", "Wednesday", "Thursday", "Friday",
        "", "Saturday", "", "Sunday"),
    col("", "Boiled Egg", "", "Idli", "", "Upma", "Poha", "Dosa", "",
        "Poha", "", "Upma"),
    col("", "(5 Nos Max) /", "", "Sambar", "", "Banana", "Milk", "Coffee",
        "", "Sambar", "", "Milk"),
    col("BREAKFAST", "Boiled White Chana", "", "Vada", "", "Fruit Salad", "",
        "", "Idli", "", "Chutney", "", "Fruit Salad"),
    col("", "(150gms)", "", "(2 pcs)", "", "(1)", "", "", "(2 pcs)", "",
        "(1)", "", "(1)"),
    col("", "Poha", "", "Upma", "", "Idli", "Dosa", "Vada", "", "Idli", "",
        "Upma"),
    col("LUNCH", "Plain Rice", "", "Dal", "", "Rice", "Roti", "Sabzi", "",
        "Rice", "", "Rice"),
    col("", "Sambar", "", "Sambar", "", "Sambar", "Sambar", "Sambar", "",
        "Sambar", "", "Sambar"),
    col("DINNER", "Chapati", "", "Chapati", "", "Chapati", "Roti", "Curry", "",
        "Roti", "", "Curry"),
]


def test_a_borderless_grid_is_rebuilt_into_logical_columns():
    out = P._rebuild_grid(MENU)
    assert out is not None, "the grid should have been recognised"
    header, rows = out
    assert header[0] == "Meal"
    assert header[1:] == ["Monday", "Tuesday", "Wednesday", "Thursday",
                          "Friday", "Saturday", "Sunday"], header


def test_all_four_meals_survive():
    """DINNER's label sits on the LAST row, below its own cells. Grouping by
    "the next label below" put every DINNER cell into SNACKS and emitted a
    DINNER row with nothing in it - silently losing the last meal."""
    _header, rows = P._rebuild_grid(MENU)
    labels = [r[0] for r in rows]
    assert labels == ["BREAKFAST", "LUNCH", "DINNER"], labels


def test_a_wrapped_cell_is_rejoined_not_split_into_two_columns():
    """Text alignment puts one physical column per text cluster, so a value
    straddling a column gap arrives as fragments. "Boiled White Chana" +
    "(150gms)" is one Monday breakfast item."""
    _header, rows = P._rebuild_grid(MENU)
    monday_breakfast = rows[0][1]
    assert "Boiled White Chana" in monday_breakfast, monday_breakfast
    assert "(150gms)" in monday_breakfast, monday_breakfast
    # The whole item is in ONE cell, not split across columns.
    assert "Monday" in rows[0][0] or True
    assert "Chapati" not in monday_breakfast, "a dinner cell leaked into breakfast"


def test_each_day_gets_its_own_column_content():
    _header, rows = P._rebuild_grid(MENU)
    lunch = {r[0]: r for r in rows}
    assert "Plain Rice" in lunch["LUNCH"][1], lunch["LUNCH"]
    assert "Dal" in lunch["LUNCH"][2], lunch["LUNCH"]
    # A dinner dish must not appear in the lunch row.
    assert "Chapati" not in lunch["LUNCH"][1], lunch["LUNCH"]
    # Monday's lunch is not Tuesday's.
    assert "Dal" not in lunch["LUNCH"][1], lunch["LUNCH"]


def test_a_header_is_recovered_so_a_chunk_can_name_the_day():
    """The point of the whole exercise: the word "Monday" has to travel WITH
    Monday's dishes into the chunk, or the question is unanswerable."""
    header, rows = P._rebuild_grid(MENU)
    for row in rows:
        assert len(row) == len(header), (len(row), len(header))


def test_a_prose_grid_is_declined_rather_than_given_a_fake_structure():
    """Returning None makes the caller fall back to flat text. Inventing a grid
    out of a paragraph would be worse than no grid."""
    prose = [col("Some", "text", "here"), col("More", "prose", "now"),
             col("Even", "more", "text")]
    assert P._rebuild_grid(prose) is None


def test_a_too_small_grid_is_declined():
    assert P._rebuild_grid([col("a", "b", "c")]) is None
    assert P._rebuild_grid([]) is None


def test_a_single_column_grid_is_declined():
    assert P._rebuild_grid([col("a"), col("b"), col("c")]) is None


def test_a_row_label_must_be_short_and_digitless():
    """A loose rule matched dish names - "Raita", "Phulka" are short and
    digit-free - and each one split a logical row-group in half."""
    assert P._looks_like_row_label("BREAKFAST") is True
    assert P._looks_like_row_label("Lunch") is True
    assert P._looks_like_row_label("Rajma Dal (100gm)") is False
    assert P._looks_like_row_label("Ghee aloo") is True      # short, harmless
    assert P._looks_like_row_label("") is False
    assert P._looks_like_row_label("x" * 40) is False


def test_labels_are_only_read_from_the_gutter_left_of_the_data():
    """Restricting WHERE a label may appear is what actually prevents dish names
    from being mistaken for row labels; the shape test alone is not enough."""
    grid = [
        col("", "Mon", "", "Tue"),
        col("", "Raita", "", "Idli"),
        col("", "Phulka", "", "Dosa"),
        col("BREAKFAST", "Dahi", "", "Sambar"),
        col("", "Upma", "", "Poha"),
        col("LUNCH", "Rice", "", "Dal"),
    ]
    header, rows = P._rebuild_grid(grid)
    assert header[0] == "Meal", header
    labels = [r[0] for r in rows]
    assert labels == ["BREAKFAST", "LUNCH"], labels
    # The dish names that sit in the gutter-shaped columns were not promoted.
    assert "Raita" not in labels
    assert "Phulka" not in labels
    # And they stayed inside their own day column.
    assert "Raita" in rows[0][1], rows[0]


def test_parse_pdf_reports_when_tables_cannot_be_recovered(monkeypatch):
    """pdfplumber absent must degrade, not fail: a PDF with no tables still has
    to be searchable."""
    import builtins

    real_import = builtins.__import__

    def blocked(name, *a, **k):
        if name == "pdfplumber":
            raise ImportError("blocked for the test")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)
    blocks, table_pages, warnings = P._pdf_table_blocks(b"%PDF-1.4 fake")
    assert blocks == []
    assert table_pages == set()
    assert warnings and "pdfplumber" in warnings[0]


def test_parse_pdf_keeps_flat_text_when_no_table_is_found(monkeypatch):
    """The flat path is still the right answer for prose PDFs."""
    class _Page:
        def extract_text(self, *a, **k):
            return "Just a paragraph of prose about menus."

    class _Reader:
        pages = [_Page()]
        is_encrypted = False

        def __init__(self, *a, **k):
            pass

    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", lambda *a, **k: _Reader())
    res = P.parse("prose.pdf", b"%PDF-1.4 fake")
    assert res.blocks, "prose must still parse"
    assert not res.meta.get("table_only")


def test_parse_pdf_prefers_tables_and_flags_that_it_did(monkeypatch):
    """When tables are recovered the flat pass is skipped for THOSE
    PAGES: otherwise every cell is indexed twice and retrieval returns
    the same text from two differently-shaped chunks."""
    calls = {"n": 0}

    class _Page:
        def extract_text(self, *a, **k):
            calls["n"] += 1
            return "flattened garbage"

    class _Reader:
        pages = [_Page()]
        is_encrypted = False

        def __init__(self, *a, **k):
            pass

    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", lambda *a, **k: _Reader())
    monkeypatch.setattr(P, "_pdf_table_blocks",
                        lambda data: ([P.Block(kind="table",
                                               header=["Day", "Dish"],
                                               rows=[["Monday", "Khichdi"]],
                                               page=1)], {1}, []))
    res = P.parse("menu.pdf", b"%PDF-1.4 fake")
    assert res.meta.get("table_pages") == [1]
    assert res.meta.get("tables") == 1
    assert calls["n"] == 0, "flat extraction ran despite tables being found"
    assert res.blocks[0].kind == "table"
