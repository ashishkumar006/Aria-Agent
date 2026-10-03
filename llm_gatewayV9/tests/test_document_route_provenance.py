"""Regression tests for two defects found while fixing PDF table retrieval.

Both were invisible to the test suite because each one fails only on a path the
suite never drove: a route nobody called, and a field nobody read.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import memory_api  # noqa: E402
from documents import registry as reg_mod  # noqa: E402


def test_every_status_constant_is_bound_where_the_registry_is_used():
    """`reg.set_status(doc_id, PENDING, ...)` raised NameError, not because the
    constant was wrong but because it was never imported. Python cannot catch
    this: the name only appears inside a function body, so the module imports
    cleanly and the route 500s only when called.

    Every status constant is therefore checked against the module namespace, so
    adding a status to the registry cannot leave a caller referencing a name it
    does not have.
    """
    statuses = {n for n in dir(reg_mod)
                if n.isupper() and isinstance(getattr(reg_mod, n), str)}
    assert len(statuses) >= 6, statuses
    missing = sorted(s for s in statuses if not hasattr(memory_api, s))
    assert not missing, (
        f"memory_api references registry statuses it never imported: {missing}. "
        "Each would raise NameError on its code path instead of doing its job."
    )


@pytest.mark.parametrize("name", ["documents_reindex", "documents_search"])
def test_document_routes_are_importable_and_callable(name):
    """A route whose body raises NameError still looks fine at import time and
    in the OpenAPI schema. Asserting the symbols exist keeps the failure at
    import rather than at request time."""
    fn = getattr(memory_api, name, None)
    assert fn is not None, f"{name} is missing"
    assert callable(fn)


def test_reindex_does_not_reference_an_unbound_status(monkeypatch):
    """Drive the reindex body far enough to reach the status update.

    The original defect: `status=PENDING` raised NameError, so reindex returned
    500 and left the document stuck. Now the update must actually happen.
    """
    calls = {}

    class _Doc:
        def __init__(self):
            self.status = "ready"

        def to_dict(self):
            return {"status": self.status}

    class _Reg:
        def get(self, doc_id):
            return _Doc()

        def update(self, doc_id, **kw):
            calls.update(kw)

    class _Svc:
        class _Store:
            def load(self):
                return []

        store = _Store()

        def delete_one(self, rid):
            pass

    class _Plane:
        drawers = {"document": _Svc()}

    class _Req:
        pass

    monkeypatch.setattr(memory_api, "_docs", lambda: _Reg())
    monkeypatch.setattr(memory_api, "_plane", lambda r: _Plane())
    monkeypatch.setattr(memory_api, "_start_index",
                        lambda r, d: calls.setdefault("started", d))

    import asyncio
    asyncio.run(memory_api.documents_reindex("doc-x", _Req()))

    assert calls.get("status") == reg_mod.PENDING, calls
    assert calls.get("chunk_count") == 0, calls
    assert calls.get("started") == "doc-x", calls


def test_search_hit_reports_the_page_number():
    """`"page": h.page if hasattr(h, "page") else None` always took the None
    branch, because the page is stored in the record's `value` dict. Every hit
    reported `page: null`, so a citation on a paged PDF could not name a page.
    """
    class _DocRef:
        doc_id = "doc-1"
        chunk_index = 4
        total_chunks = 9

    class _Hit:
        id = "mem:doc-doc-1-4"
        doc = _DocRef()
        value = {"chunk": "Meal | Monday", "doc_id": "doc-1",
                 "chunk_index": 4, "heading_path": ["Menu"],
                 "page": 3}
        descriptor = "Meal | Monday"
        embed_model = "nomic-embed-text"

    class _Reg:
        def get(self, doc_id):
            class _D:
                filename = "menu.pdf"
            return _D()

    out = memory_api._document_hit_payload(_Hit(), _Reg())
    assert out["page"] == 3, out
    assert out["filename"] == "menu.pdf"
    assert out["heading_path"] == ["Menu"]
    assert out["chunk_index"] == 4


def test_a_hit_with_no_page_reports_none_rather_than_raising():
    """Prose documents have no page. That must be an honest null, not a crash."""

    class _DocRef:
        doc_id = "doc-2"
        chunk_index = 0

    class _Hit:
        id = "mem:doc-doc-2-0"
        doc = _DocRef()
        value = {"chunk": "text"}
        descriptor = "text"
        embed_model = None

    class _Reg:
        def get(self, doc_id):
            return None

    out = memory_api._document_hit_payload(_Hit(), _Reg())
    assert out["page"] is None, out
    assert out["filename"] == ""


def test_chunk_page_survives_from_block_to_record():
    """End to end on the value: a table block on page 1 must reach the stored
    record, because that is the only place the search response reads it from."""
    from documents.parsers import Block
    from documents.chunker import chunk_blocks

    blocks = [Block(kind="table", header=["Meal", "Monday"],
                    rows=[["BREAKFAST", "Idli"]], page=1,
                    text="Meal | Monday\nBREAKFAST | Idli")]
    chunks = chunk_blocks(blocks, target_words=150)
    assert chunks, "table block produced no chunks"
    assert chunks[0].page == 1, chunks[0].page


def test_parse_pdf_sets_page_on_every_table_block(monkeypatch):
    """The parser must stamp the page it read the table from; without it a
    multi-page PDF's tables all cite page 1."""
    import documents.parsers as P

    class _Page:
        def extract_tables(self, settings):
            # The header must win the "row with the most short cells" heuristic,
            # so the data rows carry at least one blank cell and the header is
            # the first row to reach that score. Two labelled groups, because a
            # single group is not a table.
            return [[["", "Mon", "Tue"],
                     ["", "Idli (2 pcs)", "Dosa"],
                     ["LUNCH", "Rice", ""],
                     ["", "Sambar", "Dal"],
                     ["", "Roti", "Paneer"],
                     ["DINNER", "Khichdi", ""],
                     ["", "Papad", "Salad"]]]

    class _PDF:
        pages = [_Page(), _Page()]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import pdfplumber
    monkeypatch.setattr(pdfplumber, "open", lambda *a, **k: _PDF())
    blocks, _warn = P._pdf_table_blocks(b"%PDF-1.4 fake")
    assert [b.page for b in blocks] == [1, 2], [b.page for b in blocks]
