"""`/v1/documents/search` must return enough provenance to cite.

The agent's chat path retrieves document chunks on the request path and asks
the model to cite them as `[n]`. A citation the user cannot follow is worse
than no citation: they cannot tell which document a claim came from, and the
model - given a hit with no filename - either says "a document" or invents a
title. Both are worse than not retrieving at all.

So `filename` and `heading_path` are part of the response contract, not a
nicety, and are asserted here rather than left to the consumer to discover.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import main as M           # noqa: E402  (the app the router is mounted on)
import memory_api as MA    # noqa: E402  (where the route and its helpers live)


class _Hit:
    """Stands in for a MemoryRecord as the search serialiser reads it."""

    def __init__(self, doc_id: str, chunk_index: int, text: str):
        self.id = f"mem:doc-{doc_id}-{chunk_index}"
        self.doc = type("D", (), {"doc_id": doc_id, "chunk_index": chunk_index})()
        self.descriptor = text[:200]
        self.value = {"chunk": text, "heading_path": ["Refunds", "Timing"],
                      "page": 3}
        self.embed_model = "nomic-embed-text"
        self.page = 3


class _Reg:
    def __init__(self, filename="handbook.pdf", known=True):
        self.filename = filename
        self.known = known

    def get(self, _doc_id):
        return type("Doc", (), {"filename": self.filename})() if self.known else None

    def enabled_ids(self):
        return {"doc-1"}


def _wire(monkeypatch, hits, filename="handbook.pdf", known=True):
    """Patch the plane's search and the registry lookup the route performs."""
    seen: dict = {}

    async def fake_search(_self, q, drawers=None, top_k=8, doc_ids=None):
        seen["doc_ids"] = doc_ids
        seen["q"] = q
        return list(hits)

    plane = type("P", (), {"search": fake_search})()
    monkeypatch.setattr(MA, "_plane", lambda _req: plane)
    monkeypatch.setattr(MA, "_docs", lambda: _Reg(filename, known))
    return TestClient(M.app), seen


def test_search_hits_carry_filename_and_heading_path(monkeypatch):
    c, _seen = _wire(monkeypatch, [_Hit("doc-1", 0, "Refunds take 5 days.")])
    with c:
        r = c.post("/v1/documents/search", json={"query": "refunds", "top_k": 4})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hits"], body
    h = body["hits"][0]
    assert h["filename"] == "handbook.pdf", h
    assert h["heading_path"] == ["Refunds", "Timing"], h
    assert h["doc_id"] == "doc-1"
    assert h["page"] == 3
    assert "5 days" in h["chunk"]
    assert h["embed_model"] == "nomic-embed-text"


def test_search_still_serves_the_fields_it_always_did(monkeypatch):
    """The provenance fields are additive; nothing existing may regress."""
    c, _seen = _wire(monkeypatch, [_Hit("doc-1", 7, "body text")])
    with c:
        h = c.post("/v1/documents/search", json={"query": "body"}).json()["hits"][0]
    for key in ("id", "doc_id", "chunk_index", "page", "descriptor",
                "chunk", "embed_model"):
        assert key in h, f"{key} disappeared from the search response"


def test_a_hit_whose_document_is_gone_still_serialises(monkeypatch):
    """The registry lookup can miss (a document deleted while its chunks
    remain). The hit must degrade to an empty filename, not 500 the search."""
    c, _seen = _wire(monkeypatch, [_Hit("doc-missing", 0, "orphan chunk")],
                     known=False)
    with c:
        r = c.post("/v1/documents/search", json={"query": "orphan"})
    assert r.status_code == 200, r.text
    assert r.json()["hits"][0]["filename"] == ""


def test_empty_query_is_still_rejected(monkeypatch):
    c, _seen = _wire(monkeypatch, [])
    with c:
        assert c.post("/v1/documents/search", json={"query": ""}).status_code == 400


def test_doc_ids_narrowing_is_still_honoured(monkeypatch):
    """The agent sends the enabled set; the handler must pass it down rather
    than falling back to every enabled document."""
    c, seen = _wire(monkeypatch, [])
    with c:
        r = c.post("/v1/documents/search",
                   json={"query": "x", "doc_ids": ["doc-a"]})
    assert r.status_code == 200, r.text
    assert seen["doc_ids"] == {"doc-a"}, seen
