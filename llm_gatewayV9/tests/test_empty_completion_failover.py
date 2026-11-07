"""An empty completion is a failed call, not an answer.

Live: Gemini's worker repeatedly finished with zero text and zero tool calls
(~50s, output_tokens=0). The gateway returned 200, so every skill retried the
same context, then the recovery planner wrote a thinner document. Treating it
as a provider failure lets the existing failover ladder try another model.
"""
import asyncio

import pytest


def test_an_empty_completion_is_never_returned_as_success(monkeypatch):
    from starlette.testclient import TestClient
    import main as gm

    async def drive():
        # The repo convention: run the lifespan so app.state.router exists.
        async with gm.app.router.lifespan_context(gm.app):
            c = TestClient(gm.app)
            router = c.app.state.router
            if not router.order:
                return None
            name = router.order[0]
            provider = router.providers[name]

            async def empty(*_a, **_k):
                return {"text": "", "tool_calls": [],
                        "model": getattr(provider, "model", name),
                        "input_tokens": 10, "output_tokens": 0}

            monkeypatch.setattr(provider, "chat", empty)
            return c.post("/v1/chat", json={
                "prompt": "hi", "provider": name, "max_tokens": 5})

    r = asyncio.run(drive())
    if r is None:
        pytest.skip("no provider wired in this environment")
    assert r.status_code == 502, r.text
    assert "empty completion" in r.text
