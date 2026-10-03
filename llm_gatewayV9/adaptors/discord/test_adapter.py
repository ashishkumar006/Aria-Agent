"""Fundamental suite for the discord adaptor (offline, no servers).

Run: uv run pytest adaptors/discord -q
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import _adaptor_kit as kit
from adaptors.base import NotConfigured
from adaptors.discord.adapter import DiscordAdaptor

KEYS = ["DISCORD_BOT_TOKEN"]
SAMPLE = {
    "id": "123", "channel_id": "456", "guild_id": "789",
    "content": "hello discord", "timestamp": "2024-01-01T00:00:00Z",
    "author": {"id": "111", "username": "rohan", "bot": False},
    "mentions": [],
    "message_reference": {"message_id": "", "channel_id": ""},
}


@pytest.fixture(autouse=True)
def _unconfigured(monkeypatch):
    kit.clear_keys(monkeypatch, KEYS)


def test_contract():
    kit.assert_contract(
        DiscordAdaptor(), name="discord", required_keys=KEYS,
        capabilities={"send_text": True, "send_media": True,
                      "receive": True, "threads": True, "reactions": True})


def test_unconfigured():
    a = DiscordAdaptor()
    assert not a.is_configured()
    with pytest.raises(NotConfigured):
        a.send(to="456", text="hi")


def test_health():
    assert kit.assert_healthy(DiscordAdaptor())["configured"] is False


def test_normalize_sample():
    m = DiscordAdaptor().normalize(SAMPLE)
    assert m.channel == "discord"
    assert m.sender_id == "111" and m.chat_id == "456"
    assert m.text == "hello discord" and m.msg_id == "123"
    assert m.thread_id is None


def test_snowflake_ts():
    assert DiscordAdaptor._snowflake_ts("123") > 0
    assert DiscordAdaptor._snowflake_ts("nope") == 0.0


def test_send_success(monkeypatch):
    kit.set_keys(monkeypatch, {"DISCORD_BOT_TOKEN": "tok"})
    calls = kit.fake_post_factory(
        monkeypatch, lambda url, **kw: kit.FakeResp(200, {"id": "999"}))
    r = DiscordAdaptor().send(to="456", text="hi", thread_id="111")
    assert r.ok and r.msg_id == "999"
    assert "discord.com/api/v10/channels/456/messages" in calls[0]["url"]
    assert calls[0]["json"]["message_reference"] == {"message_id": "111"}


def test_send_provider_error(monkeypatch):
    kit.set_keys(monkeypatch, {"DISCORD_BOT_TOKEN": "tok"})
    kit.fake_post_factory(
        monkeypatch,
        lambda url, **kw: kit.FakeResp(403, {"message": "forbidden"}))
    with pytest.raises(RuntimeError, match="discord error 403"):
        DiscordAdaptor().send(to="456", text="hi")
