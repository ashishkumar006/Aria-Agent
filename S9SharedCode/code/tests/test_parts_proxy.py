"""The console reaches the mesh optimiser through the agent, which is a proxy.

The Parts view used to fetch the gateway from the browser, which cannot work:
the gateway has no CORS middleware and the page never receives its token, so
every call died as "failed to fetch" and no pass list ever loaded. These tests
pin the proxy: the gateway is called with its token server-side, the uploaded
bytes travel with the request, the pass parameters survive as a query string,
and a bad upload is refused before the gateway is touched.
"""
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    import agent_server
    import auth
    # Do NOT call auth.configure(): it regenerates the per-launch token for the
    # whole process. Set the module value instead.
    monkeypatch.setattr(auth, "_token", "test-token", raising=False)
    # Pin the gateway token header so the test does not depend on the gateway
    # module being importable/readable in this process.
    monkeypatch.setattr(agent_server, "_gw_auth_headers",
                        lambda extra=None: {"X-Gateway-Token": "gw-test"})
    return TestClient(agent_server.app), {"X-Aria-Token": "test-token"}


def _install_fake(monkeypatch, payload, status=200):
    """Replace httpx.AsyncClient with a recorder returning `payload`."""
    seen = {"calls": [], "headers": {}}

    class _Resp:
        def __init__(self):
            self.status_code = status
            self.is_success = 200 <= status < 300
            self.text = json.dumps(payload)

        def json(self):
            return payload

    class _Client:
        def __init__(self, *a, **kw):
            seen["headers"] = dict(kw.get("headers") or {})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            seen["calls"].append(("GET", url, None))
            return _Resp()

        async def post(self, url, files=None, json=None, **kw):
            seen["calls"].append(("POST", url, files))
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return seen


def test_pass_list_is_proxied_with_the_gateway_token(client, monkeypatch):
    c, hdr = client
    seen = _install_fake(monkeypatch, {"passes": [{"name": "decimate"}]})
    r = c.get("/api/parts/passes", headers=hdr)
    assert r.status_code == 200, r.text[:200]
    assert r.json()["passes"][0]["name"] == "decimate"
    method, url, _ = seen["calls"][0]
    assert method == "GET"
    assert url.endswith("/v1/parts/optimise/passes")
    assert seen["headers"].get("X-Gateway-Token") == "gw-test", \
        "the gateway rejects /v1/* without its token"


def test_a_mesh_upload_is_forwarded_to_the_gateway(client, monkeypatch):
    c, hdr = client
    seen = _install_fake(monkeypatch, {"objective": 1.0, "metrics": {}})
    r = c.post("/api/parts/geometry",
               files={"file": ("p.stl", b"solid x", "application/octet-stream")},
               headers=hdr)
    assert r.status_code == 200, r.text[:200]
    method, url, files = seen["calls"][0]
    assert method == "POST" and url.endswith("/v1/parts/geometry")
    assert files["file"][1] == b"solid x", "the bytes did not travel"
    assert seen["headers"].get("X-Gateway-Token") == "gw-test"


def test_pass_parameters_survive_as_a_query_string(client, monkeypatch):
    c, hdr = client
    seen = _install_fake(monkeypatch, {"changed": False})
    r = c.post("/api/parts/optimise?pass_name=decimate&target_faces=1000",
               files={"file": ("p.stl", b"solid x", "application/octet-stream")},
               headers=hdr)
    assert r.status_code == 200, r.text[:200]
    url = seen["calls"][0][1]
    assert url.endswith("/v1/parts/optimise?pass_name=decimate&target_faces=1000")


def test_an_empty_upload_is_refused_before_any_call(client, monkeypatch):
    c, hdr = client

    class _Boom:
        def __init__(self, *a, **kw):
            raise AssertionError("the gateway was called for an empty upload")

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    r = c.post("/api/parts/geometry",
               files={"file": ("p.stl", b"", "application/octet-stream")},
               headers=hdr)
    assert r.status_code == 400
    assert "empty" in r.json()["error"].lower()


def test_a_dead_gateway_degrades_to_an_error_not_a_crash(client, monkeypatch):
    c, hdr = client

    class _Boom:
        def __init__(self, *a, **kw):
            raise OSError("connection refused")

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    r = c.get("/api/parts/passes", headers=hdr)
    # The pass list degrades like the models/voices pickers: a dict carrying
    # `error`, never a 500.
    assert r.status_code == 200
    assert r.json().get("error")
