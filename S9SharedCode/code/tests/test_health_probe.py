"""Gateway liveness must fail fast, and `/api/health` must not lie.

A user-visible defect motivated these:

* The console's "gateway unreachable" banner polls `/api/health`. That endpoint
  served a cached value for up to 30s and refreshed it in the background, so a
  gateway killed right after a successful probe stayed "up" in the UI. Worse,
  the banner's Retry called the same cached value, so pressing it CLEARED the
  banner while the gateway was still dead.
* With the port closed, Windows does not refuse the connection - it drops the
  SYN. `_is_up()` tried three paths with a 2s timeout each, so every down probe
  burned ~6.4s of a background thread. `/api/health` now probes on the request
  path when its value is stale, so that multiplier mattered.

The contracts pinned here: a connection-level failure ends the probe (the
paths share one host), HTTP-level failures still fall through to the next path
(an older gateway may not have `/health`), a fresh value is reported when it is
stale, and `?fresh=1` always probes.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_server
import gateway
from agent_server import app


def test_connection_failure_ends_the_probe(monkeypatch):
    """Nothing is listening: the other two paths cannot answer either."""
    calls: list[str] = []

    class Refused:
        def get(self, url, timeout=None):
            calls.append(url)
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(gateway, "_client", lambda: Refused())
    assert gateway._is_up() is False
    assert len(calls) == 1, f"probed all {len(calls)} paths after a refused connect"


def test_http_failure_falls_through_to_the_next_path(monkeypatch):
    """An old gateway may 404 `/health`; `/healthz` still proves it is up."""
    responses = [httpx.Response(404), httpx.Response(200)]
    calls: list[str] = []

    class OldGateway:
        def get(self, url, timeout=None):
            calls.append(url)
            return responses.pop(0)

    monkeypatch.setattr(gateway, "_client", lambda: OldGateway())
    assert gateway._is_up() is True
    assert len(calls) == 2


def test_health_reports_a_stale_value_as_down(monkeypatch):
    """A cached "up" older than the TTL is refreshed before answering."""
    agent_server._GW_UP.update({"up": True, "at": time.time() - 3600})
    monkeypatch.setattr(agent_server, "_gateway_up_now", lambda: False)
    with TestClient(app) as client:
        body = client.get("/api/health").json()
    assert body["gateway_up"] is False


def test_health_fresh_always_probes(monkeypatch):
    """The banner's Retry must not be answered from a still-fresh cache.

    This is the false-clear: gateway dies, cache says up for another few
    seconds, Retry re-reads it, banner disappears.
    """
    calls: list[int] = []

    def probe() -> bool:
        calls.append(1)
        return False

    agent_server._GW_UP.update({"up": True, "at": time.time()})
    monkeypatch.setattr(agent_server, "_gateway_up_now", probe)
    with TestClient(app) as client:
        # Re-stamp inside startup: importing/starting the app can take longer
        # than the 8s TTL, which would (correctly) make the next read probe.
        agent_server._GW_UP.update({"up": True, "at": time.time()})
        cached = client.get("/api/health").json()
        assert cached["gateway_up"] is True    # within TTL: cheap, no probe
        assert calls == []                     # ...and it did not probe
        fresh = client.get("/api/health?fresh=1").json()
    assert fresh["gateway_up"] is False    # Retry asked, and got the truth
    assert calls == [1]
