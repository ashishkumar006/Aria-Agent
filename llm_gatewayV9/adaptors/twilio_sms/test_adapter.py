"""Fundamental suite for the twilio_sms adaptor (offline, no servers).

Run: uv run pytest adaptors/twilio_sms -q
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import _adaptor_kit as kit
from adaptors.base import NotConfigured
from adaptors.twilio_sms.adapter import TwilioSmsAdaptor

KEYS = ["TWILIO_SID", "TWILIO_AUTH", "TWILIO_FROM"]
SAMPLE = {
    "From": "+1555000111", "To": "+1555000222", "Body": "hello sms",
    "MessageSid": "SM1", "NumMedia": "0", "extra": {},
}


@pytest.fixture(autouse=True)
def _unconfigured(monkeypatch):
    kit.clear_keys(monkeypatch, KEYS)


def test_contract():
    a = TwilioSmsAdaptor()
    kit.assert_contract(
        a, name="twilio_sms", required_keys=KEYS,
        capabilities={"send_text": True, "send_media": True,
                      "receive": True})
    assert a.cost_hint_usd() == 0.008
    assert a.cost_hint_usd(media=True) == 0.02


def test_unconfigured():
    a = TwilioSmsAdaptor()
    assert not a.is_configured()
    with pytest.raises(NotConfigured):
        a.send(to="+1555000222", text="hi")


def test_health():
    assert kit.assert_healthy(TwilioSmsAdaptor())["configured"] is False


def test_normalize_sample():
    m = TwilioSmsAdaptor().normalize(SAMPLE)
    assert m.channel == "twilio_sms"
    assert m.sender_id == "+1555000111" and m.chat_id == "+1555000111"
    assert m.text == "hello sms" and m.msg_id == "SM1"


def test_verify_webhook_without_secret():
    assert TwilioSmsAdaptor().verify_webhook({}, b"{}") is False


def test_send_success(monkeypatch):
    kit.set_keys(monkeypatch, {"TWILIO_SID": "sid", "TWILIO_AUTH": "auth",
                               "TWILIO_FROM": "+1555000999"})
    calls = kit.fake_post_factory(
        monkeypatch, lambda url, **kw: kit.FakeResp(200, {"sid": "SM9"}))
    r = TwilioSmsAdaptor().send(to="+1555000222", text="hi")
    assert r.ok and r.msg_id == "SM9"
    assert "/Accounts/sid/Messages.json" in calls[0]["url"]
    assert calls[0]["data"]["From"] == "+1555000999"
    assert "Basic " in calls[0]["headers"]["Authorization"]


def test_send_provider_error(monkeypatch):
    kit.set_keys(monkeypatch, {"TWILIO_SID": "sid", "TWILIO_AUTH": "auth",
                               "TWILIO_FROM": "+1555000999"})
    kit.fake_post_factory(
        monkeypatch,
        lambda url, **kw: kit.FakeResp(400, {"message": "bad number"}))
    with pytest.raises(RuntimeError, match="twilio error 400"):
        TwilioSmsAdaptor().send(to="bad", text="hi")
