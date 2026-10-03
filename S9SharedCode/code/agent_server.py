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

The front-end (console-frontend/, the React SPA) streams the agent's
reasoning log and final answer back as Server-Sent Events.

Endpoints (83 routes; the full list is the code — this is the shape):
  GET  /                 → the React console (dist/index.html, legacy web/ fallback)
  GET  /console /research /runs /memory /scheduler /skills /apps /ledger /mission /settings
  GET  /api/health      → {agent, gateway_up, spa_built}
  POST /api/chat        → SSE frames started/log/status/meta/done/error
  POST /api/chat/simple → {answer, conversation_id} (one direct LLM call)
  POST /api/chat/simple/stream → SSE frames started/status/delta/done/error
  POST /api/tts         → WAV audio for a piece of text (Kokoro)

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
                              JSONResponse)

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
        # 403, not 401: there is no login flow to send the client to, and a
        # 401 invites a credential prompt for a machine-local API.
        return JSONResponse(status_code=403, content={
            "error": "missing or invalid X-Aria-Token. This agent requires the "
                     "per-launch token injected into the console shell."})

    bad = _auth.cross_origin_write(request.headers, request.method)
    if bad:
        return JSONResponse(status_code=403, content={"error": bad})

    return await call_next(request)


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


def resolve_session(conversation_id: str | None) -> str:
    """Return the session_id for a conversation, creating one if needed."""
    import uuid as _uuid
    if not conversation_id:
        return f"s8-{_uuid.uuid4().hex[:8]}"
    with _CONV_LOCK:
        data = _conv_load()
        sid = data.get(conversation_id)
        if not sid:
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


def _notify_telegram(text: str) -> None:
    if not _NOTIFY_CHAT:
        return
    try:
        import httpx as _hx
        _hx.post(
            f"{_GW_NOTIFY}/v1/channels/telegram/send",
            json={"to": _NOTIFY_CHAT, "text": text[:4096],
                  "agent": "notify"},
            timeout=15,
        )
    except Exception:
        pass


def notify_task_done(query: str, answer: str, cost: float,
                     session_id: str) -> None:
    """Push a completion notification to Telegram (opt-in ONLY).

    DISABLED by default. Set AGENT_TELEGRAM_NOTIFY=true to receive a
    Telegram summary after each agent response / scheduled task. Without
    the explicit opt-in, no message is sent. (Previously it fired on every
    chat response whenever TELEGRAM_BOT_TOKEN was set, which was noisy.)

    `cost` is the wall-clock time of the run in seconds (named `cost` for
    backwards-compat with the public API / wiring check)."""
    if os.environ.get("AGENT_TELEGRAM_NOTIFY", "").lower() != "true":
        return
    if not _NOTIFY_CHAT:
        return
    snippet = (answer or "").strip().replace("\n", " ")
    snippet = snippet[:280] + ("…" if len(snippet) > 280 else "")
    msg = (
        f"OK Agent task complete\n"
        f"time± {cost:.1f}s · session {session_id}\n"
        f"query {query[:120]}\n"
        f"🎙 {snippet}"
    )
    threading.Thread(target=_notify_telegram, args=(msg,), daemon=True).start()


# ── notification log (in-memory + on-disk) ──────────────────────────────────
# Scheduled tasks and long agent runs push a short summary here so the UI
# notification panel can show "what happened recently" even when the operator
# wasn't watching. Bounded to the last 200 entries to keep the file small.
_NOTIF_PATH = STATE_DIR / "notifications.json"
_NOTIF_LOCK = threading.Lock()
_NOTIF_MAX = 200


def _notif_load() -> list:
    return _read_json(_NOTIF_PATH, list)


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
    """Return the most recent notifications (newest first)."""
    with _NOTIF_LOCK:
        items = _notif_load()
    return list(reversed(items[-limit:]))


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
    try:
        stat = _SPA_INDEX.stat()
        key = (stat.st_mtime_ns, stat.st_size, meta)
    except OSError:
        return FileResponse(str(_SPA_INDEX))
    global _spa_cache
    if _spa_cache and _spa_cache[0] == key:
        return Response(_spa_cache[1], media_type="text/html")
    try:
        html = _SPA_INDEX.read_text(encoding="utf-8")
    except OSError:
        return FileResponse(str(_SPA_INDEX))
    if meta and TOKEN_META_NAME not in html:
        html = html.replace("</head>", f"  {meta}\n</head>", 1)
    _spa_cache = (key, html)
    return Response(html, media_type="text/html")


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
async def get_feedback(node_id: str):
    """Latest thumbs vote for a node (1 | -1 | 0 when none)."""
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
            if e.get("node_id") == node_id:
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
    nid = (body.get("node_id") or "").strip()
    try:
        vote = int(body.get("vote") or 0)
    except Exception:
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
        if t <= 0:
            t = _t.time()
        evs.append({"t": t,
                    "iso": _dt.fromtimestamp(t).strftime("%H:%M:%S"),
                    "level": lv, "src": src, "msg": str(msg)[:300]})

    # 1. run sessions (newest first)
    try:
        from persistence import SessionStore, list_sessions as _list_sids
        for sid in list(reversed(_list_sids()))[:40]:
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
                _push(mt, lv, f"run·{sid[:8]}",
                      f"{q or '(no query)'} — " +
                      ", ".join(f"{v}→{k}" for k, v in sorted(statuses.items())) or "no nodes")
            except Exception:
                continue
    except Exception:
        pass

    # 2. scheduler jobs (next firing)
    try:
        _sched = json.loads((STATE_DIR / "schedules.json").read_text(encoding="utf-8-sig"))
        _items = _sched.values() if isinstance(_sched, dict) else _sched
        for j in _items:
            if not isinstance(j, dict):
                continue
            if j.get("enabled", True):
                _push(j.get("next_fire") or 0, "sched", "scheduler",
                      f"next: {(j.get('query') or '')[:120]} ({j.get('when') or j.get('recurring') or '?'})")
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
    _ERR_KW = ('ERROR', 'Traceback (most recent call last)', 'CRITICAL')
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
                if _lv == "err" or _absorbed or any(_k in _ln for _k in _ERR_KW):
                    _l = "err"
                elif "tool" in _ln.lower():
                    _l = "tool"
                else:
                    _l = "info"
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
    if conversation_id:
        session = (conversation_id if conversation_id.startswith("ct-")
                   else resolve_session(conversation_id))
    if not session:
        return {"rows": [], "totals": {"in_tokens": 0, "out_tokens": 0,
                                       "dollars": 0.0, "calls": 0}}

    turns = _read_turn_costs(session)
    if conversation_id and conversation_id != session:
        # Merge the thread's own ledger when it differs from the resolved
        # session, so the panel isn't blank for chat-only threads. Only
        # ct-* threads have their own ledger file: c-* conversations resolve
        # to s8-* (already read above), and reading sessions/c-*/… would be
        # a phantom path that can never exist.
        if conversation_id.startswith("ct-"):
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
    }


@app.get("/api/cost/by_skill")
async def cost_by_skill(conversation_id: str | None = None):
    """Per-skill spend rollup for the Ledger page. Scoped to one conversation
    when given, else summed across all sessions' turn ledgers."""
    sids: list[str] = []
    if conversation_id:
        try:
            # ct-* threads have no graph session to resolve — resolve_session
            # would only mint a phantom s8 id. They are keyed directly below.
            if not conversation_id.startswith("ct-"):
                sids = [resolve_session(conversation_id)]
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
        for _t in _read_turn_costs(_sid):
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
    turns = []
    for _sid in keys[:200]:
        for _t in _read_turn_costs(_sid)[-50:]:
            _tt = _t.get("totals") or {}
            turns.append({"ts": _t.get("ts", 0), "session": _sid,
                          "query": (_t.get("query") or "")[:120],
                          "calls": _tt.get("calls", 0),
                          "usd": round(_tt.get("usd", 0.0), 6)})
    turns.sort(key=lambda x: x["ts"], reverse=True)
    return {"rows": rows,
            "turns": turns[:200],
            "totals": {"dollars": round(sum(r["dollars"] for r in rows), 6),
                       "calls": sum(r["calls"] for r in rows),
                       "skills": len(rows)}}


@app.get("/api/audit")
async def audit_export(redact: bool = True):
    """Export the computer-use audit log with secrets redacted by default.

    Returns the redacted log lines so the dashboard / compliance tooling can
    show a safe, shareable record of every gated action.
    """
    from computer_use import safety

    lines = safety.SafetyGates.export_audit(redact=redact)
    return {"lines": lines, "count": len(lines)}


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
            sessions.append({
                "session_id": sid,
                "conversation_id": inv.get(sid),
                "query": q,
                "topic": plan_topic or _topic_of(q),
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

    # Same newest-first mtime ordering as /api/sessions (see there).
    def _mtime(sid: str) -> float:
        try:
            return (_SESS_ROOT / sid).stat().st_mtime
        except OSError:
            return 0.0
    for sid in sorted(_list_sids(), key=_mtime, reverse=True)[:limit]:
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
                "updated": mtime,
                "updated_ago": round(_t.time() - mtime, 1) if mtime else None,
            })
        except Exception:
            continue
    return {"runs": runs,
            "totals": {"runs": len(runs), "nodes": nodes_total,
                       "dollars": round(dollars, 6),
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

    def _trim(x, depth=0):
        if isinstance(x, str):
            return x if len(x) <= 2000 else x[:2000] + f"\n…[{len(x) - 2000} chars truncated]"
        if isinstance(x, dict) and depth < 6:
            return {k: _trim(v, depth + 1) for k, v in x.items()}
        if isinstance(x, list) and depth < 6:
            return [_trim(v, depth + 1) for v in x[:50]]
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
    """Current enable/disable guard state for the Skills page."""
    from skills import _disabled_tools
    return {"disabled": sorted(_disabled_tools())}


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
    name = (body.get("tool") or "").strip()
    enabled = body.get("enabled", True)
    catalog = _TOOL_CATALOG if isinstance(_TOOL_CATALOG, dict) else {}
    if not name or name not in catalog:
        return JSONResponse(status_code=400, content={"error": f"unknown tool '{name}'"})
    import json as _json
    path = ROOT / "state" / "tools_disabled.json"
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
        path.write_text(_json.dumps({"tools": sorted(disabled)}, indent=2), encoding="utf-8")
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
    name = (body.get("name") or "").strip()
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


@app.post("/api/notifications")
async def post_notification(req: Request):
    """Append a notification entry. Body: {kind, text, session_id?, conversation_id?}."""
    try:
        body = await req.json()
    except Exception:
        return {"status": "error", "message": "invalid JSON body"}
    kind = (body.get("kind") or "info").strip()
    text = (body.get("text") or "").strip()
    if not text:
        return {"status": "error", "message": "text is required"}
    entry = add_notification(
        kind, text,
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
        async with httpx.AsyncClient(timeout=_GW_UPLOAD_TIMEOUT) as c:
            r = await c.post(f"{_GW_VOICE}/v1/documents", content=content,
                             headers={"Content-Type": ctype})
        try:
            return json.loads(r.text)
        except Exception:
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


# ── document context for chat (deterministic retrieval) ─────────────────────
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
        async with httpx.AsyncClient(timeout=_DOC_CTX_TIMEOUT_S) as c:
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
        return "", 0

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
            "It is a partial extract, not the whole document.\n\n"
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
    try:
        from skills import tool_payload as _tp
        # Aria's tool payload shape is a top-level `name` + `input_schema`,
        # NOT the OpenAI `{"function": {"name": ...}}` nesting. Reading the
        # nested form silently yielded an empty list, so the capability
        # document claimed toolCalling: false while chat was calling tools on
        # every request.
        names = [t.get("name") for t in (_tp([
            "web_search", "fetch_url", "search_knowledge",
            "recall_preferences", "list_scheduled",
        ]) or []) if isinstance(t, dict) and t.get("name")]
    except Exception:
        names = []
    import agui as _agui
    caps = _agui.capabilities(streaming=True, tools=names)
    caps["custom"] = {"aguiEndpointEnabled": enabled,
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
        tools_payload = _tp([
            "web_search", "fetch_url", "search_knowledge",
            "recall_preferences", "list_scheduled", "calendar_query",
            "gmail_query", "github_query", "slack_history", "notion_query",
        ]) if tools_on else None
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

    messages = ([{"role": "system", "content": _CHAT_SYSTEM}]
                + history_messages
                + [{"role": "user", "content": query}])
    try:
        messages, doc_hits = await _with_doc_context(
            messages, query, _conversation_doc_ids(thread_id))
    except Exception as e:
        # Retrieval is best-effort; never fail the turn on it.
        print(f"[agui] doc context unavailable ({e!r})")
        doc_hits = 0

    try:
        from gateway import LLM, ensure_gateway
        ensure_gateway()
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "error": f"gateway unavailable: {e}"})

    cost_before = _session_cost_breakdown(thread_id)
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
                emit(_agui.tool_call_args(call_id, "{}"))
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
                            doc_ids=_conversation_doc_ids(thread_id),
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
            answer = (reply.get("text") or "").strip() or "(empty answer)"
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
                thread["updated"] = _time.time()
                if not thread.get("title"):
                    thread["title"] = query[:60]
                data[thread_id] = thread
                if len(data) > _CHAT_MAX_THREADS:
                    for _cid in sorted(
                            data, key=lambda c: data[c].get("updated", 0)
                    )[:len(data) - _CHAT_MAX_THREADS]:
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


def _conversation_doc_ids(conversation_id: str | None) -> set[str] | None:
    """Which documents a conversation may draw on.

    `None` means no filtering (the caller is not filtering at all); an empty
    set means "no documents", which is what the per-conversation toggle and a
    disabled document both produce.

    The toggle used to be ignored here: this took a `conversation_id`, never
    read it, and returned every enabled document in the registry. The console
    wrote `use_documents` and rendered the resulting state faithfully, so the
    button looked like it governed retrieval while changing nothing at all.

    Docs-off is answered from the stored preference alone and never consults
    the registry, so it holds even when the gateway is unreachable.
    """
    if not conversation_id:
        return None
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

_CODE_ROOT = Path(__file__).resolve().parent.parent.parent

_CODE_ROOTS: tuple[str, ...] = ("S9SharedCode/code", "llm_gatewayV9")

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
    """Resolve `rel` (relative to `_CODE_ROOT`) inside the Code roots, or None.

    Two independent checks, because either alone is insufficient:
      1. the path must land under one of the fixed roots, and
      2. that test must run on the RESOLVED path - otherwise a symlink inside
         the tree reads whatever it points at, which may be outside.
    `rel` is relative to the repo root, not to a root itself, so the roots are
    only ever used for the containment test.
    """
    if not _code_rel_ok(rel):
        return None
    root = _CODE_ROOT.resolve()
    target = (root / rel).resolve()
    for root_rel in _CODE_ROOTS:
        allowed = (root / root_rel).resolve()
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
    base = _code_abs(rel) if rel else _CODE_ROOT
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
    """The fixed workspace roots, for the explorer's breadcrumb."""
    out = []
    for r in _CODE_ROOTS:
        p = _CODE_ROOT / r
        out.append({"name": r, "exists": p.is_dir()})
    return {"root": str(_CODE_ROOT), "roots": out,
            "max_bytes": _CODE_MAX_BYTES}

@app.get("/api/code/tree")
async def code_tree(path: str = ""):
    """List one directory inside the workspace."""
    rel = (path or "").strip().lstrip("/")
    if rel in (".", "/"):
        rel = ""
    return _code_listing(rel)


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
    files: list[str] = []
    truncated = False
    queue: list[Path] = [(_CODE_ROOT / r) for r in _CODE_ROOTS]
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
                rel = child.resolve().relative_to(_CODE_ROOT.resolve())
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
    if size > _CODE_MAX_BYTES:
        return JSONResponse(status_code=413, content={
            "error": f"file is {size} bytes; the limit is {_CODE_MAX_BYTES}"})
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


@app.get("/api/code/search")
async def code_search(q: str = "", limit: int = 200):
    """Bounded literal search across the servable workspace.

    Substring, case-insensitive, no regex: the caller is a browser text box,
    and handing it regex means a way to make the server do work proportional to
    a pathological pattern. Same path confinement and extension allow-list as
    the file endpoint, and a hard cap on files scanned, bytes read and matches
    returned so one search cannot stall the console.
    """
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
    files: list[str] = []
    queue: list[Path] = [(_CODE_ROOT / r) for r in _CODE_ROOTS]
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
                files.append(child.resolve().relative_to(_CODE_ROOT.resolve()).as_posix())
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
    if kind not in ("fact", "preference", "tool_outcome", "scratchpad") or not descriptor:
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
    try:
        import time as _time
        d = await _gw_json("/v1/memory/remember", method="POST", json={
            "kind": kind, "descriptor": descriptor,
            "keywords": body.get("keywords") or [],
            "value": body.get("value") or {},
            "source": body.get("source") or "dashboard",
            "run_id": body.get("run_id") or f"dashboard-{int(_time.time())}",
            "session_id": body.get("session_id") or None})
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
    text = (body.get("text") or "").strip()
    if not text:
        return {"status": "error", "message": "text is required"}
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
     "conversation_id"?: str, "notify"?: bool}."""
    from scheduler import schedule

    try:
        body = await req.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})
    query = (body.get("query") or "").strip()
    when = (body.get("when") or "").strip()
    if not query or not when:
        return JSONResponse(status_code=400,
                            content={"error": "query and when are required"})
    if len(query) > _CHAT_DAG_MAX_QUERY:
        return JSONResponse(status_code=400, content={
            "error": f"query too long (max {_CHAT_DAG_MAX_QUERY} chars)"})
    try:
        sid = schedule(query, when,
                       conversation_id=body.get("conversation_id"),
                       notify=bool(body.get("notify", True)))
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:200]})
    return {"status": "ok", "id": sid}


@app.get("/api/schedule")
async def list_scheduled():
    """List all scheduled tasks."""
    from scheduler import list_schedules

    return {"schedules": list_schedules()}


@app.delete("/api/schedule/{sid}")
async def cancel_scheduled(sid: str, hard: bool = False):
    """Cancel a scheduled task by id. Soft-cancel (enabled=false, stays
    visible greyed-out) by default; ?hard=true hard-deletes the row."""
    if hard:
        from scheduler import remove
        return {"status": "ok" if remove(sid) else "not_found"}
    from scheduler import cancel

    return {"status": "ok" if cancel(sid) else "not_found"}


@app.post("/api/schedule/purge")
async def purge_schedules(max_age_days: float = 7):
    """Hard-delete disabled one-shot schedules older than the cutoff."""
    from scheduler import purge_disabled

    return {"status": "ok", "purged": purge_disabled(max_age_days)}


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
    name = (body.get("name") or "").strip()
    query = (body.get("query") or "").strip()
    if not name or not query:
        # Previously a 200 with {"status":"error"} — a caller checking the
        # HTTP status alone believed the template was saved.
        return JSONResponse(status_code=400,
                            content={"error": "name and query are required"})
    tpl = save(name, query, body.get("vars"))
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


async def _gw_bytes(path: str, *, method: str = "GET", json: dict | None = None,
                    files: dict | None = None, timeout: float = 120.0):
    """POST/GET bytes (audio) from the gateway voice service."""
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as c:
        if method == "POST":
            r = await c.post(f"{_GW_VOICE}{path}", json=json, files=files)
        else:
            r = await c.get(f"{_GW_VOICE}{path}")
        return r.status_code, r.headers.get("content-type", ""), r.content


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
        async with httpx.AsyncClient(timeout=timeout) as c:
            if method == "POST":
                r = await c.post(f"{_GW_VOICE}{path}", json=json)
            elif method == "GET":
                r = await c.get(f"{_GW_VOICE}{path}")
            else:
                # DELETE / PUT / PATCH must hit the gateway with the real
                # verb — never silently downgrade to GET (that turned the
                # memory wipe into a read).
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
        with _hx.Client(timeout=5) as _c:
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


def _turn_cost_path(session_id: str) -> "Path":
    # Lightweight chat threads are keyed ct-* and have no graph dir, so park
    # their spend under threads/: writing into sessions/ would create a
    # phantom session dir that list_sessions() surfaces as an empty run.
    # Everything else (s8-* runs, plus test ids) stays under sessions/.
    if session_id.startswith("ct-"):
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
    if not (body.get("text") or "").strip():
        return StreamingResponse(
            iter([b""]), media_type="audio/wav", status_code=400
        )
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
        if up is None:
            return {"error": "no file field in form"}
        blob = await up.read()
        if not blob:
            return {"error": "empty audio file"}
        if len(blob) > 25_000_000:
            return {"error": "audio too large (25MB cap)"}
        filename = getattr(up, "filename", "") or "audio.wav"
        ctype = getattr(up, "content_type", "") or "audio/wav"
    except Exception as e:
        return {"error": f"unreadable upload: {type(e).__name__}: {e}"}
    import httpx
    try:
        async with httpx.AsyncClient(timeout=180.0) as c:
            r = await c.post(
                f"{_GW_VOICE}/v1/stt",
                files={"file": (filename, blob, ctype)})
        try:
            data = r.json()
            return data if isinstance(data, dict) else {"error": "bad gateway reply"}
        except ValueError:
            return {"error": r.text[:200] or f"gateway HTTP {r.status_code}"}
    except Exception as e:
        return {"error": f"voice gateway unreachable: {type(e).__name__}: {e}"}


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

    return {
        "approvals": get_computer_use().list_approvals(),
        "capabilities": await _cached_capabilities(),
        # P2.2: surface the current mode so the approval panel can show
        # dry-run vs live and warn the user nothing will execute in dry-run.
        "mode": _os.getenv("COMPUTER_USE_MODE", "dry-run").lower(),
    }


# ── lightweight chat (no DAG) ────────────────────────────────────────────
# The Chat page is a minimal chatbot: one direct gateway LLM call per turn,
# no orchestrator graph, no SessionStore files, no turn-cost ledger — so
# lightweight threads never appear in Runs/Memory/Ledger. Research keeps
# the full Executor DAG via /api/chat. Thread history lives in its own
# state/chat_threads.json store (bounded), purely for multi-turn context.
_CHAT_THREADS_PATH = STATE_DIR / "chat_threads.json"
_CHAT_THREADS_LOCK = threading.Lock()
_CHAT_MAX_THREADS = 50
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
_CHAT_SYSTEM = (
    "You are Aria, a concise chat assistant. Answer directly in Markdown. "
    "Keep replies tight unless the user asks for depth. "
    "You have read-only tools (web_search, fetch_url, search_knowledge, "
    "recall_preferences, list_scheduled, calendar_query, gmail_query, "
    "github_query, slack_history, notion_query) — use them instead of "
    "guessing. The user's OWN accounts are among them: if they ask what is on "
    "their calendar, what is in their inbox, what reminders exist, what is in "
    "a chat channel or a wiki, or what is in a repository, call the matching "
    "tool. Never answer such a question from general knowledge and never "
    "substitute web_search for an account query. "
    # Retrieved documents. The retrieval is done by the server before this
    # prompt is even sent; without saying so, the model treats the block as an
    # unexplained wall of text and tends to ignore it in favour of a confident
    # answer from its own weights.
    "You may also be given REFERENCE MATERIAL retrieved from the user's own "
    "uploaded documents. When it is present, prefer it over your own knowledge "
    "for anything it covers, and cite it as [n]. Use search_knowledge only to "
    "go deeper or to answer a follow-up it did not cover. If no reference "
    "material is provided, do NOT claim the user has no documents - just "
    "answer, and mention the Documents page if they seemed to expect one. "
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


def _chat_threads_load() -> dict:
    return _read_json(_CHAT_THREADS_PATH, dict)


def _chat_threads_save(data: dict) -> None:
    try:
        _write_json_atomic(_CHAT_THREADS_PATH, data)
    except Exception:
        pass


@app.get("/api/chat/threads")
async def list_chat_threads(limit: int = 50):
    """Lightweight chat threads, newest first. No DAG sessions involved."""
    with _CHAT_THREADS_LOCK:
        data = _chat_threads_load()
    items = [
        {"conversation_id": cid,
         "title": (t.get("title") or "(untitled)")[:90],
         "updated": t.get("updated") or 0,
         "turns": len(t.get("messages", [])) // 2}
        for cid, t in data.items() if isinstance(t, dict)
    ]
    items.sort(key=lambda x: x["updated"], reverse=True)
    limit = max(1, min(limit, 100))
    return {"threads": items[:limit]}


@app.get("/api/chat/threads/{conversation_id}")
async def get_chat_thread(conversation_id: str):
    """Full message history for one lightweight thread."""
    with _CHAT_THREADS_LOCK:
        t = _chat_threads_load().get(conversation_id)
    if not isinstance(t, dict):
        return JSONResponse(status_code=404, content={"error": "unknown thread"})
    return {"conversation_id": conversation_id,
            "title": t.get("title") or "(untitled)",
            "messages": t.get("messages") or []}


@app.delete("/api/chat/threads/{conversation_id}")
async def delete_chat_thread(conversation_id: str):
    with _CHAT_THREADS_LOCK:
        data = _chat_threads_load()
        if conversation_id not in data:
            return JSONResponse(status_code=404, content={"error": "unknown thread"})
        del data[conversation_id]
        _chat_threads_save(data)
    return {"status": "ok"}


@app.post("/api/chat/simple")
async def chat_simple(req: Request):
    """One direct gateway LLM call (with read-only tools), billed to the
    thread. No Executor, no graph."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    query = (body.get("query") or "").strip()
    conversation_id = _conv_id(body)
    if not query:
        return JSONResponse(status_code=400, content={"error": "query required"})
    if len(query) > _CHAT_MAX_QUERY:
        return JSONResponse(status_code=400, content={
            "error": f"query too long (max {_CHAT_MAX_QUERY} chars)"})
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
            _pre[conversation_id] = {"title": query[:60], "updated": 0,
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
            _pre[conversation_id] = {"title": query[:60], "updated": 0,
                                     "messages": []}
            _chat_threads_save(_pre)
    _capture_preference_bg(query, conversation_id)

    # Snapshot this thread's gateway spend BEFORE the call so the ledger can
    # record exactly this reply. Lightweight chat used to be invisible to
    # the Ledger ("no cost ledger") — which silently under-reported spend on
    # the very path the Chat view uses by default.
    cost_before = _session_cost_breakdown(conversation_id)

    # Build the message list under the lock, then RELEASE it before the model
    # call. This used to hold a threading.Lock across a blocking sync HTTP
    # call and an `await`, inside an async def: every coroutine that touches
    # the thread store (the sidebar's GET /api/chat/threads) blocked for the
    # entire reply, and `Lock.acquire()` on the event loop blocks every other
    # task besides. The streaming twin already scopes it this way.
    with _CHAT_THREADS_LOCK:
        data = _chat_threads_load()
        if conversation_id not in data:
            data[conversation_id] = {"title": query[:60], "updated": 0,
                                     "messages": []}
        thread = data[conversation_id]
        history = thread.get("messages") or []
        messages = ([{"role": "system", "content": _CHAT_SYSTEM}]
                    + history[-(_CHAT_MAX_TURNS * 2):]
                    + [{"role": "user", "content": query}])
    # Deterministic document retrieval. This happens before the model sees the
    # turn, so an answer grounded in the user's own uploads does not depend on
    # the model choosing to call `search_knowledge`.
    messages, _doc_hits = await _with_doc_context(
        messages, query, _conversation_doc_ids(conversation_id))
    try:
        # Read-only tools so chat can answer current/external facts
        # (web_search, fetch_url, get_time, search_knowledge,
        # currency_convert) instead of guessing. Messaging, calendar,
        # file-write and computer tools stay on the Research path.
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
        # The read-only HALF of the tool catalog. This list is the only thing
        # that decides what chat can reach: everything absent here is
        # unreachable, so an account query ("what's on my calendar?") had no
        # tool to call and the model answered from general knowledge instead.
        # Write/send tools (delete_file, send_*, schedule_task, ...) are
        # deliberately excluded — chat stays non-mutating.
        _tools = _tool_payload([
            "web_search", "fetch_url",
            "search_knowledge", "recall_preferences", "remember_preference",
            "list_scheduled", "calendar_query", "gmail_query",
            "github_query", "slack_history", "notion_query",
        ]) if _chat_tools_on else None
        if _tools:
            try:
                from mcp_runner import run_with_tools as _run_with_tools
                import outcomes as _outcomes
                reply = await _run_with_tools(
                    messages=messages, tools_payload=_tools,
                    agent="chat", session_id=conversation_id,
                    max_tokens=2048, temperature=0.7,
                    doc_ids=_conversation_doc_ids(conversation_id),
                    on_outcome=lambda n, a, ok, t, lat: _outcomes.on_tool_outcome(
                        name=n, arguments=a, ok=ok, result_text=t,
                        latency_s=lat, session_id=conversation_id,
                        run_id=conversation_id))
            except Exception as _te:
                print(f"[chat] tool loop failed, plain-text fallback: "
                      f"{type(_te).__name__}: {_te}")
                reply = LLM().chat(messages=messages, agent="chat",
                                   session=conversation_id,
                                   max_tokens=2048, temperature=0.7)
        else:
            reply = LLM().chat(messages=messages, agent="chat",
                               session=conversation_id, max_tokens=2048,
                               temperature=0.7)
        answer = (reply.get("text") or "").strip() or "(empty answer)"
    except Exception as e:
        return JSONResponse(status_code=502, content={
            "error": f"chat failed: {e}"})

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
        thread["messages"] = (live + [{"role": "user", "content": query},
                                      {"role": "assistant", "content": answer}]
                              )[-(_CHAT_MAX_TURNS * 2):]
        thread["updated"] = _t.time()
        if not thread.get("title"):
            thread["title"] = query[:60]
        # Bound the store: drop oldest threads beyond the cap.
        if len(data) > _CHAT_MAX_THREADS:
            for _cid in sorted(data, key=lambda c: data[c].get("updated", 0)
                               )[:len(data) - _CHAT_MAX_THREADS]:
                del data[_cid]
        _chat_threads_save(data)

    # Persist this reply's spend under the thread id (threads/ tree), outside
    # the thread lock so a gateway round-trip can't stall other chat saves.
    try:
        delta = _session_cost_delta(conversation_id, cost_before)
        if delta:
            _record_turn_cost(conversation_id, query, delta)
    except Exception:
        pass
    return {"answer": answer, "conversation_id": conversation_id}


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


def _inflight_run_claim(key: str, session_id: str) -> None:
    if not key:
        return
    with _inflight_lock:
        now = _time.time()
        for k, (_sid, ts) in list(_inflight_runs.items()):
            if now - ts > _IDEM_TTL_S:
                _inflight_runs.pop(k, None)
        _inflight_runs[key] = (session_id, now)


def _inflight_run_release(key: str) -> None:
    if not key:
        return
    with _inflight_lock:
        _inflight_runs.pop(key, None)


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
                     "was not run again — watch that run in /runs to see its "
                     "result"),
               deduplicated=True)
    # Only release if this stream is the one that opened the claim. A genuine
    # duplicate must not free the key the original run still owns.
    if key:
        _inflight_run_release(key)


@app.post("/api/chat/simple/stream")
async def chat_simple_stream(req: Request):
    """SSE twin of /api/chat/simple. Frames: `started{conversation_id}`,
    `status{text}` (thinking… + live tool progress), `delta{text}`
    (answer chunks), `done{answer, conversation_id}`, `error{text}`.
    Thread persistence + cost accounting are identical to chat_simple.
    `media_type` must stay `text/event-stream` or gzip buffers the stream.
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    query = (body.get("query") or "").strip()
    conversation_id = _conv_id(body)
    if not query:
        return JSONResponse(status_code=400, content={"error": "query required"})
    if len(query) > _CHAT_MAX_QUERY:
        return JSONResponse(status_code=400, content={
            "error": f"query too long (max {_CHAT_MAX_QUERY} chars)"})
    # Idempotency for research submits. "Research this topic" has no client
    # guard, so a double-click (or a retry after a slow response that looked
    # like a failure) starts a SECOND paid run of the same question and the
    # sidebar then shows two identical topics. A caller-supplied key wins;
    # otherwise an identical query inside the window collapses onto the run
    # already in flight, and the UI is told which one it joined.
    idem_key = str(body.get("idempotency_key") or "").strip()[:120]
    if not idem_key and body.get("research"):
        idem_key = "research:" + _req_digest(query)
    existing_id = _inflight_run_lookup(idem_key) if idem_key else None
    if existing_id:
        # `**_SSE_HEADERS`, not `headers=`: that name is a kwargs bundle
        # ({"media_type": ..., "headers": {...}}), so passing it as a value
        # made StreamingResponse try to encode the nested dict as a latin-1
        # header and 500 on every duplicate submit.
        return StreamingResponse(_dedup_stream(existing_id), **_SSE_HEADERS)
    # Same pre-flight as chat_simple: fail fast with a 502 the UI can show
    # as an error, rather than opening a stream that immediately errors.
    try:
        from gateway import LLM, ensure_gateway as _ensure_gateway
        _ensure_gateway()
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
                                     "messages": []}
            _chat_threads_save(_pre)
        elif conversation_id not in _pre:
            # Bound, not replaced - see the note in `chat_simple`.
            _pre[conversation_id] = {"title": query[:60], "updated": 0,
                                     "messages": []}
            _chat_threads_save(_pre)
    # Claim the idempotency key only after the id exists, so a duplicate that
    # arrives while this one is still being set up joins rather than races.
    _inflight_run_claim(idem_key, conversation_id)
    # Same deterministic preference capture as chat_simple (see
    # _capture_preference_bg for why it does not rely on the model).
    _capture_preference_bg(query, conversation_id)
    cost_before = _session_cost_breakdown(conversation_id)

    async def gen():
        import asyncio as _aio
        import time as _t
        _t0 = _t.monotonic()
        pending: list[str] = []
        # Recorded in the `finally` below, which also runs when the client
        # disconnects. A stop mid-answer has already been billed by the
        # gateway; without this the spend silently vanished from the ledger
        # (the identical bug that was fixed on the /api/chat path).
        spent: dict | None = None

        def _emit(kind: str, payload: dict) -> None:
            if kind == "tool_call":
                pending.append(_sse("status",
                                    text=f"using {payload.get('name', 'tool')}…"))
            elif kind == "tool_result" and not payload.get("ok", True):
                pending.append(_sse("status",
                                    text=f"{payload.get('name', 'tool')} failed, continuing…"))

        yield _sse("started", conversation_id=conversation_id)
        with _CHAT_THREADS_LOCK:
            data = _chat_threads_load()
            if conversation_id not in data:
                data[conversation_id] = {"title": query[:60], "updated": 0,
                                         "messages": []}
            thread = data[conversation_id]
            history = thread.get("messages") or []
            messages = ([{"role": "system", "content": _CHAT_SYSTEM}]
                        + history[-(_CHAT_MAX_TURNS * 2):]
                        + [{"role": "user", "content": query}])
        # Same deterministic retrieval as the non-streaming path; a streamed
        # answer must not be less grounded than a buffered one.
        messages, _doc_hits = await _with_doc_context(
            messages, query, _conversation_doc_ids(conversation_id))
        try:
            from skills import tool_payload as _tool_payload
            try:
                from flags import is_enabled as _flag_on
                _chat_tools_on = _flag_on("chat.tools", True)
            except Exception:
                _chat_tools_on = True
            # Must match chat_simple's list exactly — the streaming twin and
            # the one-shot endpoint otherwise give the model different
            # capabilities, which is its own kind of lie.
            _tools = _tool_payload([
                "web_search", "fetch_url",
                "search_knowledge", "recall_preferences", "remember_preference",
                "list_scheduled", "calendar_query", "gmail_query",
                "github_query", "slack_history", "notion_query",
            ]) if _chat_tools_on else None

            async def _work() -> dict:
                """The whole model turn: tool loop if tools are on, else a
                plain call. Runs as one task so the stream can keep emitting
                progress while the gateway call is still in flight."""
                if _tools:
                    try:
                        from mcp_runner import run_with_tools as _run_with_tools
                        import outcomes as _outcomes
                        return await _run_with_tools(
                            messages=messages, tools_payload=_tools,
                            agent="chat", session_id=conversation_id,
                            max_tokens=2048, temperature=0.7,
                            doc_ids=_conversation_doc_ids(conversation_id),
                            on_event=_emit,
                            on_outcome=lambda n, a, ok, t, lat: _outcomes.on_tool_outcome(
                                name=n, arguments=a, ok=ok, result_text=t,
                                latency_s=lat, session_id=conversation_id,
                                run_id=conversation_id))
                    except Exception as _te:
                        print(f"[chat] tool loop failed, plain-text fallback: "
                              f"{type(_te).__name__}: {_te}")
                return LLM().chat(messages=messages, agent="chat",
                                  session=conversation_id, max_tokens=2048,
                                  temperature=0.7)

            yield _sse("status", text="thinking…")
            _task = _aio.create_task(_work())
            try:
                # Poll so tool progress AND a heartbeat reach the client while
                # the gateway call is in flight. A slow provider (or a 503ing
                # key the router is failing over) can take minutes; without a
                # heartbeat the UI would sit on a stale "thinking…" with no
                # idea if it was alive.
                _last_beat = _t.monotonic()
                while not _task.done():
                    await _aio.sleep(0.15)
                    while pending:
                        yield pending.pop(0)
                    _now = _t.monotonic()
                    if _now - _last_beat >= _CHAT_STREAM_HEARTBEAT_S:
                        _last_beat = _now
                        yield _sse("status", text=f"still working… "
                                                  f"{int(_now - _t0)}s elapsed")
                reply = await _task
            finally:
                # If the client hit stop (or navigated away), Starlette closes
                # the generator here — cancel the in-flight turn so it stops
                # burning a gateway call instead of running on orphaned.
                if not _task.done():
                    _task.cancel()
            while pending:
                yield pending.pop(0)
            answer = (reply.get("text") or "").strip() or "(empty answer)"
            spent = answer
        except Exception as e:
            yield _sse("error", text=f"chat failed: {e}")
            return
        finally:
            if spent:
                try:
                    delta = await _aio.to_thread(_session_cost_delta,
                                                 conversation_id, cost_before)
                    if delta:
                        _record_turn_cost(conversation_id, query, delta)
                except Exception:
                    pass
        for _chunk in _chat_chunks(answer):
            yield _sse("delta", text=_chunk)
            # Light pacing so a long answer paints in progressively rather
            # than arriving as one burst. Bounded: 12ms per chunk, so even a
            # 20k-char answer adds well under a second before `done`.
            await _aio.sleep(_CHAT_STREAM_PACE_S)
        # Persistence is inside the try on purpose. If the thread store is
        # corrupt or the disk is full, the exception used to escape the async
        # generator and truncate the response — so the client saw deltas and
        # NO terminal frame, rendered a partial answer as a finished one, and
        # the turn was never stored. Now it is a clean `error` frame.
        try:
            with _CHAT_THREADS_LOCK:
                data = _chat_threads_load()
                if conversation_id not in data:
                    data[conversation_id] = {"title": query[:60], "updated": 0,
                                             "messages": []}
                thread = data[conversation_id]
                history = thread.get("messages") or []
                thread["messages"] = (history + [{"role": "user", "content": query},
                                                 {"role": "assistant", "content": answer}]
                                      )[-(_CHAT_MAX_TURNS * 2):]
                thread["updated"] = _t.time()
                if not thread.get("title"):
                    thread["title"] = query[:60]
                if len(data) > _CHAT_MAX_THREADS:
                    for _cid in sorted(data, key=lambda c: data[c].get("updated", 0)
                                       )[:len(data) - _CHAT_MAX_THREADS]:
                        del data[_cid]
                _chat_threads_save(data)
        except Exception as e:
            yield _sse("error", text=f"answer delivered but could not be saved: {e}")
            return
        yield _sse("done", answer=answer, conversation_id=conversation_id)

    return StreamingResponse(gen(), **_SSE_HEADERS)


# ── chat (streaming) ────────────────────────────────────────────────────────
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


def _run_orchestrator(query: str, session_id: str,
                      should_cancel=None) -> str:
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
                                      resume=False,
                                      should_cancel=should_cancel))


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
_ACTIVE_RUNS: dict[str, "threading.Event"] = {}
_ACTIVE_RUNS_LOCK = threading.Lock()


def _register_run(cancel_ev: "threading.Event", *keys: str | None) -> None:
    with _ACTIVE_RUNS_LOCK:
        for k in keys:
            if k:
                _ACTIVE_RUNS[k] = cancel_ev


def _unregister_run(*keys: str | None) -> None:
    with _ACTIVE_RUNS_LOCK:
        for k in keys:
            _ACTIVE_RUNS.pop(k, None)


def _active_run_count() -> int:
    with _ACTIVE_RUNS_LOCK:
        return len({id(v) for v in _ACTIVE_RUNS.values()})


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
    keys = [body.get("conversation_id"), body.get("session_id")]
    with _ACTIVE_RUNS_LOCK:
        pairs = [(k, _ACTIVE_RUNS[k]) for k in keys
                 if k and k in _ACTIVE_RUNS]
    for _k, ev in pairs:
        ev.set()
    return {"status": "ok", "found": bool(pairs), "cancelled": len(pairs),
            "flagged": any(ev.is_set() for _k, ev in pairs),
            "active_runs": _active_run_count()}


def _stream_run(query: str, conversation_id: str | None,
                request: Request | None = None,
                idem_key: str = "") -> StreamingResponse:
    """Build the SSE streaming response for one agent run.

    Shared by /api/chat and /api/templates/{name}/run so both produce
    identical frames (log → meta → done). `conversation_id` (when present)
    resumes the same orchestrator graph across calls. `request` (when given)
    lets the drain loop notice client disconnects so an abandoned run stops
    emitting frames and — critically — is NOT recorded in the cost ledger
    or notified as complete.
    """

    async def gen():
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
        # Make sure the gateway (and its client) are available. The warmup
        # thread launched at import time is already starting V9 concurrently,
        # so we join it (non-blocking on the event loop — this runs in the
        # generator, not the event loop) rather than re-launching. If warmup
        # already finished, this returns immediately.
        try:
            if _GATEWAY_WARMUP_THREAD is not None and _GATEWAY_WARMUP_THREAD.is_alive():
                _GATEWAY_WARMUP_THREAD.join(timeout=60)
            # Also join the embedder warmup so the first memory.read embed
            # doesn't race a still-loading Ollama model (cold load = 30-40s).
            if _EMBEDDER_WARMUP_THREAD is not None and _EMBEDDER_WARMUP_THREAD.is_alive():
                _EMBEDDER_WARMUP_THREAD.join(timeout=60)
            from gateway import ensure_gateway

            ensure_gateway()
        except Exception as e:  # pragma: no cover - environment issue
            # Local-model fallback (Todo 11): if the gateway is down but the
            # query is a pure read-only shell-intent (disk/ip/os/...), answer
            # it directly via the gated-shell engine — no LLM required.
            offline = _shell_intent_offline(query)
            if offline is not None:
                yield _sse("log", text="[offline] gateway down — answered via "
                                     "gated-shell fallback (no LLM)")
                yield _sse("meta", elapsed_s=round(_time.time() - _start, 1))
                yield _sse("done", answer=offline, session_id="s8-offline",
                           conversation_id=conversation_id or "")
                return
            yield _sse("error", text=f"gateway unavailable: {e}")
            return

        # The orchestrator runs its own asyncio loop internally via
        # asyncio.run in Executor.run, so we run it in a thread to keep the
        # streaming response alive. We capture stdout and STREAM each line
        # live as a `log` SSE frame (instead of buffering until the end) so
        # the operator can watch the agent work in the terminal / UI.
        answer_box: dict[str, str] = {"answer": ""}
        # asyncio.Queue, NOT threading.Queue: a blocking .get() here would
        # freeze the whole event loop for the entire agent run (health, TTS
        # and concurrent chats all hang). The worker thread pushes via
        # call_soon_threadsafe so `await log_q.get()` yields control.
        loop = asyncio.get_running_loop()
        import asyncio as _aio  # noqa: F811 - local alias used for to_thread
        # Bounded: a chatty run can't grow the queue without limit while a
        # slow/disconnected client lags behind. Overflow lines are dropped
        # (the full log still lands in the session dir via prints).
        log_q: "asyncio.Queue[str | None]" = asyncio.Queue(maxsize=2000)

        import io
        import contextlib

        class _LiveLogCatcher(io.StringIO):
            def write(self, s):  # type: ignore[override]
                if s.strip():
                    # Mirror to the real (teed) stdout so the Console feed's
                    # server tail shows live run chatter, not just startup.
                    try:
                        _LIVE_OUT.write(s)
                    except Exception:
                        pass
                    try:
                        loop.call_soon_threadsafe(log_q.put_nowait, s)
                    except asyncio.QueueFull:
                        pass  # drop overflow; session log keeps everything
                return super().write(s)

        def _run_and_drain():
            # Run the orchestrator, then push a sentinel so the generator
            # below knows the run finished and can emit the final `done`.
            try:
                with contextlib.redirect_stdout(_LiveLogCatcher()):
                    answer_box["answer"] = _run_orchestrator(
                        query, session_id, cancel_ev.is_set) or ""
            except Exception as e:  # pragma: no cover - runtime failure
                loop.call_soon_threadsafe(log_q.put_nowait, f"AGENT ERROR: {e}")
            finally:
                # Always release the cancel registry, whatever way we left.
                _unregister_run(session_id, conversation_id)
                loop.call_soon_threadsafe(log_q.put_nowait, None)  # sentinel

        # Tell live UIs the ids up front so they can poll the graph endpoint
        # while the run proceeds. The classic UI ignores unknown frames.
        yield _sse("started", session_id=session_id,
                   conversation_id=conversation_id or "")

        # Snapshot the session's cumulative cost BEFORE the run so we can
        # report only THIS response's spend afterward (the session_id is
        # reused across turns, so the raw total is the whole conversation).
        # `_session_cost_breakdown` does a blocking httpx GET against the
        # gateway (timeout=5). Calling it inline inside this async generator
        # blocked the event loop for up to 5s on EVERY /api/chat request,
        # which stalls /api/health, /api/events, TTS and every concurrent
        # chat — the exact hazard the comments 20 lines above warn about.
        _cost_before = await _aio.to_thread(_session_cost_breakdown, session_id)

        cancel_ev = threading.Event()
        _register_run(cancel_ev, session_id, conversation_id)
        thread = threading.Thread(target=_run_and_drain, daemon=True)
        thread.start()

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
                if await _client_gone():
                    disconnected = True
                    break
                _now = _time.time()
                if _now - _last_beat >= _CHAT_STREAM_HEARTBEAT_S:
                    _last_beat = _now
                    yield _sse("status",
                               text=f"working… {int(_now - _t_start)}s elapsed")
                continue
            if item is None:
                break
            if await _client_gone():
                disconnected = True
                break
            yield _sse("log", text=item.rstrip("\n"))

        # No thread.join() here: joining blocks the event loop; the thread
        # is daemon-scoped and already pushed its sentinel before we exit.
        # NOTE: on disconnect the worker thread may still run to completion
        # in the background (threads can't be killed) — but nothing below
        # records or notifies, so the abandoned run leaves no trace.

        if disconnected:
            # Nobody is listening, so skip the notification and the SSE meta —
            # but STILL persist the spend. The compute happened and the
            # gateway billed it; swallowing it made the Ledger under-report
            # real usage every time a tab was closed mid-run.
            try:
                _d = _session_cost_delta(session_id, _cost_before)
                if _d is not None:
                    _record_turn_cost(session_id, query, _d)
            except Exception:
                pass
            # Release the idempotency key. This path returns before the
            # release below, so the key stayed claimed for the full
            # _IDEM_TTL_S (600s) after any disconnect — and because a
            # completed run was still registered under it, a later *different*
            # question with the same key got "deduplicated" and was swallowed.
            _inflight_run_release(idem_key)
            return

        # Total wall-clock time for the whole response (orchestrator run +
        # any drain), reported to the UI so the operator can see how long
        # the agent took end-to-end.
        elapsed = _time.time() - _start
        # A cancelled run usually has no formatter answer (the DAG stopped at
        # a node boundary). Give the UI something explicit instead of an
        # empty done frame, and do not announce it as a completed task.
        if cancel_ev.is_set() and not answer_box["answer"]:
            answer_box["answer"] = "Stopped by operator before the run finished."
        # Smart notification: push a completion summary to Telegram if the
        # operator opted in (fire-and-forget, never blocks the response).
        if answer_box["answer"] and not cancel_ev.is_set():
            notify_task_done(query, answer_box["answer"], elapsed, session_id)
        # Per-response cost: report ONLY this run's spend. The session_id is
        # reused across every turn of a conversation (that reuse is what gives
        # memory/audit continuity), so the raw session total is the whole
        # conversation's lifetime — not one answer. Subtract the snapshot we
        # took before the run to isolate just this response's cost.
        delta = _session_cost_delta(session_id, _cost_before)
        if delta is None:
            # Gateway cost API unreachable: report unknown, record nothing.
            # A $0 entry here would be fabricated ledger data.
            yield _sse("meta", elapsed_s=round(elapsed, 1), cost_unknown=True)
        else:
            # Accumulate this turn's delta into the per-session ledger so
            # the Spend panel can sum exactly the per-response costs shown
            # beside each message (instead of the polluted raw session
            # lifetime).
            _record_turn_cost(session_id, query, delta)
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
        _browser_root = (_P(STATE_DIR) / "sessions" / session_id / "browser").resolve()
        browser_shots: list[str] = []
        for abs_path in take_browser_artifacts(session_id):
            try:
                rel = _P(abs_path).resolve().relative_to(_browser_root)
                browser_shots.append(f"/api/artifacts/{session_id}/{rel.as_posix()}")
            except ValueError:
                continue
        yield _sse("done", answer=answer_box["answer"],
                   session_id=session_id,
                   conversation_id=conversation_id or "",
                   cancelled=cancel_ev.is_set(),
                   browser_artifacts=browser_shots)
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
    query = (body.get("query") or "").strip()
    conversation_id = _conv_id(body)
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
    if not idem_key and body.get("research"):
        idem_key = "research:" + _req_digest(query)
    if idem_key:
        existing = _inflight_run_lookup(idem_key)
        if existing:
            return StreamingResponse(_dedup_stream(existing),
                                     **_SSE_HEADERS)
        _inflight_run_claim(idem_key, conversation_id)
    try:
        return _stream_run(query, conversation_id, request=req,
                           idem_key=idem_key)
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
    sid = (body.get("session_id") or "").strip()
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
    if p.startswith(("/api/", "/v1/", "/assets/")):
        return JSONResponse(status_code=404,
                            content={"detail": "Not Found", "path": p})
    if not _SPA_INDEX.exists():
        return JSONResponse(status_code=404,
                            content={"detail": f"no route for {p}"})
    return FileResponse(str(_SPA_INDEX))


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
