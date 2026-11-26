"""Figure handles: what an uploaded document offers an author, and what the
renderer can actually lift out of it.

The `image` block has existed in `docgen` for a long time, and an author could
never use it: a model cannot invent the (page, index) pair, and nothing listed
them. So the listing and the extractor have to agree exactly — a handle that
resolves to a DIFFERENT picture than the one it names is worse than no picture,
and an out-of-range handle that silently clamps is precisely that.

Live origin 2026-11-02: `_extract_figure` clamped `page`/`index` into range, so
"page 9" of a 3-page source printed page 3's figure and the document looked
right while carrying the wrong image.
"""
from __future__ import annotations

import io
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import docgen  # noqa: E402
import main as M           # noqa: E402
import memory_api as MA    # noqa: E402
from documents import parsers  # noqa: E402
from documents.registry import Registry  # noqa: E402


def _png(w: int = 60, h: int = 24, colour=(10, 120, 220)) -> bytes:
    """A real, tiny RGB PNG, written by hand so the test needs no image library."""
    raw = b"".join(b"\x00" + bytes(colour) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body)))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _pdf_with_figures(tmp_path: Path) -> Path:
    """Two pages, one figure on the first and two references on the second."""
    from reportlab.pdfgen import canvas

    png = tmp_path / "fig.png"
    png.write_bytes(_png())
    path = tmp_path / "source.pdf"
    c = canvas.Canvas(str(path), pagesize=(300, 300))
    c.drawImage(str(png), 20, 200, width=100, height=40)
    c.showPage()
    c.drawImage(str(png), 20, 200, width=100, height=40)
    c.drawImage(str(png), 20, 100, width=100, height=40)
    c.showPage()
    c.save()
    return path


def test_every_listed_handle_resolves_to_a_figure(tmp_path):
    path = _pdf_with_figures(tmp_path)
    listing = docgen.figures_in(path)
    assert listing, "a PDF with embedded figures listed none"
    for f in listing:
        got = docgen._extract_figure(path, f["page"], f["index"])
        assert got, f"handle {f} is not liftable: the listing lies"
        blob, ext = got
        assert blob and ext in ("png", "jpeg")


def test_the_listing_carries_the_shape_the_author_choices_need(tmp_path):
    """Width and height are what separate a logo from a chart. Read from the
    header, not decoded: a scanned book is a directory listing here."""
    path = _pdf_with_figures(tmp_path)
    for f in docgen.figures_in(path):
        assert f["width"] > 0 and f["height"] > 0, f
        assert isinstance(f["page"], int) and f["page"] >= 1
        assert isinstance(f["index"], int)


def test_a_bare_image_upload_is_one_figure(tmp_path):
    p = tmp_path / "shot.png"
    p.write_bytes(_png(w=32, h=16))
    got = docgen.figures_in(p)
    assert got == [{"page": 1, "index": 0, "ext": "png",
                    "bytes": p.stat().st_size, "width": 32, "height": 16}]


def test_a_non_figure_file_lists_nothing(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("nothing to draw here", encoding="utf-8")
    assert docgen.figures_in(p) == []


def test_an_out_of_range_handle_is_dropped_not_clamped(tmp_path):
    path = _pdf_with_figures(tmp_path)
    n_pages = len(docgen.figures_in(path)) or 1
    assert docgen._extract_figure(path, 99, 0) is None, (
        "a page that does not exist printed some other page's figure")
    assert docgen._extract_figure(path, 1, 40) is None, (
        "an index past the end printed the last figure on the page instead")
    assert docgen._extract_figure(path, 1, 0) is not None
    assert n_pages >= 1


def test_a_broken_pdf_lists_nothing_rather_than_raising(tmp_path):
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"%PDF-1.4\nthis is not a parseable file\n%%EOF")
    assert docgen.figures_in(p) == []


# ── the upload side: an image is a figure source, not searchable text ───────

def test_an_image_upload_parses_as_a_figure_source():
    res = parsers.parse("photo.png", _png())
    assert res.doc_type == "image"
    assert res.blocks == [], "an image has no text to chunk"
    assert any("figure" in w for w in res.warnings), res.warnings


def test_a_mislabelled_image_is_refused_at_upload():
    """Otherwise it stores cleanly and yields no figure much later, silently."""
    with pytest.raises(parsers.UnsupportedDocument):
        parsers.parse("fake.png", b"BM...not a png at all...")


def test_image_extensions_are_supported_and_advertised():
    for ext in (".png", ".jpg", ".jpeg"):
        assert parsers.SUPPORTED.get(ext) == "image", ext
    # SUPPORTED_HUMAN is the tuple behind the rejection message, so an
    # accepted type that is not named there tells the user nothing.
    assert any("PNG" in h for h in parsers.SUPPORTED_HUMAN), parsers.SUPPORTED_HUMAN


# ── the route the author reads ──────────────────────────────────────────────

def _wire(monkeypatch, tmp_path, *, filename, data, doc_type):
    reg = Registry(tmp_path / "documents")
    doc = reg.add(filename=filename, doc_type=doc_type, size_bytes=len(data))
    reg.write_source(doc.id, data)
    monkeypatch.setattr(MA, "_docs", lambda: reg)
    return TestClient(M.app), doc


def test_figures_route_lists_the_handles(monkeypatch, tmp_path):
    data = _pdf_with_figures(tmp_path).read_bytes()
    c, doc = _wire(monkeypatch, tmp_path, filename="annual.pdf",
                   data=data, doc_type="pdf")
    with c:
        r = c.get(f"/v1/documents/{doc.id}/figures")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["filename"] == "annual.pdf"
    assert body["document_id"] == doc.id
    assert body["figures"] and body["figures"][0]["page"] >= 1


def test_figures_route_404s_for_an_unknown_document(monkeypatch, tmp_path):
    reg = Registry(tmp_path / "documents")
    monkeypatch.setattr(MA, "_docs", lambda: reg)
    c = TestClient(M.app)
    with c:
        r = c.get("/v1/documents/does-not-exist/figures")
    assert r.status_code == 404


def test_figures_route_404s_when_the_source_bytes_are_gone(monkeypatch, tmp_path):
    reg = Registry(tmp_path / "documents")
    doc = reg.add(filename="gone.pdf", doc_type="pdf", size_bytes=10)
    monkeypatch.setattr(MA, "_docs", lambda: reg)
    c = TestClient(M.app)
    with c:
        r = c.get(f"/v1/documents/{doc.id}/figures")
    assert r.status_code == 404


def test_an_image_document_is_served_with_its_real_media_type(monkeypatch, tmp_path):
    """One registry type, two formats: the console can only preview a figure
    it can display."""
    c, doc = _wire(monkeypatch, tmp_path, filename="chart.png",
                   data=_png(), doc_type="image")
    with c:
        r = c.get(f"/v1/documents/{doc.id}/source")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")


def test_an_image_indexes_as_ready_so_it_can_be_enabled(tmp_path):
    """Live 2026-11-02: the upload came back `failed (no readable text found)`
    and enabling it was refused with a 409 — so the figure could never enter a
    run's document scope, and the listing would never offer it. Having no text
    is what succeeding means for an image."""
    from documents import registry as R

    reg = R.Registry(tmp_path / "docs")
    data = _png()
    d = reg.add(filename="fig.png", doc_type="image", size_bytes=len(data))
    reg.write_source(d.id, data)
    idx = R.Indexer(reg, embed=lambda texts: {
        "embeddings": [[0.0] * 4 for _ in texts],
        "embed_model": "stub", "embed_dim": 4})
    out = idx.run(d.id)
    assert out.status == R.READY, out.error
    assert reg.get(d.id).chunk_count == 0
    # The reconcile re-queues any document whose index_version is not current.
    # Without the stamp a READY image went back to PENDING on the next
    # documents request, parsed again, and could never be enabled.
    assert reg.get(d.id).index_version == R.INDEX_FORMAT_VERSION, (
        "an unversioned document is re-indexed forever")
    again = idx.run(d.id)
    assert again.status == R.READY, "re-running the index disturbed a done image"


def test_a_textless_pdf_is_still_a_failure(tmp_path):
    """The image rule must not swallow the scanned-PDF case it sits next to:
    that one really does need OCR and says so."""
    from documents import registry as R

    reg = R.Registry(tmp_path / "docs")
    body = b"%PDF-1.4\nno text here\n%%EOF"
    d = reg.add(filename="scan.pdf", doc_type="pdf", size_bytes=len(body))
    reg.write_source(d.id, body)
    idx = R.Indexer(reg, embed=lambda texts: {
        "embeddings": [[0.0] * 4 for _ in texts],
        "embed_model": "stub", "embed_dim": 4})
    out = idx.run(d.id)
    assert out.status == R.FAILED
    assert "no readable text" in (out.error or "").lower()


def test_a_pie_leaves_room_for_its_own_labels():
    """Live 2026-11-02: the pie filled its drawing, and reportlab draws a
    slice's label OUTSIDE the circle — so the labels were placed above the
    drawing's own box and the chart title printed straight through the first
    category. The inset is the labels' margin, and the circle is sized to it."""
    import inspect

    src = inspect.getsource(docgen)
    start = src.index('if kind == "pie":')
    pie = src[start:src.index('elif kind == "line":', start)]
    assert "inset" in pie and "diameter" in pie, pie
    assert "pie.x, pie.y = 4, 4" not in pie, "the pie fills its drawing again"
    assert "pie.width = pie.height = diameter" in pie


def test_a_chart_block_still_survives_normalisation_twice():
    """The sectioned writer normalises blocks, then the renderer normalises the
    spec again; a chart that vanishes on the second pass draws nothing."""
    b = {"type": "chart", "chart": "pie", "categories": ["a", "b"],
         "series": [{"name": "share", "data": [3, 7]}], "title": "Split"}
    once = docgen._norm_chart(b)
    assert once and once["chart"] == "pie"
    assert docgen._norm_chart(once) == once


# ── office documents: embedded pictures are reusable figures ────────────────

def _pptx_with_picture(tmp_path: Path) -> Path:
    pytest.importorskip("pptx")
    from pptx import Presentation
    from pptx.util import Inches
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Chart"
    img = tmp_path / "p.png"
    img.write_bytes(_png(w=48, h=20))
    s.shapes.add_picture(str(img), Inches(1), Inches(2), width=Inches(2))
    p = tmp_path / "deck.pptx"
    prs.save(str(p))
    return p


def _docx_with_picture(tmp_path: Path) -> Path:
    pytest.importorskip("docx")
    import docx
    from docx.shared import Inches
    d = docx.Document()
    d.add_heading("Report", level=1)
    img = tmp_path / "d.png"
    img.write_bytes(_png(w=40, h=16))
    d.add_picture(str(img), width=Inches(2))
    p = tmp_path / "report.docx"
    d.save(str(p))
    return p


def test_pptx_picture_lists_and_lifts_on_its_slide(tmp_path):
    p = _pptx_with_picture(tmp_path)
    listing = docgen.figures_in(p)
    assert listing, "a deck with an embedded picture listed no figure"
    assert listing[0]["page"] == 1 and listing[0]["width"] == 48
    for f in listing:
        got = docgen._extract_figure(p, f["page"], f["index"])
        assert got and got[1] in ("png", "jpeg"), f
    # No clamping: a slide or index that does not exist lifts nothing.
    assert docgen._extract_figure(p, 99, 0) is None
    assert docgen._extract_figure(p, 1, 40) is None


def test_docx_picture_lists_and_lifts(tmp_path):
    p = _docx_with_picture(tmp_path)
    listing = docgen.figures_in(p)
    assert listing, "a DOCX with an embedded picture listed no figure"
    assert listing[0]["page"] == 1 and listing[0]["width"] == 40
    for f in listing:
        got = docgen._extract_figure(p, f["page"], f["index"])
        assert got and got[1] in ("png", "jpeg"), f
    assert docgen._extract_figure(p, 2, 0) is None
    assert docgen._extract_figure(p, 1, 40) is None


def test_a_textless_office_file_lists_no_figures(tmp_path):
    pytest.importorskip("pptx")
    from pptx import Presentation
    prs = Presentation()
    prs.slides.add_slide(prs.slide_layouts[5]).shapes.title.text = "No pictures"
    p = tmp_path / "empty.pptx"
    prs.save(str(p))
    assert docgen.figures_in(p) == []
