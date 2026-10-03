"""Fundamental suite for the telegram adaptor (offline, no servers).

Run: uv run pytest adaptors/telegram -q
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import _adaptor_kit as kit
from adaptors.base import NotConfigured
from adaptors.envelope import TrustLevel
from adaptors.telegram.adapter import TelegramAdaptor

KEYS = ["TELEGRAM_BOT_TOKEN"]
SAMPLE = {
    "update_id": 1,
    "message": {
        "message_id": 5, "date": 1700000000,
        "from": {"id": 111, "username": "rohan"},
        "chat": {"id": 111, "type": "private"},
        "text": "hello",
    },
}


@pytest.fixture(autouse=True)
def _unconfigured(monkeypatch):
    kit.clear_keys(monkeypatch, KEYS)


def test_contract():
    kit.assert_contract(
        TelegramAdaptor(), name="telegram", required_keys=KEYS,
        capabilities={"send_text": True, "send_media": True,
                      "receive": True})


def test_unconfigured(monkeypatch):
    a = TelegramAdaptor()
    assert not a.is_configured()
    with pytest.raises(NotConfigured):
        a.send(to="111", text="hi")


def test_health():
    h = kit.assert_healthy(TelegramAdaptor())
    assert h["configured"] is False


def test_normalize_sample():
    m = TelegramAdaptor().normalize(SAMPLE)
    assert m.channel == "telegram"
    assert m.sender_id == "111" and m.chat_id == "111"
    assert m.text == "hello" and m.msg_id == "5"
    assert m.trust_level == TrustLevel.untrusted  # unpaired sender


def test_normalize_message_less_update():
    m = TelegramAdaptor().normalize({"update_id": 9})
    assert m.channel == "telegram" and m.text == ""
    assert m.trust_level == TrustLevel.untrusted


def test_verify_webhook_default_false():
    assert TelegramAdaptor().verify_webhook({}, b"{}") is False


def test_send_success(monkeypatch):
    kit.set_keys(monkeypatch, {"TELEGRAM_BOT_TOKEN": "tok"})
    calls = kit.fake_post_factory(
        monkeypatch,
        lambda url, **kw: kit.FakeResp(
            200, {"ok": True, "result": {"message_id": 42}}))
    r = TelegramAdaptor().send(to="111", text="hi")
    assert r.ok and r.channel == "telegram" and r.msg_id == "42"
    assert "api.telegram.org" in calls[0]["url"]
    # NOTE: Telegram embeds the token in the request path by API design;
    # the adaptor never LOGS the URL (transport errors name only the
    # exception type) — that is the no-leak guarantee being tested.
    assert calls[0]["json"] == {"chat_id": "111", "text": "hi"}


def test_send_provider_error(monkeypatch):
    kit.set_keys(monkeypatch, {"TELEGRAM_BOT_TOKEN": "tok"})
    kit.fake_post_factory(
        monkeypatch,
        lambda url, **kw: kit.FakeResp(
            200, {"ok": False, "description": "blocked"}))
    with pytest.raises(RuntimeError, match="telegram error"):
        TelegramAdaptor().send(to="111", text="hi")
