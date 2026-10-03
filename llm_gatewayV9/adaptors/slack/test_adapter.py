"""Fundamental suite for the slack adaptor (offline, no servers).

Run: uv run pytest adaptors/slack -q
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import _adaptor_kit as kit
from adaptors.base import NotConfigured
from adaptors.slack.adapter import SlackAdaptor

KEYS = ["SLACK_BOT_TOKEN"]
SAMPLE = {
    "type": "event_callback",
    "challenge": "",
    "event": {
        "type": "message", "user": "U111", "channel": "C222",
        "text": "hello slack", "ts": "1700000000.0001",
        "event_ts": "1700000000.0001", "thread_ts": "",
        "client_msg_id": "mid-1",
    },
}


@pytest.fixture(autouse=True)
def _unconfigured(monkeypatch):
    kit.clear_keys(monkeypatch, KEYS)


def test_contract():
    kit.assert_contract(
        SlackAdaptor(), name="slack", required_keys=KEYS,
        capabilities={"send_text": True, "send_media": False,
                      "receive": True, "threads": True, "reactions": True,
                      "needs_public_url": True})


def test_unconfigured():
    a = SlackAdaptor()
    assert not a.is_configured()
    with pytest.raises(NotConfigured):
        a.send(to="C222", text="hi")


def test_health():
    assert kit.assert_healthy(SlackAdaptor())["configured"] is False


def test_normalize_sample():
    m = SlackAdaptor().normalize(SAMPLE)
    assert m.channel == "slack"
    assert m.sender_id == "U111" and m.chat_id == "C222"
    assert m.text == "hello slack" and m.msg_id == "mid-1"


def test_verify_webhook_without_secret():
    assert SlackAdaptor().verify_webhook({}, b"{}") is False


def test_send_success(monkeypatch):
    kit.set_keys(monkeypatch, {"SLACK_BOT_TOKEN": "tok"})
    calls = kit.fake_post_factory(
        monkeypatch,
        lambda url, **kw: kit.FakeResp(200, {"ok": True, "ts": "1.2"}))
    r = SlackAdaptor().send(to="C222", text="hi", thread_id="1.1")
    assert r.ok and r.msg_id == "1.2"
    assert calls[0]["url"].endswith("chat.postMessage")
    assert calls[0]["json"]["thread_ts"] == "1.1"


def test_send_api_error(monkeypatch):
    kit.set_keys(monkeypatch, {"SLACK_BOT_TOKEN": "tok"})
    kit.fake_post_factory(
        monkeypatch,
        lambda url, **kw: kit.FakeResp(200, {"ok": False,
                                             "error": "channel_not_found"}))
    with pytest.raises(RuntimeError, match="channel_not_found"):
        SlackAdaptor().send(to="C222", text="hi")


def test_send_rotation_retry(monkeypatch, tmp_path):
    """token_expired → refresh triple renews the pair → send retried once."""
    import integrations.slack as _slack
    kit.set_keys(monkeypatch, {
        "SLACK_BOT_TOKEN": "at-old", "SLACK_REFRESH_TOKEN": "rt-old",
        "SLACK_CLIENT_ID": "cid", "SLACK_CLIENT_SECRET": "csec"})
    monkeypatch.setattr(_slack, "ENV_PATH", tmp_path / ".env")
    (tmp_path / ".env").write_text("SLACK_BOT_TOKEN=at-old\n", encoding="utf-8")

    def _route(url, **kw):
        if url.endswith("chat.postMessage"):
            auth = (kw.get("headers") or {}).get("Authorization", "")
            if "at-old" in auth:
                return kit.FakeResp(200, {"ok": False, "error": "token_expired"})
            return kit.FakeResp(200, {"ok": True, "ts": "9.9"})
        if url.endswith("oauth.v2.access"):
            return kit.FakeResp(200, {"ok": True, "access_token": "at-new",
                                      "refresh_token": "rt-new", "expires_in": 43200})
        raise AssertionError(url)

    kit.fake_post_factory(monkeypatch, _route)
    r = SlackAdaptor().send(to="C222", text="hi")
    assert r.ok and r.msg_id == "9.9"
    assert "SLACK_BOT_TOKEN=at-new" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_send_expired_no_triple(monkeypatch):
    """token_expired without a refresh triple fails loudly (no silent loop)."""
    kit.set_keys(monkeypatch, {"SLACK_BOT_TOKEN": "tok"})
    for k in ("SLACK_REFRESH_TOKEN", "SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    kit.fake_post_factory(
        monkeypatch,
        lambda url, **kw: kit.FakeResp(200, {"ok": False, "error": "token_expired"}))
    with pytest.raises(RuntimeError, match="refresh failed"):
        SlackAdaptor().send(to="C222", text="hi")
