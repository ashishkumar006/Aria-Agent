"""Memory-service tests (deterministic, no live LLM / Ollama needed).

Service-tier tests drive the REAL ``MemoryService`` against an isolated
tmp state dir with a deterministic bag-of-words fake embedder. API-tier
tests drive the real FastAPI routes in-process; the write path still
attempts a real embed via the lifespan embedders, but every assertion
holds whether Ollama is up (item gets a vector) or down (item persists
vectorless and stays reachable through the keyword fallback).
"""
from __future__ import annotations

import json
import re
import sys
import threading
import zlib
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))

from memory.service import MemoryService  # noqa: E402


# ── deterministic fake embedder ────────────────────────────────────────────

def _bow(text: str, dim: int = 64) -> list[float]:
    vec = [0.0] * dim
    for w in re.findall(r"\w+", text.lower()):
        if len(w) > 2:
            vec[zlib.crc32(w.encode()) % dim] += 1.0
    return vec


async def _fake_embed(text: str, task_type: str) -> list[float]:
    return _bow(text)


async def _dead_embed(text: str, task_type: str):
    return None


@pytest.fixture()
def svc(tmp_path):
    return MemoryService(tmp_path, embed_fn=_fake_embed)


# ── write paths ────────────────────────────────────────────────────────────

class TestWrites:
    async def test_add_fact_embeds_and_persists(self, svc, tmp_path):
        item = await svc.add_fact(
            "Python is a programming language",
            value={"category": "language"}, keywords=["python", "programming"],
            source="index_document", run_id="r1")
        assert item.kind == "fact"
        assert item.embedding is not None and len(item.embedding) == 64
        assert (tmp_path / "memory.json").exists()
        raw = json.loads((tmp_path / "memory.json").read_text())
        assert len(raw) == 1 and raw[0]["descriptor"].startswith("Python")

    async def test_scratchpad_skips_embedding(self, tmp_path):
        calls: list[str] = []

        async def counting(text: str, task_type: str):
            calls.append(text)
            return _bow(text)

        s = MemoryService(tmp_path, embed_fn=counting)
        item = await s.remember(kind="scratchpad", descriptor="temp note",
                                source="t", run_id="r1")
        assert item.embedding is None
        assert calls == []

    async def test_descriptor_capped(self, svc):
        item = await svc.add_fact("word " * 500, source="t", run_id="r1")
        assert len(item.descriptor) <= 200

    async def test_record_outcome_zero_llm(self, svc):
        item = await svc.record_outcome(
            tool="get_weather", arguments={"location": "London"},
            result_text="22C sunny", artifact_id=None,
            run_id="r1", goal_id="g1")
        assert item.kind == "tool_outcome"
        assert item.embedding is not None
        assert "get_weather" in item.descriptor
        assert item.value["tool"] == "get_weather"

    async def test_dim_mismatch_raises_before_persist(self, svc):
        await svc.add_fact("first fact here", source="t", run_id="r1")

        async def wide(text: str, task_type: str):
            return [1.0] * 16

        svc._embed = wide  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="[Dd]im"):
            await svc.add_fact("second fact here", source="t", run_id="r1")
        # Loud refusal must not leave a half-written store.
        assert len(svc.store.load()) == 1


# ── read paths ─────────────────────────────────────────────────────────────

class TestReads:
    async def _seed(self, svc):
        await svc.add_fact("Python is a programming language",
                           source="t", run_id="r1")
        await svc.add_fact("JavaScript is used for web development",
                           source="t", run_id="r1")
        await svc.add_fact("The weather in London is rainy",
                           source="t", run_id="r1")

    async def test_vector_search_returns_similar(self, svc):
        await self._seed(svc)
        hits = await svc.read("programming languages", top_k=3)
        assert len(hits) >= 1
        assert "python" in hits[0].descriptor.lower()

    async def test_keyword_fallback_when_embed_dead(self, tmp_path):
        s = MemoryService(tmp_path, embed_fn=_dead_embed)
        await s.add_fact("Machine learning is a subset of AI",
                         source="t", run_id="r1")
        hits = await s.read("machine learning")
        assert len(hits) >= 1
        assert "machine" in hits[0].descriptor.lower()

    async def test_read_filters_by_kind(self, svc):
        await svc.add_fact("user likes pizza", source="t", run_id="r1")
        await svc.record_outcome(tool="get_time", arguments={"timezone": "UTC"},
                                 result_text="12:00", artifact_id=None,
                                 run_id="r1", goal_id="g1")
        facts = await svc.read("pizza", kinds=["fact"])
        assert facts and all(f.kind == "fact" for f in facts)
        outcomes = await svc.read("time", kinds=["tool_outcome"])
        assert all(o.kind == "tool_outcome" for o in outcomes)

    async def test_read_respects_top_k(self, svc):
        for i in range(10):
            await svc.add_fact(f"shared token fact number {i}",
                               source="t", run_id="r1")
        hits = await svc.read("shared token fact", top_k=3)
        assert len(hits) <= 3

    async def test_empty_query_returns_empty(self, svc):
        assert await svc.read("") == []
        assert await svc.read("   ") == []

    async def test_no_match_returns_empty(self, svc):
        """An empty store returns nothing (FAISS returns nearest-first, so
        'no match' is only well-defined when there is nothing to match)."""
        assert await svc.read("zzzznonexistent") == []

    async def test_persistence_across_instances(self, tmp_path):
        s1 = MemoryService(tmp_path, embed_fn=_fake_embed)
        await s1.add_fact("persistent fact here", source="t", run_id="r1")
        s2 = MemoryService(tmp_path, embed_fn=_fake_embed)
        hits = await s2.read("persistent fact")
        assert len(hits) >= 1


# ── sessions / maintenance ─────────────────────────────────────────────────

class TestSessions:
    async def test_session_items_scoped(self, svc):
        await svc.add_fact("global fact alpha", source="t", run_id="r1")
        await svc.add_fact("session fact beta", source="t", run_id="r1",
                           session_id="s1")
        other = await svc.read("fact", session_id="s2")
        assert all("beta" not in h.descriptor for h in other)
        own = await svc.read("session fact beta", session_id="s1")
        assert any("beta" in h.descriptor for h in own)

    async def test_session_clear_keeps_globals(self, svc):
        await svc.add_fact("global fact alpha", source="t", run_id="r1")
        await svc.add_fact("session fact beta", source="t", run_id="r1",
                           session_id="s1")
        removed = svc.clear(session_id="s1")
        assert removed == 1
        assert len(svc.store.load()) == 1

    async def test_global_clear_empties_everything(self, svc, tmp_path):
        await svc.add_fact("to be cleared", source="t", run_id="r1")
        assert (tmp_path / "index.faiss").exists()
        removed = svc.clear()
        assert removed == 1
        assert svc.store.load() == []
        assert not (tmp_path / "index.faiss").exists()

    async def test_corrupt_file_loads_empty(self, svc, tmp_path):
        (tmp_path / "memory.json").write_text("{not valid json")
        assert await svc.read("anything") == []

    async def test_empty_file_returns_empty(self, svc, tmp_path):
        (tmp_path / "memory.json").write_text("")
        assert await svc.read("anything") == []

    async def test_stats(self, svc):
        await svc.add_fact("stat fact one", source="t", run_id="r1")
        st = svc.stats()
        assert st["items"] == 1 and st["by_kind"]["fact"] == 1
        assert st["index_size"] == 1 and st["embed_dim"] == 768

    async def test_startup_reconciles_orphan_vectors(self, tmp_path):
        """A stale index (ids for deleted items) is rebuilt, loudly."""
        s1 = MemoryService(tmp_path, embed_fn=_fake_embed, expected_dim=64)
        await s1.add_fact("live fact here", source="t", run_id="r1")
        # Simulate pre-migration drift: an orphan vector for a deleted item.
        s1.index.add("mem:deleted999", _bow("ghost"))
        s1.index.persist()
        assert s1.index.size == 2
        s2 = MemoryService(tmp_path, embed_fn=_fake_embed, expected_dim=64)
        assert s2.index is not None
        assert s2.index.size == 1
        hits = await s2.read("live fact")
        assert len(hits) == 1


# ── concurrency ────────────────────────────────────────────────────────────

class TestConcurrency:
    async def test_concurrent_writes_dont_lose_data(self, tmp_path):
        s = MemoryService(tmp_path, embed_fn=_fake_embed)
        errors: list[Exception] = []

        def write(i: int):
            import asyncio
            try:
                asyncio.run(s.add_fact(f"concurrent fact {i}",
                                       source="t", run_id="r1"))
            except Exception as e:  # pragma: no cover - surfaced below
                errors.append(e)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(s.store.load()) == 10


# ── RRF fusion (vector + keyword, never fallback-starved) ──────────────────

def _rec(id_suffix: str, descriptor: str = "d"):
    from memory.models import MemoryRecord
    return MemoryRecord(id=f"mem:{id_suffix}", kind="fact",
                        descriptor=descriptor, source="t", run_id="r1")


class TestRRFUnit:
    def test_both_lists_beats_single(self):
        from memory.service import rrf_fuse
        a, b, c = _rec("a"), _rec("b"), _rec("c")
        # a: vec2+kw1, b: vec1 only, c: vec3+kw2 → doubly-evidenced c
        # outranks singly-evidenced b despite b's vector rank 1.
        out = rrf_fuse([b, a, c], [a, c], top_k=3)
        assert [i.id for i in out] == ["mem:a", "mem:c", "mem:b"]

    def test_single_list_coverage_preserved(self):
        """Fused recall is never worse than either path alone."""
        from memory.service import rrf_fuse
        a, b = _rec("a"), _rec("b")
        assert [i.id for i in rrf_fuse([a], [], top_k=5)] == ["mem:a"]
        assert [i.id for i in rrf_fuse([], [b], top_k=5)] == ["mem:b"]
        assert rrf_fuse([], [], top_k=5) == []

    def test_dedup_and_top_k(self):
        from memory.service import rrf_fuse
        a, b, c = _rec("a"), _rec("b"), _rec("c")
        out = rrf_fuse([a, b, c], [c, b, a], top_k=2)
        assert len(out) == 2 and len({i.id for i in out}) == 2

    def test_deterministic(self):
        from memory.service import rrf_fuse
        a, b = _rec("a"), _rec("b")
        once = [i.id for i in rrf_fuse([a, b], [b, a], top_k=2)]
        twice = [i.id for i in rrf_fuse([a, b], [b, a], top_k=2)]
        assert once == twice


class TestRRFThroughRead:
    async def test_fused_order_through_service(self, tmp_path):
        """Mocked sub-searches disagreeing → fused order through read()."""
        from memory.service import MemoryService
        svc = MemoryService(tmp_path, embed_fn=_fake_embed)
        a, b, c = _rec("a"), _rec("b"), _rec("c")

        async def _vec(query, *, kinds=None, top_k=8, session_id=None,
                       qvec=None):
            return [b, a, c][:top_k]

        def _kw(query, history, *, kinds=None, top_k=8, session_id=None):
            return [a, c][:top_k]

        svc._vector_search = _vec  # type: ignore[method-assign]
        svc._keyword_search = _kw  # type: ignore[method-assign]
        hits = await svc.read("anything", top_k=3)
        assert [h.id for h in hits] == ["mem:a", "mem:c", "mem:b"]


class TestRRFDisagreementEval:
    """Hand-labeled mini-eval: identifier query where pure-vector top-1 is
    wrong and keyword top-1 is right. Old fallback-chain behavior served
    the distractor; fusion must serve the correct doc.

    Setup is self-validating: the test first asserts the sub-search
    orders (vector=[distractor, correct], keyword=[correct]), proving a
    genuine disagreement, then asserts the fused verdict.
    """

    DISTRACTOR = "w00005 w00006 w00040"  # hash-collides with query dims,
    # zero token overlap → vector-strong, keyword-invisible
    CORRECT = ("ERR-2043 clear print spooler queue jammed office paper "
               "tray maintenance log entry today")
    QUERY = "ERR-2043 spooler"

    async def test_fusion_overrules_vector_miss(self, tmp_path):
        from memory.service import MemoryService
        svc = MemoryService(tmp_path, embed_fn=_fake_embed)
        d = await svc.add_fact(self.DISTRACTOR, source="t", run_id="r1")
        c = await svc.add_fact(self.CORRECT, source="t", run_id="r1")
        vec = await svc._vector_search(self.QUERY, kinds=None, top_k=5,
                                       session_id=None)
        kw = svc._keyword_search(self.QUERY, None, kinds=None, top_k=5,
                                 session_id=None)
        # The disagreement that breaks fallback chains:
        assert [h.id for h in vec[:2]] == [d.id, c.id]
        assert [h.id for h in kw] == [c.id]
        # Fused verdict:
        hits = await svc.read(self.QUERY, top_k=1)
        assert [h.id for h in hits] == [c.id]

# ── HTTP routes ────────────────────────────────────────────────────────────

@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def api_client(tmp_path_factory):
    """In-process ASGI client with the memory state dir pointed at tmp."""
    import os

    import memory_api as MA

    tmp = tmp_path_factory.mktemp("memapi")
    os.environ["GATEWAY_MEMORY_STATE"] = str(tmp)
    MA._reset_cache()
    import main as M
    transport = httpx.ASGITransport(app=M.app)
    async with M.app.router.lifespan_context(M.app):
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test",
                                     timeout=60) as c:
            yield c
    MA._reset_cache()
    os.environ.pop("GATEWAY_MEMORY_STATE", None)


@pytest.mark.asyncio
async def test_api_remember_search_list_stats_clear(api_client):
    r = await api_client.post("/v1/memory/remember", json={
        "kind": "fact", "descriptor": "My lucky number is 42 and I love pizza",
        "keywords": ["lucky", "number", "pizza"], "value": {"raw": "seed"},
        "source": "seed", "run_id": "seed-ea04"})
    assert r.status_code == 200, r.text
    item_id = r.json()["item"]["id"]
    assert item_id.startswith("mem:")
    assert r.json()["item"]["drawer"] == "fact"

    r = await api_client.post("/v1/memory/search", json={
        "query": "lucky number", "top_k": 5})
    assert r.status_code == 200, r.text
    assert any(i["id"] == item_id for i in r.json()["items"])

    r = await api_client.get("/v1/memory", params={"limit": 10})
    assert r.status_code == 200, r.text
    assert any(i["id"] == item_id for i in r.json()["items"])

    r = await api_client.get("/v1/memory/stats")
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["drawers"]["fact"]["items"] >= 1

    r = await api_client.request("DELETE", "/v1/memory")
    assert r.status_code == 400, r.text          # unscoped wipe needs consent
    assert "confirm=wipe" in r.json()["error"]

    r = await api_client.request("DELETE", "/v1/memory?confirm=wipe")
    assert r.status_code == 200, r.text
    cleared = r.json()
    assert cleared["fact"] >= 1
    assert "audit_kept" in cleared


@pytest.mark.asyncio
async def test_api_record_outcome(api_client):
    r = await api_client.post("/v1/memory/record_outcome", json={
        "tool": "get_time", "arguments": {"timezone": "UTC"},
        "result_text": "12:00", "run_id": "r1", "goal_id": "g1"})
    assert r.status_code == 200, r.text
    item = r.json()["item"]
    assert item["kind"] == "tool_outcome"
    assert item["value"]["tool"] == "get_time"


@pytest.mark.asyncio
async def test_api_rejects_bad_kind(api_client):
    r = await api_client.post("/v1/memory/remember", json={
        "kind": "nope", "descriptor": "x", "source": "t", "run_id": "r1"})
    assert r.status_code == 422
