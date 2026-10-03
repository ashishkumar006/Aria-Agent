"""Shared offline test kit for channel-adaptor fundamental suites.

Every ``adaptors/<name>/test_adapter.py`` uses this: no servers, no
network, no credentials. Outbound HTTP is faked at the single seam all
adaptors share (``adaptors.http.post``); stdlib transports (smtplib,
subprocess) are patched per-adaptor in its own file.

Run one adaptor:  uv run pytest adaptors/<name> -q
Run all colocated suites (+ the registry contract test):
  uv run pytest adaptors tests/test_adaptor_contract.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adaptors.base import BaseAdaptor, Capability, NotConfigured  # noqa: E402


class FakeResp:
    """Minimal stand-in for the httpx.Response surface adaptors use."""

    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def clear_keys(monkeypatch, keys: list[str]) -> None:
    """Ensure the adaptor sees NO credentials (unconfigured path)."""
    for k in keys:
        monkeypatch.delenv(k, raising=False)


def set_keys(monkeypatch, mapping: dict[str, str]) -> None:
    for k, v in mapping.items():
        monkeypatch.setenv(k, v)


def fake_post_factory(monkeypatch, responder) -> list[dict]:
    """Patch adaptors.http.post with ``responder(url, **kw)``. Returns the
    recorded call list so tests can assert request shape (URL, auth)."""
    import adaptors.http as _http

    calls: list[dict] = []

    def _fake(url: str, **kw):
        calls.append({"url": url, **kw})
        return responder(url, **kw)

    monkeypatch.setattr(_http, "post", _fake)
    return calls


def assert_contract(adaptor: BaseAdaptor, *, name: str,
                    required_keys: list[str],
                    capabilities: dict) -> None:
    """The one interface every channel implements (adaptors/base.py)."""
    assert isinstance(adaptor, BaseAdaptor)
    assert adaptor.name == name
    assert adaptor.required_keys() == required_keys
    assert isinstance(adaptor.capabilities, Capability)
    for flag, want in capabilities.items():
        assert getattr(adaptor.capabilities, flag) is want, flag
    assert isinstance(adaptor.cost_hint_usd(), float)


def assert_healthy(adaptor: BaseAdaptor) -> dict:
    """health() is cheap, credential-free and never raises."""
    h = adaptor.health()
    assert h["name"] == adaptor.name
    assert isinstance(h["configured"], bool)
    assert isinstance(h["capabilities"], dict)
    return h
