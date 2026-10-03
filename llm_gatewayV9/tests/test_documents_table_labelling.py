"""A wide table must not require counting pipes to find a column.

Found live on the user's weekly-menu PDF. The parser had correctly recovered
`Meal | Monday | ... | Sunday` with the right dish in every cell, but the chunk
text rendered each meal as one long pipe-delimited line. Asked for THURSDAY
breakfast, the model returned the FRIDAY column verbatim - the row read
correctly and was indexed one column off - while lunch and dinner for the same
day came back right. The parser was not at fault; the rendering was.

So the rule these tests pin: when a table has three or more named columns, every
cell carries its column name in the chunk text.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from documents.chunker import chunk_blocks  # noqa: E402
from documents.parsers import Block  # noqa: E402

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday",
        "Friday", "Saturday", "Sunday"]


def _menu_block():
    """One row per meal, each day's dishes distinct enough to tell apart."""
    meals = {
        "BREAKFAST": ["mon-bf", "tue-bf", "wed-bf", "thu-bf", "fri-bf",
                      "sat-bf", "sun-bf"],
        "LUNCH": ["mon-lu", "tue-lu", "wed-lu", "thu-lu", "fri-lu",
                  "sat-lu", "sun-lu"],
        "DINNER": ["mon-di", "tue-di", "wed-di", "thu-di", "fri-di",
                   "sat-di", "sun-di"],
    }
    rows = [["Meal"] + DAYS]
    for meal, cells in meals.items():
        rows.append([meal] + cells)
    return Block(kind="table", header=DAYS and ["Meal"] + DAYS, rows=rows,
                 text="\n".join(" | ".join(r) for r in rows))


def _text():
    return "\n".join(c.text for c in chunk_blocks([_menu_block()],
                                                  target_words=150))


def test_every_column_name_appears_in_the_chunk_text():
    text = _text()
    for name in ["Meal"] + DAYS:
        assert name in text, f"{name} missing from chunk text"


def test_each_days_cell_is_labelled_with_its_own_day():
    """This is the fix. `thu-bf` must sit on a line that says Thursday, so no
    positional counting is needed to find it."""
    text = _text()
    for day, token in [("Monday", "mon-bf"), ("Thursday", "thu-bf"),
                       ("Friday", "fri-bf"), ("Sunday", "sun-bf")]:
        line = next((ln for ln in text.splitlines()
                     if token in ln), None)
        assert line is not None, f"{token} not present at all"
        assert day in line, f"{token} is not labelled {day}: {line!r}"


def test_the_wrong_day_is_not_attached_to_a_cell():
    text = _text()
    friday_breakfast = next(ln for ln in text.splitlines() if "fri-bf" in ln)
    assert "Friday" in friday_breakfast
    # The exact failure mode: a Thursday question returning Friday's column.
    for other in ("Monday", "Tuesday", "Wednesday", "Saturday", "Sunday"):
        assert other not in friday_breakfast, friday_breakfast


def test_meal_rows_stay_grouped_under_their_own_label():
    text = _text()
    for meal, token in [("BREAKFAST", "mon-bf"), ("LUNCH", "mon-lu"),
                        ("DINNER", "mon-di")]:
        block = text.split(f"Meal: {meal}")
        assert len(block) == 2, f"{meal} appears {len(block) - 1}x"
        assert token in block[1], meal


def test_a_narrow_table_keeps_its_pipe_form():
    """Two-column tables have no counting problem; changing them would churn
    every existing financial/inventory table for nothing."""
    b = Block(kind="table", header=["item", "amount"],
              rows=[["item", "amount"], ["rent", "1200"], ["power", "90"]],
              text="ignored")
    text = "\n".join(c.text for c in chunk_blocks([b], target_words=150))
    assert "item | amount" in text, text
    assert "rent | 1200" in text, text


def test_a_two_column_table_is_not_labelled():
    b = Block(kind="table", header=["item", "amount"],
              rows=[["item", "amount"], ["rent", "1200"]],
              text="ignored")
    text = "\n".join(c.text for c in chunk_blocks([b], target_words=150))
    assert "- item: rent" not in text, text


def test_an_unnamed_column_falls_back_to_pipes():
    """A blank header cell means the columns cannot be named, so fall back
    rather than emit `- : value`."""
    b = Block(kind="table", header=["", "a", "b", "c"],
              rows=[["", "a", "b", "c"], ["r", "1", "2", "3"]],
              text="ignored")
    text = "\n".join(c.text for c in chunk_blocks([b], target_words=150))
    assert "- : " not in text, text
    assert "| a | b | c" in text, text


def test_empty_cells_are_omitted_not_rendered_as_blanks():
    b = Block(kind="table", header=["Meal", "Monday", "Tuesday"],
              rows=[["Meal", "Monday", "Tuesday"],
                    ["LUNCH", "Rice", ""],
                    ["DINNER", "Dal", "Roti"]],
              text="ignored")
    text = "\n".join(c.text for c in chunk_blocks([b], target_words=150))
    tuesdays = [ln for ln in text.splitlines() if ln.startswith("- Tuesday:")]
    # One real value, so exactly one line - the blank lunch cell adds none and
    # must not appear as "- Tuesday:" with nothing after it.
    assert len(tuesdays) == 1, tuesdays
    assert tuesdays[0] == "- Tuesday: Roti", tuesdays
    assert "- Monday: Rice" in text, text
    assert "- Monday: Dal" in text, text


def test_a_row_with_no_data_at_all_is_skipped():
    b = Block(kind="table", header=["Meal", "Monday", "Tuesday"],
              rows=[["Meal", "Monday", "Tuesday"],
                    ["BREAKFAST", "", ""],
                    ["LUNCH", "Rice", "Dal"]],
              text="ignored")
    text = "\n".join(c.text for c in chunk_blocks([b], target_words=150))
    assert "BREAKFAST" not in text, text
    assert "LUNCH" in text, text


def test_page_and_heading_path_survive_labelled_rendering():
    """The breadcrumb comes from a preceding heading block, so one is needed for
    there to be a path to preserve."""
    b = _menu_block()
    b.page = 2
    heading = Block(kind="heading", text="Mess Menu", level=1)
    chunks = [c for c in chunk_blocks([heading, b], target_words=150)
              if c.kind == "table"]
    assert chunks, "no table chunk produced"
    for c in chunks:
        assert c.page == 2, c.page
        assert c.heading_path == ("Mess Menu",), c.heading_path


def test_the_labelled_form_is_not_enormous():
    """Labelling repeats the column names, so the chunk grows. It must stay
    proportionate - not blow the retrieval budget by an order of magnitude."""
    text = _text()
    plain = len(_menu_block().text)
    assert len(text) < plain * 3, (len(text), plain)
