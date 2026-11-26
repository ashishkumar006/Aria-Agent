"""DOCX visual-content retrieval: embedded pictures, including EMF/WMF charts.

Word keeps pictures in `word/media/`. Raster images pass through; EMF/WMF
vector metafiles (pasted charts, diagrams) are rasterised to PNG via Pillow so a
vision model can read them. Mirrors the PDF/PPTX visual-content tests: stub
captioner (no network), PRODUCTION read path (`MA._doc_chunks`). The EMF vector
decode itself is Pillow's; it is exercised end-to-end on the real heattransfer
docx during re-index, and the branch (zip -> ext -> rasterise -> png) is covered
here with a PNG payload standing in for the vector.
"""
from __future__ import annotations

import io
import sys
import zipfile
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


def _png_bytes():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (400, 200), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.text((20, 80), "DOCX FIGURE SECRET 24680", fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _docx_with_png():
    import docx
    d = docx.Document()
    d.add_paragraph("Selectable heading and body text.")
    d.add_picture(io.BytesIO(_png_bytes()))
    out = io.BytesIO()
    d.save(out)
    return out.getvalue()


def _zip_with(names_blobs):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, b in names_blobs.items():
            z.writestr(n, b)
    return buf.getvalue()


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


# ── extractor ────────────────────────────────────────────────────────────────

def test_iter_docx_figures_finds_embedded_png():
    figs = P.iter_docx_figures(_docx_with_png())
    assert len(figs) == 1
    assert figs[0]["ext"] == "png"
    assert figs[0]["blob"].startswith(b"\x89PNG")
    assert figs[0]["sha"]


def test_iter_docx_figures_empty_for_non_zip():
    assert P.iter_docx_figures(b"not a zip at all") == []


def test_metafile_entry_is_rasterised_to_png():
    # A word/media/*.emf entry is routed through the rasteriser and comes back
    # tagged png (PIL decodes the vector; a PNG payload exercises the same
    # branch). Real EMF vectors are verified live on the heattransfer docx.
    data = _zip_with({"word/media/image1.emf": _png_bytes()})
    figs = P.iter_docx_figures(data)
    assert len(figs) == 1
    assert figs[0]["ext"] == "png"
    assert figs[0]["blob"].startswith(b"\x89PNG")


def test_rasterize_metafile_none_on_garbage():
    assert P._rasterize_metafile(b"not an image") is None


# ── indexing ─────────────────────────────────────────────────────────────────

def test_docx_picture_is_captioned_and_indexed(reg, monkeypatch):
    monkeypatch.setenv("DOCUMENT_IMAGE_CAPTION", "1")
    calls = []

    def captioner(blob, ext, ctx=""):
        calls.append((ext, ctx))
        return "A line chart of heat flux over temperature."

    data = _docx_with_png()
    d = _upload(reg, "chart.docx", data, "docx")
    idx = CapturingIndexer(reg, caption=MA._make_caption_generator(
        reg, captioner=captioner))
    out = idx.run(d.id)
    assert out.status == R.READY
    assert calls and calls[0][0] == "png"
    rows = reg.read_captions(d.id)
    assert rows and rows[0]["source"] == "vision"
    assert "heat flux" in rows[0]["caption"]
    blob = " ".join(c.text for c in MA._doc_chunks(reg, reg.get(d.id)))
    assert "heat flux" in blob                 # caption chunk
    assert "Selectable heading" in blob        # original selectable text


def test_docx_caption_read_path_is_pure(reg, monkeypatch):
    monkeypatch.setenv("DOCUMENT_IMAGE_CAPTION", "1")
    calls = []

    def captioner(blob, ext, ctx=""):
        calls.append(1)
        return "A line chart of heat flux."

    d = _upload(reg, "chart.docx", _docx_with_png(), "docx")
    idx = CapturingIndexer(reg, caption=MA._make_caption_generator(
        reg, captioner=captioner))
    idx.run(d.id)
    n = len(calls)
    chunks = MA._doc_chunks(reg, reg.get(d.id))
    for i, text in idx.stored.items():
        assert chunks[i].text == text
    MA._doc_chunks(reg, reg.get(d.id))
    assert len(calls) == n                      # read path never re-captions