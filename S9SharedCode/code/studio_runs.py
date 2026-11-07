"""Run registry and transcripts for the studio's agents — slice 5.

Why a registry at all
---------------------
A code-agent run today exists only as an SSE stream inside one browser tab. That
means four things, all of them wrong:

* close the tab and the run is invisible — nothing knows it exists, so it
  cannot be stopped, listed, or reopened;
* "Stop" aborts the fetch, and the generator's `finally` cancels the detached
  task. It works while the tab lives, and it is the only mechanism there is:
  there is no addressable run to stop from anywhere else;
* reload the studio and the transcript is gone, including the answer the agent
  already paid for;
* two tabs asking for work in the same session produce two runs that cannot
  even be counted, let alone shown as `⚑ 2 agents`.

So a run becomes an object: an id, an owner, a status, a cost, and a JSONL
transcript on disk. The stream is then just one view of it, and replay is a
file read.

Why JSONL and not one JSON document
-----------------------------------
A run's frames are appended continuously — a long turn is hundreds of frames —
and rewriting a document per frame is the write-amplification mistake the memory
store already made at 86MB. Appending one line is O(1), survives a crash
mid-write (the last line may be truncated; the reader skips it), and is
greppable when something goes wrong.

Cancellation is a flag, checked at the hop
------------------------------------------
`should_cancel(run_id)` is published into `gateway.cancel_scope` for the
duration of the loop, which is the same seam the DAG executor uses: the model
call in flight finishes, then the loop stops before the next hop. A hard kill
of the MCP child mid-call was tried and it costs more than it saves — you
cannot un-bill a request that already left the process, and you can lose the
tool result that was about to land.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

import studio_workspace as _ws

MAX_RUNS_PER_SESSION = 200
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# run_id -> live record. Only runs with a stream attached are live; the disk
# holds every run that ever finished.
_LIVE: dict[str, dict] = {}
_LOCK = threading.RLock()


def _bucket(sid: str) -> str:
    clean = (sid or "").strip()
    return clean if _ws.valid_sid(clean) else "_default"


def runs_dir(sid: str) -> Path:
    d = _ws.state_dir() / "runs" / _bucket(sid)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _index_file(sid: str) -> Path:
    return runs_dir(sid) / "index.json"


def _transcript(sid: str, run_id: str) -> Path:
    return runs_dir(sid) / f"{run_id}.jsonl"


def _read_json(path: Path) -> dict:
    try:
        got = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def valid_run_id(run_id: str) -> bool:
    return bool(ID_RE.match(str(run_id or "")))


# ── the registry ────────────────────────────────────────────────────────
def start(sid: str, question: str, model: str = "", provider: str = "",
          mode: str = "agent") -> dict:
    """Open a run. Returns the live record, which carries its id."""
    import uuid
    run_id = f"r-{uuid.uuid4().hex[:12]}"
    rec = {"id": run_id, "sid": _bucket(sid), "question": (question or "")[:400],
           "model": model or "", "provider": provider or "", "mode": mode,
           "status": "running", "started": time.time(), "ended": 0.0,
           "frames": 0, "input_tokens": 0, "output_tokens": 0,
           "tool_calls": 0, "answer": "", "stopped": "", "error": "",
           "cancel": threading.Event(), "writers": 0}
    with _LOCK:
        _LIVE[run_id] = rec
        idx = _read_json(_index_file(sid))
        rows = [r for r in (idx.get("runs") or []) if isinstance(r, dict)]
        rows.append(_public(rec))
        # Newest last in the file; `list_runs` reverses. Trimming here is not
        # a silent cap on the user's history so much as a bound on a file that
        # would otherwise grow forever — the transcripts themselves are kept
        # until the session is forgotten.
        rows = rows[-MAX_RUNS_PER_SESSION:]
        _write_json(_index_file(sid), {"sid": _bucket(sid), "runs": rows,
                                       "updated": time.time()})
    return rec


def note_write(run_id: str, path: str) -> None:
    """Record that this turn changed a file, so the run's row can say so."""
    rec = _LIVE.get(run_id)
    if rec is None or not path:
        return
    files = rec.setdefault("files", [])
    if path not in files:
        files.append(path)


def append(sid: str, run_id: str, kind: str, payload: dict | None = None) -> None:
    """Append one frame to the transcript, and fold it into the run's totals.

    The payload is stored as the client received it, so a replay can rebuild
    the transcript exactly — including the frames the panel renders as tool
    cards and approval cards.
    """
    payload = payload or {}
    line = json.dumps({"t": kind, "at": round(time.time(), 3), **payload},
                      default=str)
    try:
        with open(_transcript(sid, run_id), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        # A transcript that cannot be written must not end a live run: the
        # stream is the user's view, this file is the record.
        return
    with _LOCK:
        rec = _LIVE.get(run_id)
        if rec is not None:
            rec["frames"] = int(rec.get("frames") or 0) + 1
            if kind == "done":
                rec["input_tokens"] = int(payload.get("input_tokens") or 0)
                rec["output_tokens"] = int(payload.get("output_tokens") or 0)
                rec["tool_calls"] = int(payload.get("tool_calls") or 0)
                rec["answer"] = str(payload.get("answer") or "")[:4000]
                rec["stopped"] = str(payload.get("stopped") or "")[:400]
                rec["provider"] = str(payload.get("provider") or rec.get("provider") or "")
            elif kind == "error":
                rec["error"] = str(payload.get("text") or "")[:400]
            elif kind == "tool_call":
                rec["tool_calls"] = int(rec.get("tool_calls") or 0) + 1


def finish(run_id: str, status: str = "finished") -> None:
    """Close a run and write its final row into the index."""
    with _LOCK:
        rec = _LIVE.pop(run_id, None)
        if rec is None:
            return
        rec["status"] = status
        rec["ended"] = time.time()
        sid = rec["sid"]
        idx = _read_json(_index_file(sid))
        rows = [r for r in (idx.get("runs") or []) if isinstance(r, dict)
                and r.get("id") != run_id]
        rows.append(_public(rec))
        _write_json(_index_file(sid), {"sid": sid, "runs": rows,
                                       "updated": time.time()})


def stop(run_id: str) -> tuple[bool, str]:
    """Ask a live run to stop at its next hop.

    Returns (ok, why-not). Stopping a run that is not live is not an error to
    hide: the caller needs to know whether the agent is still working or
    already finished, because the difference is the whole point of having a
    registry.
    """
    with _LOCK:
        rec = _LIVE.get(run_id)
        if rec is None:
            return False, "this run is not live — it finished, or it never started here"
        rec["cancel"].set()
        rec["status"] = "stopping"
        return True, ""


def cancelled(run_id: str) -> bool:
    rec = _LIVE.get(run_id)
    return bool(rec and rec["cancel"].is_set())


def predicate(run_id: str):
    """The callable `gateway.cancel_scope` wants, or None for no scope."""
    if run_id not in _LIVE:
        return None
    return lambda: cancelled(run_id)


def live_count(sid: str = "") -> int:
    with _LOCK:
        if not sid:
            return len(_LIVE)
        b = _bucket(sid)
        return sum(1 for r in _LIVE.values() if r.get("sid") == b)


def waiting_on(sid: str = "") -> list[dict]:
    """Live runs parked on an approval — the state a status bar should show."""
    with _LOCK:
        return [dict(r) for r in _LIVE.values()
                if (not sid or r.get("sid") == _bucket(sid))
                and r.get("status") == "waiting"]


def mark_waiting(run_id: str, waiting: bool) -> None:
    with _LOCK:
        rec = _LIVE.get(run_id)
        if rec is not None:
            rec["status"] = "waiting" if waiting else "running"


def _public(rec: dict) -> dict:
    return {k: v for k, v in rec.items()
            if k not in ("cancel", "writers")}


def list_runs(sid: str) -> list[dict]:
    """Every run the session has, newest first, live ones flagged."""
    idx = _read_json(_index_file(sid))
    rows = [r for r in (idx.get("runs") or []) if isinstance(r, dict)]
    with _LOCK:
        live = {rid: _public(r) for rid, r in _LIVE.items()
                if r.get("sid") == _bucket(sid)}
    merged: dict[str, dict] = {}
    for r in rows:
        merged[str(r.get("id") or "")] = r
    for rid, r in live.items():
        merged[rid] = {**merged.get(rid, {}), **r, "live": True}
    out = sorted(merged.values(), key=lambda r: float(r.get("started") or 0),
                 reverse=True)
    for r in out:
        r.setdefault("live", bool(r.get("id") in live))
    return out


def frames(sid: str, run_id: str, after: int = 0) -> dict:
    """Replay a run's transcript, newest chunk first for long answers.

    A truncated final line (the process died mid-write) is skipped rather than
    guessed at, and the reader is told how many lines were unreadable.
    """
    path = _transcript(sid, run_id)
    out: list[dict] = []
    bad = 0
    try:
        with open(path, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i < after:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    bad += 1
    except OSError:
        return {"frames": [], "error": "no transcript for this run",
                "run": run_id}
    rec = _LIVE.get(run_id)
    return {"frames": out, "run": run_id, "unreadable": bad,
            "live": rec is not None,
            "status": (rec or {}).get("status") or "finished"}


def forget(sid: str) -> None:
    """Delete a session's runs. Called when the session itself is forgotten."""
    d = runs_dir(sid)
    for p in d.glob("*"):
        try:
            p.unlink()
        except OSError:
            pass


def touch_owner(run_id: str) -> None:
    """Keep the live record's owner count honest for reconnecting streams."""
    with _LOCK:
        rec = _LIVE.get(run_id)
        if rec is not None:
            rec["writers"] = int(rec.get("writers") or 0) + 1
