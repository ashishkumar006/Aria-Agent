"""Web search integration (Tavily → Brave → DDG → Marginalia).

Key (gateway .env): TAVILY_API_KEY (optional), BRAVE_API_KEY (optional,
free 2k/mo), MARGINALIA_API_KEY (optional — the shared `public` key works
but throttles; a free personal key via email avoids it). DDG and Marginalia
need no key. The monthly usage cap file lives on the gateway now
(state/usage.json).

Order is quality-first: Tavily (best snippets), Brave (independent index),
DDG scrape fallback (flaky from datacenters), Marginalia (independent,
non-commercial index — weak on fresh news, strong on evergreen/technical).
Each link is fail-soft; an empty result falls through to the next.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from atomic_json import load_json, save_json

ROOT = Path(__file__).resolve().parent.parent
USAGE_PATH = ROOT / "state" / "usage.json"
MONTHLY_CAP = 950  # leave 50/mo headroom on Tavily
# 10, not 5. This is the widest single lever on how much a research report can
# know: one search call returns twice the candidate URLs for the same token
# cost and the same round trip. The researcher still decides which to fetch, so
# a wider net widens the choice rather than the bill. Tavily's own tier and the
# free backends all serve 10 comfortably.
MAX_RESULTS = 10
_USAGE_PROVIDERS = ("tavily", "brave", "duckduckgo", "marginalia")
_BRAVE_CAP = 1900  # leave headroom on the free 2k/mo tier
_usage_lock = threading.Lock()


def _empty_usage(month: str) -> dict:
    return {"month": month,
            **{p: {"count": 0, "errors": 0} for p in _USAGE_PROVIDERS}}


def _load_usage() -> dict:
    month = datetime.now().strftime("%Y-%m")
    data = load_json(USAGE_PATH, None)
    if not isinstance(data, dict) or data.get("month") != month:
        return _empty_usage(month)
    for k in _USAGE_PROVIDERS:
        data.setdefault(k, {"count": 0, "errors": 0})
    return data


def _save_usage(data: dict) -> None:
    save_json(USAGE_PATH, data)


def _bump(provider: str, field: str = "count") -> None:
    with _usage_lock:
        data = _load_usage()
        data[provider][field] = data[provider].get(field, 0) + 1
        _save_usage(data)


def usage() -> dict:
    """Current monthly usage (for the dashboard)."""
    with _usage_lock:
        return _load_usage()


# ── rolling source health ──────────────────────────────────────────────────
# The chain above is static and quality-ordered, which is right for a cold
# start and wrong in steady state. Two of these sources throttle shared /
# datacenter egress: GDELT and OpenAlex refuse outright, DDG's scrape
# intermittently bot-blocks, and Marginalia's shared key rate-limits. When
# that happens every run pays the same timeout before falling through, and
# nothing in the system noticed or adapted.
#
# So each call is now recorded (success/failure + latency) in a small rolling
# window, and the chain is walked in health order within its quality tiers.
# Quality still decides the tier; health only reorders *within* a tier and
# demotes a source that is currently failing to the back of the queue.
HEALTH_PATH = ROOT / "state" / "source_health.json"
HEALTH_WINDOW = 50          # calls kept per source
HEALTH_COOLDOWN_S = 300.0   # a failing source is deprioritised for 5 min
# A source that just ERRORED is skipped for this long. The constant existed
# from the start but was never used, so every search paid the same timeout
# for the same broken backend: the live ledger showed 93 web-search calls
# averaging 9.2s, with the worst at 25.5s - a chain of four sources, each
# allowed to burn its own timeout before the next was tried.
SOURCE_COOLDOWN_S = 300.0
# Per-source budget. A source that cannot answer inside this is treated as
# failed and the chain moves on.
SOURCE_TIMEOUT_S = float(os.getenv("WEBSEARCH_SOURCE_TIMEOUT_S", "6") or 6)
# Whole-chain budget. When it is spent the search returns what it has (or
# nothing) instead of continuing to the next source.
SEARCH_DEADLINE_S = float(os.getenv("WEBSEARCH_DEADLINE_S", "15") or 15)
_health_lock = threading.Lock()


def _load_health() -> dict:
    data = load_json(HEALTH_PATH, None)
    if not isinstance(data, dict):
        return {}
    return data


def _record_health(source: str, ok: bool, latency_s: float,
                   errored: bool = False) -> None:
    """Append one observation, keeping the last HEALTH_WINDOW.

    `errored` is tracked separately from "returned nothing": a source that
    answered with no hits is working, and cooling it down for that would
    take a working backend out of the chain. A source that THREW is the one
    that needs a cooldown.
    """
    try:
        with _health_lock:
            data = _load_health()
            row = data.get(source) or {"ok": 0, "fail": 0, "lat": []}
            row["ok" if ok else "fail"] = int(row.get("ok" if ok else "fail", 0)) + 1
            lat = list(row.get("lat") or [])
            lat.append(round(float(latency_s or 0.0), 3))
            row["lat"] = lat[-HEALTH_WINDOW:]
            if errored:
                row["last_err"] = time.time()
            data[source] = row
            save_json(HEALTH_PATH, data)
    except Exception:
        pass  # health telemetry must never break a search


def _in_cooldown(source: str) -> bool:
    """True when this source errored recently and must not be tried again.

    Without it every search paid the same broken source's timeout: the
    chain walked four sources in order, each allowed to burn its own
    timeout, and a search could take 25s.
    """
    row = _load_health().get(source) or {}
    last_err = float(row.get("last_err") or 0.0)
    if not last_err:
        return False
    return (time.time() - last_err) < SOURCE_COOLDOWN_S


def source_health() -> dict:
    """Per-source success rate and p50 latency, plus the current order."""
    data = _load_health()

    def _score(name: str) -> float:
        row = data.get(name) or {}
        ok, fail = int(row.get("ok", 0)), int(row.get("fail", 0))
        total = ok + fail
        lat = sorted(row.get("lat") or [])
        p50 = lat[len(lat) // 2] if lat else 0.0
        # Jeffreys prior rather than "untried == perfect": with a flat 1.0 an
        # untried source outranks every source that has ever failed, so a
        # recovered source can never climb back into the chain.
        rate = (ok + 1) / (total + 2)
        # Success dominates; latency only breaks ties and nudges. A source
        # that is right 100% of the time but takes 20s still loses to one
        # that is right 95% of the time in 1s.
        return round(rate - min(p50, 30.0) / 300.0, 4)

    tiers = {"paid": ["tavily", "brave"], "free": ["duckduckgo", "marginalia"]}
    order: dict[str, list[str]] = {}
    for tier, members in tiers.items():
        order[tier] = sorted(members, key=lambda s: -_score(s))
    return {"scores": {s: _score(s) for t in tiers.values() for s in t},
            "order": order, "window": HEALTH_WINDOW,
            "observed": {s: {"ok": int((data.get(s) or {}).get("ok", 0)),
                             "fail": int((data.get(s) or {}).get("fail", 0))}
                         for t in tiers.values() for s in t}}


def _tavily_search(query: str, max_results: int) -> list[dict]:
    from tavily import TavilyClient
    token = os.environ.get("TAVILY_API_KEY")
    if not token:
        return []
    client = TavilyClient(token)
    # Tavily's own default is 60s. A search that takes a minute is a
    # broken feature, and the chain has other sources to try.
    resp = client.search(query=query, max_results=max_results,
                         search_depth="advanced", timeout=SOURCE_TIMEOUT_S)
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": r.get("content", "")} for r in resp.get("results", [])]


def _ddg_search(query: str, max_results: int) -> list[dict]:
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return []
    hits: list[dict] = []
    try:
        with DDGS() as ddgs:
            # Two backends, not three: DDG's own fan-out already tries
            # several engines per call, and the third fallback rarely
            # answered when the first two did not - it only added seconds.
            for backend in ("auto", "html"):
                try:
                    hits = list(ddgs.text(query, max_results=max_results,
                                           backend=backend))
                except Exception:
                    hits = []
                if hits:
                    break
    except Exception:
        return []
    return [{"title": h.get("title", ""), "url": h.get("href", ""),
             "snippet": h.get("body", "")} for h in hits]


def _brave_search(query: str, max_results: int) -> list[dict]:
    """Brave Search API (optional BRAVE_API_KEY, free 2k/mo). Independent
    index, JSON-native - no scraping, so none of DDG's datacenter flakiness.
    Skipped cleanly when unset."""
    import httpx as _hx
    token = (os.environ.get("BRAVE_API_KEY") or "").strip()
    if not token:
        return []
    try:
        with _hx.Client(timeout=SOURCE_TIMEOUT_S) as c:
            r = c.get("https://api.search.brave.com/res/v1/web/search",
                      params={"q": query, "count": max_results},
                      headers={"Accept": "application/json",
                               "X-Subscription-Token": token})
            r.raise_for_status()
            results = ((r.json().get("web") or {}).get("results") or [])[:max_results]
    except Exception:
        return []
    return [{"title": x.get("title", ""), "url": x.get("url", ""),
             "snippet": x.get("description", "")} for x in results]


def _marginalia_search(query: str, max_results: int) -> list[dict]:
    """Marginalia independent index (MARGINALIA_API_KEY optional — the shared
    `public` key works but throttles; a free personal key via email avoids
    it). Last resort: weak on fresh news, strong on evergreen/technical
    content the commercial engines bury."""
    import httpx as _hx
    key = (os.environ.get("MARGINALIA_API_KEY") or "").strip() or "public"
    try:
        with _hx.Client(timeout=SOURCE_TIMEOUT_S) as c:
            r = c.get("https://api2.marginalia-search.com/search",
                      params={"query": query, "count": max_results},
                      headers={"API-Key": key})
            r.raise_for_status()
            results = (r.json().get("results") or [])[:max_results]
    except Exception:
        return []
    return [{"title": x.get("title", ""), "url": x.get("url", ""),
             "snippet": x.get("description", "")} for x in results]


def _call_with_deadline(fn, query: str, max_results: int,
                        timeout_s: float) -> list[dict]:
    """Call a search backend under a hard time budget.

    The backends are third-party clients with their own idea of how long to
    wait: Tavily's default is 60s, DDG's backend fan-out can hang on a
    bot-blocked endpoint, and Marginalia's client was set to 20s. Any of
    them could spend the whole user-visible wait. So each call runs on its
    own daemon thread and the caller stops waiting at the deadline; the
    abandoned thread is a daemon and cannot keep the process alive.
    """
    box: dict = {}

    def _run():
        try:
            box["results"] = fn(query, max_results)
        except BaseException as e:       # noqa: BLE001 - re-raised below
            box["error"] = e

    t = threading.Thread(target=_run, daemon=True, name="websearch-src")
    t.start()
    t.join(timeout=timeout_s)
    if t.is_alive():
        raise TimeoutError(f"the source did not answer within {timeout_s}s")
    if "error" in box:
        raise box["error"]
    return box.get("results") or []


def search(*, query: str, max_results: int = MAX_RESULTS) -> list[dict]:
    """Search the chain under a total deadline.

    Two budgets apply, because a search that takes 25s is a broken feature:
    a per-source timeout, and a whole-chain deadline. A source that just
    errored is skipped for `SOURCE_COOLDOWN_S` instead of being paid for
    again on the next search - but only until every other source has also
    failed. A cooldown that can return NOTHING is worse than a slow
    search: with the free tier in cooldown (and no API keys configured) the
    whole chain answered with an empty list.
    """
    import time as _t
    max_results = max(1, min(int(max_results or MAX_RESULTS), MAX_RESULTS))
    usage = _load_usage()
    h = source_health()["order"]
    deadline = _t.perf_counter() + SEARCH_DEADLINE_S
    # Paid tier first (quality), each tier walked in health order (reality).
    plan: list[str] = []
    for tier in ("paid", "free"):
        plan.extend(h.get(tier, []))
    fresh = [s for s in plan if not _in_cooldown(s)]
    cooled = [s for s in plan if _in_cooldown(s)]
    for src in fresh + cooled:
        if _t.perf_counter() >= deadline:
            return []
        if src == "tavily" and not (os.environ.get("TAVILY_API_KEY")
                                    and usage["tavily"]["count"] < MONTHLY_CAP):
            continue
        if src == "brave" and not (os.environ.get("BRAVE_API_KEY")
                                   and usage["brave"]["count"] < _BRAVE_CAP):
            continue
        t0 = _t.perf_counter()
        try:
            fn = _tavily_search if src == "tavily" else (
                _brave_search if src == "brave" else (
                    _ddg_search if src == "duckduckgo"
                    else _marginalia_search))
            results = _call_with_deadline(fn, query, max_results,
                                          SOURCE_TIMEOUT_S)
        except Exception:
            _bump(src, "errors")
            _record_health(src, False, _t.perf_counter() - t0,
                           errored=True)
            continue
        _record_health(src, bool(results), _t.perf_counter() - t0)
        if results:
            _bump(src)
            return results
        _bump(src, "errors")
    return []
