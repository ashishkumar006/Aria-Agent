"""BM25 keyword recall over the FULL searchable text.

The defect these tests pin down: document chunks keep the whole body in
``value['chunk']`` but only a 200-char prefix in ``descriptor`` and no
keywords. The old keyword path scored ``tokens(descriptor) | keywords``, so
every chunk body past char 200 was invisible to keyword recall while the
vector path (which embeds the full text) could still find it — a silent
half-blind hybrid.

Each test is a MUTATION CHECK: it is written to fail if the fix is reverted,
not merely to pass on the new code. The mutation it catches is named in the
docstring. Scoring is BM25 (length-normalised TF × IDF) over a cached inverted
index; IDF is the score gate, so a ubiquitous token cannot drag unrelated
records into recall while a rare identifier dominates.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from memory.models import MemoryRecord  # noqa: E402
from memory.service import MemoryService  # noqa: E402


@pytest.fixture()
def svc(tmp_path):
    # embed_fn=None → keyword-only service; the vector path stays empty so
    # every assertion isolates _keyword_search.
    return MemoryService(tmp_path, embed_fn=None)


def _rec(rid: str, descriptor: str, *, value: dict | None = None,
         keywords: list[str] | None = None,
         superseded_by: str | None = None) -> MemoryRecord:
    return MemoryRecord(
        id=rid, kind="fact", drawer="document", descriptor=descriptor,
        value=value or {}, keywords=keywords or [], source="t", run_id="r",
        superseded_by=superseded_by)


def _kw(svc, query, **kw):
    return svc._keyword_search(query, None, kinds=kw.get("kinds"),
                               top_k=kw.get("top_k", 10),
                               session_id=kw.get("session_id"))


def _ids(hits):
    return [h.id for h in hits]


# A prefix long enough that body[:200] contains none of the distinctive token.
_FILLER = "ordinary filler words about nothing in particular "  # 49 chars


def test_chunk_body_past_200_chars_is_keyword_searchable(svc):
    """MUTATION: revert _search_text to descriptor-only → the deep token is
    absent from the descriptor and keywords, so this record is not found."""
    body = _FILLER * 6 + " ZZDEEPBODYZZ the distinctive deep token"
    assert "zzdeepbodyzz" not in body[:200].lower()  # self-validating setup
    rec = _rec("mem:deep", body[:200], value={"chunk": body})
    svc.store.append(rec)
    hits = _kw(svc, "ZZDEEPBODYZZ")
    assert "mem:deep" in _ids(hits), \
        "a chunk body past char 200 was invisible to keyword recall"


def test_descriptor_prefix_still_matches(svc):
    """The first 200 chars must remain searchable too (no regression)."""
    body = _FILLER * 6 + " ZZDEEPBODYZZ deep"
    svc.store.append(_rec("mem:deep", body[:200], value={"chunk": body}))
    assert "mem:deep" in _ids(_kw(svc, "ordinary"))


def test_plain_fact_without_a_chunk_uses_descriptor(svc):
    """MUTATION: if _search_text only read value['chunk'], a non-document
    fact (no chunk) would never match."""
    svc.store.append(_rec("mem:fact", "user likes pizza"))
    assert "mem:fact" in _ids(_kw(svc, "pizza"))


def test_explicit_keywords_are_searched(svc):
    """MUTATION: drop keywords from _search_text → this record is not found."""
    svc.store.append(_rec("mem:kw", "nothing relevant here",
                          keywords=["zzkeywordtoken"]))
    assert "mem:kw" in _ids(_kw(svc, "zzkeywordtoken"))


def test_idf_lets_a_rare_token_outrank_two_ubiquitous_ones(svc):
    """MUTATION: revert to flat overlap counting → the 20 records matching
    BOTH common tokens (overlap 2) outrank the rare record (overlap 1).

    BM25 IDF weights the rare identifier far above the two near-ubiquitous
    tokens, so the rare record must come first. This is the score gate: a
    word in almost every record contributes almost nothing.
    """
    b_ids = []
    for i in range(20):
        rid = f"mem:common-{i}"
        b_ids.append(rid)
        svc.store.append(_rec(rid, f"common1 common2 filler text number {i}"))
    rare = _rec("mem:rare", "zzrarezz solitary marker")
    svc.store.append(rare)

    hits = _kw(svc, "zzrarezz common1 common2", top_k=25)
    assert hits, "expected candidates"
    assert hits[0].id == "mem:rare", \
        f"ubiquitous tokens outvoted a rare identifier: {_ids(hits)[:3]}"
    # Every common record is still a candidate (recall preserved), just ranked
    # below the rare one.
    assert set(b_ids) <= set(_ids(hits))


def test_superseded_records_never_surface(svc):
    """MUTATION: stop skipping superseded rows in _kw_build → the corrected
    claim reappears in recall."""
    svc.store.append(_rec("mem:live", "zzsupersededtoken live claim"))
    svc.store.append(_rec("mem:old", "zzsupersededtoken old claim",
                          superseded_by="mem:live"))
    assert _ids(_kw(svc, "zzsupersededtoken")) == ["mem:live"]


def test_index_cache_invalidates_on_write(svc):
    """MUTATION: never rebuild the cached index → a record appended after the
    first search is invisible to the next one."""
    svc.store.append(_rec("mem:a", "zzfirsttoken alpha"))
    assert "mem:a" in _ids(_kw(svc, "zzfirsttoken"))  # builds + caches
    first_key = svc._kw_key

    svc.store.append(_rec("mem:b", "zzsecondtoken beta"))  # changes the file
    assert "mem:b" in _ids(_kw(svc, "zzsecondtoken")), \
        "a write did not invalidate the keyword index cache"
    assert svc._kw_key != first_key
    # The rebuild keeps the old record too.
    assert "mem:a" in _ids(_kw(svc, "zzfirsttoken"))


def test_corpus_stats_exclude_superseded_rows(svc):
    """N and document frequency reflect the LIVE store, so IDF is computed
    over what can actually be returned."""
    svc.store.append(_rec("mem:live", "zzstatstoken live"))
    svc.store.append(_rec("mem:old", "zzstatstoken old",
                          superseded_by="mem:live"))
    index = svc._kw_get()
    assert index["N"] == 1
    assert index["df"]["zzstatstoken"] == 1


def test_empty_and_unmatched_queries_return_empty(svc):
    svc.store.append(_rec("mem:a", "zzfirsttoken alpha"))
    assert _kw(svc, "") == []
    assert _kw(svc, "zzznosuchtokenzzz") == []


@pytest.mark.asyncio
async def test_deep_body_match_survives_rrf_fusion(tmp_path):
    """End-to-end: with the embedder dead the vector path returns nothing, so
    a deep-body keyword match must still reach the caller through read()."""
    svc = MemoryService(tmp_path, embed_fn=None)
    body = _FILLER * 6 + " ZZREADPATHZZ deep in the body"
    rec = _rec("mem:deep", body[:200], value={"chunk": body})
    svc.store.append(rec)
    hits = await svc.read("ZZREADPATHZZ", top_k=5)
    assert "mem:deep" in _ids(hits)