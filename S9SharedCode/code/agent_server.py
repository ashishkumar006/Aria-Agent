"""General-purpose web agent server for Session 9.

Wraps the S9 growing-graph orchestrator (`flow.Executor`) behind a small
FastAPI service so the agent can be driven from a browser. This is the
"general agent" surface: any natural-language query is handed to the
orchestrator, which decomposes it into a skill DAG (researcher, browser,
distiller, coder, summariser, formatter, …) and runs it.

Voice modality has two halves:
  * input  — handled on the client with the Web Speech API (mic → text)
  * output — handled *server-side* with Kokoro (onnx) neural TTS, which
    sounds far more natural than the browser's built-in SpeechSynthesis.
    The front-end requests a WAV from `/api/tts` and plays it back.

The front-end (`web/`) streams the agent's reasoning log and final answer
back as Server-Sent-Events-style newline-delimited JSON.

Endpoints:
  GET  /                 → the chat UI (static index.html)
  GET  /app.js /style.css → UI assets
  GET  /api/health      → {agent, gateway_up}
  POST /api/chat        → SSE-ish stream of {type: log|done|error}
  POST /api/tts         → WAV audio for a piece of text (Kokoro)

The V9 gateway is auto-started on first use (via gateway.ensure_gateway)
if it is not already listening on :8109.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse

ROOT = Path(__file__).parent
WEB = ROOT / "web"

app = FastAPI(title="Aria — General AI Agent")


# ── static UI ──────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse(str(WEB / "index.html"))


@app.get("/app.js")
async def app_js():
    return FileResponse(str(WEB / "app.js"), media_type="text/javascript")


@app.get("/style.css")
async def style_css():
    return FileResponse(str(WEB / "style.css"), media_type="text/css")


# ── health ──────────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    from gateway import _is_up  # re-exported helper from gateway.py

    return {"agent": "ready", "gateway_up": _is_up()}


# ── TTS (Kokoro, server-side) ───────────────────────────────────────────────
_MODELS = ROOT / "models"
_KOKORO = None  # lazily-loaded singleton
_KOKORO_LOCK = threading.Lock()


def _get_kokoro():
    """Load the Kokoro ONNX model once and reuse it across requests.

    The 325 MB model + 28 MB voices take ~1.7 s to load on first use, so we
    warm it up at startup (see `_warm_kokoro`) and guard the load with a lock
    so concurrent requests don't each trigger a cold load.
    """
    global _KOKORO
    if _KOKORO is None:
        with _KOKORO_LOCK:
            if _KOKORO is None:
                from kokoro_onnx import Kokoro

                model_path = _MODELS / "kokoro-v1.0.onnx"
                voices_path = _MODELS / "voices-v1.0.bin"
                if not model_path.exists() or not voices_path.exists():
                    raise FileNotFoundError(
                        f"Kokoro model files missing in {_MODELS}. "
                        "Download kokoro-v1.0.onnx and voices-v1.0.bin."
                    )
                _KOKORO = Kokoro(str(model_path), str(voices_path))
    return _KOKORO


def _warm_kokoro():
    """Eagerly load Kokoro at startup so the first /api/tts call is fast.

    Runs in a background thread so it never blocks the uvicorn startup path.
    Failures are non-fatal: TTS simply degrades to a 500 on first use.
    """
    def _load():
        try:
            _get_kokoro()
        except Exception:
            pass
    threading.Thread(target=_load, daemon=True).start()


# Warm up Kokoro in the background at import time so the first /api/tts call
# doesn't pay the ~1.7 s model-load cost (and the ~6 s synth never blocks
# the event loop because /api/tts now runs it via asyncio.to_thread).
_warm_kokoro()


@app.post("/api/tts")
async def tts(req: Request):
    """Synthesize text → WAV using Kokoro and return it as audio/wav.

    Body: {"text": "...", "voice": "af_heart", "speed": 1.0}
    A good default English voice is `af_heart` (warm female) or
    `am_michael` (male). Returns raw 24 kHz mono WAV bytes.
    """
    import io
    import soundfile as sf

    body = await req.json()
    text = (body.get("text") or "").strip()
    voice = body.get("voice") or "af_heart"
    try:
        speed = float(body.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0

    if not text:
        return StreamingResponse(
            iter([b""]), media_type="audio/wav", status_code=400
        )

    try:
        k = _get_kokoro()
        # Kokoro's create() is CPU-bound and blocks the event loop for ~6 s
        # on a short answer. Run it in a worker thread so the server stays
        # responsive (other chat/health requests keep working) while we synth.
        audio, sr = await asyncio.to_thread(k.create, text, voice, speed)
    except Exception as e:  # pragma: no cover - model/runtime issue
        return StreamingResponse(
            iter([str(e).encode()]), media_type="text/plain", status_code=500
        )

    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV")
    buf.seek(0)
    return StreamingResponse(
        iter([buf.read()]), media_type="audio/wav"
    )


# ── computer-use approvals (gated) ───────────────────────────────────────────
@app.get("/api/computer/approvals")
async def list_approvals():
    """List pending computer-use approvals + daemon capabilities for the UI."""
    from computer_use import get_computer_use, capabilities

    return {
        "approvals": get_computer_use().list_approvals(),
        "capabilities": capabilities(),
    }


# ── chat (streaming) ────────────────────────────────────────────────────────
def _sse(event_type: str, **payload) -> str:
    """One SSE-style frame: `data: {json}\\n\\n`."""
    frame = {"type": event_type, **payload}
    return "data: " + json.dumps(frame) + "\n\n"


def _run_orchestrator(query: str) -> str:
    """Run the async orchestrator to completion in a fresh event loop.

    `Executor.run` is a coroutine, so we drive it with `asyncio.run` inside
    a worker thread (via `asyncio.to_thread`) — that keeps the streaming
    response responsive while the agent runs, and lets us capture stdout
    as a reasoning log. Returning the coroutine directly would make the
    final `done` frame non-serialisable and abort the stream.
    """
    from flow import Executor

    return asyncio.run(Executor().run(query))


@app.post("/api/chat")
async def chat(req: Request):
    body = await req.json()
    query = (body.get("query") or "").strip()
    if not query:
        return StreamingResponse(
            iter([_sse("error", text="empty query")]),
            media_type="text/plain",
        )

    async def gen():
        import time as _time

        _start = _time.time()
        # Make sure the gateway (and its client) are available.
        try:
            from gateway import ensure_gateway

            ensure_gateway()
        except Exception as e:  # pragma: no cover - environment issue
            yield _sse("error", text=f"gateway unavailable: {e}")
            return

        # Import the orchestrator lazily so a missing gateway key at import
        # time doesn't break the health endpoint.
        from flow import Executor

        # The orchestrator runs its own asyncio loop internally via
        # asyncio.run in Executor.run, so we run it in a thread to keep the
        # streaming response alive. We capture stdout and STREAM each line
        # live as a `log` SSE frame (instead of buffering until the end) so
        # the operator can watch the agent work in the terminal / UI.
        answer_box: dict[str, str] = {"answer": ""}
        # BUG-FIX (C): the previous implementation used a threading.Queue and
        # called its blocking `.get()` inside this async generator, which
        # froze the entire event loop for the whole agent run (TTS, health,
        # and concurrent chats all hung). Use an asyncio.Queue and push from
        # the worker thread via call_soon_threadsafe so `await log_q.get()`
        # yields control between log lines and the server stays responsive.
        loop = asyncio.get_running_loop()
        log_q: "asyncio.Queue[str | None]" = asyncio.Queue()

        import io
        import contextlib

        class _LiveLogCatcher(io.StringIO):
            def write(self, s):  # type: ignore[override]
                if s.strip():
                    loop.call_soon_threadsafe(log_q.put_nowait, s)
                return super().write(s)

        def _run_and_drain():
            # Run the orchestrator, then push a sentinel so the generator
            # below knows the run finished and can emit the final `done`.
            try:
                with contextlib.redirect_stdout(_LiveLogCatcher()):
                    answer_box["answer"] = _run_orchestrator(query) or ""
            except Exception as e:  # pragma: no cover - runtime failure
                loop.call_soon_threadsafe(log_q.put_nowait, f"AGENT ERROR: {e}")
            finally:
                loop.call_soon_threadsafe(log_q.put_nowait, None)  # sentinel

        thread = threading.Thread(target=_run_and_drain, daemon=True)
        thread.start()

        # Stream log lines as they arrive, until the sentinel.
        while True:
            item = await log_q.get()
            if item is None:
                break
            yield _sse("log", text=item.rstrip("\n"))

        try:
            thread.join(timeout=5)
        except Exception:
            pass

        # Total wall-clock time for the whole response (orchestrator run +
        # any drain), reported to the UI so the operator can see how long
        # the agent took end-to-end.
        elapsed = _time.time() - _start
        yield _sse("meta", elapsed_s=round(elapsed, 1))
        yield _sse("done", answer=answer_box["answer"])

    return StreamingResponse(gen(), media_type="text/plain")


# ── Computer-Use approval UI (charter §13) ──────────────────────────────────
@app.post("/api/computer/approvals/{approval_id}")
async def computer_approve(approval_id: str, req: Request):
    """Resolve a pending approval: {"approve": true|false}."""
    from computer_use import safety
    body = await req.json()
    approve = bool(body.get("approve", False))
    result = safety.shared_gates().resolve(approval_id, approve)
    if result is None:
        return {"status": "not_found"}
    return {"status": "ok", "action": result["action"], "approve": approve}


# ── Computer-Use replay viewer (charter §11) ────────────────────────────────
@app.get("/api/computer/runs")
async def computer_runs():
    """List recorded computer-use trajectories (run directories)."""
    from computer_use.core.recording import DEFAULT_RECORD_ROOT
    root = DEFAULT_RECORD_ROOT
    if not root.exists():
        return {"runs": []}
    runs = []
    for d in sorted(root.iterdir(), reverse=True)[:50]:
        if d.is_dir():
            # Count turn directories / tool-call logs inside.
            turns = [t.name for t in d.iterdir() if t.is_dir()]
            runs.append({"id": d.name, "turns": len(turns),
                         "path": str(d)})
    return {"runs": runs}


@app.get("/api/computer/runs/{run_id}")
async def computer_run_detail(run_id: str):
    """Return the trajectory detail for a recorded run: per-turn tool calls."""
    from computer_use.core.recording import DEFAULT_RECORD_ROOT
    root = DEFAULT_RECORD_ROOT / run_id
    if not root.exists():
        return {"error": "run not found"}
    turns = []
    for t in sorted(root.iterdir()):
        if t.is_dir():
            calls = []
            for f in t.iterdir():
                if f.is_file() and f.suffix == ".json":
                    try:
                        calls.append(json.loads(f.read_text()))
                    except Exception:
                        pass
            turns.append({"turn": t.name, "calls": calls})
    return {"id": run_id, "turns": turns}


@app.post("/api/computer/replay/{run_id}")
async def computer_replay(run_id: str):
    """Replay a recorded trajectory against the same starting UI state."""
    from computer_use.core.recording import Recorder
    rec = Recorder(run_id)
    result = rec.replay()
    if result is None:
        return {"status": "error", "message": "replay failed"}
    return {"status": "ok", "result": result}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("agent_server:app", host="0.0.0.0", port=8500, reload=False)
