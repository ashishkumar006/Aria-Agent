"""Agent voice channel: proxy contracts for TTS, STT, voices and status."""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_server  # noqa: E402
from agent_server import app  # noqa: E402


def _client():
    return TestClient(app)


def test_voice_status_proxies_the_gateway(monkeypatch):
    async def fake(path, **kwargs):
        assert path == "/v1/voice/status"
        return {"tts": {"available": True}, "stt": {"available": True},
                "voices": [{"id": "af_heart", "label": "Heart"}]}

    monkeypatch.setattr(agent_server, "_gw_json", fake)
    r = _client().get("/api/voice/status")
    assert r.status_code == 200
    assert r.json()["tts"]["available"] is True


def test_voice_status_degrades_when_gateway_is_down(monkeypatch):
    async def down(path, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(agent_server, "_gw_json", down)
    r = _client().get("/api/voice/status")
    assert r.status_code == 200
    body = r.json()
    assert body["tts"]["available"] is False
    assert "gateway unreachable" in body["tts"]["error"]


def test_tts_validates_text_and_returns_wav(monkeypatch):
    with _client() as client:
        assert client.post("/api/tts", json={"text": ""}).status_code == 400
        assert client.post("/api/tts", json={"text": 3}).status_code == 400
        assert client.post("/api/tts", json={"text": "x" * 8001}).status_code == 413

    async def fake_bytes(path, **kwargs):
        assert path == "/v1/tts"
        assert kwargs["json"]["voice"] == "af_heart"
        return 200, "audio/wav", b"RIFF-fake-wav"

    monkeypatch.setattr(agent_server, "_gw_bytes", fake_bytes)
    with _client() as client:
        r = client.post("/api/tts", json={"text": "hello"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/wav")
    assert r.content == b"RIFF-fake-wav"


def test_voices_fall_back_to_the_curated_list(monkeypatch):
    async def down(path, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(agent_server, "_gw_json", down)
    with _client() as client:
        r = client.get("/api/tts/voices")
    assert r.status_code == 200
    voices = r.json()["voices"]
    assert any(v["id"] == "af_heart" for v in voices)


def test_stt_rejects_missing_and_empty_uploads():
    with _client() as client:
        assert client.post("/api/stt").status_code == 400
        r = client.post("/api/stt", files={"file": ("clip.webm", b"")})
    assert r.status_code == 400
    assert "empty audio" in r.json()["error"]
