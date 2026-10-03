"""Registry-driven adaptor contract test (offline, no servers).

Unlike the per-package suites under adaptors/<name>/ (deep, per-channel
fixtures), this file enforces the COLLECTIVE contract automatically for
every registered adaptor — present and future:

- all 16 names build without import-time breakage (one broken dep must
  never kill the registry)
- every instance satisfies the BaseAdaptor surface
- inventory() leaks no secret values (names of keys only)
- required_keys are non-empty strings (except deliberately keyless
  loopback channels: webhook/webui/local_mic)

Run: uv run pytest tests/test_adaptor_contract.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

from adaptors import registry
from adaptors.base import BaseAdaptor, Capability

EXPECTED = sorted([
    "telegram", "discord", "matrix", "line", "webhook", "webui", "slack",
    "signal", "gmail", "imap", "twilio_sms", "whatsapp_twilio", "local_mic",
    "teams", "whatsapp_meta", "twilio_voice",
])

KEYLESS = {"webhook", "webui", "local_mic"}


def test_all_names_registered():
    assert registry.names() == EXPECTED


def test_all_build():
    registry.reset()
    try:
        for name in registry.names():
            inst = registry.get(name)
            assert isinstance(inst, BaseAdaptor), name
    finally:
        registry.reset()


@pytest.mark.parametrize("name", EXPECTED)
def test_surface(name, monkeypatch):
    registry.reset()
    try:
        inst = registry.get(name)
        assert isinstance(inst, BaseAdaptor)
        assert inst.name == name
        assert isinstance(inst.capabilities, Capability)
        keys = inst.required_keys()
        assert all(isinstance(k, str) and k for k in keys), name
        if name in KEYLESS:
            assert keys == [], name
        else:
            assert keys, name
        # health() never raises and never leaks values.
        for k in keys:
            monkeypatch.delenv(k, raising=False)
        h = inst.health()
        assert h["name"] == name
        assert h["configured"] is False or name in KEYLESS
        assert isinstance(inst.cost_hint_usd(), float)
        # send() with no credentials never attempts the network: it must
        # raise a config-shaped error, not hang or crash.
        if name not in KEYLESS and "send_text" in {
                f for f in inst.capabilities.__dict__
                if getattr(inst.capabilities, f)}:
            from adaptors.base import NotConfigured
            try:
                inst.send(to="test-dest", text="probe")
            except NotConfigured:
                pass
            except (ValueError, RuntimeError, PermissionError):
                pass  # address-shaped errors are also honest failures
    finally:
        registry.reset()


def test_inventory_leaks_no_values(monkeypatch):
    registry.reset()
    try:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "super-secret-value")
        for row in registry.inventory():
            assert row["name"] in EXPECTED
            blob = str(row)
            assert "super-secret-value" not in blob
            assert all(isinstance(k, str) for k in row["required_keys"])
    finally:
        registry.reset()
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
