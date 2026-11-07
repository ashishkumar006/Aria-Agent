"""Voice services on the gateway (migrated from the agent).

POST /v1/tts (+ /v1/tts/voices) and POST /v1/stt live here so the Kokoro
model files, the Whisper model, and any voice credentials are owned by the
gateway. The agent's /api/tts + /api/stt become thin proxies.
Heavy deps (kokoro-onnx, soundfile, faster-whisper) import lazily so the
gateway boots fine without them; endpoints 503 with a clear message.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

import db

router = APIRouter()

MODELS = Path(__file__).resolve().parent / "models"
# The Kokoro weights shipped with the original agent checkout live under
# S9SharedCode/code/models. The gateway is the component that owns voice now,
# so it must be able to find them without requiring a .env override.
AGENT_MODELS = Path(__file__).resolve().parent.parent / "S9SharedCode" / "code" / "models"
_KOKORO = None
_KOKORO_LOCK = threading.Lock()
_WHISPER = None
_WHISPER_LOCK = threading.Lock()


def _model_path(env_name: str, filename: str) -> Path:
    """Resolve a voice model: explicit env, gateway models, then agent models."""
    explicit = (os.getenv(env_name) or "").strip()
    if explicit:
        return Path(explicit)
    for base in (MODELS, AGENT_MODELS):
        candidate = base / filename
        if candidate.exists():
            return candidate
    return MODELS / filename

KNOWN_VOICES = [
    {"id": "af_heart", "label": "Heart (female, warm)"},
    {"id": "af_bella", "label": "Bella (female, bright)"},
    {"id": "af_nicole", "label": "Nicole (female, soft)"},
    {"id": "am_michael", "label": "Michael (male)"},
    {"id": "am_adam", "label": "Adam (male, deep)"},
    {"id": "bf_emma", "label": "Emma (British female)"},
    {"id": "bm_george", "label": "George (British male)"},
]

# Emoji, flags, dingbats, variation selectors and ZWJ sequences. Kokoro reads
# these as names or noises; TTS must only receive words and punctuation.
_EMOJI_RE = re.compile(
    "[\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U00002B00-\U00002BFF"
    "\U0000FE0F\U0000200D\U000020E3"
    "]+", re.UNICODE)


def _speech_text(text: str) -> str:
    """Plain text for TTS: no emoji, no doubled spaces."""
    return " ".join(_EMOJI_RE.sub(" ", str(text or "")).split())


def warm_up() -> None:
    """Load the voice models in the background after start-up.

    The first Kokoro call used to pay the 311 MB model load (measured: the
    first chunk of the first request took 16.9s). A user speaking once after a
    restart should not pay that; the gateway now warms both models while it
    is idle. Failures are silent - the first real request reports the reason.
    """
    try:
        k = _get_kokoro()
        k.create("Ready.", "af_heart", 1.0)
    except Exception:
        pass
    try:
        _get_whisper()
    except Exception:
        pass


def _get_kokoro():
    global _KOKORO
    if _KOKORO is None:
        with _KOKORO_LOCK:
            if _KOKORO is None:
                from kokoro_onnx import Kokoro
                model_path = _model_path("KOKORO_MODEL", "kokoro-v1.0.onnx")
                voices_path = _model_path("KOKORO_VOICES", "voices-v1.0.bin")
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
    text = _speech_text(text)
    if not text:
        return StreamingResponse(iter([b"text has no speakable content"]),
                                 media_type="text/plain", status_code=400)
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


@router.post("/v1/tts/stream")
async def tts_stream(req: Request):
    """Sentence-chunked TTS as NDJSON.

    Each event carries a complete WAV for one chunk. The first chunk is
    short, so audio starts after roughly one sentence; the generator keeps
    synthesizing the rest while the browser plays what it already has. This
    keeps time-to-first-audio flat instead of growing with the answer length.

    Chunking happens in `voice_chunking`: sentence endings only (plus hard
    paragraph breaks). No chunk ever begins or ends mid-sentence.
    """
    t0 = time.time()
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    text = str(body.get("text") or "")
    if not text.strip():
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={"error": "text is required"})
    if len(text) > 200_000:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=413, content={
            "error": f"text is {len(text)} characters; limit is 200000"})
    voice = str(body.get("voice") or "af_heart")
    try:
        speed = float(body.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    try:
        first_chars = max(40, min(400, int(body.get("first_chars") or 180)))
        target_chars = max(80, min(1200, int(body.get("chunk_chars") or 360)))
    except (TypeError, ValueError):
        first_chars, target_chars = 180, 360

    from voice_chunking import speech_chunks
    pieces = speech_chunks(_EMOJI_RE.sub(" ", text), first_chars, target_chars)
    if not pieces:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400,
                            content={"error": "text has no speakable content"})

    async def gen():
        try:
            k = _get_kokoro()
            import soundfile as sf
        except FileNotFoundError as e:
            yield json.dumps({"type": "error", "error": str(e)}) + "\n"
            return
        except ImportError as e:
            yield json.dumps({"type": "error",
                              "error": f"voice dependencies missing: {e}"}) + "\n"
            return
        total_bytes = 0
        for i, piece in enumerate(pieces):
            try:
                audio, sr = await _run_blocking(k.create, piece, voice, speed)
                buf = io.BytesIO()
                sf.write(buf, audio, sr, format="WAV")
                blob = buf.getvalue()
            except Exception as e:
                yield json.dumps({"type": "error", "index": i,
                                  "error": f"{type(e).__name__}: {e}"[:300]}) + "\n"
                return
            total_bytes += len(blob)
            yield json.dumps({
                "type": "chunk",
                "index": i,
                "total": len(pieces),
                "text": piece,
                "sample_rate": sr,
                "duration_s": round(len(audio) / float(sr or 1), 3),
                "audio": base64.b64encode(blob).decode("ascii"),
            }, ensure_ascii=False) + "\n"
        try:
            db.log_call(provider="voice", model=f"tts_stream:{voice}",
                        status="ok",
                        latency_ms=int((time.time() - t0) * 1000),
                        prompt_chars=len(text), response_chars=total_bytes,
                        call_role="voice")
        except Exception:
            pass
        yield json.dumps({"type": "done", "chunks": len(pieces),
                          "seconds": round(time.time() - t0, 2)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.get("/v1/tts/voices")
async def tts_voices():
    """Voice picker list. Curated until Kokoro is already loaded: asking for
    the list must not load a 311 MB model as a side effect."""
    try:
        if _KOKORO is not None:
            voices = getattr(_KOKORO, "voices", None)
            if isinstance(voices, (list, tuple)) and voices:
                return {"voices": [{"id": v, "label": v}
                                   for v in voices if isinstance(v, str)]}
    except Exception:
        pass
    return {"voices": KNOWN_VOICES}


@router.get("/v1/voice/status")
async def voice_status():
    """What the voice channel can actually do, without loading the models.

    The console uses this to disable the mic/speaker honestly. `available`
    means the dependency and model files are present; a model can still fail
    at load time, which the real TTS/STT endpoints report.
    """
    import importlib.util as _spec
    tts_model = _model_path("KOKORO_MODEL", "kokoro-v1.0.onnx")
    tts_voices = _model_path("KOKORO_VOICES", "voices-v1.0.bin")
    tts_deps = (bool(_spec.find_spec("kokoro_onnx"))
                and bool(_spec.find_spec("soundfile")))
    tts_files = tts_model.exists() and tts_voices.exists()
    stt_dep = bool(_spec.find_spec("faster_whisper"))
    tts_errors = []
    if not tts_deps:
        tts_errors.append("kokoro-onnx/soundfile not installed")
    if not tts_model.exists():
        tts_errors.append(f"model missing: {tts_model}")
    if not tts_voices.exists():
        tts_errors.append(f"voices missing: {tts_voices}")
    return {
        "tts": {
            "available": bool(tts_deps and tts_files),
            "loaded": _KOKORO is not None,
            "dependencies": tts_deps,
            "model": str(tts_model),
            "voices_file": str(tts_voices),
            "error": "; ".join(tts_errors)[:400],
        },
        "stt": {
            "available": stt_dep,
            "loaded": _WHISPER is not None,
            "model": os.getenv("STT_MODEL", "base"),
            "error": "" if stt_dep else "faster-whisper not installed",
        },
        "voices": KNOWN_VOICES,
    }


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
