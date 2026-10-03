"""Fundamental suite for the whatsapp_twilio adaptor (offline, no servers).

Run: uv run pytest adaptors/whatsapp_twilio -q
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import _adaptor_kit as kit
from adaptors.base import NotConfigured
from adaptors.whatsapp_twilio.adapter import WhatsAppTwilioAdaptor

KEYS = ["TWILIO_SID", "TWILIO_AUTH", "WHATSAPP_FROM"]
SAMPLE = {
    "From": "whatsapp:+1555000111", "To": "whatsapp:+1555000222",
    "Body": "hello wa", "MessageSid": "SM2", "ContentSid": "",
}


@pytest.fixture(autouse=True)
def _unconfigured(monkeypatch):
    kit.clear_keys(monkeypatch, KEYS)


def test_contract():
    a = WhatsAppTwilioAdaptor()
    kit.assert_contract(
        a, name="whatsapp_twilio", required_keys=KEYS,
        capabilities={"send_text": True, "send_media": True,
                      "receive": True})
    assert a.cost_hint_usd() == 0.01


def test_wa_prefixing():
    assert WhatsAppTwilioAdaptor._wa("+1555") == "whatsapp:+1555"
    assert WhatsAppTwilioAdaptor._wa("whatsapp:+1555") == "whatsapp:+1555"


def test_unconfigured():
    a = WhatsAppTwilioAdaptor()
    assert not a.is_configured()
    with pytest.raises(NotConfigured):
        a.send(to="+1555000222", text="hi")


def test_health():
    assert kit.assert_healthy(WhatsAppTwilioAdaptor())["configured"] is False


def test_normalize_sample():
    m = WhatsAppTwilioAdaptor().normalize(SAMPLE)
    assert m.channel == "whatsapp_twilio"
    assert m.sender_id == "+1555000111"  # prefix stripped
    assert m.text == "hello wa" and m.msg_id == "SM2"


def test_verify_webhook_without_secret():
    assert WhatsAppTwilioAdaptor().verify_webhook({}, b"{}") is False


def test_send_success(monkeypatch):
    kit.set_keys(monkeypatch, {"TWILIO_SID": "sid", "TWILIO_AUTH": "auth",
                               "WHATSAPP_FROM": "+1555000999"})
    calls = kit.fake_post_factory(
        monkeypatch, lambda url, **kw: kit.FakeResp(200, {"sid": "SM9"}))
    r = WhatsAppTwilioAdaptor().send(to="+1555000222", text="hi")
    assert r.ok and r.msg_id == "SM9"
    assert calls[0]["data"]["From"] == "whatsapp:+1555000999"
    assert calls[0]["data"]["To"] == "whatsapp:+1555000222"
