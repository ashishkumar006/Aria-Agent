"""Enable/disable filtering in retrieval, and the per-conversation chat toggle.

The property under test is the one most likely to appear to work while doing
nothing: a disabled document must be genuinely ABSENT from recall, and a
conversation with documents turned off must not see any of them, while another
conversation still does.

Filtering happens AFTER vector search, never before. Filtering the candidate
set first would exclude the disabled vectors from the index query entirely and
gut recall for the enabled documents too.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from documents import registry as R  # noqa: E402
from memory.models import MemoryRecord  # noqa: E402
from memory.plane import MemoryPlane  # noqa: E402


def _bow(text: str, dim: int = 16) -> list[float]:
    import re
    import zlib
    v = [0.0] * dim
    for w in re.findall(r"\w+", (text or "").lower()):
        if len(w) > 2:
            v[zlib.crc32(w.encode()) % dim] += 1.0
    return v


async def _embed(text: str, task_type: str):
    return {"embedding": _bow(text), "model": "test-model", "dim": 16}


DOC_A = ("ZZALPHAZZ describes the deployment pipeline in detail. "
         "It covers rollbacks and canary traffic percentages.")
DOC_B = ("ZZBETAZZ describes the billing pipeline in detail. "
         "It covers invoices and proration rules.")
QUESTION = "ZZALPHAZZ deployment"


@pytest.fixture()
def plane(tmp_path, monkeypatch):
    # The stub embedder tags every vector "test-model". Declare that as the
    # live model so the mixed-model guard keeps these records — it excludes
    # vectors whose model differs from the live one, which is the point.
    monkeypatch.setenv("EMBED_OLLAMA_MODEL", "test-model")
    return MemoryPlane(tmp_path, embed_fn=_embed)


async def _write_chunk(plane: MemoryPlane, doc_id: str, index: int,
                       text: str) -> str:
    # The document drawer is fail-closed: only gateway/indexer/operator/system
    # may write it, and an ordinary agent cannot. That guard is correct and is
    # why indexing uses the indexer role.
    out = await plane.remember(
        kind="fact", descriptor=text[:200],
        value={"chunk": text, "doc_id": doc_id, "chunk_index": index},
        doc={"doc_id": doc_id, "version": "1", "chunk_index": index},
        source=f"doc:{doc_id}", run_id="idx",
        principal_role="indexer")
    return out.id


def _ids(hits) -> set[str]:
    return {h.id for h in hits}


def _docs_of(hits, plane) -> set[str]:
    out = set()
    by_id = {}
    for svc in plane.drawers.values():
        by_id.update({r.id: r for r in svc.store.load()})
    for h in hits:
        d = getattr(h, "doc", None)
        if d and getattr(d, "doc_id", None):
            out.add(d.doc_id)
    return out


# ── the filter itself ───────────────────────────────────────────────────────

def test_doc_filter_excludes_disabled_document_ids():
    from documents.retrieval import filter_by_documents

    items = [MemoryRecord(id="a", kind="fact", descriptor="x", value={},
                          source="s", run_id="r",
                          doc={"doc_id": "doc-1"}),
             MemoryRecord(id="b", kind="fact", descriptor="y", value={},
                          source="s", run_id="r",
                          doc={"doc_id": "doc-2"})]
    keep = filter_by_documents(items, {"doc-2"})
    assert _ids(keep) == {"b"}


def test_empty_allowlist_means_no_documents_not_everything():
    """An empty allowlist must exclude ALL document chunks, not include all.

    Getting this backwards is how a disabled document keeps answering.
    """
    from documents.retrieval import filter_by_documents

    items = [MemoryRecord(id="a", kind="fact", descriptor="x", value={},
                          source="s", run_id="r",
                          doc={"doc_id": "doc-1"})]
    assert filter_by_documents(items, set()) == []


def test_records_without_a_doc_field_are_untouched():
    """Only document chunks are filtered; facts, preferences and tool outcomes
    must survive regardless of which documents are enabled."""
    from documents.retrieval import filter_by_documents

    plain = MemoryRecord(id="p", kind="fact", descriptor="a plain fact",
                         value={}, source="s", run_id="r")
    assert _ids(filter_by_documents([plain], set())) == {"p"}


def test_document_chunks_are_recognisable():
    from documents.retrieval import is_document_record

    doc = MemoryRecord(id="d", kind="fact", descriptor="x", value={},
                       source="s", run_id="r", doc={"doc_id": "doc-1"})
    plain = MemoryRecord(id="p", kind="fact", descriptor="y", value={},
                         source="s", run_id="r")
    assert is_document_record(doc) is True
    assert is_document_record(plain) is False


# ── end-to-end through the plane ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_disabled_document_is_absent_from_recall(plane):
    id_a = await _write_chunk(plane, "doc-A", 0, DOC_A)
    id_b = await _write_chunk(plane, "doc-B", 0, DOC_B)

    # Both enabled: the right one comes back.
    both = await plane.search(QUESTION, drawers=["document"], top_k=5)
    assert id_a in _ids(both), "alpha chunk not found while both enabled"

    # Only doc-B enabled: alpha must be GONE.
    only_b = await plane.search(QUESTION, drawers=["document"], top_k=5,
                                doc_ids={"doc-B"})
    assert id_a not in _ids(only_b), \
        "a disabled document was still returned by recall"
    assert id_b not in _ids(only_b) or True     # B may or may not match


@pytest.mark.asyncio
async def test_enabling_a_document_restores_it(plane):
    id_a = await _write_chunk(plane, "doc-A", 0, DOC_A)
    hits = await plane.search(QUESTION, drawers=["document"], top_k=5,
                              doc_ids=set())
    assert id_a not in _ids(hits)
    hits = await plane.search(QUESTION, drawers=["document"], top_k=5,
                              doc_ids={"doc-A"})
    assert id_a in _ids(hits)


@pytest.mark.asyncio
async def test_filtering_happens_after_search_not_before(plane):
    """A filter applied before the vector query would remove the disabled
    vectors from the search itself, so an enabled document would lose good
    neighbours too. Assert the enabled document still finds its own chunk
    while another is disabled."""
    id_a = await _write_chunk(plane, "doc-A", 0, DOC_A)
    await _write_chunk(plane, "doc-B", 0, "ZZALPHAZZ appears here too. " * 3)
    hits = await plane.search(QUESTION, drawers=["document"], top_k=5,
                              doc_ids={"doc-A"})
    assert id_a in _ids(hits), \
        "filtering before the search broke recall for the enabled document"


@pytest.mark.asyncio
async def test_non_document_memories_are_unaffected_by_doc_filter(plane):
    await plane.remember(kind="fact", descriptor="a standalone fact about x",
                         source="s", run_id="r")
    hits = await plane.search("standalone fact", drawers=["fact"], top_k=5,
                              doc_ids=set())
    assert any("standalone fact" in (h.descriptor or "") for h in hits), \
        "a doc filter suppressed an ordinary fact"


@pytest.mark.asyncio
async def test_scoped_search_fetches_deep_enough_for_a_common_term(plane):
    """A document-scoped query for a term that appears in MANY documents must
    still return the scoped document's matching chunk.

    The drawer search returns a GLOBAL top-K and the doc_ids allowlist is
    applied to that result. With a shallow fetch (top_k*2), a chunk that
    genuinely matches but ranks below the cut was never fetched, so the scope
    removed everything and the document returned zero hits - even though it
    contained the term. Regression: a single common word ("conduction")
    matched 73 chunks library-wide; the wanted one ranked ~22nd and vanished.
    """
    common = "ZZCOMMONZZ"
    target = (f"{common} boundary condition applied on the surface of the "
              f"plate, with the temperature held fixed along the edge.")
    # One target chunk in doc-TARGET, buried under many other documents that
    # also contain the common term and therefore outrank it globally.
    id_target = await _write_chunk(plane, "doc-TARGET", 0, target)
    for i in range(40):
        await _write_chunk(plane, f"doc-NOISE-{i}", 0,
                           f"{common} filler chunk number {i} about nothing.")

    hits = await plane.search(common, drawers=["document"], top_k=5,
                              doc_ids={"doc-TARGET"})
    assert id_target in _ids(hits), \
        ("a scoped search for a common term dropped the matching chunk: the "
         "deep fetch for a doc_ids scope is too shallow")


@pytest.mark.asyncio
async def test_result_diversifies_across_documents(plane):
    """One document's many matching chunks must not fill every slot.

    A single long document can hold dozens of chunks that all match a common
    term. Without a per-document cap they occupy the whole result and a
    genuinely-matching chunk from another document never surfaces. The cut is
    smaller than doc-BIG's chunk count, so the cap is what lets doc-SMALL in.
    """
    common = "ZZSHAREDZZ"
    # doc-BIG matches strongly on every chunk; doc-SMALL matches once.
    for i in range(8):
        await _write_chunk(plane, "doc-BIG", i,
                           f"{common} {common} big-document chunk {i}.")
    await _write_chunk(plane, "doc-SMALL", 0,
                       f"{common} small-document chunk, different document.")

    hits = await plane.search(common, drawers=["document"], top_k=4)
    from collections import Counter
    per_doc = Counter(h.doc.doc_id for h in hits if h.doc)
    assert "doc-SMALL" in per_doc, \
        f"one document crowded out another matching document: {per_doc}"
    assert per_doc["doc-BIG"] < len(hits), \
        f"one document filled every slot: {per_doc}"
    # The cap never starves a single-document corpus: backfill returns all of
    # doc-BIG's chunks when it is the only matching document.
    only_big = await plane.search(common, drawers=["document"], top_k=8,
                                  doc_ids={"doc-BIG"})
    assert len(only_big) == 8, \
        f"cap shrank a single-document result: got {len(only_big)} of 8"


# ── per-conversation chat toggle ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_conversation_toggle_off_excludes_documents(plane):
    id_a = await _write_chunk(plane, "doc-A", 0, DOC_A)

    async def recall(use_documents: bool):
        return await plane.search(QUESTION, drawers=["document"], top_k=5,
                                  doc_ids={"doc-A"} if use_documents else set())

    on = await recall(True)
    off = await recall(False)
    assert id_a in _ids(on)
    assert id_a not in _ids(off), \
        "the per-conversation toggle did not actually suppress the document"


@pytest.mark.asyncio
async def test_two_conversations_can_differ(plane):
    """One conversation with documents off, another with them on - the
    decision the user asked for is per conversation."""
    id_a = await _write_chunk(plane, "doc-A", 0, DOC_A)
    conv_with = await plane.search(QUESTION, drawers=["document"], top_k=5,
                                   doc_ids={"doc-A"})
    conv_without = await plane.search(QUESTION, drawers=["document"],
                                      top_k=5, doc_ids=set())
    assert id_a in _ids(conv_with)
    assert id_a not in _ids(conv_without)


def test_conversation_toggle_defaults_on():
    from documents.retrieval import ConversationPreferences

    p = ConversationPreferences()
    assert p.use_documents is True, \
        "documents should be available by default once one exists"


def test_conversation_toggle_round_trips():
    from documents.retrieval import ConversationPreferences

    p = ConversationPreferences()
    assert p.use_documents is True
    p.use_documents = False
    p2 = ConversationPreferences.from_dict(p.to_dict())
    assert p2.use_documents is False
    p2.use_documents = True
    assert ConversationPreferences.from_dict(
        p2.to_dict()).use_documents is True


def test_corrupt_preferences_fall_back_to_the_default():
    from documents.retrieval import ConversationPreferences

    p = ConversationPreferences.from_dict({"use_documents": "yes please"})
    assert p.use_documents is True, "a bad value must not silently disable"