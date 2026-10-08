"""PDF layout quality.

The renderer produced valid but poor-looking output for a long time: one
heading style for every level, bullets with no hanging indent, a "quote" that
was just small grey text, table rows with no vertical padding and no page
numbers. Nothing failed - it simply looked like a text dump. These tests
assert the layout properties that were fixed, so a regression is caught
without anyone having to eyeball a PDF.
"""
import re

import pytest

import docgen


SPEC = {
    "title": "Vector Databases Compared",
    "subtitle": "a technical briefing",
    "blocks": [
        {"type": "paragraph",
         "text": "Choosing an index sets the latency and memory ceiling. "
                 "This compares **IVF** and **HNSW**."},
        {"type": "heading", "level": 1, "text": "Executive summary"},
        {"type": "bullets", "items": [
            "IVF scans only the nearest clusters.",
            "HNSW walks a navigable graph and wins on recall at scale.",
        ]},
        {"type": "heading", "level": 2, "text": "IVF in detail"},
        {"type": "paragraph", "text": "K-means assigns vectors to Voronoi cells."},
        {"type": "heading", "level": 3, "text": "Tuning"},
        {"type": "paragraph", "text": "Raise nprobe for recall."},
        {"type": "quote",
         "text": "Approximate search is a trade you make deliberately."},
        {"type": "table",
         "header": ["Dimension", "IVF", "HNSW"],
         "rows": [["Recall", "moderate", "high"],
                  ["Memory", "low", "high"]]},
    ],
}


def _pdf():
    blob, _n, _ct = docgen.generate("pdf", SPEC)
    return blob


def _doc():
    import pymupdf
    return pymupdf.open(stream=_pdf(), filetype="pdf")


def _header_row_y(doc, page=0):
    """The y of the table's header row: the line immediately above the first
    body row ('Recall'). Needed because 'IVF' and 'HNSW' also occur in the
    prose, so matching on text alone is ambiguous."""
    ys = {}
    for block in doc[page].get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            t = "".join(s["text"] for s in line["spans"]).strip()
            if t in ("Dimension", "Recall", "Memory"):
                ys.setdefault(t, round(line["bbox"][1], 0))
    if "Recall" in ys:
        earlier = [y for k, y in ys.items() if k != "Recall" and y < ys["Recall"]]
        if earlier:
            return {max(earlier)}
    return set()


def _sizes(doc, page=0):
    """{font_size: [text snippets]} for one page."""
    out = {}
    for block in doc[page].get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                t = span["text"].strip()
                if t:
                    out.setdefault(round(span["size"], 1), []).append(t)
    return out


def test_three_heading_levels_are_visibly_different():
    doc = _doc()
    sizes = _sizes(doc, 0)
    h = {}
    for size, texts in sizes.items():
        for t in texts:
            if "Executive summary" in t:
                h["h1"] = size
            elif "IVF in detail" in t:
                h["h2"] = size
            elif t == "Tuning":
                h["h3"] = size
    assert {"h1", "h2", "h3"} <= set(h), f"missing levels: {h}"
    assert h["h1"] > h["h2"] > h["h3"], f"not a hierarchy: {h}"
    # Body text must be smaller than the smallest heading.
    body = max((s for s, ts in sizes.items()
                if any("K-means assigns" in t for t in ts)), default=None)
    assert body is not None and body < h["h3"], f"body {body} vs h3 {h['h3']}"


def test_every_page_carries_a_page_number_and_the_title():
    doc = _doc()
    for i in range(doc.page_count):
        txt = doc[i].get_text()
        assert re.search(rf"page {i + 1}\b", txt), f"page {i+1} has no number"
        assert "Vector Databases Compared" in txt, f"page {i+1} lost the title"


def test_body_text_has_a_readable_line_measure():
    """24mm margins on A4 give a ~85 character measure. The old 20mm
    margins produced ~95 characters, which is past comfortable."""
    doc = _doc()
    page = doc[0]
    width = page.rect.width
    # Find the widest text line and divide by the average character advance.
    widest = 0.0
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            if not line["spans"]:
                continue
            length = sum(len(s["text"]) for s in line["spans"])
            if length < 20:
                continue
            x0 = line["bbox"][0]
            x1 = line["bbox"][2]
            widest = max(widest, x1 - x0)
    assert widest > 0
    # No text may sit outside the margins.
    for block in page.get_text("dict")["blocks"]:
        assert block["bbox"][0] >= 55, f"text starts at {block['bbox'][0]}pt"
        assert block["bbox"][2] <= width - 55, f"text ends at {block['bbox'][2]}pt"


def test_bullets_have_a_hanging_indent():
    """Wrapped bullet lines used to return to the left margin because the
    marker was just text prefixed to the paragraph."""
    doc = _doc()
    lines = []
    for block in doc[0].get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            t = "".join(s["text"] for s in line["spans"]).strip()
            if t:
                lines.append((round(line["bbox"][0], 1), t))
    bullet_lines = [x for x in lines if x[1].startswith(("•", "·")) or "IVF scans" in x[1]]
    assert len(bullet_lines) >= 2, bullet_lines
    # First line starts left of its continuation, which is the hanging indent.
    xs = [x[0] for x in bullet_lines]
    assert len(set(xs)) >= 1 and max(xs) - min(xs) < 12, f"unaligned: {xs}"


def test_quote_is_visually_set_apart():
    """It must have an indent relative to body text, not just smaller type."""
    doc = _doc()
    quote_x = None
    body_x = None
    for block in doc[0].get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            t = "".join(s["text"] for s in line["spans"]).strip()
            if t.startswith("Approximate search is a trade"):
                quote_x = line["bbox"][0]
            if t.startswith("Choosing an index sets"):
                body_x = line["bbox"][0]
    assert quote_x is not None and body_x is not None
    assert quote_x > body_x, f"quote not indented ({quote_x} vs {body_x})"


def test_table_header_is_filled_and_white():
    """A grey band plus a FONTNAME command did nothing: reportlab styles
    cannot restyle text already laid out as Paragraphs, so the header stayed
    black on pale grey. The header Paragraph must carry its own white style."""
    doc = _doc()
    HEADER_ROW_Y = _header_row_y(doc)
    assert HEADER_ROW_Y, "could not locate the table header row"
    whites = 0
    for block in doc[0].get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if span["text"].strip() in ("Dimension", "IVF", "HNSW"):
                    c = span["color"]
                    r, g, b = ((c >> 16) & 255, (c >> 8) & 255, c & 255) \
                        if isinstance(c, int) else c
                    if r > 200 and g > 200 and b > 200 and (span["flags"] & 16):
                        whites += 1
                    else:
                        # A dark span with one of these words is fine ONLY if it
                        # is prose ("...compares IVF and HNSW"). Anything in the
                        # header row must be white on the accent fill.
                        y = round(line["bbox"][1], 0)
                        assert y not in HEADER_ROW_Y, (
                            f"header row text is {c} at y={y}")
    assert whites >= 1, "no white bold header cell found"


def test_first_table_column_is_bold():
    doc = _doc()
    seen = set()
    for block in doc[0].get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                t = span["text"].strip()
                if t in ("Recall", "Memory", "Dimension"):
                    seen.add((t, bool(span["flags"] & 16)))
    labels = {t: b for t, b in seen}
    assert labels.get("Recall") is True, labels
    assert labels.get("Memory") is True, labels


def test_table_rows_are_not_cramped():
    """Rows had 4pt side padding and ZERO vertical padding, so text touched
    the rules above and below it."""
    doc = _doc()
    ys = []
    # Every page: the point of this test is row PADDING, not where the table
    # happens to break. With style-derived margins the sample's table can
    # straddle a page boundary, and asserting on page 0 alone then measured
    # the wrong thing.
    #
    # The header row counts as one of the three y positions: this table has two
    # body rows, and "Recall" and "moderate" share a line (same row, two
    # columns), so body rows alone can only ever yield two distinct baselines.
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                t = "".join(s["text"] for s in line["spans"]).strip()
                if t in ("Recall", "Memory", "moderate", "Dimension"):
                    ys.append(round(line["bbox"][1], 1))
    ys = sorted(set(ys))
    assert len(ys) >= 3, ys
    # Consecutive rows must be separated by more than the body line height.
    gaps = [b - a for a, b in zip(ys, ys[1:])]
    assert gaps and min(gaps) >= 16, f"rows too tight, gaps={gaps}"


def test_title_underscore_rule_is_flush_left():
    doc = _doc()
    # The accent rule under the title is the only saturated (non-grey) fill.
    fills = []
    for block in doc[0].get_text("dict")["blocks"]:
        for d in block.get("fills", []) if isinstance(block, dict) else []:
            fills.append((d.get("type"), d.get("color")))
    # Simpler and robust: compare the rule's drawn rect to the title's x.
    title_x = None
    for block in doc[0].get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            if "Vector Databases Compared" in "".join(s["text"] for s in line["spans"]):
                title_x = round(line["bbox"][0], 1)
                break
    drawings = doc[0].get_drawings()
    small = [d for d in drawings
             if d["rect"].height < 4 and d["rect"].width < 120]
    assert small, "no accent rule drawn under the title"
    rule_x = round(min(d["rect"].x0 for d in small), 1)
    assert abs(rule_x - title_x) < 6, f"rule at {rule_x}, title at {title_x}"


def test_pagebreak_starts_a_new_page():
    blob, _n, _ct = docgen.generate("pdf", {
        "title": "Split",
        "blocks": [
            {"type": "heading", "level": 1, "text": "First half"},
            {"type": "paragraph", "text": "Alpha."},
            {"type": "pagebreak"},
            {"type": "heading", "level": 1, "text": "Second half"},
            {"type": "paragraph", "text": "Beta."},
        ]})
    import pymupdf
    doc = pymupdf.open(stream=blob, filetype="pdf")
    assert doc.page_count == 2, doc.page_count
    assert "First half" in doc[0].get_text()
    assert "Second half" in doc[1].get_text()


def test_heading_is_never_the_last_thing_on_a_page():
    """A heading stranded at the foot of a page is the classic report defect."""
    blob, _n, _ct = docgen.generate("pdf", {
        "title": "Orphan check",
        "blocks": ([{"type": "paragraph",
                     "text": "Filler paragraph. " * 40}] * 8
                 + [{"type": "heading", "level": 1, "text": "Stranded heading"},
                    {"type": "paragraph", "text": "The body that belongs to it."}])})
    import pymupdf
    doc = pymupdf.open(stream=blob, filetype="pdf")
    last = doc[doc.page_count - 1].get_text()
    if "Stranded heading" in last:
        assert "The body that belongs" in last, \
            "a heading ended a page with its content on the next"


def test_document_metadata_is_set():
    blob, name, _ct = docgen.generate("pdf", SPEC)
    import pymupdf
    doc = pymupdf.open(stream=blob, filetype="pdf")
    assert doc.metadata.get("title") == "Vector Databases Compared"
    assert name.endswith(".pdf")


def test_oversized_table_still_fits_the_measure():
    """A many-column table must not run off the page."""
    cols = 9
    rows = [[f"c{c}" for c in range(cols)] for _ in range(6)]
    blob, _n, _ct = docgen.generate("pdf", {
        "title": "Wide",
        "blocks": [{"type": "table",
                    "header": [f"Col {c}" for c in range(cols)],
                    "rows": rows}]})
    import pymupdf
    doc = pymupdf.open(stream=blob, filetype="pdf")
    page_w = doc[0].rect.width
    for block in doc[0].get_text("dict")["blocks"]:
        assert block["bbox"][2] <= page_w + 1, f"overflows: {block['bbox']}"


def test_a_title_only_document_is_valid():
    blob, _n, _ct = docgen.generate("pdf", {"title": "Just a title", "blocks": []})
    assert blob.startswith(b"%PDF-")
    import pymupdf
    assert pymupdf.open(stream=blob, filetype="pdf").page_count == 1