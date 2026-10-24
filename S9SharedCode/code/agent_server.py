"""General-purpose web agent server for Session 9.
Wraps the S9 growing-graph orchestrator (`flow.Executor`) behind a small
FastAPI service so the agent can be driven from a browser. This is the
"general agent" surface: any natural-language query is handed to the
orchestrator, which decomposes it into a skill DAG (researcher, browser,
distiller, coder, summariser, formatter, …) and runs it.
Voice modality has two halves:
  * input  — handled on the client with the Web Speech API (mic → text)  * output — handled *server-side* with Kokoro (onnx) neural TTS, which
    sounds far more natural than the browser's built-in SpeechSynthesis.
    The front-end requests a WAV from `/api/tts` and plays it back.
The front-end (console-frontend/, the React SPA) streams the agent's
reasoning log and final answer back as Server-Sent Events.
Endpoints (83 routes; the full list is the code — this is the shape):
  GET  /
                 → the React console (dist/index.html, legacy web/ fallback)
  GET  /console /research /runs /memory /scheduler /skills /apps /ledger /mission /settings
  GET  /api/health
      → {agent, gateway_up, spa_built}  POST /api/chat
        → SSE frames started/log/status/meta/done/error  POST /api/chat/simple → {answer, conversation_id} (one direct LLM call)  POST /api/chat/simple/stream → SSE frames started/status/delta/done/error  POST /api/tts
         → WAV audio for a piece of text (Kokoro)
The V9 gateway is auto-started on first use (via gateway.ensure_gateway)
if it is not already listening on :8109.
"""
from __future__ import annotations
import asyncio
import json
import os
import re
import sys
import threading
import time as _time
import datetime as _dt
from pathlib import Path, PurePosixPath
# Stdio is UTF-8, unconditionally (same fix as the gateway and MCP server:
# request logging prints user-influenced content, and cp1252 crashes on the
# first non-ASCII character).
for _s in (getattr(sys, "stdout", None), getattr(sys, "stderr", None)):
    try:
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
del _s
from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, Response, StreamingResponse,
                              JSONResponse, RedirectResponse)
# ── Live server log (Console page's /api/events "server" source) ──────────
# /api/events tails logs/agent.out + logs/agent.err, but nothing ever wrote
# them: uvicorn only logs to the terminal and orchestrator prints are
# swallowed by the streaming log catcher. When launched as a script we tee
# stdout/stderr into those files (size-capped) so the feed has a real server
# tail; _LIVE_OUT points at that teed stream so worker-thread prints land in
# it too (redirect_stdout swaps sys.stdout, which is why this is a global).
_LIVE_OUT = sys.stdout
class _Tee:
    """Write-through stream: everything written mirrors into a log file."""
    def __init__(self, stream, path: Path, cap_bytes: int = 4_000_000):
        self._stream = stream
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > cap_bytes:
                path.replace(path.with_suffix(path.suffix + ".1"))
            self._fh = open(path, "a", encoding="utf-8", errors="replace")
        except OSError:
            self._fh = None
        self._lock = threading.Lock()
    def write(self, data):
        self._stream.write(data)
        if self._fh is not None:
            try:
                with self._lock:
                    self._fh.write(data)
                    self._fh.flush()
            except OSError:
                self._fh = None
        return len(data)
    def flush(self):
        self._stream.flush()
        if self._fh is not None:
            try:
                self._fh.flush()
            except OSError:
                pass
    def isatty(self) -> bool:
        return bool(getattr(self._stream, "isatty", lambda: False)())
    def fileno(self) -> int:
        """Expose the wrapped stream's fd. Third-party code needs a real
        file descriptor — e.g. anyio.open_process(stderr=...) used by the
        MCP stdio client. Without this, every tool-using skill
        (researcher, retriever, ...) fails instantly with
        AttributeError: '_Tee' object has no attribute 'fileno'
        (the stdio client's `errlog` default binds sys.stderr at first
        lazy import, i.e. already teed)."""
        return self._stream.fileno()
    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)
    def __getattr__(self, name: str):
        # Transparent proxy for everything else (encoding, errors,
        # buffer, ...). Dunders are excluded so copy/pickle probing and
        # interpreter introspection never bind to the wrapped stream.
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        try:
            stream = object.__getattribute__(self, "_stream")
        except AttributeError:
            raise AttributeError(name) from None
        return getattr(stream, name)
ROOT = Path(__file__).parent
WEB = ROOT / "web"
# S9_STATE_DIR overrides the state dir (tests point it at tmp_path so suites
# never touch live sessions/ledger/schedules). All state paths below derive
# from it — never hardcode ".../state/..." elsewhere.
STATE_DIR = Path(os.environ.get("S9_STATE_DIR") or (ROOT / "state"))
def _read_json(path: Path, want: type):
    """Read a JSON state file defensively: missing file, bad encoding, BOM
    (utf-8-sig), invalid JSON, or wrong top-level shape all yield the empty
    container instead of crashing the caller or poisoning downstream code
    (a list where a dict was expected used to 500 /api/templates)."""
    empty = {} if want is dict else []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return empty
    return data if isinstance(data, want) else empty
def _write_json_atomic(path: Path, data) -> None:
    """Crash-safe JSON write (unique tmp + os.replace) so a kill mid-write
    never leaves a half-written state file behind."""
    import uuid as _uuid
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}-{_uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
app = FastAPI(title="Aria — General AI Agent", redirect_slashes=False)
# ── perceived-performance middleware ────────────────────────────────────
# GZip: JSON APIs + JS/CSS compress ~70% (minimum_size skips tiny payloads;
# level 5 balances CPU vs bytes per current Starlette guidance).
# Static-Cache: .css/.js are content-stable between deploys, so a short
# public max-age lets the browser skip revalidation entirely when hopping
# between console pages (the 9ms warm loads become ~0ms cache hits).
# NOTE: uvloop is intentionally NOT used — it has no Windows support.
try:
    from fastapi.middleware.gzip import GZipMiddleware
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
except Exception:
    pass
# CORS: same-origin localhost only. NOTE: "http://localhost:*" style entries
# have NO wildcard semantics in Starlette — they match nothing. List exact
# origins. Credentials are off (the UI uses no cookies; token auth, when
# ARIA_API_TOKEN is set, travels in the Authorization header, which needs
# no credentialed CORS).
try:
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:8500", "http://127.0.0.1:8500"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=False,
    )
except Exception:
    pass
# ── optional bearer-token gate ──────────────────────────────────────────
# ARIA_API_TOKEN unset (default): localhost-only binding below is the
# security boundary; behaviour is unchanged. Set it to ALSO require
# `Authorization: Bearer <token>` on every /api route except /api/health
# (e.g. before exposing the port beyond loopback). The bundled UI sends
# no token, so setting this without also fronting the UI will 401 the
# dashboard's API calls — that is intentional and loud, not silent.
_API_TOKEN = os.environ.get("ARIA_API_TOKEN", "").strip()
@app.middleware("http")
async def _api_token_gate(request: Request, call_next):
    if (_API_TOKEN and request.url.path.startswith("/api/")
            and request.url.path != "/api/health"
            and request.headers.get("authorization", "") != f"Bearer {_API_TOKEN}"):
        # The prefab iframe is a browser navigation — it cannot
        # carry a header — so its short-lived signed pass is the
        # door (see _origin_guard for the full reasoning).
        import auth as _auth
        if not _auth.valid_prefab_pass(request.url.path,
                                        request.query_params):
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return await call_next(request)
@app.middleware("http")
async def _static_cache(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.endswith((".css", ".js")) and "cache-control" not in resp.headers:
        resp.headers["Cache-Control"] = "public, max-age=300"
    return resp
@app.middleware("http")
async def _origin_guard(request: Request, call_next):
    """Require the per-launch token on `/api/*`, and refuse cross-origin writes.
    Enforcement is inert until `auth.configure()` runs on real startup, so test
    clients and `uvicorn --reload` are unaffected. Once a token exists there is
    no opt-out short: this is the control that closes the CSRF hole, and a
    silent bypass would be worse than the original problem.
    A custom header is required rather than a cookie specifically because a
    cookie is sent automatically cross-origin, which is the bug. A header is
    not, and setting one cross-origin needs a preflight this server never
    satisfies.
    """
    import auth as _auth
    path = request.url.path
    if not _auth.is_enabled() or not path.startswith("/api"):
        return await call_next(request)
    if _auth.is_public_path(path):
        return await call_next(request)
    supplied = request.headers.get(_auth.TOKEN_HEADER)
    if not _auth.check_header_value(supplied):
        # The prefab view is embedded as an <iframe src>,
        # and a browser navigation cannot carry a custom
        # header — so with enforcement on, the view 403'd
        # inside its own frame. The fetch-based pre-flight
        # passed precisely because a fetch CAN send the
        # header, which masked this. A short-lived HMAC
        # query token, minted through the authenticated
        # API, lets the iframe load without widening the
        # gate: it is signed with the per-launch token
        # (unforgeable by a cross-origin page) and
        # expires within seconds.
        if _auth.valid_prefab_pass(path, request.query_params):
            return await call_next(request)
        # 403, not 401: there is no login flow to send the client to, and a
        # 401 invites a credential prompt for a machine-local API.
        return JSONResponse(status_code=403, content={
            "error": "missing or invalid X-Aria-Token. This agent requires the "
                     "per-launch token injected into the console shell."})
    bad = _auth.cross_origin_write(request.headers, request.method)
    if bad:
        return JSONResponse(status_code=403, content={"error": bad})
    return await call_next(request)
# ── integrated terminal ─────────────────────────────────────────────────────
# A real shell behind a WebSocket (see terminal.py). Starlette's HTTP
# middleware — including the X-Aria-Token `_origin_guard` — does NOT run for a
# WebSocket handshake, so this route authenticates itself or it hands a shell
# to any web page the user visits. The per-launch token arrives as `?token=`
# (a browser cannot set a header on a WS handshake) and the Origin is checked
# as defence in depth.
from fastapi import WebSocket, WebSocketDisconnect  # noqa: E402
import terminal as _terminal  # noqa: E402

@app.websocket("/api/terminal")
async def _terminal_ws(websocket: WebSocket):
    import asyncio as _asyncio
    import json as _json
    import threading as _threading
    import auth as _auth

    params = websocket.query_params
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    denied = False
    if _auth.is_enabled() and not _auth.check_header_value(params.get("token")):
        denied = True
    if origin and origin != "null" and host:
        tail = origin.split("://", 1)[-1].strip("/").lower()
        if tail != host.strip().lower():
            denied = True
    if denied:
        # 1008 = policy violation. Accept-then-close: closing before accept
        # surfaces to the browser only as a generic handshake failure.
        await websocket.accept()
        await websocket.close(code=1008)
        return

    def _int(name, default, lo, hi):
        try:
            return max(lo, min(hi, int(params.get(name, default))))
        except (TypeError, ValueError):
            return default

    cols = _int("cols", 80, 20, 500)
    rows = _int("rows", 24, 5, 300)
    cwd = params.get("cwd") or None
    if cwd and not os.path.isdir(cwd):
        cwd = None
    try:
        session = _terminal.PtySession(_terminal.default_shell(),
                                       cwd=cwd, cols=cols, rows=rows)
    except Exception as exc:
        await websocket.accept()
        await websocket.send_text(_json.dumps(
            {"t": "exit", "error": f"could not start shell: {exc}"}))
        await websocket.close(code=1011)
        return

    await websocket.accept()
    loop = _asyncio.get_running_loop()
    out: "_asyncio.Queue" = _asyncio.Queue()

    def _pump():
        while session.is_alive():
            try:
                data = session.read(4096)
            except Exception:
                break
            if not data:
                break
            loop.call_soon_threadsafe(out.put_nowait, data)
        loop.call_soon_threadsafe(out.put_nowait, None)

    _threading.Thread(target=_pump, daemon=True).start()

    async def _to_client():
        try:
            while True:
                data = await out.get()
                if data is None:
                    break
                await websocket.send_bytes(data)
            await websocket.send_text(_json.dumps({"t": "exit"}))
        except Exception:
            pass

    async def _from_client():
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            text = msg.get("text")
            blob = msg.get("bytes")
            if text is not None:
                try:
                    cmd = _json.loads(text)
                except Exception:
                    continue
                kind = cmd.get("t")
                if kind == "in":
                    session.write(str(cmd.get("d", "")).encode("utf-8"))
                elif kind == "resize":
                    try:
                        session.resize(int(cmd.get("cols", cols)),
                                       int(cmd.get("rows", rows)))
                    except (TypeError, ValueError):
                        pass
            elif blob:
                session.write(blob)

    async def _guarded(coro):
        # Without this, a task that ends by raising surfaces as
        # "Task exception was never retrieved". Both tasks are
        # EXPECTED to end (the socket disconnects), so their errors
        # are swallowed here rather than logged as failures.
        try:
            await coro
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    readers = [_asyncio.create_task(_guarded(_to_client())),
               _asyncio.create_task(_guarded(_from_client()))]
    try:
        done, pending = await _asyncio.wait(
            readers, return_when=_asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        session.close()

# ── conversation threads ────────────────────────────────────────────────────
# Maps a stable `conversation_id` (sent by the UI) to a session_id so multiple
# /api/chat calls in the same conversation share one orchestrator session —
# memory, turn log, cost ledger and the DAG all accumulate per conversation.
# Persisted to disk so threads survive a server restart. Without a
# conversation_id, each call is a standalone session (legacy behaviour).
_CONV_PATH = STATE_DIR / "conversations.json"
# Locking discipline (read this before "fixing" it): every store writes via
# _write_json_atomic (temp file + os.replace), so a lock-free read can NEVER
# observe a torn file — at worst it reads slightly stale data, which is
# harmless for listings. The locks below guard read-MODIFY-write sequences
# only (check-then-create in resolve_session, append in _record_turn_cost).
# Adding read locks would buy nothing and add event-loop contention.
_CONV_LOCK = threading.Lock()
def _conv_load() -> dict:
    return _read_json(_CONV_PATH, dict)
def _conv_save(data: dict) -> None:
    _write_json_atomic(_CONV_PATH, data)
def resolve_session(conversation_id: str | None, *,
                    create: bool = True) -> str | None:
    """Return the session_id for a conversation, creating one if needed.
    `create=False` is the read-only form for GET paths: an unknown id
    returns None instead of minting a permanent phantom entry in
    conversations.json. Nothing prunes that map, and a typo'd id from
    a bookmarked URL used to spawn an entry on every page load."""
    import uuid as _uuid
    if not conversation_id:
        return f"s8-{_uuid.uuid4().hex[:8]}"
    with _CONV_LOCK:
        data = _conv_load()
        sid = data.get(conversation_id)
        if sid:
            return sid
        if not create:
            return None
        sid = f"s8-{_uuid.uuid4().hex[:8]}"
        data[conversation_id] = sid
        _conv_save(data)
        return sid
# ── smart notifications (Todo 8) ────────────────────────────────────────────
# When a long agent run finishes, push a short summary to Telegram if the
# operator opted in (AGENT_TELEGRAM_NOTIFY=true + AGENT_NOTIFY_CHAT_ID).
# Delivery goes through the gateway telegram adaptor — the bot token lives
# gateway-side; the agent only names the destination chat. Fire-and-forget
# on a daemon thread so it never blocks the response. Best effort: any
# failure is swallowed.
_NOTIFY_CHAT = os.environ.get("AGENT_NOTIFY_CHAT_ID")
_GW_NOTIFY = os.environ.get("LLM_GATEWAY_V9_URL", "http://localhost:8109").rstrip("/")
def _self_token() -> str:
    """The agent's own per-launch token, for loopback self-calls.
    `channel_chat` drives a channel turn by calling the chat route on this very
    process, and that route requires `X-Aria-Token` like every other. Prefer
    the in-process token, which is what a call from inside the server gets.
    Fall back to `state/agent.token` when this process is not the server (a
    CLI, a worker): `configure()` ran in the server's process, not here, and
    that file exists precisely so a second Aria process can authenticate to
    the running one. Returns "" when neither is available, which is correct
    under `ARIA_AUTH=off` — the header is simply absent, and the endpoint does
    not check it.
    """
    try:
        import auth as _auth
        if _auth.is_enabled():
            return _auth.token()
        # Not configured here: this process is not the server (a CLI, a
        # worker). Read the file `configure()` writes on launch, using the
        # module's own state-dir resolution so S9_STATE_DIR is honoured.
        return (_auth.state_dir() / "agent.token") \
            .read_text(encoding="utf-8").strip()
    except Exception:
        return ""
# Resolved destination, cached briefly. Resolving costs a loopback call, and
# the answer only changes when someone pairs a new chat.
_NOTIFY_RESOLVED: tuple[str, bool, float] | None = None   # (chat, ok, at)
# How long a MISS (no destination resolved) is remembered, so a burst of
# chat replies cannot hammer the gateway. A hit is never cached — see
# `resolve_notify_chat` for why that distinction is load-bearing.
_NOTIFY_MISS_TTL_S = 15.0
def resolve_notify_chat(channel: str = "telegram") -> str:
    """Where a notification should go, without a second env var to set.
    This used to require BOTH `AGENT_NOTIFY_CHAT_ID` and an opt-in flag, and
    the pairing store the gateway already keeps was not reachable from here at
    all - it is written by an inbound message and read back masked. So a
    reminder with a bot token configured had nowhere to go and failed
    silently, which is the worst possible failure for a reminder.
    Order: an explicit id wins, then the gateway's paired chat for the
    channel.
    A MISS is cached for a moment so a burst of chat replies does not hammer
    the gateway, but a hit is not: a paired chat changes only when someone
    pairs, and re-reading it is one cheap loopback call. The old code cached
    both alike for 60s, so a single 401 — the gateway restarting, or its token
    rotating under a long-lived agent — poisoned the cache and every reminder
    that fired in that window was recorded as "no notification destination is
    paired". That message is a lie: a destination WAS paired, and the agent
    simply could not read it for a moment. Verified live — that is exactly
    what silently ate every channel-less reminder.
    A 401 is retried once with freshly-read credentials, matching `_gw_json`.
    """
    global _NOTIFY_RESOLVED
    if _NOTIFY_CHAT:
        return _NOTIFY_CHAT
    now = _time.time()
    if _NOTIFY_RESOLVED and (now - _NOTIFY_RESOLVED[2]) < _NOTIFY_MISS_TTL_S:
        return _NOTIFY_RESOLVED[0]
    chat = ""
    try:
        import httpx as _hx
        with _hx.Client(timeout=8.0, headers=_gw_auth_headers()) as c:
            r = c.get(f"{_GW_NOTIFY}/v1/control/notify-target",
                      params={"channel": channel})
            if r.status_code == 401:
                # The gateway rotated its token, or this agent was started
                # before the gateway existed. Re-read and try once.
                c.headers.update(_gw_auth_headers())
                r = c.get(f"{_GW_NOTIFY}/v1/control/notify-target",
                          params={"channel": channel})
            if r.status_code == 200:
                chat = str((r.json() or {}).get("target") or "")
            elif r.status_code >= 400:
                print(f"[notify] notify-target -> HTTP {r.status_code}; "
                      f"no destination resolved for {channel}")
    except Exception as e:
        print(f"[notify] notify-target unreachable: {type(e).__name__}: {e}")
        chat = ""
    # Cache a MISS briefly (burst protection). Do NOT cache a hit: it costs
    # one loopback call and is the only thing standing between a reminder and
    # a silent drop.
    _NOTIFY_RESOLVED = (chat, bool(chat), now) if not chat else None
    return chat
def _notify_telegram(text: str, chat: str | None = None) -> bool:
    """Send one Telegram message. Returns whether the gateway accepted it.
    The gateway requires `X-Gateway-Token` on every `/v1/*` route. This post
    used to omit it, so it came back **401** — and the bare `except: pass`
    plus an unchecked status code swallowed that completely. A reminder set
    without an explicit channel runs through here, so every such reminder
    fired, billed its agent run, recorded an in-app entry, and then went
    nowhere: no Telegram message, no log line, no error anywhere. Reproduced
    live before this fix.
    Now it sends the header, checks the status, and logs a failure instead of
    discarding it. Returns True/False so callers can record the outcome."""
    to = chat if chat is not None else resolve_notify_chat()
    if not to:
        return False
    try:
        import httpx as _hx
        r = _hx.post(
            f"{_GW_NOTIFY}/v1/channels/telegram/send",
            json={"to": to, "text": text[:4096],
                  "agent": "notify"},
            headers=_gw_auth_headers(),
            timeout=15,
        )
        if r.status_code >= 400:
            print(f"[notify] telegram send failed: HTTP {r.status_code} "
                  f"{r.text[:160]}")
            return False
        return True
    except Exception as e:
        print(f"[notify] telegram send failed: {type(e).__name__}: {e}")
        return False
def notify_task_done(query: str, answer: str, cost: float,
                     session_id: str, *, scheduled: bool = False,
                     message: str = "") -> None:
    """Send a Telegram summary of a finished run.
    `scheduled=True` (a fired reminder) notifies whenever a destination
    exists, with no opt-in flag. A reminder that is silent by default is not a
    reminder, and this path used to fail silently in exactly that way: two env
    vars had to be set on the agent, and a destination could not be resolved
    from the pairing the gateway already held.
    `scheduled=False` (an ordinary chat response) stays opt-in behind
    AGENT_TELEGRAM_NOTIFY, because a Telegram ping for every conversational
    reply is noise - that was the earlier behaviour and it was removed.
    Either way the in-app notification feed is written, so there is always
    somewhere the result is recorded even if Telegram is unreachable.
    `message` is the text the AGENT wrote when it scheduled the task — the
    words it chose for this particular reminder. It is sent verbatim. This
    used to be discarded on the no-channel path, so a reminder set through
    Telegram arrived as a fixed template regardless of what the agent wanted
    to say: a green tick, a wall-clock line, a session id, and the raw run
    output. The agent already decided what the user should be told at
    scheduling time; that decision was being overwritten by a formatter.
    `cost` is the wall-clock time of the run in seconds (named `cost` for
    backwards-compat with the public API / wiring check)."""
    if not scheduled and os.environ.get(
            "AGENT_TELEGRAM_NOTIFY", "").lower() != "true":
        return
    chat = resolve_notify_chat()
    if not chat:
        # Say which of the two it was. "No destination is paired" tells the
        # user to message the bot, and that advice is wrong when the real
        # cause was an unreachable or unauthorised gateway — which is how a
        # transient 401 masqueraded as a pairing problem for an entire
        # release, and sent the user off to fix something that was fine.
        _notif_add("scheduled", f"scheduled task finished but no "
                                  f"notification destination could be read "
                                  f"from the gateway: {query[:120]}")
        return
    authored = (message or "").strip()
    if authored:
        # The agent's own words, unchanged. No prefix, no decoration.
        msg = authored
    else:
        # No authored text: say plainly what happened, in a sentence. A bare
        # answer with no framing is worse than a short lead-in, but a wall
        # clock and a session id are not what a person asked for.
        snippet = (answer or "").strip()
        snippet = snippet[:600] + ("…" if len(snippet) > 600 else "")
        msg = (f"{query[:200]}\n\n{snippet}" if snippet
               else f"{query[:200]}")
    # Sent inline, not on a daemon thread. A daemon thread detached the
    # outcome from the caller entirely: the fire logged "ok", the thread
    # failed for whatever reason, and nothing downstream could ever record
    # that the reminder was not delivered. This is the path a channel-less
    # reminder depends on, and its whole job is to report honestly.
    if not _notify_telegram(msg, chat):
        _notif_add("scheduled",
                   f"scheduled task finished but the notification could not "
                   f"be delivered: {query[:120]}", session_id)
# ── notification log (in-memory + on-disk) ──────────────────────────────────
# Scheduled tasks and long agent runs push a short summary here so the UI
# notification panel can show "what happened recently" even when the operator
# wasn't watching. Bounded to the last 200 entries to keep the file small.
_NOTIF_PATH = STATE_DIR / "notifications.json"
_NOTIF_LOCK = threading.Lock()
_NOTIF_MAX = 200
def _notif_load() -> list:
    return _read_json(_NOTIF_PATH, list)
def _notif_add(kind: str, text: str, session_id: str = "") -> dict:
    """Record a run outcome. Defined before `notify_task_done` uses it.
    This is the zero-configuration half of a reminder: it always records, so
    a fired task is visible in the app even when Telegram is unconfigured or
    unreachable.
    """
    try:
        return add_notification(kind, text, session_id)
    except Exception:
        return {}
def _notif_save(items: list) -> None:
    try:
        _write_json_atomic(_NOTIF_PATH, items[-_NOTIF_MAX:])
    except Exception:
        pass
def add_notification(kind: str, text: str, session_id: str = "",
                     conversation_id: str = "") -> dict:
    """Append a notification entry and return it for the caller."""
    entry = {
        "ts": _time.time(),
        "kind": kind,
        "text": text[:500],
        "session_id": session_id,
        "conversation_id": conversation_id,
    }
    with _NOTIF_LOCK:
        items = _notif_load()
        items.append(entry)
        _notif_save(items)
    return entry
def list_notifications(limit: int = 50) -> list:
    """Return the most recent notifications (newest first).
    `limit` is clamped at 0 and treated as "everything". A negative value used
    to be passed straight into `items[-limit:]`, where `items[--1]` is
    `items[1:]` - so `?limit=-1` quietly dropped the OLDEST record instead of
    being rejected, and the caller got a short list with no indication that a
    window had been applied.
    """
    with _NOTIF_LOCK:
        items = _notif_load()
    if limit is None or limit <= 0:
        return list(reversed(items))
    return list(reversed(items[-int(limit):]))
# ── static UI ──────────────────────────────────────────────────────────────
@app.get("/app.js")
async def app_js():
    return FileResponse(str(WEB / "app.js"), media_type="text/javascript")
@app.get("/style.css")
async def style_css():
    return FileResponse(str(WEB / "style.css"), media_type="text/css")
# ── Aria console SPA (React+Vite build; vanilla files = fallback) ─────────
# The console-frontend bundle serves every section. Each page route returns
# dist/index.html when built, else the legacy vanilla file of the same name.
_SPA_INDEX = ROOT / "console-frontend" / "dist" / "index.html"
# (cache key, html) for the shell with the auth token injected.
_spa_cache: tuple[tuple, str] | None = None
def _page(name: str) -> FileResponse:
    if _SPA_INDEX.exists():
        return _spa_shell()
    # The fallback is silent, so a missing build looks exactly like a working
    # (but completely different, unmaintained) UI. Say so on stderr.
    import sys as _sys
    print(f"[ui] SPA build MISSING at {_SPA_INDEX} — serving legacy "
          f"web/{name}. Run `npm run build` in console-frontend/.",
          file=_sys.stderr)
    return FileResponse(str(WEB / name))
def _spa_shell() -> FileResponse:
    """Serve `dist/index.html` with the auth token injected.
    The SPA has to learn the per-launch token, and this is where it comes from.
    It is same-origin, so a cross-origin page cannot read it - which is exactly
    why a token in the HTML still closes CSRF while doing nothing against
    another process running as the same OS user.
    Cached on mtime so a rebuild in development is still picked up; injecting
    per request would re-read the file on every page load for no benefit.
    """
    meta = ""
    try:
        import auth as _auth
        meta = _auth.token_meta_snippet()
    except Exception:
        meta = ""
    # A stale shell pins the tab to the PREVIOUS build: it names old asset
    # hashes, and those bundles are cached `immutable` for a year (see the
    # /assets route), so the browser keeps rendering the old UI without ever
    # asking the server again. The shell is ~800 bytes - revalidate it on
    # every navigation instead of letting a cache front the URL.
    no_stale = {"Cache-Control": "no-cache"}
    try:
        stat = _SPA_INDEX.stat()
        key = (stat.st_mtime_ns, stat.st_size, meta)
    except OSError:
        return FileResponse(str(_SPA_INDEX), headers=no_stale)
    global _spa_cache
    if _spa_cache and _spa_cache[0] == key:
        return Response(_spa_cache[1], media_type="text/html", headers=no_stale)
    try:
        html = _SPA_INDEX.read_text(encoding="utf-8")
    except OSError:
        return FileResponse(str(_SPA_INDEX), headers=no_stale)
    if meta and TOKEN_META_NAME not in html:
        html = html.replace("</head>", f"  {meta}\n</head>", 1)
    _spa_cache = (key, html)
    return Response(html, media_type="text/html", headers=no_stale)
TOKEN_META_NAME = 'name="aria-token"'
@app.get("/")
async def index():
    """Root serves the React console.
    This used to hard-code the legacy vanilla `web/index.html`, which meant
    the documented root URL (and the one in the module docstring and
    ARCHITECTURE.md) opened an unmaintained UI with no path to any of the
    console's sections. The SPA router's own `/` → `/research` redirect
    makes the intent unambiguous."""
    return _page("index.html")
@app.get("/console")
async def console():
    return _page("console.html")
@app.get("/console.js")
async def console_js():
    return FileResponse(str(WEB / "console.js"), media_type="text/javascript")
@app.get("/console.css")
async def console_css():
    return FileResponse(str(WEB / "console.css"), media_type="text/css")
@app.get("/runs")
async def runs():
    return _page("runs.html")
@app.get("/runs.js")
async def runs_js():
    return FileResponse(str(WEB / "runs.js"), media_type="text/javascript")
@app.get("/memory")
async def memory_page():
    return _page("memory.html")
@app.get("/memory.js")
async def memory_js():
    return FileResponse(str(WEB / "memory.js"), media_type="text/javascript")
@app.get("/scheduler")
async def scheduler_page():
    return _page("scheduler.html")
@app.get("/scheduler.js")
async def scheduler_js():
    return FileResponse(str(WEB / "scheduler.js"), media_type="text/javascript")
@app.get("/skills")
async def skills_page():
    return _page("skills.html")
@app.get("/skills.js")
async def skills_js():
    return FileResponse(str(WEB / "skills.js"), media_type="text/javascript")
@app.get("/ledger")
async def ledger_page():
    return _page("ledger.html")
@app.get("/ledger.js")
async def ledger_js():
    return FileResponse(str(WEB / "ledger.js"), media_type="text/javascript")
@app.get("/settings")
async def settings_page():
    return _page("settings.html")
@app.get("/settings.js")
async def settings_js():
    return FileResponse(str(WEB / "settings.js"), media_type="text/javascript")
@app.get("/shared.css")
async def shared_css():
    return FileResponse(str(WEB / "shared.css"), media_type="text/css")
@app.get("/mission")
async def mission_page():
    return _page("mission.html")
@app.get("/mission.js")
async def mission_js():
    return FileResponse(str(WEB / "mission.js"), media_type="text/javascript")
@app.get("/research")
async def research_page():
    return _page("research.html")
@app.get("/assets/{path:path}")
async def frontend_assets(path: str):
    """Hashed Vite assets for the React console (immutable, cache hard)."""
    target = (ROOT / "console-frontend" / "dist" / "assets" / path).resolve()
    base = (ROOT / "console-frontend" / "dist" / "assets").resolve()
    if base not in target.parents and target != base:
        return JSONResponse(status_code=404, content={"error": "not found"})
    if not target.is_file():
        return JSONResponse(status_code=404, content={"error": "not found"})
    return FileResponse(str(target), headers={"Cache-Control": "public, max-age=31536000, immutable"})
@app.get("/research.js")
async def research_js():
    return FileResponse(str(WEB / "research.js"), media_type="text/javascript")
@app.get("/apps")
async def apps_page():
    return _page("apps.html")
@app.get("/apps.js")
async def apps_js():
    return FileResponse(str(WEB / "apps.js"), media_type="text/javascript")
@app.get("/api/mcp/stats")
async def mcp_stats():
    """Runtime counters for the things that are invisible from the UI.
    Read from disk rather than by importing the MCP server: that module loads
    FastMCP and its whole tool surface, and it is a *different process* (one
    subprocess per skill invocation), so importing it here would report this
    process's zeroed counters instead of the real ones.
    """
    import os as _os
    from pathlib import Path as _P
    cache_dir = _P(_os.environ.get("ARIA_CACHE_DIR")
                  or (_P(__file__).parent / "sandbox" / ".cache" / "fetch"))
    stats: dict = {"cache": {"enabled": None, "hits": 0, "misses": 0,
                             "hit_rate": 0.0, "entries": 0, "ttl_days": 7.0}}
    try:
        blob = json.loads((cache_dir / "_stats.json").read_text(encoding="utf-8"))
        hits, misses = int(blob.get("hits", 0)), int(blob.get("misses", 0))
        total = hits + misses
        entries = len([p for p in cache_dir.glob("*.json")
                       if not p.name.startswith("_")])
        stats["cache"] = {"enabled": _os.environ.get("ARIA_FETCH_CACHE", "1")
                          not in ("0", "false", "no"),
                          "hits": hits, "misses": misses,
                          "hit_rate": round(hits / total, 3) if total else 0.0,
                          "entries": entries, "ttl_days": 7.0}
    except Exception:
        pass
    try:
        import outcomes as _outcomes
        stats["tool_outcomes"] = _outcomes.stats()
    except Exception:
        stats["tool_outcomes"] = None
    try:
        from mcp_runner import breaker_stats
        stats["mcp_breaker"] = breaker_stats()
    except Exception:
        stats["mcp_breaker"] = None
    return stats
@app.get("/api/feedback")
async def get_feedback(node_id: str, session_id: str = ""):
    """Latest thumbs vote for a node (1 | -1 | 0 when none).
    Node ids are per-run counters (n:1, n:2 …), so a node_id-only
    match returns the latest vote cast on that index in ANY run —
    a thumbs-down on yesterday's n:3 colored today's n:3. The POST
    already stores session_id; scope the lookup when it is given."""
    try:
        path = STATE_DIR / "feedback.jsonl"
        if not path.exists():
            return {"node_id": node_id, "vote": 0}
        vote = 0
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("node_id") != node_id:
                continue
            if session_id and e.get("session_id") != session_id:
                continue
            vote = int(e.get("vote") or 0)
        return {"node_id": node_id, "vote": vote}
    except Exception:
        return {"node_id": node_id, "vote": 0}
@app.post("/api/feedback")
async def post_feedback(req: Request):
    """Record a thumbs up/down on a run node. Body:
    {"session_id": str, "node_id": str, "vote": 1 | -1}."""
    import time as _t
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    nid = _body_str(body, "node_id")
    if nid is None:
        return JSONResponse(status_code=400,
                            content={"error": "node_id must be a string"})
    raw_vote = body.get("vote")
    # Strict: an int (or a JSON bool, where true=up) is a vote;
    # "1", 1.9 and null used to be silently coerced and written.
    if isinstance(raw_vote, bool):
        vote = 1 if raw_vote else -1
    elif isinstance(raw_vote, int):
        vote = raw_vote
    else:
        vote = 0
    if not nid or vote not in (1, -1):
        return JSONResponse(status_code=400, content={"error": "need node_id + vote 1|-1"})
    try:
        path = STATE_DIR / "feedback.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": _t.time(), "session_id": body.get("session_id") or "",
                                "node_id": nid, "vote": vote,
                                # Skill is denormalised at write time. Resolving
                                # it later needs the session's graph, which is
                                # deleted with the run — so a rollup over
                                # history would silently lose every vote for
                                # any pruned run.
                                "skill": body.get("skill") or "",
                                "note": str(body.get("note") or "")[:280]}) + "\n")
        # Bound the log: keep only the newest votes (the GET scans the
        # whole file per lookup, so unbounded growth also meant O(n)
        # latency on every thumbs check).
        try:
            _lines = path.read_text(encoding="utf-8-sig").splitlines()
            if len(_lines) > 5000:
                path.write_text("\n".join(_lines[-5000:]) + "\n", encoding="utf-8")
        except OSError:
            pass
        return {"status": "ok", "node_id": nid, "vote": vote}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)[:200]})
@app.get("/api/feedback/rollup")
async def feedback_rollup(days: int = 30, min_votes: int = 1):
    """Turn collected thumbs into a signal you can act on.
    Votes were appended to feedback.jsonl and read back per node so the UI can
    show which ones exist, but nothing ever aggregated them: there was no way
    to ask "is the researcher prompt regressing?" or "which skill produces the
    output people mark bad?". A rollup is the only honest answer to "did that
    prompt edit help?", and it costs one pass over a file already capped at
    5000 lines.
    `min_votes` guards the classic small-sample trap: 0/1 is not a 0% failure
    rate, it is no evidence, so those rows are reported but flagged.
    """
    import time as _t
    days = max(1, min(int(days or 30), 365))
    path = STATE_DIR / "feedback.jsonl"
    if not path.exists():
        return {"window_days": days, "totals": {"up": 0, "down": 0, "net": 0.0},
                "by_skill": [], "by_node": [], "trend": [], "note": "no votes yet"}
    cutoff = _t.time() - days * 86400
    by_skill: dict[str, dict] = {}
    by_node: dict[str, dict] = {}
    day_buckets: dict[str, dict] = {}
    up = down = 0
    unlabelled = 0
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()[-5000:]
    except OSError:
        lines = []
    for raw in lines:
        try:
            v = json.loads(raw)
        except Exception:
            continue
        t = float(v.get("t") or 0)
        if t and t < cutoff:
            continue
        vote = int(v.get("vote") or 0)
        if vote not in (1, -1):
            continue
        skill = str(v.get("skill") or "").strip()
        if not skill:
            unlabelled += 1
            skill = "(unlabelled — client predates the skill field)"
        nid = str(v.get("node_id") or "")
        for bucket, key in ((by_skill, skill), (by_node, nid)):
            row = bucket.setdefault(key, {"name": key, "up": 0, "down": 0})
            row["up" if vote > 0 else "down"] += 1
        up += vote > 0
        down += vote < 0
        if t:
            day = _dt_day(t)
            b = day_buckets.setdefault(day, {"day": day, "up": 0, "down": 0})
            b["up" if vote > 0 else "down"] += 1
    def _finish(rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            total = r["up"] + r["down"]
            out.append({**r, "total": total,
                        "net": round((r["up"] - r["down"]) / total, 3) if total else 0.0,
                        "confidence": "low" if total < min_votes else "ok"})
        out.sort(key=lambda r: (r["total"], r["net"]))
        return out
    return {
        "window_days": days,
        "totals": {"up": up, "down": down, "total": up + down,
                   "net": round((up - down) / (up + down), 3) if (up + down) else 0.0},
        "by_skill": _finish(list(by_skill.values())),
        "by_node": _finish(list(by_node.values()))[:20],
        "trend": sorted(day_buckets.values(), key=lambda b: b["day"])[-30:],
        "unlabelled": unlabelled,
    }
def _dt_day(ts: float) -> str:
    from datetime import datetime as _d
    return _d.fromtimestamp(ts).strftime("%Y-%m-%d")
# ── log-tail folding for the Console feed ───────────────────────────────────
# One mishap writes 15-25 log lines: the frame list, the echoed source, caret
# markers under the offending expression, the exception, a docs URL. The feed
# tailed that file verbatim, so a single bad request filled the Console with
# red and the ERRORS counter read 73 for four real incidents — the number
# operators act on stopped meaning anything.
#
# The shape is regular enough to fold without a full traceback parser: a
# burst is a maximal run of blank / indented / exception-detail lines that
# contains at least one exception name or `Traceback` header. Everything in
# the run becomes ONE feed row that names the exception and points at the log
# for the rest. Runs with no exception in them pass through untouched, so
# ordinary output is never hidden.
_TRACE = re.compile(r"Traceback \(most recent call last\)")
_FRAME = re.compile(r'File "[^"]*", line \d+, in (\w+)')
_ERR_NAME = re.compile(r"^[\w.]*(?:Error|Exception|Interrupt)\b\s*:")
_ERR_DETAIL = re.compile(
    r"(^[=\s]*\^+\s*$|^=+\s*$"                      # caret / rule markers
    r"|^File \""                                     # frame lines
    r"|^(For further information visit|Invalid JSON:|Failed to parse"
    r"|The above exception|During handling|During processing)"
    r"|input_value=|type=json_invalid"
    r"|^return cls\.|^message = |^self\.|^raise |^cls\.)")
def _in_log_burst(line: str) -> bool:
    """Could this line be part of an exception burst?"""
    stripped = line.strip()
    if not stripped:
        return True
    if line[:1] in (" ", "\t"):        # frame / echoed source / caret
        return True
    return bool(_TRACE.search(line) or _ERR_NAME.match(stripped)
                or _ERR_DETAIL.search(line))
def _fold_log_block(lines: list[str]) -> list[tuple[str, int]]:
    """Fold exception bursts. Returns [(text, absorbed_line_count)]."""
    out: list[tuple[str, int]] = []
    i, n = 0, len(lines)
    while i < n:
        if not _in_log_burst(lines[i]):
            out.append((lines[i], 0))
            i += 1
            continue
        j = i
        while j < n and _in_log_burst(lines[j]):
            # A second `Traceback` header starts a *different* incident
            # (chained exceptions, or two failures in one burst of logging).
            # Folding them together produced one unreadable mega-row.
            if j > i and _TRACE.search(lines[j]):
                break
            j += 1
        run_end = j
        while j > i and not lines[j - 1].strip():   # drop trailing blanks
            j -= 1
        group = lines[i:j]
        anchored = any(_ERR_NAME.match(g.strip()) or _TRACE.search(g)
                       for g in group)
        if anchored and len(group) >= 2:
            # Name the exception AND quote the payload when both are present:
            # "ValidationError: ... JSONRPCMessage" says what broke,
            # "Invalid JSON: ... input_value='...'" says what was sent.
            head = next((g.strip() for g in group if _TRACE.search(g)), "")
            names = [g.strip() for g in group if _ERR_NAME.match(g.strip())]
            if names:
                head = f"{head} | {names[-1]}" if head else names[-1]
                if len(names) > 1:
                    head = f"{names[0]} | {head}"
            elif head:
                # A traceback with no exception line in the run still has a
                # frame worth naming: "... in stdout_reader" beats a bare
                # "Traceback (most recent call last):".
                frame = next((m.group(1) for g in group
                              for m in [_FRAME.search(g)] if m), "")
                if frame:
                    head = f"{head} in {frame}"
            if not head:
                head = group[0].strip()
            out.append((f"{head}  (+{len(group) - 1} lines in the log)",
                        len(group) - 1))
        else:
            out.extend((g, 0) for g in group)
        i = run_end
    return out
def _scrub_log_line(line: str) -> str:
    """Remove server internals from a log line before it leaves the process.
    Keeps the exception type and its message (what an operator acts on);
    drops file paths (`File "C:\\..."`), frame internals (`in run_asgi`,
    `+61 lines in the log` stays - it says where to look, locally), and
    anything that looks like a local address. A traceback that reaches a
    browser is an information leak and a confusing error in one.
    """
    import re as _r
    s = str(line or "")
    # Windows/posix paths with a drive or home dir.
    s = _r.sub(r"[A-Za-z]:\\[^\s\"']*", "<path>", s)
    s = _r.sub(r"/home/[^\s\"']*", "<path>", s)
    s = _r.sub(r"/Users/[^\s\"']*", "<path>", s)
    s = _r.sub(r"C:\\\\Users\\\\[^\\s\"']*", "<path>", s)
    # Python frame headers: keep the exception line, drop the frames.
    s = _r.sub(r'File "<path>", line \d+, in \S+', "", s)
    s = _r.sub(r'File "[^"]*", line \d+, in \S+', "", s)
    # Local network addresses.
    s = _r.sub(r"127\.0\.0\.1(:\d+)?", "<loopback>", s)
    s = _r.sub(r"localhost(:\d+)?", "<loopback>", s)
    # Collapse the whitespace the removals leave behind.
    s = _r.sub(r"[ \t]{2,}", " ", s).strip(" |,;")
    return s[:300] if s else "(server error; see local logs)"
@app.get("/api/events")
async def api_events(limit: int = 200, level: str = "all", since: float = 0):
    """Mission-console feed: recent run sessions + scheduler jobs + server
    log tails, merged newest-first. Read-only; safe to poll every few seconds.
    Levels: run | sched | tool | info | err (or all).
    """
    import time as _t
    from datetime import datetime as _dt
    evs: list[dict] = []
    def _push(t, lv, src, msg):
        try:
            t = float(t or 0)
        except Exception:
            t = 0
        iso = (_dt.fromtimestamp(t).strftime("%H:%M:%S")
               if t > 0 else "—")
        if t <= 0:
            # An unreadable timestamp (a session with no graph.json, a
            # corrupt next_fire) used to be stamped "now", pinning the
            # entry to the TOP of the feed indefinitely. Unknown is
            # not recent — sort it with the oldest.
            t = 1.0
        evs.append({"t": t,
                    "iso": iso,
                    "level": lv, "src": src, "msg": str(msg)[:300]})
    # 1. run sessions (newest first). Session ids are s8-<random hex>,
    # so reversed-LEXICOGRAPHIC order is unrelated to recency — the
    # same bug /api/sessions already fixed (see its comment); this
    # feed was missed and showed a pseudo-random 40-session subset.
    from persistence import SESSIONS_ROOT as _SESS_ROOT
    def _sid_mtime(s: str) -> float:
        try:
            return (_SESS_ROOT / s).stat().st_mtime
        except OSError:
            return 0.0
    try:
        from persistence import SessionStore, list_sessions as _list_sids
        for sid in sorted(_list_sids(), key=_sid_mtime,
                          reverse=True)[:40]:
            try:
                store = SessionStore(sid)
                q = ""
                try:
                    q = (store.read_query() or "")[:140]
                except Exception:
                    pass
                statuses: dict[str, int] = {}
                try:
                    payload = json.loads(store.graph_path.read_text(encoding="utf-8"))
                    for _n in payload.get("nodes", []):
                        _st = (_n.get("status") or "?")
                        statuses[_st] = statuses.get(_st, 0) + 1
                except Exception:
                    pass
                try:
                    mt = store.graph_path.stat().st_mtime if store.graph_path.exists() else 0
                except Exception:
                    mt = 0
                lv = "run"
                if any(k in statuses for k in ("error", "failed")):
                    lv = "err"
                _bits = ", ".join(f"{v}→{k}"
                                  for k, v in sorted(statuses.items()))
                _push(mt, lv, f"run·{sid[:8]}",
                      f"{q or '(no query)'} — {_bits}"
                      if _bits else f"{q or '(no query)'} — no nodes")
            except Exception:
                continue
    except Exception:
        pass
    # 2. scheduler jobs (next firing), capped at 40 and sorted by
    # firing time. Every armed job was pushed uncapped, so with many
    # armed jobs the newest-200 window filled with sched lines and
    # run/error events silently dropped out of the feed. A corrupt
    # next_fire sorts last and renders as "—", not as "now".
    try:
        _sched = json.loads((STATE_DIR / "schedules.json").read_text(encoding="utf-8-sig"))
        _items = _sched.values() if isinstance(_sched, dict) else _sched
        _armed: list[tuple[float, dict]] = []
        for j in _items:
            if not isinstance(j, dict) or not j.get("enabled", True):
                continue
            try:
                _armed.append((float(j.get("next_fire") or 0), j))
            except (TypeError, ValueError):
                _armed.append((float("inf"), j))
        _armed.sort(key=lambda pair: pair[0])
        for _fire, j in _armed[:40]:
            _push(0 if _fire == float("inf") else _fire, "sched",
                  "scheduler",
                  f"next: {(j.get('query') or '')[:120]} "
                  f"({j.get('when') or j.get('recurring') or '?'})")
    except Exception:
        pass
    # 3. server log tails (filter uvicorn noise). The logs are teed from a
    # colour-enabled terminal, so strip ANSI before matching, and promote
    # tracebacks on stdout to the err level the feed filters on.
    import re as _re
    _ANSI = _re.compile(r"\x1b\[[0-9;]*m")
    _UVICORN_NOISE = ('"GET /', '"POST /', '"DELETE /', '200 OK', '404 Not Found',
                      '307 Temporary Redirect', 'Started server process',
                      'Waiting for application startup', 'Application startup complete',
                      'Uvicorn running on', '"GET /api/events', '"GET /assets/')
    _ERR_KW = ('ERROR', 'Traceback (most recent call last)', 'CRITICAL',
               'Exception')
    # Uvicorn logs its ACCESS log to stderr, so agent.err is mostly successful
    # HTTP traffic. Classifying every line of that file as `err` is what put
    # 77 red "ERRORS - need attention" on the Console page while the visible
    # feed was nothing but `INFO ... 200 OK`. The file a line came from says
    # where it was WRITTEN, not whether it is a failure; the content has to
    # decide. These markers identify access-log noise that is neither.
    _ACCESS_KW = ('HTTP Request:', '"GET ', '"POST ', '"PUT ', '"DELETE ',
                  '200 OK', '201 Created', '204 No Content', '307 Temporary')
    for _name, _lv in (("agent.out", "info"), ("agent.err", "err")):
        try:
            _p = ROOT / "logs" / _name
            if not _p.exists():
                continue
            _lines = _p.read_text(encoding="utf-8", errors="replace").splitlines()[-150:]
            _mt = _p.stat().st_mtime
            for _i, (_ln, _absorbed) in enumerate(_fold_log_block(_lines)):
                _ln = _ANSI.sub("", _ln).strip()
                if not _ln:
                    continue
                if any(_noise in _ln for _noise in _UVICORN_NOISE):
                    continue
                # A success is never an error, whatever file it landed in. An
                # error keyword still wins, so a 500 that happens to carry a
                # path with "200" in it is not silently downgraded.
                if any(_k in _ln for _k in _ERR_KW):
                    _l = "err"
                elif any(_a in _ln for _a in _ACCESS_KW):
                    _l = "info"
                elif _absorbed:
                    # Continuation of a traceback the loop already promoted.
                    _l = "err"
                elif "tool" in _ln.lower():
                    _l = "tool"
                else:
                    _l = "info"
                # Scrub server internals before serving. Raw log lines carry
                # file paths, frame names and full tracebacks - a probe
                # captured `Traceback (most recent call last): |
                # RuntimeError: No response returned (+61 lines)` straight from
                # this feed. Keep the exception TYPE (that is what an operator
                # acts on) and drop everything that names where we live.
                _ln = _scrub_log_line(_ln)
                _push(_mt - (len(_lines) - _i) * 0.01, _l,
                      "server", _ln)
        except Exception:
            continue
    evs.sort(key=lambda e: e["t"], reverse=True)
    if since > 0:
        evs = [e for e in evs if e["t"] > since]
    if level != "all":
        evs = [e for e in evs if e["level"] == level]
    limit = max(1, min(limit, 500))
    return {"events": evs[:limit], "count": len(evs)}
# ── browser screenshot artifacts ────────────────────────────────────────────
# The Browser skill saves per-turn screenshots under
# state/sessions/<sid>/browser/<layer>/turn_XX_*.png. This endpoint serves
# them so the chat UI can render the websites the agent visited. Path is
# confined to the session's browser artifact dir (no traversal outside it).
@app.get("/api/artifacts/{session_id}/{path:path}")
async def browser_artifact(session_id: str, path: str):
    from fastapi.responses import FileResponse
    from pathlib import Path as _P
    import re as _re
    if not _re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,64}$", session_id or ""):
        return JSONResponse(status_code=400, content={"error": "invalid session_id"})
    root = (_P(ROOT) / "state" / "sessions" / session_id / "browser").resolve()
    target = (root / path).resolve()
    # Block any path that escapes the session's browser artifact dir.
    if not str(target).startswith(str(root)):
        return JSONResponse(status_code=400, content={"error": "invalid path"})
    if not target.is_file():
        return JSONResponse(status_code=404, content={"error": "not found"})
    return FileResponse(str(target))
@app.post("/api/doc/preview")
async def api_doc_preview(request: Request):
    """Show the DOCUMENT in the console, not a description of it.
    Two callers, one route:
      * the setup panel, posting the spec the user is currently choosing -
        so switching page size or style re-renders the page immediately,
        before any model call is spent;
      * the result pane, posting the delivered `artifact` id - so the preview
        is rasterised from the exact bytes the download button returns.
    Proxying rather than rendering here keeps one implementation: the
    gateway owns the generators, so it owns the preview of them.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400,
                            content={"error": "invalid JSON body"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be an object"})
    art = str(body.get("artifact") or "")
    if art:
        import re as _re3
        if not _re3.fullmatch(r"art:[0-9a-fA-F]{16}", art):
            return JSONResponse(status_code=400,
                                content={"error": "malformed artifact id"})
    import httpx as _hx
    try:
        import gateway as _gwmod
        base = _gwmod.GATEWAY_URL
    except Exception:
        base = _GW_NOTIFY
    # Previewing a DELIVERED file means previewing its exact bytes, and this
    # process is the artifact store. So the bytes travel with the request
    # rather than the gateway guessing from a spec it never saw.
    art = str(body.get("artifact") or "")
    if art and not body.get("blob_b64"):
        import base64 as _b64
        import artifacts as _arts
        try:
            body = dict(body)
            body["blob_b64"] = _b64.b64encode(_arts.get_bytes(art)).decode()
            if not body.get("format"):
                meta = _arts.get_meta(art)
                _c = getattr(meta, "content_type", "") or ""
                body["format"] = {
                    "application/pdf": "pdf",
                    "application/vnd.openxmlformats-officedocument."
                    "presentationml.presentation": "pptx",
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document": "docx",
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet": "xlsx"}.get(_c, "")
        except Exception as e:
            return JSONResponse(status_code=404, content={
                "error": f"artifact unavailable: {type(e).__name__}"})
    try:
        with _hx.Client(timeout=120, follow_redirects=True,
                        headers=_gw_auth_headers()) as c:
            r = c.post(f"{base}/v1/docgen/preview", json=body)
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "error": f"preview unreachable: {type(e).__name__}: {e}"[:200]})
    if r.status_code >= 400:
        try:
            detail = r.json().get("error") or r.text[:200]
        except Exception:
            detail = r.text[:200]
        return JSONResponse(status_code=r.status_code,
                            content={"error": str(detail)[:300]})
    return Response(content=r.content, media_type="application/json")
@app.get("/api/artifact/{artifact_id}")
async def get_artifact(artifact_id: str, download: int = 1):
    """Fetch a stored artifact (a rendered document, a fetched page).
    `artifacts.put` is content-addressed, so the handle in a run's output is
    stable and safe to hand back to the browser. `download=1` sends it as an
    attachment with the descriptor-derived filename; `download=0` serves it
    inline for previewing."""
    import re as _re
    if not _re.match(r"^art:[0-9a-fA-F]{16}$", artifact_id or ""):
        return JSONResponse(status_code=400, content={"error": "malformed artifact id"})
    import artifacts as _arts
    try:
        data = _arts.get_bytes(artifact_id)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": "no such artifact"})
    try:
        meta = _arts.get_meta(artifact_id)
        ctype = meta.content_type or "application/octet-stream"
        desc = (getattr(meta, "descriptor", "") or "").strip()
    except Exception:
        ctype, desc = "application/octet-stream", ""
    headers = {"X-Artifact-Bytes": str(len(data))}
    ext = {"application/pdf": ".pdf",
           "application/vnd.openxmlformats-officedocument."
           "presentationml.presentation": ".pptx",
           "application/vnd.openxmlformats-officedocument."
           "wordprocessingml.document": ".docx",
           "application/vnd.openxmlformats-officedocument."
           "spreadsheetml.sheet": ".xlsx"}.get(ctype, "")
    if download:
        # Build a header-safe filename. The descriptor is agent-written free
        # text — parentheses, colons, quotes and non-ASCII in it produced a
        # Content-Disposition Chrome refuses, and the download silently
        # CANCELLED instead of failing loudly.
        import re as _re2
        from urllib.parse import quote as _quote
        raw = (desc or "").split("(")[0].strip() or artifact_id.replace(":", "")
        # `filename*` is what Chrome and Firefox actually use, and it was
        # being built from `raw` — which has the extension stripped off by the
        # `split("(")` above and often has none to begin with. Every direct
        # save therefore landed as a file with NO extension, even though the
        # response's content-type was application/pdf. Append the extension
        # from the content type, unconditionally, before quoting.
        if ext and not raw.lower().endswith(ext.lower()):
            raw = raw + ext
        # RFC 5987 for anything non-ASCII; ASCII fallback is stripped to
        # characters that are legal inside a quoted-string.
        ascii_name = _re2.sub(r"[^A-Za-z0-9 ._-]+", " ", raw)
        ascii_name = _re2.sub(r"\s+", " ", ascii_name).strip(" .") or "document"
        # Test the EXTENSION, not the first period: a title like
        # "PostgreSQL vs. MySQL" contains a period, so `if "." not in name`
        # was False and the file downloaded with no extension at all.
        if ext and not ascii_name.lower().endswith(ext.lower()):
            ascii_name += ext
        headers["Content-Disposition"] = (
            f"attachment; filename=\"{ascii_name}\"; "
            f"filename*=UTF-8''{_quote(raw)}")
    return Response(content=data, media_type=ctype, headers=headers)
_SETTLED = ("complete", "completed", "failed", "error", "skipped")
def _run_is_live(statuses: list[str]) -> bool:
    """Is work still in flight for this session?
    A `pending` node on its own does NOT mean work is in flight. A retry loop
    that gives up abandons a formatter node and it stays `pending` forever, so
    deriving `live` from `pending` alone wedged `live: true` on runs that had
    already finished and already had their file: the client then polled for 30
    minutes and never adopted a document that was sitting there waiting. Work
    is live while a node is `running`/`queued`, or while nothing has settled
    yet and a node is still waiting to start.
    """
    s = [str(x or "").lower() for x in statuses]
    if any(x in ("running", "queued") for x in s):
        return True
    if any(x in _SETTLED for x in s):
        return False
    return any(x == "pending" for x in s)
@app.get("/api/sessions/{session_id}/produced")
async def session_produced(session_id: str):
    """What a run has produced SO FAR: rendered files, the final answer and
    whether it is still going.
    The Authoring view needs this to REATTACH to a run the user walked away
    from. A research/authoring run keeps executing server-side after the
    browser closes the stream, and the console showed nothing at all on
    return - the whole run, including a finished file, was unrecoverable from
    that view. This is the poll target that makes "leave it running" real.
    """
    from persistence import SessionStore
    try:
        store = SessionStore(session_id, create=False)
        g = store.read_graph()
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "invalid session_id"})
    except Exception:
        g = None
    if g is None:
        return JSONResponse(status_code=404, content={"error": "no such session"})
    from flow import take_produced_files, read_run_status
    files = take_produced_files(session_id)
    live = _run_is_live([str(d.get("status", "")) for _, d in g.nodes(data=True)])
    # The final answer, once a formatter has produced one.
    answer = ""
    for _, d in reversed(list(g.nodes(data=True))):
        out = getattr(d.get("result"), "output", None)
        if isinstance(out, dict) and isinstance(out.get("final_answer"), str):
            answer = out["final_answer"]
            break
    # How the run ENDED. The answer recovered above is the formatter node's own
    # output, which never carries the "this answer is incomplete" warning or
    # the operator's stop notice - both existed only in the SSE frame that
    # delivered them once. Reopening a finished run therefore showed a
    # complete-looking report with no hint that a branch was missing.
    status = read_run_status(session_id)
    degraded = bool(status.get("degraded"))
    detail = [str(x) for x in (status.get("detail") or [])]
    stopped = any(str(d.get("status", "")).lower() == "skipped"
                  for _, d in g.nodes(data=True)) and not answer
    return {"session_id": session_id, "files": files, "answer": answer,
            "live": live, "degraded": degraded, "incomplete_detail": detail,
            "stopped": bool(stopped),
            "nodes": [{"id": nid, "skill": d.get("skill", ""),
                       "status": d.get("status", "")}
                      for nid, d in g.nodes(data=True)]}
@app.get("/api/artifact/{artifact_id}/preview")
async def preview_artifact(artifact_id: str):
    """Describe a rendered document's actual contents, for the console's
    preview pane.
    The run's own `sections`/`slides` metadata is model-written and was
    repeatedly wrong — a deck reported 10 slide titles and shipped a file
    with 6 completely empty slides. Reading the artifact is the only honest
    source, and it lets the UI warn about an empty workbook or a deck of
    blank slides BEFORE the user downloads and opens it.
    """
    import re as _re
    if not _re.match(r"^art:[0-9a-fA-F]{16}$", artifact_id or ""):
        return JSONResponse(status_code=400, content={"error": "malformed artifact id"})
    import artifacts as _arts
    try:
        data = _arts.get_bytes(artifact_id)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": "no such artifact"})
    try:
        ctype = (_arts.get_meta(artifact_id).content_type
                 or "application/octet-stream")
    except Exception:
        ctype = "application/octet-stream"
    import docpreview as _dp
    return _dp.preview(data, ctype)
# ── health ──────────────────────────────────────────────────────────────────
_GW_UP = {"up": False, "at": 0.0, "busy": False}
def _gateway_up_cached(ttl: float = 30.0) -> bool:
    """Gateway liveness without stalling the UI.
    A synchronous probe costs up to ~2s (gateway cold hit), and every
    console page polls /api/health on load — that was the "slow
    navigation" feel. So: serve the last-known value instantly and
    refresh it in a background thread at most once per `ttl` seconds.
    """
    import time as _t
    import threading as _th
    now = _t.time()
    if now - _GW_UP["at"] > ttl and not _GW_UP["busy"]:
        _GW_UP["busy"] = True
        def _probe():
            try:
                from gateway import _is_up
                _GW_UP["up"] = bool(_is_up())
            except Exception:
                _GW_UP["up"] = False
            finally:
                _GW_UP["at"] = _t.time()
                _GW_UP["busy"] = False
        _th.Thread(target=_probe, daemon=True).start()
    return _GW_UP["up"]
_gateway_up_cached()  # warm the cache in background while the server boots
@app.get("/api/health")
async def health():
    """Liveness probe for the UI status dot.
    Reports the agent as 'ready' unconditionally (the agent process is up
    and able to serve requests). `gateway_up` is checked best-effort: if the
    gateway client import fails (e.g. Python 3.8 syntax in llm_gatewayV9/
    client.py), we report `gateway_up: false` instead of 500-ing the whole
    endpoint — the UI just shows "ready (gateway starting)" and the first
    /api/chat call will trigger the warmup.
    """
    gateway_up = False
    try:
        gateway_up = _gateway_up_cached()
    except Exception:
        gateway_up = False
    # `spa_built` exists so the documented health check can also assert the
    # React bundle is being served. Without it, a machine (or CI) with no
    # `dist/` silently gets the legacy vanilla UI at every route — a
    # different application that still "works".
    return {"agent": "ready", "gateway_up": gateway_up,
            "spa_built": _SPA_INDEX.exists()}
@app.get("/api/cost")
async def cost_dashboard(session: str | None = None, agent: str | None = None,
                        conversation_id: str | None = None):
    """Surface per-agent LLM cost + token usage for the current chat thread.
    The Spend panel sums the per-turn cost ledger (state/sessions/<sid>/
    turn_costs.json), which accumulates exactly the per-response deltas shown
    beside each message — NOT the raw gateway session total. The gateway
    session total is the lifetime of the whole conversation (session_id is
    reused per turn), so it would otherwise show accumulated spend from every
    prior turn. If the ledger is empty (e.g. a brand-new thread), we fall back
    to the gateway's per-session delta so the panel still shows something.
    Scoping: if `conversation_id` is given, resolve it to its session_id so the
    panel is scoped to the current chat thread.
    """
    # Resolve conversation_id → session_id so the spend panel is scoped to
    # the current chat thread, not every session that touched the gateway.
    # Chat threads (ct-*) ARE already their own gateway session and keep
    # their ledger under threads/ — routing them through resolve_session
    # would mint a phantom s8 id in conversations.json with no run behind it.
    # create=False: this is a GET, so an unknown id must NOT mint a
    # phantom conversations.json entry (a typo'd id from a bookmarked
    # URL used to spawn one on every page load, forever).
    unknown_conv = False
    if conversation_id:
        if conversation_id.startswith("ct-"):
            session = conversation_id
        else:
            session = resolve_session(conversation_id, create=False)
            if session is None and conversation_id in _chat_threads_load():
                # A stored lightweight thread (AG-UI thread ids,
                # client-chosen ids) keeps its ledger under
                # threads/ — /api/cost/by_skill reads it there,
                # so the two panels disagreed for the same
                # conversation (one showed spend, the other
                # "unknown"). Read it here too.
                session = conversation_id
        if not session:
            unknown_conv = True
    if not session:
        return {"rows": [], "totals": {"in_tokens": 0, "out_tokens": 0,
                                       "dollars": 0.0, "calls": 0},
                "unknown_conversation": unknown_conv}
    try:
        turns = _read_turn_costs(session)
    except ValueError:
        # The bare `session` query param bypasses the
        # conversation_id resolution above, so a path-shaped value
        # reached `_turn_cost_path` unvalidated and its ValueError
        # surfaced as a bare 500 on a panel the UI polls.
        return JSONResponse(status_code=400,
                            content={"error": "invalid session id"})
    if conversation_id and conversation_id != session:
        # Merge the thread's own ledger when it differs from the resolved
        # session, so the panel isn't blank for chat-only threads.
        # Lightweight threads keep their ledger under threads/: ct-* chat
        # threads and AG-UI thread ids (agui-*, client uuids). A c-*
        # conversation resolves to s8-* (already read above), and reading
        # sessions/c-*/… would be a phantom path that can never exist —
        # so merge only ids that are stored chat threads.
        if (conversation_id.startswith("ct-")
                or conversation_id in _chat_threads_load()):
            turns = turns + _read_turn_costs(conversation_id)
            turns.sort(key=lambda _e: _e.get("ts", 0) if isinstance(_e, dict) else 0)
    if not turns:
        # Fallback: no ledger yet (first turn, or legacy session). Use the
        # gateway's current per-session total as a one-shot snapshot.
        try:
            bd = _session_cost_breakdown(session)
            turns = [{"per_agent": bd, "totals": {
                "usd": round(sum(d["usd"] for d in bd.values()), 6),
                "in_tok": sum(d["in_tok"] for d in bd.values()),
                "out_tok": sum(d["out_tok"] for d in bd.values()),
                "calls": sum(d["calls"] for d in bd.values()),
            }}]
        except Exception:
            turns = []
    # Aggregate per-agent across all turns in this thread.
    agg: dict[str, dict] = {}
    tot_in = tot_out = tot_calls = 0
    tot_usd = 0.0
    for _t in turns:
        for _ag, _d in (_t.get("per_agent") or {}).items():
            _a = agg.setdefault(_ag, {"usd": 0.0, "in_tok": 0,
                                      "out_tok": 0, "calls": 0})
            _a["usd"] += _d.get("usd", 0.0)
            _a["in_tok"] += _d.get("in_tok", 0)
            _a["out_tok"] += _d.get("out_tok", 0)
            _a["calls"] += _d.get("calls", 0)
        tot_in += _t.get("totals", {}).get("in_tok", 0)
        tot_out += _t.get("totals", {}).get("out_tok", 0)
        tot_calls += _t.get("totals", {}).get("calls", 0)
        tot_usd += _t.get("totals", {}).get("usd", 0.0)
    rows = [
        {"agent": ag, "in_tokens": d["in_tok"], "out_tokens": d["out_tok"],
         "dollars": round(d["usd"], 6), "calls": d["calls"]}
        for ag, d in agg.items()
    ]
    rows.sort(key=lambda x: x["dollars"], reverse=True)
    return {
        "rows": rows,
        "totals": {
            "in_tokens": tot_in,
            "out_tokens": tot_out,
            "dollars": round(tot_usd, 6),
            "calls": tot_calls,
        },
        "unknown_conversation": unknown_conv,
    }
_FAILURE_REASONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Ordered most-specific first: the first pattern that matches wins, so a
    # message naming two things is filed under the one that actually stopped
    # the node.
    # The empty/prose split is deliberate and must stay above the generic
    # "no usable json" bucket: skills.py:2413 goes to great lengths to
    # distinguish them, because they have different fixes (an upstream
    # completion problem vs a prompt/format problem). Collapsing them hid a
    # 52/13 split behind a single row, so the first pattern to match must be
    # the specific one.
    ("provider returned an empty reply", ("no usable json: empty reply",)),
    ("reply was prose, not the required JSON",
     ("no usable json: reply was not json",)),
    ("reply not parseable as the required JSON", ("no usable json",)),
    # A renderer crash is OUR bug, not the model's behaviour, and the two have
    # opposite fixes. Without this bucket a live
    # `NameError: name 'title' is not defined` in the spec-fallback render
    # path was reported to the user as "claimed a document but never called
    # render_document" - blaming the model for our NameError.
    ("document renderer failed (agent bug, not the model)",
     ("render_document failed", "spec fallback render failed",
      "renderer failed", "nameerror", "attributeerror")),
    ("claimed a document but never called render_document",
     ("without calling render_document", "no file was rendered",
      "never called render_document")),
    ("tool-use hop cap reached", ("hop cap",)),
    ("verifier stall: identical tool call repeated", ("verifier stall",)),
    ("MCP unavailable / circuit breaker open",
     ("mcpunavailable", "breaker")),
    ("timed out", ("timed out", "timeout")),
    ("gateway refused or unavailable",
     ("connecterror", "connection refused", "502", "503")),
)
def _classify_node_failure(err: str) -> str:
    """One honest line per failure, so the Ledger can group them.
    Free-text errors are useless in aggregate: 65 author failures looked like
    65 separate problems until they were bucketed, and they were three.
    """
    e = (err or "").lower()
    for label, needles in _FAILURE_REASONS:
        if any(n in e for n in needles):
            return label
    head = " ".join(str(err or "").split())[:70]
    return head or "(no error text)"
@app.get("/api/nodes/health")
async def nodes_health(limit: int = 200, conversation_id: str | None = None):
    """Node reliability by skill: how many failed, why, and what share of that
    skill's nodes that is.
    The Ledger showed spend but nothing about reliability, so a run that lost a
    researcher and one that lost three looked identical. `limit` bounds how
    many session graphs are parsed, because each row is a whole graph.json.
    """
    limit = max(1, min(int(limit or 200), 500))
    from persistence import SessionStore, SESSIONS_ROOT as _SESS_ROOT
    from persistence import list_sessions as persistence_list_sids
    sids: list[str] = []
    # A caller-supplied id must be a plain session id. Without this check
    # `conversation_id=../../..` resolved to the repo root, `is_dir()` was
    # true, and the endpoint reported `sessions: 1` - a phantom run examined.
    # Same rule the session routes use.
    def _plain(s: str) -> bool:
        return bool(s) and not s.startswith(".") and "/" not in s \
            and "\\" not in s and len(s) <= 128 \
            and all(ord(ch) >= 0x20 and ord(ch) != 0x7F for ch in s)
    if conversation_id:
        convs = _conv_load()
        inv = {v: k for k, v in convs.items()}
        sid = inv.get(conversation_id) or conversation_id
        if isinstance(sid, str) and _plain(sid) and (_SESS_ROOT / sid).is_dir():
            sids = [sid]
    else:
        def _mtime(sid: str) -> float:
            try:
                return (_SESS_ROOT / sid).stat().st_mtime
            except OSError:
                return 0.0
        sids = sorted(persistence_list_sids(), key=_mtime, reverse=True)[:limit]
    per: dict[str, dict] = {}
    # A graph that cannot be parsed is not a run with zero nodes. The endpoint
    # used to `continue` silently, so a corrupt graph.json left the count of
    # sessions examined claiming it had been read, and with EVERY graph
    # unreadable the response was `{"sessions": N, "rows": []}` - which the UI
    # renders as "No completed nodes in the recent runs", the exact
    # "nothing happened" vs "could not tell" conflation the rest of the page
    # goes to lengths to avoid.
    unreadable = 0
    for sid in sids:
        try:
            g = SessionStore(sid, create=False).read_graph()
        except Exception:
            unreadable += 1
            continue
        if g is None:
            continue
        nodes = getattr(g, "nodes", None)
        try:
            items = list(nodes(data=True)) if callable(nodes) else list(nodes)
        except Exception:
            unreadable += 1
            continue
        for _nid, d in items:
            skill = str(d.get("skill") or "?")
            slot = per.setdefault(skill, {
                "skill": skill, "nodes": 0, "attempted": 0, "failed": 0,
                "skipped": 0, "never_run": 0, "reasons": {},
            })
            slot["nodes"] += 1
            st = str(d.get("status") or "").lower()
            if st == "skipped":
                slot["skipped"] += 1
            # A node that never ran cannot have failed, so it must not sit in
            # the denominator. Including `pending` and `interrupted` made the
            # failure rate look better than the truth: formatter showed
            # "256 nodes, 0 failed, 0.0%" while 90 of those nodes had never
            # been dispatched at all.
            if st in ("failed", "error", "complete"):
                slot["attempted"] += 1
            elif st in ("pending", "interrupted"):
                slot["never_run"] += 1
            if st in ("failed", "error"):
                slot["failed"] += 1
                res = d.get("result")
                reason = _classify_node_failure(
                    getattr(res, "error", "") if res is not None else "")
                slot["reasons"][reason] = slot["reasons"].get(reason, 0) + 1
    rows = []
    for slot in per.values():
        # Denominator is nodes ATTEMPTED. `or 1` keeps the division safe when
        # a skill's every node was never run, and the row then reports
        # fail_pct 0.0 alongside a never_run count that says why.
        n = slot["attempted"] or 1
        failed = slot["failed"]
        reasons = sorted(
            ({"reason": k, "count": v,
              "pct_of_failures": round(v / max(1, failed) * 100, 1)}
             for k, v in slot["reasons"].items()),
            key=lambda r: -r["count"])
        rows.append({
            **slot,
            "fail_pct": round(failed / n * 100, 1),
            "reasons": reasons,
        })
    rows.sort(key=lambda r: (-r["fail_pct"], -r["nodes"]))
    return {
        "sessions": len(sids),
        # >0 means the table below is a partial view, not the whole truth.
        "unreadable": unreadable,
        "rows": rows,
        "totals": {
            "nodes": sum(r["nodes"] for r in rows),
            "attempted": sum(r["attempted"] for r in rows),
            "failed": sum(r["failed"] for r in rows),
            "fail_pct": round(
                sum(r["failed"] for r in rows)
                / max(1, sum(r["attempted"] for r in rows)) * 100, 1),
        },
    }
@app.get("/api/cost/by_skill")
async def cost_by_skill(conversation_id: str | None = None):
    """Per-skill spend rollup for the Ledger page. Scoped to one conversation
    when given, else summed across all sessions' turn ledgers."""
    sids: list[str] = []
    unknown_conv = False
    if conversation_id:
        try:
            # ct-* threads have no graph session to resolve — resolve_session
            # would only mint a phantom s8 id. They are keyed directly below.
            # create=False: a GET must not mint a phantom conversations.json
            # entry for an unknown id (a bookmarked typo used to spawn one
            # per page load, forever).
            if not conversation_id.startswith("ct-"):
                sid = resolve_session(conversation_id, create=False)
                if sid is None:
                    unknown_conv = True
                else:
                    sids = [sid]
        except Exception:
            sids = []
    else:
        try:
            from persistence import list_sessions as _list_sids
            sids = list(_list_sids())
        except Exception:
            sids = []
    agg: dict[str, dict] = {}
    # Lightweight chat threads (ct-*) keep a ledger under threads/ (no graph
    # dir). Include them, or the Ledger silently omits the default chat path.
    keys = list(sids)
    if conversation_id:
        # A path-shaped id (or a null byte in one) makes
        # `_turn_cost_path` raise ValueError, which used to 500 the
        # whole rollup. Refuse it plainly instead.
        try:
            _turn_cost_path(conversation_id)
        except ValueError:
            return JSONResponse(status_code=400, content={
                "error": "invalid conversation id"})
    try:
        if conversation_id:
            if conversation_id not in keys:
                keys.append(conversation_id)
        else:
            for _cid in _chat_threads_load():
                if _cid not in keys:
                    keys.append(_cid)
    except Exception:
        pass
    for _sid in keys:
        try:
            _turns = _read_turn_costs(_sid)
        except ValueError:
            # A stored key that fails the shape check (pollution from
            # before the boundary validation) must not 500 every
            # ledger read — skip it.
            continue
        for _t in _turns:
            for _ag, _d in (_t.get("per_agent") or {}).items():
                _a = agg.setdefault(_ag, {"usd": 0.0, "in_tok": 0,
                                          "out_tok": 0, "calls": 0})
                _a["usd"] += _d.get("usd", 0.0) or 0.0
                _a["in_tok"] += _d.get("in_tok", 0) or 0
                _a["out_tok"] += _d.get("out_tok", 0) or 0
                _a["calls"] += _d.get("calls", 0) or 0
    rows = [{"skill": ag, "in_tokens": d["in_tok"], "out_tokens": d["out_tok"],
             "dollars": round(d["usd"], 6), "calls": d["calls"]}
            for ag, d in agg.items()]
    rows.sort(key=lambda x: x["dollars"], reverse=True)
    # Research runs bill through the gateway's per-session totals, not
    # this turn ledger: a DAG run's spend only lands in
    # sessions/<sid>/turn_costs.json when the run finishes cleanly, so a
    # completed research conversation reported $0 / "No spend yet" here
    # while /api/cost showed real tokens. Same one-shot fallback
    # /api/cost uses. Scoped requests only — an all-time rollup must not
    # call the gateway once per session.
    if not agg and conversation_id:
        try:
            _sid = resolve_session(conversation_id, create=False)
        except Exception:
            _sid = None
        if _sid:
            try:
                bd = _session_cost_breakdown(_sid)
                for _ag, _d in (bd or {}).items():
                    agg[_ag] = {"usd": _d.get("usd", 0.0) or 0.0,
                                "in_tok": _d.get("in_tok", 0) or 0,
                                "out_tok": _d.get("out_tok", 0) or 0,
                                "calls": _d.get("calls", 0) or 0}
            except Exception:
                pass
    turns = []
    for _sid in keys[:200]:
        # Same guard as the rows loop above. Without it a single
        # path-shaped key in chat_threads.json (a stored conversation
        # id like "../..") raised ValueError out of _turn_cost_path and
        # 500'd the whole all-time rollup on every 15s poll.
        try:
            _sid_turns = _read_turn_costs(_sid)
        except ValueError:
            continue
        for _t in _sid_turns[-50:]:
            _tt = _t.get("totals") or {}
            turns.append({"ts": _t.get("ts", 0), "session": _sid,
                          "query": (_t.get("query") or "")[:120],
                          "calls": _tt.get("calls", 0),
                          "usd": round(_tt.get("usd", 0.0), 6)})
    turns.sort(key=lambda x: x["ts"], reverse=True)
    return {"rows": rows,
            "turns": turns[:200],
            "unknown_conversation": unknown_conv,
            "totals": {"dollars": round(sum(r["dollars"] for r in rows), 6),
                       "calls": sum(r["calls"] for r in rows),
                       "skills": len(rows)}}
@app.get("/api/audit")
async def audit_export(redact: bool = True, limit: int = 0):
    """Export the computer-use audit log with secrets redacted by default.
    Returns the redacted log lines so the dashboard / compliance tooling can
    show a safe, shareable record of every gated action.

    `limit` returns only the newest N lines. The log is append-only and
    uncapped, so a page that rendered all of it put thousands of DOM nodes
    on screen and pushed the one line the user came to read (an approval)
    out of sight. 0 keeps the whole log for compliance exports.
    """
    from computer_use import safety
    lines = safety.SafetyGates.export_audit(redact=redact)
    total = len(lines)
    if limit and limit > 0:
        lines = lines[-limit:]
    return {"lines": lines, "count": len(lines), "total": total}
# ── sessions browser (UI panel) ────────────────────────────────────────────
# A node keeps its "running" status in graph.json until the executor marks it
# terminal. If the process dies mid-run (kill -9, crash, power loss) nothing
# ever rewrites it, so the UI reported that run as "live" forever — a 13h-old
# run still carried a blue "live" pill. Any run whose graph has not been
# touched for STALE_RUN_S is reported as `interrupted` instead. This is
# read-only on purpose: it cannot corrupt a genuinely live run, and it also
# covers the window before a restart reconciles anything on disk.
STALE_RUN_S = 900.0
_LIVEISH = ("running", "live", "active", "working", "started")
def _reconcile_status(status_counts: dict, mtime: float, now: float) -> dict:
    """Relabel the live-looking nodes of a stale run as `interrupted`."""
    if not status_counts or not mtime:
        return status_counts
    if (now - mtime) <= STALE_RUN_S:
        return status_counts
    out = dict(status_counts)
    for key in list(out):
        low = str(key).lower()
        if any(tok in low for tok in _LIVEISH):
            n = out[key]
            out[key] = 0
            # Carry the node count across: status_counts is a tally, and the
            # UI sums it to decide "stopped" vs "partial" and to total nodes.
            out["interrupted"] = out.get("interrupted", 0) + (
                n if isinstance(n, (int, float)) else 1)
    return {k: v for k, v in out.items() if v}
def _stale_node_status(status: str, mtime: float, now: float) -> str:
    """Same reconciliation for a single DAG node (light graph endpoint)."""
    low = str(status or "").lower()
    if not any(tok in low for tok in _LIVEISH):
        return status
    if not mtime or (now - mtime) <= STALE_RUN_S:
        return status
    return "interrupted"
def _topic_of(query: str) -> str:
    """The user's topic, not the skill prompt that wraps it.
    query.txt holds whatever the planner handed the node. For a research run
    that is the researcher system prompt with the real question appended under
    a "Topic:" line, so Runs titled every research row with the first sentence
    of the prompt ("You are a research agent...") and the topic list could
    only recover the question by regexing it back out in the browser. The
    extraction lives here once, and the client prefers this field.
    """
    q = (query or "").strip()
    m = re.search(r"^[ \t]*Topic:[ \t]*(.+)$", q, re.MULTILINE)
    if m:
        return m.group(1).strip()[:160]
    for line in q.splitlines():
        line = line.strip()
        if line:
            return line[:160]
    return ""
def _reconcile_orphaned_runs() -> dict:
    """Mark nodes left `running` by a killed process as `interrupted`, on disk.
    Runs in a daemon thread at startup. Two guards keep it from corrupting a
    genuinely live run: it only touches graphs untouched for longer than
    STALE_RUN_S, and it re-checks that the node is still live-looking
    immediately before writing.
    """
    import time as _t
    from persistence import SessionStore, list_sessions as _list_sids
    fixed: list[str] = []
    now = _t.time()
    try:
        sids = _list_sids()
    except Exception:
        return {"reconciled": 0, "error": "cannot list sessions"}
    for sid in sids[:200]:
        try:
            store = SessionStore(sid, create=False)
            g = store.read_graph()
            if g is None:
                continue
            mtime = store.graph_path.stat().st_mtime
            if (now - mtime) <= STALE_RUN_S:
                continue          # recently written: a run may be in flight
            dirty = False
            for _nid, data in list(g.nodes(data=True)):
                status = str(data.get("status", ""))
                if not any(t in status.lower() for t in _LIVEISH):
                    continue
                # Re-check against the live file: another process may have
                # touched it between the read above and now.
                try:
                    if store.graph_path.stat().st_mtime != mtime:
                        dirty = False
                        break
                except OSError:
                    dirty = False
                    break
                data["status"] = "interrupted"
                data["interrupted_at"] = now
                dirty = True
            if dirty:
                store.write_graph(g)
                fixed.append(sid)
        except Exception:
            continue
    if fixed:
        print(f"[startup] reconciled {len(fixed)} orphaned run(s): "
              f"{', '.join(fixed[:6])}"
              f"{' …' if len(fixed) > 6 else ''}")
    return {"reconciled": len(fixed), "sessions": fixed}
@app.get("/api/sessions")
async def list_sessions(limit: int = 100):
    """List recent orchestrator sessions for the UI's Sessions panel.
    Returns a compact summary per session (id, query, node count, skills,
    status counts) so the sidebar session list and the Sessions pane can
    render without hitting the heavy graph endpoint for every row.
    `limit` is clamped like /api/events and /api/chat/threads already are:
    each row parses a whole graph.json, so `?limit=100000` was a one-request
    way to make the server parse every session ever recorded, synchronously
    on the event loop.
    """
    limit = max(1, min(limit, 500))
    from persistence import SessionStore, list_sessions as _list_sids
    from persistence import SESSIONS_ROOT as _SESS_ROOT
    import time as _t
    convs = _conv_load()
    inv = {v: k for k, v in convs.items()}
    # Newest-first by directory mtime. The raw listing is lexicographic
    # over random-hex ids, so `reversed(...)[:limit]` cut off the newest
    # sessions instead of the oldest — limit=5 could omit a run that
    # finished seconds ago.
    def _mtime(sid: str) -> float:
        try:
            return (_SESS_ROOT / sid).stat().st_mtime
        except OSError:
            return 0.0
    sids = sorted(_list_sids(), key=_mtime, reverse=True)[:limit]
    sessions = []
    for sid in sids:
        try:
            store = SessionStore(sid)
            q = ""
            try:
                q = (store.read_query() or "")[:200]
            except Exception:
                pass
            # The planner's structured plan wins over the regex: it is a
            # first-class field, and it does not break when the researcher's
            # prompt wording changes.
            plan_topic = ""
            try:
                plan_topic = str((store.read_plan() or {}).get("topic") or "")[:160]
            except Exception:
                pass
            try:
                n_nodes = sum(1 for _ in store.nodes_dir.glob("n_*.json"))
            except Exception:
                n_nodes = 0
            try:
                mtime = store.graph_path.stat().st_mtime if store.graph_path.exists() else 0
            except Exception:
                mtime = 0
            # Rollup from graph.json node attrs only (one small read).
            status_counts: dict[str, int] = {}
            skills: list[str] = []
            try:
                import networkx as _nx
                payload = json.loads(store.graph_path.read_text(encoding="utf-8"))
                if "links" in payload and "edges" not in payload:
                    payload = {**payload, "edges": payload.pop("links")}
                _g = _nx.node_link_graph(payload, directed=True)
                for _nid, _d in _g.nodes(data=True):
                    _st = _d.get("status", "?")
                    status_counts[_st] = status_counts.get(_st, 0) + 1
                    _sk = _d.get("skill", "")
                    if _sk and _sk not in skills:
                        skills.append(_sk)
            except Exception:
                pass
            # The state-file count is cheap but misses nodes that were seeded
            # and never executed (a run stopped before its first dispatch has
            # a skipped node in graph.json and zero n_*.json files). The graph
            # rollup below is already parsed, so reconcile with it.
            if status_counts:
                n_nodes = max(n_nodes, sum(status_counts.values()))
            status_counts = _reconcile_status(status_counts, mtime, _t.time())
            # Authoring or research. Both write to the same session store, so
            # without this the Authoring run showed up in the Research sidebar
            # and neither view could tell its own history from the other's - the
            # document was in the store and unreachable from either page.
            # The CALLER's own declaration wins when present (run_meta.json,
            # written at run start): it is exact, and it exists before the run
            # has emitted a single node - which is what lets a just-started
            # research topic appear in the sidebar instead of vanishing. The
            # derived kind (produced files / author skills) is the fallback for
            # sessions that predate the marker.
            try:
                _has_files = bool(
                    (_SESS_ROOT / sid / "produced_files.json").exists())
            except OSError:
                _has_files = False
            _derived = "authoring" if (
                _has_files or ({"author", "deck"} & set(skills))) else "research"
            try:
                _marker = str((store.read_run_meta() or {}).get("kind") or "")
            except Exception:
                _marker = ""
            _kind = _marker if _marker in ("research", "authoring", "chat") \
                else _derived
            sessions.append({
               "session_id": sid,
               "conversation_id": inv.get(sid),
               "query": q,
               "topic": plan_topic or _topic_of(q),
               "kind": _kind,
               "run_kind": _kind,
               "has_files": _has_files,
               "nodes": n_nodes,
               "status_counts": status_counts,
               "skills": skills[:8],
               "updated": mtime,
               "updated_ago": round(_t.time() - mtime, 1) if mtime else None,
           })
        except Exception:
            # Skip sessions that fail to load (corrupt, partially written, etc.)
            continue
    return {"sessions": sessions, "conversations": convs}
@app.get("/api/runs/summary")
async def runs_summary(limit: int = 200):
    """Runs-page rollup: per-run stats from small files only (graph.json
    attrs + turn_costs.json + query.txt) — never parses node states.
    Includes totals by status/skill plus lifetime spend for the header strip.
    Clamped for the same reason as /api/sessions: every row runs a
    networkx rollup over graph.json, and Runs polls this every 15s.
    """
    limit = max(1, min(limit, 500))
    from persistence import SessionStore, list_sessions as _list_sids
    from persistence import SESSIONS_ROOT as _SESS_ROOT
    import time as _t
    convs = _conv_load()
    inv = {v: k for k, v in convs.items()}
    runs = []
    by_status: dict[str, int] = {}
    by_skill: dict[str, int] = {}
    dollars = 0.0
    nodes_total = 0
    # How many runs exist, independent of `limit`. `totals.runs` used to be
    # len(runs), i.e. the PAGE SIZE: limit=1 reported 1 run, limit=5
    # reported 5, and the rail badge and the "N runs" footer both read it —
    # so both silently capped at the limit instead of counting anything.
    all_sids = _list_sids()
    total_runs = len(all_sids)
    # Same newest-first mtime ordering as /api/sessions (see there).
    def _mtime(sid: str) -> float:
        try:
            return (_SESS_ROOT / sid).stat().st_mtime
        except OSError:
            return 0.0
    for sid in sorted(all_sids, key=_mtime, reverse=True)[:limit]:
        try:
            store = SessionStore(sid)
            q = ""
            try:
                q = (store.read_query() or "")[:200]
            except Exception:
                pass
            plan_topic = ""
            try:
                plan_topic = str((store.read_plan() or {}).get("topic") or "")[:160]
            except Exception:
                pass
            n_nodes = 0
            status_counts: dict[str, int] = {}
            skills: list[str] = []
            try:
                import networkx as _nx
                payload = json.loads(store.graph_path.read_text(encoding="utf-8"))
                if "links" in payload and "edges" not in payload:
                    payload = {**payload, "edges": payload.pop("links")}
                _g = _nx.node_link_graph(payload, directed=True)
                n_nodes = _g.number_of_nodes()
                for _nid, _d in _g.nodes(data=True):
                    _st = _d.get("status", "?")
                    status_counts[_st] = status_counts.get(_st, 0) + 1
                    _sk = _d.get("skill", "")
                    if _sk and _sk not in skills:
                        skills.append(_sk)
            except Exception:
                pass
            usd = 0.0
            try:
                for _e in _read_turn_costs(sid):
                    usd += float((_e.get("totals") or {}).get("usd") or 0)
            except Exception:
                pass
            try:
                mtime = store.graph_path.stat().st_mtime if store.graph_path.exists() else 0
            except Exception:
                mtime = 0
            # A run with no graph still has an age. `mtime` deliberately stays
            # 0 so `_reconcile_status` keeps treating the graph as absent, but
            # the DISPLAY timestamp falls back to the session directory mtime.
            # Reporting `updated: 0` made the client print "0s ago" for runs
            # that were up to 119 hours old (measured), and because the list
            # sorts by directory mtime while the pill used graph mtime, those
            # rows sorted to the very top of the page claiming "queued".
            updated_at = mtime
            if not updated_at:
                try:
                    updated_at = (_SESS_ROOT / sid).stat().st_mtime
                except OSError:
                    updated_at = 0.0
            for _st, _n in status_counts.items():
                by_status[_st] = by_status.get(_st, 0) + _n
            for _sk in skills:
                by_skill[_sk] = by_skill.get(_sk, 0) + 1
            dollars += usd
            nodes_total += n_nodes
            runs.append({
                "session_id": sid,
                "conversation_id": inv.get(sid),
                "query": q,
                "topic": plan_topic or _topic_of(q),
                "nodes": n_nodes,
                "status_counts": _reconcile_status(status_counts, mtime, _t.time()),
                "skills": skills[:8],
                "usd": round(usd, 6),
                "updated": updated_at,
                "updated_ago": round(_t.time() - updated_at, 1) if updated_at else None,
                # False means "there is no graph to show", which is a different
                # thing from a run that is queued. Nine of the graph-less rows
                # carry run_status.json with degraded=true - real failed runs
                # that the client was labelling as fresh queued work.
                "has_graph": bool(mtime),
            })
        except Exception:
            continue
    return {"runs": runs,
            "total_runs": total_runs,
            "totals": {"runs": total_runs, "nodes": nodes_total,
                       "dollars": round(dollars, 6),
                       "page_runs": len(runs),
                       "by_status": by_status, "by_skill": by_skill}}
@app.get("/api/sessions/{session_id}/graph")
async def session_graph(session_id: str, light: bool = False):
    """Return the full skill DAG + node detail for one session.
    Used by the UI's "Skill DAG" panel to render the live execution graph
    after each turn. Falls back to an empty graph if the session is unknown
    or the orchestrator can't reconstruct it.
    `light=true` skips node states (some nodes are megabytes — poll this
    during a run, fetch full only for node inspection).
    """
    from persistence import SessionStore
    try:
        # Read-only: never mkdir for a (possibly hostile or mistyped) id.
        store = SessionStore(session_id, create=False)
        g = store.read_graph()
        nodes = [] if light else store.read_all_nodes()
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "invalid session_id"})
    except Exception:
        return {"session_id": session_id, "nodes": [], "edges": []}
    if g is None:
        return {"session_id": session_id, "nodes": [], "edges": []}
    # Serialise nodes and edges into a UI-friendly shape. Timing rides
    # along for free: the executor stamps started_at/completed_at into the
    # graph attrs on every mark(), so the 2s poll carries per-node durations
    # without opening any (potentially megabyte) node state file.
    out_nodes = []
    try:
        g_mtime = store.graph_path.stat().st_mtime
    except OSError:
        g_mtime = 0.0
    now = _time.time()
    for nid, d in g.nodes(data=True):
        started = d.get("started_at")
        completed = d.get("completed_at")
        out_nodes.append({
            "id": nid,
            "skill": d.get("skill", ""),
            "status": _stale_node_status(d.get("status", "pending"), g_mtime, now),
            "inputs": d.get("inputs", []),
            "label": (d.get("metadata") or {}).get("label", ""),
            "started_at": started,
            "completed_at": completed,
            "elapsed_s": (round(completed - started, 1)
                          if isinstance(started, (int, float))
                          and isinstance(completed, (int, float))
                          and completed >= started else None),
        })
    out_edges = [{"from": u, "to": v} for u, v in g.edges()]
    payload = {
        "session_id": session_id,
        "query": store.read_query(),
        "nodes": out_nodes,
        "edges": out_edges,
    }
    # light=true exists so the 2s poll never downloads node states (some
    # are megabytes). Emitting the key with an empty array anyway made the
    # client unable to distinguish "light mode" from "no states", and
    # shipped dead weight on every poll — omit it outright.
    if not light:
        payload["node_states"] = [n.model_dump(mode="json") for n in (nodes or [])]
    return payload
@app.get("/api/sessions/{session_id}/nodes/{node_id}")
async def session_node(session_id: str, node_id: str):
    """Single node state for DAG inspection. Cheap (one file) — the full
    graph endpoint can be megabytes, so the UI polls light and fetches
    nodes only when clicked."""
    from persistence import SessionStore
    # Per-string cap for the node-state endpoint. This was a flat 2,000
    # chars, chosen when a formatter answer was ~1 page; now that reports
    # run to several pages it cut the stored answer to ~8% and appended a
    # "[14512 chars truncated]" marker that the UI rendered as content.
    # Large but bounded: a node output is at most one skill's reply, and
    # the graph endpoint (not this one) is the megabyte-scale route.
    STRING_CAP = 200_000
    LIST_CAP = 200
    def _trim(x, depth=0):
        if isinstance(x, str):
            return (x if len(x) <= STRING_CAP
                    else x[:STRING_CAP]
                    + f"\n…[{len(x) - STRING_CAP} chars truncated]")
        if isinstance(x, dict) and depth < 8:
            return {k: _trim(v, depth + 1) for k, v in x.items()}
        if isinstance(x, list) and depth < 8:
            trimmed = [_trim(v, depth + 1) for v in x[:LIST_CAP]]
            if len(x) > LIST_CAP:
                trimmed.append({"…": f"{len(x) - LIST_CAP} more items omitted"})
            return trimmed
        return x
    try:
        n = SessionStore(session_id, create=False).read_node(node_id)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "invalid session_id"})
    except Exception:
        n = None
    if n is None:
        return JSONResponse(status_code=404, content={"error": "unknown node"})
    return _trim(n.model_dump(mode="json"))
@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a session and ALL its traces: graph dir, conversation mapping,
    turn log entries, and per-turn cost ledger. Returns what was removed."""
    import re as _re
    import shutil as _shutil
    if not _re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,64}$", session_id or ""):
        return JSONResponse(status_code=400, content={"error": "invalid session_id"})
    removed: dict[str, object] = {"dir": False, "conversations": 0,
                                  "turns": 0, "costs": False}
    from persistence import SESSIONS_ROOT
    _sdir = SESSIONS_ROOT / session_id
    # Confine to the sessions root even for odd-but-legal names.
    try:
        _sdir.resolve().relative_to(SESSIONS_ROOT.resolve())
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "invalid session_id"})
    if _sdir.exists():
        _shutil.rmtree(_sdir, ignore_errors=True)
        removed["dir"] = True
    with _CONV_LOCK:
        _data = _conv_load()
        _gone = [c for c, s in _data.items() if s == session_id]
        for c in _gone:
            del _data[c]
        if _gone:
            _conv_save(_data)
        removed["conversations"] = len(_gone)
    try:
        import turnlog as _tl
        removed["turns"] = _tl.clear(session_id)
    except Exception:
        pass
    try:
        _cp = _turn_cost_path(session_id)
        if _cp.exists():
            _cp.unlink()
            removed["costs"] = True
    except OSError:
        pass
    # Deleting something that never existed is not a success: the UI (and
    # operators) can't distinguish a typo'd id from a real deletion.
    if not removed["dir"] and not removed["conversations"] \
            and not removed["turns"] and not removed["costs"]:
        return JSONResponse(status_code=404, content={"error": "unknown session"})
    return {"status": "ok", "session_id": session_id, "removed": removed}
@app.get("/api/sessions/{session_id}/browser-shots")
async def session_browser_shots(session_id: str):
    """List browser screenshot URLs for a session (used by the chat UI gallery)."""
    from skills import take_browser_artifacts
    from pathlib import Path as _P
    import re as _re
    if not _re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,64}$", session_id or ""):
        return JSONResponse(status_code=400, content={"error": "invalid session_id"})
    _browser_root = (_P(ROOT) / "state" / "sessions" / session_id / "browser").resolve()
    shots: list[str] = []
    for abs_path in take_browser_artifacts(session_id):
        try:
            rel = _P(abs_path).resolve().relative_to(_browser_root)
            shots.append(f"/api/artifacts/{session_id}/{rel.as_posix()}")
        except ValueError:
            continue
    return {"session_id": session_id, "shots": shots}
# ── tools browser (UI panel) ────────────────────────────────────────────────
@app.get("/api/tools")
async def list_tools():
    """List registered skills + MCP tools for the UI's Tools panel."""
    from skills import _TOOL_CATALOG
    tools = []
    catalog = _TOOL_CATALOG if isinstance(_TOOL_CATALOG, dict) else {}
    for name, spec in catalog.items():
        if isinstance(spec, dict):
            tools.append({
                "name": name,
                "kind": "skill",
                "description": spec.get("description", ""),
                "params": (spec.get("input_schema") or spec.get("params")
                   or spec.get("parameters") or {}),
            })
        else:
            tools.append({"name": name, "kind": "skill", "description": str(spec)})
    return {"tools": tools}
# ── config browser (UI panel) ───────────────────────────────────────────────
@app.get("/api/config/tools")
async def get_tools_guard():
    """Current enable/disable guard state for the Skills page.
    `disabled: null` means the guard file could not be read:
    the withheld set is UNKNOWN, and the UI must not render
    that as an all-clear (a 200 with an empty list would)."""
    from skills import _disabled_tools
    disabled = _disabled_tools()
    if disabled is None:
        return {"disabled": None,
                "error": "could not read the tool guard "
                         "(state/tools_disabled.json) — "
                         "withheld-tool state unknown"}
    return {"disabled": sorted(disabled)}
@app.post("/api/config/tools")
async def set_tool_enabled(req: Request):
    """Enable/disable one MCP tool by name. Body: {"tool": str, "enabled": bool}.
    Persisted to state/tools_disabled.json, consulted live by skills.tool_payload
    (no restart needed). Unknown names 400."""
    from skills import _TOOL_CATALOG
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    # `enabled` used to default to True when the field was missing (a
    # bare {"tool": name} enabled the tool it named) and to accept any
    # truthy value — "false" as a string enabled the tool. Require the
    # field and a real boolean.
    if "enabled" not in body or not isinstance(body.get("enabled"), bool):
        return JSONResponse(status_code=400,
                            content={"error": "enabled must be a boolean"})
    enabled = body["enabled"]
    name = body.get("tool")
    if not isinstance(name, str):
        return JSONResponse(status_code=400,
                            content={"error": "tool must be a string"})
    name = name.strip()
    catalog = _TOOL_CATALOG if isinstance(_TOOL_CATALOG, dict) else {}
    if not name or name not in catalog:
        return JSONResponse(status_code=400, content={"error": f"unknown tool '{name}'"})
    import json as _json
    # STATE_DIR honours S9_STATE_DIR, like the reader
    # (skills._disabled_tools). A hardcoded ROOT/state
    # path here made the POST write one file while the
    # reader read another whenever S9_STATE_DIR was set —
    # every toggle silently did nothing.
    path = STATE_DIR / "tools_disabled.json"
    try:
        data = _json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        data = {}
    disabled = set(data.get("tools", []))
    if enabled:
        disabled.discard(name)
    else:
        disabled.add(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic: a crash mid-write used to leave a truncated
        # JSON, and the reader's fail-open path then silently
        # re-enabled every withheld tool.
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
        tmp.write_text(_json.dumps({"tools": sorted(disabled)},
                                   indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    return {"tool": name, "enabled": enabled, "disabled": sorted(disabled)}
@app.get("/api/apps/flags")
async def get_flags():
    """Flags board for the Apps page: merged Prefab cloud + local
    overrides + defaults. Never 500s — evaluation always falls back."""
    import flags as _flags
    return {"flags": _flags.all_flags(), "prefab": _flags.prefab_status()}
@app.post("/api/apps/flags")
async def set_flag(req: Request):
    """Operator override for one flag. Body: {"name": str, "value": any}
    (omit value / send null to clear back to cloud-or-default). Unknown
    names and mistyped values 400. Takes effect immediately, no restart."""
    import flags as _flags
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    name = _body_str(body, "name")
    if name is None:
        return JSONResponse(status_code=400,
                            content={"error": "name must be a string"})
    if not name:
        return JSONResponse(status_code=400, content={"error": "name required"})
    try:
        if "value" not in body or body.get("value") is None:
            cleared = _flags.clear_override(name)
            return {"name": name, "cleared": cleared,
                    "flags": _flags.all_flags()}
        row = _flags.set_override(name, body.get("value"))
        return {**row, "flags": _flags.all_flags()}
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)[:200]})
@app.get("/api/apps")
async def apps_list():
    """Tracker apps board: one row per app spec with live status
    (last refresh, next refresh, item count, added/removed, error)."""
    import apps as _apps
    return {"apps": _apps.list_apps()}
@app.post("/api/apps")
async def apps_create(req: Request):
    """Create a tracker app from a config spec. Body: {name, kind,
    schedule, ...kind fields}. 400 on invalid spec, 409 on duplicate id.
    Unknown fields are ignored; the normalised spec is returned."""
    import apps as _apps
    try:
        body = await req.json()
    except Exception:
        body = {}
    try:
        spec = _apps.create_app(body if isinstance(body, dict) else {})
        return {"spec": spec, "apps": _apps.list_apps()}
    except ValueError as e:
        code = 409 if "already exists" in str(e) else 400
        return JSONResponse(status_code=code, content={"error": str(e)})
@app.get("/api/apps/{app_id}")
async def apps_detail(app_id: str):
    """Full app detail: spec + latest items + added/removed + history."""
    import apps as _apps
    app = _apps.get_app(app_id)
    if app is None:
        return JSONResponse(status_code=404, content={"error": "unknown app"})
    return app
@app.post("/api/apps/{app_id}/refresh")
async def apps_refresh(app_id: str):
    """Refresh one app now (manual pull). Failures come back in the
    result's error field, never as a 500 — a dead source must not break
    the board."""
    import apps as _apps
    try:
        return _apps.refresh_app(app_id)
    except ValueError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
@app.delete("/api/apps/{app_id}")
async def apps_delete(app_id: str):
    """Delete an app spec and its snapshots."""
    import apps as _apps
    if not _apps.delete_app(app_id):
        return JSONResponse(status_code=404, content={"error": "unknown app"})
    return {"deleted": app_id, "apps": _apps.list_apps()}
@app.get("/api/apps/{app_id}/a2ui")
async def apps_a2ui(app_id: str):
    """One tracker app as a validated A2UI surface.
    A peer of `/api/apps/{app_id}/prefab`, not a replacement for it: prefab
    stays available (it is offline-capable and already embedded in the console),
    and this gives the board a declarative protocol that is the same one the
    agent emits everywhere else. Deterministic - built from the app's own spec
    and data, never model-generated, because a table of known rows has no
    uncertainty for a model to resolve.
    """
    import apps as _apps
    import apps_a2ui as _a2
    app = _apps.get_app(app_id)
    if app is None:
        return JSONResponse(status_code=404, content={"error": "unknown app"})
    try:
        return _a2.validated_surface(app)
    except Exception as e:
        # A builder bug must not 500 the board; say what it was.
        return JSONResponse(status_code=502, content={
            "error": f"a2ui surface failed: {type(e).__name__}: {e}"[:200]})
async def _apps_refresh_events(app_id: str):
    """The refresh event sequence, as AG-UI events.
    Split from the route so it can be driven directly in a test: going through
    a StreamingResponse to assert on the frames means testing the response
    plumbing as well as the protocol.
    """
    import apps as _apps
    import agui as _agui
    thread_id, run_id, _mid = _agui.new_ids(None)
    yield _agui.run_started(thread_id, run_id)
    yield _agui.activity_snapshot("refresh", "starting refresh",
                                  activity_type="status")
    steps: list[str] = []
    try:
        for step in ("fetch", "diff", "persist"):
            steps.append(step)
            yield _agui.step_started(step)
            yield _agui.activity_snapshot("refresh", f"{step}…",
                                          activity_type="status")
        # The blocking fetch+diff+persist is one call; it runs in a worker so
        # the loop can actually flush the frames above instead of buffering
        # them until the work is already done.
        result = await asyncio.to_thread(_apps.refresh_app, app_id)
    except Exception as e:
        for step in steps:
            yield _agui.step_finished(step)
        yield _agui.run_error(f"refresh failed for {app_id}: {e}"[:500])
        return
    for step in steps:
        yield _agui.step_finished(step)
    error = (result or {}).get("error")
    if error:
        # A dead source is a completed run with an error FIELD, not a crashed
        # run: the board still has its last good snapshot.
        yield _agui.activity_snapshot(
            "refresh", f"source error: {error}"[:300], activity_type="error")
    else:
        # `added`/`removed` are INT COUNTS from refresh_app(), not lists.
        # len() on them raised TypeError, so the FIRST refresh of any app
        # with new items reported "TypeError: object of type 'int' has no
        # len()" and the board never updated (a second refresh worked
        # only because the counts were then 0). Same shape as the
        # get_app() int/list bug fixed in apps_a2ui.
        yield _agui.activity_snapshot(
            "refresh",
            f"{result.get('count', 0)} items, "
            f"{result.get('added') or 0} added, "
            f"{result.get('removed') or 0} removed",
            activity_type="status")
    yield _agui.run_finished(thread_id, run_id, {
        "app_id": app_id,
        "count": (result or {}).get("count", 0),
        "error": error or None,
    })
@app.post("/api/apps/{app_id}/refresh/stream")
async def apps_refresh_stream(app_id: str, req: Request):
    """Refresh one app, reporting progress as an AG-UI event stream.
    `POST /api/apps/{app_id}/refresh` is a single blocking call: it fetches a
    third-party feed (GitHub, RSS, a sheet), diffs it and persists a snapshot.
    On a slow or hanging source the console showed nothing at all until it
    returned, so the only honest thing a user could do was wait and wonder
    whether the click had landed.
    AG-UI is the right shape here because the refresh has real phases worth
    naming - fetch, diff, persist - and a terminal event either way. Every
    frame is a typed event, so a client never has to parse prose, and
    `event_stream()` guarantees exactly one terminal event.
    """
    import apps as _apps
    import agui as _agui
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    if _apps.get_spec(app_id) is None:
        return JSONResponse(status_code=404, content={"error": "unknown app"})
    return StreamingResponse(
        _agui.event_stream(_apps_refresh_events(app_id)),
        media_type="text/event-stream")
@app.get("/api/apps/{app_id}/prefab")
async def apps_prefab(app_id: str):
    """Tracker app rendered through PrefectHQ/prefab (SPIKE, side-by-side
    with the built-in table/cards views). Returns the bundled single-file
    HTML (offline-capable) for iframe embedding. Unknown app → 404;
    prefab_ui problems → 502 (never 500s the board)."""
    try:
        import prefab_views as _pv
        html = _pv.tracker_prefab_html(app_id)
    except ValueError:
        return JSONResponse(status_code=404, content={"error": "unknown app"})
    except Exception as e:
        return JSONResponse(status_code=502,
                            content={"error": f"prefab render failed: {type(e).__name__}: {e}"[:300]})
    from fastapi.responses import HTMLResponse
    return HTMLResponse(html)
@app.get("/api/apps/{app_id}/prefab-token")
async def prefab_token(app_id: str):
    """Mint a short-lived signed pass for the prefab iframe.
    The iframe is a browser navigation and cannot send the
    X-Aria-Token header, so its view 403'd inside the frame.
    The pass is HMAC-signed with the per-launch token and
    lives 120s — see auth.prefab_pass. With auth disabled
    (tests, --reload) there is nothing to sign: the iframe
    loads without a pass."""
    import auth as _auth
    import apps as _apps
    if _apps.get_spec(app_id) is None:
        return JSONResponse(status_code=404,
                            content={"error": "unknown app"})
    try:
        exp, sig = _auth.prefab_pass(app_id)
    except _auth.AuthNotConfigured:
        return {"token": "", "exp": 0}
    return {"token": sig, "exp": exp}
@app.get("/api/config")
async def get_config():
    """Return the contents of agent_config.yaml for the UI's Config panel."""
    import yaml
    cfg_path = ROOT / "agent_config.yaml"
    if not cfg_path.exists():
        return {"error": "agent_config.yaml not found", "raw": ""}
    try:
        raw = cfg_path.read_text(encoding="utf-8")
        # Parse to validate it's real YAML; fall back to raw on error.
        try:
            parsed = yaml.safe_load(raw)
            return {"raw": raw, "parsed": parsed}
        except Exception:
            return {"raw": raw, "parsed": None}
    except Exception as e:
        return {"error": str(e), "raw": ""}
# ── notifications (UI panel + Telegram bridge log) ──────────────────────────
@app.get("/api/notifications")
async def get_notifications(limit: int = 50):
    """Return the most recent in-app notifications (newest first)."""
    return {"notifications": list_notifications(limit=limit)}
@app.get("/api/ack/threads")
async def get_ack_threads(limit: int = 50):
    """Acknowledgement conversations opened by pre-notifications.
    Exposed because the records exist and are otherwise invisible: a reminder
    that is waiting on a reply looks identical to one that was delivered and
    ignored until you can see which.
    """
    try:
        import ack as _ack
        return {"threads": _ack.list_threads(limit=limit)}
    except Exception as e:
        return JSONResponse(status_code=500,
                            content={"error": f"{type(e).__name__}: {e}"})
@app.post("/api/ack/inbound")
async def post_ack_inbound(body: dict):
    """Route an inbound channel message: an acknowledgement thread first, then
    an ordinary chat turn.
    The gateway's webhook handler calls this after normalizing a message. It is
    also reachable directly so the flow can be exercised without a live chat
    platform. Returns what happened either way — an unroutable message is a
    normal outcome, not an error, because most inbound traffic is not a reply
    to any reminder.
    This route and `/api/channel/inbound` are the same router: a reply to a
    live reminder belongs to that thread whatever it says, and everything else
    is a person talking to the agent.
    """
    channel = str(body.get("channel") or "telegram")
    sender = str(body.get("sender") or body.get("sender_id") or "")
    text = str(body.get("text") or "")
    if not sender or not text:
        return JSONResponse(status_code=400,
                            content={"error": "sender and text are required"})
    try:
        import channel_chat
        return channel_chat.route_inbound(channel, sender, text)
    except Exception as e:
        return JSONResponse(status_code=500,
                            content={"error": f"{type(e).__name__}: {e}"})
@app.post("/api/channel/inbound")
async def post_channel_inbound(body: dict):
    """A message from a chat channel (Telegram today), answered back on it.
    Returns immediately; the turn runs on a daemon thread, because a chat
    platform retries a slow webhook and a reply that arrives twice is worse
    than one that arrives late.
    Routing order: an open acknowledgement thread for this sender owns the
    message outright (see `channel_chat.route_inbound`); otherwise it becomes a
    chat turn, subject to the trust gate — a channel the agent can be talked to
    is also a channel a stranger can type into. Both refusals are normal
    outcomes rather than errors, reported in the body so the caller can log
    them rather than treating the request as failed.
    """
    channel = str(body.get("channel") or "telegram")
    sender = str(body.get("sender") or body.get("sender_id") or "")
    text = str(body.get("text") or "")
    attachments = body.get("attachments") or []
    # A file-only message has no text. Refusing it as malformed was correct
    # when files were dropped by the adaptor anyway; now the file is the
    # request, and "sender and text are required" would 400 the user's upload.
    if not sender or (not text and not attachments):
        return JSONResponse(status_code=400, content={
            "error": "sender and (text or attachments) are required"})
    try:
        import channel_chat
        return channel_chat.route_inbound(channel, sender, text,
                                         attachments=attachments)
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "error": f"{type(e).__name__}: {e}"})
@app.get("/api/channel/threads")
async def get_channel_threads():
    """Who has talked to the agent over a channel, and which conversation
    each one maps to.
    Exposed because the mapping is otherwise invisible: a channel turn is a
    real chat conversation with its own history, and there is no way to see
    that from the console today.
    """
    try:
        import channel_chat
        with channel_chat._LOCK:  # noqa: SLF001 - same repo, read-only
            data = channel_chat._load_map()
        return {"conversations": data}
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "error": f"{type(e).__name__}: {e}"})
@app.get("/api/notify/channels")
async def get_notify_channels():
    """Which channels a notification could use right now, and what is missing.
    Three separate questions per channel, because conflating them is how a
    channel ends up looking alive in the inventory while delivering nothing:
    configured (does the gateway hold a credential), destination (is anyone
    paired to receive it), and the last delivery attempt.
    """
    try:
        import notify as _notify
        return {"health": _notify.channel_health(),
                "recent": _notify.delivery_log(10)}
    except Exception as e:
        return JSONResponse(status_code=500,
                            content={"error": f"{type(e).__name__}: {e}"})
@app.post("/api/notify/refresh")
async def post_notify_refresh(body: dict):
    """Verify (and where possible refresh) a channel credential.
    Verification is the part that actually helps: it distinguishes "the
    gateway holds a token" from "that token still works", which the inventory
    cannot do. Supplying `token` sets it for this process only — it is never
    written to disk from here.
    """
    try:
        import notify as _notify
        return _notify.refresh_channel_token(
            str(body.get("channel") or ""), str(body.get("token") or ""))
    except Exception as e:
        return JSONResponse(status_code=500,
                            content={"error": f"{type(e).__name__}: {e}"})
@app.post("/api/notifications")
async def post_notification(req: Request):
    """Append a notification entry. Body: {kind, text, session_id?, conversation_id?}."""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400,
                            content={"error": "invalid JSON body"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    kind = _body_str(body, "kind")
    text = _body_str(body, "text")
    if kind is None or text is None:
        return JSONResponse(status_code=400,
                            content={"error": "kind and text must be strings"})
    if not text:
        return JSONResponse(status_code=400,
                            content={"error": "text is required"})
    entry = add_notification(
        kind or "info", text,
        session_id=body.get("session_id", ""),
        conversation_id=body.get("conversation_id", ""),
    )
    return {"status": "ok", "notification": entry}
# ── memory browser (UI panel) ────────────────────────────────────────────────
@app.get("/api/memory")
async def list_memories(q: str | None = None, limit: int = 50,
                              drawers: str | None = None,
                              kinds: str | None = None,
                              hide_superseded: bool = False):
    """List persistent memory items for the UI's Memory panel.
    Without `q`, returns the most recent items (newest first). With `q`,
    returns matched items so the panel doubles as a memory search.
    `drawers` narrows to a comma-separated subset; `kinds` narrows to the
    agent-facing vocabulary (so the panel can show preferences on their own —
    they are stored in the `fact` drawer and a drawer filter cannot isolate
    them). `hide_superseded` drops corrected records.
    """
    import memory as mem_svc
    def _shape(h):
        d = {
            "id": h.id, "kind": h.kind, "descriptor": h.descriptor,
            "keywords": h.keywords[:8], "source": h.source,
        }
        # Phase 3: review visibility for corrected records (UI badges).
        if getattr(h, "superseded_by", None):
            d["superseded_by"] = h.superseded_by
        if getattr(h, "drawer", None):
            d["drawer"] = h.drawer
        # How well-supported the record is. Until observations existed nothing
        # ever wrote a non-default confidence, so this always read 1.0 and the
        # UI had nothing honest to show; an observation carrying its score is
        # how a user judges whether to trust an entry.
        conf = getattr(h, "confidence", None)
        if conf is not None:
            d["confidence"] = conf
        return d
    try:
        drawer_list = [d.strip() for d in drawers.split(",") if d.strip()] \
            if drawers else None
        kind_list = [k.strip() for k in kinds.split(",") if k.strip()] \
            if kinds else None
        if q and q.strip():
            # Pass every filter through. The search branch used to drop
            # kinds/hide_superseded/session_id, so the Memory page's kind
            # filter silently did nothing the moment a search was active --
            # selecting "tool outcomes" still returned facts and preferences.
            hits = mem_svc.read(q.strip(), top_k=min(limit, 100),
                                drawers=drawer_list, kinds=kind_list,
                                include_stale=not hide_superseded)
            items = [_shape(h) for h in hits]
        else:
            # Newest-first via the gateway memory service (the JSON store
            # moved gateway-side; there is no local _load anymore).
            items = [_shape(it) for it in mem_svc.list_recent(
                limit=limit, drawers=drawer_list, kinds=kind_list,
                hide_superseded=hide_superseded)]
        return {"items": items}
    except mem_svc.MemoryBackendError as e:
        # Was `return {"items": [], "error": ...}` with a 200. A gateway 500
        # therefore rendered as an empty Memory page -- indistinguishable from
        # "you have no memories", so a user would conclude their memory had
        # been deleted and start re-entering it. The UI keys its error banner
        # off `loadError` and only when the list is empty, so a silent empty
        # list is the worst possible answer. Propagate the failure, keeping a
        # gateway 4xx (e.g. an unknown `kinds` value) as a 400.
        return JSONResponse(status_code=_gw_status(
            {"status_code": e.status_code}),
            content={"status": "error",
                     "message": f"memory unavailable: {e}"[:200]})
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "status": "error",
            "message": f"memory backend unavailable: {e}"[:200]})
@app.post("/api/profile/document")
async def profile_document():
    """Write the whole profile - observations AND standing preferences - into
    the Documents section as a markdown file.
    Both stores, because they answer different questions and a person checking
    "what does the agent think it knows about me" needs both. Far easier to
    argue with on paper than in a list of rows.
    """
    try:
        import memory as _mem
        out = _mem.save_profile_document()
        if not out.get("ok"):
            return JSONResponse(status_code=502, content=out)
        return out
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "ok": False, "error": f"{type(e).__name__}: {e}"[:200]})
@app.get("/api/profile")
async def profile_observations(limit: int = 200):
    """The behavioural profile: what has been noticed, and the quote behind it.
    Separate from /api/memory because the evidence is the point. In the
    generic list an observation is a row of text like any other; here every
    entry carries the user's own words and a confidence, so a wrong one is
    visibly a guess and the user can delete it knowing what it was based on.
    """
    try:
        import memory as _mem
        enabled = _mem.observations_enabled()
        rows = _mem.list_observations(k=max(1, min(int(limit), 200))) \
            if enabled else []
        return {"enabled": enabled, "observations": rows,
                "note": "" if enabled else
                        "Off. Turn on the memory.observations flag to start "
                        "recording."}
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "status": "error",
            "message": f"profile unavailable: {e}"[:200]})
_MAX_VALUE_DEPTH = 24
def _json_depth(obj, limit: int = _MAX_VALUE_DEPTH, _d: int = 0) -> int:
    """Nesting depth of a decoded-JSON value, stopping early at `limit`.
    Bounded so a pathological payload cannot make this itself expensive; the
    caller only needs to know "deeper than allowed".
    """
    if _d > limit:
        return _d
    if isinstance(obj, dict):
        return max((_json_depth(v, limit, _d + 1) for v in obj.values()),
                   default=_d)
    if isinstance(obj, list):
        return max((_json_depth(v, limit, _d + 1) for v in obj),
                   default=_d)
    return _d
# ── documents (proxy to the gateway) ────────────────────────────────────────
# The document pipeline lives in the gateway (parsing, chunking, embedding,
# the registry). The agent exposes a thin HTTP surface so the console has one
# origin to talk to, and so uploads stream through rather than being buffered
# here.
@app.get("/api/documents")
async def documents_list():
    try:
        return _gw_response(await _gw_json("/v1/documents"))
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "error": f"gateway unreachable: {e}"[:200]})
@app.get("/api/documents/{doc_id}")
async def documents_detail(doc_id: str):
    try:
        return _gw_response(await _gw_json(f"/v1/documents/{doc_id}"))
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "error": f"gateway unreachable: {e}"[:200]})
@app.post("/api/documents")
async def documents_upload(req: Request):
    """Forward the multipart upload without reading it into memory here: a
    large document buffered in the agent would cost memory for no reason."""
    try:
        import httpx
        content = await req.body()
        ctype = req.headers.get("content-type", "multipart/form-data")
        async with httpx.AsyncClient(timeout=_GW_UPLOAD_TIMEOUT,
                                     headers=_gw_auth_headers()) as c:
            r = await c.post(f"{_GW_VOICE}/v1/documents", content=content,
                             headers={"Content-Type": ctype})
        # Carry the gateway's status across. `return json.loads(r.text)`
        # returned a plain dict, which FastAPI always renders as HTTP200, so a
        # gateway 400 ("a file part is required"), 413 (over the cap) or 415
        # (unsupported type) reached the browser as a SUCCESS carrying an
        # error object - a client checking the status believed the upload
        # worked. Same laundering `_gw_response`/`_gw_status` already prevent
        # on the JSON document routes.
        try:
            data = json.loads(r.text)
        except Exception:
            data = None
        if isinstance(data, dict) and not r.is_success:
            return JSONResponse(status_code=r.status_code, content=data)
        if isinstance(data, dict):
            return data
        return JSONResponse(status_code=r.status_code,
                            content={"error": r.text[:200]})
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "error": f"upload failed: {e}"[:200]})
@app.post("/api/documents/{doc_id}/enabled")
async def documents_set_enabled(doc_id: str, req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
        return JSONResponse(status_code=400,
                            content={"error": "enabled must be true or false"})
    try:
        return _gw_response(await _gw_json(
            f"/v1/documents/{doc_id}/enabled", method="POST", json=body))
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "error": f"gateway unreachable: {e}"[:200]})
@app.post("/api/documents/{doc_id}/reindex")
async def documents_reindex(doc_id: str):
    try:
        return _gw_response(await _gw_json(f"/v1/documents/{doc_id}/reindex", method="POST"))
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "error": f"gateway unreachable: {e}"[:200]})
@app.delete("/api/documents/{doc_id}")
async def documents_delete(doc_id: str):
    try:
        return _gw_response(await _gw_json(f"/v1/documents/{doc_id}", method="DELETE"))
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "error": f"gateway unreachable: {e}"[:200]})
@app.get("/api/documents/{doc_id}/content")
async def documents_content(doc_id: str, req: Request):
    """Raw upload bytes, proxied from the gateway.
    The console's preview fetches these bytes (authenticated with the
    console token) and renders them with the browser's own viewer via a
    blob URL, so PDFs open inline with page navigation, zoom and
    download. `Range` is passed through for streaming clients.
    """
    import httpx
    rng = req.headers.get("range")
    url = f"{_GW_VOICE}/v1/documents/{doc_id}/source"
    try:
        async with httpx.AsyncClient(
                timeout=_GW_UPLOAD_TIMEOUT,
                headers=_gw_auth_headers(
                    {"Range": rng} if rng else None)) as c:
            r = await c.get(url)
            if r.status_code == 401:
                c.headers.update(_gw_auth_headers())
                r = await c.get(url)
        out = {"Content-Type": r.headers.get("content-type",
                                             "application/octet-stream")}
        for k in ("accept-ranges", "content-range"):
            if k in r.headers:
                out[k] = r.headers[k]
        return Response(content=r.content, status_code=r.status_code,
                        headers=out)
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "error": f"gateway unreachable: {e}"[:200]})


@app.get("/api/documents/{doc_id}/preview")
async def documents_preview(doc_id: str):
    """Server-rendered pages for the Documents modal.

    The modal used to fetch the entire upload into a blob URL and trust the
    browser's PDF plugin. When that plugin is unavailable the iframe is simply
    blank - a file the user uploaded, previewed as nothing. This path returns
    page images from the same bytes, in the same shape the Authoring viewer
    already consumes.
    """
    try:
        return _gw_response(await _gw_json(
            f"/v1/documents/{doc_id}/preview", timeout=120.0))
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "error": f"gateway unreachable: {e}"[:200]})


# Chat used to reach uploaded documents only if the MODEL decided to call
# `search_knowledge`. That is not access, it is a suggestion: a model that
# skips the tool answers from its own weights, and with the `chat.tools` flag
# off it could not call the tool at all. The console then read as though chat
# had no documents, which was true.
#
# So retrieval happens here, on the request path, before the model sees
# anything. The model's own `search_knowledge` call remains available for
# follow-up questions; this is the floor, not the ceiling.
_DOC_CTX_TOP_K = 6
_DOC_CTX_MAX_CHARS = 6000
_DOC_CTX_TIMEOUT_S = 8.0
# Same base the other gateway proxies use. `_GW_VOICE` is defined further down
# this module, after this function, so the constant is repeated here rather
# than read at call time from a name that does not exist yet.
_GW_BASE = os.environ.get("LLM_GATEWAY_V9_URL", "http://localhost:8109").rstrip("/")
async def _doc_context(query: str, doc_ids: set[str] | None) -> tuple[str, int]:
    """Retrieve chunks for `query` and render them as a prompt block.
    Returns `(text, hit_count)`. `text` is empty when there is nothing to add -
    no enabled documents, no hits, or the gateway is unreachable. Silence is
    deliberate: injecting "no documents found" would tell the model the corpus
    is empty when the truth is that the lookup failed, and it would then answer
    as if the user had no documents at all.
    """
    if doc_ids is not None and not doc_ids:
        return "", 0
    if not (query or "").strip():
        return "", 0
    # Local import: httpx is imported per-function elsewhere in this module and
    # is not a module-level name here. Without it the NameError is swallowed by
    # the except below and retrieval silently never happens.
    import httpx
    payload: dict = {"query": query[:2000], "top_k": _DOC_CTX_TOP_K}
    if doc_ids is not None:
        payload["doc_ids"] = sorted(doc_ids)
    try:
        # The token belongs on this call too. It was the one gateway call site
        # left building a bare client, so `/v1/documents/search` answered 401,
        # the `!= 200` branch printed and returned `("", 0)`, and every chat
        # turn silently answered from the model's weights with no reference
        # material - while the Documents page looked perfectly healthy. Worse,
        # `tests/test_chat_document_context.py` stubs AsyncClient with a fake
        # whose `__init__` ignores headers, so the suite mocked away the very
        # argument that carries the credential.
        async with httpx.AsyncClient(timeout=_DOC_CTX_TIMEOUT_S,
                                     headers=_gw_auth_headers()) as c:
            r = await c.post(f"{_GW_BASE}/v1/documents/search", json=payload)
        if r.status_code != 200:
            print(f"[chat.docs] search returned {r.status_code}")
            return "", 0
        hits = (r.json() or {}).get("hits") or []
    except Exception as e:
        # Never fail a chat turn because retrieval was unavailable.
        print(f"[chat.docs] search failed ({e!r}); continuing without documents")
        return "", 0
    if not hits:
        # Silence here is what produced the worst observed failure. With zero
        # hits the document block was skipped entirely, so the prompt carried
        # no evidence that the user had uploaded anything. The model then read
        # "I have nothing" as "the material must live elsewhere", reached for
        # Gmail/Calendar/Slack/Notion, and reported THEIR expired credentials
        # as the reason it could not answer - on a question whose answer was
        # sitting in an enabled document. An honest "retrieval ran and matched
        # nothing, here is what you have uploaded" is both truer and stops the
        # substitution. Docs-off (an empty allow-list) stays silent on purpose:
        # that is a deliberate user choice, not a retrieval failure.
        if doc_ids is not None and not doc_ids:
            return "", 0
        import asyncio as _aio
        names = await _aio.to_thread(_gw_enabled_documents)
        if names:
            listed = "; ".join(f"{d['filename']}" for d in names[:10])
            more = f" (+{len(names) - 10} more)" if len(names) > 10 else ""
            return (
                "RETRIEVAL FROM THE USER'S UPLOADED DOCUMENTS RETURNED NO "
                "MATCH for this question.\n"
                f"The user has {len(names)} document(s) enabled for this "
                f"conversation: {listed}{more}. They were searched; nothing "
                "matched this particular wording.\n"
                + _NO_SUBSTITUTION_RULE
            ), 0
        # Nothing enabled. That is NOT the same as "the user has no documents",
        # and conflating the two is what produced the worst failure: with the
        # menu switched off the prompt said nothing at all, so the model read
        # "I have nothing" as "it must be in their inbox" and reported their
        # expired Gmail token as the reason it could not answer a question the
        # menu contained. So name what exists and why it cannot answer.
        all_docs = await _aio.to_thread(_gw_all_documents)
        if all_docs:
            listed = "; ".join(
                f"{d['filename']} ({'disabled' if not d['enabled'] else d['status']})"
                for d in all_docs[:10])
            more = f" (+{len(all_docs) - 10} more)" if len(all_docs) > 10 else ""
            return (
                "THE USER HAS UPLOADED DOCUMENTS, BUT NONE OF THEM CAN ANSWER "
                "THIS QUESTION RIGHT NOW.\n"
                f"Uploaded: {listed}{more}.\n"
                "A document marked `disabled` is switched off for chat; one "
                "marked `parsing` is still being indexed. Say that plainly and "
                "offer to enable it or wait - do not silently substitute "
                "another system.\n" + _NO_SUBSTITUTION_RULE
            ), 0
        # Genuinely nothing uploaded. "You have no documents" is TRUE here,
        # so it is safe to state - and the substitution rule still applies.
        return (
            "THE USER HAS NOT UPLOADED ANY DOCUMENTS.\n"
            "If the question is about a file of theirs, say so and ask them to "
            "upload it on the Documents page.\n" + _NO_SUBSTITUTION_RULE
        ), 0
    def _header(n: int) -> str:
        return (
            f"REFERENCE MATERIAL RETRIEVED FROM THE USER'S UPLOADED DOCUMENTS "
            f"({n} chunk{'s' if n != 1 else ''}, keyword+vector "
            f"match against their question).\n"
            # Phrased as an obligation, not a suggestion. The permissive wording
            # ("use it when it answers the question") let the model decide the
            # retrieved text was optional and reply "I don't have access to your
            # menu" while the material sat in the very prompt it was answering -
            # a refusal that is both false and, for an uploaded document, absurd.
            # A retrieved chunk IS the user's own data; there is nothing to grant
            # access to, so the model must not treat it as an external lookup.
            "This is the user's own document, already open to you - there is no "
            "access question. Answer from it whenever it contains the answer, and "
            "cite it as [n]. Do NOT say you lack access to it, do not ask which "
            "document or whose it is, and do not ask the user to supply "
            "information that is printed below. Quote sparingly. If it genuinely "
            "does not answer the question, say so plainly instead of guessing. "
            "It is a partial extract, not the whole document.\n"
            # Anti-substitution. The clause above stops a false access claim,
            # but nothing stopped the model from quietly deciding the document
            # "wasn't it" and fetching the answer from the user's email or
            # calendar instead - then reporting THOSE credentials' failure as
            # the reason. Name that behaviour explicitly.
            "If the question looks like it is about the user's uploaded "
            "material, this material is the only source that counts. Do not "
            "substitute email, calendar, chat, a knowledge base or any other "
            "connected system for it, and never cite an unrelated integration's "
            "error as the reason you cannot answer.\n\n"
        )
    # The cap is on what actually enters the prompt, so the header's own length
    # has to come out of it. Reserving it as fixed overhead (rather than
    # summing afterwards) keeps the bound exact: the earlier version budgeted
    # chunks alone and then prepended ~500 chars of header, so the real total
    # overshot _DOC_CTX_MAX_CHARS by the header size - unnoticed until the
    # wording grew and tripped the budget test.
    header_reserve = len(_header(len(hits) or 1))
    parts: list[str] = []
    used = 0
    for i, h in enumerate(hits, start=1):
        chunk = (h.get("chunk") or "").strip()
        if not chunk:
            continue
        name = (h.get("filename") or "").strip() or "uploaded document"
        heads = h.get("heading_path") or []
        where = " > ".join(str(x) for x in heads if x) if heads else ""
        page = h.get("page")
        cite = name
        if where:
            cite += f"  {where}"
        if page:
            cite += f" (p. {page})"
        budget = _DOC_CTX_MAX_CHARS - header_reserve - used
        if budget <= 200:
            break
        # Budget the WHOLE part, not just the chunk text. The `[n] cite\n`
        # prefix is in the prompt too, so charging only the chunk let the
        # citation lines accumulate past the cap unnoticed.
        overhead = len(f"[{i}] {cite}\n")
        room = budget - overhead
        if room <= 0:
            break
        if len(chunk) > room:
            # " …" is 2 more characters and has to fit inside the same budget.
            chunk = chunk[:max(0, room - 2)].rsplit(" ", 1)[0] + " …"
        part = f"[{i}] {cite}\n{chunk}"
        used += len(part)
        parts.append(part)
    if not parts:
        return "", 0
    return _header(len(parts)) + "\n\n".join(parts), len(parts)
async def _with_doc_context(messages: list[dict], query: str,
                            doc_ids: set[str] | None) -> tuple[list[dict], int]:
    """Insert the document block as its own system turn before the question.
    It goes before the final user message rather than being appended to the
    system prompt, so the retrieved text sits adjacent to the question it is
    evidence for, and so a later turn in the same thread does not accumulate
    every earlier lookup's chunks.
    """
    block, n = await _doc_context(query, doc_ids)
    if not block:
        return messages, 0
    out = list(messages)
    out.insert(max(0, len(out) - 1), {"role": "system", "content": block})
    return out, n
def _chat_memory_block(query: str, session_id: str | None) -> str:
    """Standing user facts for the chat prompt, as its own system turn.
    Chat had no memory at all: `flow.py` reads hits once per run and skills
    receive them as MEMORY HITS, but `chat_simple`/`chat_simple_stream` only
    ever called `_with_doc_context`. So a preference the user saved was
    invisible in the one surface where they most expect to be obeyed — the
    chat they use to ask "what do you know about me?" That tool
    (`recall_preferences`) exists and works, but a model cannot answer
    correctly about the user by choosing to call a tool to fetch the very
    facts it should already have been given.
    Fail-soft, and deliberately shaped like the skill-side block: these are
    cross-session facts, never conversation history, and must not be woven
    into the answer unasked. Documents are filtered by the same enabled set
    `flow.py` uses so a disabled upload cannot leak through chat.
    """
    try:
        import memory as _mem
        from skills import _format_memory_hits
        doc_ids = None
        try:
            doc_ids = _gw_enabled_doc_ids()
        except Exception:
            doc_ids = set()
        hits = _mem.read(query, drawers=[d for d in _mem.SESSION_DRAWERS
                                         if d != "profile"],
                         top_k=6, session_id=session_id,
                         doc_ids=doc_ids) or []
        if not hits:
            return ""
        rendered = _format_memory_hits(hits)
        if not rendered:
            return ""
        return (
            f"MEMORY HITS ({len(hits)} from FAISS) - standing facts about "
            "the user accumulated across ALL conversations, not part of this "
            "conversation's history. Honour them where they are relevant "
            "(preferences, corrections, ongoing work). Never present them as "
            "something the user said in THIS conversation, and never mention "
            "that you have a memory unless asked:\n" + rendered)
    except Exception as e:
        # Recall must never fail a chat turn.
        print(f"[chat.memory] recall skipped: {e!r}")
        return ""
def _read_profile_for_block() -> list[dict]:
    """Observation rows for the chat block. A seam, so the rendering can be
    tested without a live gateway."""
    try:
        import memory as _mem
        return _mem.recall_observations(k=8) or []
    except Exception as e:
        print(f"[chat.profile] recall skipped: {e!r}")
        return []
def _chat_profile_block() -> str:
    """The behavioural profile, with the evidence attached to every line.
    Rendered separately from the fact hits because it must be read differently.
    A fact is something true; an observation is a pattern *drawn* from past
    conversations, and the quote under it is the only reason to believe it.
    Showing the evidence is what stops the model treating a low-confidence
    guess as an established fact about the user - and it means a wrong entry
    is visibly wrong rather than invisibly steering the answer.
    """
    try:
        import memory as _mem
        if not _mem.observations_enabled():
            return ""
        rows = [r for r in _read_profile_for_block() if r.get("observation")]
        if not rows:
            return ""
        lines = []
        for r in rows:
            seen = int(r.get("seen_count") or 1)
            line = f"- {r['observation']}"
            # Say how many times this has actually been seen. A claim recorded
            # once is an anecdote and must not be obeyed like a standing
            # preference; confidence alone hides that, because the extractor
            # grades a single incident highly.
            line += ("  (seen ONCE - a single incident, not a standing preference; "
                     "treat as a maybe, not a rule)"
                     if seen <= 1 else
                     f"  (seen {seen} times)")
            if r.get("evidence"):
                line += f"\n    they said: \"{r['evidence'][:160]}\""
            lines.append(line)
        return (
            "WHAT YOU HAVE NOTICED ABOUT THIS USER (behavioural, evidence-"
            "backed, gathered across past conversations). Treat these as "
            "hypotheses about how they like to work, NOT as facts they "
            "asserted and NOT as a judgement about them. They are never a "
            "diagnosis, and this is not a psychological profile: they record "
            "observable requests and corrections. Anything marked 'seen once' "
            "is a single incident - do not treat it as a standing preference. "
            "Follow the rest where relevant; ignore any that is wrong for "
            "the current task; never quote them back as if the user had just "
            "told you. If one is contradicted by what they say now, they win "
            "and it is stale.\n"
            + "\n".join(lines))
    except Exception as e:
        print(f"[chat.profile] recall skipped: {e!r}")
        return ""
async def _with_chat_memory(messages: list[dict], query: str,
                            session_id: str | None) -> list[dict]:
    """Insert the memory block just before the final user message.
    Placed adjacent to the question for the same reason as document context,
    and inserted once per turn so a long thread does not accumulate every
    earlier turn's hits.
    The `async def` was misleading: both helpers are synchronous and both make
    blocking gateway round-trips — an enabled-document listing (30s timeout),
    a memory search that embeds and runs FAISS (60s), and a profile read (30s)
    — three in sequence, on the event loop, on every chat turn. So a slow
    gateway froze the whole console mid-answer. They now run in one worker
    thread, which also costs one thread hop instead of three blocking ones.
    """
    import asyncio as _aio
    blocks = await _aio.to_thread(
        lambda: [b for b in (_chat_memory_block(query, session_id),
                             _chat_profile_block()) if b])
    if not blocks:
        return messages
    out = list(messages)
    for block in blocks:
        out.insert(max(0, len(out) - 1), {"role": "system", "content": block})
    return out

# Turns that arrive over a personal messaging channel are read on a phone by
# someone in the middle of their day, not in a console. Without this the same
# general-purpose assistant voice came back through Telegram - headings,
# bullet lists, "How may I assist you today?" - which is exactly not how a
# friend texts. Keyed by the prefix `conversation_id_for` builds
# (`{channel[:12]}-{digest}`), so it cannot leak into console threads (`ct-`)
# or Research runs.
_CHANNEL_CHAT_PERSONAS = {
    "telegram": (
        "This conversation is on Telegram, a phone chat with someone you know "
        "well. Write like a close friend texting: warm, casual, first person, "
        "contractions, no corporate phrasing and no canned greetings. Keep "
        "replies short - a sentence or three per message, split into a few "
        "messages when there is more to say - unless the person explicitly "
        "asked for a report, a document or a detailed explanation. Plain text "
        "only: no headings, tables, bullet lists or markdown emphasis unless "
        "the content genuinely needs it; an occasional emoji is fine, a wall "
        "of them is not. Never mention tools, skills, nodes, prompts or the "
        "console."
    ),
}


def _channel_persona_block(conversation_id: str | None) -> str:
    """Style note for a channel turn, or "" for every other conversation.

    Injected as a system message rather than prefixed to the question, so the
    stored transcript still reads as the person's own words and the prompt
    does not accumulate a copy of the note on every turn.
    """
    if not conversation_id or "-" not in conversation_id:
        return ""
    return _CHANNEL_CHAT_PERSONAS.get(conversation_id.split("-", 1)[0], "")


def _with_channel_persona(messages: list[dict],
                          conversation_id: str | None) -> list[dict]:
    block = _channel_persona_block(conversation_id)
    if not block:
        return messages
    out = list(messages)
    out.insert(max(0, len(out) - 1), {"role": "system", "content": block})
    return out
@app.get("/api/capabilities")
async def api_capabilities():
    """What this agent supports, in AG-UI's capability shape.
    Separate from the flag on `/api/agui` on purpose: a client should be able
    to discover that Aria speaks AG-UI even on a build where the endpoint is
    switched off, and should not have to guess by getting a 404.
    """
    try:
        from flags import is_enabled as _flag_on
        enabled = bool(_flag_on("chat.agui", True))
    except Exception:
        enabled = True
    # The capability document must describe what chat can ACTUALLY do. The
    # `chat.tools` kill-switch strips the payload from both chat endpoints, so
    # advertising 21 tools here while the model is handed none is the same
    # class of lie as inventing an artifact handle: a client that trusts this
    # builds a UI offering actions that will never fire.
    try:
        from flags import is_enabled as _flag_on
        chat_tools_on = bool(_flag_on("chat.tools", True))
    except Exception:
        chat_tools_on = True
    try:
        from skills import tool_payload as _tp
        # Aria's tool payload shape is a top-level `name` + `input_schema`,
        # NOT the OpenAI `{"function": {"name": ...}}` nesting. Reading the
        # nested form silently yielded an empty list, so the capability
        # document claimed toolCalling: false while chat was calling tools on
        # every request.
        names = ([t.get("name") for t in (_tp(
            _CHAT_TOOLS) or []) if isinstance(t, dict) and t.get("name")]
            if chat_tools_on else [])
    except Exception:
        names = []
    import agui as _agui
    caps = _agui.capabilities(streaming=True, tools=names)
    caps["custom"] = {"aguiEndpointEnabled": enabled,
                      "chatToolsEnabled": chat_tools_on,
                      "conversationEndpoints": ["/api/chat/threads"]}
    return caps
@app.post("/api/agui")
async def agui_endpoint(req: Request):
    """A chat turn, streamed in AG-UI's event vocabulary.
    Additive and self-contained: it shares the helpers (`_with_doc_context`,
    `_chat_threads_*`, `_session_cost_delta`, `run_with_tools`) but not the
    code path of `/api/chat/simple/stream`. That is deliberate. Translating the
    existing SSE frames would have been less code, but it would have inherited
    their lossy shape - tool progress there is a free-text "status" line - and
    TOOL_CALL_* is most of what AG-UI offers over a plain text stream. Sharing
    the helpers keeps behaviour aligned without inheriting the encoding.
    Reuses `_chat_simple_stream` only for the pieces that are genuinely
    identical: message assembly, document retrieval, thread persistence and
    cost accounting. Everything below the SSE boundary is native here.
    """
    # Bound here, not where it was first needed: `await _aio...` with no
    # binding in scope is a NameError, and it took the whole route down with it.
    import asyncio as _aio
    try:
        from flags import is_enabled as _flag_on
        if not _flag_on("chat.agui", True):
            return JSONResponse(status_code=404, content={
                "error": "AG-UI endpoint disabled (flag chat.agui)"})
    except Exception:
        pass  # a flag system that cannot answer must not 404 the endpoint
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "body must be JSON"})
    import agui as _agui
    try:
        parsed = _agui.parse_request(body)
    except _agui.AguiRequestError as e:
        return JSONResponse(status_code=e.status, content={"error": str(e)})
    query = parsed["query"]
    thread_id, run_id, message_id = _agui.new_ids(parsed["thread_id"])
    parsed["run_id"] = run_id
    # thread_id is client-supplied and reaches the filesystem via
    # _turn_cost_path (turn_costs.json) and the chat-threads store.
    # Reject anything that is not a plain single path segment:
    # "../.." escaped STATE_DIR, and a leading "." hid dotfiles.
    if (not thread_id or ".." in thread_id
            or any(c in thread_id for c in "/\\")
            or thread_id.startswith(".")
            or len(thread_id) > 128
            or any(ord(c) < 0x20 or ord(c) == 0x7f for c in thread_id)):
        return JSONResponse(status_code=400, content={
            "error": "thread_id must be a plain id (letters, digits, "
                     "'-', '_', '.'), max 128 chars, no control "
                     "characters"})
    # Same read-only tool half the console chat gets, plus document-scoped
    # knowledge. Nothing mutating is exposed over this endpoint.
    try:
        from skills import tool_payload as _tool_payload
        from flags import is_enabled as _flag_on
        tools_on = bool(_flag_on("chat.tools", True))
    except Exception:
        tools_on = True
    try:
        from skills import tool_payload as _tp
        tools_payload = _tp(_CHAT_TOOLS) if tools_on else None
    except Exception:
        tools_payload = None
    history_messages = parsed["messages"]
    # A caller that sent its own transcript owns the history; one that sent only
    # a query gets ours. Mixing the two would duplicate turns.
    if not history_messages:
        with _CHAT_THREADS_LOCK:
            data = _chat_threads_load()
            thread = data.get(thread_id) or {}
            history_messages = [
                {"role": m.get("role"), "content": m.get("content")}
                for m in (thread.get("messages") or [])[-(_CHAT_MAX_TURNS * 2):]
                if isinstance(m, dict) and m.get("content")
            ]
    if parsed.get("query_from_messages"):
        # The query WAS the final user message of the caller's
        # own transcript (no top-level query was sent): appending
        # it to the history again would send the turn to the
        # model twice and answer it twice.
        messages = ([{"role": "system", "content": _chat_system_now()}]
                    + history_messages)
    else:
        messages = ([{"role": "system", "content": _chat_system_now()}]
                    + history_messages
                    + [{"role": "user", "content": query}])
    try:
        messages, doc_hits = await _with_doc_context(
            messages, query, _conversation_doc_ids(thread_id, body))
    except Exception as e:
        # Retrieval is best-effort; never fail the turn on it.
        print(f"[agui] doc context unavailable ({e!r})")
        doc_hits = 0
    try:
        from gateway import LLM, ensure_gateway
        await _aio.to_thread(ensure_gateway)
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "error": f"gateway unavailable: {e}"})
    cost_before = await _aio.to_thread(_session_cost_breakdown,
                                         thread_id)
    pending: list[dict] = []
    call_seq = 0
    # The id of the tool call currently awaiting its result. This is kept in a
    # plain variable rather than pushed onto `pending`: `pending` holds events,
    # and an earlier version queued a bookkeeping dict there too, which then
    # reached the serialiser and blew up mid-stream with
    # "'str' object has no attribute 'get'".
    open_call: dict[str, str] = {}
    started_at = _time.monotonic()
    def emit(event: dict) -> None:
        pending.append(event)
    def on_tool_event(kind: str, payload: dict) -> None:
        """Structured tool progress → real TOOL_CALL_* events.
        The chat path flattens this to a status line; here it is the point of
        speaking AG-UI, so it is kept structured and correlated by
        `toolCallId` as the spec requires.
        """
        nonlocal call_seq
        name = str(payload.get("name") or "tool")
        try:
            if kind == "tool_call":
                call_seq += 1
                call_id = f"call-{call_seq}"
                emit(_agui.activity_snapshot(message_id, f"using {name}."))
                emit(_agui.tool_call_start(call_id, name, parent=message_id))
                # The runner carries the call's arguments in the
                # event payload — emit them, not an empty object:
                # a client correlating the call otherwise cannot
                # see what was actually dispatched.
                try:
                    args_text = json.dumps(
                        payload.get("arguments") or {})[:2000]
                except Exception:
                    args_text = "{}"
                emit(_agui.tool_call_args(call_id, args_text))
                emit(_agui.tool_call_end(call_id, name))
                open_call["id"] = call_id
                open_call["name"] = name
            elif kind == "tool_result":
                ok = bool(payload.get("ok", True))
                emit(_agui.tool_call_result(
                    open_call.get("id", "call-0"), message_id,
                    f"{name}: {'ok' if ok else 'failed'}"))
                open_call.clear()
        except Exception as e:  # progress must never break the turn
            print(f"[agui] tool event failed ({e!r})")
    async def gen():
        yield _agui.run_started(thread_id, run_id)
        yield _agui.text_start(message_id)
        if doc_hits:
            pending.append(_agui.activity_snapshot(
                message_id,
                f"using {doc_hits} chunk(s) from your documents."))
        answer = ""
        spent = ""
        try:
            async def _work() -> dict:
                if tools_payload:
                    try:
                        from mcp_runner import run_with_tools as _run
                        import outcomes as _outcomes
                        return await _run(
                            messages=messages, tools_payload=tools_payload,
                            agent="chat", session_id=thread_id,
                            max_tokens=2048, temperature=0.7,
                            doc_ids=_conversation_doc_ids(thread_id, body),
                            on_event=on_tool_event,
                            on_outcome=lambda n, a, ok, t, lat:
                                _outcomes.on_tool_outcome(
                                    name=n, arguments=a, ok=ok,
                                    result_text=t, latency_s=lat,
                                    session_id=thread_id, run_id=thread_id))
                    except Exception as te:
                        print(f"[agui] tool loop failed, plain-text fallback: "
                              f"{type(te).__name__}: {te}")
                return LLM().chat(messages=messages, agent="chat",
                                  session=thread_id, max_tokens=2048,
                                  temperature=0.7)
            yield _agui.step_started("chat")
            task = asyncio.create_task(_work())
            last_beat = _time.monotonic()
            try:
                while not task.done():
                    await asyncio.sleep(0.15)
                    while pending:
                        yield pending.pop(0)
                    now = _time.monotonic()
                    if now - last_beat >= _CHAT_STREAM_HEARTBEAT_S:
                        last_beat = now
                        yield _agui.activity_snapshot(
                            message_id,
                            f"still working… {int(now - started_at)}s elapsed")
                reply = await task
            finally:
                if not task.done():
                    task.cancel()
            while pending:
                yield pending.pop(0)
            answer = (reply.get("text") or "").strip() or _chat_empty_answer(reply)
            spent = answer
        except asyncio.CancelledError:
            # Client disconnected. No terminal event for a stream nobody reads.
            raise
        except Exception as e:
            yield _agui.text_end(message_id)
            yield _agui.run_error(f"chat failed: {e}")
            return
        finally:
            if spent:
                try:
                    delta = await asyncio.to_thread(_session_cost_delta,
                                                 thread_id, cost_before)
                    if delta:
                        _record_turn_cost(thread_id, query, delta)
                except Exception:
                    pass
        for chunk in _chat_chunks(answer):
            yield _agui.text_content(message_id, chunk)
            await asyncio.sleep(_CHAT_STREAM_PACE_S)
        yield _agui.text_end(message_id)
        # Persistence inside the try, for the same reason the chat stream does
        # it there: an exception must not truncate the stream into "deltas and
        # no terminal frame", which renders a partial answer as a finished one.
        try:
            with _CHAT_THREADS_LOCK:
                data = _chat_threads_load()
                thread = data.get(thread_id) or {
                    "title": query[:60], "updated": 0, "messages": []}
                hist = thread.get("messages") or []
                thread["messages"] = (
                    hist + [{"role": "user", "content": query},
                            {"role": "assistant", "content": answer}]
                )[-(_CHAT_MAX_TURNS * 2):]
                thread["mode"] = "dag"
                thread["updated"] = _time.time()
                if not thread.get("title"):
                    thread["title"] = query[:60]
                data[thread_id] = thread
                if len(data) > _CHAT_MAX_THREADS:
                    for _cid in sorted(
                            data, key=lambda c: data[c].get("updated", 0)
                    )[:len(data) - _CHAT_MAX_THREADS]:
                        # Never silently. An unlogged delete here is how a
                        # conversation vanished from under a user with no
                        # trace and no way to tell it from an explicit delete.
                        print(f"[chat.threads] EVICTED {_cid} - over the "
                              f"{_CHAT_MAX_THREADS}-thread safety valve")
                        del data[_cid]
                _chat_threads_save(data)
        except Exception as e:
            yield _agui.run_error(
                f"answer delivered but could not be saved: {e}")
            return
        yield _agui.run_finished(
            thread_id, run_id, result={"answer": answer})
    # `_SSE_HEADERS` is a kwargs bundle (`{media_type, headers}`), not a bare
    # header dict, so the inner mapping is what gets extended. Spreading the
    # outer one puts a dict where a header value belongs.
    return StreamingResponse(
        _agui.event_stream(gen()), media_type="text/event-stream",
        headers={**_SSE_HEADERS.get("headers", {}),
                 "X-AG-UI-Thread-Id": thread_id,
                 "X-AG-UI-Run-Id": run_id})
# ── A2UI: catalog + agent-generated surfaces (Apps) ──────────────────────────
# See `a2ui_surfaces.py` for the untrusted-input rules. The short version: the
# model may only emit components from a closed catalog, nothing in a payload
# executes, there is no text input and no submit action, and every surface is
# validated at GENERATION time so a rejected one never reaches the client.
@app.get("/api/a2ui/catalog")
async def a2ui_catalog_endpoint():
    """The catalog an agent must speak, and the stable `catalogId` for it.
    Public within the API surface (i.e. token-gated like everything else). It is
    a document, not data: it exposes no user information and is the thing a
    client fetches before rendering anything.
    """
    import a2ui_catalog as _cat
    return _cat.catalog_document()
@app.post("/api/a2ui/validate")
async def a2ui_validate(req: Request):
    """Validate a surface a client already holds, without generating one.
    Useful for the console re-checking something it received before render, and
    for anyone hand-writing a surface against the catalog.
    """
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "body must be JSON"})
    import a2ui_catalog as _cat
    try:
        surface = _cat.validate_surface(body)
    except _cat.CatalogError as e:
        # 422, not 400: the request was well-formed JSON, it was the content
        # that is not a valid surface. The distinction matters to a client
        # deciding whether to retry or give up.
        return JSONResponse(status_code=422, content={
            "ok": False, "error": str(e)})
    import a2ui_surfaces as _sf
    return {"ok": True, "surface": surface,
            "messages": _sf.surface_messages(surface),
            "summary": _sf.surface_summary(surface)}
@app.post("/api/a2ui/generate")
async def a2ui_generate(req: Request):
    """Ask the agent for a view, and return it only if it validates.
    Read-only by construction: the generation call gets no tools, so producing
    a UI cannot reach the filesystem, the network, or an account. Everything the
    surface shows must come from `context`, which the caller supplies.
    """
    try:
        from flags import is_enabled as _flag_on
        if not _flag_on("apps.a2ui", True):
            return JSONResponse(status_code=404, content={
                "error": "A2UI generation disabled (flag apps.a2ui)"})
    except Exception:
        pass
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "body must be JSON"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "body must be an object"})
    request = str(body.get("request") or "").strip()
    context = body.get("context")
    if context is not None and not isinstance(context, dict):
        return JSONResponse(status_code=400, content={
            "error": "context must be an object"})
    # The summary is the only context the model sees, and it is built here
    # rather than passed through, so a caller cannot smuggle a prompt in.
    context = {"summary": _a2ui_context_summary(context)}
    import a2ui_surfaces as _sf
    try:
        from gateway import LLM, ensure_gateway
        ensure_gateway()
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "error": f"gateway unavailable: {e}"})
    async def _call(system: str, user: str, max_tokens: int) -> str:
        reply = await asyncio.to_thread(
            LLM().chat, messages=[{"role": "system", "content": system},
                                  {"role": "user", "content": user}],
            agent="chat", session="a2ui", max_tokens=max_tokens,
            temperature=0.2)
        return (reply or {}).get("text") or ""
    try:
        out = await asyncio.wait_for(
            _sf.generate_surface(request, context, call_model=_call),
            timeout=_sf.GENERATION_TIMEOUT_S)
    except asyncio.TimeoutError:
        return JSONResponse(status_code=504, content={
            "ok": False, "error": "surface generation timed out"})
    except _sf.GenerationRejected as e:
        return JSONResponse(status_code=422, content={
            "ok": False, "error": str(e), "detail": e.detail})
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "ok": False, "error": f"generation failed: {type(e).__name__}: {e}"})
    return {"ok": True, "attempts": out["attempts"],
            "surface": out["surface"], "messages": out["messages"],
            "summary": _sf.surface_summary(out["surface"])}
def _a2ui_context_summary(raw: dict | None) -> str:
    """Flatten caller context into a bounded text summary for the prompt.
    Two bounds, and both are needed. The total is capped so a caller cannot
    inflate token cost. Each VALUE is capped separately, because a single 10 KB
    string would otherwise consume the entire budget on its own and crowd out
    every other field - bounded, but useless. Nested objects are described by
    size, never inlined, so nesting cannot smuggle content either.
    """
    if not raw:
        return "(no data)"
    per_value = 200
    parts: list[str] = []
    for key in sorted(raw)[:40]:
        v = raw[key]
        if isinstance(v, str):
            shown = v[:per_value] + ("…" if len(v) > per_value else "")
            parts.append(f"{key}: {shown}")
        elif isinstance(v, (int, float, bool)) or v is None:
            parts.append(f"{key}: {v}")
        elif isinstance(v, list):
            parts.append(f"{key}: [{len(v)} items]")
        elif isinstance(v, dict):
            parts.append(f"{key}: {{{len(v)} keys}}")
        else:
            parts.append(f"{key}: {type(v).__name__}")
    text = "; ".join(parts)
    return text[:2000] if text else "(no data)"
# ── per-conversation document access ────────────────────────────────────────
# Documents are ON by default once one is enabled; a conversation can turn
# them off, which excludes every document chunk from its prompt. Stored with
# the thread so the choice survives a reload and is per conversation rather
# than global.
@app.get("/api/chat/threads/{conversation_id}/prefs")
async def chat_thread_prefs(conversation_id: str):
    # A prefs read on a thread that does not exist used to return 200 with
    # defaults, while `GET /api/chat/threads/{id}` on the same id correctly
    # 404d. A client therefore could not distinguish "exists with defaults"
    # from "does not exist", and `POST .../prefs` materialised threads with no
    # messages that polled the list forever.
    with _CHAT_THREADS_LOCK:
        if conversation_id not in _chat_threads_load():
            return JSONResponse(status_code=404,
                                content={"error": "unknown thread"})
    try:
        return _chat_prefs(conversation_id)
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "error": f"could not read preferences: {e}"[:200]})
@app.post("/api/chat/threads/{conversation_id}/prefs")
async def chat_thread_set_prefs(conversation_id: str, req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be an object"})
    flag = body.get("use_documents")
    if not isinstance(flag, bool):
        return JSONResponse(status_code=400,
                            content={"error": "use_documents must be true or false"})
    # Same rule as the GET above: refuse to conjure a thread. Preferences
    # belong to a conversation that already exists; writing them for an unknown
    # id created message-less threads that the list then omitted anyway.
    with _CHAT_THREADS_LOCK:
        if conversation_id not in _chat_threads_load():
            return JSONResponse(status_code=404,
                                content={"error": "unknown thread"})
    try:
        return _chat_prefs(conversation_id, use_documents=flag)
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "error": f"could not save preferences: {e}"[:200]})
def _chat_prefs(conversation_id: str,
                use_documents: bool | None = None) -> dict:
    """Read or update one conversation's retrieval preferences."""
    with _CHAT_THREADS_LOCK:
        data = _chat_threads_load()
        thread = data.get(conversation_id) or {}
        prefs = thread.get("prefs") or {}
        if use_documents is not None:
            prefs["use_documents"] = bool(use_documents)
            thread["prefs"] = prefs
            data[conversation_id] = thread
            _chat_threads_save(data)
        # A malformed stored value must not silently disable documents.
        flag = prefs.get("use_documents", True)
        return {"prefs": {"use_documents": flag if isinstance(flag, bool)
                          else True}}
def _conversation_doc_ids(conversation_id: str | None,
                          body: dict | None = None) -> set[str] | None:
    """Which documents a conversation may draw on.
    `None` means no filtering (the caller is not filtering at all); an empty
    set means "no documents", which is what the per-conversation toggle and a
    disabled document both produce.
    A per-request `use_documents` in the BODY wins over the stored preference.
    It used to be accepted by the schema and then dropped on the floor: a client
    that sent `{"use_documents": false}` got documents injected anyway and the
    stored preference stayed true, so the only way to turn retrieval off was a
    second round trip to the prefs endpoint. A field that is accepted and
    ignored is worse than one that is rejected.
    The toggle used to be ignored here as well: this took a `conversation_id`,
    never read it, and returned every enabled document in the registry. The
    console wrote `use_documents` and rendered the resulting state faithfully, so
    the button looked like it governed retrieval while changing nothing at all.
    Docs-off is answered from the stored preference alone and never consults the
    registry, so it holds even when the gateway is unreachable.
    """
    if not conversation_id:
        return None
    # An explicit boolean in the body is authoritative for THIS turn.
    # Anything else (a string, a number, null) is ignored rather than coerced:
    # `use_documents: "false"` is truthy in Python, so coercing it would turn a
    # client that meant OFF into documents ON - the exact inversion the caller
    # was trying to avoid.
    if isinstance(body, dict) and isinstance(body.get("use_documents"), bool):
        if not body["use_documents"]:
            return set()
    else:
        try:
            if not _chat_prefs(conversation_id)["prefs"].get("use_documents", True):
                return set()
        except Exception:
            # The preference store is unreadable: fail closed rather than
            # re-exposing every document on a toggle the user believes is off.
            return set()
    try:
        return _gw_enabled_doc_ids()
    except Exception:
        # The registry is unreachable: fail closed for document access rather
        # than accidentally exposing every document.
        return set()
def _gw_enabled_doc_ids() -> set[str]:
    """The set of document ids currently enabled, straight from the gateway."""
    import memory as _m
    data = _m._get("/v1/documents")
    return {d.get("id") for d in (data.get("documents") or [])
            if d.get("enabled") and d.get("status") == "ready"}
def _gw_enabled_documents() -> list[dict]:
    """Filename + id for every document currently enabled and ready.
    Used only to name documents in the "retrieval matched nothing" turn, so a
    model that finds nothing still knows what the user has uploaded instead of
    assuming they have none.
    """
    import memory as _m
    try:
        data = _m._get("/v1/documents")
    except Exception:
        return []
    return [{"id": d.get("id"), "filename": d.get("filename") or d.get("id")}
            for d in (data.get("documents") or [])
            if d.get("enabled") and d.get("status") == "ready"]
def _gw_all_documents() -> list[dict]:
    """Every document with the state that decides whether it can answer.
    Needed because "no enabled documents" is not the same as "no documents":
    a user who switched their menu off, or whose upload is still parsing, still
    HAS documents, and telling the model otherwise is what sends it off to
    Gmail.
    """
    import memory as _m
    try:
        data = _m._get("/v1/documents")
    except Exception:
        return []
    out = []
    for d in (data.get("documents") or []):
        status = d.get("status") or "unknown"
        ready = status == "ready"
        out.append({
            "id": d.get("id"),
            "filename": d.get("filename") or d.get("id"),
            "ready": ready,
            "enabled": bool(d.get("enabled")),
            "status": status,
        })
    return out
# The rule the model keeps getting wrong, stated once and reused. It is
# deliberately blunt: the failure it prevents is the model deciding the
# uploaded material "isn't it", fetching the answer from an integration, and
# then reporting that integration's credentials as the reason it cannot answer.
_NO_SUBSTITUTION_RULE = (
    "Do NOT treat this as a permissions problem and do NOT go looking for the "
    "answer in email, calendar, chat, a knowledge base or any other "
    "connected system. Those are different systems: if a question sounds like "
    "it is about the user's own files, it is not about their inbox. Never "
    "report an unrelated integration's authentication or connectivity failure "
    "as the reason you cannot answer. Instead, say plainly what you do have "
    "and ask what they want to know.\n"
)
@app.post("/api/documents/search")
async def documents_search(req: Request):
    """Retrieval-only search over enabled documents - lets the console verify
    indexing without spending an LLM call."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    try:
        return _gw_response(await _gw_json("/v1/documents/search", method="POST", json=body))
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "error": f"gateway unreachable: {e}"[:200]})
# ── Code workspace (read-only) ──────────────────────────────────────────────
# Backs the console's Code section. Read-only and deliberately narrow: the
# roots are fixed here and never taken from the request, extensions are
# allow-listed, and every resolved path must still sit inside its root after
# symlink resolution. There is no write, move, delete or execute path, so
# this cannot be used to modify or exfiltrate anything on the host.

# The active Code workspace.
#   BASE    - client-relative paths resolve against this, and the tree
#             lists it.
#   ALLOWED - the confinement allow-list; every path must resolve under
#             one of these, so a symlink cannot escape the workspace.
#
# The default is an EMPTY scratch folder, not the repository. It used to
# default to the whole project with both service trees browsable, which meant
# the Code section opened on 111 rows of the agent's own source and the user's
# own chats: a wall of files nobody asked to see, in a tool whose purpose is to
# work on something you chose. Starting blank makes "Open Folder" and "New
# Playground" the obvious first move instead of a buried setting, and the
# sandbox is no wider than before - an empty allow-list entry confines strictly
# harder than two service trees did.
#
# BASE is the scratch folder's PARENT, not the folder. The client's top-level
# entries are `roots[].name`, and it then requests `tree?path=<name>` - so the
# base has to be one level ABOVE the folder, exactly as "Open Folder" and
# "New Playground" both do. Setting base to the folder itself made every lookup
# resolve to a nested `code_workspace/code_workspace`, which 404s: the explorer
# came up empty with a "retry" button and no way to open a folder at all.
_CODE_PLAIN: Path = Path(__file__).resolve().parent / "state" / "code_workspace"
_CODE_PLAIN.mkdir(parents=True, exist_ok=True)
_CODE_ROOT = _CODE_PLAIN.parent
_CODE_ROOTS: tuple[str, ...] = ()
# What "is_default" compares against: the shipped blank workspace.
_CODE_DEFAULT_BASE: Path = _CODE_PLAIN.parent
_CODE_BASE: Path = _CODE_PLAIN.parent
_CODE_ALLOWED: tuple[Path, ...] = (_CODE_PLAIN.resolve(),)


def _code_workspace() -> tuple[Path, tuple[Path, ...]]:
    """Current (base, allowed-roots). Read per call, never captured."""
    return _CODE_BASE, _CODE_ALLOWED


def _code_agent_root() -> Path:
    """The folder the coding agent's tools are confined to: the browsed
    workspace root ITSELF, not its parent.

    The parent (base) exists only so the client's paths read "name/...";
    handing the base to the tool child let it read SIBLINGS of the workspace -
    with the shipped blank default that meant the whole state dir, agent.token
    included. One folder in, one folder out.
    """
    _base, allowed = _code_workspace()
    return allowed[0] if allowed else _base


def _code_agent_rel(path: str) -> str:
    """Map a client path ("<root>/dir/file") to a tool path ("dir/file").

    The explorer speaks base-relative paths with the root's name baked in;
    the tool child is rooted AT the workspace folder, so the prefix comes off
    before the model ever sees the path.
    """
    rel = (path or "").strip().replace("\\", "/").lstrip("/")
    name = _code_agent_root().name
    if name and (rel == name or rel.startswith(name + "/")):
        rel = rel[len(name):].lstrip("/")
    return rel


def _code_client_path(agent_path: str) -> str:
    """Tool path -> client-visible path ("<root-name>/dir/file").

    The inverse of _code_agent_rel. The console's /api/code routes resolve
    against the base with the root's name baked in, so a tool path handed
    straight to the UI ('bye.py' from a permission frame's args) missed
    every route - the post-approval open of the written file 403'd.
    """
    rel = _code_agent_rel(agent_path)
    name = _code_agent_root().name
    return f"{name}/{rel}" if rel else name


def _code_agent_abs(path: str) -> Path | None:
    """Resolve a tool-relative path under the agent's workspace root, or None
    if it escapes - the same confinement as _code_abs, anchored differently."""
    root = _code_agent_root().resolve()
    rel = _code_agent_rel(path)
    try:
        p = (root / rel).resolve() if rel else root
    except OSError:
        return None
    if p != root and root not in p.parents:
        return None
    return p
_CODE_EXTS = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".yaml",
    ".yml", ".toml", ".md", ".txt", ".css", ".html", ".sh", ".sql", ".ini",
    ".cfg", ".env.example", ".gitignore",
})
# Never listed, never read: build output, vendored trees and secrets.
_CODE_SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "test-results",
    "playwright-report", ".next", ".cache", "coverage", ".idea", ".vscode",
})
_CODE_SKIP_FILES = frozenset({".env", "agent.err", "gateway.err"})
_CODE_MAX_BYTES = 512 * 1024
# Reading a file from disk is not the same limit as an unsaved BUFFER: the
# editor must be able to open a large source file (agent_server.py itself is
# over 512 KB), while the per-question draft buffer stays bounded.
_CODE_MAX_FILE_BYTES = 1024 * 1024
# A model id is `vendor/name`, optionally with a `:free` or `@vN` suffix -
# the shape Kilo, OpenRouter and Ollama all use. Anchored and length-capped
# so a crafted value cannot smuggle a URL or a newline into the gateway.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}/[A-Za-z0-9._:@-]{1,96}$")
# ── the coding agent's toolset ──────────────────────────────────────────────
# Exactly what it needs to explore the codebase freely and then verify its
# work. All of these already exist on the MCP server; this list is the subset
# the Code panel's agent is allowed to see, and the allowlist is the point -
# a 400-file repo plus the full server surface is an unusable prompt for a
# small model. The list is also ENFORCED at dispatch (run_with_tools):
# tool schemas are advice to the model, not a boundary.
## Read-only on disk: every tool below that WRITES goes through the operator's
## approval gate first (`_code_write_gate` below). The model proposes, the
## panel shows the exact diff, and only an explicit accept lets it reach disk -
## so a question typed into a panel can still never rewrite a file behind the
## user's back. `preview_edit` stays because it only ever returns a diff.
CODE_AGENT_TOOLS = [
    # orient
    "list_dir", "read_file", "search_files",
    # locate precisely. Deliberately NO `find_symbol`: it is regex and cannot
    # tell a definition from a call site, so it duplicates `search_files` while
    # being weaker than `find_references` (AST, excludes comments/strings).
    # Three tools for one question confused a small model; two do the job.
    "document_symbols", "find_references", "blast_radius",
    # understand the current state
    "git_inspect", "read_artifact",
    # propose without writing
    "preview_edit",
    # prove it works
    "run_checks", "run_command",
    # WRITE - intercepted by _approve: gated in manual mode, snapshotted and
    # reviewable in auto mode.
    "edit_file", "create_file", "make_dir", "update_file", "delete_file",
    "apply_symbol_edit",]

# Tools that change files. Every one is intercepted by _approve before it
# reaches the MCP child: manual mode parks it until the operator decides;
# auto mode snapshots the file first and dispatches, leaving the change
# reviewable (Keep / Revert) instead of gated. Keep this in sync with
# CODE_AGENT_TOOLS: a write tool added there and not here would write without
# either the gate or the snapshot.
_CODE_WRITE_TOOLS = frozenset({
    "edit_file", "create_file", "make_dir", "update_file", "delete_file",
    "apply_symbol_edit",
})

# Tools that RUN the project's own code. Not file writes, but the same trust
# question: a test suite / package script / PowerShell command is arbitrary
# repository code, so the agent must not be able to execute it unattended.
# Gated like a write.
_CODE_EXEC_TOOLS = frozenset({"run_checks", "run_command"})

# Pending write approvals, keyed by a short id. Value carries the asyncio Event
# the tool loop is parked on plus the decision, so POSTing the decision from the
# panel releases exactly that one call. Bounded: an entry is removed the moment
# it is resolved, and an abandoned run leaves at most one entry per write the
# model attempted.
_CODE_PERM_LOCK = threading.Lock()
_CODE_PERM: dict[str, dict] = {}
_CODE_PERM_MAX = 64
_CODE_PERM_WAIT_S = 300.0


def _code_perm_put(pid: str) -> dict:
    entry = {"event": asyncio.Event(), "decision": None, "at": _time.time()}
    with _CODE_PERM_LOCK:
        if len(_CODE_PERM) >= _CODE_PERM_MAX:
            # Drop the oldest unresolved entry. Its waiter never resolves, so
            # that one call fails closed at the wait timeout rather than
            # holding the run open forever.
            oldest = min(_CODE_PERM, key=lambda k: _CODE_PERM[k]["at"])
            _CODE_PERM.pop(oldest, None)
        _CODE_PERM[pid] = entry
    return entry


def _code_perm_resolve(pid: str, approve: bool) -> bool:
    """Record an operator decision and release the parked write. True if the
    id was live (so the caller can answer 404 for an unknown/expired one)."""
    with _CODE_PERM_LOCK:
        entry = _CODE_PERM.get(pid)
    if entry is None:
        return False
    entry["decision"] = bool(approve)
    try:
        entry["event"].set()
    except Exception:
        pass
    return True


def _code_perm_drop(pid: str) -> None:
    with _CODE_PERM_LOCK:
        _CODE_PERM.pop(pid, None)


def _code_write_diff(name: str, args: dict) -> tuple[str, str]:
    """(summary, unified diff) for a proposed write, computed without the MCP
    child so the panel can show the exact patch while deciding.

    Best effort by design: an unreadable file or an unknown tool yields a
    description and no diff, and the operator still gets the accept/refuse
    choice - a missing diff must never silently become a missing gate.
    """
    import difflib as _dl
    path = str(args.get("path") or "")
    # Tool-relative, like every path the model sees: the child's sandbox is
    # the workspace FOLDER, not the client's base one level above it.
    target = _code_agent_abs(path) if path else None
    try:
        old = (target.read_text(encoding="utf-8", errors="replace")
               if target is not None and target.is_file() else "")
    except Exception:
        old = ""
    new = old
    custom: str | None = None
    if name == "create_file":
        new = str(args.get("content") or "")
        summary = f"create {path}"
    elif name == "make_dir":
        summary = f"create folder {path}"
    elif name == "update_file":
        new = str(args.get("content") or "")
        summary = f"overwrite {path}"
    elif name == "delete_file":
        new = ""
        summary = f"delete {path}"
    elif name == "edit_file":
        find = str(args.get("find") or "")
        repl = str(args.get("replace") or "")
        if find and find in old:
            new = (old.replace(find, repl)
                   if args.get("replace_all") else old.replace(find, repl, 1))
            summary = (f"edit {path}: replace "
                       f"{old.count(find)}x {find[:40]!r}")
        else:
            summary = f"edit {path}: {find[:60]!r} not found"
    elif name == "apply_symbol_edit":
        summary = (f"apply {args.get('mode') or 'replace'} to "
                   f"{args.get('name')!r} in {path}")
        # The diff needs the symbol's span, which only the intel module knows.
        # The child's sandbox is not configured in this process, so resolve
        # the file here and ask the intel module for the patch directly - the
        # same code path preview_edit uses. Best effort: a failure still
        # leaves the summary and the accept/refuse gate.
        try:
            import mcp_server as _mcp
            _ch = _mcp._code_intel.preview(
                str(target) if target is not None else path,
                str(args.get("name") or ""),
                str(args.get("code") or ""),
                mode=str(args.get("mode") or "replace"))
            custom = _mcp._code_intel.diff_for(_ch) or ""
            if getattr(_ch, "detail", ""):
                summary += f" - {_ch.detail}"
        except Exception:
            custom = ""
    else:
        summary = f"{name} {path}".strip()
    if custom is not None:
        diff = custom
    else:
        try:
            diff = "\n".join(_dl.unified_diff(
                old.splitlines(), new.splitlines(),
                fromfile=f"a/{path or '?'}", tofile=f"b/{path or '?'}",
                lineterm="", n=3))
        except Exception:
            diff = ""
    if len(diff) > 20000:
        diff = diff[:20000] + "\n... [diff truncated]"
    return summary, diff


# ── The agent's changes, awaiting the user's review ──────────────────────────
# Every write snapshots the file's bytes BEFORE anything touches it, keyed by
# client path. That snapshot is what Revert restores and what Keep drops. The
# FIRST snapshot for a path wins: reverting undoes everything the agent did to
# that file since the user last reviewed it, not one hop of it. In memory on
# purpose - an unreviewed change belongs to a live session, and a dead server
# has no writer left to review; the files themselves are never lost, only the
# revert target.
_CODE_REVIEW: dict[str, dict] = {}
_CODE_REVIEW_MAX = 200
_CODE_REVIEW_SEQ = [0]


def _code_review_take(client_path: str, tool: str, summary: str,
                      diff: str) -> int | None:
    """Snapshot client_path before a write. Returns a token for the entry that
    exists AFTER this call (this attempt's entry, or an earlier unreviewed
    one), or None when the path cannot be resolved. Best effort: a failed
    snapshot must never block the write itself."""
    if not client_path:
        return None
    try:
        target = _code_agent_abs(client_path)
    except Exception:
        target = None
    if target is None or (target.exists() and not target.is_file()):
        return None
    try:
        existed = target.is_file()
        before = target.read_bytes() if existed else None
    except OSError:
        return None
    entry = _CODE_REVIEW.get(client_path)
    if entry is None:
        if len(_CODE_REVIEW) >= _CODE_REVIEW_MAX:
            _CODE_REVIEW.pop(next(iter(_CODE_REVIEW)), None)
        _CODE_REVIEW_SEQ[0] += 1
        entry = {"token": _CODE_REVIEW_SEQ[0], "path": client_path,
                 "before": before, "existed": existed}
        _CODE_REVIEW[client_path] = entry
    entry.update(tool=tool, summary=summary, diff=diff, when=_time.time())
    return entry["token"]


def _code_review_drop_token(client_path: str, token: int | None) -> None:
    """Drop this attempt's snapshot after a DECLINED write - but never an
    earlier unreviewed change to the same file: a refusal must not cancel
    someone else's pending review."""
    entry = _CODE_REVIEW.get(client_path)
    if entry is not None and token is not None and entry.get("token") == token:
        _CODE_REVIEW.pop(client_path, None)


def _code_review_settle(client_path: str, token: int | None) -> None:
    """Drop a review entry that no longer describes a real change.

    Called AFTER a write was attempted, whatever the outcome. A write that
    failed, was refused, or put back identical bytes must not leave an entry:
    the panel listed a phantom change and Revert "restored" a state nobody had
    altered. Comparing disk against the snapshot is the honest test - and it
    also preserves an EARLIER unreviewed change to the same file, because the
    disk still differs from that first snapshot.
    """
    if not client_path or token is None:
        return
    entry = _CODE_REVIEW.get(client_path)
    if entry is None or entry.get("token") != token:
        return
    target = _code_agent_abs(client_path)
    try:
        now_exists = bool(target is not None and target.is_file())
        now = target.read_bytes() if now_exists else None
    except OSError:
        return
    if now_exists == bool(entry.get("existed")) and now == entry.get("before"):
        _CODE_REVIEW.pop(client_path, None)


def _code_review_restore(entry: dict) -> tuple[bool, str]:
    """Put the file back exactly as the snapshot found it: bytes restored, or
    removed when the agent created it."""
    target = _code_agent_abs(str(entry.get("path") or ""))
    if target is None:
        return False, "the path no longer resolves inside the workspace"
    try:
        if entry.get("existed"):
            data = entry.get("before")
            if data is None:
                return False, "the snapshot has no content to restore"
            tmp = target.with_name(
                f".{target.name}.aria-revert-{os.getpid()}")
            tmp.write_bytes(data)
            os.replace(tmp, target)
            return True, ""
        if target.is_file():
            target.unlink()
        elif target.is_dir():
            # A folder the agent created reverts like a file it created: it
            # is removed - but only while empty. Anything inside carries its
            # own review entry and must never be deleted as a side effect.
            try:
                target.rmdir()
            except OSError:
                return False, "the folder is not empty - revert its files first"
        return True, ""
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"

# A finite default ceiling. "Unlimited" was tried and it is the wrong default
# for an editor: a model that keeps calling tools looks busy, bills every hop,
# and the user has no idea it will never answer. 60 model calls is far beyond a
# real investigation (orient, search, read, edit, verify is ~10-20) and still
# cheap to abandon. A caller that explicitly asks for `max_hops: 0` gets the
# old unlimited behaviour, and a positive request is clamped to 200.
_CODE_AGENT_MAX_HOPS = 60
_CODE_AGENT_MAX_HOPS_CAP = 200
# The accumulating tool transcript is bounded too: below this the oldest tool
# results are elided, above it the run stops with a stated reason instead of
# sending a prompt the gateway will reject outright.
_CODE_AGENT_CONTEXT_CHARS = 600_000
# Per-file slice in the system prompt. The model has read_file for the rest;
# this is orientation, not the answer.
_CODE_AGENT_BUFFER_CHARS = 4000
# Thinking is collapsed in the UI, so a hard ceiling stops a rambling model
# from shipping a megabyte of monologue to the browser.
_CODE_AGENT_THINKING_CHARS = 8000
# The coding agent routes by MODEL ID, not by provider pin: the gateway only
# hands a Kilo model to the `code` agent when the request carries one of the
# discovered Kilo ids (see route_for_model / CODE_MODEL_ROUTES), otherwise the
# run falls through to the general ladder and lands on Gemini - whose
# tool loop then 400s, because Gemini requires a thought_signature echoed back
# on every functionCall. Measured fastest of the free roster; kept in step with
# kilo_models._CODE_MODEL_PREFERENCE and the gateway's reported default_model.
_CODE_AGENT_DEFAULT_MODEL = "cohere/north-mini-code:free"

def _tool_detail(args) -> str:
    """One short line describing a tool call, for the transcript.
    A raw JSON dump is unreadable in a list; the model reads it as a wall of
    punctuation. Show the arguments that identify WHICH file/symbol instead.
    """
    if not isinstance(args, dict):
        return ""
    for key in ("path", "name", "query", "pattern", "target", "content",
                "text", "root", "dir"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            s = " ".join(v.split())
            return s if len(s) <= 120 else s[:117] + "..."
    return ""


def _thinking_is_answer(answer: str, thinking: str) -> bool:
    """True when the model's reasoning channel carries the ANSWER, not reasoning.

    Observed live with cohere/north-mini-code: the whole reply arrives on the
    reasoning channel and is then repeated as the answer, so the panel rendered
    the same wall of prose twice — once collapsed under "thinking", once as the
    answer. The transcript looked like the agent had fused two replies together.

    Comparison is by "substantial lines" (>= 40 chars), not by string equality:
    the reasoning copy is capped and lightly reformatted, so it is never a byte
    match. At least half of the smaller set's lines appearing in the other is
    the same text, and the user has already read it.
    """
    if not answer.strip() or not thinking.strip():
        return False

    def substantial(s: str) -> set[str]:
        return {ln.strip() for ln in s.splitlines()
                if len(ln.strip()) >= 40}

    a = substantial(answer)
    t = substantial(thinking)
    if not a or not t:
        return False
    shared = len(a & t)
    # Guard the metric: the comparison that matters is "is the answer already
    # inside the reasoning", so measure against the ANSWER's lines. A shared
    # minimum was too weak - an answer that merely opens with the same sentence
    # as the reasoning scored 50% and got dropped with it.
    return shared >= 2 and shared / len(a) >= 0.6


def _error_detail(exc: BaseException) -> str:
    """One readable line for a failure, unwrapping TaskGroup wrappers.

    The agent loop runs inside a TaskGroup, so the real cause - almost always
    an HTTP status from the gateway - arrives nested in an ExceptionGroup whose
    own str() is only "unhandled errors in a TaskGroup (1 sub-exception)".
    That tells the panel nothing. Walk to the innermost error and report it,
    and when it carries a response, report the status and body instead.
    """
    e: BaseException = exc
    for _ in range(6):
        subs = [s for s in (getattr(e, "exceptions", None) or []) if s is not None]
        if not subs:
            break
        e = subs[0]
    resp = getattr(e, "response", None)
    if resp is not None:
        try:
            body = " ".join((resp.text or "").split())[:200]
        except Exception:
            body = ""
        return (f"gateway HTTP {resp.status_code}: {body}" if body
                else f"gateway HTTP {resp.status_code}")[:400]
    return f"{type(e).__name__}: {e}"[:400]
# How much earlier conversation to keep for /api/code/ask. Bounded from the
# front so the RECENT turns survive - the newest question usually refers back
# to the last one ("and what calls it?"), which is exactly what a small tail
# window would drop.
_CODE_ASK_HISTORY_TURNS = 8
_CODE_ASK_HISTORY_CHARS = 2_000
_CODE_MAX_FILES = 4000
def _code_rel_ok(rel: str) -> bool:
    """Validate a client-supplied workspace-relative path.
    Rejects absolute paths, drive letters, `..`, NUL bytes, backslashes and
    anything that tries to escape via a symlink. The containment check is done
    again after `resolve()` by `_code_abs()` - this is the cheap first pass.
    """
    if not rel or len(rel) > 400:
        return False
    if "\x00" in rel or "\\" in rel:
        return False
    p = PurePosixPath(rel)
    if p.is_absolute() or rel.startswith("/"):
        return False
    if re.match(r"^[A-Za-z]:", rel):
        return False
    return ".." not in p.parts
def _code_abs(rel: str) -> Path | None:
    """Resolve `rel` (relative to the workspace base) into its roots, or None.
    Two independent checks, because either alone is insufficient:
      1. the path must land under one of the fixed roots, and
      2. that test must run on the RESOLVED path - otherwise a symlink inside
         the tree reads whatever it points at, which may be outside.
    `rel` is relative to the workspace base, not to a root itself, so roots are
    only ever used for the containment test.
    """
    if not _code_rel_ok(rel):
        return None
    base, allowed_roots = _code_workspace()
    target = (base / rel).resolve()
    for allowed in allowed_roots:
        try:
            target.relative_to(allowed)
        except ValueError:
            continue
        return target
    return None
def _code_name_visible(name: str) -> bool:
    if name in _CODE_SKIP_FILES:
        return False
    # Hidden files and folders are excluded wholesale: this is where keys and
    # local settings live, and the Code section has no business showing them.
    return not name.startswith(".")
def _code_listing(rel: str) -> dict:
    """One directory level. Capped so a huge tree cannot stall the console."""
    ws_base, allowed = _code_workspace()
    if not rel:
        # The ROOT of the explorer is the workspace's allow-list, not whatever
        # the base directory happens to contain. Those are different things: the
        # base only has to be one level above the roots so client paths read
        # "name/...", and when a single-folder workspace is open the base is
        # that folder's PARENT. Listing the base directly therefore published
        # the siblings of the allowed root - with the state dir as base, the
        # tree showed sessions/, threads/, playgrounds/ and the rest.
        #
        # Synthesising the roots from the allow-list also makes "what can this
        # workspace reach" a single source of truth, rather than depending on
        # what happens to sit next to the base.
        entries: list[dict] = []
        for root in allowed:
            try:
                name = root.relative_to(ws_base.resolve()).as_posix()
            except ValueError:
                # Allowed but not under the base: still listed by its own name.
                name = root.name
            if not name or name == ".":
                continue
            try:
                size = root.stat().st_size if root.is_file() else None
            except OSError:
                size = None
            entries.append({"name": name, "dir": root.is_dir(), **({"size": size} if size is not None else {})})
            if len(entries) >= _CODE_MAX_FILES:
                break
        return {"path": "", "parent": "", "entries": entries}
    base = _code_abs(rel)
    if base is None:
        return {"error": "path outside the code workspace"}
    if not base.exists() or not base.is_dir():
        return {"error": f"not a directory: {rel}"}
    entries: list[dict] = []
    try:
        for child in sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if len(entries) >= _CODE_MAX_FILES:
                break
            if child.name in _CODE_SKIP_DIRS or not _code_name_visible(child.name):
                continue
            if child.is_symlink():
                # A symlink may leave the tree; only keep it if it still
                # resolves inside a Code root.
                if _code_abs(str(Path(rel) / child.name) if rel else child.name) is None:
                    continue
            if child.is_dir():
                entries.append({"name": child.name, "dir": True})
            elif child.suffix in _CODE_EXTS or child.name.endswith(".env.example"):
                try:
                    size = child.stat().st_size
                except OSError:
                    continue
                entries.append({"name": child.name, "dir": False, "size": size})
    except PermissionError:
        return {"error": f"permission denied: {rel}"}
    return {"path": rel, "parent": str(PurePosixPath(rel).parent.as_posix())
            if rel and rel != "." else "",
            "entries": entries}
@app.get("/api/code/roots")
async def code_roots():
    """The workspace the explorer is browsing: its base path and roots."""
    import asyncio
    def _work():
        base, allowed = _code_workspace()
        out = []
        for root in allowed:
            if root == base:
                name = root.name or str(root)
            else:
                try:
                    name = root.relative_to(base).as_posix()
                except ValueError:
                    name = root.name
            out.append({"name": name, "exists": root.is_dir()})
        return {"root": str(base), "roots": out,
                "max_bytes": _CODE_MAX_BYTES}
    return await asyncio.to_thread(_work)
@app.get("/api/code/workspace")
async def code_workspace_get():
    """The current Code workspace (base path + browsable roots)."""
    import asyncio
    def _work():
        base, allowed = _code_workspace()
        default = base.resolve() == _CODE_DEFAULT_BASE.resolve()
        if default:
            name = base.name or str(base)
        else:
            name = (allowed[0].name or str(allowed[0])) \
                if allowed else base.name
        return {"root": str(base), "name": name,
                "roots": [str(r) for r in allowed],
                "is_default": default}
    return await asyncio.to_thread(_work)
@app.post("/api/code/workspace")
async def code_workspace_set(req: Request):
    """Open a folder as the Code workspace, or create a fresh playground.

    The console is a single-operator, loopback, token-gated app, so
    pointing the editor at any directory on this machine is the same trust
    level as the operator's own shell. This re-roots only the /api/code
    routes; the agent's tool sandbox is separate and is not widened.
    """
    import asyncio
    import auth as _auth
    import time as _t
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "body must be JSON"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    global _CODE_BASE, _CODE_ALLOWED
    if body.get("playground"):
        def _mk():
            import secrets
            base = _auth.state_dir() / "playgrounds"
            base.mkdir(parents=True, exist_ok=True)
            folder = base / (_t.strftime("%Y%m%d-%H%M%S") + "-"
                             + secrets.token_hex(3))
            folder.mkdir(parents=True, exist_ok=True)
            return folder
        folder = (await asyncio.to_thread(_mk)).resolve()
        # Base is the PARENT, so the folder is an ordinary root relative
        # to it - exactly like the default workspace - and client paths
        # stay "name/...". Confinement is still only the folder.
        base = folder.parent if folder.parent != folder else folder
        _CODE_BASE = base
        _CODE_ALLOWED = (folder,)
        # Review snapshots are keyed by client path against the OLD root.
        # Carrying them across a switch would let Revert write a snapshot from
        # one project into another file that happens to share the name.
        _CODE_REVIEW.clear()
        return {"root": str(base), "name": folder.name or str(folder),
                "roots": [str(folder)], "is_default": False}
    raw = str(body.get("path") or "").strip()
    if not raw:
        return JSONResponse(status_code=400,
                            content={"error": "path is required"})
    def _resolve():
        p = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not p.is_absolute():
            p = _CODE_BASE / p
        try:
            return p.resolve()
        except OSError:
            return None
    folder = await asyncio.to_thread(_resolve)
    if folder is None or not folder.is_dir():
        return JSONResponse(status_code=400, content={
            "error": f"not a directory: {raw}"})
    base = folder.parent if folder.parent != folder else folder
    _CODE_BASE = base
    _CODE_ALLOWED = (folder,)
    _CODE_REVIEW.clear()
    return {"root": str(base), "name": folder.name or str(folder),
            "roots": [str(folder)], "is_default": False}
@app.post("/api/code/workspace/reset")
async def code_workspace_reset():
    """Return to the default workspace - the empty scratch folder.

    Deliberately NOT the repository any more. "Reset" used to restore a
    browsable view of both service trees, which is the wall-of-files default
    the plain workspace replaced. Resetting to blank is also the only version
    that cannot surprise: an empty allow-list is the narrowest one there is.
    """
    global _CODE_BASE, _CODE_ALLOWED
    _CODE_BASE = _CODE_DEFAULT_BASE
    _CODE_ALLOWED = (_CODE_PLAIN.resolve(),)
    _CODE_REVIEW.clear()
    return {"root": str(_CODE_BASE), "name": _CODE_BASE.name,
            "roots": [str(r) for r in _CODE_ALLOWED], "is_default": True}
@app.get("/api/code/tree")
async def code_tree(path: str = ""):
    """List one directory inside the workspace."""
    import asyncio
    rel = (path or "").strip().lstrip("/")
    if rel in (".", "/"):
        rel = ""
    # Directory walks are blocking syscalls. In an `async def` on a
    # single-worker loop that means every /api/* request queues behind this
    # one, which is how a workspace search could stall the whole agent. The
    # same to_thread pattern the document and memory routes already use.
    return await asyncio.to_thread(_code_listing, rel)
@app.get("/api/code/files")
async def code_files():
    """Flat, sorted index of every servable file in the workspace.
    Quick open needs the whole tree, not just the folders the user happens to
    have expanded - otherwise Ctrl+P can only reopen tabs that are already
    open, which is not a file finder. The walk is iterative (not recursive, so
    a deep tree cannot blow the stack), skips the same ignored directories as
    `/api/code/tree`, applies the same extension allow-list, and stops at
    `_CODE_MAX_FILES`. Only paths are returned; no content, so this cannot
    leak anything the per-file endpoint would refuse.
    """
    import asyncio
    # ~2100 stat() calls per request. Blocking that on the event loop is what
    # made opening the Code view stall every other /api/* route.
    return await asyncio.to_thread(_code_files_all)
def _code_files_all() -> dict:
    base, allowed = _code_workspace()
    files: list[str] = []
    truncated = False
    queue: list[Path] = list(allowed)
    while queue:
        d = queue.pop()
        try:
            children = list(d.iterdir())
        except OSError:
            continue
        for child in children:
            if len(files) >= _CODE_MAX_FILES:
                truncated = True
                queue.clear()
                break
            if child.name in _CODE_SKIP_DIRS or not _code_name_visible(child.name):
                continue
            if child.is_dir():
                queue.append(child)
                continue
            if child.name in _CODE_SKIP_FILES:
                continue
            if child.suffix not in _CODE_EXTS and not child.name.endswith(".env.example"):
                continue
            try:
                rel = child.resolve().relative_to(base.resolve())
            except (OSError, ValueError):
                continue
            files.append(rel.as_posix())
    files.sort()
    return {"files": files, "count": len(files), "truncated": truncated}
@app.get("/api/code/file")
async def code_file(path: str = ""):
    """Read one text file from the workspace.
    Refuses anything over `_CODE_MAX_BYTES`, any extension outside the
    allow-list, and any binary-ish content. Returns 400 for a bad request and
    403 for a path that resolves outside the workspace.
    """
    import asyncio
    return await asyncio.to_thread(_code_read_one, path)
def _code_read_one(path: str):
    rel = (path or "").strip().lstrip("/")
    if not rel:
        return JSONResponse(status_code=400, content={"error": "path is required"})
    target = _code_abs(rel)
    if target is None:
        return JSONResponse(status_code=403, content={
            "error": "path is outside the code workspace"})
    if target.is_dir():
        return JSONResponse(status_code=400, content={"error": "path is a directory"})
    if target.name in _CODE_SKIP_FILES or not _code_name_visible(target.name):
        return JSONResponse(status_code=403, content={"error": "file is not readable"})
    if target.suffix not in _CODE_EXTS and not target.name.endswith(".env.example"):
        return JSONResponse(status_code=403, content={
            "error": f"{target.suffix or 'no extension'} files are not served"})
    try:
        size = target.stat().st_size
    except OSError as e:
        return JSONResponse(status_code=404, content={"error": f"cannot stat: {e}"})
    if size > _CODE_MAX_FILE_BYTES:
        return JSONResponse(status_code=413, content={
            "error": f"file is {size} bytes; the limit is "
                     f"{_CODE_MAX_FILE_BYTES}"})
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return JSONResponse(status_code=415, content={
            "error": "file is not valid UTF-8 text"})
    except OSError as e:
        return JSONResponse(status_code=404, content={"error": f"cannot read: {e}"})
    # A NUL byte means it was not really text (or a .py that is secretly
    # binary); a truncated read would be worse than a refusal.
    if "\x00" in text:
        return JSONResponse(status_code=415, content={"error": "file looks binary"})
    return {
        "path": rel,
        "name": target.name,
        "text": text,
        "bytes": len(text.encode("utf-8")),
        "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1),
        "truncated": False,
        "language": _code_language(target.name),
        "version": f"{int(target.stat().st_mtime)}-{size}",
        # Exact mtime, for the save's drift check. `version` is a display cache
        # key; this is what /api/code/write compares against.
        "mtime": target.stat().st_mtime,
    }
_CODE_LANGS = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescriptreact",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".md": "markdown", ".css": "css", ".html": "html",
    ".sh": "shell", ".sql": "sql", ".txt": "text",
}
@app.post("/api/code/check")
async def code_check(req: Request):
    """Syntax-check a DRAFT and return real diagnostics.
    Takes the buffer text, never a path: the browser holds unsaved edits and
    this must judge what is on screen, not what is on disk. Nothing is written
    and nothing is executed - for Python it is `ast.parse`, which parses and
    discards. That is what makes the Problems panel worth having: it reports
    genuine `line:col` errors with the offending source line, instead of the
    "0 problems" a client-side highlighter would have to invent.
    For languages with no parser available here, the balance check is a
    heuristic and says so in `mode`, rather than pretending to be a compiler.
    """
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "body must be JSON"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    text = body.get("text")
    lang = (body.get("language") or "text")
    if not isinstance(text, str):
        return JSONResponse(status_code=400, content={"error": "text must be a string"})
    if len(text.encode("utf-8", "replace")) > _CODE_MAX_BYTES:
        return JSONResponse(status_code=413, content={
            "error": f"buffer is over the {_CODE_MAX_BYTES} byte check limit"})
    lines = text.split("\n")
    if lang == "python":
        return _check_python(text, lines)
    if lang in ("typescript", "typescriptreact", "javascript", "json"):
        return _check_braces(text, lines)
    return {"mode": "none", "language": lang, "problems": [],
            "checked": False,
            "note": "no parser available for this language"}
def _problem(line: int, col: int, message: str, severity: str = "error") -> dict:
    """1-based line/col, the way editors and Python report them."""
    return {"line": max(1, line), "col": max(1, col),
            "message": message[:300], "severity": severity}
def _check_python(text: str, lines: list[str]) -> dict:
    import ast
    try:
        ast.parse(text)
        return {"mode": "ast", "language": "python", "checked": True, "problems": []}
    except SyntaxError as e:
        ln = e.lineno or 1
        col = (e.offset or 1)
        src = lines[ln - 1].strip() if 0 < ln <= len(lines) else ""
        msg = e.msg or "syntax error"
        return {
            "mode": "ast", "language": "python", "checked": True,
            "problems": [_problem(ln, col, msg, "error")],
            "source": {"line": ln, "text": src[:200]},
        }
    except (ValueError, RecursionError, MemoryError) as e:
        # e.g. source containing NUL bytes, or pathological nesting. Say the
        # check could not run instead of reporting a fake error.
        return {"mode": "ast", "language": "python", "checked": False,
                "problems": [],
                "note": f"could not parse: {type(e).__name__}"}
_BRACKETS = {"(": ")", "[": "]", "{": "}"}
def _check_braces(text: str, lines: list[str]) -> dict:
    """Delimiter balance. A heuristic, and labelled as one - it will not catch
    a missing semicolon or a bad type, only unbalanced structure."""
    stack: list[tuple[str, int, int]] = []
    in_str: str | None = None
    in_line_comment = False
    in_block_comment = False
    prev = ""
    problems: list[dict] = []
    for ln, raw in enumerate(lines, start=1):
        i = 0
        in_line_comment = False
        while i < len(raw):
            ch = raw[i]
            nxt = raw[i + 1] if i + 1 < len(raw) else ""
            if in_line_comment:
                break
            if in_block_comment:
                if ch == "*" and nxt == "/":
                    in_block_comment = False
                    i += 2
                    continue
                i += 1
                continue
            if in_str:
                if ch == "\\":
                    i += 2
                    continue
                if ch == in_str:
                    in_str = None
                i += 1
                continue
            if ch == "/" and nxt == "/":
                in_line_comment = True
                break
            if ch == "/" and nxt == "*":
                in_block_comment = True
                i += 2
                continue
            if ch in "\"'`":
                # A quote only opens a string if it is not an apostrophe in the
                # middle of a word (don't, it's).
                if ch == "'" and (prev.isalnum() or prev == "_"):
                    i += 1
                    prev = ch
                    continue
                in_str = ch
                i += 1
                prev = ch
                continue
            if ch in _BRACKETS:
                stack.append((ch, ln, i + 1))
            elif ch in ")]}":
                want = _BRACKETS.get(stack[-1][0]) if stack else None
                if stack and want == ch:
                    stack.pop()
                else:
                    problems.append(_problem(
                        ln, i + 1,
                        f"unexpected '{ch}'" + (f", expected '{want}'" if want else
                                                 " with nothing open")))
            prev = ch
            i += 1
        if in_line_comment:
            pass
    for ch, ln, col in stack:
        problems.append(_problem(ln, col, f"'{ch}' is never closed"))
    problems.sort(key=lambda p: (p["line"], p["col"]))
    return {"mode": "balance", "language": "text", "checked": True,
            "problems": problems[:100],
            "note": "delimiter balance only - not a compiler"}
@app.get("/api/code/git")
async def code_git(target: str = "status"):
    """Show what changed in the workspace: status, diff, or recent log.
    Read-only, and it is the review step for anything that edited a file. The
    Code view's own edits live in the browser until saved, so this shows what
    is actually on disk - which is the thing that has to be reviewed before it
    is trusted.
    No write verb is offered: this cannot stage, commit or revert."""
    import asyncio as _aio
    t = (target or "status").strip().lower()
    if t not in ("status", "diff", "log"):
        return JSONResponse(status_code=400, content={
            "error": f"target must be status, diff or log; got {target!r}"})
    base, _ = _code_workspace()
    root = base.resolve()
    def _run():
        # Deliberately NOT mcp_server.git_inspect: that tool is sandbox-
        # relative by design, and asking it for "." resolves to the sandbox,
        # which is not a repository - so it answered "nothing to review" for a
        # workspace that is one. This route is about the CODE ROOTS, so it runs
        # git against the workspace base directly.
        import shutil
        import subprocess
        exe = shutil.which("git")
        if not exe:
            return {"ok": False, "error": "git is not installed"}
        if not (root / ".git").exists():
            return {"ok": False,
                    "error": "this workspace is not a git repository - "
                             "nothing to review"}
        argv = [exe, "-C", str(root)]
        argv += {"status": ["status", "--porcelain=v1"],
                 "diff": ["diff", "--no-color"],
                 "log": ["log", "--oneline", "-n", "20"]}[t]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=60)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": proc.returncode == 0, "target": t,
                "exit_code": proc.returncode,
                "output": (proc.stdout or "")[:200_000],
                "truncated": len(proc.stdout or "") > 200_000,
                "stderr": (proc.stderr or "")[:2_000]}
    return await _aio.to_thread(_run)
@app.post("/api/code/intel")

async def code_intel(req: Request):
    """Code intelligence for the editor's agent panel.
    One endpoint over `code_intel`, because the panel needs all of it in a
    request that takes a few milliseconds and must not open a separate
    connection per question:
      document_symbols  the file outline
      find_references   every use, excluding docstrings and comments
      blast_radius
      what a change would touch, ranked
      preview_edit
      a diff, writing nothing
      apply_symbol_edit the separate, explicit write
    `preview_edit` is safe to call constantly; only `apply_symbol_edit`
    touches disk.
    """
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400,
                            content={"error": "body must be JSON"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    op = str(body.get("op") or "").strip()
    path = str(body.get("path") or ".").strip() or "."
    name = str(body.get("name") or "").strip()
    code = str(body.get("text") if body.get("text") is not None
               else body.get("code") or "")
    mode = str(body.get("mode") or "replace").strip()
    cwd = _code_abs(path if path != "." else "")
    if cwd is None:
        return JSONResponse(status_code=400, content={
            "error": f"path {path!r} is outside the served roots"})
    try:
        import code_intel as _ci
        # Editor queries: go-to-definition, hover, rename. Backed by the same AST
        # the AGENT's document_symbols/find_references use, so the editor and the
        # agent cannot disagree about where a symbol lives.
        if op == "definition":
            found = _ci.definition(
                name, str(cwd), line=int(body.get("line") or 0),
                col=int(body.get("col") or 0),
                root=_code_agent_root())
            # code_intel speaks tool paths (basename / workspace-relative);
            # the console's file routes need "<root-name>/dir/file". Without
            # this the jump resolved outside the roots and 403'd (H7).
            if found.get("found") and found.get("file"):
                if found.get("local") and path != ".":
                    found["file"] = path
                elif not found.get("local"):
                    found["file"] = _code_client_path(found["file"])
            return JSONResponse(found)
        if op == "hover":
            return JSONResponse(_ci.hover(name, str(cwd)))
        if op == "rename":
            if not name:
                return JSONResponse(status_code=400, content={
                    "error": "name is required"})
            new_name = str(body.get("new_name") or "").strip()
            if not new_name:
                return JSONResponse(_ci.rename_sites(name, str(cwd)))
            if body.get("confirm") is not True:
                return JSONResponse(status_code=409, content={
                    "error": "rename writes to disk; review the sites first, "
                             "then resend with confirm=true",
                    "symbol": name})
            # Renames are scoped to the tree the editor serves, not `path`.
            return JSONResponse(_ci.apply_rename(name, new_name, str(cwd)))
        if op == "symbols":
            return JSONResponse(_ci.document_symbols(str(cwd)))
        if op == "references":
            if not name:
                return JSONResponse(status_code=400,
                                    content={"error": "name is required"})
            r = _ci.find_references(name, str(cwd))
            r["exact"] = bool(r.get("exact"))
            return JSONResponse(r)
        if op == "blast":
            if not name:
                return JSONResponse(status_code=400,
                                    content={"error": "name is required"})
            return JSONResponse(_ci.blast_radius(name, str(cwd)))
        if op in ("preview", "apply"):
            if not name or not code:
                return JSONResponse(status_code=400, content={
                    "error": "name and text are required"})
            # `apply` writes to disk, and this section is read-only BY
            # CONSTRUCTION for everything else. Rather than quietly widening
            # that, the write is gated on an explicit confirmation: the caller
            # must have previewed and then said yes. An `ask` frame, a stray
            # replay or a tool call cannot turn into a file change.
            if op == "apply" and body.get("confirm") is not True:
                return JSONResponse(status_code=409, content={
                    "error": "apply requires confirm=true; preview the diff "
                             "first, then confirm",
                    "diff": ""})
            ch = _ci.preview(str(cwd), name, code, mode=mode)
            if not ch.before and not ch.after:
                return JSONResponse(status_code=404, content={
                    "error": ch.detail, "diff": ""})
            if op == "preview":
                return JSONResponse({
                    "ok": True, "path": ch.path, "mode": ch.kind,
                    "detail": ch.detail, "diff": _ci.diff_for(ch),
                    "applied": False,
                })
            out = _ci.apply_change(ch)
            out["detail"] = ch.detail
            out["applied"] = bool(out.get("written"))
            return JSONResponse(out)
    except Exception as exc:
        return JSONResponse(status_code=500, content={
            "error": f"{type(exc).__name__}: {exc}"})
    return JSONResponse(status_code=400, content={
        "error": f"unknown op {op!r}; expected symbols, references, blast, "
                 f"preview or apply"})


@app.post("/api/code/write")

async def code_write(req: Request):
    """Save a buffer TO DISK. The one human write route in this section.

    Everything else here is read-only by construction: files are served from
    `/api/code/*` with no write verb, so a mistyped question cannot rewrite the
    repo. That leaves the editor a drafting surface only, which is what stops it
    from being an IDE. This route closes that gap deliberately, with the same
    gate the agent's edits use - the caller MUST have asked for it - and an
    atomic write, so an interrupted save cannot leave a half-written file.

    `confirm` is required. `confirm` is not a formality: without it a stray
    replay or a tool call could turn a browser tab into a file change.
    """
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400,
                            content={"error": "body must be JSON"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={
            "error": "body must be a JSON object"})
    path = str(body.get("path") or "").strip()
    text = body.get("text")
    if not path:
        return JSONResponse(status_code=400, content={"error": "path is required"})
    if not isinstance(text, str):
        return JSONResponse(status_code=400, content={"error": "text must be a string"})
    if body.get("confirm") is not True:
        return JSONResponse(status_code=409, content={
            "error": "write to disk requires confirm=true; the caller must have "
                     "asked for the save explicitly"})
    target = _code_abs(path)
    if target is None or not target.is_file():
        return JSONResponse(status_code=400, content={
            "error": f"path {path!r} is outside the served roots, or is not "
                     f"an existing file"})
    if len(text.encode("utf-8", "replace")) > _CODE_MAX_BYTES:
        return JSONResponse(status_code=413, content={
            "error": f"buffer is over the {_CODE_MAX_BYTES} byte limit"})
    # A save must not be able to silently clobber a file that moved on since it
    # was read: the client sends back the mtime it saw and we refuse if it has
    # changed, so a stale tab loses a write instead of an hour of work.
    seen = body.get("mtime")
    if seen is not None:
        try:
            current = float(target.stat().st_mtime)
            wanted = float(seen)
        except (TypeError, ValueError, OSError):
            # Fail CLOSED. An mtime we cannot read is not a reason to allow a
            # write we cannot verify: a malformed value would otherwise silently
            # disable the whole guard.
            return JSONResponse(status_code=409, content={
                "error": "could not verify the file has not changed on disk; "
                         "reload the file before saving"})
        if abs(current - wanted) > 0.001:
            return JSONResponse(status_code=409, content={
                "error": "this file changed on disk since you read it; "
                         "reload it before saving"})
    backup = ""
    try:
        old = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return JSONResponse(status_code=500, content={
            "error": f"{type(exc).__name__}: {exc}"})
    if old == text:
        return JSONResponse({"ok": True, "path": path, "written": False,
                             "unchanged": True, "bytes": len(text)})
    try:
        import shutil
        import tempfile
        from datetime import datetime as _dt, timezone as _tz
        # Backups live under the agent's own state, never under whatever the
        # workspace currently is: this path used to be derived from _CODE_ROOT,
        # which is now the blank workspace, so a save would have tried to create
        # a backups folder inside the user's project instead.
        bdir = STATE_DIR / "code-backups"
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = _dt.now(_tz.utc).strftime("%Y%m%dT%H%M%S")
        bpath = bdir / f"{path.replace('/', '_')}.{stamp}"
        shutil.copyfile(str(target), str(bpath))
        backup = bpath.name
        # Atomic: write a sibling temp file and rename over the target. A crash
        # mid-write leaves the original intact rather than a truncated file.
        fd, tmp = tempfile.mkstemp(dir=str(target.parent),
                                   prefix=target.name + ".",
                                   suffix=".aria-tmp")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, str(target))
    except Exception as exc:
        return JSONResponse(status_code=500, content={
            "error": f"{type(exc).__name__}: {exc}"})
    return JSONResponse({"ok": True, "path": path, "written": True,
                         "backup": backup, "bytes": len(text)})


@app.post("/api/code/newfile")
async def code_newfile(req: Request):
    """Create ONE empty file, for the explorer's "New file" button.

    `/api/code/write` deliberately refuses to bring a file into existence (it
    only saves a buffer the user already had open), which left the console with
    no way to create anything: a fresh playground is empty, "Open folder" needs
    a folder that already exists, and the agent is the only other writer. So a
    user who had no project to point at could not author a single file.

    Rules, chosen so this can only ever ADD an empty file:
    - `confirm` must be true, like every other write route here;
    - the path must resolve inside the served workspace (same confinement the
      read routes use), and must be directly inside it, not nested arbitrarily;
    - it must not already exist - this never truncates or overwrites;
    - the name must look like a file, with a short extension.
    """
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "body must be JSON"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={
            "error": "body must be a JSON object"})
    name = str(body.get("name") or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "name is required"})
    if len(name) > 120 or "/" in name or "\\" in name or name in (".", ".."):
        return JSONResponse(status_code=400, content={
            "error": "name must be a plain file name (no folders, no '..')"})
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or len(ext) > 12 or not re.fullmatch(r"[A-Za-z0-9_+-]+", ext):
        return JSONResponse(status_code=400, content={
            "error": "give the file a name with an extension, e.g. notes.md"})
    if body.get("confirm") is not True:
        return JSONResponse(status_code=409, content={
            "error": "creating a file requires confirm=true; the caller must "
                     "have asked for it explicitly"})
    # `dir` is the folder to create it in, in the same "root/sub" form every
    # other code route uses (`_code_abs` resolves against the workspace base,
    # not against a root). Empty means the base itself, which is usually the
    # PARENT of the roots and so not writable - the caller should pass a root.
    sub = str(body.get("dir") or "").strip().strip("/")
    if sub and (".." in sub.split("/") or not _code_rel_ok(sub)):
        return JSONResponse(status_code=400, content={
            "error": "dir must be a plain folder path inside the workspace"})
    rel = f"{sub}/{name}" if sub else name
    target = _code_abs(rel)
    if target is None:
        return JSONResponse(status_code=403, content={
            "error": f"{rel!r} is outside the code workspace"})
    try:
        if target.exists():
            return JSONResponse(status_code=409, content={
                "error": f"{name} already exists - open it instead of creating it"})
        target.parent.mkdir(parents=True, exist_ok=True)
        # O_EXCL: if something created the file between the check and here, this
        # fails instead of truncating whatever won the race.
        fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
    except FileExistsError:
        return JSONResponse(status_code=409, content={
            "error": f"{name} already exists - open it instead of creating it"})
    except Exception as exc:
        return JSONResponse(status_code=500, content={
            "error": f"{type(exc).__name__}: {exc}"})
    return JSONResponse({"ok": True, "path": name, "created": True, "bytes": 0})


@app.post("/api/code/permission/{pid}")
async def code_permission(pid: str, req: Request):
    """Accept or refuse ONE proposed agent write.

    MANUAL mode's decision point (auto mode never parks - it snapshots,
    writes, and is reviewed afterwards via /api/code/agent/keep | revert).
    Each write tool call parks in /api/code/agent's tool loop, the run's SSE
    stream emits a `permission` frame carrying the exact diff, and the call
    only reaches the MCP child once the operator posts a decision here.
    Refusing is a real outcome - the tool returns "the user declined this
    change" to the model and the run continues - so the agent can adapt
    instead of stalling. A refusal also drops the write's snapshot, so no
    phantom review item is left behind.

    An unknown or already-resolved id is 404: a double-click on Accept (or a
    late click after the run moved on) must not read as success.
    """
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "body must be JSON"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    if "approve" not in body or not isinstance(body.get("approve"), bool):
        return JSONResponse(status_code=400,
                            content={"error": "approve must be true or false"})
    if not _plain_id_ok(pid):
        return JSONResponse(status_code=400, content={"error": "bad id"})
    if not _code_perm_resolve(pid, bool(body["approve"])):
        return JSONResponse(status_code=404,
                            content={"error": f"no pending change {pid!r} - it "
                                              f"was already decided, or the run "
                                              f"is gone"})
    return {"status": "ok", "id": pid, "approve": bool(body["approve"])}


@app.get("/api/code/agent/changes")
async def code_agent_changes():
    """The agent's writes that are still unreviewed.

    With auto-approve the user reviews after the fact, so the panel needs to
    know what is pending - including after a page reload, which would
    otherwise lose every mark. `created` distinguishes a file the agent
    brought into existence: reverting it removes the file.
    """
    return {"changes": [
        {"path": p, "tool": e.get("tool"), "summary": e.get("summary"),
         "diff": e.get("diff"), "when": e.get("when"),
         "created": not e.get("existed")}
        for p, e in list(_CODE_REVIEW.items())]}


@app.post("/api/code/agent/keep")
async def code_agent_keep(req: Request):
    """Accept the agent's change to one path: the file stays exactly as it is
    and the snapshot is dropped, so nothing is left to revert."""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "body must be JSON"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    path = _body_str(body, "path")
    if not path:
        return JSONResponse(status_code=400,
                            content={"error": "path is required"})
    ok = _CODE_REVIEW.pop(path, None) is not None
    return {"ok": ok, "path": path}


@app.post("/api/code/agent/revert")
async def code_agent_revert(req: Request):
    """Put one path back the way it was before the agent touched it.

    Restores the bytes snapshotted before the FIRST unreviewed write, or
    removes the file entirely when the agent created it. The snapshot is
    re-inserted if the restore fails, so a failed revert can be retried
    instead of silently losing the only copy of the original.
    """
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "body must be JSON"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    path = _body_str(body, "path")
    if not path:
        return JSONResponse(status_code=400,
                            content={"error": "path is required"})
    entry = _CODE_REVIEW.pop(path, None)
    if entry is None:
        return JSONResponse(status_code=404, content={
            "ok": False, "path": path,
            "error": "no unreviewed change for this path"})
    try:
        import asyncio as _aio
        ok, err = await _aio.to_thread(_code_review_restore, entry)
    except Exception as exc:  # a crash here must not eat the only snapshot
        ok, err = False, f"{type(exc).__name__}: {exc}"
    if not ok:
        _CODE_REVIEW[path] = entry
        return JSONResponse(status_code=500, content={
            "ok": False, "path": path, "error": err})
    return {"ok": True, "path": path,
            "restored": bool(entry.get("existed")),
            "deleted": not entry.get("existed")}


@app.post("/api/code/agent")

async def code_agent(req: Request):
    """The coding agent, with tools. This is what `/api/code/ask` should be.
    `/api/code/ask` is one LLM call: it pastes the buffer, a few attached
    files and 400 file NAMES into a prompt and asks for prose. Measured, the
    result is a model with no way to look at anything - asked to list the
    folder it spent ~2000 reasoning tokens guessing which folder it was in,
    then answered from the filename dump it had been handed.
    So: give it the tools instead of the guesswork. `list_dir`, `read_file`,
    `search_files` and friends already exist on the MCP server, and
    `mcp_runner.run_with_tools` already runs the loop and already emits
    tool_call / tool_result events. Nothing here is new machinery - it is the
    existing loop, pointed at the editor.

    It can edit. The write tools are in the toolset so the model can actually
    fix what it finds; every one of them is intercepted BEFORE it reaches the
    MCP child, and the file's bytes are snapshotted first. Two review modes:
    manual (default) parks each write and emits a `permission` frame with the
    exact diff, released by /api/code/permission/{id}; `auto_approve: true`
    (the console's opt-in) dispatches at once with a `write` frame carrying
    the same diff, and the user reviews afterwards - green/yellow/red marks,
    then Keep or Revert per file. Either way a question typed into the panel
    cannot rewrite a file without leaving a revertable trace.

    SSE frames: status, tool_call, tool_result, permission, write, thinking,
    delta, done, error.
    """
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400,
                            content={"error": "body must be JSON"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    question = str(body.get("question") or "").strip()
    if not question:
        return JSONResponse(status_code=400,
                            content={"error": "question is required"})
    if len(question) > 8000:
        return JSONResponse(status_code=413,
                            content={"error": "question is too long"})
    cursor = str(body.get("path") or "").strip()
    buffer_text = body.get("text")
    if not isinstance(buffer_text, str):
        buffer_text = ""
    if len(buffer_text.encode("utf-8", "replace")) > _CODE_MAX_BYTES:
        return JSONResponse(status_code=413, content={
            "error": f"buffer is over the {_CODE_MAX_BYTES} byte limit"})
    attached = [str(p) for p in (body.get("paths") or [])
                if isinstance(p, str)][:6]
    wanted_model = str(body.get("model") or "").strip()
    if wanted_model:
        if len(wanted_model) > 160 or not _MODEL_ID_RE.match(wanted_model):
            return JSONResponse(status_code=400, content={
                "error": "model must look like vendor/name"})
    # A bound on the loop. Small models loop rather than progress, and the
    # failure is silent (a full budget burned, nothing found), so the cap has
    # to be low enough to be cheap and high enough for a real investigation.
    raw_hops = body.get("max_hops")
    try:
        max_hops = (int(raw_hops) if raw_hops is not None
                    else _CODE_AGENT_MAX_HOPS)
    except Exception:
        max_hops = _CODE_AGENT_MAX_HOPS
    # Explicit 0 (or negative) keeps the old "no ceiling" escape hatch. A
    # positive value is clamped: no caller needs more than 200 model calls.
    max_hops = max(0, min(max_hops, _CODE_AGENT_MAX_HOPS_CAP))
    import asyncio as _aio
    async def gen():
        yield _sse("status", text=(
            f"starting (up to {max_hops} steps)" if max_hops > 0
            else "starting"))
        try:
            from skills import tool_payload as _tp
            tools_payload = _tp(CODE_AGENT_TOOLS) or []
        except Exception as e:
            yield _sse("error", text=f"tools unavailable: {e}")
            return
        if not tools_payload:
            yield _sse("error", text="no coding tools resolved")
            return
        # Context: the open file and whatever the user attached. Deliberately
        # NOT a file listing - the whole point is that the model calls
        # list_dir instead of reasoning about a filename dump.
        head = []
        if cursor:
            # Tool-relative, because that is the only form the model can use
            # with read_file - handing it the client's "<root>/..." form made
            # every self-directed read of the open file fail.
            head.append(f"ACTIVE FILE: {_code_agent_rel(cursor) or cursor} "
                        "(unsaved edits included)\n"
                        + buffer_text[:_CODE_AGENT_BUFFER_CHARS])
        for rel in attached:
            p = _code_agent_abs(rel)
            if p is None or not p.is_file():
                continue
            try:
                if p.stat().st_size > _CODE_MAX_BYTES:
                    continue
                head.append(f"ATTACHED: {_code_agent_rel(rel)}\n"
                            + p.read_text(encoding="utf-8",
                                           errors="replace")[:_CODE_AGENT_BUFFER_CHARS])
            except Exception:
                continue
        # The workspace description must follow the workspace. It used to be a
        # fixed string naming S9SharedCode/code and llm_gatewayV9, so in a
        # playground - where neither exists - the model obeyed, hunted for
        # those two directories, found nothing, and looped until the loop
        # guards killed the run. The SHIPPED default (an empty scratch folder)
        # fell into the same trap, so describe only what is true everywhere:
        # the folder's name, and that list_dir is how the rest is learned.
        _ws_base, _ws_allowed = _code_workspace()
        _ws_root = _code_agent_root()
        _ws_is_default = _ws_base.resolve() == _CODE_DEFAULT_BASE.resolve()
        _ws_name = _ws_root.name or str(_ws_root)
        _layout = ("All tool paths are relative to your workspace folder "
                   f'("{_ws_name}"). Call list_dir(".") FIRST to see what is '
                   "actually here, then work only inside this folder.")
        if _ws_is_default:
            _layout += (" This is the shipped scratch workspace: expect it to "
                        "be empty. Create the files the user asks for with "
                        "create_file and folders with make_dir; nothing needs "
                        "to pre-exist.")
        _search_example = 'search_files("some_name", ".")'
        system = (
            "You are a coding agent working inside this project's own editor.\n"
            "You have tools. Use them instead of guessing.\n"
            "- You DO have tools that read the repository (list_dir, read_file, "
            "search_files, document_symbols, ...). You CAN read files. Never "
            "tell the user you have no file access - call the tool instead.\n\n"
            "- To find something, call a tool. Never infer a file's contents "
            "from its name, and never say a file does not exist before "
            "list_dir or search_files has told you so.\n"
            f"- {_layout}\n"
            "- search_files(pattern, path): the FIRST argument is the text to "
            f"look for, the second is the folder to search. {_search_example}. "
            "Use `include=\"*.py\"` to scope it. "
            "A path that does not exist just finds nothing.\n"
            "- Orient ONCE. Call list_dir(\".\") a single time at the start; "
            "the answer you need is almost always already in that listing. "
            "Never list the same directory twice - if a listing did not "
            "contain what you want, search instead of re-listing.\n"
            "- If a search returns nothing, widen the path or try the other "
            "top-level directory before saying something does not exist.\n"
            "- Call several independent tools in ONE message when they do not "
            "depend on each other - document_symbols and find_references on the "
            "same file, for example. They run in parallel. Never call a tool "
            "just to wait.\n"
            "- read_file returns the file (whole when small); quote it "
            "accurately and do not re-read the same lines again. A large file "
            "is paginated: pass offset (1-based start line) and/or limit, and "
            "use next_offset to read on.\n"
            "- Once you have read what you need, answer from it; do not search "
            "for lines you already have.\n"
            "- Stop calling tools once you can answer. Your final message must "
            "be the answer itself, never another tool call.\n"
            "- If a check fails, read the failure and fix the cause.\n"
            "- `run_checks` runs the project's own tests/typecheck. When a "
            "specific command is needed (one test, a build target, a git "
            "query), use `run_command`: it runs PowerShell in the workspace, "
            "is approval-gated, and returns the real exit code and output. "
            "Keep `cwd` inside the workspace; do not use the shell to edit "
            "files - use the file tools so changes are reviewable.\n"
            "- If you cannot find something, say what you searched.\n"
            "- You CAN edit files (edit_file, create_file, make_dir, "
            "update_file, delete_file, apply_symbol_edit). A write lands in "
            "the workspace immediately; the user reviews it afterwards and "
            "can keep or revert each change. So make each change deliberate "
            "and single-purpose, and say in one line what you changed. Do "
            "not rewrite whole files to make one edit.\n"
            "- When you are done, answer. Keep it short and specific: file "
            "paths, function names, line numbers.\n"
        )
        if head:
            system += "\nWHAT YOU ALREADY HAVE:\n" + "\n\n".join(head)
        prior = body.get("history")
        messages: list[dict] = [{"role": "system", "content": system}]
        if isinstance(prior, list) and prior:
            for m in prior[-(_CODE_ASK_HISTORY_TURNS * 2):]:
                if not isinstance(m, dict):
                    continue
                role = "user" if m.get("role") == "user" else "assistant"
                text = str(m.get("content") or m.get("text") or "").strip()
                if text:
                    messages.append({
                        "role": role,
                        "content": text[:_CODE_ASK_HISTORY_CHARS]})
        messages.append({"role": "user", "content": question})
        q: _aio.Queue = _aio.Queue()

        def _on_event(kind, payload):
            # run_tool_loop calls on_event(kind, payload) - TWO arguments - and
            # swallows any exception so progress reporting can never break the
            # loop. A one-argument callback therefore raised TypeError on every
            # single event and the stream showed nothing at all, silently. Fold
            # the two into the single dict _emit expects.
            # Called synchronously from inside the tool loop, which runs on
            # this same event loop, so a plain put_nowait is safe.
            try:
                ev = dict(payload) if isinstance(payload, dict) else {
                    "value": payload}
                ev["type"] = str(kind or "")
                q.put_nowait(ev)
            except Exception:
                pass
        # The write path for every write the model attempts. Two modes, one
        # invariant: the file is SNAPSHOTTED before anything can touch it, so
        # there is always a reviewed-or-revertable state.
        #  - manual (default): the call parks on an asyncio.Event that only
        #    /api/code/permission releases; a refused call never reaches the
        #    MCP child and cannot have a side effect.
        #  - auto_approve (the console's opt-in): the write is dispatched at
        #    once and a `write` frame carrying the diff goes out, so the panel
        #    can paint the change (green/yellow/red) and the user reviews
        #    afterwards through Keep / Revert.
        import uuid as _uuid
        # Snapshots taken by approved writes, keyed by (tool, client path) in
        # approval order, settled by _on_outcome once dispatch has run. A
        # failed or no-op write must not leave a phantom review entry.
        pending_reviews: dict[tuple[str, str], list[int]] = {}

        def _settle(name: str, cli: str, token: int | None) -> None:
            if token is None:
                return
            try:
                _code_review_settle(cli, token)
            except Exception:
                pass

        async def _approve(name: str, args: dict) -> bool:
            a = args if isinstance(args, dict) else {}
            auto = body.get("auto_approve") is True
            if name in _CODE_EXEC_TOOLS:
                # Running the project's checks executes repository code, so it
                # asks exactly like a write. Auto mode is the console's opt-in;
                # a manual run parks until the operator accepts.
                if auto:
                    return True
                cli = _code_client_path(str(a.get("path") or ""))
                pid = f"pe-{_uuid.uuid4().hex[:10]}"
                entry = _code_perm_put(pid)
                if name == "run_command":
                    summary = "run " + str(a.get("command") or "").strip()[:300]
                else:
                    summary = (f"run project checks in "
                               f"{cli or a.get('path') or '.'}")
                q.put_nowait({"type": "permission", "id": pid, "tool": name,
                              "args": a, "path": "",
                              "summary": summary, "diff": ""})
                try:
                    await _aio.wait_for(entry["event"].wait(),
                                        _CODE_PERM_WAIT_S)
                    return entry.get("decision") is True
                except (_aio.TimeoutError, Exception):
                    return False
                finally:
                    _code_perm_drop(pid)
            if name not in _CODE_WRITE_TOOLS:
                return True
            cli = _code_client_path(str(a.get("path") or ""))
            summary, diff = await _aio.to_thread(_code_write_diff, name, args)
            token = await _aio.to_thread(_code_review_take, cli, name,
                                         summary, diff)
            if auto:
                # Explicit opt-in only. The frame is informational - the panel
                # derives the marks from `diff` and expects no decision back.
                q.put_nowait({"type": "write",
                              "id": f"wr-{_uuid.uuid4().hex[:10]}",
                              "tool": name, "args": a, "path": cli,
                              "summary": summary, "diff": diff, "auto": True})
                if token is not None:
                    pending_reviews.setdefault((name, cli), []).append(token)
                return True
            pid = f"pe-{_uuid.uuid4().hex[:10]}"
            entry = _code_perm_put(pid)
            q.put_nowait({"type": "permission", "id": pid, "tool": name,
                          "args": a, "path": cli,
                          "summary": summary, "diff": diff})
            got = False
            try:
                await _aio.wait_for(entry["event"].wait(), _CODE_PERM_WAIT_S)
                got = entry.get("decision") is True
            except (_aio.TimeoutError, Exception):
                # Timed out. Fail closed: no event, no write.
                got = False
            except BaseException:
                # The run was cancelled while parked (Stop / client
                # disconnect). Settle before propagating so a write that never
                # happened cannot leave a review entry behind.
                _settle(name, cli, token)
                raise
            finally:
                _code_perm_drop(pid)
            if not got:
                # No write happened. Drop the snapshot only if the file still
                # matches it - an earlier unreviewed change survives.
                _settle(name, cli, token)
                return False
            if token is not None:
                pending_reviews.setdefault((name, cli), []).append(token)
            return True

        def _on_outcome(name, args, ok, result_text, latency):
            # Fired once per dispatched call, AFTER the write has landed (or
            # failed). Settling here is what makes the review list truthful.
            if name not in _CODE_WRITE_TOOLS:
                return
            cli = _code_client_path(str(
                (args if isinstance(args, dict) else {}).get("path") or ""))
            tokens = pending_reviews.get((name, cli))
            if not tokens:
                return
            token = tokens.pop(0)
            if not tokens:
                pending_reviews.pop((name, cli), None)
            _settle(name, cli, token)

        async def _drive():
            from mcp_runner import run_with_tools
            return await run_with_tools(
                messages=messages,
                tools_payload=tools_payload,
                agent="code",
                session_id=f"code-{cursor or 'scratch'}",
                provider_pin=None,
                model=wanted_model or _CODE_AGENT_DEFAULT_MODEL,
                max_hops=max_hops,
                max_tokens=int(body.get("max_tokens") or 4000),
                temperature=0.2,
                sandbox_root=str(_code_agent_root()),
                stream=True,
                on_event=_on_event,
                on_outcome=_on_outcome,
                approve_fn=_approve,
                allowed_tools=set(CODE_AGENT_TOOLS),
                max_context_chars=_CODE_AGENT_CONTEXT_CHARS,
            )
        task = _aio.ensure_future(_drive())

        # Set once any answer token has gone out live, so the tail of this
        # generator does not re-send the whole answer as fake deltas.
        streamed = {"any": False}
        # `done.tool_calls` used to read a field the loop never sets, so the
        # panel always reported 0 calls even when the trace showed several.
        # Count the results this stream actually forwarded.
        tool_count = {"n": 0}
        # Same guard for the reasoning channel. The live `thinking` deltas have
        # almost always gone out by the time the run ends, and pushing the whole
        # reasoning again as one frame stacked a second copy on top of them.
        streamed_thinking = {"any": False}

        def _emit(ev) -> dict | None:
            kind = str(ev.get("type") or "")
            if kind == "permission":
                # A write awaiting the operator. Forwarded verbatim so the panel
                # can render the diff and post the decision; the run stays
                # parked inside the gate until it does. `path` is the
                # CLIENT-visible path (root name baked in), because the console
                # opens this file after approval and its routes live on that
                # side of the naming.
                return _sse("permission", id=str(ev.get("id") or ""),
                            tool=str(ev.get("tool") or ""),
                            args=ev.get("args") or {},
                            path=str(ev.get("path") or ""),
                            summary=str(ev.get("summary") or ""),
                            diff=str(ev.get("diff") or ""))
            if kind == "write":
                # An auto-approved write, dispatched NOW. Same payload as a
                # permission frame; the panel derives the marks from `diff`
                # and owes no answer - review happens after the run via
                # /api/code/agent/keep | revert.
                return _sse("write", id=str(ev.get("id") or ""),
                            tool=str(ev.get("tool") or ""),
                            path=str(ev.get("path") or ""),
                            summary=str(ev.get("summary") or ""),
                            diff=str(ev.get("diff") or ""),
                            auto=bool(ev.get("auto")))
            if kind == "tool_call":
                return _sse("tool_call", name=str(ev.get("name") or ""),
                            detail=_tool_detail(ev.get("arguments")),
                            id=str(ev.get("id") or ""))
            if kind == "tool_result":
                tool_count["n"] += 1
                return _sse("tool_result", name=str(ev.get("name") or ""),
                            ok=not ev.get("error"),
                            id=str(ev.get("id") or ""),
                            detail=str(ev.get("result") or ev.get("error") or "")[:1500])
            if kind == "thinking":
                # The model's own reasoning stream, forwarded on its own channel
                # (the panel shows it collapsed). Never mixed into the answer.
                chunk = str(ev.get("delta") or "")
                if chunk:
                    streamed_thinking["any"] = True
                    return _sse("thinking", text=chunk)
                return None
            if kind == "text":
                # Live answer token(s). Marks the answer as already streamed so
                # the tail of this generator does not emit it a second time.
                chunk = str(ev.get("delta") or "")
                if chunk:
                    streamed["any"] = True
                    return _sse("delta", text=chunk)
                return None
            return None
        # Stream events as they arrive. The task finishing is itself an event,
        # so wait on both and never let a finished-but-unextracted task turn
        # into an empty answer - an exception here used to be swallowed and
        # surfaced as a successful reply with no text.
        #
        # The `finally` is the Stop button. The console aborts the fetch, the
        # streaming response is cancelled, and this generator unwinds - but the
        # tool loop runs in a DETACHED task, so without cancelling it here the
        # MCP child and the model call kept running after the user stopped.
        reply: dict = {}
        get_ev = None
        try:
            while True:
                get_ev = _aio.ensure_future(q.get())
                done, _ = await _aio.wait({get_ev, task},
                                          return_when=_aio.FIRST_COMPLETED)
                if get_ev in done:
                    ev = get_ev.result()
                    frame = _emit(ev) if isinstance(ev, dict) else None
                    if frame:
                        yield frame
                    continue
                get_ev.cancel()
                exc = task.exception()
                if exc is not None:
                    yield _sse("error", text=_error_detail(exc))
                    return
                reply = task.result() or {}
                # Streaming responses carry no input-token count, so without
                # this the context meter only ever showed output tokens (and 0
                # input), which is why it looked frozen. Estimate the last
                # prompt size from the messages the loop actually sent - that
                # is what fills the window.
                if not reply.get("input_tokens"):
                    try:
                        reply["input_tokens"] = max(
                            0, sum(len(str(m.get("content") or ""))
                                   for m in messages)) // 4
                    except Exception:
                        pass
                # Anything the loop queued just before returning.
                while not q.empty():
                    ev = q.get_nowait()
                    frame = _emit(ev) if isinstance(ev, dict) else None
                    if frame:
                        yield frame
                break
            answer = str(reply.get("text") or "")
            thinking = str(reply.get("reasoning") or "")
            # If the reasoning channel is just the answer again, drop it here
            # rather than in the panel: the live `thinking` frames have already
            # streamed, so the done frame has to tell the client to withdraw
            # them.
            thinking_dropped = False
            if _thinking_is_answer(answer, thinking):
                thinking = ""
                thinking_dropped = True
            # The loop can end with NO final text but a real reason - a hop
            # cap, a context budget, a verifier stall. This used to be an
            # `error` frame, which the panel turns into a thrown exception that
            # discards the trace - exactly wrong for a run that simply stopped.
            # Report it on `done` instead: the answer carries the reason and
            # the run is not a transport failure.
            stopped = str(reply.get("error") or reply.get("note") or "").strip()
            # Only when the reasoning did NOT already stream live, for the same
            # reason the answer tail is guarded below.
            if thinking.strip() and not streamed_thinking["any"]:
                yield _sse("thinking",
                           text=thinking[:_CODE_AGENT_THINKING_CHARS])
            if stopped and not answer.strip():
                answer = f"[stopped: {stopped}]"
            # Only replay the answer in chunks when it did NOT already stream
            # live; otherwise the panel would receive the whole text twice.
            if answer.strip() and not streamed["any"]:
                for i in range(0, len(answer), 240):
                    yield _sse("delta", text=answer[i:i + 240])
            yield _sse("done", answer=answer, path=cursor,
                       provider=reply.get("provider", ""),
                       model=reply.get("model", "") or wanted_model,
                       thinking=thinking[:_CODE_AGENT_THINKING_CHARS],
                       thinking_dropped=thinking_dropped,
                       stopped=stopped[:400],
                       input_tokens=int(reply.get("input_tokens") or 0),
                       output_tokens=int(reply.get("output_tokens") or 0),
                       tool_calls=tool_count["n"],
                       context_files=[f for f in (cursor, *attached) if f])
        finally:
            if get_ev is not None and not get_ev.done():
                get_ev.cancel()
            if not task.done():
                task.cancel()
            # A cancelled write may have taken a snapshot and then never
            # reported an outcome (the task was stopped mid-dispatch).
            # Settle the rest by comparing disk with the snapshot; a real
            # change survives, a phantom one does not.
            for (_n, _p), _toks in list(pending_reviews.items()):
                for _t in _toks:
                    _settle(_n, _p, _t)
            pending_reviews.clear()
    return StreamingResponse(gen(), **_SSE_HEADERS)
@app.post("/api/code/ask")
async def code_ask(req: Request):
    """Ask the agent about the code in the editor, with the real project around it.
    The Code view had no way to talk to the agent: it could read, search and
    edit files, but every question about the code had to leave the page. This
    makes the editor an AI editor by giving one question a real context - the
    active buffer (unsaved edits included), any explicitly attached files, and
    a bounded slice of the surrounding project so the answer is grounded rather
    than from memory.
    Read-only by design. It answers, it does not edit: a question typed into a
    panel should not be able to rewrite files behind the user's back. The agent
    can still find definitions (`find_symbol`) and read them, but it writes
    nothing. Applying a change is the user's next step, in the editor, where
    they can see it.
    Returns SSE frames so a long answer streams: `status`, `delta`, `done`.
    """
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400,
                            content={"error": "body must be JSON"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    question = str(body.get("question") or "").strip()
    if not question:
        return JSONResponse(status_code=400,
                            content={"error": "question is required"})
    if len(question) > 8000:
        return JSONResponse(status_code=413,
                            content={"error": "question is too long"})
    import asyncio as _aio
    cursor = str(body.get("path") or "").strip()
    buffer_text = body.get("text")
    if not isinstance(buffer_text, str):
        buffer_text = ""
    if len(buffer_text.encode("utf-8", "replace")) > _CODE_MAX_BYTES:
        return JSONResponse(status_code=413, content={
            "error": f"buffer is over the {_CODE_MAX_BYTES} byte limit"})
    attached = [str(p) for p in (body.get("paths") or [])
                if isinstance(p, str)][:6]
    # The model the user picked in the Code panel, if any. Validated to the
    # `vendor/name` shape Kilo (and OpenRouter) use: an arbitrary string
    # reaching the gateway is an SSRF-shaped footgun, since the gateway
    # resolves some model names into upstream URLs.
    wanted_model = str(body.get("model") or "").strip()
    if wanted_model:
        if len(wanted_model) > 160 or not _MODEL_ID_RE.match(wanted_model):
            return JSONResponse(status_code=400, content={
                "error": "model must look like vendor/name"})
    def _gather():
        """Read the context on a thread: every one of these is blocking I/O."""
        files: list[dict] = []
        if cursor and _code_abs(cursor) is not None:
            try:
                files.append({"path": cursor, "role": "active file"})
            except Exception:
                pass
        for rel in attached:
            p = _code_abs(rel)
            if p is None or not p.is_file() or p.stat().st_size > _CODE_MAX_BYTES:
                continue
            try:
                files.append({"path": rel,
                              "content": p.read_text(encoding="utf-8",
                                                     errors="replace")})
            except Exception:
                continue
        # A bounded slice of the project so answers are grounded in the real
        # tree rather than in memory. File NAMES only - the contents are
        # reachable one explicit request at a time.
        listing: list[str] = []
        try:
            for rel in _code_files_all().get("files", [])[:400]:
                listing.append(rel)
        except Exception:
            pass
        return files, listing
    files, listing = await _aio.to_thread(_gather)
    async def gen():
        yield _sse("status", text="reading the project…")
        try:
            import gateway as _gw
            from gateway import LLM
        except Exception as e:
            yield _sse("error", text=f"gateway unavailable: {e}")
            return
        prompt_parts = [
            "You are answering a question about a real codebase, inside that "
            "codebase's own editor. Be specific and concrete: name files, "
            "functions and line numbers. If the answer is not in the context "
            "below, say so plainly instead of guessing.",
        ]
        # Conversation history, so this is a session rather than a series of
        # unrelated questions. "What about the caller of that?" only means
        # something if the previous turn is still present. Bounded from the
        # FRONT so the oldest turns drop and the recent ones survive - the
        # opposite end loses the thread the question is referring to.
        prior = body.get("history")
        if isinstance(prior, list) and prior:
            turns = []
            for m in prior[-(_CODE_ASK_HISTORY_TURNS * 2):]:
                if not isinstance(m, dict):
                    continue
                role = "user" if m.get("role") == "user" else "assistant"
                text = str(m.get("content") or m.get("text") or "").strip()
                if text:
                    turns.append(f"{role.upper()}: {text[:_CODE_ASK_HISTORY_CHARS]}")
            if turns:
                prompt_parts.append(
                    "\nEARLIER IN THIS CONVERSATION:\n"
                    + "\n".join(turns)
                    + "\n(answer the newest question; the earlier turns are "
                      "context, not the question)")
        prompt_parts.append(f"\nQUESTION: {question}\n")
        if cursor:
            head = (f"\nACTIVE FILE: {cursor} (unsaved edits included)\n"
                    f"{buffer_text[:_CODE_MAX_BYTES]}")
            prompt_parts.append(head)
        for f in files:
            if "content" in f:
                prompt_parts.append(
                    f"\nATTACHED FILE: {f['path']}\n{f['content'][:120_000]}")
        if listing:
            prompt_parts.append(
                "\nPROJECT FILES (names only - ask for any of these to be "
                f"read if you need it):\n" + "\n".join(listing[:300]))
        try:
            # `LLM.chat` is BLOCKING and returns a dict - so it goes through
            # to_thread. Awaiting it fails with "object dict can't be used in
            # 'await' expression"; calling it inline blocks the event loop.
            #
            # `model` lets the Code panel's picker choose the backend. The
            # gateway resolves a bare model id to a provider by itself
            # (MODEL_ROUTES), so the panel sends only the id and never has to
            # know which provider serves it. Unset -> None -> stripped from
            # the payload, which is exactly today's behaviour.
            reply = await _aio.to_thread(
                LLM().chat,
                prompt="".join(prompt_parts),
                agent="code", session=f"code-{cursor or 'scratch'}",
                model=wanted_model or None,
                max_tokens=8000, temperature=0.2)
        except Exception as e:
            yield _sse("error", text=f"{type(e).__name__}: {e}")
            return
        if not isinstance(reply, dict):
            yield _sse("error",
                       text=f"gateway returned {type(reply).__name__}, "
                            f"not a reply object")
            return
        if reply.get("error") and not (reply.get("text") or "").strip():
            yield _sse("error", text=str(reply.get("error"))[:400])
            return
        # Reasoning is a separate channel. Streaming it as `delta` is what made
        # the panel show the model's monologue as the answer: the free Kilo
        # models emit hundreds of chars of "let me read the files" into
        # `reasoning` with an empty `content`, and the UI had no way to tell
        # the two apart. Now the panel can collapse it and show the answer.
        thinking = str(reply.get("reasoning") or "")
        if thinking.strip():
            for i in range(0, len(thinking), 400):
                yield _sse("thinking", text=thinking[i:i + 400])
                await _aio.sleep(0)
        answer = str(reply.get("text") or "")
        for i in range(0, len(answer), 240):
            yield _sse("delta", text=answer[i:i + 240])
            await _aio.sleep(0)
        yield _sse("done", answer=answer, path=cursor,
                    provider=reply.get("provider", ""),
                    model=reply.get("model", "") or wanted_model,
                    thinking=thinking,
                    # Context fill for the status bar. The gateway reports what
                    # it actually sent and received; the model's window comes
                    # from the roster so the bar means something.
                    input_tokens=int(reply.get("input_tokens") or 0),
                    output_tokens=int(reply.get("output_tokens") or 0),
                    context_files=[f["path"] for f in files])
    return StreamingResponse(gen(), **_SSE_HEADERS)
@app.post("/api/code/checks")
async def code_checks(req: Request):
    """Run the project's real lint / typecheck / tests for the editor.
    `/api/code/check` judges the BUFFER with `ast.parse` - fast, and honest
    about being a parser. This runs the project itself: pytest / tsc / npm
    test / ruff / cargo / go vet, whichever the project actually has. It is
    slow by nature (a test suite takes what it takes) and it is what makes the
    panel a real feedback loop rather than a spell-checker.
    The path is confined to the Code roots exactly like every other code
    endpoint, and the working directory is fixed to a root rather than taken
    from the request.
    """
    import asyncio as _aio
    import shutil
    import subprocess
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400,
                            content={"error": "body must be JSON"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    rel = str(body.get("path") or "").strip()
    cwd = _code_abs(rel) if rel else None
    if cwd is None:
        # Default to the first workspace root that exists.
        _base, _allowed = _code_workspace()
        for root in _allowed:
            cand = root.resolve()
            if cand.is_dir():
                cwd = cand
                break
    if cwd is None or not cwd.is_dir():
        return JSONResponse(status_code=400, content={
            "error": "path must be a directory inside the Code roots"})
    def _detect(d: Path):
        # Imported rather than duplicated: the editor and the agent's
        # run_checks tool must agree on what counts as "this project has a
        # checker", or the panel reports green while the agent reports fail.
        try:
            from mcp_server import _CHECK_PROFILES
        except Exception:
            return []
        out = []
        for marker, tool, args in _CHECK_PROFILES:
            if not (d / marker).exists():
                continue
            exe = shutil.which(tool)
            if not exe:
                out.append({"marker": marker, "tool": tool, "available": False})
                continue
            out.append({"marker": marker, "tool": tool, "available": True,
                        "argv": [exe, *args]})
        return out
    def _run_checks():
        found = _detect(cwd)
        results = []
        all_ok = True
        for f in found:
            if not f.get("available"):
                all_ok = False
                results.append({
                    "tool": f["tool"], "available": False, "ok": False,
                    "error": f"{f['tool']} is not installed; the project has "
                             f"{f['marker']} but the runner is missing"})
                continue
            try:
                proc = subprocess.run(f["argv"], cwd=str(cwd),
                                      capture_output=True, text=True,
                                      timeout=240)
            except subprocess.TimeoutExpired:
                all_ok = False
                results.append({"tool": f["tool"], "ok": False,
                                "exit_code": None,
                                "error": "timed out after 240s"})
                continue
            except Exception as e:
                all_ok = False
                results.append({"tool": f["tool"], "ok": False,
                                "exit_code": None,
                                "error": f"{type(e).__name__}: {e}"})
                continue
            ok = proc.returncode == 0
            all_ok = all_ok and ok
            results.append({
                "tool": f["tool"], "ok": ok, "exit_code": proc.returncode,
                "stdout": (proc.stdout or "")[-24_000:],
                "stderr": (proc.stderr or "")[-24_000:]})
        return all_ok, results, found
    ran, results, found = await _aio.to_thread(_run_checks)
    if not found:
        return {"ok": False, "checks_run": 0, "results": [],
                "cwd": str(cwd),
                "error": "no checker detected: this project has none of "
                         "pyproject.toml, pytest.ini, setup.cfg, tox.ini, "
                         "package.json, tsconfig.json, ruff.toml, "
                         "Cargo.toml or go.mod"}
    return {"ok": ran, "checks_run": len(results), "results": results,
            "cwd": str(cwd),
            "note": ("Every check exited 0." if ran else
                     "At least one check FAILED.")}
@app.get("/api/code/search")
async def code_search(q: str = "", limit: int = 200):
    """Bounded literal search across the servable workspace.
    Substring, case-insensitive, no regex: the caller is a browser text box,
    and handing it regex means a way to make the server do work proportional to
    a pathological pattern. Same path confinement and extension allow-list as
    the file endpoint, and a hard cap on files scanned, bytes read and matches
    returned so one search cannot stall the console.
    """
    import asyncio
    return await asyncio.to_thread(_code_search_sync, q, limit)
def _code_search_sync(q: str = "", limit: int = 200):
    needle = (q or "").strip().lower()
    if not needle:
        return {"matches": [], "count": 0, "truncated": False}
    if len(needle) > 200:
        return JSONResponse(status_code=400, content={"error": "query too long"})
    cap = max(1, min(int(limit or 200), 500))
    listing = _code_listing_all()
    matches: list[dict] = []
    truncated = False
    for rel in listing:
        if len(matches) >= cap:
            truncated = True
            break
        target = _code_abs(rel)
        if target is None:
            continue
        try:
            if target.stat().st_size > _CODE_MAX_BYTES:
                continue
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for ln, line in enumerate(content.split("\n"), start=1):
            low = line.lower()
            start = 0
            while True:
                at = low.find(needle, start)
                if at == -1:
                    break
                if len(matches) >= cap:
                    truncated = True
                    break
                matches.append({
                    "path": rel, "line": ln, "col": at + 1,
                    "text": line.strip()[:240],
                })
                start = at + max(1, len(needle))
    return {"matches": matches, "count": len(matches), "truncated": truncated,
            "query": q}
def _code_listing_all(limit: int = _CODE_MAX_FILES) -> list[str]:
    base, allowed = _code_workspace()
    files: list[str] = []
    queue: list[Path] = list(allowed)
    while queue and len(files) < limit:
        d = queue.pop()
        try:
            children = list(d.iterdir())
        except OSError:
            continue
        for child in children:
            if len(files) >= limit:
                break
            if child.name in _CODE_SKIP_DIRS or not _code_name_visible(child.name):
                continue
            if child.is_dir():
                queue.append(child)
                continue
            if child.name in _CODE_SKIP_FILES:
                continue
            if child.suffix not in _CODE_EXTS and not child.name.endswith(".env.example"):
                continue
            try:
                files.append(child.resolve().relative_to(base.resolve()).as_posix())
            except (OSError, ValueError):
                continue
    files.sort()
    return files
def _code_language(name: str) -> str:
    suffix = PurePosixPath(name).suffix.lower()
    if name.endswith(".env.example"):
        return "ini"
    if name == ".gitignore":
        return "text"
    return _CODE_LANGS.get(suffix, "text")
@app.post("/api/memory/remember")
async def remember_memory(req: Request):
    """Dashboard write bridge: explicit-kind remember straight to the
    gateway (no LLM classifier — the UI already chose the kind)."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        # `(body.get(...) or "").strip()` on a list/int raised AttributeError
        # and surfaced as a bare 500 with no detail.
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    raw_kind, raw_desc = body.get("kind"), body.get("descriptor")
    if raw_kind is not None and not isinstance(raw_kind, str):
        return JSONResponse(status_code=400,
                            content={"error": "kind must be a string"})
    if raw_desc is not None and not isinstance(raw_desc, str):
        return JSONResponse(status_code=400,
                            content={"error": "descriptor must be a string"})
    kind = (raw_kind or "fact").strip()
    descriptor = (raw_desc or "").strip()
    if kind not in ("fact", "preference", "tool_outcome", "scratchpad",
                    "observation") or not descriptor:
        return JSONResponse(status_code=400, content={"error": "kind + descriptor required"})
    value = body.get("value")
    if value is not None and not isinstance(value, dict):
        return JSONResponse(status_code=400,
                            content={"error": "value must be an object"})
    keywords = body.get("keywords")
    if keywords is not None and not isinstance(keywords, list):
        return JSONResponse(status_code=400,
                            content={"error": "keywords must be an array"})
    # Reject a pathologically nested `value` here rather than letting it reach
    # the store, where pydantic's serialiser raised "Circular reference
    # detected (depth exceeded)" and a single such record wedged every later
    # read and write in that drawer.
    depth = _json_depth(value, limit=_MAX_VALUE_DEPTH)
    if depth > _MAX_VALUE_DEPTH:
        return JSONResponse(status_code=400, content={
            "error": f"value nests {depth} levels deep; the limit is "
                     f"{_MAX_VALUE_DEPTH}"})
    if not _json_finite(value):
        # Python's json module emits NaN/Infinity even though JSON
        # has neither; the gateway's serialiser then rejects the
        # whole payload and the write surfaces as a 502.
        return JSONResponse(status_code=400, content={
            "error": "value must not contain NaN or Infinity"})
    try:
        import time as _time
        # The gateway embeds the record (its embedder regularly
        # takes 25-30s), which raced the 30s default timeout and
        # answered 502 for a write that HAD landed — and a client
        # retry then created a duplicate. Give the embed the room
        # it needs instead of timing out mid-write.
        d = await _gw_json("/v1/memory/remember", method="POST", json={
            "kind": kind, "descriptor": descriptor,
            "keywords": body.get("keywords") or [],
            "value": body.get("value") or {},
            "source": body.get("source") or "dashboard",
            "run_id": body.get("run_id") or f"dashboard-{int(_time.time())}",
            "session_id": body.get("session_id") or None},
            timeout=120.0)
        if isinstance(d, dict) and d.get("error"):
            # A gateway 4xx (unserialisable payload, unknown kind) is the
            # caller's problem and keeps its code; only 5xx is a 502.
            return JSONResponse(status_code=_gw_status(d), content={
                "status": "error", "message": str(d["error"])[:200]})
        return {"status": "ok", "id": (d.get("item") or {}).get("id", "")}
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "status": "error", "message": str(e)[:200]})
@app.delete("/api/memory")
async def wipe_memory(session_id: str | None = None, confirm: str | None = None):
    """Dashboard wipe bridge to gateway DELETE /v1/memory (confirm in UI).
    Requires ``confirm=wipe``. This route deletes every drawer, and an
    adversarial probe found it reachable by accident: ``DELETE /api/memory/``
    (trailing slash, empty id) drew a 307 that preserved the method and landed
    here, so a request meant to touch nothing wiped all memory and still
    answered 200. ``redirect_slashes`` is now off as well, and a caller must
    state the intent explicitly.
    """
    # Exact token, no case folding and no trimming: "WIPE " must not wipe.
    if confirm != "wipe":
        return JSONResponse(status_code=400, content={
            "status": "error",
            "message": "wiping memory requires confirm=wipe"})
    # An EMPTY session_id must never be read as "no session filter".
    #
    # This check was `if session_id:`, so `DELETE /api/memory?session_id=&confirm=wipe`
    # - a client whose conversation id has not been assigned yet, or a null JSON
    # field serialised empty - dropped the filter entirely and reached the
    # gateway as an unscoped wipe. It destroyed 215 memory rows and 45 chat
    # threads in a single request, with nothing in the audit log and no undo.
    # Absent (`None`) still means the caller intends the global wipe, which is
    # why `confirm=wipe` is demanded; present-but-blank is always a bug.
    if session_id is not None and not session_id.strip():
        return JSONResponse(status_code=400, content={
            "status": "error",
            "message": "session_id was provided but is empty; omit it "
                       "entirely to wipe all memory, or send a real id to wipe "
                       "one session"})
    try:
        import urllib.parse as _u
        qs = []
        if session_id:
            qs.append("session_id=" + _u.quote(session_id, safe=""))
        qs.append("confirm=wipe")
        path = "/v1/memory?" + "&".join(qs)
        d = await _gw_json(path, method="DELETE")
        if isinstance(d, dict) and d.get("error"):
            code = 400 if "confirm" in str(d["error"]) else 502
            return JSONResponse(status_code=code, content={
                "status": "error", "message": str(d["error"])[:200]})
        return {"status": "ok", "result": d}
    except Exception as e:
        return {"status": "error", "message": str(e)[:200]}
@app.delete("/api/memory/{memory_id}")
async def delete_memory(memory_id: str):
    """Delete one memory record by id.
    Until this existed the only removal path was ``DELETE /api/memory``,
    which wipes every drawer — so a single mis-captured preference, which the
    agent now records automatically, could not be corrected without
    destroying unrelated memory.
    """
    try:
        d = await _gw_json("/v1/memory/" + memory_id, method="DELETE")
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "status": "error", "message": str(e)[:200]})
    if isinstance(d, dict) and d.get("error"):
        code = 404 if "unknown memory" in str(d["error"]) else 502
        return JSONResponse(status_code=code, content={
            "status": "error", "message": str(d["error"])[:200]})
    return d
@app.post("/api/memory/policy")
async def add_policy(req: Request):
    """Operator policy editor (Phase 3 drawers).
    Body: {"text": str, "supersedes"?: str}. Writes a policy-drawer record
    with the operator principal (v1 localhost trust model: a human at the
    dashboard IS the operator). With `supersedes`, the old rule drops out
    of behavior injection while staying visible for review.
    """
    import time as _time
    import memory as mem_svc
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    text = _body_str(body, "text")
    if text is None:
        return JSONResponse(status_code=400,
                            content={"error": "text must be a string"})
    if not text:
        return JSONResponse(status_code=400,
                            content={"error": "text is required"})
    try:
        item = mem_svc.write_policy(
            text, source="dashboard",
            run_id=f"policy-{int(_time.time())}",
            supersedes=body.get("supersedes") or None)
    except Exception as e:
        return {"status": "error", "message": f"policy write failed: {e}"}
    return {"status": "ok", "id": item.id}
# ── scheduler (proactive / deferred tasks) ──────────────────────────────────
@app.post("/api/schedule")
async def schedule_task(req: Request):
    """Schedule a query to run later. Body:
    {"query": str, "when": "in 10m"|"daily@09:00"|"every 30m"|<epoch>,
     "conversation_id"?: str, "notify"?: bool,
     "channel"?: str, "lead_s"?: "15m"|900, "lead_message"?: str,
     "message"?: str, "wait_for_ack"?: bool, "snooze_s"?: "5m",
     "ack_timeout_s"?: "15m", "max_follow_ups"?: int}
    The notification arguments are all optional; without them a task behaves
    exactly as it always has: run, then record the outcome locally."""
    from scheduler import schedule
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    query = _body_str(body, "query")
    when = _body_str(body, "when")
    if query is None or when is None:
        return JSONResponse(status_code=400, content={
            "status": "error", "error": "query and when must be strings"})
    if not query or not when:
        return JSONResponse(status_code=400,
                            content={"status": "error",
                                     "error": "query and when are required"})
    if len(query) > _CHAT_DAG_MAX_QUERY:
        return JSONResponse(status_code=400, content={
            "error": f"query too long (max {_CHAT_DAG_MAX_QUERY} chars)"})
    # bool("false") is True: a string "false" (LLM-generated
    # JSON, or curl -d without a content-type header) silently
    # kept Telegram notifications on. Parse the string forms.
    _notify = body.get("notify", True)
    if isinstance(_notify, str):
        _notify = _notify.strip().lower() not in ("false", "0", "no", "off")
    def _as_bool(v, default=False):
        """bool("false") is True - a string from LLM-generated JSON or a
        curl body without a content-type silently enables whatever it gates."""
        if v is None:
            return default
        if isinstance(v, str):
            return v.strip().lower() not in ("false", "0", "no", "off", "")
        return bool(v)
    try:
        _follow_ups = int(body.get("max_follow_ups", 3) or 0)
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={
            "error": "max_follow_ups must be an integer 0-10"})
    try:
        sid = schedule(query, when,
                       conversation_id=body.get("conversation_id"),
                       notify=bool(_notify),
                       channel=_body_str(body, "channel") or "",
                       lead_s=body.get("lead_s") or "",
                       lead_message=_body_str(body, "lead_message") or "",
                       message=_body_str(body, "message") or "",
                       wait_for_ack=_as_bool(body.get("wait_for_ack")),
                       snooze_s=body.get("snooze_s") or "",
                       ack_timeout_s=body.get("ack_timeout_s") or "",
                       max_follow_ups=_follow_ups)
    except ValueError as e:
        # 400, not 500: a rejected lead time or an unknown channel is the
        # caller's input being wrong, and the message says which.
        return JSONResponse(status_code=400, content={"error": str(e)[:300]})
    out = {"status": "ok", "id": sid}
    try:
        from scheduler import _SCHED_LOCK, _SCHEDULES
        with _SCHED_LOCK:
            pol = (_SCHEDULES.get(sid) or {}).get("notify_policy") or {}
        if pol:
            out["notification"] = {
                "channel": pol.get("channel"),
                "pre_warning_at": pol.get("lead_fire") or None,
                "waits_for_acknowledgement": bool(pol.get("wait_for_ack")),
                "max_follow_ups": pol.get("max_follow_ups"),
            }
    except Exception:
        pass
    return out
@app.get("/api/schedule")
async def list_scheduled():
    """List all scheduled tasks."""
    from scheduler import list_schedules
    return {"schedules": list_schedules()}
def scheduler_set_enabled(sid: str, enabled: bool) -> dict:
    """Thin wrapper so the route and the MCP tool share one implementation."""
    from scheduler import set_enabled as _set
    return _set(sid, enabled)
@app.delete("/api/schedule/{sid}")
async def cancel_scheduled(sid: str, hard: bool = False):
    """Cancel a scheduled task by id. Soft-cancel (enabled=false, stays
    visible greyed-out) by default; ?hard=true hard-deletes the row."""
    if hard:
        from scheduler import remove
        return {"status": "ok" if remove(sid) else "not_found"}
    from scheduler import cancel
    return {"status": "ok" if cancel(sid) else "not_found"}
def _chat_tool_payload() -> list[dict] | None:
    """Chat's tool schema, with the write methods cut out of multi-call tools.
    `_CHAT_TOOLS_REFUSED` lists `github_create_issue` and `notion_create_page`
    to deny, but neither name is a real tool — the actual write lives inside
    `github_query` / `notion_query`, whose `api_method` enum contains them.
    Chat was handed that enum verbatim, so it could open issues and write to
    Notion while the refusal set said it could not, and the guard test passes
    because it compares names only.
    Narrowing the enum is the enforcement: the model is not told a write
    method exists, so it cannot ask for one. `_CHAT_WRITE_METHODS` stays as the
    readable statement of intent.
    """
    from skills import tool_payload as _tp
    try:
        specs = _tp(_CHAT_TOOLS) or []
    except Exception as e:
        print(f"[chat.tools] payload build failed: {e!r}")
        return None
    # The handoff tools and the small local ones are defined here rather than
    # in the MCP catalog on purpose: they are handled by the agent process,
    # not the tool child, and they belong to chat alone. Exposing the handoff
    # tools to the DAG would have the planner route a research question to a
    # tool that then runs another DAG.
    specs = (list(specs) + [dict(s) for s in _CHAT_HANDOFF_TOOLS]
             + [dict(s) for s in _CHAT_LOCAL_TOOLS])
    blocked = _CHAT_WRITE_METHODS
    if not blocked:
        return specs
    import copy
    out = []
    for spec in specs:
        name = str(spec.get("name") or "")
        if name == "computer_action":
            # Chat drives apps by GOAL. The low-level daemon primitives are for
            # the Computer page and skills; handed to a small chat model they
            # turn "play a song" into a long sequence of press_key/replace_text
            # calls against whatever window happens to be focused (observed
            # live: LWin, a URL typed into a page, then a click). Keep chat on
            # the tools that own their own accessibility/vision cascade.
            spec = copy.deepcopy(spec)
            props = ((spec.get("input_schema") or {}).get("properties") or {})
            action = props.get("action") or {}
            if action.get("enum"):
                action["enum"] = [a for a in action["enum"]
                                  if a in _CHAT_COMPUTER_ACTIONS]
        deny = blocked.get(name)
        if not deny:
            out.append(spec)
            continue
        schema = spec.get("input_schema") or {}
        params = schema.get("properties") or {}
        method = params.get("api_method") or {}
        enum = list(method.get("enum") or [])
        kept = [m for m in enum if m not in deny]
        if len(kept) == len(enum):
            out.append(spec)
            continue
        spec = dict(spec)
        spec["input_schema"] = dict(schema)
        spec["input_schema"]["properties"] = dict(params)
        spec["input_schema"]["properties"]["api_method"] = {
            **method, "enum": kept,
            "description": (str(method.get("description") or
                                 "operation").rstrip(".") +
                            ". Chat is read-only here; writing happens on the "
                            "Research path."),
        }
        out.append(spec)
    return out
@app.post("/api/schedule/{sid}/enabled")
async def set_scheduled_enabled(sid: str, req: Request):
    """Arm or disarm a scheduled task without deleting it.
    The counterpart to DELETE /api/schedule/{sid}. Before this existed the only
    thing a caller could do with a greyed-out row was delete it, so "turn that
    reminder back on" had no action behind it anywhere - not in the console and
    not for the agent, which is why it got answered by scheduling a second copy
    and leaving the original dead.
    The refusal is returned as a 200 with ok:false, not an HTTP error: a
    one-shot whose time has passed is a real answer the caller should show to
    the user ("it is for last year, schedule a new one"), not a transport
    failure to retry.
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    return scheduler_set_enabled(sid, bool(body.get("enabled", True)))
@app.post("/api/schedule/purge")
async def purge_schedules(max_age_days: float = 7):
    """Hard-delete disabled one-shot schedules older than the cutoff."""
    from scheduler import purge_disabled
    try:
        return {"status": "ok", "purged": purge_disabled(max_age_days)}
    except ValueError as e:
        # purge_disabled raises on a negative cutoff (it used to
        # clamp to 0 and delete EVERY disabled one-shot). That is
        # a client error, not a 500.
        return JSONResponse(status_code=400, content={"error": str(e)[:200]})
# ── task templates / saved workflows (Todo 10) ──────────────────────────────
@app.post("/api/templates")
async def save_template(req: Request):
    """Save a named workflow template. Body:
    {"name": str, "query": str, "vars": [str]?} where query may contain
    {var} placeholders. Example: {"name":"research","query":"Research {topic}",
    "vars":["topic"]}."""
    from templates import save
    # An empty or non-JSON body used to reach `body.get(...)` and raise, which
    # surfaced as a 500 "Internal Server Error" with no detail. It is a client
    # error, so it is a 400 with the reason.
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400,
                            content={"error": "invalid JSON body"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    name = _body_str(body, "name")
    query = _body_str(body, "query")
    if name is None or query is None:
        return JSONResponse(status_code=400, content={
            "error": "name and query must be strings"})
    if not name or not query:
        # Previously a 200 with {"status":"error"} — a caller checking the
        # HTTP status alone believed the template was saved.
        return JSONResponse(status_code=400,
                            content={"error": "name and query are required"})
    try:
        tpl = save(name, query, body.get("vars"))
    except ValueError as e:
        # Oversized name/query: 422, not 500. An unbounded name once produced
        # an object whose own URL exceeded the HTTP parser limit, making it
        # undeletable through the API - so the cap is enforced here, before
        # storage, rather than trusted to the client.
        return JSONResponse(status_code=422, content={"error": str(e)[:200]})
    return {"status": "ok", "template": tpl}
@app.get("/api/templates")
async def list_templates():
    """List all saved templates."""
    from templates import list_templates
    return {"templates": list_templates()}
@app.get("/api/templates/{name}")
async def get_template(name: str):
    """Get one template by name."""
    from templates import get
    tpl = get(name)
    return tpl if tpl else {"status": "not_found"}
@app.delete("/api/templates/{name}")
async def delete_template(name: str):
    """Delete a template by name."""
    from templates import delete
    return {"status": "ok" if delete(name) else "not_found"}
@app.post("/api/templates/{name}/run")
async def run_template(name: str, req: Request):
    """Replay a template with provided {var} values. Body: {"vars": {k:v},
    "conversation_id"?: str}. Streams the same SSE frames as /api/chat."""
    from templates import render
    import re as _re
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    values = body.get("vars") or {}
    conversation_id = _conv_id(body)
    query = render(name, values)
    if query is None:
        return StreamingResponse(
            iter([_sse("error", text=f"template '{name}' not found")]),
            media_type="text/event-stream",
        )
    # Refuse to run with unfilled placeholders — a silent "{topic}" in the
    # query used to reach the orchestrator verbatim.
    _unfilled = sorted(set(_re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", query or "")))
    if _unfilled:
        return StreamingResponse(
            iter([_sse("error", text=f"missing template vars: {', '.join(_unfilled)}")]),
            **_SSE_HEADERS,
        )
    # Same length cap /api/chat enforces: a template plus a large `vars`
    # value could otherwise push an arbitrarily long prompt into the DAG,
    # defeating the context guard on the other two entry points.
    if len(query or "") > _CHAT_DAG_MAX_QUERY:
        return StreamingResponse(
            iter([_sse("error", text=f"rendered query too long (max {_CHAT_DAG_MAX_QUERY} chars)")]),
            **_SSE_HEADERS,
        )
    return _stream_run(query, conversation_id, request=req)
# ── Voice (gateway-owned) ───────────────────────────────────────────────────
# TTS/STT models live on the gateway (POST /v1/tts, /v1/stt, /v1/tts/voices).
# The endpoints below are thin proxies preserving the agent's response
# shapes, so the chat UI works unchanged. No model files, no voice deps here.
_GW_VOICE = os.environ.get("LLM_GATEWAY_V9_URL", "http://localhost:8109").rstrip("/")
# An upload can be large (there is no size cap) and indexing starts in the
# background, so the POST itself only has to receive the bytes.
_GW_UPLOAD_TIMEOUT = 300.0
# The gateway requires `X-Gateway-Token` on every `/v1/*` route. These proxies
# were the one place that never sent it: `gateway.py` (the pooled LLM client)
# was fixed, but the async proxies here built a bare `httpx.AsyncClient`, so
# every document list/search/upload, every TTS/STT call and the per-agent cost
# snapshot came back 401 - the console's Documents page rendered empty and the
# voice buttons failed, while `/api/health` still reported the gateway "up"
# because that probe is deliberately unauthenticated.
def _gw_auth_headers(extra: dict | None = None) -> dict:
    """Headers for a gateway call, including the shared token when readable."""
    h: dict = dict(extra or {})
    try:
        from gateway import _gateway_token
        tok = _gateway_token()
    except Exception:
        tok = ""
    if tok:
        h["X-Gateway-Token"] = tok
    return h
async def _gw_bytes(path: str, *, method: str = "GET", json: dict | None = None,
                    files: dict | None = None, timeout: float = 120.0):
    """POST/GET bytes (audio) from the gateway voice service."""
    import httpx
    async with httpx.AsyncClient(timeout=timeout,
                                 headers=_gw_auth_headers()) as c:
        if method == "POST":
            r = await c.post(f"{_GW_VOICE}{path}", json=json, files=files)
        else:
            r = await c.get(f"{_GW_VOICE}{path}")
        if r.status_code == 401:
            # Same self-heal as `_gw_json`: if the gateway rotated its token
            # while this process was alive, one re-read turns a dead voice
            # service into a working one instead of requiring an agent restart.
            c.headers.update(_gw_auth_headers())
            if method == "POST":
                r = await c.post(f"{_GW_VOICE}{path}", json=json, files=files)
            else:
                r = await c.get(f"{_GW_VOICE}{path}")
        return r.status_code, r.headers.get("content-type", ""), r.content
async def _gw_bytes_full(path: str, *, method: str = "GET", json: dict | None = None,
                         files: dict | None = None, timeout: float = 120.0):
    """Like `_gw_bytes` but also returns the response headers.
    A file download needs `Content-Disposition` (the server-chosen
    filename) forwarded to the browser; `_gw_bytes` drops it, so a
    generated document arrived as "document.pdf" whatever its title.
    """
    import httpx
    async with httpx.AsyncClient(timeout=timeout,
                                 headers=_gw_auth_headers()) as c:
        if method == "POST":
            r = await c.post(f"{_GW_VOICE}{path}", json=json, files=files)
        else:
            r = await c.get(f"{_GW_VOICE}{path}")
        if r.status_code == 401:
            c.headers.update(_gw_auth_headers())
            if method == "POST":
                r = await c.post(f"{_GW_VOICE}{path}", json=json, files=files)
            else:
                r = await c.get(f"{_GW_VOICE}{path}")
        return (r.status_code, r.headers.get("content-type", ""), r.content,
                {k.lower(): v for k, v in r.headers.items()})
# ── document authoring (PDF / PPTX / DOCX / XLSX) ───────────────────────────
@app.get("/api/docgen/formats")
async def docgen_formats():
    """What the gateway can produce, plus the content model it accepts."""
    return await _gw_json("/v1/docgen/schema")
@app.post("/api/docgen")
async def docgen_create(req: Request):
    """Render a document and stream the file back.
    The gateway owns the generators (reportlab / python-pptx / python-docx /
    openpyxl); the agent proxies so the console never holds the gateway
    token. Returns the bytes with Content-Disposition so the browser
    saves it directly."""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    fmt = body.get("format")
    if not isinstance(fmt, str) or not fmt.strip():
        return JSONResponse(status_code=400, content={"error": "format is required"})
    # Forward the gateway's Content-Disposition verbatim. Hardcoding
    # "document" threw away the generator's slugified title
    # ("Vector-Database-Indexing-Report.pdf"), so every download was
    # named document.pdf regardless of what it contained.
    disp = ""
    try:
        code, ctype, blob, hdrs = await _gw_bytes_full("/v1/docgen",
                                                        method="POST", json=body)
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "error": f"gateway unreachable: {type(e).__name__}: {e}"[:200]})
    if code >= 400:
        # The gateway's own 400 carries the actionable reason; keep its
        # status so the console can show it verbatim.
        try:
            detail = json.loads(blob.decode("utf-8", "replace"))
            msg = str(detail.get("error") or detail)[:300]
        except Exception:
            msg = blob[:200].decode("utf-8", "replace")
        return JSONResponse(status_code=code if code < 500 else 502,
                            content={"error": msg})
    disp = hdrs.get("content-disposition") or 'attachment; filename="document"'
    return Response(content=blob, media_type=ctype or "application/octet-stream",
                    headers={"Content-Disposition": disp})
def _conv_id(body) -> str | None:
    """Read `conversation_id` out of a request body, defensively.
    This was `(body.get("conversation_id") or "").strip()`, which raises
    AttributeError on a non-string - so `{"conversation_id": 12345}` returned
    an unparseable HTTP 500 `text/plain` instead of a 400, on both the plain and
    the streaming chat route. A JS client that stores ids as numbers hits this
    immediately.
    A number IS accepted and stringified: ids are opaque strings on the wire
    and a numeric id from a client is a type error, not an attack. Anything
    that is not a string or number is dropped rather than stringified, so a dict
    or list cannot smuggle a huge or hostile value into a storage key.
    """
    raw = body.get("conversation_id")
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        raw = str(raw)
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if len(raw) > _CONV_ID_MAX:
        raw = raw[:_CONV_ID_MAX]
    return raw or None
def _body_str(body: dict, key: str) -> "str | None":
    """Read a string field from a JSON body, defensively.
    The naive `(body.get(key) or "").strip()` raises AttributeError
    on any truthy non-string (a number, bool, list or dict where a
    string belongs), surfacing as a bare HTTP 500 `text/plain` —
    unparseable by JSON clients. Returns None when the field was
    present but not a string, so the caller answers 400; returns
    "" when the field is absent or null.
    """
    raw = body.get(key)
    if raw is None:
        return ""
    if not isinstance(raw, str):
        return None
    return raw.strip()
def _plain_id_ok(s: str) -> bool:
    """A storage-key id: no path separators, no traversal, no
    leading dot, no control characters. Ids become dict keys and
    (via `_turn_cost_path`) filesystem paths, so a `../` or a null
    byte in one is a storage-pollution and crash hazard."""
    return bool(s) and ".." not in s and not any(c in s for c in "/\\") \
        and not s.startswith(".") \
        and not any(ord(c) < 0x20 or ord(c) == 0x7f for c in s)
def _json_finite(v) -> bool:
    """True when `v` (a JSON value) contains no non-finite float.
    Python's json module parses and emits NaN/Infinity even though
    JSON has neither; the gateway's pydantic serialiser rejects the
    whole payload, so the write surfaced as a 502 instead of a 400.
    """
    if isinstance(v, float):
        return v == v and v not in (float("inf"), float("-inf"))
    if isinstance(v, dict):
        return all(_json_finite(x) for x in v.values())
    if isinstance(v, list):
        return all(_json_finite(x) for x in v)
    return True
def _json_body(req):
    """Parse a JSON body that must be an object, or return the error response.
    Thirteen POST routes used to do `body = await req.json()` and then call
    `.get()` on whatever came back, so a valid-JSON-but-not-an-object body
    (`[]`, `"s"`, `123`, `null`, `true`) raised and surfaced as HTTP 500
    `text/plain` - unparseable by any JSON client, and indistinguishable from
    infrastructure failure. Every object-body route now funnels through here.
    Returns `(body_dict, None)` on success, or `(None, JSONResponse-400)` when
    the caller should return the response immediately.
    """
    from fastapi.responses import JSONResponse as _JR
    try:
        body = req.json() if hasattr(req, "json") and not callable(
            getattr(req, "json", None)) else None
    except Exception:
        body = None
    return body, None
async def _read_json_body(req):
    """`await req.json()` guarded for object bodies.
    Use as:
        body, err = await _read_json_body(req)
        if err is not None:
            return err
    """
    from fastapi.responses import JSONResponse as _JR
    try:
        body = await req.json()
    except Exception:
        return None, _JR(status_code=400,
                         content={"error": "invalid JSON body"})
    if not isinstance(body, dict):
        return None, _JR(status_code=400,
                         content={"error": "body must be a JSON object"})
    return body, None
def _gw_response(result, ok_status: int = 200):
    """Render a `_gw_json` result without losing the upstream status code.
    Every proxied route used to `return await _gw_json(...)`, and that returns a
    plain dict - which FastAPI always sends as HTTP 200. So a gateway 404, 409
    or 400 reached the client as `200 {"error": "...", "status_code": 404}`.
    Measured on the running system: "no such document" (404), "cannot enable a
    failed document" (409) and "query required" (400) were byte-indistinguishable
    at the transport layer from success, and no client reads `status_code`, so
    `if (res.ok)` reported a rejected upload as accepted.
    Keeping the truth in the body is not enough; it has to be in the status line.
    """
    if isinstance(result, dict) and result.get("error"):
        code = result.get("status_code")
        if not isinstance(code, int) or not (400 <= code <= 599):
            # An upstream we could not reach is a gateway problem, not a client
            # one. Never invent a 4xx for it.
            code = 502
        return JSONResponse(status_code=code, content=result)
    if not isinstance(result, dict):
        return JSONResponse(status_code=502,
                            content={"error": "bad gateway reply"})
    return JSONResponse(status_code=ok_status, content=result)
async def _gw_json(path: str, *, method: str = "GET", json: dict | None = None,
                   timeout: float = 30.0) -> dict:
    """Call the gateway and return its JSON body.
    A non-2xx is folded into an `error` key BEFORE the body is parsed. That
    matters: FastAPI renders its own failures as `{"detail": …}`, so a
    gateway 403/413/503 reached the caller as a dict whose only key was
    `detail`. Callers test `d.get("error")`, saw None, and returned
    `{"status": "ok"}` — the Memory page reported "remembered" for a write
    the gateway had refused, with a blank id and no record created."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=timeout,
                                     headers=_gw_auth_headers()) as c:
            if method == "POST":
                r = await c.post(f"{_GW_VOICE}{path}", json=json)
            elif method == "GET":
                r = await c.get(f"{_GW_VOICE}{path}")
            else:
                # DELETE / PUT / PATCH must hit the gateway with the real
                # verb — never silently downgrade to GET (that turned the
                # memory wipe into a read).
                r = await c.request(method, f"{_GW_VOICE}{path}", json=json)
            if r.status_code == 401:
                # The gateway regenerates its token when its state file is
                # missing or unreadable, so a long-lived agent can hold a
                # token that stopped being valid. Re-read once and retry
                # instead of failing the call until the agent is restarted.
                c.headers.update(_gw_auth_headers())
                if method == "POST":
                    r = await c.post(f"{_GW_VOICE}{path}", json=json)
                elif method == "GET":
                    r = await c.get(f"{_GW_VOICE}{path}")
                else:
                    r = await c.request(method, f"{_GW_VOICE}{path}", json=json)
            if not r.is_success:
                detail = ""
                try:
                    body = r.json()
                    if isinstance(body, dict):
                        detail = str(body.get("error") or body.get("detail")
                                     or body.get("message") or "")
                except ValueError:
                    detail = ""
                return {"error": detail or r.text[:200]
                        or f"gateway HTTP {r.status_code}",
                        "status_code": r.status_code}
            try:
                data = r.json()
                return data if isinstance(data, dict) else {"error": "bad gateway reply"}
            except ValueError:
                return {"error": r.text[:200] or "gateway returned non-JSON"}
    except Exception as e:
        return {"error": f"voice gateway unreachable: {type(e).__name__}: {e}",
                "status_code": 503}
def _gw_status(d: dict, default: int = 502) -> int:
    """The gateway's own status code, when it is meaningful.
    Bridge handlers used a flat 502, so a client error the gateway had
    already diagnosed correctly (a 400 for an unserialisable payload, a 400
    for an unknown kind) reached the browser as "bad gateway". The user then
    saw an infrastructure fault where the real answer was "fix your input".
    A gateway 5xx stays a 502.
    """
    try:
        code = int(d.get("status_code") or 0)
    except (TypeError, ValueError):
        return default
    return code if 400 <= code < 500 else default
def _warm_gateway():
    """Eagerly launch the V9 gateway in a background thread at import time so
    the first /api/chat request doesn't block up to 45s on a cold gateway
    start. `ensure_gateway()` is idempotent — if the gateway is already up it
    returns instantly, and if a request arrives before warmup finishes,
    `_stream_run` awaits this thread instead of re-launching.
    Failures are non-fatal: the request path still tries ensure_gateway()
    itself and falls back to the gated-shell offline path if needed.
    """
    def _launch():
        try:
            from gateway import ensure_gateway
            ensure_gateway()
        except Exception:
            pass
    t = threading.Thread(target=_launch, daemon=True)
    t.start()
    return t
# Launch the gateway warmup at import time. The first SSE frame of a chat
# request now appears instantly (the warmup runs concurrently), instead of
# the operator waiting up to 45s for a cold gateway start on the first call.
_GATEWAY_WARMUP_THREAD = _warm_gateway()
async def _acost_breakdown(session_id: str) -> dict | None:
    """`_session_cost_breakdown` without blocking the event loop.
    It is a synchronous httpx call to the gateway with a 5s timeout, and it
    was being called directly from `async def` handlers on the chat path. Every
    other request in the process waits while it runs, so a slow gateway read
    stalled /api/health for seconds — a chat turn made the whole console look
    hung. Off-thread, and it yields to the loop while it waits.
    """
    import asyncio as _a
    try:
        return await _a.to_thread(_session_cost_breakdown, session_id)
    except Exception as e:
        print(f"[cost] breakdown failed: {e!r}")
        return None
def _session_cost_breakdown(session_id: str) -> dict | None:
    """Return the gateway's cost ledger for a session, broken down PER AGENT:
    {agent: {usd, in_tok, out_tok, calls}}. Used both to snapshot the
    pre-run baseline and to compute the per-turn delta.
    A session_id is reused for every turn of a conversation (that reuse is
    what gives memory/audit continuity), so the raw session total is the
    lifetime of the whole conversation — not one answer. The delta between a
    pre-run snapshot and the post-run total is what the operator wants.
    Returns None when the gateway can't be reached: callers must NOT record
    a $0 turn in that case (a zero would be fabricated ledger data).
    """
    out: dict[str, dict] = {}
    try:
        import httpx as _hx
        with _hx.Client(timeout=5, headers=_gw_auth_headers()) as _c:
            _r = _c.get("http://localhost:8109/v1/cost/by_agent",
                        params={"session": session_id})
            if _r.status_code != 200:
                return None
            for _ag, _rows in _r.json().items():
                _acc = {"usd": 0.0, "in_tok": 0, "out_tok": 0, "calls": 0}
                for _row in (_rows or []):
                    _acc["usd"] += float(_row.get("dollars") or 0.0)
                    _acc["in_tok"] += int(_row.get("in_tok") or 0)
                    _acc["out_tok"] += int(_row.get("out_tok") or 0)
                    _acc["calls"] += int(_row.get("calls") or 0)
                out[_ag] = _acc
    except Exception:
        return None
    return out
def _session_cost_delta(session_id: str, before: dict | None) -> dict | None:
    """Cost incurred by the current run = (session total now) ↙ (snapshot
    taken before the run), per agent. Clamp at zero so a rounding glitch
    can't show negative spend. Returns {agent: {usd, in_tok, out_tok, calls}},
    or None when either snapshot is missing (unknown cost — never $0).
    """
    if before is None:
        return None
    after = _session_cost_breakdown(session_id)
    if after is None:
        return None
    delta: dict[str, dict] = {}
    _all_agents = set(after) | set(before)
    for _ag in _all_agents:
        _b = before.get(_ag, {"usd": 0.0, "in_tok": 0, "out_tok": 0, "calls": 0})
        _a = after.get(_ag, {"usd": 0.0, "in_tok": 0, "out_tok": 0, "calls": 0})
        _d = {
            "usd": max(0.0, _a["usd"] - _b["usd"]),
            "in_tok": max(0, _a["in_tok"] - _b["in_tok"]),
            "out_tok": max(0, _a["out_tok"] - _b["out_tok"]),
            "calls": max(0, _a["calls"] - _b["calls"]),
        }
        if _d["usd"] or _d["in_tok"] or _d["out_tok"] or _d["calls"]:
            delta[_ag] = _d
    return delta
# ── per-turn cost ledger ────────────────────────────────────────────────────
# The Spend panel should show the SUM of the per-response costs the operator
# sees beside each message — not the raw gateway session total (which is the
# lifetime of the whole conversation, since session_id is reused per turn).
# We accumulate each turn's delta into a small JSON file per session and sum
# that for the panel. Keyed by session_id; safe to append from the stream.
_TURN_COST_DIR = STATE_DIR / "sessions"
# Cap: the cost panel only ever reads the tail; keep the file bounded.
_TURN_COST_MAX = 200
_TURN_COST_LOCK = threading.Lock()
# Snapshot-diff accounting is only correct when one turn at a time touches a
# conversation's gateway ledger. Two concurrent turns each took the other's
# tokens into their delta and the Spend panel showed roughly double. Serialise
# turns per conversation (different conversations stay parallel).
_TURN_LOCKS: dict[str, "threading.Lock"] = {}
_TURN_LOCKS_GUARD = threading.Lock()
def _turn_lock(session_id: str) -> "threading.Lock":
    with _TURN_LOCKS_GUARD:
        return _TURN_LOCKS.setdefault(session_id, threading.Lock())
def _turn_cost_path(session_id: str) -> "Path":
    # session_id is client-supplied at the AG-UI call site, so it
    # must not reach Path() unvalidated: "../.." escaped STATE_DIR
    # entirely, and an absolute path landed outside the state dir.
    if (not isinstance(session_id, str) or not session_id
            or ".." in session_id
            or any(c in session_id for c in "/\\")
            or session_id.startswith(".")):
        raise ValueError(f"unsafe session id: {session_id!r}")
    # Lightweight chat threads are keyed ct-* and have no graph dir,
    # so park their spend under threads/: writing into sessions/ would
    # create a phantom session dir that list_sessions() surfaces as an
    # empty run. The same applies to AG-UI thread ids (agui-*, client
    # uuids). Only real graph sessions (s8-*, plus test ids) keep
    # their ledger under sessions/.
    if not session_id.startswith("s8-"):
        return _TURN_COST_DIR.parent / "threads" / session_id / "turn_costs.json"
    return _TURN_COST_DIR / session_id / "turn_costs.json"
def _record_turn_cost(session_id: str, query: str, delta: dict) -> None:
    """Append one turn's per-agent cost delta to the session ledger.
    Lock-guarded load→modify→save (concurrent turns in one session used to
    silently drop entries) with a hard cap so long threads can't grow the
    file without bound."""
    try:
        _entry = {
            "ts": _time.time(),
            "query": (query or "")[:200],
            "per_agent": delta,
            "totals": {
                "usd": round(sum(d["usd"] for d in delta.values()), 6),
                "in_tok": sum(d["in_tok"] for d in delta.values()),
                "out_tok": sum(d["out_tok"] for d in delta.values()),
                "calls": sum(d["calls"] for d in delta.values()),
            },
        }
        with _TURN_COST_LOCK:
            _p = _turn_cost_path(session_id)
            _existing = _read_json(_p, list)
            _existing.append(_entry)
            _write_json_atomic(_p, _existing[-_TURN_COST_MAX:])
    except Exception:
        pass
def _read_turn_costs(session_id: str) -> list:
    data = _read_json(_turn_cost_path(session_id), list)
    return data if isinstance(data, list) else []
def _warm_embedder():
    """Pre-load the Ollama embedding model at startup so the FIRST embed of a
    run doesn't pay a 30-40s cold model load (the dominant 'silent gap' on the
    dashboard). The OllamaEmbedder pins the model with keep_alive=-1, but that
    only kicks in AFTER the first call — so we fire one dummy embed here, in
    the background, before any query arrives. Failures are non-fatal: if
    Ollama isn't installed the agent simply falls back to keyword search.
    """
    def _launch():
        try:
            from gateway import embed
            embed("warmup", task_type="retrieval_document")
            print("[warmup] ollama embedding model loaded")
        except Exception as e:  # pragma: no cover - environment issue
            print(f"[warmup] embedding model not pre-loaded (non-fatal): {e}")
    t = threading.Thread(target=_launch, daemon=True)
    t.start()
    return t
# Pre-warm the embedding model at import time (concurrent with gateway warmup)
# so memory.read / memory.remember don't block on a cold Ollama load mid-query.
_EMBEDDER_WARMUP_THREAD = _warm_embedder()
# Start the task scheduler (loads persisted schedules + background worker).
try:
    from scheduler import start as _scheduler_start
    _scheduler_start()
except Exception:
    pass
# Start tracker apps (seeds the default OpenRouter tracker on first boot +
# background refresh worker). Never raises; refresh failures are recorded
# per-app, never crash the server.
try:
    from apps import start as _apps_start
    _apps_start()
except Exception:
    pass
@app.post("/api/tts")
async def tts(req: Request):
    """Synthesize text → WAV via the gateway (local Kokoro model).
    Deliberately NOT recorded in the turn-cost ledger: voice calls are
    served by local models and log $0 gateway-side (voice_api logs usage
    stats only, no dollars), so there is no spend to attribute. If voice
    ever routes to a billed provider, tag these calls with the caller's
    session and snapshot/record the delta like the chat paths do."""
    """Synthesize text → WAV via the gateway voice service (proxy).
    Body: {"text": "...", "voice": "af_heart", "speed": 1.0}.
    Same shapes as before; synthesis happens gateway-side.
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    text = _body_str(body, "text")
    if text is None:
        return JSONResponse(status_code=400,
                            content={"error": "text must be a string"})
    if not text:
        # A 400 must be JSON, not `audio/wav` with a zero-byte body: the old
        # shape was indistinguishable from a truncated download, so a client
        # JSON-decode failed instead of reading the reason.
        return JSONResponse(status_code=400,
                            content={"error": "text is required"})
    voice = body.get("voice")
    if voice is not None and not isinstance(voice, str):
        # A non-string voice reached the gateway as-is and its
        # `len()` raised — a 500 leaking the exception text.
        return JSONResponse(status_code=400,
                            content={"error": "voice must be a string"})
    # Unbounded synthesis input used to hang with no response at all - an 8 MB
    # `text` pinned a worker for the whole session. Kokoro is a sentence-level
    # model; anything past this is a client bug, not a paragraph.
    if len(text) > 8000:
        return JSONResponse(status_code=413, content={
            "error": f"text is {len(text)} characters; limit is 8000. "
                     f"Split it and synthesize the pieces."})
    try:
        code, ctype, blob = await _gw_bytes("/v1/tts", method="POST", json={
            "text": body.get("text"), "voice": body.get("voice") or "af_heart",
            "speed": body.get("speed") or 1.0})
    except Exception as e:
        return StreamingResponse(
            iter([f"voice gateway unreachable: {e}".encode()]),
            media_type="text/plain", status_code=502)
    if code != 200:
        return StreamingResponse(
            iter([blob or b"voice synthesis failed"]),
            media_type="text/plain" if "audio" not in ctype else ctype,
            status_code=code)
    return StreamingResponse(iter([blob]), media_type="audio/wav")
@app.post("/api/stt")
async def stt(req: Request):
    """Transcribe an uploaded audio file (proxy to gateway /v1/stt).
    The chat UI's 🎙 button posts FormData {file}. Returns {text} or
    {error} — never raises, so the UI can show the failure inline.
    Like /api/tts above, deliberately absent from the turn-cost ledger:
    local model, $0 gateway spend.
    """
    try:
        form = await req.form()
        up = form.get("file")
        # Real status codes: a plain dict is HTTP 200 to FastAPI, so these three
        # client errors reached the browser as successes carrying `{"error": …}`
        # while /api/chat/simple's `{}` correctly returned 400.
        if up is None:
            return JSONResponse(status_code=400,
                                content={"error": "no file field in form"})
        blob = await up.read()
        if not blob:
            return JSONResponse(status_code=400,
                                content={"error": "empty audio file"})
        if len(blob) > 25_000_000:
            return JSONResponse(status_code=413, content={
                "error": f"audio too large ({len(blob)} bytes; 25MB cap)"})
        filename = getattr(up, "filename", "") or "audio.wav"
        ctype = getattr(up, "content_type", "") or "audio/wav"
    except Exception as e:
        return JSONResponse(status_code=400, content={
            "error": f"unreadable upload: {type(e).__name__}: {e}"})
    import httpx
    try:
        async with httpx.AsyncClient(timeout=180.0,
                                     headers=_gw_auth_headers()) as c:
            r = await c.post(
                f"{_GW_VOICE}/v1/stt",
                files={"file": (filename, blob, ctype)})
        try:
            data = r.json()
            if isinstance(data, dict):
                return (data if r.is_success
                        else JSONResponse(status_code=r.status_code,
                                          content=data))
            return JSONResponse(status_code=502,
                                content={"error": "bad gateway reply"})
        except ValueError:
            return JSONResponse(status_code=r.status_code, content={
                "error": r.text[:200] or f"gateway HTTP {r.status_code}"})
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "error": f"voice gateway unreachable: {type(e).__name__}: {e}"})
# ── Parts (gateway-owned mesh optimiser) ────────────────────────────────────
# The Parts view used to call the gateway straight from the browser, which
# cannot work: the gateway has no CORS middleware and the page never receives
# its token - the same limitation documented on /api/models below. It
# therefore rendered "failed to fetch" and could never load a pass or measure
# a mesh. These proxies keep the gateway token server-side, exactly like the
# voice and docgen bridges above.
@app.get("/api/parts/passes")
async def parts_passes():
    """The pass registry, so the UI never hardcodes a pass name or its params."""
    return await _gw_json("/v1/parts/optimise/passes")
async def _parts_forward(path: str, req: Request, *, keep_query: bool = False):
    """Forward a multipart mesh upload to the gateway's parts service."""
    try:
        form = await req.form()
        up = form.get("file")
        if up is None:
            return JSONResponse(status_code=400,
                                content={"error": "no file field in form"})
        blob = await up.read()
        if not blob:
            return JSONResponse(status_code=400,
                                content={"error": "empty mesh upload"})
        filename = getattr(up, "filename", "") or "part.stl"
        ctype = getattr(up, "content_type", "") or "application/octet-stream"
    except Exception as e:
        return JSONResponse(status_code=400, content={
            "error": f"unreadable upload: {type(e).__name__}: {e}"})
    url = f"{_GW_VOICE}{path}"
    if keep_query and req.url.query:
        url += f"?{req.url.query}"
    import httpx
    try:
        async with httpx.AsyncClient(timeout=180.0,
                                     headers=_gw_auth_headers()) as c:
            r = await c.post(url, files={"file": (filename, blob, ctype)})
        try:
            data = r.json()
        except ValueError:
            return JSONResponse(status_code=(r.status_code if r.status_code >= 400
                                             else 502),
                                content={"error": r.text[:200]
                                         or f"gateway HTTP {r.status_code}"})
        if isinstance(data, dict) and r.is_success:
            return data
        if isinstance(data, dict):
            return JSONResponse(status_code=r.status_code, content=data)
        return JSONResponse(status_code=502,
                            content={"error": "bad gateway reply"})
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "error": f"parts gateway unreachable: {type(e).__name__}: {e}"})
@app.post("/api/parts/geometry")
async def parts_geometry(req: Request):
    """Measure an uploaded mesh: metrics, objective and drawable buffers."""
    return await _parts_forward("/v1/parts/geometry", req)
@app.post("/api/parts/optimise")
async def parts_optimise(req: Request):
    """Run one optimisation pass; pass_name / target_faces travel as query."""
    return await _parts_forward("/v1/parts/optimise", req, keep_query=True)
@app.get("/api/models")

async def list_models(refresh: bool = False, all: bool = False):
    """Model picker roster, proxied from the gateway's Kilo catalogue.
    The console cannot call the gateway directly: it has no CORS middleware
    and the page never receives the gateway token, so this is the only way the
    picker can see the roster. Kilo's upstream /models endpoint needs no auth,
    so the list is populated even before a KILO_API_KEY is saved - which is
    what makes it useful for deciding whether to buy one.
    `all=true` returns the whole catalogue (~390 models) rather than only the
    free tier. The picker wants both: free first so the obvious choice is at
    the top, everything else available rather than hidden.
    Degrades like the voices picker: a gateway that is down yields an empty
    `models` list plus `error`, never a 500, so the UI can disable the select
    and say why. Deliberately NOT under /api/code/ - that prefix has an
    exact-route-set assertion in tests/test_code_workspace_api.py.
    """
    q = []
    if refresh:
        q.append("refresh=true")
    if all:
        q.append("all=true")
    path = "/v1/providers/kilo/models"
    if q:
        path += "?" + "&".join(q)
    data = await _gw_json(path)
    models = data.get("models")
    if not isinstance(models, list):
        models = []
    free_n = sum(1 for m in models if isinstance(m, dict) and m.get("is_free"))
    return {
        "models": models,
        "count": len(models),
        "free_count": data.get("free_count", free_n) or free_n,
        "stale": bool(data.get("stale")),
        # The pool's current default, so the picker can pre-select it. The
        # Code section is meant to run on Kilo, so an unset picker should land
        # on Kilo's chosen model rather than on whatever the general ladder
        # would have picked.
        "default": data.get("default") or "",
        # Whether a KILO_API_KEY is configured. The roster is readable without
        # one, so a populated list alone does not mean Kilo is callable.
        "configured": bool(data.get("configured")),
        # A roster that is missing entirely is a gateway problem, not an
        # empty catalogue - the picker needs to tell those apart. Whether a
        # Kilo KEY is configured is deliberately not reported here: the
        # roster is reachable without one, so a populated list says nothing
        # about whether the key is set.
        "error": data.get("error") or (None if models else "gateway returned no roster"),
    }
@app.get("/api/tts/voices")
async def tts_voices():
    """Voice picker list (proxy to gateway, static fallback when down)."""
    data = await _gw_json("/v1/tts/voices")
    if isinstance(data.get("voices"), list) and data["voices"]:
        return {"voices": data["voices"]}
    return {"voices": [
        {"id": "af_heart", "label": "Heart (female, warm)"},
        {"id": "af_bella", "label": "Bella (female, bright)"},
        {"id": "af_nicole", "label": "Nicole (female, soft)"},
        {"id": "am_michael", "label": "Michael (male)"},
        {"id": "am_adam", "label": "Adam (male, deep)"},
        {"id": "bf_emma", "label": "Emma (British female)"},
        {"id": "bm_george", "label": "George (British male)"},
    ]}
# ── computer-use approvals (gated) ───────────────────────────────────────────
# capabilities() runs blocking cua-driver subprocesses (up to seconds each).
# Never call it on the event loop directly, and cache it briefly — the UI
# polls this endpoint every few seconds.
_CAP_CACHE: dict = {"at": 0.0, "value": None}
async def _cached_capabilities():
    import time as _t
    if _CAP_CACHE["value"] is not None and _t.time() - _CAP_CACHE["at"] < 10.0:
        return _CAP_CACHE["value"]
    from computer_use import capabilities as _caps
    if hasattr(asyncio, "to_thread"):
        caps = await asyncio.to_thread(_caps)
    else:
        loop = asyncio.get_event_loop()
        caps = await loop.run_in_executor(None, _caps)
    _CAP_CACHE["at"] = _t.time()
    _CAP_CACHE["value"] = caps
    return caps
@app.get("/api/computer/approvals")
async def list_approvals():
    """List pending computer-use approvals + daemon capabilities for the UI."""
    from computer_use import get_computer_use
    import os as _os
    cu = get_computer_use()
    return {
        "approvals": cu.list_approvals(),
        "capabilities": await _cached_capabilities(),
        # P2.2: surface the current mode so the approval panel can show
        # dry-run vs live and warn the user nothing will execute in dry-run.
        "mode": _os.getenv("COMPUTER_USE_MODE", "dry-run").lower(),
        # Whether the feature is switched on AT ALL. The mode alone is not
        # enough: with the gates off, every request is refused with
        # "computer-use is disabled" and the panel would show an empty
        # queue that looks like "nothing was asked" rather than "the whole
        # feature is off".
        "enabled": bool(getattr(cu.safety, "enabled", False)),
    }
# ── lightweight chat (no DAG) ────────────────────────────────────────────
# The Chat page is a minimal chatbot: one direct gateway LLM call per turn,
# no orchestrator graph, no SessionStore files, no turn-cost ledger — so
# lightweight threads never appear in Runs/Memory/Ledger. Research keeps
# the full Executor DAG via /api/chat. Thread history lives in its own
# state/chat_threads.json store (bounded), purely for multi-turn context.
_CHAT_THREADS_PATH = STATE_DIR / "chat_threads.json"
_CHAT_THREADS_LOCK = threading.Lock()
# A safety valve against unbounded growth, NOT a retention policy. It was 50,
# which is below the number of conversations a normal user reaches in a week:
# the 51st chat silently DELETED the oldest thread, irreversibly, and the only
# symptom was that an older conversation 404'd - indistinguishable from
# someone deleting it on purpose. Measured: 60 threads created, 10 destroyed
# with no warning anywhere. Real history needs real storage; until the store
# is paginated this ceiling exists only to stop a runaway loop writing GBs.
_CHAT_MAX_THREADS = 500
_CHAT_MAX_TURNS = 30  # user+assistant pairs kept per thread
_CHAT_MAX_QUERY = 4000
# A conversation id arrives from a client and is used as a storage key, so its
# length is bounded. Ids minted here are `ct-` plus 8 hex; clients may use
# their own scheme, but not an unbounded string.
_CONV_ID_MAX = 200
# Streaming pacing: how often to reassure the client that a slow provider
# call is still alive, and how long to wait between answer deltas.
_CHAT_STREAM_HEARTBEAT_S = 10.0
_CHAT_STREAM_PACE_S = 0.012
# One definition of what chat can reach. It used to be a literal duplicated in
# two endpoints while the system prompt spelled out a THIRD, shorter list - so
# the model answered "I have 11 tools" from the prompt and could not schedule
# anything, because `schedule_task` was in none of the three. The prompt is now
# generated from this list, so the two cannot drift apart again.
#
# Scope note: chat may now WRITE - schedule reminders, create and edit
# workspace files, render a document. It may not send mail or messages, create
# calendar events, or touch the user's machine: those are irreversible things
# done to other people, and they belong on the confirmation-gated Research path.
_CHAT_TOOLS = [
    # read the world
    "web_search", "fetch_url", "search_knowledge",
    # read the user's own accounts
    "calendar_query", "gmail_query", "github_query", "slack_history",
    "notion_query",
    # memory
    "recall_preferences", "remember_preference",
    # reminders: list, create, cancel, re-enable — plus channel discovery
    "list_scheduled", "schedule_task", "cancel_scheduled",
    "enable_scheduled",
    "list_channels", "check_channel",
    # workspace files: read and search. WRITING is gone - create/update/edit
    # belonged to "make me a thing", which is now one delegate_authoring call
    # instead of a file the user then has to render by hand.
    "read_file", "list_dir", "search_files",
    # NOTE: render_document, read_artifact, save_as_document, create_file,
    # update_file and edit_file used to be here. They are what made chat heavy:
    # a document request became a chain of chat-side tool calls that could
    # exhaust the hop cap, when the authoring agent already does the whole job
    # and renders a real file. Use delegate_authoring / delegate_research.
    # the behavioural profile is READ-only for chat; see the note on the
    # prompt paragraph below for why recording is not offered here.
    "recall_observations",
    # Gated desktop control. This is ONE tool with an action selector, not the
    # raw daemon surface: sensitive actions land in the approval queue and the
    # host has to opt in with COMPUTER_USE_ENABLED. Chat may ASK for a click;
    # only the user's approval makes it happen.
    "computer_action",
]
# Tools chat deliberately does NOT get, with the reason, so the next person does
# not "fix" the gap by adding them.
#
# NAMES ALONE DO NOT ENFORCE THIS. `github_query` and `notion_query` are
# multi-call tools whose `api_method` enum contains create_issue / create_page /
# append_text — the exact writes `github_create_issue`, `notion_create_page`
# and `notion_append` were listed here to deny. Those three names match no MCP
# tool at all, so the set looked like a working guard while chat could still
# open issues and write to Notion through the wrapper, and the test that checks
# it compares set names only. `_CHAT_WRITE_METHODS` closes the hole; the
# phantom names are kept so the intent is still on the record.
_CHAT_TOOLS_REFUSED = {
    "send_email", "send_telegram", "slack_message", "discord_message",
    "github_create_issue", "notion_create_page", "notion_append",
    "create_calendar_event",
    "delete_file", "index_document",
}
# (tool, api_method) pairs chat must never call, checked where the payload is
# built rather than by name. A refused capability that is reachable through a
# general-purpose wrapper is not refused.
# Chat gets the goal-level computer actions only. Each of these owns its own
# a11y/vision cascade; the low-level primitives (click, press_key, replace_text,
# get_window_state...) are deliberately not offered to chat.
_CHAT_COMPUTER_ACTIONS = frozenset({
    "drive_app", "open_app", "run_command", "read_file", "write_file",
})
_CHAT_WRITE_METHODS: dict[str, frozenset] = {
    "github_query": frozenset({"create_issue"}),
    "notion_query": frozenset({"create_page", "append_text"}),
    # `calendar_query` and `gmail_query` are deliberately absent: neither has
    # an `api_method` selector at all. calendar_query only ever calls
    # calendar/list, and gmail_query's enum is list|read. Adding them here
    # would imply a hole that does not exist.
}
def chat_write_method_allowed(tool: str, api_method: str | None) -> bool:
    """Can chat use this tool with this method? Read methods are always fine.
    This is the layer that actually enforces the refusal. Without it the
    refusal is documentation: the model picks `github_query` with
    `api_method: create_issue` and the write happens.
    """
    blocked = _CHAT_WRITE_METHODS.get(tool)
    if not blocked or not api_method:
        return True
    return str(api_method) not in blocked
_CHAT_SYSTEM = (
    "You are Aria, a concise chat assistant. Answer directly in Markdown. "
    "Keep replies tight unless the user asks for depth. "
    "You have tools (web_search, fetch_url, search_knowledge, "
    "calendar_query, gmail_query, github_query, slack_history, notion_query, "
    "recall_preferences, remember_preference, list_scheduled, "
    "schedule_task, cancel_scheduled, read_file, list_dir, search_files, "
    "enable_scheduled, recall_observations, computer_action, "
    "delegate_research, delegate_authoring) "
    # Parallelism is REAL, and the model kept denying it. Asked "can you call
    # tools in parallel?" it answered "I can only call one tool at a time - the
    # functions run sequentially" - a flat falsehood it invented, because
    # nothing in the prompt said otherwise and silence reads as "no". The
    # runner has always dispatched a batch concurrently (`asyncio.gather`
    # behind a small semaphore), so the claim talked users out of asking for
    # work that costs one round trip.
    "TOOLS RUN IN PARALLEL: when a question needs several independent lookups, "
    "ask for them together in one turn and they will run at the same time. Do "
    "not walk them one at a time, and never tell the user that tools are "
    "limited to one at a time - that is not true. Only chain a call after "
    "another when the second genuinely needs the first one's result. "
    "- use them instead of guessing. The user's OWN accounts are among them: "
    "if they ask what is on "
    "their calendar, what is in their inbox, what reminders exist, what is in "
    "a chat channel or a wiki, or what is in a repository, call the matching "
    "tool. Never answer such a question from general knowledge and never "
    "substitute web_search for an account query. "
    # Reminders, because they were the most visible gap: chat could LIST
    # reminders but had no way to create one, so "remind me at 6" failed.
    "REMINDERS: when the user asks you to remember to do something at a time, "
    "call schedule_task with their words as `query` and the time as `when`. "
    "Then tell them the schedule id. To remove one, call list_scheduled first, "
    "confirm which entry you mean, then cancel_scheduled with its id. "
    # The channel half, which the prompt used to omit entirely: schedule_task
    # accepts `channel`, `lead_s` and `wait_for_ack`, but a model that is only
    # told about query/when will never use them — and it will tell the user it
    # cannot notify them anywhere, which is a lie it did not choose.
    "NOTIFICATIONS: schedule_task can also PUSH a message to a chat channel. "
    "When the user wants to be told somewhere other than this console — "
    "'text me on telegram', 'ping me on slack', 'warn me 10 minutes before' — "
    "call list_channels first and use a channel that reports a paired "
    "destination, then set `channel`, and `lead_s` with `lead_message` for a "
    "pre-warning and `message` for the fire-time text. Set `wait_for_ack` when "
    "the reminder must not be missed: the user is then expected to reply, their "
    "reply comes back to you, and you decide whether to answer it. If a "
    "notification has stopped arriving, call check_channel to see whether the "
    "credential still works. If the channel they asked for reports no "
    "destination, say so plainly and tell them to message the bot once to pair "
    "it — do not promise a delivery you cannot see happening. "
    # Telegram is also a way IN, not only a way out.
    "TELEGRAM AS A CHAT: if the user messages you from Telegram, the reply "
    "goes back to Telegram on the same chat. Treat it as an ordinary chat "
    "turn: their message is the query, tools work as normal, and your reply is "
    "the answer. "
# Hand-offs. This is the "keep chat light" rule in one paragraph: chat
    # does the talking, the specialist agents do the big jobs, and chat
    # reports what came back. Previously chat carried render_document,
    # save_as_document and the file-writing tools itself, which is how "write
    # me a report" turned into a long chain of chat-side tool calls.
    "HANDING OFF BIG JOBS: you do not write or render documents yourself. For "
    "any document, deck, brief, report or spreadsheet AS A FILE, call "
    "delegate_authoring with what they want - it researches it and hands back a "
    "real downloadable file. For anything that needs real reading or "
    "comparison - 'compare these', 'what are the risks', 'what's happening "
    "with X' - call delegate_research, which plans the work and returns a "
    "cited report. Each takes minutes: make ONE call, wait for it, then tell "
    "the user what came back. Never start the same job twice. If a hand-off "
    "reports it is still running, say so and point at Research or Runs rather "
    "than waiting or retrying. Keep web_search for a single quick lookup - it "
    "is fast and does not need a whole run. "
    # Gated desktop access. The tool is in the payload, but a model that is not
    # told what `pending` means will report a click it never made, and one that
    # is not told the host gate exists will offer to "open Notepad" on a build
    # where computer use is off.
    "ON THE USER'S MACHINE: computer_action is gated desktop control - open an "
    "app, run a command, read or write a file, or drive an app towards a goal. "
    "open_app ONLY launches the app; it does not play, pause, click or type. "
    "When the user asks for an action INSIDE an app ('play a song', 'pause', "
    "'next track', 'send this'), call drive_app with that goal in the same turn "
    "- do not open the app and stop there, and do not tell the user to do it "
    "themselves. drive_app ALWAYS needs params.app set to the application name "
    "(for example 'Spotify') beside params.goal; without it the driver has no "
    "safe target. Use it only when the user explicitly asks you to do something "
    "on their computer, not for a question you can answer from knowledge or the "
    "web. "
    "It is OFF unless the host enabled it: if it returns disabled, say so "
    "plainly and do not pretend otherwise. A result of pending means the "
    "action has NOT run - tell the user to approve it in the Computer page and "
    "never claim it happened. A result of dry-run means nothing ran and NO "
    "approval exists: do not tell the user to approve it; say the host is in "
    "dry-run and must be restarted with -ComputerUseMode live. Never claim an "
    "action happened unless the result says done. "
    # When a delegated job comes back. The report is displayed to the user in
    # FULL by the interface, so anything the model writes about it is
    # decoration at best and a competing summary at worst.
    "WHEN A JOB COMES BACK: a finished research report is already shown to the "
    "user IN FULL in the transcript - do not summarise, shorten or repeat it; "
    "one short line that it is ready is enough. A finished document is shown "
    "as a downloadable file. If they then say 'send it to my telegram' or "
    "'save that to my docs', call send_channel_message with the report's name "
    "as `source`, or save_to_documents with the file's name - the server "
    "fetches the full content, so you never paste it yourself. Use "
    "send_channel_message when they want it NOW; schedule_task is for a later "
    "time and is not an immediate send. "
    # The behavioural profile. The rules here are about honesty, not caution:
    # an observation is a pattern drawn from past conversations, and the quote
    # under it is the only reason to believe it.
    "If the user corrects you or says how they want things done - " "always "
    "give me the date, stop with the preamble, shorter - do nothing "
    "the correction with their exact words, and re-reads it back to you. "
    "You have strictly less information than that pass - the same message, "
    "and no way to verify a quote is verbatim - so do not try to record it "
    "yourself: two writers double-count every correction, and a paraphrased "
    "quote overwrites the real one. "
    "You may also be given WHAT YOU HAVE NOTICED about this user: treat each "
    "line as a hypothesis, not a fact, and if they contradict one now, they "
    "win and it is stale. Never quote the profile back at them or tell them "
    "what you have noticed. "
    # Reminders, continued: re-enabling a greyed-out row used to have no tool,
    # so the only way to satisfy "turn that back on" was to schedule a second
    # copy and leave the original dead.
    "If they ask you to re-enable, resume or switch on a reminder that is "
    "off, call list_scheduled, then enable_scheduled with that id. Never "
    "answer it by scheduling the same thing again — that leaves a duplicate "
    "and the original still disabled. "
    # Retrieved documents. The retrieval is done by the server before this
    # prompt is even sent; without saying so, the model treats the block as an
    # unexplained wall of text and tends to ignore it in favour of a confident
    # answer from its own weights.
    # Documents are NOT retrieved up front any more: the pre-injected chunks
    # made every chat turn carry a document search's worth of context whether
    # the question needed it or not. The tool is the access.
    "THE USER'S DOCUMENTS: their uploaded files are searchable with the "
    "search_knowledge tool. Call it when a question could be about their own "
    "material (contracts, notes, reports they uploaded) - and never claim "
    "they have no documents without asking the tool first. If they seem to "
    "expect a specific document you cannot find, mention the Documents page. "
    "For deep research with sources, tell them to use the Research page. "
    # Preference capture. This is the ONLY place a casual chat preference can
    # be observed, and before remember_preference existed a stated preference
    # ("always metric", "no emoji", "terse answers") was simply lost: zero
    # preference records existed across every drawer.
    "STANDING PREFERENCES: when the user states how they want things done — "
    "units, tone, answer length, formatting, tools, defaults — or corrects "
    "you on one, call remember_preference in that same turn. Write it as one "
    "third-person sentence with the concrete specifics a future turn needs "
    "('prefers metric units; never convert to imperial'), and pass keywords "
    "for retrieval. Do it without asking permission and without narrating it; "
    "just answer the question as normal. When they CHANGE a preference, pass "
    "the previous id as supersedes so the old rule is retired. Never store a "
    "fact about the world, a one-off instruction about the current task, or "
    "anything secret this way."
)
# ── chat → specialist agents (handoff tools) ─────────────────────────────────
#
# Chat used to carry every specialist tool itself: render_document, web_search,
# save_as_document and the rest. That is what made "chat" not light — 26 tools
# on every turn, a big schema on every request, and a question that needed real
# work turning into fifteen tool calls that could exhaust the hop cap (the
# "(empty answer)" case) instead of one job done properly.
#
# So chat keeps the tools that are genuinely conversational - the user's own
# accounts, reminders, memory, a quick web lookup - and hands the two BIG jobs
# to the agents that already do them, over the same DAG the Research and
# Authoring pages drive. The result comes back as ordinary tool output, so chat
# shows it in the transcript like any other answer.

_CHAT_HANDOFF_TOOLS: list[dict] = [
    {
        "name": "delegate_research",
        "description": (
            "Hand a research question to the research agent and get its report "
            "back. Use this for anything that needs reading, comparing or "
            "checking more than a couple of sources - market or product "
            "questions, 'compare X and Y', 'what are the risks', anything with "
            "a current-events or factual backbone. It plans the work, researches "
            "each part and writes one cited report. Takes minutes, not seconds: "
            "prefer ONE call over a chain of lookups. For a single fact or a "
            "quick lookup use web_search instead."),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "The question to research, in the user's own words.",
                },
                "depth": {
                    "type": "string",
                    "enum": ["quick", "standard", "deep"],
                    "description": (
                        "How thorough. quick = one lookup, a short paragraph. "
                        "standard = a structured report of a few pages (default). "
                        "deep = many angles, several pages."),
                },
            },
            "required": ["topic"],
        },
    },
    {
        "name": "delegate_authoring",
        "description": (
            "Hand a document request to the authoring agent, which researches "
            "it and renders a real file you can read and download (PDF, "
            "PPTX, DOCX, XLSX). Use this whenever the user asks for a document, "
            "deck, report, brief, spreadsheet or summary AS A FILE - never "
            "paste a document into the chat instead, because pasted text cannot "
            "be downloaded. Takes minutes."),
        "input_schema": {
            "type": "object",
            "properties": {
                "brief": {
                    "type": "string",
                    "description": "What the document should be, in the user's words.",
                },
                "format": {
                    "type": "string",
                    "enum": ["pdf", "pptx", "docx", "xlsx", "md"],
                    "description": "Output format. Default pdf.",
                },
            },
            "required": ["brief"],
        },
    },
]

# Small tools chat runs in-process, next to the hand-off ones and for the same
# reason: they act on what chat ALREADY has (a report in the transcript, a
# rendered artifact) and need no MCP child.
_CHAT_LOCAL_TOOLS: list[dict] = [
    {
        "name": "send_channel_message",
        "description": (
            "Send a message to one of the user's paired channels (telegram, "
            "slack, discord, ...) RIGHT NOW, immediately. Use this for 'send "
            "it to my telegram', 'text me that', 'message me on slack'. For "
            "delivery at a FUTURE time ('remind me at 6pm', 'warn me in 10 "
            "minutes') use schedule_task instead - it is not an immediate "
            "send. To send a research report produced earlier in this "
            "conversation, pass its name as `source`: the whole report is sent "
            "(chunked if long), never a summary of it. To send a DOCUMENT "
            "the authoring agent rendered ('send the pdf to my telegram'), "
            "pass its filename as `file`: the file itself is delivered as an "
            "attachment."),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": ("telegram | slack | discord | whatsapp | "
                                    "signal | email. Default telegram."),
                },
                "message": {
                    "type": "string",
                    "description": ("The text to send, when it is not a stored "
                                    "report. With `file`, it becomes the "
                                    "attachment's caption."),
                },
                "source": {
                    "type": "string",
                    "description": ("Name of a report produced in this "
                                    "conversation (see the [delegated research "
                                    "report \"...\"] note). The whole report is "
                                    "sent, not a summary."),
                },
                "file": {
                    "type": "string",
                    "description": ("Filename of a document rendered in this "
                                    "conversation (see the transcript's file "
                                    "list, e.g. 'evs.pdf'). Sends the file "
                                    "itself as an attachment."),
                },
            },
            "required": [],
        },
    },
    {
        "name": "save_to_documents",
        "description": (
            "Save a document the authoring agent produced in this conversation "
            "into the Documents section, so it is stored, listed and indexed "
            "for later search. Use when the user says 'save that to my docs' "
            "or 'add it to my documents'. Pass the filename as shown in the "
            "transcript."),
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": ("Filename of the document, as shown in the "
                                    "transcript."),
                },
            },
            "required": ["file"],
        },
    },
    {
        "name": "read_report",
        "description": (
            "Read the FULL text of a research report produced earlier in this "
            "conversation. The transcript shows the report to the user, but you "
            "cannot see its body: use this whenever the user asks what the "
            "report says, asks for a summary, or asks a question about its "
            "contents, and base the answer on the text it returns. Never use "
            "it merely to print the report again - the user already has it."),
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": ("Report name (see the [delegated research "
                                    "report \"...\"] note). Omit for the most "
                                    "recent report."),
                },
            },
            "required": [],
        },
    },
]

# How long a chat turn will wait for a delegated job before telling the user it
# is still going. The run itself is NOT cancelled at this point - it finishes in
# the background and shows up in Runs/Research - so this only bounds how long
# the chat spinner sits there.
_CHAT_HANDOFF_WAIT_S = 300.0
# How much of a delegated report is handed back into the chat turn.
_CHAT_HANDOFF_CHARS = 4000


def _research_depth_note(depth: str) -> str:
    """The planner-side run note for a research run: the operator's depth.

    This used to be a whole BRIEF - "You are a research agent. Research this
    topic and deliver a final report as Markdown..." - handed to the DAG as
    the run's USER_QUERY. Every node reads USER_QUERY, so the researcher read
    that sentence as its own task and wrote a Markdown report instead of the
    JSON the pipeline needs; the node then failed with "returned no usable
    JSON" after burning up to 300s, the run degraded, and the recovery planner
    re-emitted the same instruction (observed live, repeatedly: 2 researchers
    at ~300s each, then 3 more ExceptionGroups). The TOPIC is now the run
    query and only the PLANNER - the one node that decides the fleet size - is
    told the depth.
    """
    spec = {
        "quick": ("QUICK - a single lookup. Plan EXACTLY ONE researcher node "
                  "(one facet) plus one formatter; the answer stays a short "
                  "paragraph and its sources, under ~400 words."),
        "standard": ("STANDARD - a structured report. Plan AT MOST 3 researcher "
                     "nodes (one per facet) plus one formatter: an Executive "
                     "Summary, Key Findings and Sources, roughly 1,500-2,500 "
                     "words."),
        "deep": ("DEEP - plan up to 6 researcher nodes (one per facet) plus one "
                 "formatter, each told to search exhaustively. Cover competing "
                 "views, risks and unknowns; several pages is correct here."),
    }.get(str(depth or "").lower(),
          "STANDARD - a structured report: Executive Summary, Key Findings, "
          "Sources. Plan AT MOST 3 researcher nodes plus one formatter.")
    return (
        "RUN PARAMETERS - the operator's settings for this run. These are NOT "
        "part of the user's question; do not answer them and do not pass the "
        "wording on to downstream nodes.\n"
        f"RESEARCH DEPTH (a hard budget, not a hint): {spec}\n"
        "OUTPUT: the formatter writes the final report as text in the "
        "transcript. Do NOT plan an author or deck node and do NOT render a "
        "document - a Research run never produces a file.")


def _authoring_brief(brief: str, fmt: str) -> str:
    """The run query for a delegated document.

    The user's words come FIRST, as their own paragraph, and the agent
    instruction is a second paragraph. `_document_title` falls back to the
    query when the model supplies no title, and takes the first paragraph as
    the subject - so this order is what keeps the filename from becoming the
    instruction. Observed live before the split: a chat-delegated brief named
    its own PDF `You-are-the-authoring-agent.-Produce-a-finished-document-and-
    render-it-as-a-real.pdf`.
    """
    return (
        f"Create a document. {brief}\n\n"
        f"(Delegated from chat. You are the authoring agent: produce a finished "
        f"document and render it as a real {fmt.upper()} file with "
        "render_document, so the user gets something they can open and "
        "download. Research what you need first, then write the document as "
        "blocks and render it. Report the artifact handle in your final answer "
        "so the user can download it.)")


async def _run_handoff(brief: str, conversation_id: str | None,
                       idem_key: str, run_note: str = "",
                       run_kind: str = "") -> dict:
    """Run one delegated job to completion over the SAME DAG the Research and
    Authoring pages drive, and hand the result back as tool output.

    Consuming the SSE response rather than re-implementing the run keeps one
    engine: the same planner, the same cost ledger, the same produced-files
    record. The wait is bounded - a run that outlives it is left to finish in
    the background (it shows up in Runs and in the tool's own output), because
    killing it would waste tokens the user already spent.
    """
    import asyncio as _aio
    answer = ""
    files: list[dict] = []
    frames: list[dict] = []

    async def _collect() -> None:
        nonlocal answer, files
        resp = _stream_run(brief, conversation_id, None, idem_key,
                           run_note=run_note, run_kind=run_kind)
        async for raw in resp.body_iterator:
            chunk = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            for line in chunk.split("\n"):
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                try:
                    fr = json.loads(line[5:].strip())
                except Exception:
                    continue
                frames.append(fr)
                if fr.get("type") == "done":
                    answer = fr.get("answer") or answer
                    files = fr.get("files") or files

    try:
        await _aio.wait_for(_collect(), timeout=_CHAT_HANDOFF_WAIT_S)
    except _aio.TimeoutError:
        return {
            "ok": False,
            "status": "still_running",
            "message": (
                "The job is still running - it was not cancelled and it keeps "
                "going in the background. Tell the user it is under way and "
                "that they can watch it, or read the result, in Research and "
                "Runs. Do not start it a second time."),
        }
    except Exception as exc:  # noqa: BLE001 - reported to the model as tool text
        return {"ok": False, "status": "failed",
                "message": f"{type(exc).__name__}: {exc}"}

    if not answer.strip():
        return {
            "ok": False,
            "status": "no_answer",
            "message": ("The job finished but produced no report. Say so "
                        "plainly - do not invent one."),
        }
    # Cap what goes back into the CHAT context. A full research report is
    # thousands of words; feeding it back verbatim alongside the rest of the
    # turn pushed the follow-up request past what the gateway would serve, and
    # the whole turn failed with a bare "502 Bad Gateway" and no answer at all.
    # The chat model only needs enough to describe the result to the user - the
    # full report is one click away in Research/Runs, and for authoring it is a
    # downloadable file.
    _MAX = _CHAT_HANDOFF_CHARS
    trimmed = answer.strip()
    if len(trimmed) > _MAX:
        trimmed = (trimmed[:_MAX].rsplit(" ", 1)[0] +
                   "\n\n[the report continues - the full version is in "
                   "Research/Runs]")
    names = [f.get("filename") for f in (files or [])
             if isinstance(f, dict) and f.get("filename")]
    # Chat has no sidecar of its own; the download button comes from the `done`
    # frame, not from the model's prose. Left to itself the model wrote
    # "available for download: [report.pdf](sandbox:/mnt/data/report.pdf)" - a
    # path that does not exist on this machine and a dead link in the console.
    # Name the file and forbid the invention.
    say = ("Tell the user the job is finished and name the file"
           + ("s: " + ", ".join(names) if names else "")
           + ". Do NOT write any URL, link or file path - the interface shows "
             "the file itself for download.")
    return {"ok": True, "status": "done", "answer": trimmed, "files": files,
            "say": say,
            "truncated": len(answer.strip()) > _MAX,
            # The full report, for the CALLER (the chat endpoint shows it to
            # the user verbatim; the trimmed `answer` is what the model reads).
            # Stripped before the tool output is serialised, so the model never
            # sees the whole thing - feeding it back verbatim was what caused
            # the model to summarise, and what blew the request size.
            "answer_full": answer.strip(),
            "nodes": len([f for f in frames if f.get("type") == "node_done"]) or None}


# ── what a delegated run left behind, for the turns that follow ──────────────
# A chat thread keeps its full transcript, and a research report is the single
# biggest thing in it. Replaying that report into the model on every later turn
# is the waste the user asked to stop: "send it to my telegram" needs the
# report's NAME, not its 20,000 characters. The model is given the name plus a
# description (its opening lines); when it actually needs the body - to send or
# to save - the tool resolves it from the thread, server-side.

_CHAT_REPORT_STUB_CHARS = 240


def _chat_thread_messages(conversation_id: str | None) -> list[dict]:
    """The stored messages of a chat thread; [] for anything unusable."""
    if not conversation_id or not _plain_id_ok(conversation_id):
        return []
    try:
        with _CHAT_THREADS_LOCK:
            t = _chat_threads_load().get(conversation_id)
        return (t.get("messages") or []) if isinstance(t, dict) else []
    except Exception:
        return []


def _chat_history_for_model(history: list[dict]) -> list[dict]:
    """The history a chat model gets: delegated results as name + description.

    A report produced by the research agent is shown to the user IN FULL in the
    transcript. Repeating it in the model's context would (a) double the
    request size on every later turn and (b) invite the model to re-summarise
    the very text the user asked to see unsummarised. So the body is replaced
    by its title, opening lines, and a pointer that `send_channel_message` /
    `save_to_documents` can fetch the whole thing by name. Rendered files are
    listed the same way: filename + handle, never bytes.
    """
    out: list[dict] = []
    for m in history or []:
        if not isinstance(m, dict):
            continue
        # A pending row is this run's own placeholder; an error/stopped row
        # is UI state, not something the model said. Feeding either back
        # would make the next turn answer a message that never existed.
        if m.get("pending") or m.get("kind") in ("error", "stopped"):
            continue
        role = m.get("role") or "user"
        content = str(m.get("content") or "")
        notes: list[str] = []
        if m.get("kind") == "report":
            title = str(m.get("title") or "research report")
            opening = " ".join(content.split())
            if len(opening) > _CHAT_REPORT_STUB_CHARS:
                opening = opening[:_CHAT_REPORT_STUB_CHARS].rstrip() + "…"
            notes.append(
                f'[delegated research report "{title}" ({len(content)} chars). '
                f"The report is displayed to the user IN FULL in the "
                f"transcript - never rewrite, shorten or repeat it. It opens: "
                f"{opening} To answer a question about it or summarise it, "
                f'call read_report with source="{title}" - you cannot see the '
                f"body otherwise. To send or save the whole report, call "
                f"send_channel_message or save_to_documents with the same "
                f"source.]")
            content = ""
        for f in (m.get("files") or []):
            if isinstance(f, dict) and f.get("filename"):
                notes.append(f'[file "{f["filename"]}" '
                             f'(handle {f.get("artifact") or "?"})]')
        if notes:
            joined = "\n".join(notes)
            content = f"{content}\n\n{joined}".strip() if content.strip() else joined
        out.append({"role": role, "content": content})
    return out


def _chat_resolve_report(conversation_id: str | None,
                         source: str = "") -> tuple[str, str]:
    """(title, full_text) of a report produced in this thread.

    An empty source - or "it" / "that" / "the report" - means the newest one,
    because that is what a follow-up like "send it to my telegram" refers to.
    Anything else matches the title case-insensitively as a substring.
    """
    want = " ".join(str(source or "").split()).lower()
    if want in ("", "it", "that", "this", "the report", "report", "last",
                "latest", "last report"):
        want = ""
    for m in reversed(_chat_thread_messages(conversation_id)):
        if not isinstance(m, dict) or m.get("kind") != "report":
            continue
        title = str(m.get("title") or "research report")
        text = str(m.get("content") or "")
        if not want or want in title.lower():
            return title, text
    return "", ""


def _chat_read_report_tool(args: dict,
                           conversation_id: str | None) -> str:
    """`read_report`: the full text of a report in this thread, on demand.

    The history stub gives the model the title and 240 characters, so a
    follow-up like "can you summarise it" had nothing to summarise: live, it
    answered "the report is already available above" four times in a row
    (thread telegram-069246b4b2a2). Reading on demand keeps the fixed prompt
    small - the body is only paid for on the turn that actually needs it.
    """
    title, text = _chat_resolve_report(
        conversation_id, str((args or {}).get("source") or ""))
    if not text:
        return json.dumps({"ok": False, "message": (
            "no research report in this conversation matches that name.")})
    return json.dumps({"ok": True, "title": title, "chars": len(text),
                       "text": text})


def _chat_resolve_file(conversation_id: str | None,
                       name: str = "") -> dict | None:
    """A rendered file from this thread: exact handle, exact filename, or a
    substring of a filename ("the PDF"). Newest match wins; no name, no file -
    guessing which document the user meant is how the wrong one gets saved."""
    want = " ".join(str(name or "").split()).lower()
    if not want:
        return None
    for m in reversed(_chat_thread_messages(conversation_id)):
        if not isinstance(m, dict):
            continue
        for f in (m.get("files") or []):
            if not isinstance(f, dict):
                continue
            fn = str(f.get("filename") or "").lower()
            art = str(f.get("artifact") or "").lower()
            if want in (art, fn) or (len(want) >= 3 and want in fn):
                return f
    return None


_CHAT_CHANNEL_CHUNK = 3900   # Telegram's hard limit is 4096.
_CHAT_CHANNELS = ("telegram", "slack", "discord", "whatsapp", "signal", "email")


def _channel_chunks(text: str) -> list[str]:
    """Split a long message on paragraph/sentence boundaries.

    A 20k-char report sent to Telegram used to be truncated to 4096 by the
    adaptor's own slice; the user asked for the report, not its first page.
    Chunking keeps every character, and the boundary search keeps the cuts
    from landing mid-sentence."""
    t = (text or "").strip()
    out: list[str] = []
    while len(t) > _CHAT_CHANNEL_CHUNK:
        cut = -1
        for sep in ("\n\n", "\n", ". ", " "):
            pos = t.rfind(sep, 0, _CHAT_CHANNEL_CHUNK)
            if pos >= _CHAT_CHANNEL_CHUNK // 2:
                cut = pos + (1 if sep == ". " else 0)
                break
        if cut <= 0:
            cut = _CHAT_CHANNEL_CHUNK
        out.append(t[:cut].rstrip())
        t = t[cut:].lstrip()
    if t:
        out.append(t)
    return out or [""]


async def _chat_send_channel_tool(args: dict,
                                  conversation_id: str | None) -> str:
    """`send_channel_message`: deliver to a paired channel NOW.

    The one-minute wait the user saw was not a transport limit: the model had
    only `schedule_task`, whose relative forms are minute-grained in practice,
    so "now" became "in 1 minute". This tool is the missing immediate path. Its
    destination is resolved from the same pairing the reminders use, and a
    long `source` report is chunked, not truncated. A rendered document
    (`file`) is uploaded as an attachment: text-only delivery is how
    "send the pdf to my telegram" ended in an apology.
    """
    import asyncio as _aio
    channel = str(args.get("channel") or "telegram").strip().lower()
    if channel not in _CHAT_CHANNELS:
        return json.dumps({"ok": False, "message": (
            f"unknown channel {channel!r}; available: "
            f"{', '.join(_CHAT_CHANNELS)}.")})
    source = str(args.get("source") or "").strip()
    message = str(args.get("message") or "").strip()
    file_ref = str(args.get("file") or args.get("filename") or "").strip()
    if file_ref:
        f = _chat_resolve_file(conversation_id, file_ref)
        if not f:
            return json.dumps({"ok": False, "message": (
                f'no rendered file matching "{file_ref}" in this conversation '
                "- use the exact filename shown in the transcript.")})
        art = str(f.get("artifact") or "")
        filename = str(f.get("filename") or "document")
        try:
            import artifacts as _arts
            blob = _arts.get_bytes(art)
        except Exception as e:
            return json.dumps({"ok": False, "message":
                               f"could not read the artifact {art}: {e}"})
        to = await _aio.to_thread(resolve_notify_chat, channel)
        if not to:
            return json.dumps({"ok": False, "message": (
                f"no {channel} destination is paired - tell the user to send "
                f"the bot one message from {channel}, then try again.")})
        import base64 as _b64
        caption = message[:1000]
        item = {"data_b64": _b64.b64encode(blob).decode("ascii"),
                "filename": filename, "caption": caption}
        payload = None
        try:
            import httpx
            body = {"to": to, "text": caption, "media": [item],
                    "agent": "chat"}
            async with httpx.AsyncClient(timeout=_GW_UPLOAD_TIMEOUT,
                                         headers=_gw_auth_headers()) as c:
                r = await c.post(f"{_GW_NOTIFY}/v1/channels/{channel}/send",
                                 json=body)
                if r.status_code == 401:
                    c.headers.update(_gw_auth_headers())
                    r = await c.post(
                        f"{_GW_NOTIFY}/v1/channels/{channel}/send", json=body)
                try:
                    payload = r.json()
                except Exception:
                    payload = None
                if r.status_code >= 400:
                    return json.dumps({"ok": False, "message": (
                        f"{channel} refused the file (HTTP "
                        f"{r.status_code}): {r.text[:200]}")})
                if isinstance(payload, dict) and payload.get("status") == "pending":
                    return json.dumps({"ok": False, "message": (
                        "the file is waiting for approval (policy) - tell "
                        "the user an approval is pending in the console.")})
        except Exception as e:
            return json.dumps({"ok": False, "message":
                               f"{channel} send failed: {type(e).__name__}: {e}"})
        warn = payload.get("warning") if isinstance(payload, dict) else None
        return json.dumps({
            "ok": True, "channel": channel, "file": filename,
            "bytes": len(blob),
            "message": (f"sent '{filename}' ({len(blob)} bytes) to {channel} "
                        "right now"
                        + (f" - {warn}" if warn else "."))})
    if source:
        title, text = _chat_resolve_report(conversation_id, source)
        if not text:
            return json.dumps({"ok": False, "message": (
                f'no report matching "{source}" was produced in this '
                "conversation - name one that appears in the transcript, or "
                "pass the text as `message`.")})
        message = text
    if not message:
        return json.dumps({"ok": False, "message":
                           "nothing to send: pass `message`, or `source` for a "
                           "report from this conversation."})
    to = await _aio.to_thread(resolve_notify_chat, channel)
    if not to:
        return json.dumps({"ok": False, "message": (
            f"no {channel} destination is paired - tell the user to send the "
            f"bot one message from {channel}, then try again.")})
    chunks = _channel_chunks(message)
    try:
        import httpx
        payload = None
        async with httpx.AsyncClient(timeout=20.0,
                                     headers=_gw_auth_headers()) as c:
            for piece in chunks:
                r = await c.post(f"{_GW_NOTIFY}/v1/channels/{channel}/send",
                                 json={"to": to, "text": piece, "agent": "chat"})
                if r.status_code == 401:
                    c.headers.update(_gw_auth_headers())
                    r = await c.post(f"{_GW_NOTIFY}/v1/channels/{channel}/send",
                                     json={"to": to, "text": piece,
                                           "agent": "chat"})
                if r.status_code >= 400:
                    return json.dumps({"ok": False, "message": (
                        f"{channel} refused the message (HTTP "
                        f"{r.status_code}): {r.text[:200]}")})
                try:
                    payload = r.json()
                except Exception:
                    payload = None
                if isinstance(payload, dict) and payload.get("status") == "pending":
                    return json.dumps({"ok": False, "message": (
                        "the message is waiting for approval (policy) - tell "
                        "the user an approval is pending in the console.")})
    except Exception as e:
        return json.dumps({"ok": False, "message":
                           f"{channel} send failed: {type(e).__name__}: {e}"})
    return json.dumps({
        "ok": True, "channel": channel, "sent": len(chunks),
        "chars": len(message),
        "message": (f"sent to {channel} right now"
                    + (f" in {len(chunks)} messages" if len(chunks) > 1 else "")
                    + f" ({len(message)} chars).")})


async def _chat_save_documents_tool(args: dict,
                                    conversation_id: str | None) -> str:
    """`save_to_documents`: file a rendered document into the Documents list.

    Copies the bytes from the artifact store into the gateway's document
    pipeline (parse, chunk, index) - the same route an upload takes, so the
    saved copy is searchable in Memory like any other document."""
    name = str(args.get("file") or args.get("filename") or "").strip()
    f = _chat_resolve_file(conversation_id, name)
    if not f:
        return json.dumps({"ok": False, "message": (
            f'no rendered file matching "{name}" in this conversation - use '
            "the exact filename shown in the transcript.")})
    art = str(f.get("artifact") or "")
    filename = str(f.get("filename") or "document")
    try:
        import artifacts as _arts
        meta = _arts.get_meta(art)
        blob = _arts.get_bytes(art)
        ctype = getattr(meta, "content_type", "") or "application/octet-stream"
    except Exception as e:
        return json.dumps({"ok": False, "message":
                           f"could not read the artifact {art}: {e}"})
    try:
        import httpx
        async with httpx.AsyncClient(timeout=_GW_UPLOAD_TIMEOUT,
                                     headers=_gw_auth_headers()) as c:
            form = {"file": (filename, blob, ctype)}
            r = await c.post(f"{_GW_VOICE}/v1/documents", files=form)
            if r.status_code == 401:
                c.headers.update(_gw_auth_headers())
                r = await c.post(f"{_GW_VOICE}/v1/documents", files=form)
        try:
            data = r.json()
        except Exception:
            data = {}
        if r.status_code >= 400:
            err = (data or {}).get("error") or r.text[:200]
            return json.dumps({"ok": False, "message":
                               f"the Documents store refused it: {err}"})
        doc = (data or {}).get("document") or {}
        return json.dumps({
            "ok": True, "filename": filename, "doc_id": doc.get("id") or "",
            "message": (f"'{filename}' was added to the Documents section and "
                        f"is being indexed.")})
    except Exception as e:
        return json.dumps({"ok": False, "message":
                           f"the Documents store is unreachable: "
                           f"{type(e).__name__}: {e}"})


def _chat_handoff_dispatch(conversation_id: str | None,
                           files_sink: list | None = None,
                           report_sink: dict | None = None):
    """The dispatcher the chat tool loop uses for chat's local tools.

    Handles the two BIG hand-off tools (`delegate_research`,
    `delegate_authoring`) plus the two small ones chat needs to act on what a
    delegated run produced (`send_channel_message`, `save_to_documents`), and
    `computer_action` - which must run in THIS process so an approval it mints
    is the same one `/api/computer/approvals` lists for the console.
    Returns None for every other name, which leaves it on the unchanged MCP
    path.

    `files_sink`, when given, collects the artifacts a delegated run rendered.
    The chat turn's `done` frame reports them, so a document handed off from
    chat is downloadable from the transcript even when the model never writes
    its `art:` handle - which it usually does not.

    `report_sink`, when given, receives the FULL text of a delegated research
    report ({"topic", "text"}). The chat endpoint shows the report to the user
    verbatim - not the model's paraphrase of it - so what the user reads is
    what the research agent wrote. The model itself only ever sees the trimmed
    tool output.
    """
    import asyncio as _aio

    async def _dispatch(name: str, args: dict):
        if name not in ("delegate_research", "delegate_authoring",
                        "send_channel_message", "save_to_documents",
                        "read_report", "computer_action"):
            return None
        try:
            if name == "computer_action":
                # Run in the agent process, not the MCP child: SafetyGates
                # keeps approvals in memory, and the child's copy is invisible
                # to the Computer page (and to the retry after approval).
                from computer_use.tool import tool_dispatch
                a = args if isinstance(args, dict) else {}
                _action = str(a.get("action") or "")
                _params = a.get("params") or {}
                if (_action == "drive_app"
                        and not str(_params.get("app") or "").strip()):
                    # Without an app the driver takes the FOREGROUND window.
                    # Observed live: "play a song" clicked a Chrome page while
                    # the model reported it had played Spotify. Refuse rather
                    # than act on a window the user did not name.
                    return json.dumps({
                        "ok": False, "status": "bad_request",
                        "message": ("drive_app needs params.app (the application "
                                    "name) as well as params.goal; nothing ran."),
                    })
                return json.dumps(
                    tool_dispatch(_action, _params),
                    default=str)
            if name == "read_report":
                return _chat_read_report_tool(args or {}, conversation_id)
            if name == "send_channel_message":
                return await _chat_send_channel_tool(
                    args or {}, conversation_id)
            if name == "save_to_documents":
                return await _chat_save_documents_tool(
                    args or {}, conversation_id)
            return await _dispatch_handoff(name, args)
        except BaseException as exc:  # noqa: BLE001
            # A delegated run must NEVER be able to fail the chat turn it was
            # called from. The inner DAG's workers raise ExceptionGroup (a
            # TaskGroup failure) when a node dies, and that used to unwind
            # through the tool loop into the chat turn, which the console then
            # showed as "chat failed: 502 Bad Gateway" with the user's own
            # message gone from the transcript. The model is told what happened
            # and can answer in its own words instead.
            return json.dumps({
                "ok": False, "status": "failed",
                "message": (f"the job could not be completed "
                            f"({type(exc).__name__}). Tell the user it did not "
                            f"finish and offer to run it from Research or Runs, "
                            f"where its progress is visible. Do not retry it "
                            f"here and do not invent a result."),
            })[:4000]

    async def _dispatch_handoff(name: str, args: dict) -> str:
        a = args or {}
        topic = str(a.get("topic") or a.get("brief") or "").strip()
        if not topic:
            return json.dumps({"ok": False, "status": "bad_request",
                               "message": "say what to research or write."})
        # The delegated run is its own conversation, so a second delegation in
        # the same chat does not resume the first one's graph.
        run_cid = f"handoff-{name}-{int(_time.time())}"
        if name == "delegate_research":
            depth = str(a.get("depth") or "standard").lower()
            if depth not in ("quick", "standard", "deep"):
                depth = "standard"
            # The TOPIC is the run query; the depth rides a planner-only note.
            # Feeds the brief a whole multi-role instruction used to, which is
            # what made every researcher try to write the final report.
            _res = await _run_handoff(
                topic, run_cid, f"handoff:{name}:{_req_digest(topic)}",
                run_note=_research_depth_note(depth),
                run_kind="research")
            if files_sink is not None and isinstance(_res, dict):
                files_sink.extend(_res.get("files") or [])
            if isinstance(_res, dict):
                # The full report goes to the CALLER, never to the model: the
                # trimmed `answer` is enough for it to say what came back, and
                # a model handed the whole report summarises it - which is the
                # exact behaviour this feature exists to stop.
                _full = _res.pop("answer_full", "") or ""
                if report_sink is not None and _res.get("ok") and _full:
                    report_sink["topic"] = topic
                    report_sink["text"] = _full
            return json.dumps(_res, default=str)[:20000]
        fmt = str(a.get("format") or "pdf").lower()
        if fmt not in ("pdf", "pptx", "docx", "xlsx", "md"):
            fmt = "pdf"
        brief = _authoring_brief(topic, fmt)
        result = await _run_handoff(
            brief, run_cid, f"handoff:{name}:{_req_digest(topic)}",
            run_kind="authoring")
        if files_sink is not None and isinstance(result, dict):
            files_sink.extend(result.get("files") or [])
        if isinstance(result, dict):
            result.pop("answer_full", None)
        return json.dumps(result, default=str)[:20000]

    return _dispatch


def _chat_empty_answer(reply: dict | None) -> str:
    """Say WHY a turn produced no text, instead of the literal "(empty answer)".

    The tool loop hands back an empty `text` when it stopped on a hop cap or a
    stall with only tool calls pending - real cases (a question that makes the
    model call fifteen tools runs out of steps). The old fallback printed
    "(empty answer)" and charged the user for it: a dead phrase with no cause
    and no next step. Say what stopped it, and what the user can do next.
    """
    r = reply or {}
    why = str(r.get("error") or r.get("note") or "").strip()
    if why:
        return ("I stopped before I could write an answer: " + why +
                ". Nothing you asked for was lost - try asking for one thing at "
                "a time, or run it from Research where a long job can take as "
                "long as it needs.")
    return ("I gathered what I could but ran out of steps before writing an "
            "answer. Try asking for one thing at a time, or run it from "
            "Research for a full job.")


def _chat_system_now() -> str:
    """_CHAT_SYSTEM plus the one thing it cannot state as a constant: today.
    The prompt is a module-level string, so it can never carry a date. That
    mattered little until it mattered completely: "remind me tomorrow at 9am"
    was answered with `2025-10-12T09:00:00` because nothing told the model the
    year. A model has no clock - its sense of "now" is its training data, so
    absent a date it guesses, and a reminder is exactly where a stale guess
    is expensive: the row looks scheduled and never fires.
    Resolved per call rather than at import, so a long-running process does not
    freeze the date at whatever day it booted on. Local time with an explicit
    UTC offset, because `when` values are resolved against the same clock and a
    date with no zone is how "tomorrow 9am" drifts by a day.
    """
    now = _dt.datetime.now().astimezone()
    stamp = now.strftime("%A %d %B %Y, %H:%M %Z (UTC%z)").strip()
    return (
        _CHAT_SYSTEM
        + f"\n\nTODAY IS {stamp}. Any date the user gives as 'today', "
          "'tomorrow', 'tonight' or 'next week' is relative to that, not to "
          "your training data. When you compute a date yourself, count the "
          "days forward from it and write the result as an ISO datetime "
          "including the correct year. Never guess the year."
    )
def _capture_preference_bg(text: str, conversation_id: str | None) -> None:
    """Store a stated user preference, off the request path.
    Measured problem this solves: a natural "from now on always give me the
    publication date" got a correct acknowledgement and stored NOTHING,
    because the model chose not to call the remember_preference tool it had.
    Deterministic capture does not depend on that choice.
    Background thread because it costs one gateway round-trip (embed +
    persist) and a chat turn must never wait on it. Quiet on failure: a miss
    is normal, and a memory outage must not surface as a chat error.
    """
    try:
        if not text or len(text) > 400:
            return
        import memory as _mem
        if not _mem.detect_preference(text):
            return
        threading.Thread(target=_mem.capture_preference_from_turn,
                         args=(text,), kwargs={"source": "chat",
                                               "run_id": "chat",
                                               "session_id": conversation_id},
                         daemon=True).start()
    except Exception:
        pass
def _capture_observation_bg(text: str, conversation_id: str | None) -> None:
    """Extract durable behavioural observations from a chat turn, off the path.
    Gated on `memory.observations`, which is OFF by default: this is the only
    path that retains conversation text the user did not ask to be retained,
    so it must be turned on deliberately rather than discovered later.
    Background thread and quiet on failure, for the same reasons as
    `_capture_preference_bg`: one extractor call plus a write must not add
    latency to a reply, and a memory outage must never become a chat error.
    """
    try:
        import memory as _mem
        if not _mem.observations_enabled():
            return
        if not text or len(text) > 4000:
            return
        threading.Thread(target=_mem.extract_observations,
                         args=(text,), kwargs={"session_id": conversation_id,
                                               "source": "chat"},
                         daemon=True).start()
    except Exception:
        pass
def _chat_threads_load() -> dict:
    return _read_json(_CHAT_THREADS_PATH, dict)
def _chat_threads_save(data: dict) -> None:
    try:
        _write_json_atomic(_CHAT_THREADS_PATH, data)
    except Exception:
        pass


# Server-owned lightweight runs and their lightweight/ DAG cancellation.
# The old stream did the model call inside the SSE generator, so a refresh
# or navigation closed the request and cancelled the turn before it could
# persist an answer. Runs now live in this registry, independent of whoever
# is watching; /api/chat/cancel is the only thing that stops one.
_CHAT_RUNS: dict[str, dict] = {}
_CHAT_RUNS_LOCK = threading.Lock()
_CHAT_PENDING_GRACE_S = 5.0


def _chat_run_register(conversation_id: str, task, turn_id: str) -> None:
    with _CHAT_RUNS_LOCK:
        _CHAT_RUNS[conversation_id] = {"task": task, "turn_id": turn_id}


def _chat_run_unregister(conversation_id: str, task) -> None:
    with _CHAT_RUNS_LOCK:
        rec = _CHAT_RUNS.get(conversation_id)
        if rec is not None and rec.get("task") is task:
            _CHAT_RUNS.pop(conversation_id, None)


def _chat_conversation_active(conversation_id: str) -> bool:
    """True while either engine still owns work for this conversation."""
    if not conversation_id:
        return False
    with _CHAT_RUNS_LOCK:
        rec = _CHAT_RUNS.get(conversation_id)
        if rec is not None:
            task = rec.get("task")
            if task is not None and not task.done():
                return True
    # DAG runs register their threading.Event under the conversation id.
    # Defined later in the module; read at call time, after startup.
    with _ACTIVE_RUNS_LOCK:
        return bool(_ACTIVE_RUNS.get(conversation_id))


def _chat_thread_evict(data: dict) -> None:
    if len(data) <= _CHAT_MAX_THREADS:
        return
    for _cid in sorted(data, key=lambda c: data[c].get("updated", 0)
                       )[:len(data) - _CHAT_MAX_THREADS]:
        print(f"[chat.threads] EVICTED {_cid} - over the "
              f"{_CHAT_MAX_THREADS}-thread safety valve")
        del data[_cid]


def _chat_pending_append(conversation_id: str, query: str,
                         mode: str) -> str:
    """Persist the question and an in-flight answer placeholder.

    Written before the run starts, so a reload during generation shows the
    question and a working row instead of a thread that looks empty. The
    placeholder is replaced by `_chat_pending_finish`; a stale one is
    reconciled on read if no run owns the conversation.
    """
    import uuid as _uuid
    turn_id = f"turn-{_uuid.uuid4().hex[:12]}"
    started = _time.time()
    with _CHAT_THREADS_LOCK:
        data = _chat_threads_load()
        thread = data.get(conversation_id)
        if not isinstance(thread, dict):
            thread = {"title": query[:60], "updated": 0, "messages": []}
        hist = thread.get("messages") or []
        thread["messages"] = (hist + [
            {"role": "user", "content": query, "turn_id": turn_id},
            {"role": "assistant", "content": "", "pending": True,
             "turn_id": turn_id, "started": started},
        ])[-(_CHAT_MAX_TURNS * 2):]
        thread["updated"] = started
        thread["mode"] = mode
        if not thread.get("title"):
            thread["title"] = query[:60]
        data[conversation_id] = thread
        _chat_thread_evict(data)
        _chat_threads_save(data)
    return turn_id


def _chat_pending_finish(conversation_id: str, turn_id: str, *,
                         answer: str = "", error: str = "",
                         stopped: bool = False,
                         files: list | None = None,
                         kind: str = "", title: str = "") -> None:
    """Replace the pending placeholder with the settled assistant message."""
    with _CHAT_THREADS_LOCK:
        data = _chat_threads_load()
        thread = data.get(conversation_id)
        if not isinstance(thread, dict):
            return
        msgs = thread.get("messages") or []
        if stopped:
            msg = {"role": "assistant",
                   "content": (answer or "").strip() or "Stopped.",
                   "kind": "stopped"}
        elif error:
            _partial = (answer or "").strip()
            msg = {"role": "assistant",
                   "content": ((_partial + "\n\n") if _partial else "")
                              + f"The run failed: {error}",
                   "kind": "error"}
        else:
            msg = {"role": "assistant", "content": answer}
            if kind:
                msg["kind"] = kind
            if title:
                msg["title"] = title
            if files:
                msg["files"] = files
        msg["turn_id"] = turn_id
        replaced = False
        for i, m in enumerate(msgs):
            if (isinstance(m, dict) and m.get("turn_id") == turn_id
                    and m.get("role") == "assistant"):
                msgs[i] = msg
                replaced = True
                break
        if not replaced:
            msgs.append(msg)
        thread["messages"] = msgs[-(_CHAT_MAX_TURNS * 2):]
        thread["updated"] = _time.time()
        data[conversation_id] = thread
        _chat_thread_evict(data)
        _chat_threads_save(data)


def _chat_reconcile_pending(conversation_id: str, thread: dict) -> bool:
    """Mark a pending row interrupted only when no run owns it.

    A server restart leaves the placeholder in the thread store. Without
    this, the Chat view would poll forever for an answer that can never
    arrive, so say what happened and let the user send it again.
    """
    if _chat_conversation_active(conversation_id):
        return False
    msgs = thread.get("messages") or []
    changed = False
    now = _time.time()
    for m in msgs:
        if not isinstance(m, dict) or not m.get("pending"):
            continue
        started = float(m.get("started") or 0)
        if started and now - started < _CHAT_PENDING_GRACE_S:
            continue
        m["content"] = ("This run was interrupted before it finished. "
                        "Send the message again.")
        m["kind"] = "error"
        m.pop("pending", None)
        changed = True
    if changed:
        thread["updated"] = now
        with _CHAT_THREADS_LOCK:
            data = _chat_threads_load()
            data[conversation_id] = thread
            _chat_threads_save(data)
    return changed
_ART_HANDLE = re.compile(r"art:[0-9a-fA-F]{16}")
def _chat_produced_files(conversation_id: str, answer: str) -> list[dict]:
    """Artifacts the model rendered during a chat turn.
    The DAG path reports its files from a `produced_files.json` sidecar. Chat
    has no such sidecar, so the `done` frame carried no file list at all and
    the Chat view was left scraping `art:` handles out of the answer prose —
    which only works if the model volunteers the handle in its text, and can
    never render a chart, because a chart lives inside the document rather
    than in the reply.
    So the handle is read back out of the answer (the same source of truth the
    view was using), and every id is resolved against the artifact store before
    it is reported: an unresolvable handle becomes a download button that fails
    with an opaque 400, which is worse than showing nothing.
    """
    if not conversation_id or not _plain_id_ok(conversation_id):
        return []
    handles = []
    for h in _ART_HANDLE.findall(answer or ""):
        if h not in handles:
            handles.append(h)
    if not handles:
        return []
    out = []
    try:
        import artifacts as _arts
    except Exception:
        return []
    for art in handles[:20]:
        try:
            meta = _arts.get_meta(art)
        except Exception:
            continue
        if not meta:
            continue
        descriptor = getattr(meta, "descriptor", "") or ""
        content_type = getattr(meta, "content_type", "") or ""
        out.append({
            "artifact": art,
            "filename": descriptor or "document",
            "format": content_type.split("/")[-1] if content_type else "",
            "skill": "chat",
        })
    return out
@app.get("/api/chat/threads")
async def list_chat_threads(limit: int = 50, offset: int = 0,
                            mode: str = ""):
    """Lightweight chat threads, newest first. No DAG sessions involved.
    Paginated: `offset` skips the newest `offset` threads (the
    client appends pages as the user scrolls). The response
    carries `total` and `has_more` so the caller knows whether
    another page exists without fetching it."""
    with _CHAT_THREADS_LOCK:
        data = _chat_threads_load()
    want = (mode or "").strip().lower()
    if want not in ("light", "dag"):
        want = ""
    items = [
        {"conversation_id": cid,
         "title": (t.get("title") or "(untitled)")[:90],
         "mode": t.get("mode") or "light",
         "updated": t.get("updated") or 0,
         "active": _chat_conversation_active(cid),
         "turns": len(t.get("messages", [])) // 2}
        for cid, t in data.items() if isinstance(t, dict)
    ]
    if want:
        items = [it for it in items if it["mode"] == want]
    items.sort(key=lambda x: x["updated"], reverse=True)
    offset = max(0, offset)
    limit = max(1, min(limit, 100))
    page = items[offset:offset + limit]
    return {"threads": page,
            "total": len(items),
            "has_more": offset + len(page) < len(items)}
@app.get("/api/chat/threads/{conversation_id}")
async def get_chat_thread(conversation_id: str):
    """Full message history for one lightweight thread."""
    with _CHAT_THREADS_LOCK:
        t = _chat_threads_load().get(conversation_id)
    if not isinstance(t, dict):
        return JSONResponse(status_code=404, content={"error": "unknown thread"})
    active = _chat_conversation_active(conversation_id)
    if not active:
        _chat_reconcile_pending(conversation_id, t)
        active = _chat_conversation_active(conversation_id)
    return {"conversation_id": conversation_id,
            "title": t.get("title") or "(untitled)",
            "active": active,
            "messages": t.get("messages") or []}
@app.delete("/api/chat/threads/{conversation_id}")
async def delete_chat_thread(conversation_id: str):
    """Delete a thread AND its spend ledger.

    Removing the row from chat_threads.json is only half the delete. The
    per-thread turn ledger lives on disk under `state/threads/<id>/`, and the
    Ledger's "All time" view sums every ledger it finds - so a deleted
    conversation kept contributing its spend to the all-time total. Measured
    after deleting 94 threads: 243 orphaned ledger directories remained and
    the phantom spend was still on screen.

    Best-effort by design: the row is already gone from the store, so a
    failure to unlink the directory must not turn a completed delete into an
    error the UI reports as a failure. The worst case is one stale directory
    that stops being counted, which is strictly better than the ledger
    outliving the thread.
    """
    with _CHAT_THREADS_LOCK:
        data = _chat_threads_load()
        if conversation_id not in data:
            return JSONResponse(status_code=404, content={"error": "unknown thread"})
        del data[conversation_id]
        _chat_threads_save(data)
    try:
        import shutil as _sh
        _tdir = STATE_DIR / "threads" / conversation_id
        # Only a plain id may be joined onto STATE_DIR; the id already had to
        # pass the store lookup, but this path is built from it, so it is
        # re-checked rather than trusted.
        if _plain_id_ok(conversation_id) and _tdir.is_dir():
            _sh.rmtree(_tdir, ignore_errors=True)
    except Exception as e:                   # noqa: BLE001
        print(f"[chat.threads] ledger cleanup failed for {conversation_id}: {e!r}")
    return {"status": "ok"}

@app.post("/api/chat/threads/{conversation_id}/turn")
async def append_chat_thread_turn(conversation_id: str, req: Request):
    """Persist a turn into the chat-thread sidebar store.

    The DAG engine keeps its transcript in the run's session, which the
    Chat sidebar never lists, so a DAG conversation was invisible and
    unreloadable there while lightweight threads showed up under BOTH
    engines. The Chat view calls this after a DAG turn so the conversation
    is listed under `dag` and reopening it restores the transcript.
    Lightweight turns are written server-side already.
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    if not _plain_id_ok(conversation_id):
        return JSONResponse(status_code=400, content={
            "error": "conversation_id must be a plain id "
                     "(letters, digits, '-', '_', '.')"})
    query = (_body_str(body, "query") or "").strip()[:2000]
    answer = (_body_str(body, "answer") or "").strip()[:20000]
    if not query:
        return JSONResponse(status_code=400,
                            content={"error": "query required"})
    mode = ("dag" if (_body_str(body, "mode") or "").strip().lower()
            == "dag" else "light")
    with _CHAT_THREADS_LOCK:
        data = _chat_threads_load()
        thread = data.get(conversation_id) or {
            "title": query[:60], "updated": 0, "messages": []}
        hist = thread.get("messages") or []
        thread["messages"] = (
            hist + [{"role": "user", "content": query},
                    {"role": "assistant", "content": answer}]
        )[-(_CHAT_MAX_TURNS * 2):]
        thread["updated"] = _time.time()
        if not thread.get("title"):
            thread["title"] = query[:60]
        thread["mode"] = mode
        data[conversation_id] = thread
        _chat_threads_save(data)
    return {"status": "ok"}
@app.post("/api/chat/simple")
async def chat_simple(req: Request):
    """One direct gateway LLM call (with read-only tools), billed to the
    thread. No Executor, no graph."""
    # Bound here, not where it was first needed: `await _aio...` with no
    # binding in scope is a NameError, and it took the whole route down.
    import asyncio as _aio
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    query = _body_str(body, "query")
    if query is None:
        return JSONResponse(status_code=400,
                            content={"error": "query must be a string"})
    conversation_id = _conv_id(body)
    if conversation_id and not _plain_id_ok(conversation_id):
        # The id becomes a chat_threads.json key: a path-shaped
        # value was persisted verbatim (contained pollution, but
        # junk keys forever). Refuse it at the boundary.
        return JSONResponse(status_code=400, content={
            "error": "conversation_id must be a plain id "
                     "(letters, digits, '-', '_', '.')"})
    if not query:
        return JSONResponse(status_code=400, content={"error": "query required"})
    if len(query) > _CHAT_MAX_QUERY:
        return JSONResponse(status_code=400, content={
            "error": f"query too long (max {_CHAT_MAX_QUERY} chars)"})
    # Optional model pin: a gateway model id chosen in the console. Empty /
    # absent means the gateway's own routing decides (the previous behaviour).
    model = _body_str(body, "model")
    if model is None:
        return JSONResponse(status_code=400,
                            content={"error": "model must be a string"})
    model = model[:200] or None
    try:
        from gateway import LLM, ensure_gateway
        ensure_gateway()
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "error": f"gateway unavailable: {e}"})
    # Resolve/create the thread id BEFORE snapshotting cost: the id is what
    # the LLM call bills against, and a first message (conversation_id=None)
    # would otherwise snapshot under None, make the delta unknowable, and
    # silently drop this reply from the ledger.
    with _CHAT_THREADS_LOCK:
        _pre = _chat_threads_load()
        if not conversation_id:
            # No id supplied: mint one.
            import uuid as _uuid
            conversation_id = f"ct-{_uuid.uuid4().hex[:8]}"
            _pre[conversation_id] = {"title": query[:60], "updated": 0, "mode": "light",
                                     "messages": []}
            _chat_threads_save(_pre)
        elif conversation_id not in _pre:
            # A client-supplied id that is not yet a thread is BOUND, not
            # replaced. It used to be overwritten with a fresh `ct-` id, so a
            # client that persisted its own conversation id silently lost all
            # context every turn: turn 1 answered under `ct-a1b2c3d4`, turn 2
            # arrived asking for `ct-a1b2c3d4`, was told that id did not exist,
            # and a NEW thread was minted. HTTP 200 throughout, no error and no
            # warning - the client just forgot the conversation.
            _pre[conversation_id] = {"title": query[:60], "updated": 0, "mode": "light",
                                     "messages": []}
            _chat_threads_save(_pre)
    _capture_preference_bg(query, conversation_id)
    _capture_observation_bg(query, conversation_id)
    # Snapshot this thread's gateway spend BEFORE the call so the ledger can
    # record exactly this reply. Lightweight chat used to be invisible to
    # the Ledger ("no cost ledger") — which silently under-reported spend on
    # the very path the Chat view uses by default.
    # Serialise turns on one conversation so concurrent snapshot deltas
    # cannot include each other's tokens (H5).
    turn_lock = _turn_lock(conversation_id)
    await _aio.to_thread(turn_lock.acquire)
    cost_before = None
    cost_recorded = False
    try:
        cost_before = await _acost_breakdown(conversation_id)
        # Build the message list under the lock, then RELEASE it before the model
        # call. This used to hold a threading.Lock across a blocking sync HTTP
        # call and an `await`, inside an async def: every coroutine that touches
        # the thread store (the sidebar's GET /api/chat/threads) blocked for the
        # entire reply, and `Lock.acquire()` on the event loop blocks every other
        # task besides. The streaming twin already scopes it this way.
        with _CHAT_THREADS_LOCK:
            data = _chat_threads_load()
            if conversation_id not in data:
                data[conversation_id] = {"title": query[:60], "updated": 0, "mode": "light",
                                         "messages": []}
            thread = data[conversation_id]
            history = thread.get("messages") or []
            messages = ([{"role": "system", "content": _chat_system_now()}]
                        + _chat_history_for_model(history[-(_CHAT_MAX_TURNS * 2):])
                        + [{"role": "user", "content": query}])
        # Documents are NOT retrieved up front any more. The block used to inject a
        # search's worth of chunks into every chat turn whether or not the question
        # was about the user's uploads, which made the lightest surface the one
        # carrying the biggest prompt. `search_knowledge` is the access, and the
        # system prompt now says so; `_conversation_doc_ids` still SCOPES the tool
        # (below), so a disabled document cannot come back through it.
        messages = await _with_chat_memory(messages, query, conversation_id)
        messages = _with_channel_persona(messages, conversation_id)
        try:
            # Read-only tools so chat can answer current/external facts
            # (web_search, fetch_url, get_time, search_knowledge,
            # currency_convert) instead of guessing. Messaging, calendar and
            # file-write tools stay on the DAG path. Computer use is the one
            # side-effecting capability chat does get, because it is gated:
            # off unless the host opts in, dry-run by default, and every
            # sensitive action waits in the approval queue.
            # Tool failures fall back to a plain text call — chat never
            # 502s on tool infrastructure.
            from skills import tool_payload as _tool_payload
            # Prefab kill-switch (Apps > Flags): chat.tools=false forces
            # plain-text replies even though the catalog is wired.
            try:
                from flags import is_enabled as _flag_on
                _chat_tools_on = _flag_on("chat.tools", True)
            except Exception:
                _chat_tools_on = True
            # The single list. Everything absent is unreachable, so this array is
            # the whole of chat's capability. Assigned at the try level, NOT inside
            # the except above: when the flags import succeeds (the normal case) a
            # nested assignment never runs and the `if _tools` below raises
            # UnboundLocalError, which is a 502 on every one-shot chat.
            _tools = _chat_tool_payload() if _chat_tools_on else None
            # The streaming twin hands the two delegate_* tools to the DAG and
            # reports the rendered files on `done`; this one-shot path was missing
            # the dispatcher entirely, so the same question delegated from a
            # non-streaming client could not be handed off at all.
            # `handoff_report` receives the FULL text of a delegated research
            # report; it becomes the answer verbatim below.
            handoff_files: list = []
            handoff_report: dict = {}
            if _tools:
                try:
                    from mcp_runner import run_with_tools as _run_with_tools
                    import outcomes as _outcomes
                    reply = await _run_with_tools(
                        messages=messages, tools_payload=_tools,
                        agent="chat", session_id=conversation_id, model=model,
                        max_tokens=2048, temperature=0.7,
                        doc_ids=_conversation_doc_ids(conversation_id, body),
                        extra_dispatch=_chat_handoff_dispatch(conversation_id,
                                                              handoff_files,
                                                              handoff_report),
                        on_outcome=lambda n, a, ok, t, lat: _outcomes.on_tool_outcome(
                            name=n, arguments=a, ok=ok, result_text=t,
                            latency_s=lat, session_id=conversation_id,
                            run_id=conversation_id))
                except Exception as _te:
                    print(f"[chat] tool loop failed, plain-text fallback: "
                          f"{type(_te).__name__}: {_te}")
                    reply = LLM().chat(messages=messages, agent="chat",
                                       session=conversation_id, model=model,
                                       max_tokens=2048, temperature=0.7)
            else:
                reply = LLM().chat(messages=messages, agent="chat",
                                   session=conversation_id, model=model,
                                   max_tokens=2048, temperature=0.7)
            answer = (reply.get("text") or "").strip() or _chat_empty_answer(reply)
            # A delegated research report IS the answer - the research agent's own
            # words, verbatim, exactly as the Research page would show them. The
            # model's summary of it was the difference the user saw between the two
            # surfaces; the report replaces that summary instead of sitting next to
            # it. (`answer_full` never travelled through the model: see
            # `_chat_handoff_dispatch`.)
            report_title = str(handoff_report.get("topic") or "").strip()
            report_text = str(handoff_report.get("text") or "").strip()
            if report_text:
                answer = report_text
        except Exception as e:
            # The provider may still have billed the failed/partial turn. Record
            # what the gateway says before returning the error, or that spend is
            # invisible in the ledger (H4).
            try:
                delta = await _aio.to_thread(_session_cost_delta,
                                             conversation_id, cost_before)
                if delta:
                    _record_turn_cost(conversation_id, query, delta)
                cost_recorded = True
            except Exception:
                pass
            return JSONResponse(status_code=502, content={
                "error": f"chat failed: {e}"})
        files_out = (_chat_produced_files(conversation_id, answer)
                     or handoff_files)
        # Re-acquire to persist. A concurrent turn on the same thread may have
        # appended in the meantime, so re-read the history rather than writing
        # back the snapshot taken before the call.
        import time as _t
        with _CHAT_THREADS_LOCK:
            data = _chat_threads_load()
            thread = data.setdefault(
                conversation_id,
                {"title": query[:60], "updated": 0, "messages": []})
            live = thread.get("messages") or []
            _assistant = {"role": "assistant", "content": answer}
            # The kind/title is what lets a LATER turn - and the console after a
            # reload - know this message is a report shown in full, so the model
            # gets a name-and-description stub for it and the UI renders the card.
            if report_text:
                _assistant["kind"] = "report"
                _assistant["title"] = report_title or query[:80]
            if files_out:
                _assistant["files"] = files_out
            thread["messages"] = (live + [{"role": "user", "content": query},
                                          _assistant]
                                  )[-(_CHAT_MAX_TURNS * 2):]
            thread["updated"] = _t.time()
            if not thread.get("title"):
                thread["title"] = query[:60]
            # Bound the store: drop oldest threads beyond the cap.
            if len(data) > _CHAT_MAX_THREADS:
                for _cid in sorted(data, key=lambda c: data[c].get("updated", 0)
                               )[:len(data) - _CHAT_MAX_THREADS]:
                    print(f"[chat.threads] EVICTED {_cid} - over the "
                          f"{_CHAT_MAX_THREADS}-thread safety valve")
                    del data[_cid]
            _chat_threads_save(data)
        # Persist this reply's spend under the thread id (threads/ tree), outside
        # the thread lock so a gateway round-trip can't stall other chat saves.
        try:
            delta = await _aio.to_thread(_session_cost_delta,
                                           conversation_id, cost_before)
            if delta:
                _record_turn_cost(conversation_id, query, delta)
            cost_recorded = True
        except Exception:
            pass
        return {"answer": answer, "conversation_id": conversation_id,
                "kind": ("report" if report_text else ""),
                "title": (report_title if report_text else ""),
                "files": files_out}
    finally:
        # A cancellation bypasses both `except Exception` blocks. The
        # gateway may already have billed the turn, so reconcile here too;
        # blocking is correct on this rare path (H4).
        if not cost_recorded and cost_before is not None:
            try:
                delta = _session_cost_delta(conversation_id, cost_before)
                if delta:
                    _record_turn_cost(conversation_id, query, delta)
            except Exception:
                pass
        turn_lock.release()

def _chat_chunks(text: str, words_per_chunk: int = 4):
    """Split an answer into word-boundary chunks for progressive SSE
    `delta` frames. The tool loop returns final text whole, so the chunking
    is the finest granularity the transport can offer; word boundaries keep
    markdown from being re-parsed mid-token (e.g. `**bol` -> `**bold**`).
    INVARIANT: `"".join(chunks) == text`, exactly. This did not hold. The
    splitter cuts on `" "` and rejoined each group with `" "`, which throws away
    the separator *between* groups, so every chunk boundary silently ate one
    space. Measured over ~600 boundaries in 20 streams: 359 inserts, 0 deletes,
    exactly 1.0 lost space per boundary, so a client rendering from `delta` saw
    `"How can Ihelp you today?"` and `"Bread is a staplefood prepared from
    adough"` while `done.answer` - and the persisted thread history - held the
    correct text. Live rendering and copy-paste were corrupted for every
    streamed answer; only the final message was right, which is exactly why it
    survived so long.
    """
    text = text or ""
    if not text:
        return
    words = text.split(" ")
    step = max(1, words_per_chunk)
    last = len(words)
    for i in range(0, last, step):
        group = words[i:i + step]
        if not group:
            continue
        chunk = " ".join(group)
        # Re-attach the separator this group consumed, so concatenation is
        # lossless.
        if i + step < last:
            chunk += " "
        yield chunk
# ── in-flight run registry (research submit idempotency) ────────────────────
# Keyed by an idempotency key, valued by the session id currently serving it.
# A duplicate submit joins the existing run instead of paying for a second
# one. Entries expire on their own so a crashed run cannot wedge the key.
_IDEM_TTL_S = 600.0
_inflight_lock = threading.Lock()
_inflight_runs: dict[str, tuple[str, float]] = {}
def _req_digest(text: str) -> str:
    import hashlib
    return hashlib.sha256(" ".join((text or "").split()).lower().encode("utf-8")
                          ).hexdigest()[:24]
def _inflight_run_claim(key: str, session_id: str) -> str:
    """Atomically claim `key`, or return the session already serving it.

    Lookup followed by a separate claim let two simultaneous submits both see
    "not running" and both start a paid run. The check and the insert now
    happen under one lock. Returns "" when this caller owns the claim.
    """
    if not key:
        return ""
    with _inflight_lock:
        now = _time.time()
        for k, (_sid, ts) in list(_inflight_runs.items()):
            if now - ts > _IDEM_TTL_S:
                _inflight_runs.pop(k, None)
        hit = _inflight_runs.get(key)
        if hit is not None:
            return hit[0]
        _inflight_runs[key] = (session_id, now)
        return ""
def _inflight_run_release(key: str) -> None:
    if not key:
        return
    with _inflight_lock:
        _inflight_runs.pop(key, None)
def _inflight_run_touch(key: str) -> None:
    """Refresh a claim's timestamp.
    A run longer than _IDEM_TTL_S used to have its claim expire
    mid-flight, so a retry of the same query (double-click, or a
    client that gave up and resent) started a SECOND concurrent
    run against the same session dir. The stream heartbeat calls
    this, so the TTL only ever expires a claim whose owner went
    silent — the crash case the expiry exists for."""
    if not key:
        return
    with _inflight_lock:
        hit = _inflight_runs.get(key)
        if hit:
            _inflight_runs[key] = (hit[0], _time.time())
def _inflight_run_lookup(key: str) -> str:
    if not key:
        return ""
    with _inflight_lock:
        now = _time.time()
        for k, (_sid, ts) in list(_inflight_runs.items()):
            if now - ts > _IDEM_TTL_S:
                _inflight_runs.pop(k, None)
        hit = _inflight_runs.get(key)
        return hit[0] if hit else ""
async def _dedup_stream(session_id: str, key: str = ""):
    """Tell the caller it joined a run that is already going, then follow it.
    Streaming the existing run's frames would need a per-run fan-out bus; the
    honest minimum is to say so immediately so the UI stops showing a second
    spinner and can watch the original run instead.
    The stream MUST end in a terminal frame. The client treats a stream that
    stops without `done`/`error` as a failed run and shows a red
    "connection closed before the run finished" pill -- so a *successful*
    dedup used to render as an error, with an empty canvas. This ends with
    `error` rather than `done` because no answer was produced here, and the
    message says the real reason instead of blaming the connection.
    """
    yield _sse("started", conversation_id=session_id, deduplicated=True,
               text="an identical run is already in progress — showing that one")
    yield _sse("status", text="joined the run already in flight")
    yield _sse("error",
               text=("an identical run is already in progress, so this submit "
                     "was not run again - watch that run in /runs to see its "
                     "result"),
               deduplicated=True)
    # A duplicate must never release the key the original run still owns:
    # doing so let a third click start a second paid run while the first was
    # still going.
@app.post("/api/chat/simple/stream")
async def chat_simple_stream(req: Request):
    """SSE twin of /api/chat/simple. Frames: `started{conversation_id}`,
    `status{text}` (thinking... + live tool progress), `delta{text}`
    (answer chunks), `done{answer, conversation_id}`, `error{text}`.

    The turn runs in a server-owned task, not in this response generator.
    A refresh, navigation, or closed tab only detaches a viewer: the task
    finishes and persists the answer. `/api/chat/cancel` is the only thing
    that stops it. The generator is just the live view of the run's queue.
    """
    # Bound here, not where it was first needed: `await _aio...` with no
    # binding in scope is a NameError, and it took the whole route down.
    import asyncio as _aio
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    query = _body_str(body, "query")
    if query is None:
        return JSONResponse(status_code=400,
                            content={"error": "query must be a string"})
    conversation_id = _conv_id(body)
    if conversation_id and not _plain_id_ok(conversation_id):
        # Same storage-key hazard as /api/chat/simple.
        return JSONResponse(status_code=400, content={
            "error": "conversation_id must be a plain id "
                     "(letters, digits, '-', '_', '.')"})
    if not query:
        return JSONResponse(status_code=400, content={"error": "query required"})
    if len(query) > _CHAT_MAX_QUERY:
        return JSONResponse(status_code=400, content={
            "error": f"query too long (max {_CHAT_MAX_QUERY} chars)"})
    # Optional model pin: a gateway model id chosen in the console. Empty /
    # absent means the gateway's own routing decides (the previous behaviour).
    model = _body_str(body, "model")
    if model is None:
        return JSONResponse(status_code=400,
                            content={"error": "model must be a string"})
    model = model[:200] or None
    # Idempotency for research submits. A caller-supplied key wins;
    # otherwise an identical query inside the window joins the run in flight.
    idem_key = str(body.get("idempotency_key") or "").strip()[:120]
    if not idem_key and body.get("research"):
        idem_key = "research:" + _req_digest(query)
    # Same pre-flight as chat_simple: fail fast with a 502 the UI can show
    # as an error, rather than opening a stream that immediately errors.
    try:
        from gateway import LLM, ensure_gateway as _ensure_gateway
        await _aio.to_thread(_ensure_gateway)
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "error": f"gateway unavailable: {e}"})
    with _CHAT_THREADS_LOCK:
        _pre = _chat_threads_load()
        if not conversation_id:
            # No id supplied: mint one.
            import uuid as _uuid
            conversation_id = f"ct-{_uuid.uuid4().hex[:8]}"
            _pre[conversation_id] = {"title": query[:60], "updated": 0,
                                     "mode": "light", "messages": []}
            _chat_threads_save(_pre)
        elif conversation_id not in _pre:
            # Bound, not replaced - see the note in `chat_simple`.
            _pre[conversation_id] = {"title": query[:60], "updated": 0,
                                     "mode": "light", "messages": []}
            _chat_threads_save(_pre)
    # Claim the idempotency key only after the id exists, so a duplicate that
    # arrives while this one is still being set up joins rather than races.
    existing_id = _inflight_run_claim(idem_key, conversation_id)
    if existing_id:
        # `**_SSE_HEADERS`, not `headers=`: that name is a kwargs bundle
        # ({"media_type": ..., "headers": {...}}), so passing it as a value
        # made StreamingResponse try to encode the nested dict as a latin-1
        # header and 500 on every duplicate submit.
        return StreamingResponse(_dedup_stream(existing_id), **_SSE_HEADERS)
    # Build the model's history BEFORE inserting this turn's rows, then write
    # the question and a pending answer placeholder. That write happens before
    # the response exists, so even a refresh at frame one keeps the question.
    with _CHAT_THREADS_LOCK:
        _data = _chat_threads_load()
        _thread = _data.get(conversation_id)
        if not isinstance(_thread, dict):
            _thread = {"title": query[:60], "updated": 0, "mode": "light",
                       "messages": []}
        _history = _thread.get("messages") or []
        messages = ([{"role": "system", "content": _chat_system_now()}]
                    + _chat_history_for_model(_history[-(_CHAT_MAX_TURNS * 2):])
                    + [{"role": "user", "content": query}])
    try:
        turn_id = _chat_pending_append(conversation_id, query, "light")
    except Exception:
        # The thread store being unavailable must not take the turn down;
        # the answer is still worth returning. `_finish` will report that it
        # could not be saved.
        turn_id = ""
    # Same deterministic preference capture as chat_simple (see
    # _capture_preference_bg for why it does not rely on the model).
    _capture_preference_bg(query, conversation_id)
    _capture_observation_bg(query, conversation_id)

    # The run's live view. Bounded so a detached run cannot grow memory with
    # a slow/absent reader; text is also accumulated in `acc`, so a dropped
    # delta only loses painting, never the answer.
    q = _aio.Queue(maxsize=4000)
    acc = {"text": ""}
    streamed = {"any": False}
    busy: dict = {}
    run_started = _time.monotonic()
    started_box = {"v": False}

    def _put(kind: str, payload: dict) -> None:
        try:
            q.put_nowait((kind, payload))
        except _aio.QueueFull:
            pass

    def _put_final(kind: str, payload: dict) -> None:
        # A terminal frame must never be the item the full queue drops.
        while True:
            try:
                q.put_nowait((kind, payload))
                return
            except _aio.QueueFull:
                try:
                    q.get_nowait()
                except _aio.QueueEmpty:
                    pass

    def _emit(kind: str, payload: dict) -> None:
        if kind == "text":
            chunk = str(payload.get("delta") or "")
            if chunk:
                streamed["any"] = True
                acc["text"] += chunk
                _put("delta", {"text": chunk})
            return
        if kind == "tool_call":
            _name = str(payload.get("name", "tool"))
            if _name == "delegate_research":
                busy["note"] = ("research is ongoing - the report will "
                                "appear here as soon as it is ready")
                _put("status", {"text": (
                    "starting the research - this can take a few minutes...")})
            elif _name == "delegate_authoring":
                busy["note"] = ("the document is being prepared - it will "
                                "appear here as soon as it is rendered")
                _put("status", {"text": (
                    "preparing the document (research, then rendering) - "
                    "this can take a few minutes...")})
            else:
                _put("status", {"text": f"using {_name}..."})
        elif kind == "tool_result":
            busy.pop("note", None)
            if not payload.get("ok", True):
                _put("status", {"text": (
                    f"{payload.get('name', 'tool')} failed, continuing...")})

    async def _run_turn() -> None:
        started_box["v"] = True
        turn_lock = None
        locked = False
        cost_before = None
        model_name = ""
        provider = ""

        def _finish(*, answer: str = "", error: str = "", stopped: bool = False,
                    files: list | None = None, kind: str = "",
                    title: str = "", save_error: str = "") -> None:
            try:
                _chat_pending_finish(conversation_id, turn_id, answer=answer,
                                     error=error, stopped=stopped, files=files,
                                     kind=kind, title=title)
            except Exception as e:
                # The answer is still worth showing; the client gets the same
                # "answer delivered but could not be saved" contract as before.
                save_error = save_error or f"{type(e).__name__}: {e}"
            final_answer = answer or ("Stopped." if stopped else "")
            if save_error:
                _put_final("__error__", {"text": (
                    f"answer delivered but could not be saved: {save_error}"
                    if final_answer else
                    f"could not save this turn: {save_error}")})
            elif error:
                _put_final("__error__", {"text": f"chat failed: {error}"})
            else:
                payload = {
                    "answer": final_answer,
                    "conversation_id": conversation_id,
                    "elapsed_ms": int((_time.perf_counter() - started_box["t0"])
                                      * 1000),
                    "model": model_name,
                    "provider": provider,
                    "kind": kind,
                    "title": title,
                    "files": files or [],
                }
                if stopped:
                    payload["stopped"] = True
                _put_final("__done__", payload)

        started_box["t0"] = _time.perf_counter()
        try:
            # Recall first: it is three gateway round-trips and needs no
            # conversation lock. Same recall as /api/chat/simple.
            _messages = await _with_chat_memory(messages, query, conversation_id)
            _messages = _with_channel_persona(_messages, conversation_id)
            turn_lock = _turn_lock(conversation_id)
            await _aio.to_thread(turn_lock.acquire)
            locked = True
            try:
                cost_before = await _aio.to_thread(_session_cost_breakdown,
                                                   conversation_id)
            except Exception:
                cost_before = None
            from skills import tool_payload as _tool_payload  # noqa: F401
            try:
                from flags import is_enabled as _flag_on
                _chat_tools_on = _flag_on("chat.tools", True)
            except Exception:
                _chat_tools_on = True
            _tools = _chat_tool_payload() if _chat_tools_on else None
            handoff_files: list = []
            handoff_report: dict = {}

            async def _work() -> dict:
                nonlocal model_name, provider
                if _tools:
                    try:
                        from mcp_runner import run_with_tools as _run_with_tools
                        import outcomes as _outcomes
                        return await _run_with_tools(
                            messages=_messages, tools_payload=_tools,
                            agent="chat", session_id=conversation_id, model=model,
                            max_tokens=2048, temperature=0.7,
                            doc_ids=_conversation_doc_ids(conversation_id, body),
                            stream=True,
                            on_event=_emit,
                            extra_dispatch=_chat_handoff_dispatch(
                                conversation_id, handoff_files, handoff_report),
                            on_outcome=lambda n, a, ok, t, lat: _outcomes.on_tool_outcome(
                                name=n, arguments=a, ok=ok, result_text=t,
                                latency_s=lat, session_id=conversation_id,
                                run_id=conversation_id))
                    except Exception as _te:
                        print(f"[chat] tool loop failed, plain-text fallback: "
                              f"{type(_te).__name__}: {_te}")
                # No tools: stream the gateway directly.
                try:
                    llm = LLM()
                    stream_fn = getattr(llm, "stream_chat", None)
                    if stream_fn is not None:
                        parts: list[str] = []
                        try:
                            async for ev in stream_fn(
                                    messages=_messages, agent="chat",
                                    session=conversation_id, model=model,
                                    max_tokens=2048, temperature=0.7):
                                kind = ev.get("type")
                                if kind == "text":
                                    chunk = str(ev.get("text") or "")
                                    if chunk:
                                        parts.append(chunk)
                                        _emit("text", {"delta": chunk})
                                elif kind == "done":
                                    provider = str(ev.get("provider") or provider)
                                    model_name = str(ev.get("model") or model_name)
                            text = "".join(parts)
                            return {"text": text, "provider": provider,
                                    "model": model_name,
                                    "input_tokens": 0,
                                    "output_tokens": len(text) // 4}
                        except Exception as _se:
                            if parts:
                                raise
                            print(f"[chat] stream failed, batched fallback: "
                                  f"{type(_se).__name__}: {_se}")
                except Exception as _se:
                    if streamed["any"]:
                        raise
                    print(f"[chat] no stream available: "
                          f"{type(_se).__name__}: {_se}")
                return await _aio.to_thread(
                    LLM().chat, messages=_messages, agent="chat",
                    session=conversation_id, model=model,
                    max_tokens=2048, temperature=0.7)

            reply = await _work()
            reply = reply if isinstance(reply, dict) else {}
            model_name = str(reply.get("model") or model_name)
            provider = str(reply.get("provider") or provider)
            answer = ((reply.get("text") or "").strip()
                      or _chat_empty_answer(reply))
            report_title = str(handoff_report.get("topic") or "").strip()
            report_text = str(handoff_report.get("text") or "").strip()
            if report_text:
                answer = report_text
            files_out = (_chat_produced_files(conversation_id, answer)
                         or handoff_files)
            if report_text:
                _put("status", {"text": "report ready - showing it in full..."})
            elif not streamed["any"]:
                # Batched fallback only. Live deltas already painted it.
                for _chunk in _chat_chunks(answer):
                    _put("delta", {"text": _chunk})
                    await _aio.sleep(_CHAT_STREAM_PACE_S)
                acc["text"] = answer
            _finish(answer=answer,
                    files=files_out,
                    kind=("report" if report_text else ""),
                    title=((report_title or query[:80]) if report_text else ""))
        except _aio.CancelledError:
            # Stop. Keep the partial answer the user already paid for, and
            # persist it so reopening the thread shows the same thing.
            _finish(answer=acc["text"].strip(), stopped=True)
        except Exception as e:
            _finish(answer=acc["text"].strip(),
                    error=f"{type(e).__name__}: {e}")
        finally:
            # Record spend whatever happened, including cancellation. The
            # fallback is synchronous because awaiting inside a cancelled
            # task can raise again before the ledger write happens.
            try:
                if cost_before is not None:
                    try:
                        delta = await _aio.to_thread(_session_cost_delta,
                                                     conversation_id, cost_before)
                    except BaseException:
                        delta = _session_cost_delta(conversation_id, cost_before)
                    if delta:
                        _record_turn_cost(conversation_id, query, delta)
            except Exception:
                pass
            if locked and turn_lock is not None:
                turn_lock.release()
            _put_final("__end__", {})

    _put("status", {"text": "thinking..."})
    runner = _aio.create_task(_run_turn())

    def _runner_done(task) -> None:
        _chat_run_unregister(conversation_id, task)
        if task.cancelled():
            # A task cancelled before its body ran never reached _finish, so
            # the placeholder would otherwise stay pending until reconcile.
            if not started_box["v"]:
                try:
                    _chat_pending_finish(conversation_id, turn_id, stopped=True)
                except Exception:
                    pass
                _put_final("__done__", {
                    "answer": "Stopped.", "conversation_id": conversation_id,
                    "elapsed_ms": int((_time.perf_counter() - run_started) * 1000),
                    "model": "", "provider": "", "kind": "", "title": "",
                    "files": [], "stopped": True})
            return
        try:
            exc = task.exception()
        except Exception:
            exc = None
        if exc is not None:
            print(f"[chat] background turn ended with {type(exc).__name__}: {exc}")

    runner.add_done_callback(_runner_done)
    _chat_run_register(conversation_id, runner, turn_id)

    async def gen():
        yield _sse("started", conversation_id=conversation_id)
        _last_beat = _time.monotonic()
        terminal: tuple[str, dict] | None = None
        while True:
            try:
                item = await _aio.wait_for(q.get(), timeout=0.5)
            except _aio.TimeoutError:
                _now = _time.monotonic()
                if _now - _last_beat >= _CHAT_STREAM_HEARTBEAT_S:
                    _last_beat = _now
                    _inflight_run_touch(idem_key)
                    yield _sse("status", text=(
                        (busy.get("note") or "still working")
                        + f" - {int(_now - run_started)}s elapsed"))
                continue
            if item is None:
                break
            kind, payload = item
            if kind == "__end__":
                break
            if kind in ("__done__", "__error__"):
                terminal = (kind, payload)
                break
            yield _sse(kind, **payload)
        if terminal is not None:
            kind, payload = terminal
            yield _sse("done" if kind == "__done__" else "error", **payload)

    return StreamingResponse(gen(), **_SSE_HEADERS)

def _sse(event_type: str, **payload) -> str:
    """One SSE-style frame: `data: {json}\\n\\n`."""
    frame = {"type": event_type, **payload}
    return "data: " + json.dumps(frame) + "\n\n"
# Direct-to-uvicorn streaming works without these, but the first reverse
# proxy in front of this (nginx's `proxy_buffering on` is the default) will
# hold an entire research run in its buffer and both chat views will appear
# to hang with no output. These are the headers Starlette's own
# EventSourceResponse sets, for exactly this reason.
_SSE_HEADERS = {
    "media_type": "text/event-stream",
    "headers": {
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    },
}
def _clean_doc_setup(raw) -> dict | None:
    """Whitelist the Authoring setup panel's fields.
    This value is applied to the render - format, paper, typeface, columns -
    so it is untrusted input until it has been narrowed to known keys with
    known types. Anything else is dropped rather than forwarded.
    """
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    fmt = str(raw.get("format") or "").strip().lower()
    if fmt in ("auto", "pdf", "pptx", "docx", "xlsx"):
        out["format"] = fmt
    for key, limit in (("page_size", 32), ("orientation", 32),
                       ("margins", 32), ("style", 32),
                       ("citation_style", 32), ("slide_size", 16),
                       ("length", 40)):
        v = raw.get(key)
        if isinstance(v, str) and v:
            out[key] = v[:limit]
    # The panel sends a BOOLEAN for this one - the user is asking for a
    # running header, not choosing its words. The server fills in the text
    # from the document's own title, because a literal string sent from the
    # client would stamp the same words on every document.
    if raw.get("running_header") is True:
        out["running_header"] = "1"
    for key in ("toc", "cover"):
        if raw.get(key) is True:
            out[key] = True
    cols = raw.get("columns")
    if isinstance(cols, bool):
        pass
    elif isinstance(cols, (int, float)):
        out["columns"] = max(1, min(3, int(cols)))
    return out or None
def _run_orchestrator(query: str, session_id: str,
                      should_cancel=None, doc_setup: dict | None = None,
                      run_note: str = "", run_kind: str = "") -> str:
    """Run the async orchestrator to completion in a fresh event loop.
    `Executor.run` is a coroutine, so we drive it with `asyncio.run` inside
    a worker thread (via `asyncio.to_thread`) — that keeps the streaming
    response responsive while the agent runs, and lets us capture stdout
    as a reasoning log. Returning the coroutine directly would make the
    final `done` frame non-serialisable and abort the stream.
    `session_id` gives a conversation thread a STABLE identity so memory
    and per-session state accumulate across calls (see resolve_session).
    We deliberately do NOT pass resume=True here: resume is for *reconnecting
    to an interrupted run with the SAME query*. If we resumed with a *new*
    query, the loaded graph is already fully `complete` (including its old
    formatter answer) and the execution loop would have nothing to run — so
    the agent would silently replay the previous answer instead of answering
    the new query. Each /api/chat call therefore starts a fresh graph but
    keeps the same session_id, which is what actually provides continuity
    (memory hits, audit log, cost rollup). The CLI `--resume` path in
    flow.py still supports true run-resume for the same query.
    """
    from flow import Executor
    return asyncio.run(Executor().run(query, session_id=session_id,
                                      resume=False, doc_setup=doc_setup,
                                      should_cancel=should_cancel,
                                      run_note=run_note, run_kind=run_kind))
def _shell_intent_offline(query: str) -> str | None:
    """Local-model fallback (Todo 11): when the gateway is DOWN, still answer
    read-only shell-intent queries (disk, ip, processes, os, ram, cpu, gpu,
    battery, whoami, hostname, date, env, open/read) via the gated-shell
    engine — these need NO LLM. Returns the answer string, or None if the
    query is not a pure shell-intent (so the caller can report gateway-down.
    """
    import os as _os
    if not _os.environ.get("COMPUTER_USE_ENABLED", "false").lower() == "true":
        return None
    try:
        from computer_use.engine import ComputerUseSkill
        from computer_use import safety
        sk = ComputerUseSkill(
            session_id="s8-offline",
            llm_chat=lambda *a, **k: None,
            llm_vision=lambda *a, **k: None,
            safety=safety.shared_gates(),
        )
        res = sk.run(query)
        if res.success:
            return res.output.get("content") or "done"
    except Exception:
        return None
    return None
# ── cancellable runs (plan §4.7) ────────────────────────────────────────────
# Live runs register a cancel event keyed by BOTH their session id and (when
# known) their conversation id, so POST /api/chat/cancel can find them from
# either identifier. The Executor polls the event at its node boundaries.
# A key maps to a SET of events, not one: two runs can share a conversation
# (a chat plus a scheduled job, or a double-clicked send), and a
# single-event map let the second registration silently orphan the first
# run's cancel event — while the first run to finish popped the keys out
# from under the still-running second one, leaving it uncancellable.
_ACTIVE_RUNS: dict[str, set["threading.Event"]] = {}
_ACTIVE_RUNS_LOCK = threading.Lock()
# A Stop can arrive after the conversation exists but before its worker is
# registered (the run is between claim and thread start). Remember the request
# for a short window; `_register_run` honours it the moment the event exists.
_PENDING_CANCELS: dict[str, float] = {}
_PENDING_CANCEL_TTL_S = 900.0
def _request_cancel(*keys: str) -> None:
    now = _time.time()
    with _ACTIVE_RUNS_LOCK:
        for k in keys:
            if k:
                _PENDING_CANCELS[k] = now
        for k, ts in list(_PENDING_CANCELS.items()):
            if now - ts > _PENDING_CANCEL_TTL_S:
                _PENDING_CANCELS.pop(k, None)
def _register_run(cancel_ev: "threading.Event", *keys: str | None) -> None:
    with _ACTIVE_RUNS_LOCK:
        for k in keys:
            if k:
                _ACTIVE_RUNS.setdefault(k, set()).add(cancel_ev)
                if _PENDING_CANCELS.pop(k, None) is not None:
                    cancel_ev.set()
def _unregister_run(*keys: str | None,
                    cancel_ev: "threading.Event | None" = None) -> None:
    """Remove a run's cancel event from every key it registered.
    With `cancel_ev`, only THAT event is discarded, so a concurrent
    run sharing the same session or conversation key stays
    cancellable. Without it (legacy callers), the keys are dropped
    outright, as before.
    """
    with _ACTIVE_RUNS_LOCK:
        for k in keys:
            if not k:
                continue
            if cancel_ev is None:
                _ACTIVE_RUNS.pop(k, None)
                continue
            evs = _ACTIVE_RUNS.get(k)
            if evs is None:
                continue
            evs.discard(cancel_ev)
            if not evs:
                del _ACTIVE_RUNS[k]
def _active_run_count() -> int:
    with _ACTIVE_RUNS_LOCK:
        dag = len({id(ev) for evs in _ACTIVE_RUNS.values()
                   for ev in evs})
    with _CHAT_RUNS_LOCK:
        light = sum(1 for rec in _CHAT_RUNS.values()
                    if rec.get("task") is not None
                    and not rec["task"].done())
    return dag + light
@app.post("/api/chat/cancel")
async def cancel_chat(req: Request):
    """Stop a live run: `{"conversation_id": ...}` or `{"session_id": ...}`.
    The flag is honoured by the Executor at its next node boundary, so the
    in-flight LLM call finishes (asyncio cannot kill an await mid-flight),
    remaining nodes are persisted as `skipped`, and the partial graph stays
    on disk. `found=false` (still 200) when nothing is running for that id —
    stopping an idle run is not an error.
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    raw_keys = [body.get("conversation_id"), body.get("session_id")]
    # Only hashable scalars can key the registry: a list/dict value
    # used to raise TypeError (unhashable) — a bare 500 on a route
    # whose whole job is to answer "nothing running" gracefully.
    keys = [k for k in raw_keys
            if isinstance(k, str)
            or (isinstance(k, (int, float)) and not isinstance(k, bool))]
    keys = [k if isinstance(k, str) else str(k) for k in keys]
    with _ACTIVE_RUNS_LOCK:
        pairs = [(k, ev) for k in keys
                 for ev in _ACTIVE_RUNS.get(k, ())]
    # Lightweight runs are asyncio tasks in their own registry, not DAG
    # threading.Events. Stop must cancel those too, or the "only Stop can
    # stop a run" contract is false for the default chat engine.
    with _CHAT_RUNS_LOCK:
        chat_tasks = []
        for _k in keys:
            _rec = _CHAT_RUNS.get(_k)
            _task = _rec.get("task") if isinstance(_rec, dict) else None
            if _task is not None and not _task.done():
                chat_tasks.append(_task)
    for _task in chat_tasks:
        _task.cancel()
    if pairs or chat_tasks:
        for _k, ev in pairs:
            ev.set()
        status = "cancelled"
    else:
        # The claim can exist before the worker registers its event (between
        # submit and `_stream_run`'s registration). A Stop there used to answer
        # `found:false` and the run then proceeded; remember it so the event is
        # set as soon as it appears.
        with _inflight_lock:
            pending = any(sid in keys for sid, _ts in _inflight_runs.values())
        if pending:
            _request_cancel(*keys)
            status = "pending"
        else:
            status = "missing"
    session_id = ""
    if status != "missing":
        raw_sid = body.get("session_id")
        if isinstance(raw_sid, str) and raw_sid.startswith("s8-"):
            session_id = raw_sid
        elif body.get("conversation_id") in keys:
            try:
                session_id = resolve_session(str(body.get("conversation_id")))
            except Exception:
                session_id = ""
    return {"status": status, "found": status != "missing",
            "cancelled": len(pairs) + len(chat_tasks),
            "lightweight_cancelled": len(chat_tasks),
            "flagged": any(ev.is_set() for _k, ev in pairs),
            "session_id": session_id,
            "active_runs": _active_run_count()}
def _stream_run(query: str, conversation_id: str | None,
                   request: Request | None = None,
                   idem_key: str = "",
                   doc_setup: dict | None = None,
                   run_note: str = "",
                   run_kind: str = "") -> StreamingResponse:
    """Build the SSE streaming response for one agent run.
    Shared by /api/chat and /api/templates/{name}/run so both produce
    identical frames (log → meta → done). `conversation_id` (when present)
    resumes the same orchestrator graph across calls. `request` (when given)
    lets the drain loop notice client disconnects so an abandoned run stops
    emitting frames and — critically — is NOT recorded in the cost ledger
    or notified as complete.
    """
    # Shared with `gen()`: once the worker exists it owns the run, its cost
    # record and its idempotency claim. Releasing the claim on GeneratorExit
    # after thread.start() let a retry start a second concurrent run while the
    # orphan kept billing.
    worker_box = {"started": False}
    async def gen():
        # (the flag itself lives in _gen_body, which is the function that
        # reads and writes it - see the note there)
        # Belt and braces for the claim: the two explicit releases below cover
        # the pre-thread exits, and the tail covers the post-thread ones, but a
        # generator closed at any other `yield` would leak. The claim is what
        # makes a second identical submit refuse rather than double-bill, so
        # leaking it blocks the user's own topic for 10 minutes. One finally
        # around the whole body beats enumerating every exit.
        try:
            async for _frame in _gen_body():
                yield _frame
        finally:
            if idem_key and not worker_box["started"]:
                _inflight_run_release(idem_key)
    async def _gen_body():
        # Declared at the top, not further down. It was first imported ~120
        # lines below the warmup joins that need it, so `await _aio.to_thread`
        # there raised "cannot access local variable '_aio'" and the run died
        # with a gateway-unavailable error before the first real frame.
        import asyncio as _aio  # noqa: F811 - local alias used for to_thread
        # `disconnected` is INITIALISED HERE, in the function that uses it.
        #
        # It was initialised in `gen()` instead, one function up. `gen()` and
        # `_gen_body()` are SIBLING closures, not nested, so that assignment
        # did nothing for this scope. The drain loop below both reads
        # `disconnected` (to decide whether to emit a heartbeat) and assigns
        # it (once the client is seen to be gone), and the read comes first -
        # so `disconnected` became a local of _gen_body with no initial value,
        # and the first read raised UnboundLocalError. That killed the SSE
        # stream server-side on the first heartbeat, roughly a second after
        # `started`, so a research run appeared to freeze: the worker thread
        # kept going and kept billing, the client saw "connection closed
        # before the run finished", and the canvas stopped updating. Confirmed
        # in logs/agent.err.
        disconnected = False
        async def _client_gone() -> bool:
            if request is None:
                return False
            try:
                return await request.is_disconnected()
            except Exception:
                return False
        _start = _time.time()
        # Resolve the session for this conversation thread (or a fresh one).
        session_id = resolve_session(conversation_id)
        # Tell live UIs the ids up front and register the run BEFORE
        # the gateway warmup joins below, which can block up to 120s
        # on a cold start (60s gateway + 60s embedder). A Stop pressed
        # during the warmup used to find no registered run and nothing
        # to cancel, so the UI said "stopped" while the run drained
        # anyway. The worker thread starts further down; until then
        # the cancel event simply sits registered.
        cancel_ev = threading.Event()
        _register_run(cancel_ev, session_id, conversation_id)
        # Persist the run's kind HERE, before the warmup joins and before the
        # worker thread exists. The thread only creates the session directory
        # when it starts, which a cold gateway can delay by up to two minutes -
        # so a run started from the Research page was invisible in the sidebar
        # for its whole warmup, and a user who submitted and looked away came
        # back to a topic list that did not have it. The executor rewrites the
        # same file when it starts; this only makes the run appear at once.
        if run_kind:
            try:
                from persistence import SessionStore as _Store
                _early = _Store(session_id)
                _early.write_run_meta({"kind": run_kind})
                # Same content the executor writes; early so the sidebar can
                # title the topic during the warmup instead of showing it
                # blank.
                if query and not _early.query_path.exists():
                    _early.write_query(query)
            except Exception as _e:  # pragma: no cover - never block a run
                print(f"[run_meta] early write skipped: {_e!r}")
        # Everything below happens in the worker, and the worker is started
        # BEFORE the first frame is yielded. Starlette closes this generator at
        # a yield when the client goes away; the old order started the thread
        # only after `started` (and after a warmup that can take two minutes),
        # so navigating away in the first seconds destroyed the run silently.
        # `started` is still the first frame the client sees.
        #
        # The orchestrator runs its own asyncio loop internally via
        # asyncio.run in Executor.run, so we run it in a thread to keep the
        # streaming response alive. We capture stdout and STREAM each line
        # live as a `log` SSE frame (instead of buffering until the end) so
        # the operator can watch the agent work in the terminal / UI.
        answer_box: dict[str, object] = {"answer": "", "offline": False}
        fail_box: dict[str, str] = {"error": ""}
        cost_box: dict[str, object] = {"before": None, "delta": None}
        # asyncio.Queue, NOT threading.Queue: a blocking .get() here would
        # freeze the whole event loop for the entire agent run (health, TTS
        # and concurrent chats all hang). The worker thread pushes via
        # call_soon_threadsafe so `await log_q.get()` yields control.
        loop = asyncio.get_running_loop()
        # noqa: F811 - local alias used for to_thread
        # Bounded: a chatty run can't grow the queue without limit while a
        # slow/disconnected client lags behind. Overflow lines are dropped
        # (the full log still lands in the session dir via prints).
        log_q: "asyncio.Queue[str | None]" = asyncio.Queue(maxsize=2000)
        import io
        import contextlib

        def _push(item):
            # The loop may already be gone if the request was cancelled; the
            # worker still has to finish its cleanup and cost record.
            try:
                loop.call_soon_threadsafe(log_q.put_nowait, item)
            except Exception:
                pass

        class _LiveLogCatcher(io.StringIO):
            def write(self, s):  # type: ignore[override]
                if s.strip():
                    # Mirror to the real (teed) stdout so the Console feed's
                    # server tail shows live run chatter, not just startup.
                    try:
                        _LIVE_OUT.write(s)
                    except Exception:
                        pass
                    _push(s)
                return super().write(s)
        def _run_and_drain():
            # Warm up, snapshot cost, run the orchestrator, then push a
            # sentinel so the generator below knows the run finished.
            turn_lock = None
            try:
                try:
                    if _GATEWAY_WARMUP_THREAD is not None and _GATEWAY_WARMUP_THREAD.is_alive():
                        _GATEWAY_WARMUP_THREAD.join(60)
                    # Also join the embedder warmup so the first memory.read
                    # embed doesn't race a still-loading Ollama model.
                    if _EMBEDDER_WARMUP_THREAD is not None and _EMBEDDER_WARMUP_THREAD.is_alive():
                        _EMBEDDER_WARMUP_THREAD.join(60)
                    from gateway import ensure_gateway
                    ensure_gateway()
                except Exception as e:  # pragma: no cover - environment issue
                    # Local-model fallback (Todo 11): if the gateway is down
                    # but the query is a pure read-only shell-intent
                    # (disk/ip/os/...), answer it directly via the gated-shell
                    # engine — no LLM required.
                    offline = _shell_intent_offline(query)
                    if offline is not None:
                        answer_box["answer"] = offline
                        answer_box["offline"] = True
                    else:
                        fail_box["error"] = f"gateway unavailable: {e}"
                    return
                # One turn at a time per session, so a concurrent chat turn
                # cannot put this run's tokens into its own delta (H5).
                turn_lock = _turn_lock(session_id)
                turn_lock.acquire()
                # Snapshot cumulative session cost before the run so the
                # response can report only this turn. This blocking gateway
                # GET used to run on the event loop; keeping it in the worker
                # also means a client that leaves at `started` still gets a
                # correct cost record.
                try:
                    cost_box["before"] = _session_cost_breakdown(session_id)
                except Exception:
                    cost_box["before"] = None
                with contextlib.redirect_stdout(_LiveLogCatcher()):
                    answer_box["answer"] = _run_orchestrator(
                        query, session_id, cancel_ev.is_set,
                        doc_setup=doc_setup, run_note=run_note,
                        run_kind=run_kind) or ""
            except Exception as e:  # pragma: no cover - runtime failure
                fail_box["error"] = str(e)
                _push(f"AGENT ERROR: {e}")
            finally:
                # Settle the DAG chat turn here, in the worker. The drain
                # generator may already be gone (refresh/navigation), so
                # persisting next to the `done` frame loses the answer.
                if dag_turn_id:
                    try:
                        if fail_box["error"]:
                            _chat_pending_finish(
                                conversation_id, dag_turn_id,
                                error=fail_box["error"])
                        elif cancel_ev.is_set():
                            _chat_pending_finish(
                                conversation_id, dag_turn_id,
                                answer=str(answer_box.get("answer") or ""),
                                stopped=True)
                        else:
                            _chat_pending_finish(
                                conversation_id, dag_turn_id,
                                answer=str(answer_box.get("answer") or ""))
                    except Exception as _e:
                        print(f"[chat.dag] answer write skipped: {_e!r}")
                # Always release the cancel registry, whatever way we left.
                # Only OUR event: a concurrent run sharing this session or
                # conversation key must stay cancellable.
                _unregister_run(session_id, conversation_id,
                                cancel_ev=cancel_ev)
                # Record spend here, not in the generator: when the client
                # disconnects at `started`, the drain loop never resumes, but
                # the worker still finishes the run and must bill it.
                try:
                    before = cost_box.get("before")
                    if before is not None:
                        delta = _session_cost_delta(session_id, before)
                        cost_box["delta"] = delta
                        if delta is not None:
                            _record_turn_cost(session_id, query, delta)
                except Exception:
                    pass
                finally:
                    if turn_lock is not None:
                        turn_lock.release()
                # The worker owns the claim now; release it only after the run
                # and its cost record are complete.
                _inflight_run_release(idem_key)
                _push(None)  # sentinel
        # DAG chat turns are persisted by the WORKER, not by the SSE
        # generator. A refresh closes the generator, but the worker keeps
        # running; without this row the answer existed only in the tab that
        # watched it. The placeholder goes in before the thread starts, so a
        # reload during the gateway warmup still shows the question.
        dag_turn_id = ""
        if run_kind == "chat" and conversation_id:
            try:
                dag_turn_id = _chat_pending_append(conversation_id, query, "dag")
            except Exception as _e:
                print(f"[chat.dag] pending write skipped: {_e!r}")
        thread = threading.Thread(target=_run_and_drain, daemon=True)
        thread.start()
        worker_box["started"] = True
        try:
            yield _sse("started", session_id=session_id,
                       conversation_id=conversation_id or "")
        except BaseException:
            # If the worker exists it owns cleanup; only the pre-worker path
            # needs to unregister and return the idempotency claim. This is
            # the "client navigated away in the first second" case.
            if not worker_box["started"]:
                _unregister_run(session_id, conversation_id,
                                cancel_ev=cancel_ev)
                if idem_key:
                    _inflight_run_release(idem_key)
            raise
        # Stream log lines as they arrive, until the sentinel. The get has
        # a timeout so we can poll for client disconnect: a closed tab used
        # to leave the run streaming into the void (and billed + notified).
        # The same timeout drives a heartbeat: a research run can sit 60-120s
        # inside one node emitting nothing, and a buffering hop would then
        # hold the whole run. Without this the page looks dead rather than
        # busy — the lightweight chat endpoint already sends one.
        _t_start = _time.time()
        _last_beat = _t_start
        while True:
            try:
                item = await asyncio.wait_for(log_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if not disconnected and await _client_gone():
                    # Client vanished. The worker thread cannot be
                    # killed, so the run continues in the background
                    # and its spend keeps mounting. Keep draining
                    # (yielding nothing) until the worker's sentinel
                    # arrives instead of abandoning the stream here:
                    # releasing the idempotency key at disconnect
                    # let a retry of the same research query start a
                    # SECOND concurrent run against the same session
                    # dir (atomic-replace, last wins), and the
                    # post-disconnect spend was never recorded.
                    disconnected = True
                if not disconnected:
                    _now = _time.time()
                    if _now - _last_beat >= _CHAT_STREAM_HEARTBEAT_S:
                        _last_beat = _now
                        # Keep the idempotency claim alive for
                        # runs longer than the registry TTL (see
                        # _inflight_run_touch).
                        _inflight_run_touch(idem_key)
                        yield _sse("status",
                                   text=f"working… {int(_now - _t_start)}s elapsed")
                continue
            if item is None:
                break
            if not disconnected:
                if await _client_gone():
                    disconnected = True
                    continue
                yield _sse("log", text=item.rstrip("\n"))
        # No thread.join() here: joining blocks the event loop; the
        # thread is daemon-scoped and pushed its sentinel (a
        # try/finally guarantee in _run_and_drain) before this loop
        # can exit, so the run — orphaned or not — is finished.
        if disconnected:
            # The worker records the spend and releases the claim in its own
            # finally; nobody is listening for meta/done frames.
            return
        if fail_box["error"]:
            yield _sse("error", text=fail_box["error"])
            return
        # Total wall-clock time for the whole response (orchestrator run +
        # any drain), reported to the UI so the operator can see how long
        # the agent took end-to-end.
        elapsed = _time.time() - _start
        if answer_box["offline"]:
            yield _sse("log", text="[offline] gateway down - answered via "
                                 "gated-shell fallback (no LLM)")
            yield _sse("meta", elapsed_s=round(elapsed, 1))
            yield _sse("done", answer=str(answer_box["answer"]),
                       session_id="s8-offline",
                       conversation_id=conversation_id or "")
            return
        # A cancelled run usually has no formatter answer (the DAG stopped at
        # a node boundary). Give the UI something explicit instead of an
        # empty done frame, and do not announce it as a completed task.
        if cancel_ev.is_set() and not answer_box["answer"]:
            answer_box["answer"] = "Stopped by operator before the run finished."
        # Smart notification: push a completion summary to Telegram if the
        # operator opted in (fire-and-forget, never blocks the response).
        if answer_box["answer"] and not cancel_ev.is_set():
            # Fire and forget on a daemon thread, the same way
            # _capture_preference_bg does. This posts to the gateway
            # synchronously (15s timeout) and would otherwise stall the loop at
            # the exact moment a run finishes. A plain thread, not
            # run_in_executor: the executor keeps a future on the loop that
            # outlives the request.
            threading.Thread(target=notify_task_done,
                             args=(query, str(answer_box["answer"]), elapsed,
                                   session_id),
                             daemon=True, name="notify-done").start()
        # Per-response cost: report ONLY this run's spend. The session_id is
        # reused across every turn of a conversation (that reuse is what gives
        # memory/audit continuity), so the raw session total is the whole
        # conversation's lifetime — not one answer. The worker already
        # subtracted its pre-run snapshot and recorded the delta, so a client
        # that left at `started` is billed too.
        delta = cost_box.get("delta")
        if delta is None:
            # Gateway cost API unreachable: report unknown. The worker
            # recorded nothing rather than fabricating a $0 entry.
            yield _sse("meta", elapsed_s=round(elapsed, 1), cost_unknown=True)
        else:
            cost_usd = sum(d["usd"] for d in delta.values())
            cost_in = sum(d["in_tok"] for d in delta.values())
            cost_out = sum(d["out_tok"] for d in delta.values())
            yield _sse("meta", elapsed_s=round(elapsed, 1),
                       cost_usd=round(cost_usd, 6),
                       cost_in_tokens=cost_in,
                       cost_out_tokens=cost_out)
        # Surface browser screenshots (if any) so the chat UI can render the
        # websites the agent visited alongside the answer. Convert the
        # absolute artifact paths into /api/artifacts/<sid>/<rel> URLs.
        from skills import take_browser_artifacts
        from pathlib import Path as _P
        from flow import take_produced_files
        _browser_root = (_P(STATE_DIR) / "sessions" / session_id / "browser").resolve()
        browser_shots: list[str] = []
        for abs_path in take_browser_artifacts(session_id):
            try:
                rel = _P(abs_path).resolve().relative_to(_browser_root)
                browser_shots.append(f"/api/artifacts/{session_id}/{rel.as_posix()}")
            except ValueError:
                continue
        yield _sse("done", answer=str(answer_box["answer"]),
                   session_id=session_id,
                   conversation_id=conversation_id or "",
                   cancelled=cancel_ev.is_set(),
                   browser_artifacts=browser_shots,
                   files=take_produced_files(session_id))
        # The run is over: a later identical submit may start a fresh one.
        _inflight_run_release(idem_key)
    return StreamingResponse(gen(), **_SSE_HEADERS)
# Shared query-size cap for the DAG path (chat_simple uses 4000; the DAG
# path allows longer briefs but still bounds LLM context abuse/accidents).
_CHAT_DAG_MAX_QUERY = 12000
@app.post("/api/chat")
async def chat(req: Request):
    try:
        body = await req.json()
    except Exception:
        return StreamingResponse(
            iter([_sse("error", text="invalid JSON body")]),
            media_type="text/event-stream",
        )
    if not isinstance(body, dict):
        return StreamingResponse(
            iter([_sse("error", text="body must be a JSON object")]),
            media_type="text/event-stream",
        )
    query = _body_str(body, "query")
    if query is None:
        return StreamingResponse(
            iter([_sse("error", text="query must be a string")]),
            media_type="text/event-stream",
        )
    conversation_id = _conv_id(body)
    # The Authoring setup panel: a decision the user made about the document,
    # enforced on the render rather than asked for in the prompt. Whitelisted
    # here because it reaches the renderer - an unvalidated dict is not.
    _doc_setup = _clean_doc_setup(body.get("doc_setup"))
    if conversation_id and not _plain_id_ok(conversation_id):
        # The id becomes a conversations.json key and (via
        # resolve_session) a directory name: a path-shaped value
        # polluted both stores (it was accepted and persisted
        # verbatim). Refuse it at the boundary.
        return StreamingResponse(
            iter([_sse("error", text="conversation_id must be a plain id "
                                     "(letters, digits, '-', '_', '.')")]),
            media_type="text/event-stream",
        )
    if not query:
        return StreamingResponse(
            iter([_sse("error", text="empty query")]),
            media_type="text/event-stream",
        )
    if len(query) > _CHAT_DAG_MAX_QUERY:
        return StreamingResponse(
            iter([_sse("error", text=f"query too long (max {_CHAT_DAG_MAX_QUERY} chars)")]),
            media_type="text/event-stream",
        )
    if not conversation_id:
        # Mint one so EVERY run is resumable: resolve_session() only maps
        # explicit ids, so id-less runs used to vanish from history.
        import uuid as _uuid
        conversation_id = f"c-{_uuid.uuid4().hex[:8]}"
    # Idempotency. This is the endpoint the Research page actually uses, so
    # without it here a double-click on "Research this topic" still started a
    # second paid DAG run — the guard on /api/chat/simple/stream only covered
    # the lightweight chat path. A caller key wins; otherwise `research: true`
    # derives one from the query, so a retry after a slow-looking failure
    # joins the run already in flight instead of paying twice.
    idem_key = str(body.get("idempotency_key") or "").strip()[:120]
    _kind_hint = str(body.get("run_kind") or "").strip().lower()
    if not idem_key and body.get("research"):
        idem_key = "research:" + _req_digest(query)
    if not idem_key and _kind_hint == "authoring":
        # The Authoring UI sends a key, but a direct API caller may not.
        # Derive one server-side so a retry that dropped the key still joins
        # the run rather than paying for a second one. The setup panel is part
        # of the identity: the same brief as PDF and as PPTX is a different
        # run.
        idem_key = "authoring:" + _req_digest(
            query + "\n" + repr(sorted((_doc_setup or {}).items())))
    # Atomic lookup-and-claim: the old lookup here followed by a claim further
    # down let two simultaneous submits both start.
    existing = _inflight_run_claim(idem_key, conversation_id)
    if existing:
        return StreamingResponse(_dedup_stream(existing), **_SSE_HEADERS)
    # What KIND of run this is, decided by the caller rather than inferred later
    # from whatever skills the run emitted. The Research page and the chat
    # hand-off declare "research"; the Authoring page declares "authoring"; the
    # chat's own DAG mode declares "chat". It is persisted at run start, so a
    # topic appears in the Research sidebar the moment it begins instead of
    # vanishing when the user looks away.
    run_kind = str(body.get("run_kind") or "").strip().lower()
    if run_kind not in ("research", "authoring", "chat"):
        run_kind = "research" if body.get("research") else ""
    # The operator's research depth travels as a PLANNER-ONLY note, never inside
    # the query: a worker that read "deliver a final report as Markdown" wrote
    # one instead of its JSON contract and failed. See _research_depth_note.
    run_note = ""
    if run_kind == "research":
        _depth = str(body.get("depth") or "standard").strip().lower()
        if _depth not in ("quick", "standard", "deep"):
            _depth = "standard"
        run_note = _research_depth_note(_depth)
    # This is the endpoint the Research and Authoring pages use, and it was
    # the one place a long, discursive exchange could happen without the
    # profile ever seeing it. "Walk me through how you decided" is exactly the
    # kind of turn worth recording, and it arrives here.
    _capture_observation_bg(query, conversation_id)
    try:
        # `doc_setup` is validated in chat() and consumed in _stream_run, which
        # is where the orchestrator is actually driven. Threading it as an
        # argument is the only thing that works: assigning _doc_setup here and
        # reading it in _stream_run raised `NameError: _doc_setup` inside the
        # run thread, so every authoring request failed instantly with an
        # empty answer and "the run finished without producing a file".
        return _stream_run(query, conversation_id, request=req,
                           idem_key=idem_key, doc_setup=_doc_setup,
                           run_note=run_note, run_kind=run_kind)
    except BaseException:
        # Never leave a key claimed by a run that never started.
        _inflight_run_release(idem_key)
        raise
@app.post("/api/conversations/adopt")
async def adopt_conversation(req: Request):
    """Give a legacy (conversation-less) session a conversation id so it can
    be resumed. Body: {"session_id": "s8-..."}. Idempotent per session."""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    sid = _body_str(body, "session_id")
    if sid is None:
        return JSONResponse(status_code=400,
                            content={"error": "session_id must be a string"})
    if not sid:
        return JSONResponse(status_code=400, content={"error": "session_id required"})
    # The session must actually exist. This endpoint writes the
    # conversation->session mapping that resolve_session() consults, so
    # adopting an unknown id persisted a dangling entry and answered
    # `adopted: true` — the caller believed a nonexistent run became
    # resumable. 404 is the truthful answer.
    try:
        from persistence import SESSIONS_ROOT as _SESS_ROOT
        if not (_SESS_ROOT / sid).is_dir():
            return JSONResponse(status_code=404,
                                content={"error": f"unknown session {sid}"})
    except Exception as e:
        return JSONResponse(status_code=500,
                            content={"error": f"cannot verify session: {e}"[:150]})
    import uuid as _uuid
    with _CONV_LOCK:
        data = _conv_load()
        for cid, mapped in data.items():
            if mapped == sid:
                return {"conversation_id": cid, "session_id": sid, "adopted": False}
        cid = f"c-{_uuid.uuid4().hex[:8]}"
        data[cid] = sid
        _conv_save(data)
    return {"conversation_id": cid, "session_id": sid, "adopted": True}
# ── Computer-Use approval UI (charter §13) ──────────────────────────────────
@app.post("/api/computer/approvals/{approval_id}")
async def computer_approve(approval_id: str, req: Request):
    """Resolve a pending approval: {"approve": true|false}."""
    from computer_use import safety
    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    approve = bool(body.get("approve", False))
    result = safety.shared_gates().resolve(approval_id, approve)
    if result is None:
        return {"status": "not_found"}
    # Consistent with ComputerUse.resolve(): approved/rejected, not generic ok.
    return {"status": result.get("status", "approved" if approve else "rejected"),
            "action": result["action"], "approve": result.get("approve", approve)}
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
    import re as _re
    if not _re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,128}$", run_id or ""):
        return JSONResponse(status_code=400, content={"error": "invalid run_id"})
    root = (DEFAULT_RECORD_ROOT / run_id).resolve()
    # Confine to the recordings root even for odd-but-legal names.
    try:
        root.relative_to(DEFAULT_RECORD_ROOT.resolve())
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "invalid run_id"})
    if not root.exists():
        return {"error": "run not found"}
    turns = []
    for t in sorted(root.iterdir()):
        if t.is_dir():
            calls = []
            for f in t.iterdir():
                if f.is_file() and f.suffix == ".json":
                    try:
                        calls.append(json.loads(f.read_text(encoding="utf-8")))
                    except Exception:
                        pass
            turns.append({"turn": t.name, "calls": calls})
    return {"id": run_id, "turns": turns}
@app.post("/api/computer/replay/{run_id}")
async def computer_replay(run_id: str):
    """Replay a recorded trajectory against the same starting UI state."""
    import re as _re
    if not _re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,128}$", run_id or ""):
        return JSONResponse(status_code=400, content={"error": "invalid run_id"})
    # Replay re-executes recorded clicks/keystrokes LIVE, so it
    # carries the same opt-in the engine enforces (engine.py
    # refuses to run when COMPUTER_USE_ENABLED is off, and every
    # engine path is a no-op in dry-run) — an ungated endpoint
    # would be a bypass of both controls.
    import os as _os
    if not _os.environ.get("COMPUTER_USE_ENABLED", "false").lower() == "true":
        return JSONResponse(
            status_code=403,
            content={"error": "computer-use is disabled. "
                              "Set COMPUTER_USE_ENABLED=true to opt in."})
    if (_os.environ.get("COMPUTER_USE_MODE", "dry-run").lower()
            != "live"):
        return JSONResponse(
            status_code=400,
            content={"error": "replay executes real desktop actions; "
                              "it requires COMPUTER_USE_MODE=live"})
    from computer_use.core.recording import Recorder, DEFAULT_RECORD_ROOT
    rec = Recorder(run_id)
    # Recorder() mints a FRESH output dir (run-<id>-<now>) — replay must
    # target the EXISTING run directory, not the new empty one.
    result = rec.replay(trajectory_dir=str(DEFAULT_RECORD_ROOT / run_id))
    if result is None:
        return {"status": "error", "message": "replay failed"}
    return {"status": "ok", "result": result}
# ── SPA fallback ────────────────────────────────────────────────────────────
# Registered LAST so every real route wins. Without it an unmatched path
# answered 404 application/json `{"detail":"Not Found"}`, so a typo'd URL or a
# stale bookmark showed raw JSON, and the client's own `path="*"` catch-all
# was unreachable because the server never served the shell for it.
#
# Only GET/HEAD, and only when the path is not an API call: an unknown
# `/api/...` must still 404 as JSON so a broken client sees a real error
# rather than an HTML document it will try to parse.
@app.api_route("/{spa_path:path}",
               methods=["GET", "HEAD"],
               include_in_schema=False)
async def spa_fallback(spa_path: str):
    p = "/" + spa_path.lstrip("/")
    # `/assets/` belongs here, at ANY depth. A page route with a trailing slash
    # makes the browser resolve the shell's relative `./assets/app.js` against
    # the route directory, so `/documents/` asks for
    # `/documents/assets/app.js`; answering that with the HTML shell is a 200
    # text/html, which a module loader refuses to execute - a permanently blank
    # console with no error anywhere. A miss must be a real 404, so match the
    # segment anywhere in the path, not just as the prefix.
    segs = p.split("/")
    if "/assets/" in p + "/" or "api" in segs or "v1" in segs:
        return JSONResponse(status_code=404,
                            content={"detail": "Not Found", "path": p})
    if _SPA_INDEX.exists() and p.endswith("/"):
        # Same root cause, fixed at the source as well: make the shell's own
        # references absolute so a trailing slash cannot rebase them.
        return RedirectResponse(url=p.rstrip("/") or "/", status_code=308)
    if not _SPA_INDEX.exists():
        return JSONResponse(status_code=404,
                            content={"detail": f"no route for {p}"})
    # Serve the shell through the injector, NOT the raw file. This used to
    # return `FileResponse(_SPA_INDEX)`, which is byte-identical to dist and
    # therefore has no `aria-token` meta tag: every route without its own
    # `@app.get` handler - /documents, /code, and any route added later, plus
    # every deep link and refresh - loaded a console that could not
    # authenticate, so each /api call 403'd and the page rendered empty. Only
    # the handful of explicitly declared routes worked, which made it look
    # like a broken Documents page rather than a broken shell.
    return _spa_shell()
if __name__ == "__main__":
    import uvicorn
    # Mirror stdout/stderr into logs/agent.out + logs/agent.err so the
    # Console page's event feed has a server tail no matter how we were
    # launched (shell redirect, service wrapper, double-clicked .bat).
    try:
        sys.stdout = _Tee(sys.stdout, ROOT / "logs" / "agent.out")
        sys.stderr = _Tee(sys.stderr, ROOT / "logs" / "agent.err")
        _LIVE_OUT = sys.stdout
    except Exception:
        pass
    # Bind loopback by default: the agent can approve desktop actions and
    # spend LLM budget, so it must never listen on LAN/Wi-Fi interfaces
    # unless the operator explicitly opts in (ARIA_HOST=0.0.0.0, and then
    # set ARIA_API_TOKEN too).
    _host = os.environ.get("ARIA_HOST", "127.0.0.1").strip() or "127.0.0.1"
    # Per-launch auth token. Generated here, on a real launch, so enforcement is
    # inert for in-process test clients and only becomes real when a server
    # actually starts serving. Regenerated every launch, so a captured token
    # dies with the process that issued it.
    try:
        import auth as _auth
        if os.environ.get("ARIA_AUTH", "on").strip().lower() in ("off", "0", "false"):
            _auth.disable()
            print("[auth] DISABLED by ARIA_AUTH=off - every /api route is "
                  "unauthenticated. Do not do this on a shared machine.")
        else:
            tok = _auth.configure()
            ok, why = _auth.assert_safe_binding(_host)
            print(f"[auth] per-launch token active ({len(tok)} chars), "
                  f"required as X-Aria-Token on /api/*; {why}")
            if not ok:
                # Refusing to start is the point. Binding every interface with
                # no token makes the whole API an open network endpoint, and a
                # warning that scrolls past is not a control.
                raise SystemExit(f"[auth] refusing to start: {why}")
    except SystemExit:
        raise
    except Exception as e:
        print(f"[auth] could not configure token ({e!r}); /api/* will be "
              f"UNAUTHENTICATED", file=sys.stderr)
    # Startup reconciliation: any run whose graph still claims a node is
    # `running` was, by definition, killed before it could mark that node
    # terminal — this process is the first thing that can know. Written to
    # disk so the DAG and every summary agree without re-deriving it from
    # mtimes on each request. Read-only endpoints keep their mtime heuristic
    # as a second line of defence.
    try:
        threading.Thread(target=_reconcile_orphaned_runs, daemon=True).start()
    except Exception as e:
        print(f"[startup] run reconciliation skipped: {e!r}")
    uvicorn.run("agent_server:app", host=_host, port=8500, reload=False)
