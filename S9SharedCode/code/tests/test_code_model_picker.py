"""The Code panel can pick a model; the pick must actually reach the gateway.

Two things are easy to get wrong here and both are silent:

  1. The pick is a bare `vendor/name` id. The gateway resolves it to a
     provider itself, so the panel never hardcodes a provider - but that also
     means an unvalidated string travels into a URL-shaped field upstream.
     `_MODEL_ID_RE` is the guard, and it is pinned here.

  2. When no model is picked the request must be byte-identical to before.
     `model=None` is stripped from the payload by the gateway client, so
     "unset" and "not implemented" stay distinguishable.

`/api/models` is the roster feed. It is deliberately NOT under `/api/code/`:
that prefix has an exact-route-set assertion, and a picker is not a
workspace operation.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import agent_server as ag  # noqa: E402


@pytest.fixture(scope="module")
def tc():
    from starlette.testclient import TestClient
    return TestClient(ag.app)


# ── model id validation ────────────────────────────────────────────────────

@pytest.mark.parametrize("good", [
    "kilo-auto/free",
    "cohere/north-mini-code:free",
    "anthropic/claude-sonnet-4.5",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "qwen/qwen3-coder-30b-a3b-instruct@v2",
])
def test_accepts_real_model_ids(good):
    assert ag._MODEL_ID_RE.match(good), good


@pytest.mark.parametrize("bad", [
    "",                              # empty -> treated as unset, not an error
    "gpt-4",                         # no vendor prefix
    "../../etc/passwd",              # traversal
    "vendor/name/extra",             # two slashes
    "http://evil.example/x",         # URL smuggling
    "vendor/name\nX-Injected: 1",    # header injection
    "vendor/na me",                  # space
    "a" * 80 + "/" + "b" * 200,      # length
])
def test_rejects_malformed_model_ids(bad):
    # The empty string is handled by the caller as "unset", so it must not
    # match the regex - otherwise it would be rejected instead of ignored.
    assert not ag._MODEL_ID_RE.match(bad), bad


def test_code_ask_rejects_a_malformed_model(tc):
    r = tc.post("/api/code/ask", json={
        "question": "what does this do?", "model": "not a model id",
    })
    assert r.status_code == 400, r.text
    assert "vendor/name" in r.json().get("error", "")


def test_model_reachability_is_exposed(tc):
    """The picker needs this route, and it must answer rather than 404."""
    r = tc.get("/api/models")
    assert r.status_code == 200, r.text
    body = r.json()
    # Shape is fixed even when the gateway is unreachable: the UI branches on
    # `error`, and a missing key would make it look like an empty catalogue.
    for k in ("models", "count", "free_count", "stale", "error"):
        assert k in body, k
    assert isinstance(body["models"], list)
    assert body["count"] == len(body["models"])


def test_roster_fallback_when_gateway_is_down(tc, monkeypatch):
    async def boom(*a, **k):
        return {"models": [], "count": 0, "free_count": 0, "stale": False,
                "error": "gateway unreachable"}
    monkeypatch.setattr(ag, "_gw_json", boom)
    body = tc.get("/api/models").json()
    assert body["models"] == []
    assert body["error"], "a down gateway must be distinguishable from no models"


def test_roster_normalizes_upstream_entries(tc, monkeypatch):
    async def fake(path, **k):
        assert path.startswith("/v1/providers/kilo/models")
        return {
            "count": 2, "free_count": 1, "stale": False, "error": None,
            "models": [
                {"id": "cohere/north-mini-code:free", "name": "Code",
                 "vendor": "cohere", "context_length": 256000,
                 "is_free": True, "supports_tools": True},
                {"id": "anthropic/claude-sonnet-4.5", "name": "Sonnet",
                 "vendor": "anthropic", "context_length": 200000,
                 "is_free": False, "supports_tools": True},
            ],
        }
    monkeypatch.setattr(ag, "_gw_json", fake)
    body = tc.get("/api/models").json()
    assert body["count"] == 2 and body["free_count"] == 1
    assert body["error"] is None
    assert body["models"][0]["id"] == "cohere/north-mini-code:free"


def test_refresh_flag_is_forwarded(tc, monkeypatch):
    seen = {}

    async def fake(path, **k):
        seen["path"] = path
        return {"count": 0, "free_count": 0, "stale": False, "error": None,
                "models": []}
    monkeypatch.setattr(ag, "_gw_json", fake)
    tc.get("/api/models")
    assert "refresh" not in seen["path"]
    tc.get("/api/models?refresh=true")
    assert "refresh=true" in seen["path"]