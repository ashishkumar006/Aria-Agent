"""Registry and indexer tests.

The behaviours worth protecting, in order of how badly they would bite:

1. An unavailable embedder must leave the document `blocked`, never `ready`
   and never `failed`, and must not discard work already done.
2. Enabling a document that is not fully embedded must be refused - that is
   the difference between "partial answers with no error" and a clear refusal.
3. Resume must be idempotent: re-running an interrupted index must not
   re-embed anything, and a crash re-does at most one batch.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from documents import registry as R  # noqa: E402
from documents.chunker import Block, chunk_blocks  # noqa: E402


@pytest.fixture()
def reg(tmp_path):
    return R.Registry(tmp_path / "docs")


DOC_MD = """# Title

First paragraph with content worth retrieving. It has two sentences.

## Section Two

Second paragraph mentioning a distinctive marker: ZZUNIQUEBEE.

- a bullet item
- another bullet item

Closing paragraph with more words in it to make the document longer.
"""


def _upload(reg: R.Registry, name="doc.md", data=DOC_MD.encode()):
    d = reg.add(filename=name, doc_type="md", size_bytes=len(data))
    reg.write_source(d.id, data)
    return d


class FakeEmbedder:
    """Stands in for the gateway batch endpoint."""

    def __init__(self, fail_times: int = 0, dim: int = 8):
        self.fail_times = fail_times
        self.dim = dim
        self.calls: list[int] = []

    def __call__(self, texts: list[str]):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise R.EmbedderUnavailable("ollama is not reachable")
        self.calls.append(len(texts))
        return {
            "embeddings": [[float((sum(map(ord, t)) + i) % 97) / 97
                            for i in range(self.dim)] for t in texts],
            "embed_model": "nomic-embed-text",
            "embed_dim": self.dim,
        }


class MemoryIndexer(R.Indexer):
    """Indexer that keeps chunks in a plain dict instead of the vector store,
    so the state machine can be tested without Ollama or FAISS."""

    def __init__(self, registry, embed=None, **kw):
        super().__init__(registry, embed=embed, **kw)
        self.chunks: dict[str, list[str]] = {}
        self.stored: dict[str, list[tuple[str, list[float]]]] = {}

    def _chunk_texts(self, doc_id, indices):
        if doc_id not in self.chunks:
            from documents import parsers as P
            data = self.registry.read_source(doc_id)
            parsed = P.parse(self.registry.get(doc_id).filename, data)
            self.chunks[doc_id] = [c.text for c in chunk_blocks(parsed.blocks)]
        return [self.chunks[doc_id][i] for i in indices]

    def _store_chunks(self, doc_id, indices, vectors, result):
        texts = self._chunk_texts(doc_id, indices)
        self.stored.setdefault(doc_id, []).extend(zip(indices, vectors))
        # Provenance must be recorded, or vectors lose their identity.
        self.registry.update(doc_id, embed_model=result.get("embed_model", ""),
                             embed_dim=int(result.get("embed_dim") or 0))


# ── registry basics ─────────────────────────────────────────────────────────

def test_add_and_get(reg):
    d = _upload(reg)
    got = reg.get(d.id)
    assert got is not None and got.filename == "doc.md"
    assert got.status == R.PENDING
    assert got.embedded_count == 0


def test_unknown_id_is_none_not_a_crash(reg):
    assert reg.get("doc-nope") is None
    assert reg.update("doc-nope", enabled=True) is None


def test_records_survive_a_reopen(tmp_path):
    root = tmp_path / "docs"
    r1 = R.Registry(root)
    d = _upload(r1)
    r1.update(d.id, chunk_count=3, embedded_count=3)
    r2 = R.Registry(root)
    got = r2.get(d.id)
    assert got is not None and got.embedded_count == 3


def test_corrupt_registry_does_not_wedge_the_feature(tmp_path):
    root = tmp_path / "docs"
    r = R.Registry(root)
    _upload(r)
    (root / "documents.json").write_text("{not json", encoding="utf-8")
    r2 = R.Registry(root)
    assert r2.list() == []
    # The unreadable file is preserved for inspection, not silently dropped.
    assert (root / "documents.corrupt.json").exists()


def test_update_touches_updated_at(reg):
    d = _upload(reg)
    before = d.updated_at
    time.sleep(1.1)
    after = reg.update(d.id, chunk_count=5)
    assert after.updated_at != before


# ── the status machine ──────────────────────────────────────────────────────

def test_enable_is_refused_until_ready(reg):
    d = _upload(reg)
    for st in (R.PENDING, R.PARSING, R.CHUNKING, R.EMBEDDING, R.BLOCKED,
               R.FAILED):
        reg.update(d.id, status=st, enabled=False)
        doc, err = reg.set_enabled(d.id, True)
        assert doc.enabled is False, f"{st} must not be enableable"
        assert err, f"{st} must explain why"


def test_enable_allowed_once_ready(reg):
    d = _upload(reg)
    reg.update(d.id, status=R.READY, chunk_count=5, embedded_count=5)
    doc, err = reg.set_enabled(d.id, True)
    assert doc.enabled is True and err == ""
    assert d.id in reg.enabled_ids()


def test_partially_embedded_cannot_be_enabled(reg):
    d = _upload(reg)
    reg.update(d.id, status=R.READY, chunk_count=10, embedded_count=4)
    doc, err = reg.set_enabled(d.id, True)
    assert doc.enabled is False
    assert "partial" in err.lower()


def test_disable_is_always_allowed_and_idempotent(reg):
    d = _upload(reg)
    reg.update(d.id, status=R.READY, chunk_count=2, embedded_count=2)
    reg.set_enabled(d.id, True)
    doc, err = reg.set_enabled(d.id, False)
    assert doc.enabled is False and err == ""
    assert d.id not in reg.enabled_ids()


def test_failure_forces_disabled(reg):
    d = _upload(reg)
    reg.update(d.id, status=R.READY, chunk_count=1, embedded_count=1,
               enabled=True)
    reg.set_status(d.id, R.FAILED, "embedder died")
    assert reg.get(d.id).enabled is False


def test_enabled_ids_only_contains_ready_documents(reg):
    a = _upload(reg, "a.md", b"# A\n\nbody a. more a.\n")
    b = _upload(reg, "b.md", b"# B\n\nbody b. more b.\n")
    reg.update(a.id, status=R.READY, chunk_count=1, embedded_count=1,
               enabled=True)
    reg.update(b.id, status=R.BLOCKED, enabled=True)   # illegal state
    ids = reg.enabled_ids()
    assert a.id in ids and b.id not in ids


# ── indexing ────────────────────────────────────────────────────────────────

def test_happy_path_reaches_ready(reg, monkeypatch):
    monkeypatch.setattr(R, "YIELD_BETWEEN_BATCHES_S", 0)
    d = _upload(reg)
    idx = MemoryIndexer(reg, embed=FakeEmbedder())
    out = idx.run(d.id)
    assert out.status == R.READY, out.error
    assert out.chunk_count > 0
    assert out.embedded_count == out.chunk_count
    assert out.progress() == 1.0
    assert out.embed_model == "nomic-embed-text"
    assert out.embed_dim == 8


def test_no_size_cap_is_imposed(reg, monkeypatch):
    """Decision 4: no per-file, per-corpus or page-count limit. Pacing the
    worker protects the model instead."""
    monkeypatch.setattr(R, "YIELD_BETWEEN_BATCHES_S", 0)
    big = ("# Big\n\n" + ("A sentence of ordinary prose here. " * 400))
    d = _upload(reg, "big.md", big.encode())
    idx = MemoryIndexer(reg, embed=FakeEmbedder())
    out = idx.run(d.id)
    assert out.status == R.READY
    assert out.chunk_count > 20, out.chunk_count


def test_embedder_down_blocks_and_keeps_progress(reg, monkeypatch):
    """The rule the user set: never `ready`, never `failed`."""
    monkeypatch.setattr(R, "YIELD_BETWEEN_BATCHES_S", 0)
    d = _upload(reg)
    idx = MemoryIndexer(reg, embed=FakeEmbedder(fail_times=99))
    out = idx.run(d.id)
    assert out.status == R.BLOCKED, out.status
    assert out.embedded_count == 0
    # Chunks are still counted, so resuming knows the total.
    assert out.chunk_count > 0


def test_blocked_document_resumes_without_redoing_work(reg, monkeypatch):
    monkeypatch.setattr(R, "YIELD_BETWEEN_BATCHES_S", 0)
    d = _upload(reg)
    first = FakeEmbedder(fail_times=99)
    out = MemoryIndexer(reg, embed=first).run(d.id)
    assert out.status == R.BLOCKED

    # The embedder comes back.
    second = FakeEmbedder()
    out2 = MemoryIndexer(reg, embed=second).run(d.id)
    assert out2.status == R.READY, out2.status
    assert out2.embedded_count == out2.chunk_count
    # Nothing was embedded twice: exactly one call per batch, no repeats.
    assert sum(second.calls) == out2.chunk_count


def test_partial_progress_is_kept_across_a_pause(reg, monkeypatch):
    monkeypatch.setattr(R, "YIELD_BETWEEN_BATCHES_S", 0)
    d = _upload(reg)
    idx = MemoryIndexer(reg, embed=FakeEmbedder(), batch_size=2)
    first = idx.run(d.id, stop_after_batches=1)
    assert first.status == R.EMBEDDING
    done = first.embedded_count
    assert 0 < done < first.chunk_count

    second = idx.run(d.id, stop_after_batches=1)
    assert second.embedded_count > done, "resume made no progress"
    final = idx.run(d.id)
    assert final.status == R.READY
    assert final.embedded_count == final.chunk_count


def test_resume_is_idempotent(reg, monkeypatch):
    """Running again after READY must not re-embed anything."""
    monkeypatch.setattr(R, "YIELD_BETWEEN_BATCHES_S", 0)
    d = _upload(reg)
    idx = MemoryIndexer(reg, embed=FakeEmbedder())
    idx.run(d.id)
    calls_before = sum(idx.embed.calls) if hasattr(idx, "embed") else None
    again = idx.run(d.id)
    assert again.status == R.READY
    assert again.embedded_count == again.chunk_count
    # Re-storing the same chunks would duplicate the vector entries.
    ids = [i for i, _v in idx.stored[d.id]]
    assert len(ids) == len(set(ids)), ids


def test_batches_respect_the_configured_width(reg, monkeypatch):
    monkeypatch.setattr(R, "YIELD_BETWEEN_BATCHES_S", 0)
    d = _upload(reg)
    emb = FakeEmbedder()
    MemoryIndexer(reg, embed=emb, batch_size=3).run(d.id)
    assert emb.calls and max(emb.calls) <= 3, emb.calls


def test_batch_size_is_clamped_to_the_maximum(reg):
    assert R.Indexer(reg, batch_size=999).batch_size == R.BATCH_MAX == 32
    assert R.Indexer(reg, batch_size=0).batch_size == 1


# ── failure paths ───────────────────────────────────────────────────────────

def test_unsupported_type_fails_with_a_clear_message(reg, monkeypatch):
    monkeypatch.setattr(R, "YIELD_BETWEEN_BATCHES_S", 0)
    d = _upload(reg, "thing.exe", b"MZ...")
    out = MemoryIndexer(reg, embed=FakeEmbedder()).run(d.id)
    assert out.status == R.FAILED
    assert "not supported" in out.error


def test_corrupt_pdf_fails_rather_than_blocking(reg, monkeypatch):
    monkeypatch.setattr(R, "YIELD_BETWEEN_BATCHES_S", 0)
    d = _upload(reg, "broken.pdf", b"not a pdf")
    out = MemoryIndexer(reg, embed=FakeEmbedder()).run(d.id)
    # A parse failure is terminal, not something an embedder retry can fix.
    assert out.status == R.FAILED


def test_empty_document_fails(reg, monkeypatch):
    monkeypatch.setattr(R, "YIELD_BETWEEN_BATCHES_S", 0)
    d = _upload(reg, "empty.txt", b"   \n\n  ")
    out = MemoryIndexer(reg, embed=FakeEmbedder()).run(d.id)
    assert out.status == R.FAILED
    assert "no readable text" in out.error.lower()


def test_missing_source_file_fails(reg, monkeypatch):
    monkeypatch.setattr(R, "YIELD_BETWEEN_BATCHES_S", 0)
    d = reg.add(filename="gone.md", doc_type="md", size_bytes=1)
    out = MemoryIndexer(reg, embed=FakeEmbedder()).run(d.id)
    assert out.status == R.FAILED
    assert "missing" in out.error.lower()


def test_unknown_document_is_none(reg):
    assert MemoryIndexer(reg, embed=FakeEmbedder()).run("doc-nope") is None


# ── delete ──────────────────────────────────────────────────────────────────

def test_delete_removes_the_record_and_the_bytes(reg):
    d = _upload(reg)
    src = reg.source_path(d.id)
    assert src.exists()
    assert reg.delete(d.id) is True
    assert reg.get(d.id) is None
    assert not src.exists()
    assert reg.delete(d.id) is False


def test_delete_removes_the_source_even_when_the_record_is_gone(reg):
    d = _upload(reg)
    reg.delete(d.id)
    assert reg.source_path(d.id) is None


# ── progress accounting ─────────────────────────────────────────────────────

def test_progress_is_a_fraction(reg):
    d = _upload(reg)
    reg.update(d.id, chunk_count=8, embedded_count=2)
    assert reg.get(d.id).progress() == 0.25
    reg.update(d.id, chunk_count=0, embedded_count=0)
    assert reg.get(d.id).progress() == 0.0


def test_mark_embedded_deduplicates_indices(reg):
    d = _upload(reg)
    reg.update(d.id, chunk_count=5)
    reg.mark_embedded(d.id, [0, 1, 2])
    doc = reg.mark_embedded(d.id, [1, 2, 3])
    assert doc.embedded_indices == [0, 1, 2, 3]
    assert doc.embedded_count == 4


def test_marking_all_chunks_flips_to_ready(reg):
    d = _upload(reg)
    reg.update(d.id, chunk_count=3)
    doc = reg.mark_embedded(d.id, [0, 1, 2])
    assert doc.status == R.READY
    assert doc.to_dict()["can_enable"] is True

# ── regressions found by the live end-to-end run ──────────────────────────
# Both of these passed every unit test and only showed up against the real
# gateway, which is why the live check exists.

def test_indexer_records_provenance_from_either_result_key(reg, monkeypatch):
    """The embedder returns `model`/`dim`; the registry read `embed_model`.
    The document ended up 'ready' with NO recorded model, so its vectors had
    no provenance and could never be checked against a future model."""
    monkeypatch.setattr(R, "YIELD_BETWEEN_BATCHES_S", 0)
    d = _upload(reg)
    MemoryIndexer(reg, embed=FakeEmbedder()).run(d.id)
    assert reg.get(d.id).embed_model == "nomic-embed-text"
    assert reg.get(d.id).embed_dim == 8


def test_plane_indexer_notes_the_document_version(tmp_path):
    """Search() hides chunks whose version is not the registered current one,
    and only plane.remember() registers a version. An indexer that writes
    straight to the drawer therefore makes EVERY chunk look stale, and recall
    silently returns nothing - the document is 'ready' but unfindable."""
    import asyncio as _a

    import memory_api as MA
    from memory.plane import MemoryPlane

    reg = R.Registry(tmp_path / "docs")
    d = _upload(reg)
    plane = MemoryPlane(tmp_path / "state", embed_fn=_embed_for_test)

    indexer = MA._PlaneIndexer(reg, plane, FakeEmbedder())
    out = indexer.run(d.id)
    assert out.status == R.READY, out.status

    loop = _a.new_event_loop()
    try:
        hits = loop.run_until_complete(
            plane.search("distinctive marker", drawers=["document"],
                         top_k=5, doc_ids={d.id}))
    finally:
        loop.close()
    assert hits, ("the document is ready but unretrievable: its version was "
                  "never registered as current")
    assert any("ZZUNIQUEBEE" in (h.value or {}).get("chunk", "")
               for h in hits), [h.descriptor[:40] for h in hits]


async def _embed_for_test(text, task_type):
    return {"embedding": [float((sum(map(ord, text)) + i) % 97) / 97
                          for i in range(16)],
            "model": "nomic-embed-text", "dim": 16}
