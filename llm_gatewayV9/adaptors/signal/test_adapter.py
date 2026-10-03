"""Fundamental suite for the signal adaptor (offline, no servers).

signal-cli is a subprocess seam: send/poll shell out, so success paths
fake subprocess.run and failure paths assert the mapped errors.

Run: uv run pytest adaptors/signal -q
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import _adaptor_kit as kit
from adaptors.base import NotConfigured
from adaptors.signal.adapter import SignalAdaptor

KEYS = ["SIGNAL_NUMBER"]
SAMPLE = {
    "method": "receive",
    "envelope": {
        "source": "+1555000111", "sourceUuid": "uuid-1",
        "timestamp": 1700000000000,
        "dataMessage": {"message": "hello signal",
                        "groupInfo": {"groupId": ""}},
    },
}


@pytest.fixture(autouse=True)
def _unconfigured(monkeypatch):
    kit.clear_keys(monkeypatch, KEYS)


def _completed(rc=0, out="", err=""):
    return subprocess.CompletedProcess(args=["signal-cli"], returncode=rc,
                                       stdout=out, stderr=err)


def test_contract():
    kit.assert_contract(
        SignalAdaptor(), name="signal", required_keys=KEYS,
        capabilities={"send_text": True, "send_media": True,
                      "receive": True})


def test_unconfigured():
    a = SignalAdaptor()
    assert not a.is_configured()
    with pytest.raises(NotConfigured):
        a.send(to="+1555000222", text="hi")


def test_health():
    assert kit.assert_healthy(SignalAdaptor())["configured"] is False


def test_normalize_sample():
    m = SignalAdaptor().normalize(SAMPLE)
    assert m.channel == "signal"
    assert m.sender_id == "+1555000111"
    assert m.text == "hello signal" and m.msg_id == "1700000000000"


def test_send_success(monkeypatch):
    kit.set_keys(monkeypatch, {"SIGNAL_NUMBER": "+1555000111"})
    seen = {}
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: seen.update(cmd=cmd) or _completed())
    r = SignalAdaptor().send(to="+1555000222", text="hi")
    assert r.ok and r.channel == "signal"
    assert seen["cmd"][:4] == ["signal-cli", "-u", "+1555000111", "send"]
    assert "+1555000222" in seen["cmd"]


def test_send_cli_error(monkeypatch):
    kit.set_keys(monkeypatch, {"SIGNAL_NUMBER": "+1555000111"})
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: _completed(rc=1, err="nope"))
    with pytest.raises(RuntimeError, match="signal error"):
        SignalAdaptor().send(to="+1555000222", text="hi")


def test_poll_parses_json_lines(monkeypatch):
    kit.set_keys(monkeypatch, {"SIGNAL_NUMBER": "+1555000111"})
    line = ('{"envelope":{"source":"+1","sourceUuid":"u","timestamp":1,'
            '"dataMessage":{"message":"m","groupInfo":{"groupId":""}}}}')
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: _completed(out="noise\n" + line + "\n"))
    out = SignalAdaptor().poll()
    assert len(out) == 1 and out[0]["envelope"]["source"] == "+1"
