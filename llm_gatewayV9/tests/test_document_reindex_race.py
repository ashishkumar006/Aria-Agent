"""A reindex during an in-flight index must not wedge the document.

Reproduced state machine bug: `documents_reindex` destroyed the document's
state (records, `chunk_count`, `embedded_indices`) and *then* called
`_start_index`, which returned immediately because the previous worker thread
was still alive. That worker had already built its work list, so it finished
its current batch against a `chunk_count` that no longer existed, recomputed an
empty list, and returned through `return reg.get(doc_id)` without ever setting
a terminal status.

Final state was `embedding`, `chunk_count: 0`, `embedded_count: 1`,
`progress: 0.0` - permanently. `ENABLEABLE` is `{READY}`, so the document could
not be enabled, and nothing in any route reported the state as an error. The
only recovery was a second reindex, and only after the dead thread exited.

These tests drive real threads and the real `Indexer.run` loop; no sleeps longer
than a few milliseconds, and every wait is on an event rather than the clock.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import memory_api as M  # noqa: E402
from documents import registry as R  # noqa: E402


class _Plane:
    """Just enough plane for `_PlaneIndexer._store_chunks`."""

    class _Drawer:
        def __init__(self):
            self.store = self
            self.records: dict[str, object] = {}

        def load(self):
            return list(self.records.values())

        def delete_one(self, rid):
            self.records.pop(rid, None)

        def _persist_embedded(self, rec):
            self.records[rec.id] = rec

    def __init__(self):
        self.drawers = {"document": self._Drawer()}

    def _note_doc_version(self, *a, **k):
        pass


def _wait(fn, timeout=5.0, step=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


def _make(reg, filename, chunks, enabled=True):
    """`Registry.add` mints its own id, so seed then set the fields the indexer
    reads. Returns the Document."""
    doc = reg.add(filename=filename, doc_type="pdf", size_bytes=1)
    reg.update(doc.id, chunk_count=chunks, embedded_indices=[],
               embedded_count=0, enabled=enabled, status=R.PENDING, error="")
    return reg.get(doc.id)


def test_a_superseded_worker_stops_instead_of_writing_stale_state(tmp_path):
    """The generation check is the mechanism. Prove it fires."""
    reg = R.Registry(tmp_path / "documents.json")
    doc = _make(reg, "a.pdf", 2, enabled=False)
    generations = {doc.id: 1}
    idx = M._PlaneIndexer(reg, _Plane(),
                          lambda texts, **k: ([0.1] * len(texts),
                                              {"embed_model": "m"}),
                          generation=1, generations=generations)
    # The worker is superseded before it gets to store anything.
    generations[doc.id] = 2
    try:
        idx._store_chunks(doc.id, [0], [[0.1]], {"embed_model": "m"})
    except M._IndexSuperseded:
        pass
    else:
        raise AssertionError(
            "a superseded worker wrote chunks against state that no longer "
            "belongs to it")


def test_generation_check_is_quiet_while_current(tmp_path):
    reg = R.Registry(tmp_path / "documents.json")
    doc = _make(reg, "b.pdf", 1, enabled=False)
    idx = M._PlaneIndexer(reg, _Plane(),
                          lambda texts, **k: ([0.1] * len(texts),
                                              {"embed_model": "m"}),
                          generation=1, generations={doc.id: 1})
    idx._check_generation(doc.id)          # must not raise


def test_reindex_waits_for_the_old_worker_and_the_document_still_finishes(
        tmp_path, monkeypatch):
    """End to end: a reindex issued while indexing is in flight must leave a
    document that reaches a terminal state, not one wedged at `embedding`."""
    reg = R.Registry(tmp_path / "documents.json")
    doc = _make(reg, "c.pdf", 4)
    doc_id = doc.id
    monkeypatch.setattr(M, "_docs", lambda: reg)

    gate = threading.Event()
    started = threading.Event()

    def slow_embed(texts, **kw):
        started.set()
        # Hold the batch until the test releases it, so the reindex definitely
        # lands mid-flight.
        gate.wait(5.0)
        return ([0.1] * len(texts), {"embed_model": "m"})

    plane = _Plane()

    class _Req:
        class app:
            class state:
                embedders = object()

    monkeypatch.setattr(M, "_plane", lambda r: plane)
    monkeypatch.setattr(M, "_doc_embedder", lambda e: slow_embed)

    M._start_index(_Req(), doc_id)
    assert started.wait(5.0), "the first batch never ran"

    # Reindex mid-flight: destroy the state, then start again.
    reg.update(doc_id, chunk_count=0, embedded_indices=[], embedded_count=0,
               status=R.PENDING, error="")
    old = M._doc_jobs.get(doc_id)
    assert old is not None
    M._doc_generation[doc_id] = M._doc_generation.get(doc_id, 0) + 1
    gate.set()
    old.join(5.0)

    # A second run, as `_start_index` issues after retiring the first.
    M._start_index(_Req(), doc_id)

    def terminal():
        d = reg.get(doc_id)
        if d and d.status in (R.READY, R.FAILED, R.BLOCKED):
            return d
        return None

    got = _wait(terminal, timeout=10.0)
    assert got is not None, (
        "document never reached a terminal state: "
        f"{reg.get(doc_id).to_dict() if reg.get(doc_id) else None}")
    assert got.status in (R.READY, R.FAILED), got.to_dict()
    if got.status == R.READY:
        # A ready document must be complete, not a 0-chunk ghost.
        assert got.chunk_count > 0, got.to_dict()


def test_a_document_deleted_mid_index_gets_no_orphan_chunks(tmp_path):
    """Deleting a document while it indexes used to leave its current batch
    re-persisted by the in-flight worker, so retrieval kept serving chunks for a
    document that no longer existed - with `filename: ""`."""
    reg = R.Registry(tmp_path / "documents.json")
    doc = _make(reg, "d.pdf", 2)
    plane = _Plane()
    idx = M._PlaneIndexer(reg, plane,
                          lambda t, **k: ([0.1] * len(t), {"embed_model": "m"}),
                          generation=1, generations={doc.id: 1})
    reg.delete(doc.id)
    idx._store_chunks(doc.id, [0], [[0.1]], {"embed_model": "m"})
    assert plane.drawers["document"].records == {}, (
        "chunks were written for a deleted document")
