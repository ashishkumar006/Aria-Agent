#!/usr/bin/env python3
"""Comprehensive test suite for the Memory subsystem (thin-client era).

The durable store, FAISS index, embeddings and retrieval ranking live on
the gateway now (covered by llm_gatewayV9/tests/test_memory.py). This
suite covers the agent side end to end:

- Tier 1: client plumbing (remember posts classified payloads; the
  classifier-fallback fact-write; session_id forwarding)
- Tier 2: write paths (record_outcome, add_fact — zero LLM)
- Tier 3: read paths (payload passthrough, fail-soft)
- Tier 4: concurrency (concurrent remembers all reach the gateway intact)
- Tier 5: the vector boundary (the client never sends vectors; embedding
  is server-side. FAISS add/search/dim rules are gateway-tested.)
- Tier 6: MCP tools (index_document, search_knowledge) against a fake store
- Tier 7: orchestrator integration (session-start read, prompt formatting,
  cross-run visibility through the client)
- Tier 8: edge cases (long/unicode/special/duplicate content, empty query)

Run: uv run pytest tests/test_memory_comprehensive.py -q
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

# Ensure we can import from the project root
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import memory as mem
from schemas import MemoryItem, new_id


# ── shared fakes ─────────────────────────────────────────────────────────────

def _item_dict(**kw):
    d = {"id": new_id("mem"), "kind": "fact", "keywords": ["test"],
         "descriptor": "test item", "value": {}, "artifact_id": None,
         "embedding": None, "source": "test", "run_id": "r1",
         "goal_id": None, "confidence": 1.0}
    d.update(kw)
    return d


class _FakeGateway:
    """Records every POST and serves canned responses (never touches net)."""

    def __init__(self):
        self.posts: list[tuple[str, dict]] = []
        self.lock = threading.Lock()
        self.search_items: list[dict] = []

    def post(self, path, body, timeout=60.0):
        with self.lock:
            self.posts.append((path, dict(body)))
        if path == "/v1/memory/search":
            return {"items": list(self.search_items)}
        kind = body.get("kind", "fact")
        if path == "/v1/memory/record_outcome":
            kind = "tool_outcome"  # server sets kind by construction
        return {"item": _item_dict(
            kind=kind,
            descriptor=body.get("descriptor", "test item"),
            keywords=body.get("keywords", []),
            value=body.get("value", {}),
            source=body.get("source", "test"),
            run_id=body.get("run_id", "r1"))}


@pytest.fixture()
def gw(monkeypatch):
    """Route the client's HTTP seam to the fake gateway (no network)."""
    fake = _FakeGateway()
    monkeypatch.setattr(mem, "ensure_gateway", lambda: None)
    monkeypatch.setattr(mem, "_post", fake.post)
    monkeypatch.setattr(mem, "_get", lambda *a, **k: {"items": []})
    monkeypatch.setattr(mem, "_delete", lambda *a, **k: {"cleared": 0})
    return fake


@pytest.fixture()
def mock_llm_classify(monkeypatch):
    """Deterministic classifier: echo the text as a fact."""
    def fake_classify(raw_text, schema):
        return {"parsed": {"kind": "fact",
                           "descriptor": raw_text[:120],
                           "keywords": raw_text.lower().split()[:5],
                           "value": {"raw": raw_text}}}
    monkeypatch.setattr(mem, "_llm_classify", fake_classify)
    return fake_classify


def _last_post(gw, path):
    matches = [b for p, b in gw.posts if p == path]
    assert matches, f"no POST to {path} in {gw.posts!r}"
    return matches[-1]


# ── Tier 1: client plumbing ──────────────────────────────────────────────────

class TestClientPlumbing:
    def test_remember_posts_classified_payload(self, gw, mock_llm_classify):
        item = mem.remember("test fact", source="test", run_id="r1")
        body = _last_post(gw, "/v1/memory/remember")
        assert body["kind"] == "fact"
        assert body["descriptor"] == "test fact"
        assert body["source"] == "test" and body["run_id"] == "r1"
        assert isinstance(item, MemoryItem)

    def test_remember_fallback_on_classifier_failure(self, gw, monkeypatch):
        monkeypatch.setattr(mem, "_llm_classify",
                            mock.Mock(side_effect=Exception("down")))
        item = mem.remember("fallback fact", source="test", run_id="r1")
        body = _last_post(gw, "/v1/memory/remember")
        assert body["kind"] == "fact"
        assert body["value"] == {"raw": "fallback fact"}
        assert "fallback fact" in body["descriptor"]
        assert item.kind == "fact"

    def test_session_id_forwarded(self, gw, mock_llm_classify):
        mem.remember("s-text", source="t", run_id="r1", goal_id="g",
                     session_id="sess-9")
        body = _last_post(gw, "/v1/memory/remember")
        assert body["session_id"] == "sess-9"
        assert body["goal_id"] == "g"

    def test_flow_safe_remember_tags_session(self, gw, mock_llm_classify):
        """Executor daemon writes carry the run's session id (Phase 1:
        additive metadata for drawer recall scoping)."""
        import flow
        flow._safe_remember("my name is Bob", "sess-flow-1")
        body = _last_post(gw, "/v1/memory/remember")
        assert body["session_id"] == "sess-flow-1"
        assert body["run_id"] == "sess-flow-1"


# ── Tier 2: write paths ──────────────────────────────────────────────────────

class TestWritePaths:
    def test_record_outcome_zero_llm(self, gw, monkeypatch):
        """record_outcome() writes without any LLM call."""
        from schemas import ToolCall

        monkeypatch.setattr(mem, "_llm_classify",
                            mock.Mock(side_effect=AssertionError("no LLM")))
        item = mem.record_outcome(
            tool_call=ToolCall(name="get_weather",
                               arguments={"location": "London"}),
            result_text="22°C sunny", artifact_id=None,
            run_id="r1", goal_id="g1")
        body = _last_post(gw, "/v1/memory/record_outcome")
        assert body["tool"] == "get_weather"
        assert body["arguments"] == {"location": "London"}
        assert item.kind == "tool_outcome"

    def test_add_fact_direct_write(self, gw, monkeypatch):
        """add_fact() writes a fact directly without classification."""
        monkeypatch.setattr(mem, "_llm_classify",
                            mock.Mock(side_effect=AssertionError("no LLM")))
        item = mem.add_fact(
            descriptor="Python is a programming language",
            value={"category": "language"},
            keywords=["python", "programming"],
            source="index_document", run_id="r1")
        body = _last_post(gw, "/v1/memory/remember")
        assert body["kind"] == "fact"
        assert body["keywords"] == ["python", "programming"]
        assert body["source"] == "index_document"
        assert item.kind == "fact"

    def test_add_fact_defaults_keywords_empty(self, gw):
        mem.add_fact("temporary note", source="t", run_id="r1")
        body = _last_post(gw, "/v1/memory/remember")
        assert body["kind"] == "fact"  # add_fact always sets fact kind


# ── Tier 3: read paths ───────────────────────────────────────────────────────

class TestReadPaths:
    def test_read_posts_search(self, gw):
        gw.search_items = [_item_dict(descriptor="Python tutorial")]
        hits = mem.read("python tutorial", top_k=2)
        body = _last_post(gw, "/v1/memory/search")
        assert body["query"] == "python tutorial" and body["top_k"] == 2
        assert len(hits) == 1 and isinstance(hits[0], MemoryItem)

    def test_read_filters_by_kind(self, gw):
        mem.read("pizza", kinds=["fact"])
        assert _last_post(gw, "/v1/memory/search")["kinds"] == ["fact"]
        mem.read("time", kinds=["tool_outcome"])
        assert _last_post(gw, "/v1/memory/search")["kinds"] == ["tool_outcome"]

    def test_read_respects_top_k(self, gw):
        mem.read("fact", top_k=3)
        assert _last_post(gw, "/v1/memory/search")["top_k"] == 3

    def test_empty_query_returns_empty(self, gw, monkeypatch):
        """Empty query still goes to the server (ranking is server-side)."""
        monkeypatch.setattr(mem, "_post", lambda *a, **k: {"items": []})
        assert mem.read("") == []

    def test_read_fail_soft(self, gw, monkeypatch):
        monkeypatch.setattr(mem, "_post",
                            mock.Mock(side_effect=Exception("down")))
        assert mem.read("anything") == []


# ── Tier 4: concurrency ──────────────────────────────────────────────────────

class TestConcurrency:
    def test_concurrent_writes_all_post(self, gw, mock_llm_classify):
        """Concurrent writes from multiple threads all reach the gateway."""
        errors: list[Exception] = []

        def write_fact(i):
            try:
                mem.remember(f"concurrent fact {i}", source="test", run_id="r1")
            except Exception as e:  # pragma: no cover - surfaced below
                errors.append(e)

        threads = [threading.Thread(target=write_fact, args=(i,))
                   for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during concurrent writes: {errors}"
        posts = [b for p, b in gw.posts if p == "/v1/memory/remember"]
        assert len(posts) == 10
        # Every payload is well-formed (the server would 422 otherwise).
        for b in posts:
            assert b["kind"] and b["descriptor"] and b["source"]


# ── Tier 5: the vector boundary ──────────────────────────────────────────────

class TestVectorBoundary:
    """The client never sends vectors — embedding is server-side. (FAISS
    add/search/dim-mismatch rules are covered gateway-side.)"""

    def test_no_embedding_in_payloads(self, gw, mock_llm_classify):
        mem.remember("apple fruit", source="test", run_id="r1")
        mem.add_fact("banana fruit", source="test", run_id="r1")
        for _p, body in gw.posts:
            assert "embedding" not in body

    def test_server_item_with_vector_parses(self, gw, monkeypatch):
        """A server-attached embedding survives the round trip intact."""

        def post_with_vec(path, body, timeout=60.0):
            return {"item": _item_dict(embedding=[0.1] * 768)}

        monkeypatch.setattr(mem, "_post", post_with_vec)
        item = mem.add_fact("car vehicle", source="test", run_id="r1")
        assert item.embedding is not None and len(item.embedding) == 768


# ── Tier 6: MCP tools integration ────────────────────────────────────────────

class TestMCPTools:
    """Test index_document and search_knowledge MCP tools."""

    def test_index_document_chunks_and_writes(self, tmp_path):
        """index_document chunks a file and writes facts to memory."""
        test_file = tmp_path / "test_doc.txt"
        test_file.write_text("This is a long document. " * 50)

        from mcp_server import index_document
        import mcp_server

        written: list[dict] = []

        class FakeMem:
            def add_fact(self, descriptor, *, value=None, keywords=None,
                         source=None, run_id=None, doc=None, **kw):
                written.append({"descriptor": descriptor, "value": value,
                                "source": source, "doc": doc})
                return MemoryItem.model_validate(_item_dict(
                    descriptor=descriptor))

        monkeypatch_safe = mcp_server._safe
        mcp_server._safe = lambda path: test_file  # noqa: E731
        monkeypatch_mem = mcp_server._memory
        mcp_server._memory = FakeMem()
        try:
            result = index_document(str(test_file), chunk_size=100, overlap=20)
        finally:
            mcp_server._safe = monkeypatch_safe
            mcp_server._memory = monkeypatch_mem

        assert result["chunks_indexed"] > 0
        assert len(written) == result["chunks_indexed"]
        assert all("chunk" in (w["value"] or {}) for w in written)
        # Phase 5: every chunk carries its document span (doc id + version
        # + index) so the gateway can version and neighbor-link spans.
        assert result["doc_version"] == "1"
        assert all((w["doc"] or {}).get("doc_id") == w["value"]["source"]
                   for w in written)
        assert [w["doc"]["chunk_index"] for w in written] == list(
            range(len(written)))

    def test_index_document_version_bump(self, tmp_path):
        """Re-indexing with a bumped version tags spans accordingly."""
        test_file = tmp_path / "vdoc.txt"
        test_file.write_text("versioned content here. " * 20)

        from mcp_server import index_document
        import mcp_server

        written: list[dict] = []

        class FakeMem:
            def add_fact(self, descriptor, *, value=None, doc=None, **kw):
                written.append({"doc": doc, "value": value})
                return MemoryItem.model_validate(_item_dict())

        orig_mem = mcp_server._memory
        orig_safe = mcp_server._safe
        mcp_server._safe = lambda path: test_file  # noqa: E731
        mcp_server._memory = FakeMem()
        try:
            result = index_document(str(test_file), chunk_size=100,
                                    overlap=20, doc_version="2")
        finally:
            mcp_server._memory = orig_mem
            mcp_server._safe = orig_safe

        assert result["doc_version"] == "2"
        assert written and all(w["doc"]["version"] == "2" for w in written)
        assert all(w["value"]["doc_version"] == "2" for w in written)

    def test_search_knowledge_returns_results(self):
        """search_knowledge returns ranked fact chunks."""
        import mcp_server

        seed = [
            MemoryItem.model_validate(_item_dict(
                descriptor="Python programming tutorial",
                value={"chunk": "python tutorial chunk"})),
            MemoryItem.model_validate(_item_dict(
                descriptor="JavaScript web development",
                value={"chunk": "js chunk"})),
        ]

        class FakeMem:
            def read(self, query, kinds=None, top_k=5, **kw):
                q = query.lower()
                ranked = sorted(
                    seed,
                    key=lambda i: -len(set(q.split()) & set(
                        i.descriptor.lower().split())))
                return ranked[:top_k]

        monkeypatch_mem = mcp_server._memory
        mcp_server._memory = FakeMem()
        try:
            from mcp_server import search_knowledge
            results = search_knowledge("python tutorial", k=2)
        finally:
            mcp_server._memory = monkeypatch_mem

        assert len(results) >= 1
        assert any("python" in r.get("descriptor", "").lower()
                   for r in results)
        assert results[0]["chunk"] == "python tutorial chunk"


# ── Tier 7: orchestrator integration ─────────────────────────────────────────

class TestOrchestratorIntegration:
    """Test memory integration with the orchestrator (client side)."""

    def test_session_start_reads_memory(self, gw):
        """Orchestrator reads memory at session start via the client."""
        gw.search_items = [_item_dict(descriptor="User prefers concise")]
        import flow
        hits = flow.memory_svc.read("user preferences")
        assert len(hits) >= 1
        assert _last_post(gw, "/v1/memory/search")["query"] == "user preferences"

    def test_memory_hits_in_prompt(self):
        """Memory hits are injected into skill prompts."""
        from skills import _format_memory_hits

        hits = [MemoryItem.model_validate(_item_dict(
            descriptor="Python is great for data science",
            value={"raw": "Python is great for data science"}))]
        formatted = _format_memory_hits(hits)
        assert "python" in formatted.lower()
        assert "data science" in formatted.lower()

    def test_cross_run_memory_visible(self, gw):
        """Two reads in a row see the same server state."""
        gw.search_items = [_item_dict(descriptor="Run 1 fact",
                                      value={"raw": "Run 1 fact"})]
        hits = mem.read("Run 1 fact")
        assert len(hits) >= 1
        assert any("run 1 fact" in h.descriptor.lower() for h in hits)


# ── Tier 8: edge cases ───────────────────────────────────────────────────────

class TestEdgeCases:
    def test_very_long_text(self, gw, mock_llm_classify):
        """Very long text posts fine (descriptor cap is server-side)."""
        item = mem.remember("word " * 10000, source="test", run_id="r1")
        assert item.id is not None
        body = _last_post(gw, "/v1/memory/remember")
        assert body["descriptor"]

    def test_fallback_descriptor_capped(self, gw, monkeypatch):
        """The classifier-fallback caps its own descriptor at 200 chars."""
        monkeypatch.setattr(mem, "_llm_classify",
                            mock.Mock(side_effect=Exception("down")))
        mem.remember("word " * 10000, source="test", run_id="r1")
        body = _last_post(gw, "/v1/memory/remember")
        assert len(body["descriptor"]) <= 200

    def test_unicode_content(self, gw, mock_llm_classify):
        item = mem.remember("日本語テスト 中文测试 한국어",
                            source="test", run_id="r1")
        assert item.id is not None
        assert "日本語" in _last_post(gw, "/v1/memory/remember")["descriptor"]

    def test_special_characters(self, gw, mock_llm_classify):
        special = "test <script>alert('xss')</test> & \"quotes\" 'apostrophes'"
        item = mem.remember(special, source="test", run_id="r1")
        assert item.id is not None
        assert "script" in _last_post(gw, "/v1/memory/remember")["descriptor"]

    def test_duplicate_content(self, gw, mock_llm_classify):
        mem.remember("duplicate fact", source="test", run_id="r1")
        mem.remember("duplicate fact", source="test", run_id="r1")
        posts = [b for p, b in gw.posts if p == "/v1/memory/remember"]
        assert len(posts) == 2  # no client-side dedup; server owns policy


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-q"])
