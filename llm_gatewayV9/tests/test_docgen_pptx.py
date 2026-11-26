r"""The deck renderer is a layout engine now, not a text placer.

`build_pptx` used to put every block in a fixed-height text box on the Blank
layout: 15pt body on a 13.3x7.5in canvas, `**bold**` stripped, bullets as
literal "• " characters in one text frame, no colour anywhere, and — despite
what prompts/deck.md tells the model — a level-1 heading did NOT start a new
slide. Measured on a live deck: 4 slides for a topic that rendered 23 pages of
PDF, 19 text boxes, 0 tables, 0 charts, 0 font colours.

These tests pin the properties that make a deck readable on a projector, and
the one that matters most: nothing a section says may be dropped or packed.
"""
import io
import re
import zipfile

import pytest

import docgen

Presentation = pytest.importorskip("pptx").Presentation


def _slide_text(s):
    return "\n".join(sh.text_frame.text for sh in s.shapes if sh.has_text_frame)


def _deck(blocks, title="How LLMs Work", **extra):
    spec = {"title": title, "blocks": blocks, **extra}
    blob, name, _ct = docgen.generate("pptx", spec)
    return Presentation(io.BytesIO(blob))


def _all_text(prs):
    out = []
    for s in prs.slides:
        for sh in s.shapes:
            if sh.has_text_frame:
                out.append(sh.text_frame.text)
            if sh.has_table:
                for row in sh.table.rows:
                    for c in row.cells:
                        out.append(c.text)
    return "\n".join(out)


def _titles(prs):
    out = []
    for s in prs.slides:
        for p in s.placeholders:
            try:
                if int(p.placeholder_format.idx) == 0:
                    out.append(p.text_frame.text.strip())
            except Exception:
                pass
    return out


def _sections(n):
    out = []
    for i in range(1, n + 1):
        out.append({"type": "heading", "level": 1, "text": f"Section {i}"})
        out.append({"type": "bullets",
                    "items": [f"point {i}.{j}" for j in range(1, 4)]})
    return out


def test_a_heading_without_a_level_is_still_a_slide_and_never_vanishes():
    """Two bugs the first version of this engine shipped with.

    `_norm_blocks` defaulted a bare `{"type": "heading", "text": ...}` to
    level 2, so it stopped being a slide boundary; and the deck loop had no
    branch for a non-section heading at all, so its text was dropped from the
    file. Both were caught by an existing regression test.
    """
    prs = _deck([{"type": "heading", "text": "Core Indexing: HNSW"},
                 {"type": "bullets", "items": ["graph based", "log memory"]},
                 {"type": "heading", "text": "Trade-offs"},
                 {"type": "bullets", "items": ["memory heavy", "slow build"]}])
    text = _all_text(prs)
    for want in ("Core Indexing", "graph based", "Trade-offs", "slow build"):
        assert want in text, f"{want!r} missing from the rendered deck"
    assert len(prs.slides) == 3, "a bare heading did not open its own slide"


def test_a_pagebreak_does_not_leave_a_blank_slide_behind():
    """A break used to add a slide immediately, and the heading after it added
    another, so every `pagebreak` in a spec produced an empty page."""
    prs = _deck([{"type": "pagebreak"},
                 {"type": "heading", "level": 1, "text": "First"},
                 {"type": "bullets", "items": ["one"]},
                 {"type": "pagebreak"},
                 {"type": "heading", "level": 1, "text": "Second"},
                 {"type": "bullets", "items": ["two"]}])
    assert len(prs.slides) == 3, [s.slide_layout.name for s in prs.slides]
    for i, s in enumerate(prs.slides, 1):
        body = " ".join(sh.text_frame.text for sh in s.shapes
                        if sh.has_text_frame).strip()
        assert body, f"slide {i} is blank"


def test_a_deeper_heading_renders_as_a_subheading():
    prs = _deck([{"type": "heading", "level": 1, "text": "Section"},
                 {"type": "heading", "level": 2, "text": "A sub-topic"},
                 {"type": "bullets", "items": ["detail"]}])
    text = _all_text(prs)
    assert "A sub-topic" in text, text
    assert len(prs.slides) == 2, "a level-2 heading stole its own slide"
    sizes = [r.font.size.pt for s in prs.slides for sh in s.shapes
             if sh.has_text_frame for p in sh.text_frame.paragraphs
             for r in p.runs if r.text.strip() == "A sub-topic"]
    assert sizes and 18 <= sizes[0] < 30, sizes


def test_a_level_one_heading_always_opens_a_slide():
    """The contract prompts/deck.md makes to the model.

    Six sections used to arrive as three slides, two sections apiece, because a
    heading only rolled the page when the old fixed-height cursor ran out.
    """
    prs = _deck(_sections(6))
    assert len(prs.slides) == 7, f"{len(prs.slides)} slides for 6 sections"
    titles = _titles(prs)
    for i in range(1, 7):
        assert f"Section {i}" in titles, titles


def test_slide_titles_are_real_placeholders():
    """Blank layout + floating text boxes meant no theme and an empty Outline."""
    prs = _deck(_sections(2))
    for s in list(prs.slides)[1:]:
        assert s.slide_layout.name != "Blank", "titles are back to text boxes"
        assert _titles_type(s), "no title placeholder on a content slide"


def test_the_deck_title_is_said_once():
    """Blocks before the first heading do not get the deck title as their own.

    The closing group used to fall back to `title or "Presentation"`, so a lead
    paragraph (the deck's own subtitle, which the title slide already shows)
    arrived as slide 2 titled exactly like slide 1. Measured on the live deck
    ac61a8ea8710f5f3: slide 1 and slide 2 both read "Large Language Models:
    Technical Foundations".
    """
    blocks = ([{"type": "paragraph", "text": "An engineering overview."}]
              + _sections(3))
    prs = _deck(blocks, subtitle="An engineering overview.")
    titles = _titles(prs)
    assert titles.count("How LLMs Work") == 1, titles
    assert "An engineering overview." in _all_text(prs), "lead text dropped"
    for i in range(1, 4):
        assert f"Section {i}" in titles, titles


def test_an_untitled_group_leaves_no_empty_placeholder_behind():
    """The title band is reclaimed, not left as a gap above the body.

    Two lead paragraphs: the first becomes the cover line, the second is a
    heading-less group and gets a slide with no title placeholder at all.
    """
    prs = _deck([{"type": "paragraph", "text": "Framing line."},
                 {"type": "paragraph", "text": "A second lead thought."}]
                + _sections(1))
    lead = list(prs.slides)[1]
    assert "A second lead thought." in _slide_text(lead)
    assert [p for p in lead.placeholders
            if not p.text_frame.text.strip()] == []
    tops = [sh.top for sh in lead.shapes if sh.has_text_frame
            and sh.text_frame.text.strip()]
    assert min(tops) < docgen._PPTX_IN * 3.0, \
        f"body starts at {min(tops)} EMU — an empty title band is still there"


def test_a_title_slide_written_as_a_heading_makes_one_slide():
    """The live cause, on run ac61a8ea8710f5f3.

    prompts/deck.md says "Title slide: the deck's title as the `title`", and the
    model also wrote that title as the first level-1 heading of its blocks. That
    heading opened slide 2 titled exactly like slide 1, with the cover sentence
    under it. A heading that restates the cover opens no slide.
    """
    blocks = ([{"type": "heading", "level": 1, "text": "How LLMs Work"},
               {"type": "paragraph", "text": "An engineering overview."}]
              + _sections(3))
    prs = _deck(blocks, subtitle="An engineering overview.")
    titles = _titles(prs)
    assert titles.count("How LLMs Work") == 1, titles
    assert titles[0] == "How LLMs Work", titles
    assert titles[1] == "Section 1", f"slide 2 is {titles[1]!r}"
    assert len(prs.slides) == 4, f"{len(prs.slides)} slides for 3 sections + cover"
    assert "An engineering overview." in _all_text(prs), "cover text lost"


def test_a_cover_written_as_a_block_still_makes_a_title_slide():
    """`cover` used to be `continue`d — the deck opened with no title at all."""
    prs = _deck([{"type": "cover", "title": "Vector Search",
                  "subtitle": "A field guide"},
                 {"type": "heading", "level": 1, "text": "Why recall matters"},
                 {"type": "bullets", "items": ["Top-k is a business metric"]}],
                title="")
    first = list(prs.slides)[0]
    assert first.slide_layout.name == "Title Slide", first.slide_layout.name
    blob = _all_text(prs)
    for text in ("Vector Search", "A field guide", "Top-k is a business metric"):
        assert text in blob, f"{text!r} missing"


def test_a_lead_paragraph_becomes_the_cover_line_not_a_second_cover():
    """No `subtitle` field: the first paragraph is the cover line, once."""
    prs = _deck([{"type": "paragraph", "text": "A short framing line."}]
                + _sections(2))
    assert "A short framing line." in _slide_text(list(prs.slides)[0])
    titles = _titles(prs)
    assert len(prs.slides) == 3, f"{len(prs.slides)} slides: {titles}"


def _titles_type(s):
    return [p for p in s.placeholders
            if str(p.name).lower().startswith("title")
            or (p.placeholder_format.idx == 0)]


def test_body_type_is_sized_for_a_projector():
    prs = _deck(_sections(1))
    H = prs.slide_height
    sizes = []
    for s in list(prs.slides)[1:]:
        for sh in s.shapes:
            if not sh.has_text_frame or sh.top > H - int(0.6 * 914400):
                continue          # the footer band is deliberately small
            for p in sh.text_frame.paragraphs:
                for r in p.runs:
                    if r.text.strip():
                        sizes.append(r.font.size.pt if r.font.size else 0)
    assert sizes, "no runs at all"
    assert min(sizes) >= 14, f"type as small as {min(sizes)}pt on a slide"
    assert max(sizes) >= 24, "slide titles are not visually distinct"


def test_emphasis_and_math_survive():
    prs = _deck([{"type": "heading", "level": 1, "text": "Attention"},
                 {"type": "paragraph",
                  "text": "Queries ($Q$) are **learned** projections, "
                          "scaled by $\\sqrt{d_k}$."}])
    text = _all_text(prs)
    assert "$" not in text and "\\sqrt" not in text, text
    assert "**" not in text, "the asterisks were printed instead of applied"
    assert "Queries (Q) are" in text and "sqrt(d_k)" in text
    bold = [r.text for s in prs.slides for sh in s.shapes
            if sh.has_text_frame for p in sh.text_frame.paragraphs
            for r in p.runs if r.font.bold]
    assert any("learned" in b for b in bold), bold


def test_bullets_are_list_paragraphs_not_glyphs_in_a_blob():
    prs = _deck([{"type": "heading", "level": 1, "text": "List"},
                 {"type": "bullets", "items": ["one", "two", "three"]}])
    frame = None
    for s in prs.slides:
        for sh in s.shapes:
            if sh.has_text_frame and "one" in sh.text_frame.text:
                frame = sh.text_frame
    assert frame is not None
    texts = [p.text for p in frame.paragraphs if p.text]
    assert texts == ["one", "two", "three"], texts
    xml = frame.paragraphs[0]._p.xml
    assert "a:buChar" in xml or "a:buAutoNum" in xml, "no real bullet format"
    assert "•" not in "".join(texts), "manual bullet characters are back"


def test_a_table_gets_an_accented_header():
    prs = _deck([{"type": "heading", "level": 1, "text": "Stages"},
                 {"type": "table", "header": ["Stage", "Data"],
                  "rows": [["Pre-train", "15T"], ["SFT", "200k"]]}])
    tbl = None
    for s in prs.slides:
        for sh in s.shapes:
            if sh.has_table:
                tbl = sh.table
    assert tbl is not None, "the table was dropped"
    head = tbl.cell(0, 0)
    assert head.text.strip() == "Stage"
    fills = {str(c.fill.fore_color.rgb) for c in tbl.rows[0].cells}
    assert fills and next(iter(fills)).upper() not in ("FFFFFF",), \
        f"header row has no accent fill: {fills}"


def test_a_long_list_continues_on_the_next_slide_and_loses_nothing():
    items = [f"item {i} with enough words to take a line or two on its own"
             for i in range(40)]
    prs = _deck([{"type": "heading", "level": 1, "text": "Everything"},
                 {"type": "bullets", "items": items}])
    text = _all_text(prs)
    missing = [i for i in items if i[:24] not in text]
    assert not missing, f"{len(missing)} of {len(items)} bullets lost"
    assert len(prs.slides) >= 2, "40 bullets on one slide is a wall of text"


def test_no_unfilled_placeholder_is_left_behind():
    """An empty placeholder reads as "Click to add text" while editing."""
    prs = _deck(_sections(2), subtitle=None)
    for i, s in enumerate(prs.slides, 1):
        for p in s.placeholders:
            assert p.text_frame.text.strip(), \
                f"slide {i} left placeholder {p.placeholder_format.idx} empty"


def test_speaker_notes_stay_off_the_slide_face():
    prs = _deck([{"type": "heading", "level": 1, "text": "S"},
                 {"type": "bullets", "items": ["a"]},
                 {"type": "note", "text": "Remember to pause here."}])
    assert "Remember to pause here." not in _all_text(prs)
    notes = [s.notes_slide.notes_text_frame.text for s in prs.slides
             if s.has_notes_slide]
    assert any("Remember to pause here." in n for n in notes), notes


def test_nothing_is_printed_over_the_footer():
    """Body content must stop above the footer band, not run into it."""
    prs = _deck(_sections(3))
    H = prs.slide_height
    limit = H - int(0.60 * 914400)      # footer lives below this line
    for i, s in enumerate(prs.slides, 1):
        for sh in s.shapes:
            if not sh.has_text_frame or not sh.text_frame.text.strip():
                continue
            big = any(r.font.size and r.font.size.pt >= 14
                      for p in sh.text_frame.paragraphs for r in p.runs)
            if not big:
                continue                 # the footer itself is exempt
            assert sh.top + sh.height <= limit, (
                f"slide {i}: body text runs to {(sh.top + sh.height)/914400:.2f}in "
                f"of {H/914400:.2f}in")


# ── the visual layer ─────────────────────────────────────────────────────
# Every one of these came off a PowerPoint-exported PNG of a live deck, not
# from a guess: a centred title over a left-aligned rule, four bullets in the
# top third of the canvas with the bottom half white, Office's default red and
# blue series, a square bullet glyph, a quote in plain text, and a chart
# caption that never arrived.

def _shapes(s):
    return list(s.shapes)


def _rule(s):
    """The short horizontal bar under a title, not the deck's edge strip."""
    bars = [sh for sh in _shapes(s)
            if int(sh.height) < docgen._pptx_in(0.10)
            and docgen._pptx_in(0.8) < int(sh.width) < docgen._pptx_in(3.0)]
    assert bars, "no accent rule on the slide"
    return bars[0]


def test_titles_align_with_the_rule_under_them():
    """The layout centres its title placeholder; the rule and body are left."""
    from pptx.enum.text import PP_ALIGN
    prs = _deck(_sections(2))
    for i, s in enumerate(list(prs.slides)[1:], 1):
        ph = [p for p in s.placeholders if int(p.placeholder_format.idx) == 0]
        assert ph, f"slide {i} has no title placeholder"
        para = ph[0].text_frame.paragraphs[0]
        assert para.alignment == PP_ALIGN.LEFT, (
            f"slide {i} title alignment is {para.alignment} while its rule "
            f"sits at the left margin")
        rule = _rule(s)
        assert rule.left == ph[0].left, (
            f"slide {i}: rule at {rule.left} under a title at {ph[0].left}")


def test_the_cover_says_its_title_where_the_rule_is():
    from pptx.enum.text import PP_ALIGN
    prs = _deck(_sections(1), subtitle="A line")
    cover = list(prs.slides)[0]
    ph = [p for p in cover.placeholders if int(p.placeholder_format.idx) == 0][0]
    assert ph.text_frame.paragraphs[0].alignment == PP_ALIGN.LEFT
    assert ph.left >= docgen._pptx_in(0.8), "cover text is against the edge band"


def test_a_short_section_is_not_pinned_to_the_top_of_a_tall_canvas():
    """Three bullets in the top third with 60% white is what read as unfinished."""
    prs = _deck([{"type": "heading", "level": 1, "text": "Short"},
                 {"type": "bullets", "items": ["one", "two", "three"]}])
    s = list(prs.slides)[1]
    box = [sh for sh in _shapes(s)
           if sh.has_text_frame and sh.text_frame.text.startswith("one")][0]
    top_in = box.top / 914400
    assert top_in > 1.4, f"body starts at {top_in:.2f}in — still top-pinned"
    assert top_in < prs.slide_height / 914400 * 0.55, "balanced past the middle"
    # The slack is spent on the rhythm, not left as a void under the rule:
    # three short bullets on a 7.5in canvas get real air between the lines.
    gaps = [p.space_after.pt for p in box.text_frame.paragraphs
            if p.space_after is not None]
    assert gaps and max(gaps) > 12, f"list gaps stayed at the base {gaps}"
    assert (box.top + box.height
            < prs.slide_height - docgen._pptx_in(0.6)), "list runs under the footer"


def test_bullets_take_the_accent_marker_not_a_black_square():
    from pptx.oxml.ns import qn
    prs = _deck(_sections(1))
    s = list(prs.slides)[1]
    box = [sh for sh in _shapes(s)
           if sh.has_text_frame and "point 1.1" in sh.text_frame.text][0]
    pPr = box.text_frame.paragraphs[0]._p.get_or_add_pPr()
    assert pPr.find(qn("a:buChar")) is not None
    assert pPr.find(qn("a:buChar")).get("char") == "•", "still the square glyph"
    bu_clr = pPr.find(qn("a:buClr"))
    assert bu_clr is not None, "the marker inherits the text colour"
    hexval = bu_clr.find(qn("a:srgbClr")).get("val").upper()
    assert hexval == "%02X%02X%02X" % tuple(
        docgen._pptx_rgb("5b4bd6")), hexval


def test_a_quote_is_marked_as_one():
    prs = _deck([{"type": "heading", "level": 1, "text": "Voices"},
                 {"type": "quote", "text": "A faster index you cannot tune "
                                           "is a slower product."}])
    s = list(prs.slides)[1]
    runs = [r for sh in _shapes(s) if sh.has_text_frame
            for p in sh.text_frame.paragraphs for r in p.runs
            if "faster index" in r.text]
    assert runs and runs[0].font.italic, "the quote is plain body text"
    bars = [sh for sh in _shapes(s)
            if sh.width < docgen._pptx_in(0.12) and sh.height > docgen._pptx_in(0.3)]
    assert bars, "no accent bar beside the quote"


def test_chart_series_use_the_deck_palette_and_a_real_axis():
    prs = _deck([{"type": "heading", "level": 1, "text": "Recall"},
                 {"type": "chart", "kind": "bar", "title": "Recall@10",
                  "categories": ["16", "32", "64"],
                  "series": [{"name": "HNSW", "data": [0.81, 0.90, 0.95]},
                             {"name": "IVF", "data": [0.62, 0.74, 0.83]}]}])
    s = list(prs.slides)[1]
    chart = [sh for sh in _shapes(s) if sh.has_chart][0].chart
    cols = [str(ser.format.fill.fore_color.rgb) for ser in chart.plots[0].series]
    assert cols[0] == "5B4BD6", cols
    assert cols[0] != cols[1], cols
    assert "C0504D" not in cols and "4F81BD" not in cols, \
        f"still Office 2007 defaults: {cols}"
    assert float(chart.value_axis.maximum_scale) == 1.0, \
        "axis runs past the data"


def test_a_chart_caption_reaches_the_slide():
    """It survived normalisation and was dropped on the floor in `media`."""
    prs = _deck([{"type": "heading", "level": 1, "text": "Recall"},
                 {"type": "chart", "kind": "bar", "title": "T",
                  "categories": ["a", "b"],
                  "series": [{"name": "s", "data": [1, 2]}],
                  "caption": "ef_search on the x axis."}])
    assert "ef_search on the x axis." in _all_text(prs)


def test_numeric_table_columns_read_as_columns():
    from pptx.enum.text import PP_ALIGN
    prs = _deck([{"type": "heading", "level": 1, "text": "Numbers"},
                 {"type": "table", "header": ["Index", "Recall@10"],
                  "rows": [["HNSW", "0.97"], ["IVF-PQ", "0.88"]]}])
    s = list(prs.slides)[1]
    tbl = [sh for sh in _shapes(s) if sh.has_table][0].table
    assert tbl.cell(0, 1).text_frame.paragraphs[0].alignment == PP_ALIGN.RIGHT
    assert tbl.cell(2, 1).text_frame.paragraphs[0].alignment == PP_ALIGN.RIGHT
    assert tbl.cell(2, 0).text_frame.paragraphs[0].alignment == PP_ALIGN.LEFT
    heights = {int(r.height) for r in tbl.rows}
    assert len(heights) == 1, f"ragged rows: {heights}"


def test_every_slide_carries_the_deck_identity():
    """One accent form on every slide: the deck reads as one object.

    Content slides take a strip down the left edge; the cover takes a panel
    over the right third, because a title on a white page is what made the
    deck look like a document that had been converted rather than designed."""
    prs = _deck(_sections(2))
    cover = list(prs.slides)[0]
    panels = [sh for sh in _shapes(cover)
              if int(sh.width) > int(prs.slide_width) * 0.20
              and int(sh.height) == int(prs.slide_height)]
    assert panels, "the cover has no accent panel"
    for i, s in enumerate(list(prs.slides)[1:], 1):
        strips = [sh for sh in _shapes(s)
                  if sh.left == 0 and int(sh.width) > 0
                  and int(sh.height) == int(prs.slide_height)]
        assert strips, f"slide {i} has no accent edge"


def test_a_small_table_fills_its_slide():
    """Rows grow into the column; a table cannot use line spacing."""
    def rows(n):
        return [[f"r{i}", str(i)] for i in range(n)]

    def heights(prs, slide_i):
        tbl = [sh for sh in list(prs.slides)[slide_i].shapes
               if sh.has_table][0].table
        return [int(r.height) / 914400 for r in tbl.rows]

    small = _deck([{"type": "heading", "level": 1, "text": "Three rows"},
                   {"type": "table", "header": ["Name", "Value"],
                    "rows": rows(3)}])
    h3 = heights(small, 1)
    assert min(h3) > 0.40, f"rows stayed small on an empty slide: {h3}"
    assert max(h3) <= 0.63, f"rows grew past a table: {h3}"

    big = _deck([{"type": "heading", "level": 1, "text": "Many rows"},
                 {"type": "table", "header": ["Name", "Value"],
                  "rows": rows(24)}])
    hb = heights(big, 1)
    assert max(hb) <= 0.63, f"a 25-row table cannot hold those heights: {hb}"
    assert len(list(big.slides)) > 2, "the long table never continued"
    # Nothing may be lost by the split, and the second page keeps its headers.
    seen = []
    for s in list(big.slides)[1:]:
        tbl = [sh for sh in s.shapes if sh.has_table][0].table
        assert tbl.cell(0, 0).text == "Name", "continuation lost the header"
        seen += [tbl.cell(r, 0).text for r in range(1, len(tbl.rows))]
    assert seen == [f"r{i}" for i in range(24)], seen


def test_a_split_list_repeats_nothing():
    """A continuation REPLACES the block it came from.

    `flush` used one signal for "did not fit at all" and "wrote some of it", so
    the partially written block stayed in the queue behind its own remainder and
    the next slide carried the list again. A 24-row table made it obvious (475
    cells); a long bullet list only duplicated quietly."""
    items = [f"item {i}" for i in range(30)]
    prs = _deck([{"type": "heading", "level": 1, "text": "Long list"},
                 {"type": "bullets", "items": items}])
    seen = []
    for s in list(prs.slides)[1:]:
        for sh in _shapes(s):
            if not sh.has_text_frame:
                continue
            for p in sh.text_frame.paragraphs:
                txt = p.text.strip()
                if txt.startswith("item "):
                    seen.append(txt)
    assert seen == items, f"expected each item once, got {len(seen)} lines"
    assert len(list(prs.slides)) > 2, "thirty bullets fit on one slide?"


def test_a_subheading_clears_the_list_above_it():
    """Spread went after a sub-heading only, so it sat on the last bullet.

    Caught on the exported PNG of slide 5: "What breaks" overlapped the third
    numbered item while the items themselves had generous air between them."""
    prs = _deck([{"type": "heading", "level": 1, "text": "Operations"},
                 {"type": "numbers", "items": ["step one", "step two",
                                               "step three"]},
                 {"type": "heading", "level": 2, "text": "What breaks"},
                 {"type": "bullets", "items": ["decay", "skew", "drift"]}])
    s = list(prs.slides)[1]
    boxes = [sh for sh in _shapes(s) if sh.has_text_frame
             and sh.text_frame.text.strip()]
    lst = [sh for sh in boxes if sh.text_frame.text.startswith("step one")][0]
    sub = [sh for sh in boxes if sh.text_frame.text.strip() == "What breaks"][0]
    gap = (sub.top - (lst.top + lst.height)) / 914400
    assert gap >= 0.10, f"sub-heading sits {gap:.2f}in above the list bottom"


def test_a_list_measures_its_own_line_gaps():
    """The height that places the next block has to include space_after.

    One more bullet must cost more than one more line of text: the renderer puts
    9pt between items, and when the measure ignored that the block after the
    list landed a quarter inch too high — on top of the last bullet."""
    def height(items):
        prs = _deck([{"type": "heading", "level": 1, "text": "List"},
                     {"type": "bullets", "items": items}])
        sl = list(prs.slides)[1]
        box = [sh for sh in _shapes(sl)
               if sh.has_text_frame
               and sh.text_frame.text.startswith(items[0])][0]
        return box.height

    pad = docgen._pptx_in(0.06)          # the box's own allowance, not a line
    two = height(["alpha", "bravo"])
    three = height(["alpha", "bravo", "charlie"])
    line = height(["alpha"]) - pad
    step = three - two
    assert step - line > docgen._pptx_in(0.09), (
        f"a third bullet added {step/914400:.3f}in for a {line/914400:.3f}in "
        f"line — the 9pt gap between items is not in the measure")


def test_a_short_list_gets_short_list_type():
    """Three bullets on a 7.5in canvas at 18pt reads as a shrunken report."""
    def sizes(items):
        prs = _deck([{"type": "heading", "level": 1, "text": "Takeaways"},
                     {"type": "bullets", "items": items}])
        sl = list(prs.slides)[1]
        box = [sh for sh in _shapes(sl)
               if sh.has_text_frame and sh.text_frame.text.startswith(items[0])][0]
        return {r.font.size.pt for p in box.text_frame.paragraphs for r in p.runs
                if r.font.size}

    few = sizes(["one", "two", "three"])
    many = sizes([f"point number {i} with some words in it" for i in range(6)])
    assert max(few) > max(many), f"short list {few} vs long list {many}"
    assert max(few) <= docgen._Deck.MAX_BODY, few


def test_the_deck_sets_its_own_type_pairing():
    """Calibri Light is the default face, and a default face is why a deck
    reads as generated rather than made."""
    prs = _deck(_sections(1))
    s = list(prs.slides)[1]
    title = [p for p in s.placeholders if int(p.placeholder_format.idx) == 0][0]
    assert title.text_frame.paragraphs[0].runs[0].font.name == "Segoe UI Semibold"
    body = [sh for sh in _shapes(s)
            if sh.has_text_frame and "point 1.1" in sh.text_frame.text][0]
    assert body.text_frame.paragraphs[0].runs[0].font.name == "Segoe UI"


def test_a_list_that_owns_its_slide_sits_in_a_card():
    prs = _deck([{"type": "heading", "level": 1, "text": "Takeaways"},
                 {"type": "bullets", "items": ["one", "two", "three"]}])
    s = list(prs.slides)[1]
    cards = [sh for sh in _shapes(s)
             if int(sh.width) == int(prs.slide_width) - 2 * docgen._pptx_in(0.8)
             and (not sh.has_text_frame or not sh.text_frame.text.strip())]
    assert cards, "no card behind a slide whose only content is a list"
    body = [sh for sh in _shapes(s)
            if sh.has_text_frame and sh.text_frame.text.startswith("one")][0]
    assert body.left > cards[0].left, "the list is not inset inside its card"
    # Two blocks on one slide is a page of content, not a card.
    plain = _deck([{"type": "heading", "level": 1, "text": "Mixed"},
                   {"type": "paragraph", "text": "A lead sentence."},
                   {"type": "bullets", "items": ["one", "two"]}])
    s2 = list(plain.slides)[1]
    assert not [sh for sh in _shapes(s2)
                if int(sh.width) == int(plain.slide_width) - 2 * docgen._pptx_in(0.8)
                and int(sh.height) > docgen._pptx_in(1.0)
                and (not sh.has_text_frame or not sh.text_frame.text.strip())],         "a card on a mixed slide"


def test_the_slide_numbers_itself():
    prs = _deck(_sections(2))
    for i, s in enumerate(list(prs.slides)[1:], 1):
        nums = [sh for sh in _shapes(s)
                if sh.has_text_frame and sh.text_frame.text.strip() == f"{i:02d}"]
        assert nums, f"slide {i} carries no section index"


def test_the_card_holds_every_bullet():
    """The card is sized on the list's extent, not on its bare line height."""
    prs = _deck([{"type": "heading", "level": 1, "text": "Why recall"},
                 {"type": "bullets", "items": [
                     "Retrieval errors compound downstream",
                     "Top-10 recall below 0.85 hides the answer",
                     "Latency budget: 120 ms p95 for the hop",
                     "Cost scales with candidates scanned, not corpus"]}])
    s = list(prs.slides)[1]
    card = [sh for sh in _shapes(s)
            if int(sh.width) == int(prs.slide_width) - 2 * docgen._pptx_in(0.8)
            and (not sh.has_text_frame or not sh.text_frame.text.strip())][0]
    body = [sh for sh in _shapes(s)
            if sh.has_text_frame and sh.text_frame.text.startswith("Retrieval")][0]
    assert body.top > card.top and body.left > card.left, "text not inset"
    assert (card.top + card.height) >= (body.top + body.height), (
        f"card ends at {(card.top+card.height)/914400:.2f}in but the list "
        f"runs to {(body.top+body.height)/914400:.2f}in")


# ── the designed surface: backgrounds, theme, dividers ────────────────────
# A deck that is stark white on every slide, carries the stock Office theme
# underneath its explicit runs, and renders a heading-only section as a
# near-empty page reads as converted, not designed. These pin the visual
# system the renderer now owns.

def _deck_raw(blocks, title="How LLMs Work", **extra):
    spec = {"title": title, "blocks": blocks, **extra}
    blob, name, _ct = docgen.generate("pptx", spec)
    return Presentation(io.BytesIO(blob)), blob


def _slide_xml(blob, i):
    return zipfile.ZipFile(io.BytesIO(blob)).read(
        f"ppt/slides/slide{i}.xml").decode("utf-8")


def _bg_hex(blob, i):
    m = re.search(r'<p:bg>.*?<a:srgbClr val="([0-9A-Fa-f]{6})"',
                  _slide_xml(blob, i), re.S)
    return m.group(1).upper() if m else None


def _theme(blob):
    return zipfile.ZipFile(io.BytesIO(blob)).read(
        "ppt/theme/theme1.xml").decode("utf-8")


def test_no_slide_is_left_on_stark_white():
    prs, blob = _deck_raw(_sections(2))
    for i in range(1, len(prs.slides) + 1):
        assert "<p:bg>" in _slide_xml(blob, i), f"slide {i} has no background"
        assert _bg_hex(blob, i) not in (None, "FFFFFF"), \
            f"slide {i} background is stark white"


def test_the_theme_is_the_deck_palette_not_office():
    prs, blob = _deck_raw(_sections(1), style="whitepaper")
    theme = _theme(blob)
    assert "4F81BD" not in theme and "Calibri" not in theme, \
        "the stock Office theme survived under the explicit runs"
    a1 = re.search(r'<a:accent1><a:srgbClr val="([0-9A-Fa-f]{6})"', theme)
    assert a1 and a1.group(1).upper() == "0F6E5C", a1
    assert '<a:majorFont><a:latin typeface="Georgia"' in theme
    assert '<a:minorFont><a:latin typeface="Georgia"' in theme


def test_style_changes_the_deck_palette():
    def accent(style):
        _prs, blob = _deck_raw(_sections(1), style=style)
        return re.search(r'<a:accent1><a:srgbClr val="([0-9A-Fa-f]{6})"',
                         _theme(blob)).group(1).upper()
    assert accent("report") != accent("manual"), \
        "every style renders the same palette"


def test_a_heading_only_section_is_a_full_accent_divider():
    prs, blob = _deck_raw([
        {"type": "heading", "level": 1, "text": "Part One"},
        {"type": "heading", "level": 1, "text": "Detail"},
        {"type": "bullets", "items": ["a", "b"]},
    ])
    div = list(prs.slides)[1]
    assert div.slide_layout.name == "Section Header", div.slide_layout.name
    ph = [p for p in div.placeholders if int(p.placeholder_format.idx) == 0][0]
    assert ph.text_frame.text.strip() == "Part One"
    assert str(ph.text_frame.paragraphs[0].runs[0].font.color.rgb) == "FFFFFF"
    assert _bg_hex(blob, 2) == "5B4BD6", "divider is not on the full accent"
    # A section WITH a body is still a content slide, not a divider.
    assert list(prs.slides)[2].slide_layout.name == "Title Only"


def test_a_divider_keeps_its_speaker_note():
    prs, blob = _deck_raw([
        {"type": "heading", "level": 1, "text": "Part"},
        {"type": "note", "text": "Transition line."},
        {"type": "heading", "level": 1, "text": "Body"},
        {"type": "bullets", "items": ["x"]},
    ])
    notes = [s.notes_slide.notes_text_frame.text for s in prs.slides
             if s.has_notes_slide]
    assert any("Transition line." in n for n in notes), notes


def test_the_cover_panel_is_a_gradient():
    prs, blob = _deck_raw(_sections(1))
    cover = list(prs.slides)[0]
    grad = [sh for sh in cover.shapes if "gradFill" in sh._element.xml]
    assert grad, "the cover panel is a flat swatch, not a gradient"
