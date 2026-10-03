"""F10 FIX (cross-session memory contamination): per-conversation turn log.

Problem this solves
-------------------
`state/memory.json` is GLOBAL across conversations by design (durable user
facts like "my favorite composer is Mozart" must survive across threads).
But when a user asks an *episodic* question — "what was the first thing I
asked in this conversation?" — vector search over global memory surfaces
stale facts from OTHER sessions, and the agent confidently presents them as
in-thread history (observed live: a leftover fact from the previous day's
test round was reported as "the first question you asked").

Design
------
A tiny append-only JSON store keyed by session_id:

    state/turn_logs.json  =  { "<sid>": [ {"q":..., "a":..., "ts":...}, ... ] }

* One entry appended per completed orchestrator run (query + final answer).
* `recent()` returns the last N turns so the planner context gets a compact,
  AUTHORITATIVE record of THIS conversation only.
* Same atomic-write discipline as memory.py (unique tmp name + os.replace,
  shared I/O lock) because concurrent sessions write concurrently.
* Best-effort everywhere: any failure logs and returns empty — the turn log
  must never break a run.

The store is intentionally separate from memory.json: durable facts and
episodic history have different lifetimes and different consumers.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

STATE_PATH = Path(os.environ.get("S9_STATE_DIR")
                   or (Path(__file__).parent / "state")) / "turn_logs.json"
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

# Max turns retained per session (oldest trimmed). A conversation rarely
# needs more than this much context, and unbounded growth would slow the
# read on every long-lived thread.
_MAX_TURNS_PER_SESSION = 40

_IO_LOCK = threading.Lock()


def _read_nolock() -> dict:
    """Read the store. Caller must hold _IO_LOCK (or accept a torn read)."""
    if not STATE_PATH.exists():
        return {}
    try:
        text = STATE_PATH.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    if not text.strip():
        return {}
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _load() -> dict:
    with _IO_LOCK:
        return _read_nolock()


def _write_nolock(data: dict) -> None:
    """Atomic write. Caller must hold _IO_LOCK."""
    tmp = STATE_PATH.with_name(
        STATE_PATH.name + f".tmp-{threading.current_thread().ident}-{os.getpid()}"
    )
    last_exc: OSError | None = None
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        for attempt in range(3):
            try:
                os.replace(tmp, STATE_PATH)
                last_exc = None
                break
            except PermissionError as e:
                last_exc = e
                time.sleep(0.05 * (attempt + 1))
    finally:
        try:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        except OSError:
            pass
    if last_exc is not None:
        raise last_exc


def _save(data: dict) -> None:
    with _IO_LOCK:
        _write_nolock(data)


def append(session_id: str, query: str, answer: str) -> None:
    """Record one completed turn (query + final answer) for a session.

    The whole load→modify→save runs under one lock hold: concurrent turns
    in the same session used to interleave and silently drop entries."""
    if not session_id or not query:
        return
    entry = {
        "q": str(query)[:500],
        "a": str(answer or "")[:1000],
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        with _IO_LOCK:
            data = _read_nolock()
            turns = data.setdefault(session_id, [])
            turns.append(entry)
            del turns[:-_MAX_TURNS_PER_SESSION]
            _write_nolock(data)
    except OSError as e:  # pragma: no cover - best-effort persistence
        print(f"[turnlog] append failed: {e!r}")


def recent(session_id: str, n: int = 8) -> list[dict]:
    """Return the most recent <=n turns for this session, oldest first."""
    if not session_id:
        return []
    turns = _load().get(session_id) or []
    return turns[-n:] if n else []


def format_for_prompt(turns: list[dict]) -> str:
    """Render turns as a compact numbered transcript block for prompts."""
    if not turns:
        return ""
    lines = []
    for i, t in enumerate(turns, 1):
        q = (t.get("q") or "").replace("\n", " ")
        a = (t.get("a") or "").replace("\n", " ")
        lines.append(f"  {i}. USER: {q}\n     AGENT: {a}")
    return "\n".join(lines)


def clear(session_id: str | None = None) -> int:
    """Delete one session's log, or everything when session_id is None.
    Returns the number of sessions removed (diagnostics/tests)."""
    try:
        with _IO_LOCK:
            data = _read_nolock()
            if session_id is None:
                removed = len(data)
                data = {}
            else:
                removed = 1 if session_id in data else 0
                data.pop(session_id, None)
            _write_nolock(data)
    except OSError as e:  # pragma: no cover
        print(f"[turnlog] clear failed: {e!r}")
    return removed
