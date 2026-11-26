"""PDF visual-content retrieval: embedded-figure captioning + scanned-page OCR.

A text-only PDF parse loses two things a reader sees: figures (charts, diagrams,
scanned equations) and whole pages that are pure images (no text layer). Both are
recovered at index time through the same vision bridge as the image-caption
feature, behind the same master flag, persisted to the caption sidecar so the
chunk list stays deterministic. These tests use stub caption/OCR callers — no
network — and the PRODUCTION read path (`MA._doc_chunks`).
"""
from __future__ import annotations

import io
import struct
import sys
import zlib
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from documents import parsers as P  # noqa: E402
from documents import registry as R  # noqa: E402
import memory_api as MA  # noqa: E402


@pytest.fixture()
def reg(tmp_path):
    return R.Registry(tmp_path / "docs")


@pytest.fixture(autouse=True)
def _no_yield(monkeypatch):
    monkeypatch.setenv("DOCUMENT_IMAGE_CAPTION_YIELD_S", "0")


def _png(width=800, height=400, lines=None, color=(255, 255, 255)):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (width, height), color)
    d = ImageDraw.Draw(img)
    font = None
    for name in ("arial.ttf", "DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(name, 40)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    for i, ln in enumerate(lines or []):
        d.text((20, 20 + i * 50), ln, fill=(0, 0, 0), font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _scanned_pdf(secret_lines):
    """A one-page PDF that is a pure image (no text layer) carrying the secret."""
    import fitz
    doc = fitz.open()
    page = doc.new_page(width=800, height=400)
    page.insert_image(fitz.Rect(0, 0, 800, 400),
                      stream=_png(lines=secret_lines))
    out = io.BytesIO()
    doc.save(out)
    doc.close()
    return out.getvalue()


def _mixed_pdf():
    """Page 1: selectable text + an embedded figure. Page 2: scanned (image only)."""
    import fitz
    doc = fitz.open()
    p1 = doc.new_page(width=800, height=400)
    p1.insert_text((40, 40), "Selectable heading and body text on page one.")
    p1.insert_image(fitz.Rect(40, 80, 440, 280),
                    stream=_png(width=400, height=200, lines=["FIGURE: 42 GW"]))
    p2 = doc.new_page(width=800, height=400)
    p2.insert_image(fitz.Rect(0, 0, 800, 400),
                    stream=_png(width=800, height=400,
                                lines=["SCANNED SECRET 98765"]))
    out = io.BytesIO()
    doc.save(out)
    doc.close()
    return out.getvalue()


def _upload(reg, name, data, doc_type):
    d = reg.add(filename=name, doc_type=doc_type, size_bytes=len(data))
    reg.write_source(d.id, data)
    return d


class CapturingIndexer(R.Indexer):
    """Uses the PRODUCTION read path (`MA._doc_chunks`) for chunk texts."""

    def __init__(self, registry, caption=None, **kw):
        super().__init__(registry, embed=self._emb, caption=caption, **kw)
        self.stored: dict[int, str] = {}

    def _emb(self, texts):
        return {"embeddings": [[0.1] * 4 for _ in texts],
                "embed_model": "fake", "embed_dim": 4}

    def _chunk_texts(self, doc_id, indices):
        ch = MA._doc_chunks(self.registry, self.registry.get(doc_id))
        return [ch[i].text for i in indices if i < len(ch)]

    def _store_chunks(self, doc_id, indices, vectors, result):
        ch = MA._doc_chunks(self.registry, self.registry.get(doc_id))
        for i in indices:
            if i < len(ch):
                self.stored[i] = ch[i].text


# ── extractors ───────────────────────────────────────────────────────────────

def test_iter_pdf_scanned_pages_finds_textless_page(reg):
    data = _scanned_pdf(["SCANNED SECRET 98765"])
    scans = P.iter_pdf_scanned_pages(data)
    assert len(scans) == 1
    assert scans[0]["ext"] == "png"
    assert scans[0]["page"] == 1
    assert scans[0]["sha"] and scans[0]["blob"]


def test_iter_pdf_figures_finds_embedded_image(reg):
    data = _mixed_pdf()
    figs = P.iter_pdf_figures(data)
    assert len(figs) == 1
    assert figs[0]["ext"] in ("png", "jpeg")
    assert figs[0]["blob"]


def test_text_page_is_not_scanned(reg):
    """A page with a real text layer must not be sent to OCR."""
    import fitz
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((40, 40), "This page has selectable text.")
    out = io.BytesIO()
    doc.save(out)
    doc.close()
    assert P.iter_pdf_scanned_pages(out.getvalue()) == []


# ── index-time OCR through the sidecar ───────────────────────────────────────

def test_scanned_pdf_is_ocr_ed_and_indexed(reg, monkeypatch):
    monkeypatch.setenv("DOCUMENT_IMAGE_CAPTION", "1")
    ocr_calls = []

    def ocr(blob, ext, ctx=""):
        ocr_calls.append(1)
        return "SCANNED SECRET 98765 is the access code."

    data = _scanned_pdf(["SCANNED SECRET 98765"])
    d = _upload(reg, "scan.pdf", data, "pdf")
    idx = CapturingIndexer(reg, caption=MA._make_caption_generator(
        reg, captioner=None, ocr=ocr))
    out = idx.run(d.id)
    assert out.status == R.READY
    assert len(ocr_calls) == 1
    rows = reg.read_captions(d.id)
    assert rows and rows[0]["source"] == "ocr"
    assert "98765" in rows[0]["caption"]
    assert any("98765" in t for t in idx.stored.values())
    assert any("98765" in c.text for c in MA._doc_chunks(reg, reg.get(d.id)))


def test_ocr_off_leaves_scanned_page_unindexed(reg, monkeypatch):
    """With no OCR caller, a scanned page produces no blocks — no residue."""
    monkeypatch.setenv("DOCUMENT_IMAGE_CAPTION", "1")
    data = _scanned_pdf(["SCANNED SECRET 98765"])
    d = _upload(reg, "scan.pdf", data, "pdf")
    idx = CapturingIndexer(reg, caption=MA._make_caption_generator(
        reg, captioner=lambda *a: "", ocr=None))
    out = idx.run(d.id)
    # The page has no text layer and OCR is not wired: the document has no
    # readable text, so it fails rather than indexing an empty chunk.
    assert out.status == R.FAILED
    assert reg.read_captions(d.id) == []


def test_mixed_pdf_text_and_figure_and_scanned(reg, monkeypatch):
    monkeypatch.setenv("DOCUMENT_IMAGE_CAPTION", "1")
    data = _mixed_pdf()
    d = _upload(reg, "mixed.pdf", data, "pdf")
    idx = CapturingIndexer(reg, caption=MA._make_caption_generator(
        reg, captioner=lambda *a: "A figure showing 42 GW output.",
        ocr=lambda *a: "SCANNED SECRET 98765"))
    out = idx.run(d.id)
    assert out.status == R.READY
    sources = {r["source"] for r in reg.read_captions(d.id)}
    assert "vision" in sources       # the embedded figure
    assert "ocr" in sources          # the scanned page
    blob = " ".join(c.text for c in MA._doc_chunks(reg, reg.get(d.id)))
    assert "42 GW" in blob           # figure caption
    assert "98765" in blob           # OCR text
    assert "Selectable heading" in blob  # original selectable text


def test_ocr_is_deterministic_and_read_path_is_pure(reg, monkeypatch):
    monkeypatch.setenv("DOCUMENT_IMAGE_CAPTION", "1")
    calls = []

    def ocr(blob, ext, ctx=""):
        calls.append(1)
        return "SCANNED SECRET 98765"

    data = _scanned_pdf(["SCANNED SECRET 98765"])
    d = _upload(reg, "scan.pdf", data, "pdf")
    idx = CapturingIndexer(reg, caption=MA._make_caption_generator(
        reg, captioner=None, ocr=ocr))
    idx.run(d.id)
    n = len(calls)
    chunks = MA._doc_chunks(reg, reg.get(d.id))
    for i, text in idx.stored.items():
        assert chunks[i].text == text
    MA._doc_chunks(reg, reg.get(d.id))
    assert len(calls) == n           # the read path never re-OCRs