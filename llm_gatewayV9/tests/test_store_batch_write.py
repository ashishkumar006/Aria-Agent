"""One batch of chunks must cost one store rewrite.

`_store_chunks` appended each chunk with `store.append`, and `append`
re-serialises the WHOLE drawer — 86 MB / 4,824 records for the live document
drawer, measured at 3.2-6.0 s per write, all of it inside `store._lock`. A
16-chunk batch therefore owned the lock for ~72 s, and a document search —
which reads that store ten-plus times — queued behind each of them. That is
the user's 35-second search: the same query costs 320 ms when nothing is
indexing.
"""

from __future__ import annotations

import json

import pytest

from memory.models import MemoryRecord
from memory.plane import MemoryPlane
from memory.store import MemoryStore


def _rec(i: int) -> MemoryRecord:
    return MemoryRecord(
        id=f"mem:batch-{i}", kind="fact", drawer="document",
        descriptor=f"chunk {i}", value={"chunk": f"text {i} " * 200,
                                        "doc_id": "doc-1", "chunk_index": i},
        source="test", run_id=f"batch-{i}",
    )


def test_append_many_writes_the_file_once(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "memory.json")
    calls: list[int] = []
    real = store._save_locked

    def counting(items):
        calls.append(len(items))
        return real(items)

    monkeypatch.setattr(store, "_save_locked", counting)

    store.append_many([_rec(i) for i in range(16)])

    assert calls == [16], (
        f"{len(calls)} rewrites for one batch — the indexer is back to paying "
        f"for a full serialisation per chunk")
    assert len(store.load()) == 16


def test_append_many_survives_a_second_batch(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    store.append_many([_rec(i) for i in range(4)])
    store.append_many([_rec(i) for i in range(4, 9)])
    got = store.load()
    assert len(got) == 9
    assert {r.id for r in got} == {f"mem:batch-{i}" for i in range(9)}


def test_append_many_of_nothing_touches_the_disk(tmp_path):
    path = tmp_path / "memory.json"
    store = MemoryStore(path)
    assert store.append_many([]) == []
    assert not path.exists(), "an empty batch still rewrote the store"


def test_the_file_is_valid_json_after_a_batch(tmp_path):
    """A partial write here would read as an empty drawer, not as an error."""
    store = MemoryStore(tmp_path / "memory.json")
    store.append_many([_rec(i) for i in range(3)])
    raw = json.loads((tmp_path / "memory.json").read_text(encoding="utf-8"))
    assert len(raw) == 3


def test_neighbors_are_read_from_the_caller_once(tmp_path, monkeypatch):
    """`_with_neighbors` used to load the document drawer per hit: eight hits
    was eight reads, and a write in between makes each read re-parse 86 MB."""
    from memory.models import DocSpan

    reads: list[int] = []

    def _doc(i: int) -> MemoryRecord:
        r = _rec(i)
        r.doc = DocSpan(doc_id="doc-1", version="1", chunk_index=i,
                        total_chunks=5)
        return r

    records = [_doc(i) for i in range(5)]

    class _Drawer:
        class store:  # noqa: N801 - attribute-shaped stub
            @staticmethod
            def load():
                reads.append(1)
                return records

    plane = MemoryPlane.__new__(MemoryPlane)
    plane.drawers = {"document": _Drawer()}

    hit = records[2]
    enriched = plane._with_neighbors(hit, records)
    for _ in range(7):
        plane._with_neighbors(hit, records)
    assert reads == [], "the shared read was ignored and the drawer re-loaded"
    # The neighbours still arrive, so the refactor did not just skip the work.
    previews = (enriched.value or {}).get("neighbor_previews") or []
    assert {p["chunk_index"] for p in previews} == {1, 3}

    plane._with_neighbors(hit)
    assert reads == [1], "the no-records path must still work for other callers"
