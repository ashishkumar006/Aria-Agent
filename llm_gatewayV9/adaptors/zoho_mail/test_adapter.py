"""Zoho Mail adaptor tests (offline). Run: uv run pytest adaptors/zoho_mail -q"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import _adaptor_kit as kit
from adaptors.zoho_mail import ZohoMailAdaptor


def test_required_keys():
    a = ZohoMailAdaptor()
    assert a.required_keys() == ["ZOHO_TOKEN"]
    assert a.name == "zoho_mail"


def test_unconfigured_send_raises_not_configured(monkeypatch):
    kit.clear_keys(monkeypatch, ["ZOHO_TOKEN", "ZOHO_REFRESH_TOKEN",
                                 "ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET",
                                 "ZOHO_DC", "ZOHO_USER", "ZOHO_ACCOUNT_ID"])
    a = ZohoMailAdaptor()
    with pytest.raises(kit.NotConfigured):
        a.send(to="a@b.c", text="hi")


def test_normalize_row():
    a = ZohoMailAdaptor()
    msg = a.normalize({"messageId": "m1", "fromAddress": "a@b.c",
                       "subject": "Hi", "snippet": "body"})
    assert msg.channel == "zoho_mail"
    assert msg.sender_id == "a@b.c" and msg.chat_id == "a@b.c"
    assert msg.msg_id == "m1" and msg.text == "body"