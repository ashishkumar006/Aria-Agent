"""The mixed-embedding-model comparability guard in MemoryService.

The defect these tests pin down: ``_embed_model()`` read ``os.environ`` without
``os`` being imported, so the NameError was swallowed and it always returned
``""``. Both halves of the guard live behind ``if live:`` / ``and
self._embed_model()``, so an empty live model made the WHOLE guard inert — a
record embedded with a different model (or a different width) was returned as a
vector neighbour, comparing two incomparable embedding spaces and quietly
serving nonsense.

A second, related defect: ``_embed_dim()`` returned the pinned ``EMBED_DIM``
constant rather than the live index's width, so once the guard was switched on
it would have excluded every record from a service built with a different
``expected_dim``. The guard must judge against what the index actually holds.

Each test is a MUTATION CHECK: it fails if the fix is reverted, and the mutation
it catches is named in the docstring.
"""
from __future__ import annotations

import re
import sys
import zlib
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from embedders import EMBED_DIM  # noqa: E402
from memory.models import MemoryRecord  # noqa: E402
from memory.service import MemoryService  # noqa: E402


def _bow(text: str, dim: int) -> list[float]:
    v = [0.0] * dim
    for w in re.findall(r"\w+", (text or "").lower()):
        if len(w) > 2:
            v[zlib.crc32(w.encode()) % dim] += 1.0
    return v


def _rec(rid: str, *, model: str | None, dim: int) -> MemoryRecord:
    return MemoryRecord(
        id=rid, kind="fact", drawer="fact", descriptor="d", value={},
        keywords=[], source="t", run_id="r",
        embedding=[0.1] * dim, embed_dim=dim, embed_model=model)


def test_embed_model_reads_the_environment(tmp_path, monkeypatch):
    """MUTATION: drop `import os` (or return "") → _embed_model() is empty and
    the guard behind it never runs."""
    svc = MemoryService(tmp_path, embed_fn=None)
    monkeypatch.setenv("EMBED_OLLAMA_MODEL", "nomic-embed-text")
    assert svc._embed_model() == "nomic-embed-text"
    monkeypatch.setenv("EMBED_OLLAMA_MODEL", "some-other-model")
    assert svc._embed_model() == "some-other-model", \
        "the live model must track the environment, not a hardcoded default"


def test_matches_index_excludes_a_foreign_embedding_model(tmp_path, monkeypatch):
    """MUTATION: revert to the inert guard (_embed_model() == "") → the foreign
    record is judged comparable and returned True."""
    monkeypatch.setenv("EMBED_OLLAMA_MODEL", "model-live")
    svc = MemoryService(tmp_path, embed_fn=None)
    assert svc._matches_index(_rec("mem:old", model="model-old", dim=EMBED_DIM)) is False
    assert svc._matches_index(_rec("mem:live", model="model-live", dim=EMBED_DIM)) is True


def test_record_without_a_model_is_not_excluded(tmp_path, monkeypatch):
    """A legacy bare-vector record carries no model provenance; the guard must
    not over-exclude it. MUTATION: an unconditional `!=` without the truthiness
    check would drop every model-less record."""
    monkeypatch.setenv("EMBED_OLLAMA_MODEL", "model-live")
    svc = MemoryService(tmp_path, embed_fn=None)
    assert svc._matches_index(_rec("mem:legacy", model=None, dim=EMBED_DIM)) is True


@pytest.mark.asyncio
async def test_vector_search_excludes_a_record_from_a_different_model(
        tmp_path, monkeypatch):
    """End-to-end through _vector_search (NOT read(), whose keyword fusion would
    re-surface an excluded record). Two records share the query's tokens, so
    both are genuine vector neighbours; only the model guard separates them.

    MUTATION: revert `import os` → live == "" → the `if live:` filter is skipped
    → the foreign-model record comes back as a neighbour.
    """
    monkeypatch.setenv("EMBED_OLLAMA_MODEL", "model-live")
    holder = {"m": "model-live"}

    async def embed(text: str, task_type: str):
        return {"embedding": _bow(text, EMBED_DIM), "model": holder["m"],
                "dim": EMBED_DIM}

    svc = MemoryService(tmp_path, embed_fn=embed, expected_dim=EMBED_DIM)
    live = await svc.add_fact("ZZSHAREDZZ alpha deployment canary pipeline",
                              source="t", run_id="r1")
    holder["m"] = "model-old"
    old = await svc.add_fact("ZZSHAREDZZ beta deployment canary rollout",
                             source="t", run_id="r1")
    assert live.embed_model == "model-live" and old.embed_model == "model-old"

    qvec = _bow("ZZSHAREDZZ deployment canary pipeline", EMBED_DIM)
    hits = await svc._vector_search("q", kinds=None, top_k=10,
                                    session_id=None, qvec=qvec)
    ids = {h.id for h in hits}
    assert live.id in ids, "the live-model record should surface"
    assert old.id not in ids, \
        "a foreign-model record was returned as a vector neighbour"


@pytest.mark.asyncio
async def test_vector_guard_judges_dim_against_the_live_index(tmp_path, monkeypatch):
    """A service built with expected_dim=64 indexes 64-wide vectors. The guard
    must keep them (dim judged against the index, not the 768 constant) and
    still reject a genuinely foreign width.

    MUTATION: revert _embed_dim() to `return EMBED_DIM` → 64 != 768 → the
    service's own records are excluded and vector recall returns nothing.
    """
    monkeypatch.setenv("EMBED_OLLAMA_MODEL", "model-live")

    async def embed(text: str, task_type: str):
        return {"embedding": _bow(text, 64), "model": "model-live", "dim": 64}

    svc = MemoryService(tmp_path, embed_fn=embed, expected_dim=64)
    rec = await svc.add_fact("ZZDIMZZ deployment canary pipeline",
                             source="t", run_id="r1")
    assert rec.embed_dim == 64 and svc.index.dim == 64
    assert svc._matches_index(rec) is True, \
        "a record matching the live index width was wrongly excluded"
    assert svc._matches_index(_rec("mem:wide", model="model-live", dim=EMBED_DIM)) is False

    hits = await svc._vector_search("q", kinds=None, top_k=5, session_id=None,
                                    qvec=_bow("ZZDIMZZ deployment canary", 64))
    assert rec.id in {h.id for h in hits}