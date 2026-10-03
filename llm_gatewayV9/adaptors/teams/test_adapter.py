"""Fundamental suite for the teams adaptor (offline, no servers).

Run: uv run pytest adaptors/teams -q
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import _adaptor_kit as kit
from adaptors.base import NotConfigured
from adaptors.envelope import TrustLevel
from adaptors.teams.adapter import TeamsAdaptor

KEYS = ["TEAMS_APP_ID", "TEAMS_APP_PASSWORD", "TEAMS_TENANT"]
SAMPLE = {
    "type": "message", "id": "act1", "text": "hello <at>bot</at> teams",
    "speak": "", "replyToId": "", "serviceUrl": "https://svc",
    "channelId": "msteams",
    "entities": [], "attachments": [], "value": {},
    "from": {"id": "user1", "name": "Rohan"},
    "recipient": {"id": "bot1", "name": "Bot"},
    "conversation": {"id": "conv1", "tenantId": "ten1"},
}


@pytest.fixture(autouse=True)
def _unconfigured(monkeypatch):
    kit.clear_keys(monkeypatch, KEYS)


def _responder(url, **kw):
    if "login.microsoftonline" in url:
        return kit.FakeResp(200, {"access_token": "tok", "expires_in": 3600})
    return kit.FakeResp(200, {"id": "act9"})


def test_contract():
    kit.assert_contract(
        TeamsAdaptor(), name="teams", required_keys=KEYS,
        capabilities={"send_text": True, "send_media": False,
                      "receive": True, "threads": True,
                      "needs_public_url": True})


def test_unconfigured():
    a = TeamsAdaptor()
    assert not a.is_configured()
    with pytest.raises(NotConfigured):
        a._token()


def test_health():
    assert kit.assert_healthy(TeamsAdaptor())["configured"] is False


def test_normalize_sample():
    m = TeamsAdaptor().normalize(SAMPLE)
    assert m.channel == "teams"
    assert m.sender_id == "user1" and m.chat_id == "conv1"
    assert m.text == "hello  teams"  # mentions stripped
    assert m.thread_id is None


def test_normalize_non_message():
    m = TeamsAdaptor().normalize({**SAMPLE, "type": "typing"})
    assert m.text == "" and m.trust_level == TrustLevel.untrusted


def test_send_needs_service_url():
    with pytest.raises(ValueError, match="service_url"):
        TeamsAdaptor().send(to="conv1", text="hi")


def test_send_success(monkeypatch):
    kit.set_keys(monkeypatch, {"TEAMS_APP_ID": "id", "TEAMS_APP_PASSWORD": "pw",
                               "TEAMS_TENANT": "ten"})
    calls = kit.fake_post_factory(monkeypatch, _responder)
    r = TeamsAdaptor().send(to="conv1", text="hi",
                            service_url="https://svc")
    assert r.ok and r.msg_id == "act9"
    urls = [c["url"] for c in calls]
    assert any("login.microsoftonline" in u for u in urls)
    assert any(u.startswith("https://svc/v3/conversations/conv1") for u in urls)
