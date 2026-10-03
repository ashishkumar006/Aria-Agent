"""Fundamental suite for the line adaptor (offline, no servers).

Run: uv run pytest adaptors/line -q
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import _adaptor_kit as kit
from adaptors.base import NotConfigured
from adaptors.envelope import TrustLevel
from adaptors.line.adapter import LineAdaptor

KEYS = ["LINE_CHANNEL_SECRET", "LINE_ACCESS_TOKEN"]
SAMPLE = {
    "destination": "dest",
    "events": [{
        "type": "message", "replyToken": "tok",
        "source": {"type": "user", "userId": "U111",
                   "groupId": "", "roomId": ""},
        "message": {"id": "m1", "type": "text", "text": "hello line"},
        "timestamp": 1700000000000, "webhookEventId": "ev1",
    }],
}


@pytest.fixture(autouse=True)
def _unconfigured(monkeypatch):
    kit.clear_keys(monkeypatch, KEYS)


def test_contract():
    kit.assert_contract(
        LineAdaptor(), name="line", required_keys=KEYS,
        capabilities={"send_text": True, "send_media": True,
                      "receive": True})


def test_unconfigured():
    a = LineAdaptor()
    assert not a.is_configured()
    with pytest.raises(NotConfigured):
        a.send(to="U111", text="hi")


def test_health():
    assert kit.assert_healthy(LineAdaptor())["configured"] is False


def test_normalize_sample():
    m = LineAdaptor().normalize(SAMPLE)
    assert m.channel == "line"
    assert m.sender_id == "U111" and m.chat_id == "U111"
    assert m.text == "hello line" and m.msg_id == "ev1"


def test_normalize_empty_events():
    m = LineAdaptor().normalize({"destination": "d", "events": []})
    assert m.channel == "line" and m.text == ""
    assert m.trust_level == TrustLevel.untrusted


def test_verify_webhook_without_secret():
    assert LineAdaptor().verify_webhook({}, b"{}") is False


def test_send_push_success(monkeypatch):
    kit.set_keys(monkeypatch, {"LINE_CHANNEL_SECRET": "s",
                               "LINE_ACCESS_TOKEN": "tok"})
    calls = kit.fake_post_factory(
        monkeypatch, lambda url, **kw: kit.FakeResp(200, {}))
    r = LineAdaptor().send(to="U111", text="hi")
    assert r.ok and r.channel == "line"
    assert calls[0]["url"].endswith("/message/push")
    assert calls[0]["json"]["to"] == "U111"


def test_send_reply_token_variant(monkeypatch):
    kit.set_keys(monkeypatch, {"LINE_CHANNEL_SECRET": "s",
                               "LINE_ACCESS_TOKEN": "tok"})
    calls = kit.fake_post_factory(
        monkeypatch, lambda url, **kw: kit.FakeResp(200, {}))
    LineAdaptor().send(to="U111", text="hi", thread_id="reply:tok123")
    assert calls[0]["url"].endswith("/message/reply")
    assert calls[0]["json"]["replyToken"] == "tok123"
