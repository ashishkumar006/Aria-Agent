"""Charts, figures from the user's own uploads, covers and the length math."""
import io
import re
import zipfile

import pytest

import docgen


# ── charts ───────────────────────────────────────────────────────────────

def _chart_blob(fmt="pdf"):
    return docgen.generate(fmt, {
        "title": "Indexing",
        "blocks": [
            {"type": "heading", "level": 1, "text": "Recall"},
            {"type": "chart", "chart": "bar", "title": "By scale",
             "categories": ["1M", "10M", "100M"],
             "series": [{"name": "IVF", "data": [94, 91, 82]},
                        {"name": "HNSW", "data": [96, 97, 95]}]},
        ]})[0]


def test_chart_renders_as_vectors_not_a_raster():
    """A chart baked to a PNG would be blurry and would add ~100KB. reportlab
    draws it as real paths."""
    import pymupdf
    doc = pymupdf.open(stream=_chart_blob(), filetype="pdf")
    page = doc[0]
    drawings = [d for d in page.get_drawings() if d["rect"].width > 20]
    assert len(drawings) >= 4, f"expected vector marks, got {len(drawings)}"
    # No embedded image: the chart is not a picture.
    assert not page.get_images(full=True), "chart was rasterised"
    doc.close()


def test_chart_axis_uses_round_numbers():
    """Raw steps printed 28.615 / 57.23 / 85.845 / 114.46 for a chart of
    94/91/82 — technically correct and completely unreadable."""
    import pymupdf
    doc = pymupdf.open(stream=_chart_blob(), filetype="pdf")
    text = doc[0].get_text()
    for ugly in ("28.615", "57.23", "85.845", "114.46"):
        assert ugly not in text, f"unrounded axis label {ugly} in {text!r}"
    assert "100" in text
    doc.close()


def test_chart_legend_names_every_series():
    """Two unlabelled colour series is an unreadable chart."""
    import pymupdf
    doc = pymupdf.open(stream=_chart_blob(), filetype="pdf")
    text = doc[0].get_text()
    assert "IVF" in text and "HNSW" in text, text[:300]
    doc.close()


def test_pptx_chart_is_native_and_editable():
    blob, _n, _ct = docgen.generate("pptx", {
        "title": "Deck",
        "blocks": [{"type": "chart", "chart": "bar", "title": "By scale",
                    "categories": ["Q1", "Q2"],
                    "series": [{"name": "IVF", "data": [94, 91]}]}]})
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        charts = [n for n in z.namelist() if "charts/chart" in n]
        assert charts, "no native chart part in the deck"
        xml = z.read(charts[0]).decode("utf-8", "replace")
    assert "BarChart" in xml or "barChart" in xml


def test_docx_chart_degrades_to_the_numbers_not_a_broken_picture():
    """python-docx cannot make charts. The honest degradation is the data in
    a table."""
    blob, _n, _ct = docgen.generate("docx", {
        "title": "Doc",
        "blocks": [{"type": "chart", "chart": "bar", "title": "By scale",
                    "categories": ["Q1", "Q2"],
                    "series": [{"name": "IVF", "data": [94, 91]}]}]})
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        body = z.read("word/document.xml").decode("utf-8", "replace")
    assert "Q1" in body and "94" in body


def _pdf_text(blob: bytes) -> str:
    import pymupdf
    d = pymupdf.open(stream=blob, filetype="pdf")
    t = "\n".join(p.get_text() for p in d)
    d.close()
    return t


def test_chart_with_no_numeric_data_is_dropped_and_reported():
    """Silently rendering an empty axis looks like a bug in the document."""
    blob, _n, _ct = docgen.generate("pdf", {
        "title": "T",
        "blocks": [{"type": "paragraph", "text": "before"},
                   {"type": "chart", "chart": "bar",
                    "categories": ["a", "b"],
                    "series": [{"name": "s", "data": ["high", "low"]}]},
                   {"type": "paragraph", "text": "after"}]})
    # The surrounding prose must survive; PDF bodies are compressed, so this
    # has to read the text rather than search the bytes.
    text = _pdf_text(blob)
    assert "before" in text and "after" in text, text[:200]
    assert any("chart" in d for d in docgen.dropped_blocks()), \
        docgen.dropped_blocks()


def test_chart_numeric_coercion():
    c = docgen._norm_chart({"chart": "column", "categories": ["a", "b"],
                            "series": [{"name": "n", "data": ["1,200", "45%"]}]})
    assert c["chart"] == "bar"
    assert c["series"][0]["data"] == [1200.0, 45.0]


def test_chart_pads_a_short_series_rather_than_dropping_bars():
    c = docgen._norm_chart({"categories": ["a", "b", "c"],
                            "series": [{"name": "s", "data": [1, 2]}]})
    assert c["series"][0]["data"] == [1.0, 2.0, 0.0]


def test_nice_step_rounds_to_readable_numbers():
    assert docgen._nice_step(28.615) == 50.0
    assert docgen._nice_step(7.2) == 10.0
    assert docgen._nice_step(0.4) == 0.5
    assert docgen._nice_step(0) == 1.0


@pytest.mark.parametrize("spec_chart", ["line", "pie"])
def test_every_chart_kind_renders(spec_chart):
    blob, _n, _ct = docgen.generate("pdf", {
        "title": "T",
        "blocks": [{"type": "chart", "chart": spec_chart,
                    "categories": ["a", "b", "c"],
                    "series": [{"name": "s", "data": [3, 5, 2]}]}]})
    assert blob.startswith(b"%PDF-")


def test_schema_documents_every_block_type():
    """The schema endpoint is the only thing an LLM reads to learn the block
    vocabulary, and it had fallen behind the normaliser: 7 types documented
    against 10 accepted, with chart/image/cover unreachable however good the
    prompting. This fails if the two ever diverge again."""
    documented = {b["type"] for b in docgen.BLOCK_SCHEMA}
    aliased = set(docgen._BLOCK_ALIASES.values())
    assert not aliased - documented, "aliases resolve to undocumented types"
    assert not documented - aliased, "documented types are never reached"

    # Every documented example must survive normalisation - a schema entry the
    # normaliser rejects is worse than no schema at all.
    for entry in docgen.BLOCK_SCHEMA:
        if entry["type"] == "image":
            continue          # needs a real upload; covered separately
        sample = {k: v for k, v in entry.items() if k not in ("note", "format")}
        sample["type"] = entry["type"]
        assert docgen._norm_blocks([sample]), \
            f"documented block {entry['type']!r} was dropped by the normaliser"


def test_a_table_spelled_columns_data_still_renders():
    # The shape a model reaches for when it has not read the schema.
    blocks = docgen._norm_blocks([
        {"type": "table", "columns": ["A", "B"], "data": [["1", "2"]]}])
    assert blocks and blocks[0]["type"] == "table"
    assert blocks[0]["header"] == ["A", "B"]
    assert blocks[0]["rows"] == [["1", "2"]]


# ── images from the user's own uploads ───────────────────────────────────

def test_image_block_requires_a_plain_document_id():
    """`document` arrives from a model, so a traversal attempt must not
    resolve to a file."""
    for bad in ("../../secrets", "..", "a/b", "", "x" * 200, "C:\\win"):
        assert docgen._resolve_document(bad) is None, bad


def test_image_block_is_dropped_when_the_document_is_unknown():
    blob, _n, _ct = docgen.generate("pdf", {
        "title": "T",
        "blocks": [{"type": "paragraph", "text": "kept"},
                   {"type": "image", "document": "doc-does-not-exist",
                    "page": 1}]})
    assert "kept" in _pdf_text(blob)
    assert any("image" in d for d in docgen.dropped_blocks())


def test_image_block_from_a_real_upload(tmp_path, monkeypatch):
    """Reuses a figure that is already in the user's files. Built as a real
    single-page PDF with an embedded PNG, then read back."""
    reportlab = pytest.importorskip("reportlab")
    from reportlab.lib.utils import ImageReader
    from PIL import Image as PILImage
    from reportlab.pdfgen import canvas as rl_canvas

    png = tmp_path / "fig.png"
    im = PILImage.new("RGB", (240, 160), (91, 75, 214))
    for x in range(240):
        for y in range(0, 160, 3):
            im.putpixel((x, y), (244, 244, 245))
    im.save(png)

    pdf_path = tmp_path / "src.pdf"
    c = rl_canvas.Canvas(str(pdf_path))
    c.drawImage(ImageReader(str(png)), 40, 400, width=240, height=160)
    c.drawString(40, 360, "Figure 1: the placement hierarchy")
    c.save()

    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "doc-abc123.source").write_bytes(pdf_path.read_bytes())
    monkeypatch.setattr(docgen, "ROOT", tmp_path)

    assert docgen._resolve_document("doc-abc123") is not None
    got = docgen._extract_figure(pdf_path, 1, 0)
    assert got, "no embedded figure extracted from the upload"
    blob, ext = got
    assert ext in ("png", "jpeg") and blob[:4] == b"\x89PNG" or blob[:2] == b"\xff\xd8"

    norm = docgen._norm_image({"type": "image", "document": "doc-abc123",
                               "page": 1, "caption": "The hierarchy"})
    assert norm and norm["type"] == "image" and norm["caption"] == "The hierarchy"

    out, _n, _ct = docgen.generate("pdf", {
        "title": "With figure",
        "blocks": [{"type": "paragraph", "text": "see below"},
                   norm]})
    import pymupdf
    doc = pymupdf.open(stream=out, filetype="pdf")
    assert doc[0].get_images(full=True), "the figure was not embedded"
    doc.close()


def test_image_block_also_lands_in_pptx_and_docx(tmp_path, monkeypatch):
    reportlab = pytest.importorskip("reportlab")
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas as rl_canvas
    from PIL import Image as PILImage

    png = tmp_path / "f.png"
    PILImage.new("RGB", (200, 120), (200, 40, 60)).save(png)
    pdf_path = tmp_path / "s.pdf"
    c = rl_canvas.Canvas(str(pdf_path))
    c.drawImage(ImageReader(str(png)), 40, 400, width=200, height=120)
    c.save()
    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "doc-xyz.source").write_bytes(pdf_path.read_bytes())
    monkeypatch.setattr(docgen, "ROOT", tmp_path)

    norm = docgen._norm_image({"type": "image", "document": "doc-xyz", "page": 1})
    assert norm
    for fmt in ("pptx", "docx"):
        blob, _n, _ct = docgen.generate(fmt, {"title": "T", "blocks": [norm]})
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            media = [n for n in z.namelist() if "/media/" in n]
        assert media, f"{fmt} embedded no image"


# ── cover / contents / numbering ─────────────────────────────────────────

def _long_doc(n_sections=6, cover=False, toc=None):
    blocks = []
    if cover:
        blocks.append({"type": "cover", "title": "The Report",
                       "subtitle": "A briefing", "meta": ["Team", "2026"]})
    for i in range(n_sections):
        blocks.append({"type": "heading", "level": 1, "text": f"Section {i+1}"})
        blocks.append({"type": "paragraph", "text": "Body text. " * 60})
    spec = {"title": "The Report", "blocks": blocks}
    if toc is not None:
        spec["toc"] = toc
    return docgen.generate("pdf", spec)[0]


def test_sections_are_numbered_when_a_contents_list_is_present():
    import pymupdf
    doc = pymupdf.open(stream=_long_doc(6, toc=True), filetype="pdf")
    text = "\n".join(p.get_text() for p in doc)
    assert "1. Section 1" in text and "6. Section 6" in text
    doc.close()


def test_contents_list_is_generated_for_a_long_document():
    import pymupdf
    doc = pymupdf.open(stream=_long_doc(6, toc=True), filetype="pdf")
    toc_page = next((i for i in range(doc.page_count)
                     if "Contents" in doc[i].get_text()), None)
    assert toc_page is not None, "no contents page"
    text = doc[toc_page].get_text()
    # Every top-level section is listed, and the page carries page numbers.
    for i in range(1, 7):
        assert f"Section {i}" in text, f"Section {i} missing from the contents"
    assert any(l.rstrip().endswith(tuple("0123456789"))
               and re.search(r"\d\s*$", l) for l in text.split("\n")), \
        f"contents entries carry no page numbers: {text[-200:]!r}"
    doc.close()


def test_a_short_document_gets_no_contents_list():
    import pymupdf
    doc = pymupdf.open(stream=_long_doc(2), filetype="pdf")
    assert "Contents" not in doc[0].get_text()
    doc.close()


def test_cover_is_page_one_and_owns_no_blank_sheet():
    import pymupdf
    doc = pymupdf.open(stream=_long_doc(5, cover=True, toc=True),
                       filetype="pdf")
    first = doc[0].get_text()
    assert "The Report" in first and "Team" in first
    # No page between the cover and the contents.
    assert "Contents" in doc[1].get_text(), "stray blank page after the cover"
    doc.close()


def test_cover_alone_does_not_leave_an_empty_first_page():
    import pymupdf
    blob = docgen.generate("pdf", {
        "title": "Solo",
        "blocks": [{"type": "cover", "title": "Solo", "subtitle": "sub"},
                   {"type": "heading", "level": 1, "text": "A"},
                   {"type": "paragraph", "text": "Body." * 40}]})[0]
    doc = pymupdf.open(stream=blob, filetype="pdf")
    assert doc[0].get_text().strip(), "page one is blank"
    doc.close()