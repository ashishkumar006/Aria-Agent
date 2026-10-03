"""Fundamental suite for the matrix adaptor (offline, no servers).

Run: uv run pytest adaptors/matrix -q
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import _adaptor_kit as kit
from adaptors.base import NotConfigured
from adaptors.matrix.adapter import MatrixAdaptor

KEYS = ["MATRIX_HOMESERVER", "MATRIX_USER", "MATRIX_PASSWORD"]
SAMPLE = {
    "event_id": "$ev1", "sender": "@rohan:example.org",
    "room_id": "!room:example.org", "origin_server_ts": 1700000000000,
    "type": "m.room.message",
    "content": {"body": "hello matrix", "msgtype": "m.text",
                "relates_to": {"event_id": "", "rel_type": ""}},
}


@pytest.fixture(autouse=True)
def _unconfigured(monkeypatch):
    kit.clear_keys(monkeypatch, KEYS)


def _responder(url, **kw):
    if url.endswith("/login"):
        return kit.FakeResp(200, {"access_token": "tok"})
    return kit.FakeResp(200, {"event_id": "$sent"})


def test_contract():
    kit.assert_contract(
        MatrixAdaptor(), name="matrix", required_keys=KEYS,
        capabilities={"send_text": True, "send_media": True,
                      "receive": True, "threads": True, "reactions": True})


def test_unconfigured():
    a = MatrixAdaptor()
    assert not a.is_configured()
    with pytest.raises(NotConfigured):
        a.send(to="!room:x", text="hi")


def test_health():
    assert kit.assert_healthy(MatrixAdaptor())["configured"] is False


def test_normalize_sample():
    m = MatrixAdaptor().normalize(SAMPLE)
    assert m.channel == "matrix"
    assert m.sender_id == "@rohan:example.org"
    assert m.chat_id == "!room:example.org"
    assert m.text == "hello matrix" and m.msg_id == "$ev1"


def test_send_login_then_send(monkeypatch):
    kit.set_keys(monkeypatch, {"MATRIX_HOMESERVER": "https://mx.example.org",
                               "MATRIX_USER": "u", "MATRIX_PASSWORD": "p"})
    calls = kit.fake_post_factory(monkeypatch, _responder)
    r = MatrixAdaptor().send(to="!room:example.org", text="hi")
    assert r.ok and r.msg_id == "$sent"
    urls = [c["url"] for c in calls]
    assert any(u.endswith("/login") for u in urls)
    assert any("send/m.room.message" in u for u in urls)


def test_send_login_failure(monkeypatch):
    kit.set_keys(monkeypatch, {"MATRIX_HOMESERVER": "https://mx.example.org",
                               "MATRIX_USER": "u", "MATRIX_PASSWORD": "p"})
    kit.fake_post_factory(
        monkeypatch, lambda url, **kw: kit.FakeResp(403, {"error": "deny"}))
    with pytest.raises(RuntimeError, match="matrix login failed"):
        MatrixAdaptor().send(to="!room:x", text="hi")
