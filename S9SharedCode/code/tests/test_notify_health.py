"""Regression: `channel_health()` must not raise for the common case.

This is the same bug class as the one-shot chat 502 fixed earlier: an
assignment that only executes on one branch, then read unconditionally. The
whole-health call (no channel argument) 500'd with
`UnboundLocalError: cannot access local variable 'rows'`, so the endpoint that
tells the operator which channels can deliver a reminder was the one endpoint
guaranteed to be broken.
"""
from __future__ import annotations

import pytest

import notify


def _stub(monkeypatch):
    monkeypatch.setattr(notify, "available_channels", lambda: [
        {"name": "telegram", "configured": True, "needs": ["T"], "target": "pairing"},
        {"name": "slack", "configured": False, "needs": ["S"], "target": "pairing"},
    ])
    monkeypatch.setattr(notify, "resolve_target", lambda ch: "999" if ch == "telegram" else "")


def test_whole_health_does_not_raise(monkeypatch):
    _stub(monkeypatch)
    out = notify.channel_health()
    assert {c["name"] for c in out["channels"]} == {"telegram", "slack"}


def test_filtered_health_returns_only_that_channel(monkeypatch):
    _stub(monkeypatch)
    out = notify.channel_health("telegram")
    assert [c["name"] for c in out["channels"]] == ["telegram"]


def test_a_configured_channel_with_no_destination_is_reported(monkeypatch):
    """The state a reminder dies in: looks alive, delivers nothing."""
    _stub(monkeypatch)
    rows = {c["name"]: c for c in notify.channel_health()["channels"]}
    assert rows["telegram"]["configured"] is True
    assert rows["telegram"]["destination"] is True
    assert rows["slack"]["configured"] is False


def test_credential_location_is_not_mislabelled(monkeypatch):
    """The gateway holds most credentials, not the agent process. Reporting
    'no credential' when the gateway has one is simply wrong."""
    _stub(monkeypatch)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    rows = {c["name"]: c for c in notify.channel_health()["channels"]}
    assert rows["telegram"]["credential_in_agent_process"] is False
    assert rows["telegram"]["credential_lives_in_gateway"] is True
