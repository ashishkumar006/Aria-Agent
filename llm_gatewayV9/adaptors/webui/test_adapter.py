"""Fundamental suite for the webui adaptor (offline, no servers).

No credentials, no transport: delivery rides the agent's SSE stream and
the gateway only records the reply for the ledger.

Run: uv run pytest adaptors/webui -q
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import _adaptor_kit as kit
from adaptors.envelope import TrustLevel
from adaptors.webui.adapter import WebUIAdaptor


def test_contract():
    kit.assert_contract(
        WebUIAdaptor(), name="webui", required_keys=[],
        capabilities={"send_text": True, "send_media": True,
                      "receive": True, "threads": True})


def test_health():
    assert kit.assert_healthy(WebUIAdaptor())["configured"] is True


def test_normalize_local_is_owner():
    m = WebUIAdaptor().normalize({
        "sender_id": "op", "conversation_id": "c1", "chat_id": "",
        "text": "hello ui", "query": "", "msg_id": "m1",
        "ts": 0.0, "local": True})
    assert m.channel == "webui" and m.text == "hello ui"
    assert m.chat_id == "c1" and m.trust_level == TrustLevel.owner


def test_normalize_remote_is_paired():
    m = WebUIAdaptor().normalize({
        "sender_id": "dev", "conversation_id": "", "chat_id": "c9",
        "text": "", "query": "status?", "msg_id": "m2",
        "ts": 0.0, "local": False})
    assert m.text == "status?" and m.trust_level == TrustLevel.paired


def test_send_is_ledger_record():
    r = WebUIAdaptor().send(to="c1", text="hi")
    assert r.ok and r.channel == "webui" and r.msg_id.startswith("webui-")
