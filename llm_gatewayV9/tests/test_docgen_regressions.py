"""Regression tests for the docgen bugs the audit surfaced."""
import io
import zipfile

import docgen


def _gen(fmt, spec):
    data, name, _ctype = docgen.generate(fmt, spec)
    return data


def _sheet_cells(blob, path="xl/worksheets/sheet1.xml"):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        return z.read(path).decode("utf-8").count("<c ")


def _workbook_xml(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        return z.read("xl/workbook.xml").decode("utf-8")


def test_empty_sheets_list_still_renders_table_blocks():
    """The real bug: `sheets: []` plus real `table` blocks produced a workbook
    with a blank Sheet1 and nothing else, because the table-branch guard was
    `isinstance(sheets_in, list) and wb.sheetnames` and the workbook had just
    had its only sheet removed. Result: 0 cells, 2 sheets, both empty."""
    blob = _gen("xlsx", {
        "title": "Comparison",
        "blocks": [
            {"type": "heading", "text": "IVF vs HNSW"},
            {"type": "table",
             "header": ["Feature", "IVF", "HNSW"],
             "rows": [["Recall", "moderate", "high"],
                      ["Memory", "low", "high"]]},
        ],
        "sheets": [],
    })
    assert _sheet_cells(blob) > 0, "workbook shipped with no cells at all"


def test_workbook_opens_on_a_populated_sheet():
    """activeTab must point at real content, not a blank leading sheet."""
    blob = _gen("xlsx", {
        "title": "Book",
        "blocks": [{"type": "table", "header": ["A", "B"],
                    "rows": [["1", "2"]]}],
        "sheets": [],
    })
    assert _sheet_cells(blob) > 0
    # No blank leading sheet: the first sheet in the file holds the data.
    assert "Sheet1" not in _workbook_xml(blob)


def test_explicit_sheets_still_win():
    blob = _gen("xlsx", {
        "title": "Book",
        "blocks": [{"type": "table", "header": ["Ignored"], "rows": [["x"]]}],
        "sheets": [{"name": "Real", "header": ["H1"], "rows": [["a"]]}],
    })
    wb = _workbook_xml(blob)
    assert 'name="Real"' in wb
    assert "Ignored" not in wb


def test_single_bullet_with_text_is_not_dropped():
    """The root cause of the empty decks. `_norm_blocks` read only `items`,
    so `{"type": "bullet", "text": "..."}` - which is what a model sends when
    it has not been shown the JSON shape - was silently discarded. A 10-slide
    deck request produced 11 slides whose only text was a speaker note."""
    blocks = [{"type": "heading", "text": "Trade-offs"}]
    blocks += [{"type": "bullet", "text": f"point {i}"} for i in range(1, 4)]
    normalized = docgen._norm_blocks(blocks)
    kinds = [b["type"] for b in normalized]
    assert "bullets" in kinds, f"bullets dropped: {kinds}"
    # Three separate single-item bullet blocks stay three blocks; none vanish.
    got = [i for b in normalized if b["type"] == "bullets" for i in b["items"]]
    assert got == ["point 1", "point 2", "point 3"], got


def test_speaker_notes_go_to_the_notes_slide_not_the_slide_face():
    blob = _gen("pptx", {
        "title": "Deck",
        "blocks": [{"type": "heading", "text": "Overview"},
                   {"type": "note", "text": "Welcome the team."}],
    })
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        notes = " ".join(z.read(n).decode("utf-8", "replace")
                         for n in z.namelist()
                         if n.startswith("ppt/notesSlides/"))
    assert "Welcome the team." in notes


def test_unknown_block_types_are_recorded_not_silently_ignored():
    blob = _gen("pptx", {"title": "Deck",
                         "blocks": [{"type": "wat", "text": "x"}]})
    dropped = docgen.dropped_blocks()
    assert any("wat" in d for d in dropped), f"not reported: {dropped}"
    assert blob[:2] == b"PK"


def test_rendered_deck_contains_its_own_bullets():
    blob = _gen("pptx", {
        "title": "Stack Overview",
        "blocks": [{"type": "heading", "text": "Core Indexing: HNSW"},
                   {"type": "bullets", "items": ["graph based", "log memory"]},
                   {"type": "heading", "text": "Trade-offs"},
                   {"type": "bullets", "items": ["memory heavy", "slow build"]}],
    })
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        text = " ".join(z.read(n).decode("utf-8", "replace")
                        for n in z.namelist()
                        if n.startswith("ppt/slides/slide"))
    for want in ("Core Indexing", "graph based", "log memory",
                 "Trade-offs", "memory heavy", "slow build"):
        assert want in text, f"{want!r} missing from the rendered deck"
