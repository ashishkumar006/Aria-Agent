"""`integrations.websearch.search_images` — the licensed supply side.

The two things worth pinning are both about honesty of the result set:

1. Only PNG/JPEG URLs are returned. docgen embeds exactly those two formats, so a
   GIF or WebP that searches successfully and is then dropped at render time is a
   figure the model asked for and nobody ever sees.
2. The licence travels with the image. A result without `license`/`creator` is a
   legal problem for the user, not a smaller picture.
"""
from __future__ import annotations

import httpx
import pytest

import integrations.websearch as w


def _fake_client(monkeypatch, payload, *, status=200):
    class _R:
        def __init__(self):
            self.status_code = status
            self.headers = {"content-type": "application/json"}

        def raise_for_status(self):
            if status >= 400:
                raise httpx.HTTPStatusError("boom", request=None, response=None)

        def json(self):
            return payload

    class _C:
        def __init__(self, *a, **k):
            self.kwargs = k

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            _C.last = {"url": url, "params": params or {},
                       "timeout": self.kwargs.get("timeout")}
            return _R()

    monkeypatch.setattr(httpx, "Client", _C)
    return _C


def test_only_formats_the_renderer_can_embed_survive(monkeypatch):
    payload = {"result_count": 4, "results": [
        {"id": "a", "title": "png", "url": "https://x.test/a.png",
         "license": "by", "license_version": "4.0", "creator": "Ann",
         "width": 800, "height": 600, "foreign_landing_url": "https://x.test/a"},
        {"id": "b", "title": "gif", "url": "https://x.test/b.gif",
         "license": "by", "creator": "Bob", "width": 10, "height": 10},
        {"id": "c", "title": "webp", "url": "https://x.test/c.webp",
         "license": "by", "creator": "Cid", "width": 10, "height": 10},
        {"id": "d", "title": "jpg", "url": "https://x.test/d.JPG",
         "license": "by-nc", "creator": "Dee", "width": 40, "height": 40,
         "foreign_landing_url": "https://x.test/d"},
    ]}
    _fake_client(monkeypatch, payload)
    out = w.search_images(query="aqueduct", max_results=8)
    assert out["ok"] is True
    ids = [r["title"] for r in out["results"]]
    assert ids == ["png", "jpg"], f"GIF/WebP leaked through: {ids}"


def test_the_licence_and_creator_are_never_dropped(monkeypatch):
    _fake_client(monkeypatch, {"result_count": 1, "results": [{
        "id": "a", "title": "A bridge", "url": "https://x.test/a.jpg",
        "license": "by-sa", "license_version": "2.0", "creator": "Ann Thorpe",
        "license_url": "https://creaivecommons.org/licenses/by-sa/2.0/",
        "foreign_landing_url": "https://flickr.com/photos/1",
        "source": "flickr", "width": 1024, "height": 768,
        "attribution": "A bridge by Ann Thorpe, licensed CC BY-SA 2.0",
    }]})
    r = w.search_images(query="bridge")["results"][0]
    assert r["license"] == "BY-SA 2.0", r["license"]
    assert r["creator"] == "Ann Thorpe"
    assert r["page_url"] == "https://flickr.com/photos/1"
    assert r["attribution"].startswith("A bridge")


def test_image_search_uses_its_own_generous_timeout(monkeypatch):
    """Measured: an anonymous Openverse query answered in 12.8s. The shared text
    search budget is 6s, so the call returned a ReadTimeout and the author read
    that as 'there are no images' — a timeout configured for a forty-per-run text
    call, silently disabling a once-per-document one."""
    C = _fake_client(monkeypatch, {"result_count": 0, "results": []})
    w.search_images(query="nothing here")
    assert C.last["timeout"] == w.IMAGE_TIMEOUT_S
    assert w.IMAGE_TIMEOUT_S >= 20, w.IMAGE_TIMEOUT_S


def test_rate_limit_says_so_instead_of_returning_empty(monkeypatch):
    """429 anonymous throttling is the normal failure for this API. An empty
    result set and a throttled one must not look the same to the caller."""
    _fake_client(monkeypatch, {}, status=429)
    out = w.search_images(query="aqueduct")
    assert out["ok"] is False
    assert "rate limit" in out["error"].lower(), out


def test_commercial_only_is_passed_as_a_licence_filter(monkeypatch):
    C = _fake_client(monkeypatch, {"result_count": 0, "results": []})
    w.search_images(query="aqueduct", commercial_only=True)
    assert C.last["params"].get("license_type") == "commercial,modification"
    w.search_images(query="aqueduct", commercial_only=False)
    assert "license_type" not in C.last["params"]


def test_a_blank_query_is_refused_before_the_network(monkeypatch):
    out = w.search_images(query="   ")
    assert out["ok"] is False and "required" in out["error"]
