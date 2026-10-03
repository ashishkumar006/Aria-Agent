"""Fundamental suite for the gmail adaptor (offline, no servers).

Send/read delegate to integrations.gmail (single implementation); the
transport seam here is that module, patched directly.

Run: uv run pytest adaptors/gmail -q
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import _adaptor_kit as kit
from adaptors.base import NotConfigured
from adaptors.gmail.adapter import GmailAdaptor

KEYS = ["GMAIL_TOKEN"]
ROW = {
    "id": "m1", "thread_id": "t1", "threadId": "t1",
    "sender_id": "", "sender": "rohan@example.com", "From": "",
    "subject": "", "Subject": "Hello", "snippet": "see you soon",
}


@pytest.fixture(autouse=True)
def _unconfigured(monkeypatch):
    kit.clear_keys(monkeypatch, KEYS)


def test_contract():
    kit.assert_contract(
        GmailAdaptor(), name="gmail", required_keys=KEYS,
        capabilities={"send_text": True, "send_media": True,
                      "receive": True})


def test_unconfigured_send(monkeypatch):
    # No key anywhere: integrations.gmail reports unset → NotConfigured
    # (returns before any network — this test never touches the wire).
    assert not GmailAdaptor().is_configured()
    with pytest.raises(NotConfigured):
        GmailAdaptor().send(to="a@b.c", text="hi")


def test_health():
    assert kit.assert_healthy(GmailAdaptor())["configured"] is False


def test_normalize_row():
    m = GmailAdaptor().normalize(ROW)
    assert m.channel == "gmail"
    assert m.sender_id == "rohan@example.com"
    assert m.chat_id == "t1"
    assert m.text == "see you soon"  # snippet wins over subject
    assert m.msg_id == "m1"


def test_send_success(monkeypatch):
    import integrations.gmail as _g

    monkeypatch.setattr(
        _g, "send_email",
        lambda **kw: {"ok": True, "id": "m9"})
    r = GmailAdaptor().send(to="a@b.c", text="hi",
                            media=[{"subject": "Subj"}])
    assert r.ok and r.msg_id == "m9"


def test_send_carries_thread_id(monkeypatch):
    """thread_id is a real ChannelReply field (not silently dropped)."""
    import integrations.gmail as _g

    monkeypatch.setattr(
        _g, "send_email",
        lambda **kw: {"ok": True, "id": "m9"})
    r = GmailAdaptor().send(to="a@b.c", text="hi", thread_id="t1")
    assert r.thread_id == "t1"


def test_send_upstream_error(monkeypatch):
    import integrations.gmail as _g

    monkeypatch.setattr(
        _g, "send_email",
        lambda **kw: {"ok": False, "error": "boom"})
    with pytest.raises(RuntimeError, match="boom"):
        GmailAdaptor().send(to="a@b.c", text="hi")
