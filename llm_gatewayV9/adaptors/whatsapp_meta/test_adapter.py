"""Fundamental suite for the whatsapp_meta adaptor (offline, no servers).

Run: uv run pytest adaptors/whatsapp_meta -q
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import _adaptor_kit as kit
from adaptors.base import NotConfigured
from adaptors.envelope import TrustLevel
from adaptors.whatsapp_meta.adapter import WhatsAppMetaAdaptor

KEYS = ["WA_TOKEN", "WA_PHONE_ID", "WA_VERIFY_TOKEN"]
SAMPLE = {
    "object": "whatsapp_business_account",
    "entry": [{
        "id": "entry1",
        "changes": [{
            "field": "messages",
            "value": {
                "messaging_product": "whatsapp",
                "messages": [{
                    "from": "+1555000111", "id": "wamid.1",
                    "timestamp": "1700000000", "type": "text",
                    "text": {"body": "hello meta"},
                }],
                "statuses": [],
            },
        }],
    }],
}


@pytest.fixture(autouse=True)
def _unconfigured(monkeypatch):
    kit.clear_keys(monkeypatch, KEYS)


def test_contract():
    kit.assert_contract(
        WhatsAppMetaAdaptor(), name="whatsapp_meta", required_keys=KEYS,
        capabilities={"send_text": True, "send_media": True,
                      "receive": True, "needs_public_url": True,
                      "needs_verification": True})


def test_unconfigured():
    a = WhatsAppMetaAdaptor()
    assert not a.is_configured()
    with pytest.raises(NotConfigured):
        a.send(to="+1555000222", text="hi")


def test_health():
    assert kit.assert_healthy(WhatsAppMetaAdaptor())["configured"] is False


def test_normalize_sample():
    m = WhatsAppMetaAdaptor().normalize(SAMPLE)
    assert m.channel == "whatsapp_meta"
    assert m.sender_id == "+1555000111"
    assert m.text == "hello meta" and m.msg_id == "wamid.1"


def test_normalize_status_only():
    payload = {"object": "whatsapp_business_account",
               "entry": [{"id": "e", "changes": [
                   {"field": "messages",
                    "value": {"messaging_product": "whatsapp",
                              "messages": [], "statuses": [{"id": "s"}]}}]}]}
    m = WhatsAppMetaAdaptor().normalize(payload)
    assert m.text == "" and m.trust_level == TrustLevel.untrusted


def test_send_success(monkeypatch):
    kit.set_keys(monkeypatch, {"WA_TOKEN": "tok", "WA_PHONE_ID": "pid",
                               "WA_VERIFY_TOKEN": "v"})
    calls = kit.fake_post_factory(
        monkeypatch,
        lambda url, **kw: kit.FakeResp(200, {"messages": [{"id": "wamid.9"}]}))
    r = WhatsAppMetaAdaptor().send(to="+1555000222", text="hi")
    assert r.ok and r.msg_id == "wamid.9"
    assert "graph.facebook.com/v21.0/pid/messages" in calls[0]["url"]
    assert calls[0]["json"]["messaging_product"] == "whatsapp"


def test_send_missing_phone_id(monkeypatch):
    kit.set_keys(monkeypatch, {"WA_TOKEN": "tok"})
    with pytest.raises(NotConfigured):
        WhatsAppMetaAdaptor().send(to="+1555000222", text="hi")
