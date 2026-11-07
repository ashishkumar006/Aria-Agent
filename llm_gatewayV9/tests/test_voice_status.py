"""Voice channel status and voice-list contracts (offline)."""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import voice_api  # noqa: E402


def _client():
    app = FastAPI()
    app.include_router(voice_api.router)
    return TestClient(app)


def test_voice_status_reports_models_and_dependencies(tmp_path, monkeypatch):
    model = tmp_path / "kokoro-v1.0.onnx"
    voices = tmp_path / "voices-v1.0.bin"
    model.write_bytes(b"model")
    voices.write_bytes(b"voices")
    monkeypatch.setattr(voice_api, "_model_path",
                        lambda env, name: tmp_path / name)
    r = _client().get("/v1/voice/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) >= {"tts", "stt", "voices"}
    assert body["tts"]["available"] is True
    assert body["tts"]["model"].endswith("kokoro-v1.0.onnx")
    assert body["stt"]["available"] is True
    assert body["voices"]


def test_voice_status_names_missing_model_files(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_api, "_model_path",
                        lambda env, name: tmp_path / name)
    r = _client().get("/v1/voice/status")
    assert r.status_code == 200
    body = r.json()
    assert body["tts"]["available"] is False
    assert "model missing" in body["tts"]["error"]


def test_tts_text_drops_emojis_and_flags():
    assert voice_api._speech_text("Hello 😀 world 🚀") == "Hello world"
    assert voice_api._speech_text("🇮🇳 flag") == "flag"
    assert voice_api._speech_text("❤️") == ""
    assert voice_api._speech_text("normal text") == "normal text"


def test_tts_rejects_emoji_only_text_without_loading_the_model():
    r = _client().post("/v1/tts", json={"text": "😀🚀❤️"})
    assert r.status_code == 400
    assert "speakable" in r.text


def test_voices_endpoint_never_loads_the_model(monkeypatch):
    def _boom():
        raise AssertionError("voice list must not load Kokoro")

    monkeypatch.setattr(voice_api, "_get_kokoro", _boom)
    r = _client().get("/v1/tts/voices")
    assert r.status_code == 200
    assert len(r.json()["voices"]) >= 3
