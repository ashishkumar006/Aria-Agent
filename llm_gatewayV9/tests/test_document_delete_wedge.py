"""Deleting a document must not wedge the gateway.

Reproduced live before this change: upload a ~280 KB text file, wait two
seconds, then `DELETE /v1/documents/{id}`. The request blocked for **31
seconds**, returned 503 "voice gateway unreachable: ReadTimeout", and after it
the gateway stopped answering HTTP entirely - while its TCP listener stayed
open, so a port check and `/api/health` both reported a healthy service. Every
document route and all gateway-backed LLM traffic were dead. Nothing restarted
it.

Two independent causes, both fixed here:

1. `MemoryService.delete_one` rebuilds the entire vector index per record, and
   the delete route called it once per chunk. A 40-chunk document meant 40 full
   FAISS rebuilds, contending with an indexer writing at the same time.
   `delete_where` removes them all in one pass with at most one rebuild.
2. Nothing told an in-flight indexer to stop. It held a work list built before
   the delete, so it kept re-persisting chunks for a document that no longer
   existed - and `_retire_index` now bumps the generation and waits.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from memory.service import MemoryService  # noqa: E402


def _service(tmp_path, n=40):
    svc = MemoryService(tmp_path / "mem")
    for i in range(n):
        svc.store.append(_rec(f"mem:{i}", doc_id="doc-1", chunk_index=i))
    svc._rebuild_locked()
    return svc


class _Doc:
    def __init__(self, doc_id, chunk_index):
        self.doc_id = doc_id
        self.version = "1"
        self.chunk_index = chunk_index
        self.total_chunks = 40


def _rec(rid, *, doc_id, chunk_index, embedding=None):
    from memory.models import MemoryRecord
    return MemoryRecord(id=rid, kind="fact", drawer="document",
                        descriptor=f"chunk {chunk_index}",
                        source=f"document:{doc_id}", run_id=f"index-{doc_id}",
                        value={"chunk": f"text {chunk_index}", "doc_id": doc_id,
                               "chunk_index": chunk_index},
                        doc={"doc_id": doc_id, "version": "1",
                             "chunk_index": chunk_index, "total_chunks": 40},
                        embedding=embedding)


def test_bulk_delete_removes_every_chunk_of_one_document(tmp_path):
    svc = _service(tmp_path, 40)
    svc.store.append(_rec("mem:other", doc_id="doc-2", chunk_index=0))
    removed = svc.delete_where(lambda r: bool(r.doc and r.doc.doc_id == "doc-1"))
    assert removed == 40, removed
    left = [r.id for r in svc.store.load() if r.doc and r.doc.doc_id == "doc-1"]
    assert left == [], left
    # A different document is untouched - the predicate is the scope.
    assert any(r.id == "mem:other" for r in svc.store.load())


def test_bulk_delete_is_far_faster_than_one_at_a_time(tmp_path):
    """The wedge was quadratic: one full index rebuild per chunk."""
    import shutil

    a, b = tmp_path / "a", tmp_path / "b"
    svc_bulk = _service(a, 40)
    svc_one = _service(b, 40)

    t0 = time.perf_counter()
    svc_bulk.delete_where(lambda r: bool(r.doc and r.doc.doc_id == "doc-1"))
    bulk = time.perf_counter() - t0

    t0 = time.perf_counter()
    for r in list(svc_one.store.load()):
        if r.doc and r.doc.doc_id == "doc-1":
            svc_one.delete_one(r.id)
    one = time.perf_counter() - t0

    assert bulk < one, f"bulk {bulk:.3f}s was not faster than {one:.3f}s"
    assert bulk < 1.0, f"bulk delete took {bulk:.2f}s"


def test_deleting_a_missing_document_is_a_no_op(tmp_path):
    svc = _service(tmp_path, 3)
    assert svc.delete_where(lambda r: False) == 0


def test_retire_index_signals_a_running_worker(monkeypatch):
    """The generation bump must be visible to the worker so it abandons its
    stale work rather than writing against state that is being removed."""
    import memory_api as MA
    import threading

    started = threading.Event()
    released = threading.Event()

    class _Job:
        def is_alive(self):
            return True

        def join(self, timeout=None):
            started.set()
            released.wait(timeout or 0)

    monkeypatch.setattr(MA, "_doc_jobs", {"doc-x": _Job()})
    monkeypatch.setattr(MA, "_doc_generation", {"doc-x": 1})
    MA._retire_index("doc-x")
    assert started.is_set(), "the running worker was never joined"
    assert MA._doc_generation["doc-x"] == 2, (
        "the generation must advance so the worker can detect it is superseded")


def test_retire_index_is_safe_with_no_running_worker(monkeypatch):
    import memory_api as MA

    monkeypatch.setattr(MA, "_doc_jobs", {})
    monkeypatch.setattr(MA, "_doc_generation", {})
    MA._retire_index("doc-y")          # must not raise
    assert MA._doc_generation["doc-y"] == 1