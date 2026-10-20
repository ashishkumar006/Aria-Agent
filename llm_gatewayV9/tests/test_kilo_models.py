"""Kilo Code model discovery: normalization, route registration, fallback.

Convention matches tests/test_all_providers.py: offline `test_*` functions are
collected by pytest, the network-touching one is `check_live` and runs only
when this file is executed directly.
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import providers as P  # noqa: E402
import kilo_models as KM  # noqa: E402


def test_normalize_extracts_vendor_free_and_capabilities():
    raw = [
        {"id": "nvidia/nemotron-3-ultra:free", "name": "NVIDIA: Ultra (free)",
         "pricing": {"prompt": "0", "completion": "0"},
         "context_length": 1000000, "isFree": True,
         "supported_parameters": ["tools", "reasoning", "max_tokens"],
         "architecture": {"input_modalities": ["text", "image"]},
         "description": "Line one.\nLine two."},
        {"id": "anthropic/claude-sonnet-4.5",
         "pricing": {"prompt": "0.000003", "completion": "0.000015"}},
        {"id": "kilo-auto/free", "pricing": {"prompt": "-1", "completion": "-1"}},
        {"id": "", "name": "no id, must be dropped"},
    ]
    out = [n for n in (KM._normalize(m) for m in raw) if n]
    assert len(out) == 3
    a = out[0]
    assert a["vendor"] == "nvidia" and a["owned_by"] == "nvidia"
    assert a["is_free"] is True
    assert a["supports_tools"] and a["supports_reasoning"] and a["supports_vision"]
    assert a["context_length"] == 1000000
    assert a["description"] == "Line one."
    assert out[1]["is_free"] is False          # isFree absent -> pricing decides
    assert out[2]["is_free"] is False          # -1 is unmetered, not free
    assert out[2]["vendor"] == "kilo-auto"


def test_picks_a_free_tool_calling_model_not_a_hardcoded_name():
    """The default must come from the roster, not from a literal in source.

    `tencent/hy3:free` was the hardcoded default and upstream no longer serves
    it (it is now `tencent/hy3`, paid), so every Kilo call 404'd until a model
    was picked by hand.
    """
    roster = [
        {"id": "anthropic/claude-sonnet-4.5", "is_free": False,
         "supports_tools": True, "context_length": 200000},
        {"id": "small/free-mini", "is_free": True, "supports_tools": True,
         "context_length": 8000},
        {"id": "big/free-max", "is_free": True, "supports_tools": True,
         "context_length": 1000000},
        {"id": "free/no-tools", "is_free": True, "supports_tools": False,
         "context_length": 4000000},
    ]
    # Largest-context free model that can actually run the harness: the 4M
    # free model is bigger but has no tools, so it is useless for coding.
    assert KM.pick_default_model(roster) == "big/free-max"
    # The free router does NOT win on context alone: it rotates backends and
    # measured 0-token empty bodies. A deterministic model beats a variable
    # one for a harness.
    assert KM.pick_default_model(roster + [
        {"id": "kilo-auto/free", "is_free": True, "supports_tools": True,
         "context_length": 256000}]) == "big/free-max"
    # It is still preferred when nothing free supports tools.
    assert KM.pick_default_model([
        {"id": "kilo-auto/free", "is_free": True, "supports_tools": False,
         "context_length": 256000},
        {"id": "free/no-tools", "is_free": True, "supports_tools": False,
         "context_length": 4000}]) == "kilo-auto/free"
    # No free tier -> no default, and we must not invent a paid one.
    assert KM.pick_default_model([roster[0]]) is None
    assert KM.pick_default_model([]) is None
    assert KM.pick_default_model(None) is None


def test_verified_code_model_beats_a_bigger_unknown_one():
    """A measured code model outranks a larger model that does not answer.

    inkling-small has the catalogue's largest free context and returns 429;
    nemotron-3-ultra returns an empty body. Ranking by context alone picks
    exactly those.
    """
    roster = [
        {"id": "thinkingmachines/inkling-small:free", "is_free": True,
         "supports_tools": True, "context_length": 1048576},
        {"id": "nvidia/nemotron-3-ultra-550b-a55b:free", "is_free": True,
         "supports_tools": True, "context_length": 1000000},
        {"id": "cohere/north-mini-code:free", "is_free": True,
         "supports_tools": True, "context_length": 256000},
    ]
    assert KM.pick_default_model(roster) == "cohere/north-mini-code:free"
    # If the verified model stops being free or loses tools, it is skipped and
    # the heuristic takes over rather than returning something unusable.
    for bad in ({"is_free": False}, {"supports_tools": False}):
        entry = dict(roster[2], **bad)
        others = [r for r in roster if r["id"] != "cohere/north-mini-code:free"]
        assert KM.pick_default_model(others + [entry]).startswith("thinkingmachines")
    # And if it leaves the catalogue entirely.
    assert KM.pick_default_model(roster[:2]) == "thinkingmachines/inkling-small:free"


def test_no_dead_kilo_ids_remain_hardcoded():
    """`tencent/hy3:free` and `stepfun/step-3.7-flash:free` are gone upstream.

    Keeping them as fallbacks would route coding requests to ids that 404.
    """
    dead = {"tencent/hy3:free", "stepfun/step-3.7-flash:free"}
    assert not (dead & set(P._KILO_FALLBACK_IDS))
    for mid in P._KILO_FALLBACK_IDS:
        assert "/" in mid, mid
    # The one hardcoded id we do keep must be a real free router.
    assert P._KILO_SAFE_DEFAULT == KM.SAFE_DEFAULT_MODEL
    assert P._KILO_SAFE_DEFAULT == "kilo-auto/free"
    # The machine .env must not pin a retired id either: KILO_MODEL outranks
    # the derived default, so a stale value there silently wins.
    assert P._KILO_SAFE_DEFAULT not in dead


def test_registers_discovered_ids_for_code_agents_only():
    P.MODEL_ROUTES.clear()
    P.CODE_MODEL_ROUTES.clear()
    P.KILO_MODEL_IDS.clear()
    P.register_model_routes(
        {"kilo": type("X", (), {"model": "vendor/configured"})()})
    # The no-key free router must be reachable by the coding agent...
    assert P.CODE_MODEL_ROUTES["kilo-auto/free"] == "kilo"
    assert P.route_for_model("kilo-auto/free", "code") == "kilo"
    # The configured KILO_MODEL is registered too, so a picker default routes.
    assert P.route_for_model("vendor/configured", "code") == "kilo"
    # ...and invisible to everything else.
    assert P.route_for_model("kilo-auto/free") is None
    assert P.route_for_model("kilo-auto/free", "chat") is None
    assert P.route_for_model("kilo-auto/free", "researcher") is None
    assert "kilo-auto/free" not in P.MODEL_ROUTES

    n = P.register_kilo_model_routes([
        "thinkingmachines/inkling-small:free",   # not a pre-registered fallback
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "gemini-3.5-flash-lite",            # no slash: must be ignored
        "",                                  # empty: ignored
        "thinkingmachines/inkling-small:free",  # duplicate: not counted twice
        "cohere/north-mini-code:free",       # already a fallback -> not "new"
    ])
    assert n == 2, n
    assert P.CODE_MODEL_ROUTES["thinkingmachines/inkling-small:free"] == "kilo"
    assert P.route_for_model("thinkingmachines/inkling-small:free", "code") == "kilo"
    assert P.route_for_model("thinkingmachines/inkling-small:free", "document") is None
    assert "gemini-3.5-flash-lite" not in P.KILO_MODEL_IDS
    assert P.register_kilo_model_routes(["thinkingmachines/inkling-small:free"]) == 0


def test_global_route_wins_over_the_code_scoped_table():
    """Scoping must never shadow an explicitly configured provider."""
    P.MODEL_ROUTES.clear()
    P.CODE_MODEL_ROUTES.clear()
    try:
        P.MODEL_ROUTES["vendor/model"] = "gemini35lite"
        P.CODE_MODEL_ROUTES["vendor/model"] = "kilo"
        assert P.route_for_model("vendor/model") == "gemini35lite"
        assert P.route_for_model("vendor/model", "code") == "gemini35lite"
    finally:
        P.MODEL_ROUTES.clear()
        P.CODE_MODEL_ROUTES.clear()


def test_unreachable_roster_degrades_without_raising():
    KM.reset_cache()
    r = asyncio.run(KM.fetch_models("http://127.0.0.1:1/nope"))
    assert r["error"], "expected an error to be reported, not raised"
    assert r["models"] == [] and r["stale"] is False and r["count"] == 0


def test_kilo_primary_key_is_dashboard_writable():
    """PROVIDER_POOLS advertises KILO_API_KEY, so ALLOWED_KEYS must allow it."""
    from channels_api import ALLOWED_KEYS, PROVIDER_POOLS
    single, plural = PROVIDER_POOLS["kilo"]
    assert single == "KILO_API_KEY" and plural == "KILO_API_KEYS"
    assert single in ALLOWED_KEYS, "dashboard rejects the one key a user has"
    assert plural in ALLOWED_KEYS


def test_startup_repoints_the_wired_provider_at_the_live_roster(monkeypatch):
    """build_providers() guesses; discovery corrects it. Unless KILO_MODEL is set."""
    import asyncio

    async def fake_get(*a, **k):
        return {"base_url": "x", "count": 1, "free_count": 1, "stale": False,
                "error": None, "fetched_at": 1.0,
                "models": [{"id": "big/free-max", "name": "n", "vendor": "v",
                            "owned_by": "v", "context_length": 1000000,
                            "max_completion_tokens": 0, "price_in": 0.0,
                            "price_out": 0.0, "is_free": True,
                            "supports_tools": True, "supports_vision": False,
                            "supports_reasoning": False, "description": ""}]}
    monkeypatch.setattr(KM, "get_models", fake_get)

    def mk(nm):
        return type(nm, (), {"name": "kilo", "model": "guessed"})()

    # Unset: every kilo instance moves together, or a failover would land on a
    # different model than the picker is showing.
    monkeypatch.delenv("KILO_MODEL", raising=False)
    ws = {"kilo": mk("a"), "kilo-2": mk("b")}
    asyncio.run(KM.prime_routes(ws))
    assert ws["kilo"].model == "big/free-max"
    assert ws["kilo-2"].model == "big/free-max"

    # Explicitly configured: the operator asked for it by name, never override.
    monkeypatch.setenv("KILO_MODEL", "anthropic/claude-sonnet-4.5")
    ws = {"kilo": mk("a")}
    asyncio.run(KM.prime_routes(ws))
    assert ws["kilo"].model == "guessed"

    # A non-kilo provider is never touched.
    monkeypatch.delenv("KILO_MODEL", raising=False)
    other = type("g", (), {"name": "gemini35lite", "model": "gemini-3.5"})()
    ws = {"kilo": mk("a"), "gemini35lite": other}
    asyncio.run(KM.prime_routes(ws))
    assert other.model == "gemini-3.5"


def check_live():
    r = asyncio.run(KM.fetch_models())
    assert r["error"] is None, r["error"]
    assert r["count"] > 100, r["count"]
    assert r["free_count"] > 0, r["free_count"]
    assert r["models"][0]["is_free"] is True, "sort must put free first"
    n = P.register_kilo_model_routes([m["id"] for m in r["models"]])
    print(f"  live: {r['count']} models, {r['free_count']} free; "
          f"+{n} routes, {len(P.KILO_MODEL_IDS)} routable")
    print(f"  first free: {r['models'][0]['id']} "
          f"ctx={r['models'][0]['context_length']}")


if __name__ == "__main__":
    print("offline:")
    for fn in (test_normalize_extracts_vendor_free_and_capabilities,
               test_picks_a_free_tool_calling_model_not_a_hardcoded_name,
               test_verified_code_model_beats_a_bigger_unknown_one,
               test_no_dead_kilo_ids_remain_hardcoded,
               test_registers_discovered_ids_for_code_agents_only,
               test_global_route_wins_over_the_code_scoped_table,
               test_unreachable_roster_degrades_without_raising,
               test_startup_repoints_the_wired_provider_at_the_live_roster,
               test_kilo_primary_key_is_dashboard_writable):
        fn()
        print(f"  ok {fn.__name__}")
    print("live:")
    check_live()
    print("all ok")