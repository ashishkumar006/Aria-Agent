"""Fundamental suite for the local_mic adaptor (offline, no servers).

Pointer adaptor: capture/TTS live in the browser + gateway voice
services; this package only normalises transcripts into envelopes.

Run: uv run pytest adaptors/local_mic -q
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import _adaptor_kit as kit
from adaptors.envelope import TrustLevel
from adaptors.local_mic.adapter import LocalMicAdaptor


def test_contract():
    kit.assert_contract(
        LocalMicAdaptor(), name="local_mic", required_keys=[],
        capabilities={"send_text": True, "send_media": True,
                      "receive": True})


def test_health():
    assert kit.assert_healthy(LocalMicAdaptor())["configured"] is True


def test_normalize_transcript():
    m = LocalMicAdaptor().normalize({
        "text": "what time is it", "sender_id": "op", "chat_id": "mic",
        "msg_id": "t1", "ts": 0.0, "lang": "en", "duration_s": 1.5})
    assert m.channel == "local_mic"
    assert m.text == "what time is it"
    assert m.trust_level == TrustLevel.owner


def test_send_is_ledger_record():
    r = LocalMicAdaptor().send(to="mic", text="hi")
    assert r.ok and r.msg_id.startswith("mic-")
    assert r.raw.get("via") == "/v1/tts"
