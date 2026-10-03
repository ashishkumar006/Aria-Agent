"""Fetch cache + in-flight dedupe.

The researcher was 36s of a 74s run and is fetch-bound, with no cache: the
same URL was re-fetched every run, and two facets of one plan that cite the
same page paid for it twice because their fetch_url calls were issued in the
same turn. These tests pin correctness properties, not just "it caches".
"""
import asyncio
import json
import time
from pathlib import Path

import pytest

import mcp_server as mcp


@pytest.fixture(autouse=True)
def _tmp_cache(tmp_path, monkeypatch):
    """Point the cache at a temp dir and reset counters per test."""
    monkeypatch.setattr(mcp, "_CACHE_DIR", tmp_path / "fetch")
    monkeypatch.setattr(mcp, "_CACHE_ENABLED", True)
    mcp._cache_hits = 0
    mcp._cache_misses = 0
    mcp._inflight.clear()
    yield
    mcp._inflight.clear()


def _body(text="hello world", status=200):
    return {"status": status, "content_type": "text/markdown",
            "length_bytes": len(text), "truncated": False, "text": text}


# ── basics ──────────────────────────────────────────────────────────────────
def test_second_fetch_is_served_from_cache():
    calls = []

    async def fake(url, timeout_s=20):
        calls.append(url)
        return _body("the real page")

    orig = mcp._extract_page
    mcp._extract_page = fake
    try:
        a = asyncio.run(mcp.fetch_url("https://example.org/a"))
        b = asyncio.run(mcp.fetch_url("https://example.org/a"))
    finally:
        mcp._extract_page = orig
    assert calls == ["https://example.org/a"], "the second fetch hit the network"
    assert b["cached"] is True
    assert b["text"] == a["text"]
    assert mcp.cache_stats()["hits"] == 1


def test_refresh_bypasses_the_cache():
    calls = []

    async def fake(url, timeout_s=20):
        calls.append(url)
        return _body(f"v{len(calls)}")

    orig = mcp._extract_page
    mcp._extract_page = fake
    try:
        asyncio.run(mcp.fetch_url("https://example.org/b"))
        r = asyncio.run(mcp.fetch_url("https://example.org/b", refresh=True))
    finally:
        mcp._extract_page = orig
    assert len(calls) == 2
    assert r.get("cached") is not True, "refresh must not report a cache hit"
    assert r["text"] == "v2"


def test_url_case_and_whitespace_do_not_defeat_the_cache():
    calls = []

    async def fake(url, timeout_s=20):
        calls.append(url)
        return _body()

    orig = mcp._extract_page
    mcp._extract_page = fake
    try:
        asyncio.run(mcp.fetch_url("https://Example.ORG/Path"))
        asyncio.run(mcp.fetch_url("  https://example.org/Path  "))
    finally:
        mcp._extract_page = orig
    assert len(calls) == 1, calls


def test_expired_entries_refetch():
    calls = []

    async def fake(url, timeout_s=20):
        calls.append(url)
        return _body()

    orig = mcp._extract_page
    mcp._extract_page = fake
    try:
        asyncio.run(mcp.fetch_url("https://example.org/c"))
        # Age the entry past the TTL.
        for p in mcp._CACHE_DIR.glob("*.json"):
            blob = json.loads(p.read_text(encoding="utf-8"))
            blob["t"] = time.time() - (mcp._CACHE_TTL_S + 10)
            p.write_text(json.dumps(blob), encoding="utf-8")
        asyncio.run(mcp.fetch_url("https://example.org/c"))
    finally:
        mcp._extract_page = orig
    assert len(calls) == 2, "an expired entry must refetch"


# ── failures are cached briefly, not for a week ─────────────────────────────
def test_timeouts_get_a_short_negative_entry_not_a_stale_success():
    calls = []

    async def fake(url, timeout_s=20):
        calls.append(url)
        return {"status": 504, "content_type": "text/markdown",
                "length_bytes": 0, "truncated": False,
                "text": f"[fetch timed out after {timeout_s + 15}s: {url}]"}

    orig = mcp._extract_page
    mcp._extract_page = fake
    try:
        asyncio.run(mcp.fetch_url("https://slow.example/d"))
        second = asyncio.run(mcp.fetch_url("https://slow.example/d"))
    finally:
        mcp._extract_page = orig
    assert len(calls) == 1, "a repeated dead link must not re-burn the timeout"
    assert second["cache"] == "negative"
    assert "cached failure" in second["text"]


def test_a_negative_entry_expires_far_sooner_than_a_success():
    async def fake(url, timeout_s=20):
        return {"status": 404, "content_type": "text/markdown",
                "length_bytes": 0, "truncated": False, "text": "nothing"}

    orig = mcp._extract_page
    mcp._extract_page = fake
    try:
        asyncio.run(mcp.fetch_url("https://gone.example/e"))
        for p in mcp._CACHE_DIR.glob("*.json"):
            blob = json.loads(p.read_text(encoding="utf-8"))
            # Older than the negative TTL, younger than the success TTL.
            blob["t"] = time.time() - (mcp._NEG_TTL_S + 10)
            p.write_text(json.dumps(blob), encoding="utf-8")
        again = asyncio.run(mcp.fetch_url("https://gone.example/e"))
    finally:
        mcp._extract_page = orig
    assert again.get("cache") != "negative", "a dead link must be retried later"


# ── concurrency ─────────────────────────────────────────────────────────────
def test_concurrent_identical_fetches_hit_the_network_once():
    """Sibling facets of one plan fetch in the same turn. Without in-flight
    dedupe they race and both pay for the same page."""
    calls = []

    async def fake(url, timeout_s=20):
        calls.append(url)
        await asyncio.sleep(0.15)          # make the race real
        return _body("shared")

    orig = mcp._extract_page
    mcp._extract_page = fake

    async def both():
        return await asyncio.gather(
            mcp.fetch_url("https://example.org/f"),
            mcp.fetch_url("https://example.org/f"),
            mcp.fetch_url("https://example.org/f"),
        )

    try:
        out = asyncio.run(both())
    finally:
        mcp._extract_page = orig
    assert len(calls) == 1, f"expected one network fetch, got {len(calls)}"
    assert all(o["text"] == "shared" for o in out)


def test_a_page_level_failure_does_not_raise_and_is_cached_briefly():
    """A DNS/TLS/parse failure used to propagate out of the tool, so the model
    saw an opaque "tool error" and the failure was never cached."""
    calls = []

    async def boom(url, timeout_s=20):
        calls.append(url)
        raise RuntimeError("getaddrinfo failed")

    orig = mcp._extract_page
    mcp._extract_page = boom
    try:
        out = asyncio.run(mcp.fetch_url("https://nowhere.invalid/x"))
        again = asyncio.run(mcp.fetch_url("https://nowhere.invalid/x"))
    finally:
        mcp._extract_page = orig
    assert out["status"] == 502
    assert "getaddrinfo failed" in out["text"]
    assert out["untrusted"] is False
    assert len(calls) == 1, "a repeat must not re-attempt the dead host"
    assert again.get("cache") == "negative"


def test_inflight_map_is_cleaned_up_after_an_error():
    """A page-level failure is now returned as a structured 502 rather than
    raised, but the in-flight slot must still be released either way."""
    async def boom(url, timeout_s=20):
        raise RuntimeError("dns exploded")

    orig = mcp._extract_page
    mcp._extract_page = boom
    try:
        out = asyncio.run(mcp.fetch_url("https://bad.example/g"))
    finally:
        mcp._extract_page = orig
    assert out["status"] == 502
    assert mcp._inflight == {}, "a failed fetch leaked its in-flight slot"


# ── bounding ────────────────────────────────────────────────────────────────
def test_cache_directory_is_bounded():
    orig_max = mcp._CACHE_MAX_ENTRIES
    mcp._CACHE_MAX_ENTRIES = 10
    try:
        for i in range(25):
            mcp._cache_write(f"https://example.org/page{i}", _body(f"p{i}"))
        assert len(list(mcp._CACHE_DIR.glob("*.json"))) <= 10
    finally:
        mcp._CACHE_MAX_ENTRIES = orig_max


def test_cache_writes_never_raise_on_a_readonly_dir(tmp_path, monkeypatch):
    blocked = tmp_path / "nope"
    blocked.mkdir()
    blocked.chmod(0o500)                  # read-only
    monkeypatch.setattr(mcp, "_CACHE_DIR", blocked / "cache")
    mcp._cache_write("https://example.org/h", _body())   # must not raise
    mcp._cache_evict()                                 # must not raise
    blocked.chmod(0o700)


def test_cache_is_not_written_when_disabled(monkeypatch):
    monkeypatch.setattr(mcp, "_CACHE_ENABLED", False)
    mcp._cache_write("https://example.org/i", _body())
    assert not list(mcp._CACHE_DIR.glob("*.json"))
    assert mcp._cache_read("https://example.org/i") is None


def test_cache_survives_an_mcp_restart(monkeypatch, tmp_path):
    """The MCP process is spawned per skill invocation, so an in-process cache
    would never hit. The on-disk store is the whole point."""
    mcp._cache_write("https://example.org/j", _body("persisted"))
    # Simulate a fresh process: drop every in-memory structure.
    mcp._cache_hits = 0
    mcp._cache_misses = 0
    got = mcp._cache_read("https://example.org/j")
    assert got is not None and got["text"] == "persisted"
    assert got["cached"] is True
