"""Fundamental suite for the imap adaptor (offline, no servers).

SMTP/IMAP are stdlib seams: smtplib.SMTP_SSL / imaplib.IMAP4_SSL are
patched at the module attribute the adaptor looks up at call time.

Run: uv run pytest adaptors/imap -q
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import _adaptor_kit as kit
from adaptors.base import NotConfigured
from adaptors.imap.adapter import ImapSmtpAdaptor

KEYS = ["IMAP_HOST", "IMAP_USER", "IMAP_PASS",
        "SMTP_HOST", "SMTP_USER", "SMTP_PASS"]
CREDS = {"IMAP_HOST": "imap.x", "IMAP_USER": "u", "IMAP_PASS": "p",
         "SMTP_HOST": "smtp.x", "SMTP_USER": "u@x", "SMTP_PASS": "p"}
SAMPLE = {
    "From": "rohan@example.com", "from_": "", "to": "me@x",
    "subject": "Hello", "Subject": "", "date": "Mon, 01 Jan 2024",
    "message_id": "<m1>", "messageId": "", "thread": "",
    "snippet": "see you soon", "ts": 0.0,
}


@pytest.fixture(autouse=True)
def _unconfigured(monkeypatch):
    kit.clear_keys(monkeypatch, KEYS)


class _FakeSMTP:
    def __init__(self, *a, **k):
        self.logins: list[tuple] = []
        self.sent: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, u, p):
        self.logins.append((u, p))

    def send_message(self, msg):
        self.sent.append(msg)


def test_contract():
    kit.assert_contract(
        ImapSmtpAdaptor(), name="imap", required_keys=KEYS,
        capabilities={"send_text": True, "send_media": True,
                      "receive": True})


def test_unconfigured():
    a = ImapSmtpAdaptor()
    assert not a.is_configured()
    with pytest.raises(NotConfigured):
        a.send(to="a@b.c", text="hi")


def test_health():
    assert kit.assert_healthy(ImapSmtpAdaptor())["configured"] is False


def test_normalize_sample():
    m = ImapSmtpAdaptor().normalize(SAMPLE)
    assert m.channel == "imap"
    assert m.sender_id == "rohan@example.com"
    assert "Hello" in m.text and m.msg_id == "<m1>"


def test_normalize_poll_shape():
    # poll() emits hyphenated RFC header names — normalize accepts them.
    m = ImapSmtpAdaptor().normalize({"message-id": "<m2>",
                                     "from": "a@b.c", "to": "me",
                                     "subject": "S", "date": "",
                                     "snippet": "body"})
    assert m.msg_id == "<m2>"


def test_send_success(monkeypatch, tmp_path):
    import smtplib

    kit.set_keys(monkeypatch, CREDS)
    box: dict = {}

    class _SMTP(_FakeSMTP):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            box["inst"] = self

    monkeypatch.setattr(smtplib, "SMTP_SSL", _SMTP)
    r = ImapSmtpAdaptor().send(to="a@b.c", text="hi", subject="Subj")
    assert r.ok and r.channel == "imap"
    inst = box["inst"]
    assert inst.logins == [("u@x", "p")]
    assert len(inst.sent) == 1
    assert inst.sent[0]["Subject"] == "Subj"


def test_oversize_attachment_skipped_loudly(monkeypatch, tmp_path):
    """Attachments over the cap are skipped AND named — never silently
    truncated (the old code sent the first 5MB without a word)."""
    import smtplib

    import adaptors.imap.adapter as imap_mod

    kit.set_keys(monkeypatch, CREDS)
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 100)
    monkeypatch.setattr(imap_mod, "MAX_ATTACH_BYTES", 10)
    box: dict = {}

    class _SMTP(_FakeSMTP):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            box["inst"] = self

    monkeypatch.setattr(smtplib, "SMTP_SSL", _SMTP)
    r = ImapSmtpAdaptor().send(
        to="a@b.c", text="hi", media=[{"path": str(big)}])
    assert r.ok
    assert "warning" in (r.raw or {})
    assert "big.bin" in r.raw["warning"]
    # The mail still went out (body intact), minus the attachment.
    assert len(box["inst"].sent) == 1
