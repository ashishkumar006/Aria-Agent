"""Fundamental suite for the webhook adaptor (offline, no servers).

Run: uv run pytest adaptors/webhook -q
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import _adaptor_kit as kit
from adaptors.base import NotConfigured
from adaptors.envelope import TrustLevel
from adaptors.webhook.adapter import GenericWebhookAdaptor

SAMPLE = {
    "sender_id": "ext-1", "chat_id": "chat-9", "text": "hello hook",
    "message": "", "msg_id": "w1", "id": "", "ts": 1700000000.0,
    "to": "", "sender": "",
}


def test_contract():
    kit.assert_contract(
        GenericWebhookAdaptor(), name="webhook", required_keys=[],
        capabilities={"send_text": True, "receive": True})


def test_no_keys_needed():
    assert GenericWebhookAdaptor().is_configured()


def test_health_reflects_out_url(monkeypatch):
    # No keys exist, but sending needs WEBHOOK_OUT_URL — health says so.
    monkeypatch.delenv("WEBHOOK_OUT_URL", raising=False)
    assert kit.assert_healthy(GenericWebhookAdaptor())["configured"] is False
    monkeypatch.setenv("WEBHOOK_OUT_URL", "https://hooks.example/in")
    assert kit.assert_healthy(GenericWebhookAdaptor())["configured"] is True


def test_normalize_sample():
    m = GenericWebhookAdaptor().normalize(SAMPLE)
    assert m.channel == "webhook"
    assert m.sender_id == "ext-1" and m.chat_id == "chat-9"
    assert m.text == "hello hook" and m.msg_id == "w1"


def test_verify_webhook_without_secret(monkeypatch):
    # Open endpoint BY DESIGN (see verify.py): without WEBHOOK_SECRET
    # everything is accepted but arrives UNTRUSTED — trust rides the
    # envelope's trust_level, never the signature.
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    assert GenericWebhookAdaptor().verify_webhook({}, b"{}") is True
    m = GenericWebhookAdaptor().normalize(SAMPLE)
    assert m.trust_level == TrustLevel.untrusted


def test_send_needs_out_url(monkeypatch):
    monkeypatch.delenv("WEBHOOK_OUT_URL", raising=False)
    with pytest.raises(NotConfigured):
        GenericWebhookAdaptor().send(to="x", text="hi")


def test_send_success(monkeypatch):
    monkeypatch.setenv("WEBHOOK_OUT_URL", "https://hooks.example/in")
    calls = kit.fake_post_factory(
        monkeypatch, lambda url, **kw: kit.FakeResp(200, {}))
    r = GenericWebhookAdaptor().send(to="x", text="hi")
    assert r.ok and r.channel == "webhook"
    assert calls[0]["url"] == "https://hooks.example/in"
    assert calls[0]["json"]["text"] == "hi"
