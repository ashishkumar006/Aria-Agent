"""New free channels (ntfy, zoho_cliq, bluesky, mastodon, google_chat).

Offline: httpx faked at the client seam. Contract: missing creds fail soft;
success parses each provider's response. Run:
  uv run pytest integrations/test_new_channels.py -q
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import _adaptor_kit as kit


class _FakeClient:
    def __init__(self, handler):
        self._handler = handler
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _call(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        return self._handler(method, url, **kw)

    def get(self, url, **kw):
        return self._call("GET", url, **kw)

    def post(self, url, **kw):
        return self._call("POST", url, **kw)


def _patch(monkeypatch, handler):
    import httpx
    import adaptors.http as _h
    _h.reset_pools()
    box: dict = {}

    def _factory(*a, **k):
        box["client"] = _FakeClient(handler)
        return box["client"]

    monkeypatch.setattr(httpx, "Client", _factory)
    return box


def _handler(method, url, **kw):
    if "createSession" in url:
        return kit.FakeResp(200, {"accessJwt": "jwt", "did": "did:me"})
    if "createRecord" in url:
        return kit.FakeResp(200, {"uri": "at://x", "cid": "cid1"})
    if url.endswith("/api/v1/statuses"):
        return kit.FakeResp(200, {"id": "1", "url": "https://m/1"})
    return kit.FakeResp(200, {"id": "n1"})


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("NTFY_TOKEN", "ZOHO_CLIQ_TOKEN", "BSKY_HANDLE", "BSKY_APP_PASSWORD",
              "MASTODON_TOKEN", "MASTODON_INSTANCE", "GOOGLE_CHAT_WEBHOOK"):
        monkeypatch.delenv(k, raising=False)
    yield


def test_ntfy_keyless_and_send(monkeypatch):
    import integrations.ntfy as n
    assert n.send(topic="", message="x")["ok"] is False  # topic required
    _patch(monkeypatch, _handler)
    r = n.send(topic="alerts", message="hi")
    assert r["ok"] is True and r["topic"] == "alerts"


def test_cliq_fail_soft_and_send(monkeypatch):
    import integrations.cliq as c
    r = c.send(to="bot", text="hi")
    assert r["ok"] is False and "ZOHO_CLIQ_TOKEN" in r["error"]
    monkeypatch.setenv("ZOHO_CLIQ_TOKEN", "tok")
    _patch(monkeypatch, _handler)
    assert c.send(to="bot", text="hi")["ok"] is True


def test_bluesky_fail_soft_and_send(monkeypatch):
    import integrations.bluesky as b
    r = b.send(text="hi")
    assert r["ok"] is False and "BSKY_HANDLE" in r["error"]
    monkeypatch.setenv("BSKY_HANDLE", "me.bsky.social")
    monkeypatch.setenv("BSKY_APP_PASSWORD", "pw")
    _patch(monkeypatch, _handler)
    r2 = b.send(text="hi")
    assert r2["ok"] is True and r2["uri"] == "at://x", r2


def test_mastodon_fail_soft_and_send(monkeypatch):
    import integrations.mastodon as m
    r = m.send(text="hi")
    assert r["ok"] is False and "MASTODON_TOKEN" in r["error"]
    monkeypatch.setenv("MASTODON_TOKEN", "tok")
    monkeypatch.setenv("MASTODON_INSTANCE", "mastodon.social")
    _patch(monkeypatch, _handler)
    r2 = m.send(text="hi")
    assert r2["ok"] is True and r2["id"] == "1"


def test_google_chat_fail_soft_and_send(monkeypatch):
    import integrations.google_chat as g
    r = g.send(text="hi")
    assert r["ok"] is False and "GOOGLE_CHAT_WEBHOOK" in r["error"]
    monkeypatch.setenv("GOOGLE_CHAT_WEBHOOK", "https://chat.example/hook")
    _patch(monkeypatch, _handler)
    assert g.send(text="hi")["ok"] is True