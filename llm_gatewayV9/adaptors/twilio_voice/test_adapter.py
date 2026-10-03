"""Fundamental suite for the twilio_voice adaptor (offline, no servers).

Run: uv run pytest adaptors/twilio_voice -q
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import _adaptor_kit as kit
from adaptors.base import NotConfigured
from adaptors.twilio_voice.adapter import TwilioVoiceAdaptor

KEYS = ["TWILIO_SID", "TWILIO_AUTH", "TWILIO_VOICE_FROM", "VOICE_WS_URL"]
SAMPLE = {
    "From": "+1555000111", "Caller": "+1555000111", "To": "+1555000222",
    "CallSid": "CA1", "CallStatus": "completed", "Digits": "123",
    "TranscriptionText": "",
}


@pytest.fixture(autouse=True)
def _unconfigured(monkeypatch):
    kit.clear_keys(monkeypatch, KEYS)


def test_contract():
    a = TwilioVoiceAdaptor()
    kit.assert_contract(
        a, name="twilio_voice", required_keys=KEYS,
        capabilities={"send_text": False, "send_media": False,
                      "receive": True, "voice_calls": True,
                      "needs_public_url": True})
    assert a.cost_hint_usd() == 0.05


def test_unconfigured():
    a = TwilioVoiceAdaptor()
    assert not a.is_configured()
    with pytest.raises(NotConfigured):
        a.send(to="+1555000222", text="hi")


def test_health():
    assert kit.assert_healthy(TwilioVoiceAdaptor())["configured"] is False


def test_normalize_sample():
    m = TwilioVoiceAdaptor().normalize(SAMPLE)
    assert m.channel == "twilio_voice"
    assert m.sender_id == "+1555000111"
    assert m.text == "123" and m.msg_id == "CA1"


def test_verify_webhook_without_secret():
    assert TwilioVoiceAdaptor().verify_webhook({}, b"{}") is False


def test_send_call_success(monkeypatch):
    kit.set_keys(monkeypatch, {"TWILIO_SID": "sid", "TWILIO_AUTH": "auth",
                               "TWILIO_VOICE_FROM": "+1555000999",
                               "VOICE_WS_URL": "wss://x"})
    calls = kit.fake_post_factory(
        monkeypatch, lambda url, **kw: kit.FakeResp(200, {"sid": "CA9"}))
    r = TwilioVoiceAdaptor().send(to="+1555000222", text="hello")
    assert r.ok and r.msg_id == "CA9"
    assert calls[0]["url"].endswith("/Accounts/sid/Calls.json")
    assert "<Say>hello</Say>" in calls[0]["data"]["Twiml"]


def test_send_stream_variant(monkeypatch):
    kit.set_keys(monkeypatch, {"TWILIO_SID": "sid", "TWILIO_AUTH": "auth",
                               "TWILIO_VOICE_FROM": "+1555000999",
                               "VOICE_WS_URL": "wss://media"})
    calls = kit.fake_post_factory(
        monkeypatch, lambda url, **kw: kit.FakeResp(200, {"sid": "CA9"}))
    TwilioVoiceAdaptor().send(to="+1555000222", text="hi", stream=True)
    assert "<Stream" in calls[0]["data"]["Twiml"]
