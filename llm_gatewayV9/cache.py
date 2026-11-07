"""Gemini prompt cache: SHA-256-keyed reuse of cached system content.

Implicit prefix caching on OpenAI-compat providers does not need a module —
the gateway just keeps system byte-stable across calls and the upstream takes
care of the rest. So this module is Gemini-only.
"""
from __future__ import annotations
import time, hashlib, asyncio, httpx
from typing import Optional

# Pooled upstream client: building an httpx.AsyncClient costs 230-590 ms on this
# host, and it used to happen on every cache mint. `providers` imports nothing
# from here, so this is not a cycle.
#
# `upstream_session`, NOT `upstream_client`: exiting `async with` on an
# httpx.AsyncClient calls `aclose()`, and this client is shared — wrapping the
# pooled object would close everybody else's connection mid-flight and undo the
# pooling it is supposed to benefit from.
from providers import upstream_session


_MINT_FAILURES: dict[tuple[str, str], int] = {}
_MINT_SAMPLE: dict[tuple[str, str], str] = {}
_LAST_REPORT: dict[tuple[str, str], float] = {}
_REPORT_EVERY_S = 300.0

# Some answers are not transient, and asking again costs a whole extra HTTPS
# round trip on the request that is already waiting on us. The live one here is
# a plan limit, not a rate limit: "TotalCachedContentStorageTokensPerModelFreeTier
# limit exceeded for model gemini-3.5-flash-lite: limit=0" — explicit caching is
# simply not sold on this credential's tier, so every mint for that model was
# destined to fail until the plan changes.
_REFUSED_UNTIL: dict[str, float] = {}
_REFUSAL_COOLDOWN_S = 900.0
_PERMANENT_REFUSALS = (401, 403, 429)


def _sanitize(body: str) -> str:
    detail = " ".join((body or "").split())[:240]
    if "key=" in detail:  # upstream echoed the request; drop the query string
        detail = detail.split("key=")[0]
    return detail


def _report_failure(model: str, tag, body: str) -> None:
    """Count and (sparingly) log why a cachedContent could not be minted.

    Only the model, a coarse tag (HTTP status or exception class) and a
    truncated message reach the log. The request URL is never included: it
    carries the API key as a query parameter.
    """
    key = (str(model), str(tag))
    _MINT_FAILURES[key] = _MINT_FAILURES.get(key, 0) + 1
    detail = _sanitize(body)
    if detail and key not in _MINT_SAMPLE:
        _MINT_SAMPLE[key] = detail
    now = time.time()
    if now - _LAST_REPORT.get(key, 0.0) < _REPORT_EVERY_S:
        return
    _LAST_REPORT[key] = now
    print(f"[gemini-cache] mint failed model={model} status={tag} "
          f"count={_MINT_FAILURES[key]} {detail}")


def failure_report() -> list[dict]:
    """Why explicit caching is not happening, for /v1/cache and tests."""
    return [{"model": m, "reason": r, "count": n, "detail": _MINT_SAMPLE.get((m, r), "")}
            for (m, r), n in sorted(_MINT_FAILURES.items())]


def refused_until(model: str) -> float:
    """Epoch when this model may be asked again, 0.0 if it is not refused."""
    return _REFUSED_UNTIL.get(str(model), 0.0)


def reset_failure_report() -> None:
    _MINT_FAILURES.clear()
    _MINT_SAMPLE.clear()
    _LAST_REPORT.clear()
    _REFUSED_UNTIL.clear()


class GeminiCache:
    """Maps SHA-256(system_text) -> (cache_resource_name, expires_at)."""

    def __init__(self, ttl_seconds: int = 300):
        self.ttl = ttl_seconds
        self._store: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(api_key: str, model: str, text: str) -> str:
        """Include the KEY in the cache key.

        A cachedContent resource is created under one key and cannot be used
        by another, so sharing this map across a multi-key or multi-user pool
        either 403s or reuses content minted on somebody else's credential.
        Hash the key; never store it.
        """
        h = hashlib.sha256()
        h.update(hashlib.sha256(str(api_key).encode()).digest())
        h.update(b"\x00")
        h.update(model.encode())
        h.update(b"\x00")
        h.update(text.encode())
        return h.hexdigest()

    async def get_or_create(self, api_key: str, model: str, text: str, base_url: str) -> tuple[Optional[str], int]:
        """Returns (cache_resource_name|None, cache_creation_input_tokens).
        cache_creation_input_tokens is non-zero only when we mint a fresh entry.
        """
        key = self._key(api_key, model, text)
        now = time.time()
        # A model upstream has refused is not retried on every chat call: the
        # ask is a whole extra HTTPS round trip in front of a request that is
        # already waiting, and the answer is already known.
        if _REFUSED_UNTIL.get(str(model), 0.0) > now:
            _report_failure(model, "skipped-refusal", "")
            return None, 0
        async with self._lock:
            if key in self._store:
                name, exp = self._store[key]
                if exp > now + 5:
                    return name, 0
                self._store.pop(key, None)

        # Mint a new cached content. Gemini requires the content list.
        # The key goes in the request HEADER, not the query string: an error
        # body from this endpoint quotes the request, and a `?key=` in the URL
        # therefore lands in whatever log records the failure (see
        # tests/test_cache_mint_refusal.py).
        headers = {"x-goog-api-key": str(api_key)}
        url = f"{base_url}/cachedContents"
        body = {
            "model": f"models/{model}",
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "ttl": f"{self.ttl}s",
        }
        try:
            async with upstream_session(60.0) as c:
                r = await c.post(url, json=body, headers=headers)
                if r.status_code != 200:
                    _report_failure(model, r.status_code, r.text)
                    if r.status_code in _PERMANENT_REFUSALS:
                        _REFUSED_UNTIL[str(model)] = now + _REFUSAL_COOLDOWN_S
                    return None, 0
                d = r.json()
                name = d.get("name")  # "cachedContents/<id>"
                usage = d.get("usageMetadata") or {}
                tokens = usage.get("totalTokenCount", 0) or len(text) // 4
                if not name:
                    _report_failure(model, "no-name", "")
                    return None, 0
                async with self._lock:
                    self._store[key] = (name, now + self.ttl)
                return name, tokens
        except Exception as e:
            # Swallowing this used to be silent, and "caching is on" then meant
            # nothing: a 403, a bad model id and a DNS failure all looked
            # identical from the ledger (cache_read_tokens stayed 0, the call
            # still succeeded uncached). The reason is logged so the feature can
            # be confirmed broken or fixed; the request URL is NOT, because it
            # carries ?key=<api key>.
            _report_failure(model, type(e).__name__, "")
            return None, 0
