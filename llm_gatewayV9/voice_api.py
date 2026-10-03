"""Voice services on the gateway (migrated from the agent).

POST /v1/tts (+ /v1/tts/voices) and POST /v1/stt live here so the Kokoro
model files, the Whisper model, and any voice credentials are owned by the
gateway. The agent's /api/tts + /api/stt become thin proxies.
Heavy deps (kokoro-onnx, soundfile, faster-whisper) import lazily so the
gateway boots fine without them; endpoints 503 with a clear message.
"""
from __future__ import annotations

import asyncio
import io
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

import db

router = APIRouter()

MODELS = Path(__file__).resolve().parent / "models"
_KOKORO = None
_KOKORO_LOCK = threading.Lock()
_WHISPER = None
_WHISPER_LOCK = threading.Lock()

KNOWN_VOICES = [
    {"id": "af_heart", "label": "Heart (female, warm)"},
    {"id": "af_bella", "label": "Bella (female, bright)"},
    {"id": "af_nicole", "label": "Nicole (female, soft)"},
    {"id": "am_michael", "label": "Michael (male)"},
    {"id": "am_adam", "label": "Adam (male, deep)"},
    {"id": "bf_emma", "label": "Emma (British female)"},
    {"id": "bm_george", "label": "George (British male)"},
]


def _get_kokoro():
    global _KOKORO
    if _KOKORO is None:
        with _KOKORO_LOCK:
            if _KOKORO is None:
                from kokoro_onnx import Kokoro
                model_path = Path(os.getenv("KOKORO_MODEL", MODELS / "kokoro-v1.0.onnx"))
                voices_path = Path(os.getenv("KOKORO_VOICES", MODELS / "voices-v1.0.bin"))
                if not model_path.exists() or not voices_path.exists():
                    raise FileNotFoundError(
                        f"Kokoro model files missing ({model_path}, {voices_path}). "
                        "Set KOKORO_MODEL/KOKORO_VOICES or place the files in llm_gatewayV9/models/.")
                _KOKORO = Kokoro(str(model_path), str(voices_path))
    return _KOKORO


def _get_whisper():
    global _WHISPER
    if _WHISPER is None:
        with _WHISPER_LOCK:
            if _WHISPER is None:
                from faster_whisper import WhisperModel
                _WHISPER = WhisperModel(
                    os.getenv("STT_MODEL", "base"),
                    device=os.getenv("STT_DEVICE", "cpu"),
                    compute_type=os.getenv("STT_COMPUTE", "int8"))
    return _WHISPER


async def _run_blocking(fn, *args, **kwargs):
    if hasattr(asyncio, "to_thread"):
        return await asyncio.to_thread(fn, *args, **kwargs)
    loop = asyncio.get_event_loop()
    import functools
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


@router.post("/v1/tts")
async def tts(req: Request):
    """Synthesize text -> WAV. Body: {text, voice=af_heart, speed=1.0}."""
    t0 = time.time()
    try:
        body = await req.json()
    except Exception:
        body = {}
    text = ((body.get("text") if isinstance(body, dict) else "") or "").strip()
    voice = (body.get("voice") if isinstance(body, dict) else None) or "af_heart"
    try:
        speed = float((body.get("speed") if isinstance(body, dict) else None) or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    if not text:
        return StreamingResponse(iter([b""]), media_type="audio/wav", status_code=400)
    try:
        k = _get_kokoro()
        audio, sr = await _run_blocking(k.create, text, voice, speed)
    except FileNotFoundError as e:
        return StreamingResponse(iter([str(e).encode()]), media_type="text/plain", status_code=503)
    except ImportError:
        return StreamingResponse(iter([b"kokoro-onnx not installed in the gateway venv"]),
                                 media_type="text/plain", status_code=503)
    except Exception as e:
        try:
            db.log_call(provider="voice", model="tts",
                        status="error", error=f"{type(e).__name__}: {e}"[:300],
                        latency_ms=int((time.time() - t0) * 1000),
                        prompt_chars=len(text), call_role="voice")
        except Exception:
            pass
        return StreamingResponse(iter([f"{type(e).__name__}: {e}".encode()]),
                                 media_type="text/plain", status_code=500)
    try:
        import soundfile as sf
    except ImportError:
        return StreamingResponse(iter([b"soundfile not installed in the gateway venv"]),
                                 media_type="text/plain", status_code=503)
    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV")
    buf.seek(0)
    blob = buf.read()
    try:
        db.log_call(provider="voice", model=f"tts:{voice}",
                    status="ok", latency_ms=int((time.time() - t0) * 1000),
                    prompt_chars=len(text), response_chars=len(blob),
                    call_role="voice")
    except Exception:
        pass
    return StreamingResponse(iter([blob]), media_type="audio/wav")


@router.get("/v1/tts/voices")
async def tts_voices():
    """Voice picker list: model voices when loadable, else the curated list."""
    try:
        k = _get_kokoro()
        voices = getattr(k, "voices", None)
        if isinstance(voices, (list, tuple)) and voices:
            return {"voices": [{"id": v, "label": v} for v in voices if isinstance(v, str)]}
    except Exception:
        pass
    return {"voices": KNOWN_VOICES}


@router.post("/v1/stt")
async def stt(req: Request):
    """Transcribe multipart {file} -> {text}. 25MB cap, CPU Whisper."""
    t0 = time.time()

    def _log(status: str, byte_len: int, text_len: int = 0,
             error: str | None = None) -> None:
        try:
            db.log_call(provider="voice", model="stt", status=status,
                        error=(error or "")[:300] if error else None,
                        latency_ms=int((time.time() - t0) * 1000),
                        prompt_chars=byte_len, response_chars=text_len,
                        call_role="voice")
        except Exception:
            pass

    try:
        form = await req.form()
        up = form.get("file")
        blob = await up.read() if up is not None else b""
    except Exception as e:
        err = f"unreadable upload: {type(e).__name__}: {e}"
        _log("error", 0, error=err)
        return {"error": err}
    if not blob:
        _log("error", 0, error="empty audio file")
        return {"error": "empty audio file"}
    if len(blob) > 25_000_000:
        _log("error", len(blob), error="audio too large (25MB cap)")
        return {"error": "audio too large (25MB cap)"}
    try:
        model = _get_whisper()
    except ImportError:
        return {"error": "faster-whisper not installed in the gateway venv"}
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        _log("error", len(blob), error=err)
        return {"error": err}
    import tempfile
    suffix = ".wav"
    try:
        fn = getattr(up, "filename", "") or ""
        if "." in fn:
            suffix = "." + fn.rsplit(".", 1)[-1][:4]
    except Exception:
        pass
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(blob)
        tmp = f.name
    try:
        import functools as _ft
        segs, _info = await _run_blocking(
            _ft.partial(model.transcribe, tmp, beam_size=5))
        text = " ".join(getattr(s, "text", "") for s in list(segs)).strip()
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        _log("error", len(blob), error=err)
        return {"error": err}
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass
    if not text:
        _log("error", len(blob), error="no speech detected")
        return {"error": "no speech detected"}
    _log("ok", len(blob), text_len=len(text))
    return {"text": text}
