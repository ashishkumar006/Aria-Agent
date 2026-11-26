"""Image-content retrieval: alt-text + index-time vision captions.

The contract these tests protect, in order of how badly a regression would bite:

1. The master flag is OFF by default and leaves NO residue: a fresh document
   chunks exactly as it did before the feature, and no caption sidecar appears.
2. A caption, once generated, is persisted and read back deterministically, so
   the indexer and the detail route agree on chunk index N. The read path never
   calls the model.
3. Alt-text is preferred when it is a real description; a filename or an
   auto-placeholder ("image.png", "Picture 3") is NOT a description and must not
   suppress the vision call.
4. A vision outage never fails the document — the text still indexes.
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


def _png(color=b"\xff\x00\x00") -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(t, d):
        return (struct.pack(">I", len(d)) + t + d
                + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (sig + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00" + color))
            + chunk(b"IEND", b""))


def _deck(alt: str | None = None, blob: bytes | None = None) -> bytes:
    """One slide: a heading, a body sentence, and one picture.

    `alt=None` leaves python-pptx's default filename descr in place (the case
    that must fall through to vision); a string sets a real description."""
    from pptx import Presentation
    from pptx.util import Inches
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[6])
    h = s.shapes.add_textbox(Inches(0.5), Inches(0.2), Inches(8), Inches(0.6))
    h.text_frame.text = "Energy overview"
    body = s.shapes.add_textbox(Inches(0.5), Inches(3.5), Inches(8), Inches(1))
    body.text_frame.text = "Solar capacity grew across the region."
    pic = s.shapes.add_picture(io.BytesIO(blob or _png()),
                               Inches(1), Inches(1), Inches(2), Inches(2))
    if alt is not None:
        for el in pic._element.iter():
            if el.tag.endswith("}cNvPr"):
                el.set("descr", alt)
                break
    out = io.BytesIO()
    prs.save(out)
    return out.getvalue()


FACT = "The hidden chart reports solar capacity reaching 42 gigawatts in 2025."


class CapturingIndexer(R.Indexer):
    """Uses the PRODUCTION read path (`MA._doc_chunks`) for chunk texts, so the
    test exercises the same determinism contract the gateway relies on."""

    def __init__(self, registry, **kw):
        super().__init__(registry, embed=self._emb, **kw)
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


def _upload(reg, name, data, doc_type):
    d = reg.add(filename=name, doc_type=doc_type, size_bytes=len(data))
    reg.write_source(d.id, data)
    return d


def _gen(reg, captioner):
    return MA._make_caption_generator(reg, captioner)


# ── the master flag ──────────────────────────────────────────────────────────

def test_flag_is_off_by_default(monkeypatch):
    monkeypatch.delenv("DOCUMENT_IMAGE_CAPTION", raising=False)
    assert R.image_caption_enabled() is False


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("off", False),
])
def test_flag_reads_env(monkeypatch, val, expected):
    monkeypatch.setenv("DOCUMENT_IMAGE_CAPTION", val)
    assert R.image_caption_enabled() is expected


# ── extraction ───────────────────────────────────────────────────────────────

def test_real_alt_text_is_extracted():
    figs = P.iter_pptx_pictures(_deck(alt="A chart of solar capacity by year"))
    assert len(figs) == 1
    assert figs[0]["alt"] == "A chart of solar capacity by year"
    assert figs[0]["page"] == 1 and figs[0]["ext"] == "png"
    assert len(figs[0]["sha"]) == 64


def test_filename_alt_text_is_not_a_description():
    # python-pptx defaults descr to the filename; that must NOT count as alt.
    figs = P.iter_pptx_pictures(_deck(alt=None))
    assert len(figs) == 1
    assert figs[0]["alt"] == ""


@pytest.mark.parametrize("placeholder", ["image.png", "photo.JPG", "Picture 3",
                                         "Image", "graphic 12", "figure 2"])
def test_placeholder_alt_text_is_discarded(placeholder):
    figs = P.iter_pptx_pictures(_deck(alt=placeholder))
    assert figs[0]["alt"] == ""


def test_picture_hash_matches_bytes():
    import hashlib
    blob = _png(b"\x00\x00\xff")
    figs = P.iter_pptx_pictures(_deck(blob=blob))
    assert figs[0]["sha"] == hashlib.sha256(blob).hexdigest()
    assert figs[0]["blob"] == blob


def test_legacy_ppt_yields_no_pictures():
    assert P.iter_pptx_pictures(b"\xd0\xcf\x11\xe0rest") == []


def test_image_figure_magic_gate():
    blob = _png()
    one = P.image_figure(blob)
    assert one and one["page"] == 1 and one["ext"] == "png"
    assert one["sha"] == P.iter_pptx_pictures(_deck(blob=blob))[0]["sha"]
    assert P.image_figure(b"definitely not an image") is None


# ── sidecar read path ────────────────────────────────────────────────────────

def test_caption_blocks_read_sidecar(reg):
    d = _upload(reg, "e.pptx", _deck(), "pptx")
    assert R.image_caption_blocks(reg, d) == []
    reg.write_captions(d.id, [
        {"sha": "a", "page": 2, "index": 0, "caption": "A real description.",
         "source": "vision"},
        {"sha": "b", "page": 3, "index": 0, "caption": "   ", "source": "alt"},
    ])
    blocks = R.image_caption_blocks(reg, d)
    assert len(blocks) == 1                       # blank caption skipped
    assert blocks[0].kind == "para"
    assert blocks[0].text == "A real description."
    assert blocks[0].page == 2
    assert blocks[0].meta["image"] is True
    assert blocks[0].meta["image_caption_source"] == "vision"


def test_delete_removes_caption_sidecar(reg):
    d = _upload(reg, "e.pptx", _deck(), "pptx")
    reg.write_captions(d.id, [{"sha": "a", "caption": "x", "source": "alt"}])
    assert (reg.root / f"{d.id}.captions.json").exists()
    reg.delete(d.id)
    assert not (reg.root / f"{d.id}.captions.json").exists()


# ── end-to-end indexing ──────────────────────────────────────────────────────

def test_flag_off_indexes_no_caption(reg, monkeypatch):
    monkeypatch.setenv("DOCUMENT_IMAGE_CAPTION", "0")
    d = _upload(reg, "e.pptx", _deck(alt="A real description"), "pptx")
    idx = CapturingIndexer(reg, caption=_gen(reg, lambda *a: FACT))
    out = idx.run(d.id)
    assert out.status == R.READY
    assert reg.read_captions(d.id) == []          # no residue
    texts = [c.text for c in MA._doc_chunks(reg, reg.get(d.id))]
    assert not any("42 gigawatts" in t for t in texts)
    assert not any("A real description" in t for t in texts)


def test_flag_on_indexes_vision_caption(reg, monkeypatch):
    monkeypatch.setenv("DOCUMENT_IMAGE_CAPTION", "1")
    d = _upload(reg, "e.pptx", _deck(alt=None), "pptx")
    idx = CapturingIndexer(reg, caption=_gen(reg, lambda *a: FACT))
    out = idx.run(d.id)
    assert out.status == R.READY
    assert any(FACT in t for t in idx.stored.values())
    rows = reg.read_captions(d.id)
    assert rows and rows[0]["source"] == "vision"


def test_alt_text_short_circuits_vision(reg, monkeypatch):
    monkeypatch.setenv("DOCUMENT_IMAGE_CAPTION", "1")
    calls = []

    def captioner(*a):
        calls.append(1)
        return "SHOULD NOT BE CALLED"

    d = _upload(reg, "e.pptx", _deck(alt="Solar capacity rising to 42 GW"), "pptx")
    idx = CapturingIndexer(reg, caption=_gen(reg, captioner))
    out = idx.run(d.id)
    assert out.status == R.READY
    assert calls == []
    assert reg.read_captions(d.id)[0]["source"] == "alt"
    assert any("42 GW" in c.text for c in MA._doc_chunks(reg, reg.get(d.id)))


def test_caption_is_deterministic_and_read_path_is_pure(reg, monkeypatch):
    monkeypatch.setenv("DOCUMENT_IMAGE_CAPTION", "1")
    calls = []

    def captioner(*a):
        calls.append(1)
        return FACT

    d = _upload(reg, "e.pptx", _deck(alt=None), "pptx")
    idx = CapturingIndexer(reg, caption=_gen(reg, captioner))
    idx.run(d.id)
    n_after_index = len(calls)
    chunks = MA._doc_chunks(reg, reg.get(d.id))
    # Every stored index maps to the identical text on a fresh re-parse.
    for i, text in idx.stored.items():
        assert chunks[i].text == text
    # The read path must not call the model again.
    MA._doc_chunks(reg, reg.get(d.id))
    assert len(calls) == n_after_index


def test_reindex_reuses_persisted_caption(reg, monkeypatch):
    monkeypatch.setenv("DOCUMENT_IMAGE_CAPTION", "1")
    calls = []

    def captioner(*a):
        calls.append(1)
        return FACT

    d = _upload(reg, "e.pptx", _deck(alt=None), "pptx")
    CapturingIndexer(reg, caption=_gen(reg, captioner)).run(d.id)
    before = reg.read_captions(d.id)
    assert len(calls) == 1
    reg.update(d.id, chunk_count=0, embedded_indices=[], embedded_count=0,
               status=R.PENDING)
    CapturingIndexer(reg, caption=_gen(reg, captioner)).run(d.id)
    assert len(calls) == 1                        # not re-described
    assert reg.read_captions(d.id) == before


def test_vision_failure_does_not_fail_document(reg, monkeypatch):
    monkeypatch.setenv("DOCUMENT_IMAGE_CAPTION", "1")

    def boom(*a):
        raise RuntimeError("vision provider is down")

    d = _upload(reg, "e.pptx", _deck(alt=None), "pptx")
    idx = CapturingIndexer(reg, caption=_gen(reg, boom))
    out = idx.run(d.id)
    assert out.status == R.READY                  # text still indexed
    assert reg.read_captions(d.id) == []
    assert any("Solar capacity grew" in t for t in idx.stored.values())


def test_bare_image_on_is_captioned(reg, monkeypatch):
    monkeypatch.setenv("DOCUMENT_IMAGE_CAPTION", "1")
    d = _upload(reg, "photo.png", _png(b"\x00\x00\xff"), "image")
    idx = CapturingIndexer(reg, caption=_gen(reg, lambda *a: FACT))
    out = idx.run(d.id)
    assert out.status == R.READY and out.chunk_count >= 1
    assert any(FACT in c.text for c in MA._doc_chunks(reg, reg.get(d.id)))


def test_bare_image_off_stays_figure_only(reg, monkeypatch):
    monkeypatch.setenv("DOCUMENT_IMAGE_CAPTION", "0")
    d = _upload(reg, "photo.png", _png(), "image")
    idx = CapturingIndexer(reg, caption=_gen(reg, lambda *a: FACT))
    out = idx.run(d.id)
    assert out.status == R.READY and out.chunk_count == 0


def test_precaption_cache_does_not_drop_the_caption(reg, monkeypatch):
    """A detail-route poll during PARSING must not poison the embedding.

    The console polls the document detail every few seconds while it indexes, and
    that route calls `_doc_chunks` unconditionally — so a poll that lands before
    the caption sidecar is written caches a no-caption chunk list. The embedding
    runs AFTER the sidecar exists and used to reuse that stale entry, storing a
    chunk with no caption and making the image unsearchable. The cache is keyed
    on the caption count so the pre-caption and post-caption reads are distinct."""
    monkeypatch.setenv("DOCUMENT_IMAGE_CAPTION", "1")
    d = _upload(reg, "e.pptx", _deck(alt=None), "pptx")
    MA._CHUNK_CACHE.clear()
    # The poll during PARSING: no sidecar yet, so this caches caption-free chunks.
    pre = MA._doc_chunks(reg, reg.get(d.id))
    assert not any("42 gigawatts" in c.text for c in pre)
    # Real indexing: the caption hook writes the sidecar, then the embedding
    # reads chunks back. The stored chunk must carry the caption.
    idx = CapturingIndexer(reg, caption=_gen(reg, lambda *a: FACT))
    out = idx.run(d.id)
    assert out.status == R.READY
    assert any(FACT in t for t in idx.stored.values())
    assert any(FACT in c.text for c in MA._doc_chunks(reg, reg.get(d.id)))