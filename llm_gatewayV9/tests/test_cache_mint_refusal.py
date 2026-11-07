"""The explicit Gemini cache was configured, counted as "on", and never worked.

Three facts that the ledger proved and the code hid:

* `cache_create_tokens` is 0 in every one of ~100k rows — not "rarely used",
  never once succeeded.
* The mint gate is on CHARACTER length (`> 1000`), while upstream measures in
  TOKENS: a 4k-character system block came back
  "Cached content is too small. total_token_count=851, min_total_token_count=1024".
* A big enough block came back
  "TotalCachedContentStorageTokensPerModelFreeTier limit exceeded for model
  gemini-3.5-flash-lite: limit=0" — explicit caching is not sold on this
  credential's tier, so every ask was doomed.

`except Exception: return None, 0` made all three invisible: the call succeeded
anyway (uncached) and the only trace was a zero that looks identical to "this
prompt was too small to bother".
"""
from __future__ import annotations

import asyncio
import json

import pytest

import cache
import providers


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


class _Client:
    """Stands in for the pooled upstream client and records the requests."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "body": json, "headers": headers or {}})
        return self.replies.pop(0) if self.replies else _Response(500, text="boom")


@pytest.fixture
def minted(monkeypatch):
    """Route cache.get_or_create through a stub client; hand back the stub."""
    box = {}

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session(timeout=60.0):
        yield box["client"]

    monkeypatch.setattr(cache, "upstream_session", _session)
    cache.reset_failure_report()

    def install(*replies):
        box["client"] = _Client(replies)
        return box["client"]
    yield install
    cache.reset_failure_report()


def test_a_refused_model_is_not_asked_again(minted):
    """The 429 is a plan limit, so retrying it per chat call is pure latency.

    Before: every call with a >1000-character system block paid an extra HTTPS
    round trip in front of the request the user was already waiting on, to be
    told the same thing.
    """
    refused = _Response(429, text=json.dumps({
        "error": {"code": 429, "message": "TotalCachedContentStorageTokensPerModel"
                                          "FreeTier limit exceeded: limit=0",
                  "status": "RESOURCE_EXHAUSTED"}}))
    client = minted(refused, refused, refused)

    async def main():
        store = cache.GeminiCache(ttl_seconds=300)
        for _ in range(4):
            name, created = await store.get_or_create(
                "k", "gemini-3.5-flash-lite", "y" * 5000, "https://example.test")
            assert name is None and created == 0
        return client.calls

    calls = asyncio.run(main())
    assert len(calls) == 1, f"{len(calls)} mints attempted after a refusal"
    report = cache.failure_report()
    assert any(r["reason"] == "429" for r in report), report
    assert any(r["reason"] == "skipped-refusal" for r in report), (
        "the skipped attempts must still be visible, or 'caching is off' looks "
        "identical to 'nothing asked for it'")


def test_the_mint_never_puts_the_key_in_the_url(minted):
    """Upstream error bodies quote the request they rejected.

    The mint used to be `POST .../cachedContents?key=<api key>`; the 400 body
    then reproduced that URL, and the body is exactly what gets logged.
    """
    echoed = _Response(400, text=json.dumps({
        "error": {"code": 400,
                  "message": "Cached content is too small. Request "
                             "https://generativelanguage.googleapis.com/v1beta/"
                             "cachedContents?key=SECRET123 min_total_token_count=1024"}}))
    client = minted(echoed)

    async def main():
        return await cache.GeminiCache().get_or_create(
            "SECRET123", "gemini-3.5-flash-lite", "y" * 5000,
            "https://example.test")

    asyncio.run(main())
    assert "key=" not in client.calls[0]["url"], client.calls[0]["url"]
    assert client.calls[0]["headers"].get("x-goog-api-key") == "SECRET123"
    detail = cache.failure_report()[0]["detail"]
    assert "SECRET123" not in detail, f"the credential reached the report: {detail}"


def test_a_system_block_below_the_token_floor_is_not_offered():
    """The gate must measure tokens, because that is what upstream measures.

    1000 characters is ~250 tokens; the floor is 1024 tokens. A paragraph
    therefore asked for a cache it could never be granted.
    """
    assert providers._EXPLICIT_CACHE_MIN_CHARS // 4 >= 1024, (
        "the character gate must imply at least upstream's 1024-token minimum")


def test_the_provider_asks_only_when_the_block_can_be_cached(monkeypatch):
    """Small system block → no mint; big one → exactly one mint."""
    asked = []

    class _Store:
        ttl = 300

        async def get_or_create(self, api_key, model, text, base_url):
            asked.append(len(text))
            return "cachedContents/fresh", 4200

    reply = _Response(200, payload={
        "candidates": [{"content": {"parts": [{"text": "done"}]},
                        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2}})
    client = _Client([reply, reply])

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _chat_session(timeout=None):
        yield client

    monkeypatch.setattr(providers, "upstream_session", _chat_session)
    provider = providers.GeminiProvider("k", "gemini-3.5-flash-lite", _Store())

    async def main():
        await provider.chat([{"role": "user", "content": "hi"}],
                            system_blocks="x" * 1500, cache_system=True,
                            max_tokens=8)
        await provider.chat([{"role": "user", "content": "hi"}],
                            system_blocks="x" * 9000, cache_system=True,
                            max_tokens=8)

    asyncio.run(main())
    assert asked == [9000], (
        f"cache asked for sizes {asked} — a 1500-character block is ~375 tokens "
        "and cannot be cached")
