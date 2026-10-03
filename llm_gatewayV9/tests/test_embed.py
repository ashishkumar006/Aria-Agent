"""V9 embed-endpoint tests. Run from llm_gatewayV9/:  uv run pytest -v tests/test_embed.py

Markers:
  - local:   requires `ollama` running locally with `nomic-embed-text` pulled

The tests start an in-process httpx ASGI client against the V9 FastAPI app —
they do NOT require V9 to be running on port 8109. This keeps the test suite
fast and self-contained.

Note: V9 has no Gemini embedding fallback (Ollama-only by design), so the
unknown-provider and embedder-down paths below assert the fail-closed
contract (400/503 with attempts), not a fallback.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from dotenv import load_dotenv

# Add parent dir to path so `import main` finds V7's modules.
HERE = Path(__file__).parent.parent
sys.path.insert(0, str(HERE))
load_dotenv(HERE.parent / ".env")  # same .env as V3

EXPECTED_OLLAMA_DIM = 768  # nomic-embed-text
EXPECTED_FALLBACK_DIM = 768  # gemini-embedding-001 with outputDimensionality=768


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def client():
    """In-process ASGI client. Manually drives FastAPI's lifespan so
    app.state.embedders is wired before any test sends a request."""
    import main as M
    transport = httpx.ASGITransport(app=M.app)
    async with M.app.router.lifespan_context(M.app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            timeout=60,
        ) as c:
            yield c


@pytest.mark.local
@pytest.mark.asyncio
async def test_ollama_embed(client):
    """Hits the live Ollama endpoint; asserts shape and dim = 768."""
    r = await client.post("/v1/embed", json={
        "text": "the quick brown fox",
        "task_type": "retrieval_document",
        "provider": "ollama",
    })
    assert r.status_code == 200, r.text
    d = r.json()
    print("ollama:", {k: v for k, v in d.items() if k != "embedding"}, "vec[0:3]:", d["embedding"][:3])
    assert d["provider"] == "ollama"
    assert d["model"]
    assert d["dim"] == EXPECTED_OLLAMA_DIM
    assert isinstance(d["embedding"], list) and len(d["embedding"]) == EXPECTED_OLLAMA_DIM
    assert all(isinstance(x, (int, float)) for x in d["embedding"][:5])


@pytest.mark.asyncio
async def test_fallback_embed(client):
    """Unknown embedder name fails closed with 400 (no silent fallback)."""
    r = await client.post("/v1/embed", json={
        "text": "the quick brown fox",
        "task_type": "retrieval_document",
        "provider": "gemini",
    })
    assert r.status_code == 400, r.text
    assert "unknown embedder" in r.text


@pytest.mark.asyncio
async def test_failover(client, monkeypatch):
    """Point Ollama at an unused port → single-member ring fails closed
    with 503 and a non-empty attempts trail (no fallback exists in V9)."""
    # Rebuild embedders with a broken Ollama URL, install onto app state.
    import main as M
    import embedders as E
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:1")  # unbound
    broken_embedders, order = E.build_embedders()
    monkeypatch.setattr(M.app.state, "embedders", broken_embedders)
    monkeypatch.setattr(M.app.state, "embed_order", order)

    r = await client.post("/v1/embed", json={
        "text": "fall over please",
        "task_type": "retrieval_query",
    })
    assert r.status_code == 503, r.text
    d = r.json()
    print("failover:", str(d)[:200])
    assert "ollama" in str(d)


@pytest.mark.local
@pytest.mark.asyncio
async def test_provider_explicit(client):
    """Pinned provider should appear in the response provider field."""
    r = await client.post("/v1/embed", json={
        "text": "hello world",
        "provider": "ollama",
    })
    assert r.status_code == 200, r.text
    d = r.json()
    print("explicit:", {k: v for k, v in d.items() if k != "embedding"})
    assert d["provider"] == "ollama"
    assert d["dim"] == EXPECTED_OLLAMA_DIM
