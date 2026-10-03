"""Tests for POST /v1/embed/batch.

The batch endpoint exists because one call per chunk made document indexing
~7x slower than Ollama's native array path. These tests cover the contract
that makes it safe to use: provenance on every vector, one bad chunk not
losing the rest of its window, and the size clamp.
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import embedders as E  # noqa: E402
import schemas  # noqa: E402


class _FakeProvider:
    """Stands in for the Ollama embedder: no network, deterministic vectors."""

    name = "fake"
    model = "fake-embed-v1"

    class _State:
        def can_use(self):
            return True, ""

        def record(self):
            pass

        def mark_failure(self, reason):
            pass

    state = _State()

    def _vec(self, text: str) -> list[float]:
        # Deterministic, and different per input, so mix-ups are visible.
        seed = sum(ord(c) for c in text[:40])
        return [float((seed * (i + 1)) % 97) / 97.0 for i in range(8)]

    async def embed(self, text, task_type):
        if len(text) > 40:
            raise E.EmbedderError("input too long", status=400)
        return {"embedding": self._vec(text), "model": self.model,
                "dim": 8}

    async def embed_batch(self, texts, task_type):
        if any(len(t) > 40 for t in texts):
            raise E.EmbedderError("input too long", status=400)
        return {"embeddings": [self._vec(t) for t in texts],
                "model": self.model, "dim": 8}


class _NoBatchProvider(_FakeProvider):
    name = "scalar-only"

    embed_batch = None       # attribute exists but is not callable


@contextmanager
def _client(provider=None):
    """Point the gateway's own app at a fake embedder.

    The handler reads `app.state.embedders` from main's MODULE-level app, so
    mounting the route on a separate FastAPI() would leave it looking at the
    wrong object. TestClient without a context manager does not run the
    lifespan, so the state we set here is exactly what the handler sees.
    """
    import main as M
    prev = getattr(M.app.state, "embedders", "__unset__")
    M.app.state.embedders = [provider or _FakeProvider()]
    try:
        yield TestClient(M.app)
    finally:
        if prev == "__unset__":
            try:
                del M.app.state.embedders
            except Exception:
                pass
        else:
            M.app.state.embedders = prev


# ── provenance ──────────────────────────────────────────────────────────────

def test_batch_returns_model_and_dim_for_every_vector():
    with _client() as c:
        r = c.post("/v1/embed/batch", json={"texts": ["a", "b", "c"]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["count"] == 3
    # Provenance is the point: a caller must be able to record which model
    # produced each vector, or mixed-model vectors compare silently later.
    assert d["embed_model"] == "fake-embed-v1"
    assert d["embed_dim"] == 8
    assert d["model"] == d["embed_model"]
    assert len(d["embeddings"]) == 3
    assert all(len(v) == 8 for v in d["embeddings"])


def test_vectors_differ_per_input():
    with _client() as c:
        d = c.post("/v1/embed/batch",
                   json={"texts": ["alpha", "beta"]}).json()
    assert d["embeddings"][0] != d["embeddings"][1]


# ── windowing ────────────────────────────────────────────────────────────────

def test_default_batch_size_is_sixteen():
    import main as M
    assert M._DOC_BATCH_DEFAULT == 16
    assert M._DOC_BATCH_MAX == 32


@pytest.mark.parametrize("requested, seen", [
    (None, 16), (4, 4), (16, 16), (32, 32),
    (1, 1), (999, 32), (0, 1), (-5, 1),
])
def test_batch_size_is_honoured_and_clamped(requested, seen):
    body = {"texts": ["x"] * 2}
    if requested is not None:
        body["batch_size"] = requested
    with _client() as c:
        d = c.post("/v1/embed/batch", json=body).json()
    assert d["batch_size"] == seen, d


def test_large_request_is_split_into_windows_and_stays_ordered():
    texts = [f"chunk number {i}" for i in range(40)]
    with _client() as c:
        d = c.post("/v1/embed/batch",
                   json={"texts": texts, "batch_size": 16}).json()
        # Order must survive windowing, or a chunk gets someone else's
        # vector. Compare against the same text embedded on its own.
        one = [c.post("/v1/embed/batch",
                      json={"texts": [t]}).json()["embeddings"][0]
               for t in ("chunk number 0", "chunk number 39")]
    assert d["count"] == 40
    assert d["embeddings"][0] == one[0]
    assert d["embeddings"][39] == one[1]


# ── one bad chunk must not lose the batch ────────────────────────────────────

def test_oversized_input_is_rejected_naming_its_position():
    with _client() as c:
        r = c.post("/v1/embed/batch",
                   json={"texts": ["fine", "x" * (E.MAX_INPUT_CHARS + 10)]})
    assert r.status_code == 413
    # The message must name WHICH input, or the caller cannot fix it.
    assert "texts[1]" in r.json()["detail"]


def test_empty_input_is_rejected():
    with _client() as c:
        r = c.post("/v1/embed/batch", json={"texts": ["ok", "   "]})
    assert r.status_code == 400
    assert "texts[1]" in r.json()["detail"]


def test_empty_batch_is_rejected_by_schema():
    with _client() as c:
        assert c.post("/v1/embed/batch", json={"texts": []}).status_code == 422


def test_a_failing_window_falls_back_per_chunk_and_reports_the_bad_one():
    """If the batch call fails, the window must be retried one chunk at a
    time so a single bad input costs one chunk rather than sixteen."""

    class _Flaky(_FakeProvider):
        async def embed_batch(self, texts, task_type):
            if any(len(t) > 20 for t in texts):
                raise E.EmbedderError("boom", status=500)
            return await super().embed_batch(texts, task_type)

    with _client(_Flaky()) as c:
        r = c.post("/v1/embed/batch",
                   json={"texts": ["ok one", "x" * 50, "ok two"]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["count"] == 3
    # The good chunks survived; the bad one is reported, not silently null.
    assert d["embeddings"][0] is not None
    assert d["embeddings"][1] is None
    assert d["embeddings"][2] is not None
    assert d["failed_indices"] == [1], d["failed_indices"]


def test_provider_without_batch_support_degrades_to_single_calls():
    """A provider with no array support must not fail the request - the
    endpoint falls back to one call per chunk and still returns everything."""
    with _client(_NoBatchProvider()) as c:
        r = c.post("/v1/embed/batch", json={"texts": ["one", "two"]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["count"] == 2
    assert d["failed_indices"] == []
    assert all(v is not None for v in d["embeddings"])


# ── provider pinning ────────────────────────────────────────────────────────

def test_unknown_pinned_provider_is_rejected():
    with _client() as c:
        r = c.post("/v1/embed/batch",
                   json={"texts": ["a"], "provider": "nope"})
    assert r.status_code in (400, 502, 503)


def test_task_type_is_honoured():
    with _client() as c:
        for tt in ("retrieval_document", "retrieval_query"):
            r = c.post("/v1/embed/batch",
                       json={"texts": ["a"], "task_type": tt})
            assert r.status_code == 200, (tt, r.text)


def test_bad_task_type_rejected():
    with _client() as c:
        assert c.post("/v1/embed/batch",
                      json={"texts": ["a"], "task_type": "nope"}
                      ).status_code == 422


# ── the real embedder's batch method ────────────────────────────────────────

def test_ollama_embedder_calls_the_array_endpoint(monkeypatch):
    """The whole speedup depends on using /api/embed (input array) rather
    than /api/embeddings (single prompt)."""
    import httpx

    seen = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"embeddings": [[0.1] * 4, [0.2] * 4]}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            seen["url"] = url
            seen["json"] = json
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
    p = E.OllamaEmbedder(model="m", base_url="http://x")
    out = asyncio.new_event_loop().run_until_complete(
        p.embed_batch(["one", "two"], "retrieval_document"))
    assert seen["url"].endswith("/api/embed"), seen["url"]
    assert seen["json"]["input"] == ["search_document: one",
                                      "search_document: two"]
    assert out["dim"] == 4 and len(out["embeddings"]) == 2


def test_ollama_batch_reports_count_mismatch():
    import httpx

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"embeddings": [[0.1] * 4]}      # one for two inputs

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            return _Resp()

    orig = httpx.AsyncClient
    httpx.AsyncClient = lambda **kw: _Client()
    try:
        p = E.OllamaEmbedder(model="m", base_url="http://x")
        with pytest.raises(E.EmbedderError):
            asyncio.new_event_loop().run_until_complete(
                p.embed_batch(["a", "b"], "retrieval_document"))
    finally:
        httpx.AsyncClient = orig


def test_embed_batch_request_schema_defaults():
    m = schemas.EmbedBatchRequest(texts=["a"])
    assert m.task_type == "retrieval_document"
    assert m.batch_size is None
