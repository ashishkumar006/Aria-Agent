"""Live model discovery for the Kilo Code gateway.

Kilo serves several hundred models behind one OpenAI-compatible endpoint, but
`GET {base}/models` needs no auth at all -- so the roster can be read even
before a key is saved. That is what makes this useful as a picker source: the
console can show what a key *would* buy before the user has one.

Why this exists: `/v1/providers` reports one static `provider.model` per
provider (the `KILO_MODEL` default). For Kilo that is a single hardcoded
string, which hid 394 models behind `tencent/hy3:free` - an id that no longer
exists upstream.

Design notes:
- Discovery never blocks startup. A failure leaves the roster empty and
  `stale=True`; the gateway keeps serving whatever it was already wired for.
- The cache is TTL'd and stores the last good roster, so a transient outage
  degrades to "slightly old list", never to "no models".
- Free models are flagged from `isFree` when present and from pricing
  otherwise, because upstream uses both conventions.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.kilo.ai/api/gateway"
DEFAULT_TTL_SECONDS = 300.0
_FETCH_TIMEOUT = httpx.Timeout(20.0, connect=8.0)

# The fallback used only when the roster cannot be read AND KILO_MODEL is
# unset. It must be an id that exists upstream: the previously hardcoded
# `tencent/hy3:free` no longer does (upstream has `tencent/hy3` and
# `tencent/hy3-preview`, both paid), so that default 404'd on every call.
# `kilo-auto/free` rotates through whatever is currently free, which is the
# right behaviour to fall back to when we cannot see the catalogue.
SAFE_DEFAULT_MODEL = "kilo-auto/free"

# Free-tier ids we must never lose, even if a fetch fails: they are the
# models that work with no key at all, which is the whole point of the picker.
_LAST_GOOD: dict[str, Any] = {"models": [], "fetched_at": 0.0, "error": None}


def _num(value: Any) -> float:
    """Kilo prices in USD-per-token strings, sometimes "-1" (unmetered)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return -1.0


def _normalize(raw: dict[str, Any]) -> dict[str, Any] | None:
    mid = str(raw.get("id") or "").strip()
    if not mid:
        return None
    pricing = raw.get("pricing") or {}
    pin, pout = _num(pricing.get("prompt")), _num(pricing.get("completion"))
    is_free = raw.get("isFree")
    if not isinstance(is_free, bool):
        # `isFree` is absent on some entries; pricing is the fallback signal.
        is_free = (pin == 0.0 and pout == 0.0)
    params = raw.get("supported_parameters") or []
    arch = raw.get("architecture") or {}
    top = raw.get("top_provider") or {}
    # Ids are "vendor/model"; the vendor prefix is the closest thing to an
    # `owned_by`, which this endpoint omits for most entries.
    vendor = mid.split("/", 1)[0] if "/" in mid else ""
    desc = str(raw.get("description") or "").strip().split("\n", 1)[0]
    return {
        "id": mid,
        "name": str(raw.get("name") or mid),
        "vendor": vendor,
        "owned_by": str(raw.get("owned_by") or vendor),
        "context_length": int(raw.get("context_length")
                              or top.get("context_length") or 0),
        "max_completion_tokens": int(top.get("max_completion_tokens") or 0),
        "price_in": pin,
        "price_out": pout,
        "is_free": bool(is_free),
        "supports_tools": "tools" in params,
        "supports_vision": "image" in (arch.get("input_modalities") or []),
        "supports_reasoning": "reasoning" in params
        or "reasoning_effort" in params,
        "description": desc[:200],
    }


# Free models verified to answer a coding prompt quickly and cleanly.
#
# This is a RANKING HINT, not the only source of truth, and it exists because
# neither context length nor "is free" predicts whether a model actually
# replies: measured against the live catalogue, the largest-context free model
# (inkling-small) 429s, a 1M-context one returns empty, and two "free" Google
# entries 402. `tests/check_free_models.py` re-probes the whole free tier, so
# this list can be re-verified instead of trusted forever.
#
# Ordered by measured latency on "What is 2+2? Answer with just the number":
#   cohere/north-mini-code:free  1.2s / 27 tok  (code-specific)
#   openrouter/free              1.9s / 61 tok
#   poolside/laguna-s-2.1:free   2.4s /  2 tok  (code models)
#   nvidia/nemotron-3-super-120b-a12b:free  2.5s / 37 tok
_CODE_MODEL_PREFERENCE = (
    "cohere/north-mini-code:free",
    "openrouter/free",
    "poolside/laguna-s-2.1:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
)


def pick_default_model(roster: list[dict[str, Any]] | None) -> str | None:
    """Best default for the coding agent, chosen from the live roster.

    Replaces a hardcoded model name. Upstream renames, retires and reprices
    models constantly - `tencent/hy3:free` and `stepfun/step-3.7-flash:free`
    both vanished - so a name baked into the source is guaranteed to rot.

    Preference order:
      1. A verified code model from _CODE_MODEL_PREFERENCE, if it is still in
         the catalogue, still free, and still supports tools.
      2. The largest-context free model that supports tool calls.
      3. `kilo-auto/free`, the free router.
      4. Any free model.

    The router is deliberately not first. `kilo-auto/free` spreads each request
    across whatever free backend is up, which sounds ideal and is not: measured
    against this gateway it returned a 0-token empty body on a 2000-token
    request while a fixed model answered the same prompt every time. A harness
    needs a model that answers the same way twice. It stays in the picker.
    """
    if not roster:
        return None
    free = {m["id"]: m for m in roster if m.get("is_free")}
    if not free:
        return None
    for mid in _CODE_MODEL_PREFERENCE:
        m = free.get(mid)
        if m is not None and m.get("supports_tools"):
            return mid
    tool_models = [m for m in free.values() if m.get("supports_tools")]
    if tool_models:
        return max(tool_models,
                   key=lambda m: (m.get("context_length", 0), m["id"]))["id"]
    if "kilo-auto/free" in free:
        return "kilo-auto/free"
    return next(iter(free))


async def fetch_models(base_url: str | None = None,
                       timeout: httpx.Timeout | None = None) -> dict[str, Any]:
    """Read the roster from Kilo. Never raises -- returns an error dict."""
    base = (base_url or os.getenv("KILO_BASE_URL")
            or DEFAULT_BASE_URL).rstrip("/")
    global _LAST_GOOD
    try:
        async with httpx.AsyncClient(timeout=timeout or _FETCH_TIMEOUT) as c:
            r = await c.get(f"{base}/models")
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            data = r.json()
    except Exception as e:  # noqa: BLE001 - surfaced as `error`, never fatal
        msg = str(e)[:200]
        _LAST_GOOD["error"] = msg
        return {
            "base_url": base,
            "models": list(_LAST_GOOD["models"]),
            "count": len(_LAST_GOOD["models"]),
            "free_count": sum(1 for m in _LAST_GOOD["models"] if m["is_free"]),
            "fetched_at": _LAST_GOOD["fetched_at"],
            "stale": bool(_LAST_GOOD["models"]),
            "error": msg,
        }

    out = [n for n in (_normalize(m) for m in (data.get("data") or [])) if n]
    # Free first, then best context: the order a person actually wants when
    # they are choosing a model for a coding agent on a free tier.
    out.sort(key=lambda m: (not m["is_free"], -m["context_length"], m["id"]))
    _LAST_GOOD = {"models": out, "fetched_at": time.time(), "error": None}
    return {
        "base_url": base,
        "models": out,
        "count": len(out),
        "free_count": sum(1 for m in out if m["is_free"]),
        "fetched_at": _LAST_GOOD["fetched_at"],
        "stale": False,
        "error": None,
    }


async def get_models(base_url: str | None = None,
                     ttl: float | None = None,
                     force: bool = False) -> dict[str, Any]:
    """TTL-cached roster, stored on app.state when available."""
    ttl = DEFAULT_TTL_SECONDS if ttl is None else ttl
    try:
        from main import app  # late import: avoids a circular import at load
        store = app.state
    except Exception:
        store = None
    if not force and store is not None:
        cached = getattr(store, "kilo_models", None)
        if cached and (time.time() - cached.get("_mono", 0.0)) < ttl:
            return {k: v for k, v in cached.items() if k != "_mono"}
    result = await fetch_models(base_url)
    if store is not None:
        store.kilo_models = {**result, "_mono": time.time()}
    return result


def reset_cache() -> None:
    """For tests and for an explicit refresh after the base URL changes."""
    global _LAST_GOOD
    _LAST_GOOD = {"models": [], "fetched_at": 0.0, "error": None}
    try:
        from main import app
        app.state.kilo_models = None
    except Exception:
        pass


async def prime_routes(providers: dict) -> int:
    """Register every discovered Kilo model id so `model=` alone routes to it.

    Without this, callers who know the id they want still have to also know
    which provider serves it. Returns the number of ids registered.

    Also re-points the wired kilo provider's default model at the live roster
    when KILO_MODEL is unset. build_providers() has to pick *something* before
    discovery has run, and a name guessed there is exactly the thing that goes
    stale; this corrects it a moment later. An explicit KILO_MODEL is never
    overridden - the operator asked for that model by name.
    """
    roster = await get_models()
    ids = [m["id"] for m in roster["models"]]
    import providers as P
    n = P.register_kilo_model_routes(ids)

    if roster["models"] and not (os.getenv("KILO_MODEL") or "").strip():
        chosen = pick_default_model(roster["models"])
        if chosen:
            for name, p in providers.items():
                # Siblings (`kilo-2`, …) are separate instances of the same
                # provider and must move together, or a failover would land on
                # a different model than the one the picker is showing.
                if getattr(p, "name", None) == "kilo":
                    p.model = chosen
    return n


__all__ = ["fetch_models", "get_models", "prime_routes", "reset_cache",
           "DEFAULT_BASE_URL", "DEFAULT_TTL_SECONDS"]
