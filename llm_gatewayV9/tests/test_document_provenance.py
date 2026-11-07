"""Provenance, section folders, and enable-at-READY.

Three separate gaps, all of the same shape: the Documents plane worked, but
nothing it held could be attributed, filed, or turned on without the user
clicking a switch.

  * A generated report and a hand-uploaded PDF were the same row, so the agent
    could not tell its own output from the user's files.
  * No code ever created a folder for a section, so everything landed in
    "Unfiled".
  * Every upload started switched off and stayed off until the user enabled it
    — which is right at upload time (a half-embedded document answers questions
    in pieces and reports nothing) and wrong as a permanent state.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main as M           # noqa: E402
import memory_api as MA    # noqa: E402
from documents import registry as R  # noqa: E402

MD = b"# Title\n\nA paragraph with enough words to chunk and embed properly.\n"


class MemIndexer(R.Indexer):
    """The registry's state machine without Ollama or FAISS: chunks live in a
    dict, so `run()` can reach READY in a test."""

    def __init__(self, registry, embed=None, **kw):
        super().__init__(registry, embed=embed, **kw)
        self.chunks: dict[str, list[str]] = {}

    def _chunk_texts(self, doc_id, indices):
        if doc_id not in self.chunks:
            from documents import parsers as P
            from documents.chunker import chunk_blocks
            data = self.registry.read_source(doc_id)
            parsed = P.parse(self.registry.get(doc_id).filename, data)
            self.chunks[doc_id] = [c.text for c in chunk_blocks(parsed.blocks)]
        return [self.chunks[doc_id][i] for i in indices]

    def _store_chunks(self, doc_id, indices, vectors, result):
        self.registry.update(doc_id,
                             embed_model=result.get("embed_model", ""),
                             embed_dim=int(result.get("embed_dim") or 0))


def test_provenance_round_trips_through_the_registry(tmp_path):
    reg = R.Registry(tmp_path / "docs")
    d = reg.add(filename="report.pdf", doc_type="pdf", size_bytes=10,
                origin="authoring", origin_ref="art:0123456789abcdef",
                session_id="s8-abc123")
    got = reg.get(d.id).to_dict()
    assert got["origin"] == "authoring"
    assert got["origin_ref"] == "art:0123456789abcdef"
    assert got["session_id"] == "s8-abc123"
    assert got["auto_enable"] is True


def test_a_document_with_no_stated_origin_is_an_upload(tmp_path):
    """The pre-provenance rows in a live registry must not read as generated."""
    reg = R.Registry(tmp_path / "docs")
    d = reg.add(filename="scan.pdf", doc_type="pdf", size_bytes=10)
    assert reg.get(d.id).origin == "upload"


def test_ensure_folder_is_idempotent_and_marks_system(tmp_path):
    reg = R.Registry(tmp_path / "docs")
    a = reg.ensure_folder("Authoring", system=True)
    b = reg.ensure_folder("authoring")          # case-insensitive
    assert a.id == b.id, "two folders for one section"
    assert len(reg.list_folders()) == 1
    assert reg.ensure_folder("Authoring").system is True


def test_ready_enables_an_auto_enable_document(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "YIELD_BETWEEN_BATCHES_S", 0)
    reg = R.Registry(tmp_path / "docs")
    d = reg.add(filename="a.md", doc_type="md", size_bytes=len(MD))
    reg.write_source(d.id, MD)
    idx = MemIndexer(reg, embed=lambda texts: {
        "embeddings": [[0.0] * 4 for _ in texts],
        "embed_model": "stub", "embed_dim": 4})
    out = idx.run(d.id)
    assert out.status == R.READY
    assert reg.get(d.id).enabled is True, (
        "indexing finished and the document is still invisible to retrieval")


def test_partial_embedding_never_enables(tmp_path):
    """The reason enable was manual at all: a half-indexed document answers
    partially and says nothing about it. Auto-enable must not undo that."""
    reg = R.Registry(tmp_path / "docs")
    d = reg.add(filename="big.md", doc_type="md", size_bytes=10)
    reg.update(d.id, chunk_count=50, embedded_count=17)
    reg.set_status(d.id, R.READY)
    assert reg.get(d.id).enabled is False


def test_switching_a_document_off_stops_it_coming_back_on(tmp_path):
    """A re-index after a deliberate 'off' would otherwise re-enable it behind
    the user's back."""
    reg = R.Registry(tmp_path / "docs")
    d = reg.add(filename="a.md", doc_type="md", size_bytes=10)
    reg.update(d.id, chunk_count=2, embedded_count=2)
    reg.set_status(d.id, R.READY)
    assert reg.get(d.id).enabled is True
    doc, err = reg.set_enabled(d.id, False)
    assert err == "" and doc.enabled is False and doc.auto_enable is False
    reg.set_status(d.id, R.PENDING)
    reg.set_status(d.id, R.READY)
    assert reg.get(d.id).enabled is False, "a refused document was re-enabled"


def test_a_legacy_row_without_the_field_is_switched_on_once(tmp_path):
    """Rows written before this change carry no `auto_enable`.

    The first version of this test asserted they must stay manual, because
    defaulting them on widens what an existing conversation can retrieve. That
    reasoning held for a week and then became the complaint: 21 finished,
    embedded documents sat in the library answering nothing, and the rows could
    not tell "nobody ever turned this on" from "the user turned it off" — because
    turning one off did not record an intent either, until it did.

    So the rule is now: no key means no decision was recorded, and a document
    with no recorded decision follows the default that a fresh upload gets. The
    backfill also stamps `auto_enable=True` on the row, so the ambiguity cannot
    be inherited a second time, and a switch-off afterwards is durable."""
    reg = R.Registry(tmp_path / "docs")
    d = reg.add(filename="old.md", doc_type="md", size_bytes=10)
    path = tmp_path / "docs" / "documents.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    for row in rows:
        row.pop("auto_enable", None)
    path.write_text(json.dumps(rows), encoding="utf-8")
    reg.update(d.id, chunk_count=1, embedded_count=1)
    reg.set_status(d.id, R.READY)
    assert reg.get(d.id).enabled is True, "the legacy row was left off"
    assert reg.get(d.id).auto_enable is True, "the row still carries no intent"

    # and once it carries intent, switching it off sticks across re-indexes
    reg.set_enabled(d.id, False)
    reg.set_status(d.id, R.PENDING)
    reg.set_status(d.id, R.READY)
    assert reg.get(d.id).enabled is False, "a refused document was re-enabled"


def test_failed_document_is_disabled_even_when_it_wanted_to_be(tmp_path):
    reg = R.Registry(tmp_path / "docs")
    d = reg.add(filename="broken.pdf", doc_type="pdf", size_bytes=10)
    reg.set_status(d.id, R.READY)
    assert reg.get(d.id).enabled is True
    reg.set_status(d.id, R.FAILED, "no readable text found")
    assert reg.get(d.id).enabled is False


# ── the upload route carries the new fields ─────────────────────────────────

def _client(monkeypatch, tmp_path):
    reg = R.Registry(tmp_path / "documents")
    monkeypatch.setattr(MA, "_docs", lambda: reg)
    return TestClient(M.app), reg


def test_upload_accepts_origin_folder_and_title(monkeypatch, tmp_path):
    c, reg = _client(monkeypatch, tmp_path)
    with c:
        r = c.post("/v1/documents",
                   files={"file": ("notes.md", MD, "text/markdown")},
                   data={"origin": "research", "origin_ref": "https://x.dev/a",
                         "folder_name": "Research", "title": "Notes on A"})
    assert r.status_code == 200, r.text
    doc = r.json()["document"]
    assert doc["origin"] == "research"
    assert doc["origin_ref"] == "https://x.dev/a"
    assert doc["title"] == "Notes on A"
    folders = [f.name for f in reg.list_folders()]
    assert folders == ["Research"], folders
    assert doc["folder_id"], "the document was not filed in the folder it named"


def test_auto_enable_zero_can_be_overridden(monkeypatch, tmp_path):
    c, reg = _client(monkeypatch, tmp_path)
    with c:
        r = c.post("/v1/documents",
                   files={"file": ("quiet.md", MD, "text/markdown")},
                   data={"auto_enable": "0"})
    assert r.json()["document"]["auto_enable"] is False


def test_a_second_upload_to_the_same_folder_reuses_it(monkeypatch, tmp_path):
    c, reg = _client(monkeypatch, tmp_path)
    with c:
        c.post("/v1/documents", files={"file": ("a.md", MD, "text/markdown")},
               data={"folder_name": "Authoring"})
        c.post("/v1/documents", files={"file": ("b.md", MD, "text/markdown")},
               data={"folder_name": "Authoring"})
    assert [f.name for f in reg.list_folders()] == ["Authoring"]


# ── the legacy rows: complete, searchable, and switched off ─────────────────

def _ready_row(reg, name, *, chunks=3, embedded=3, enabled=False,
               auto_enable=True):
    d = reg.add(filename=name, doc_type="pdf", size_bytes=10)
    reg.update(d.id, status=R.READY, chunk_count=chunks, embedded_count=embedded,
               embedded_indices=list(range(embedded)), enabled=enabled,
               auto_enable=auto_enable, index_version=R.INDEX_FORMAT_VERSION)
    return d.id


def test_completed_rows_predating_the_rule_are_switched_on(tmp_path):
    """Enable-at-READY fires when a document REACHES ready, so the rows that
    were already ready when the rule landed stay off forever. They are the ones
    the user sees: a library full of files that `docs off` apparently cannot
    explain. The reconcile pass backfills them."""
    reg = R.Registry(tmp_path / "documents")
    a = _ready_row(reg, "old-a.pdf")
    b = _ready_row(reg, "old-b.pdf")
    assert reg.get(a).enabled is False and reg.get(b).enabled is False
    assert reg.auto_enable_ready() == 2
    assert reg.get(a).enabled is True and reg.get(b).enabled is True
    # and it stays settled: a second pass has nothing to do.
    assert reg.auto_enable_ready() == 0


def test_a_document_switched_off_by_hand_stays_off(tmp_path):
    """The backfill must not overwrite a decision. Switching a document off
    clears its auto-enable intent, which is what this honours."""
    reg = R.Registry(tmp_path / "documents")
    kept = _ready_row(reg, "private.pdf", auto_enable=False)
    open_one = _ready_row(reg, "shared.pdf")
    assert reg.auto_enable_ready() == 1
    assert reg.get(kept).enabled is False
    assert reg.get(open_one).enabled is True


def test_a_partially_embedded_row_is_not_switched_on(tmp_path):
    """Same gate as the live rule: one chunk short of complete answers in
    pieces, so it waits for the indexer rather than being backfilled."""
    reg = R.Registry(tmp_path / "documents")
    partial = _ready_row(reg, "half.pdf", chunks=4, embedded=2)
    assert reg.auto_enable_ready() == 0
    assert reg.get(partial).enabled is False


def test_a_row_predating_the_field_is_switched_on(tmp_path):
    """The rows actually in the library when auto-enable landed: written as JSON
    with no `auto_enable` key at all. Reading a missing key as "do not enable"
    left 21 finished, embedded documents invisible to chat, with no switch
    anyone had ever touched."""
    reg = R.Registry(tmp_path / "documents")
    legacy = _ready_row(reg, "before-the-field.pdf")
    with reg._lock:
        rows = reg._load()
        for row in rows:
            row.pop("auto_enable", None)
        reg._save(rows)
    assert "auto_enable" not in reg._load()[0]
    assert reg.auto_enable_ready() == 1
    assert reg.get(legacy).enabled is True


# ── the same bytes must not become two documents ────────────────────────────

def test_identical_bytes_are_stored_once(monkeypatch, tmp_path):
    """Artifacts are content-addressed, so the agent hands the same bytes back
    repeatedly — a re-render, a re-fetch, the same finding spilled by a second
    run. Each upload used to create its own row and its own copy of every chunk,
    so retrieval answered from one source twice."""
    c, reg = _client(monkeypatch, tmp_path)
    with c:
        first = c.post("/v1/documents",
                       files={"file": ("notes.md", MD, "text/markdown")},
                       data={"origin": "web"})
        again = c.post("/v1/documents",
                       files={"file": ("notes copy.md", MD, "text/markdown")},
                       data={"origin": "authoring"})
    a, b = first.json()["document"], again.json()["document"]
    assert a["id"] == b["id"], "the same bytes were indexed twice"
    assert len(reg.list()) == 1
    # and the second caller still gets a usable document id to link to
    assert b["filename"] == "notes.md"


def test_a_failed_upload_is_not_treated_as_the_existing_copy(monkeypatch, tmp_path):
    """Retrying a file the plane could not parse must re-index it. A FAILED row
    holds no chunks, so pointing the retry at it would report success and leave
    the document unsearchable."""
    c, reg = _client(monkeypatch, tmp_path)
    with c:
        r = c.post("/v1/documents",
                   files={"file": ("notes.md", MD, "text/markdown")},
                   data={})
        did = r.json()["document"]["id"]
        reg.set_status(did, R.FAILED, "no readable text found")
        again = c.post("/v1/documents",
                       files={"file": ("notes.md", MD, "text/markdown")},
                       data={})
    assert again.json()["document"]["id"] != did


# ── reading a document in order ─────────────────────────────────────────────

def test_the_text_route_returns_chunks_in_document_order(monkeypatch, tmp_path):
    """`search_knowledge` ranks, so it cannot answer "read this document" or
    "what does section 4 say". This route is the deterministic path: the same
    chunks that were embedded, at the same indices, in order."""
    c, reg = _client(monkeypatch, tmp_path)
    body = (b"# First section\n\nAlpha paragraph with several words in it.\n\n"
            b"# Second section\n\nBeta paragraph with several words in it.\n")
    with c:
        up = c.post("/v1/documents",
                    files={"file": ("ordered.md", body, "text/markdown")},
                    data={})
        did = up.json()["document"]["id"]
        whole = c.get(f"/v1/documents/{did}/text").json()
        first = c.get(f"/v1/documents/{did}/text",
                      params={"start": 0, "count": 1}).json()
        rest = c.get(f"/v1/documents/{did}/text",
                     params={"start": 1, "count": 10}).json()

    assert whole["total_chunks"] == len(whole["chunks"])
    assert [ch["index"] for ch in whole["chunks"]] == list(
        range(len(whole["chunks"])))
    text = "\n".join(ch["text"] for ch in whole["chunks"])
    assert "Alpha paragraph" in text and "Beta paragraph" in text
    # In order, not by rank: Alpha precedes Beta.
    assert text.index("Alpha") < text.index("Beta")

    # A window walks the document, and `next_start` says where to continue.
    assert first["start"] == 0 and len(first["chunks"]) == 1
    assert first["next_start"] == 1
    assert rest["start"] == 1 and "next_start" not in rest, rest
    assert "Beta paragraph" in rest["chunks"][-1]["text"]


def test_the_text_route_returns_full_text_not_a_preview(monkeypatch, tmp_path):
    """The detail view truncates each chunk to 400 characters for a list. A
    reader needs the whole chunk, or a figure in the second half of a paragraph
    is invisible and the model invents it."""
    c, reg = _client(monkeypatch, tmp_path)
    long_para = "word " * 300  # ~1,500 chars, well past the preview limit
    with c:
        up = c.post("/v1/documents",
                    files={"file": ("long.md", long_para.encode(), "text/markdown")},
                    data={})
        did = up.json()["document"]["id"]
        got = c.get(f"/v1/documents/{did}/text").json()
    joined = " ".join(ch["text"] for ch in got["chunks"])
    assert joined.count("word") == 300, joined[:80]


def test_the_text_route_is_404_for_an_unknown_document(monkeypatch, tmp_path):
    c, _reg = _client(monkeypatch, tmp_path)
    with c:
        r = c.get("/v1/documents/doc-nope/text")
    assert r.status_code == 404, r.text



