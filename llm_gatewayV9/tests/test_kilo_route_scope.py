"""Kilo model ids must route for the coding agent and for nobody else.

The unit tests cover the two tables. This covers the thing that actually
matters: that `POST /v1/chat` honours the scope. A Kilo id in the global table
would not crash anything - it would quietly send document-pipeline and chat
work to a free coding model, which is a slow bill and a wrong answer rather
than a visible failure.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import providers as P  # noqa: E402


KILO_ID = "cohere/north-mini-code:free"


@pytest.fixture
def wired(monkeypatch):
    """Kilo wired, with its catalogue registered for code agents."""
    for v in ("GEMINI_API_KEY", "GEMINI_API_KEYS", "NVIDIA_API_KEY",
              "NVIDIA_API_KEYS", "GROQ_API_KEY", "GROQ_API_KEYS",
              "CEREBRAS_API_KEY", "CEREBRAS_API_KEYS", "OPEN_ROUTER_API_KEY",
              "GITHUB_ACCESS_TOKEN", "OLLAMA_MODEL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("KILO_API_KEY", "kilo-test-key")
    P.KILO_MODEL_IDS.clear()
    from cache import GeminiCache
    ws = P.build_providers(GeminiCache())
    P.register_kilo_model_routes([KILO_ID])
    yield ws
    P.MODEL_ROUTES.clear()
    P.CODE_MODEL_ROUTES.clear()
    P.KILO_MODEL_IDS.clear()


def test_kilo_is_in_the_pool(wired):
    assert "kilo" in wired
    assert wired["kilo"].model


def test_code_agent_routes_to_kilo(wired):
    assert P.route_for_model(KILO_ID, "code") == "kilo"


@pytest.mark.parametrize("agent", [
    None, "", "chat", "researcher", "author", "planner", "document", "codex",
])
def test_no_other_agent_can_reach_kilo_by_model_name(wired, agent):
    assert P.route_for_model(KILO_ID, agent) is None


def test_a_non_code_agent_falls_through_to_the_normal_ladder(wired):
    """No route means no override: the request uses the provider ladder."""
    from router import Router
    r = Router(wired, list(wired.keys()))
    assert P.route_for_model(KILO_ID, "researcher") is None
    # The ladder still contains kilo, so it stays reachable as a last-resort
    # failover. That is separate from model-name routing and is intentional:
    # scoping the ROUTE does not remove the provider from the pool.
    assert "kilo" in r.order


def test_unwired_kilo_does_not_pin_the_request(monkeypatch):
    """A catalogue id with no key must fall back, not 400.

    Kilo's /models needs no auth, so the picker is populated before a key
    exists. If routing pinned provider="kilo" anyway, every pick would fail
    with 'unknown provider' instead of using the normal ladder.

    The env is controlled here rather than assumed: whether this machine has a
    KILO_API_KEY is not something a behaviour test should depend on.
    """
    import asyncio

    from starlette.testclient import TestClient
    import main as gm
    import providers as PP

    seen = {}

    def fake_pick(self, *a, **k):
        seen["provider"] = k.get("provider")
        # pick() returns (name, attempts); the request then fails on the fake
        # provider. Fine - the assertion is about what got pinned.
        return "gemini35lite", []

    for v in ("KILO_API_KEY", "KILO_API_KEYS"):
        monkeypatch.delenv(v, raising=False)
    PP.MODEL_ROUTES.clear()
    PP.CODE_MODEL_ROUTES.clear()
    PP.register_kilo_model_routes([KILO_ID])
    orig = gm.Router.pick
    gm.Router.pick = fake_pick

    async def drive():
        # The repo convention: run the lifespan so app.state.router exists.
        async with gm.app.router.lifespan_context(gm.app):
            c = TestClient(gm.app)
            assert "kilo" not in c.app.state.router.providers, (
                "this test requires Kilo to be UNWIRED")
            return c.post("/v1/chat", json={
                "prompt": "hi", "agent": "code", "model": KILO_ID,
                "max_tokens": 5,
            })

    try:
        r = asyncio.run(drive())
        assert seen.get("provider") != "kilo", seen
        # Degrades to a normal ladder failure, not an unknown-provider 400.
        assert r.status_code != 400, r.text
    finally:
        gm.Router.pick = orig
        PP.MODEL_ROUTES.clear()
        PP.CODE_MODEL_ROUTES.clear()


def test_explicit_model_pins_provider_even_for_chat(wired):
    """The console's picker passes a model id; the request must run on THAT
    model's provider, not the whole failover ladder.

    `route_for_model` stays scope-gated (a chat request must not reach Kilo by
    *default*), but an EXPLICIT id is the user's choice, so the endpoint-level
    resolver honours it for any agent - guarded on the provider being wired so
    an unconfigured key still degrades to the ladder instead of a 400.
    """
    import main as gm

    assert gm._explicit_provider_for(KILO_ID, "chat", wired) == "kilo"
    assert gm._explicit_provider_for(KILO_ID, None, wired) == "kilo"
    # Not wired -> no pin, so the request falls back instead of 400-ing.
    assert gm._explicit_provider_for(KILO_ID, "chat", {}) is None
    # An unknown id pins nothing.
    assert gm._explicit_provider_for("someone/unknown", "chat", wired) is None

