"""Lightweight task scheduler for proactive / deferred agent runs.

Lets the operator schedule an agent query to run later ("remind me in 10
min") or on a recurring cadence ("run daily at 09:00"). Schedules are
persisted to disk so they survive a server restart. When a schedule fires,
the orchestrator runs the query and (if configured) pushes the result to
Telegram via the same notify path the chat endpoint uses.

This is intentionally minimal: a single background thread walks a heap of
due tasks. No external broker, no cron daemon. For heavy production use you
would swap this for APScheduler / a real queue, but the surface (schedule,
list, cancel, fire) is the same.
"""
from __future__ import annotations

import heapq
import json
import os
import threading
import time
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_STATE_DIR = Path(os.environ.get("S9_STATE_DIR") or (ROOT / "state"))
_SCHED_PATH = _STATE_DIR / "schedules.json"
_SCHED_LOCK = threading.Lock()

# In-memory heap of (fire_epoch, schedule_id). Persisted alongside.
_HEAP: list[tuple[float, str]] = []
_SCHEDULES: dict[str, dict] = {}
_STOP = threading.Event()
_WORKER: "threading.Thread | None" = None


def _load() -> None:
    global _SCHEDULES, _HEAP
    try:
        data = json.loads(_SCHED_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    _SCHEDULES = data
    _HEAP = [(s["next_fire"], sid) for sid, s in data.items()
             if isinstance(s, dict) and s.get("enabled", True)
             and isinstance(s.get("next_fire"), (int, float))]
    heapq.heapify(_HEAP)


_LOG_PATH = _STATE_DIR / "scheduler.log"
_LOG_MAX = 256 * 1024


def _log(msg: str) -> None:
    """Append a line to state/scheduler.log (rotated at 256KB) + stderr.

    The worker used to swallow every exception with `except: pass`, which
    is exactly why a broken scheduler looked like a working one.
    """
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    try:
        if _LOG_PATH.exists() and _LOG_PATH.stat().st_size > _LOG_MAX:
            _LOG_PATH.replace(_LOG_PATH.with_name("scheduler.log.1"))
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass
    print(f"[scheduler] {msg}", flush=True)


def _record(sid: str, error: str | None = None) -> None:
    """Persist the outcome of a fire (last_fire + last_error) so the
    console can show whether a job ran and, if not, why."""
    with _SCHED_LOCK:
        _load()
        spec = _SCHEDULES.get(sid)
        if not isinstance(spec, dict):
            return
        spec["last_fire"] = time.time()
        spec["last_error"] = error
        _save()


def _save() -> None:
    _SCHED_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SCHED_PATH.with_name(f"{_SCHED_PATH.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(_SCHEDULES, indent=2), encoding="utf-8")
    os.replace(tmp, _SCHED_PATH)


def _next_fire_from_cron(cron: str, base: float) -> float:
    """Minimal cron: 'daily@HH:MM', 'every N[mh]', 'tomorrow HH:MM',
    ISO datetime, or epoch seconds.

    Returns the next fire epoch >= base. Kept tiny on purpose.
    """
    import datetime as _dt
    import re as _re
    cron = cron.strip()
    low = cron.lower()
    if low.startswith("daily@"):
        hh, mm = low[len("daily@"):].split(":")
        now = _dt.datetime.fromtimestamp(base)
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if target.timestamp() <= base:
            target = target + _dt.timedelta(days=1)
        return target.timestamp()
    if low.startswith("every "):
        m = _re.fullmatch(r"(\d+)\s*([smh]?)", low[len("every "):].strip())
        if not m:
            raise ValueError(f"unparseable recurring interval: {cron!r} "
                             f"(want 'every 30m', 'every 2h' or 'every 45s')")
        n, unit = int(m.group(1)), m.group(2) or "m"
        return base + n * {"s": 1, "m": 60, "h": 3600}[unit]
    m = _re.fullmatch(r"tomorrow\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", low)
    if m:
        hh, mm, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ap == "pm" and hh < 12:
            hh += 12
        if ap == "am" and hh == 12:
            hh = 0
        now = _dt.datetime.fromtimestamp(base) + _dt.timedelta(days=1)
        return now.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()
    try:
        return _dt.datetime.fromisoformat(cron).timestamp()
    except ValueError:
        pass
    # bare epoch
    return float(cron)


def schedule(query: str, when: str, *,
             conversation_id: str | None = None,
             notify: bool = True) -> str:
    """Schedule `query` to run at `when`.

    `when` forms:
      - "in 10m" / "in 1h"  → relative delay
      - "daily@09:00"      → recurring daily at 09:00 local
      - "every 30m"        → recurring every 30 minutes
      - "<epoch seconds>"  → absolute one-shot
    Returns the schedule id.

    Idempotent on (query, when): posting the identical enabled job twice
    (double-click, client retry, replayed request) returns the EXISTING id
    instead of scheduling a duplicate run that bills twice.
    """
    sid = f"sch-{uuid.uuid4().hex[:8]}"
    now = time.time()
    try:
        if when.startswith("in "):
            unit = when[3:].strip()
            if unit.endswith("m"):
                delay = int(unit[:-1]) * 60
            elif unit.endswith("h"):
                delay = int(unit[:-1]) * 3600
            elif unit.endswith("s"):
                delay = int(unit[:-1])
            else:
                delay = int(unit)
            fire = now + delay
            recurring = None
        elif when.startswith("daily@") or when.startswith("every "):
            fire = _next_fire_from_cron(when, now)
            recurring = when
        else:
            fire = float(when)
            recurring = None
    except ValueError as e:
        # _next_fire_from_cron already names valid forms for recurring
        # intervals — keep its message; otherwise name them all.
        if "want '" in str(e):
            raise
        raise ValueError(
            f"unparseable schedule {when!r}: want 'in 30m' / 'in 1h' / "
            f"'daily@09:00' / 'every 30m' / 'every 2h' / 'tomorrow 9am' / "
            f"ISO datetime / epoch seconds")
    with _SCHED_LOCK:
        _load()
        for _esid, _job in _SCHEDULES.items():
            if _job.get("enabled") and _job.get("query") == query \
                    and _job.get("when") == when:
                return _esid
        _SCHEDULES[sid] = {
            "query": query,
            "when": when,
            "next_fire": fire,
            "recurring": recurring,
            "conversation_id": conversation_id,
            "notify": notify,
            "enabled": True,
            "created": now,
        }
        heapq.heappush(_HEAP, (fire, sid))
        _save()
    _ensure_worker()
    return sid


def list_schedules() -> list[dict]:
    with _SCHED_LOCK:
        _load()
        # Sweep stale disabled one-shots here too: fired one-shots are
        # marked disabled by the worker (not by cancel()), so without this
        # every completed reminder left a permanent ghost row behind.
        _purge_locked(_PURGE_AFTER_S)
        # Return ALL schedules (enabled and disabled). Cancelled schedules
        # remain visible so callers can inspect the `enabled` flag; the UI
        # layer decides whether to grey them out.
        return [{"id": k, **v} for k, v in _SCHEDULES.items()]


def cancel(sid: str) -> bool:
    with _SCHED_LOCK:
        _load()
        if sid in _SCHEDULES:
            _SCHEDULES[sid]["enabled"] = False
            _save()
            _purge_locked(_PURGE_AFTER_S)
            return True
    return False


def remove(sid: str) -> bool:
    """Hard-delete a schedule (no ghost row). Returns False when unknown."""
    with _SCHED_LOCK:
        _load()
        if sid in _SCHEDULES:
            del _SCHEDULES[sid]
            _save()
            _load()  # rebuild the heap without the removed entry
            return True
    return False


# Disabled one-shots older than this are auto-purged so schedules.json
# can't accumulate ghost rows forever. Recurring jobs are never purged.
_PURGE_AFTER_S = 7 * 86400


def _purge_locked(max_age_s: float) -> int:
    """Drop stale disabled one-shots. Caller must hold _SCHED_LOCK."""
    now = time.time()
    dead = [sid for sid, s in _SCHEDULES.items()
            if isinstance(s, dict) and not s.get("enabled", True)
            and not s.get("recurring")
            and (now - float(s.get("next_fire") or s.get("created") or now))
            > max_age_s]
    for sid in dead:
        del _SCHEDULES[sid]
    if dead:
        _save()
    return len(dead)


def purge_disabled(max_age_days: float = 7) -> int:
    """Public entry: hard-delete disabled one-shots older than the cutoff."""
    with _SCHED_LOCK:
        _load()
        return _purge_locked(max(0, float(max_age_days)) * 86400)


def _fire(sid: str, spec: dict | None = None) -> None:
    """Run one scheduled query through the orchestrator (blocking).

    `spec` is the snapshot taken by the worker under _SCHED_LOCK — _fire
    never reads the shared _SCHEDULES dict itself, so a concurrent
    schedule()/cancel() can't clobber the firing run's parameters."""
    if spec is None:
        with _SCHED_LOCK:
            _load()
            spec = _SCHEDULES.get(sid)
    if not spec or not spec.get("enabled"):
        return
    # Derive a stable session id without assuming the "sch-" prefix format.
    short = sid[4:] if sid.startswith("sch-") and len(sid) > 4 else sid
    derived_sid = f"s8-{short}"
    import time as _t
    _start = _t.time()
    error: str | None = None
    # Cost snapshot BEFORE the run so the Ledger records this job's spend.
    # Scheduled runs bypass POST /api/chat (where turns are recorded), so
    # without this every scheduled job spent money invisibly.
    before = None
    try:
        from agent_server import _session_cost_breakdown
        before = _session_cost_breakdown(derived_sid)
    except Exception as e:
        _log(f"[{sid}] cost snapshot failed (non-fatal): {e}")
    try:
        from flow import Executor
        import asyncio
        # RELIABILITY FIX (resume on missing session): the scheduler used to
        # pass resume=True for a brand-new session id, but Executor.run
        # raises when there is no persisted graph for that id. Detect a
        # missing session and start fresh instead. Probe the graph file
        # directly — constructing SessionStore would mkdir empty session
        # dirs that pollute list_sessions().
        from persistence import SESSIONS_ROOT
        resume = (SESSIONS_ROOT / derived_sid / "graph.json").exists()
        answer = asyncio.run(
            Executor().run(spec["query"],
                           session_id=derived_sid, resume=resume)
        )
        _log(f"[{sid}] ok in {round(_t.time() - _start, 1)}s -> "
             f"{str(answer or '')[:160]!r}")
    except Exception as e:
        answer = f"[scheduler] run failed: {e}"
        error = str(e)[:400]
        _log(f"[{sid}] run failed: {traceback.format_exc()}")
    # Record the outcome even when notify=False — previously the result
    # was dropped entirely, leaving no trace that the job had run.
    _record(sid, error)
    # Record spend into the per-session ledger (same helper the chat
    # stream uses) so scheduled jobs show up on the Ledger page.
    try:
        if before is not None:
            from agent_server import _session_cost_delta, _record_turn_cost
            delta = _session_cost_delta(derived_sid, before)
            if delta:
                _record_turn_cost(derived_sid, spec["query"], delta)
                _log(f"[{sid}] cost recorded: {delta}")
    except Exception as e:
        _log(f"[{sid}] cost record failed (non-fatal): {e}")
    if spec.get("notify", True):
        try:
            from agent_server import notify_task_done
            notify_task_done(spec["query"], answer or "",
                             round(_t.time() - _start, 1), derived_sid)
        except Exception as e:
            _log(f"[{sid}] notify failed: {e}")


def _worker() -> None:
    while not _STOP.wait(timeout=1.0):
        with _SCHED_LOCK:
            if not _HEAP:
                continue
            fire, sid = _HEAP[0]
            if time.time() < fire:
                continue
            heapq.heappop(_HEAP)
            spec = _SCHEDULES.get(sid)
            if not spec or not spec.get("enabled"):
                continue
            # CRITICAL: snapshot BEFORE mutating. The worker used to set
            # enabled=False and then pass that same dict to _fire, whose
            # own enabled guard aborted the run — so every ONE-SHOT
            # schedule silently fired nothing. Capture the still-enabled
            # spec first, then persist the disable (crash safety), then
            # hand the untouched snapshot to _fire.
            snap = dict(spec)
            # Re-arm recurring schedules before firing.
            if spec.get("recurring"):
                spec["next_fire"] = _next_fire_from_cron(
                    spec["recurring"], time.time())
                heapq.heappush(_HEAP, (spec["next_fire"], sid))
                _save()
            else:
                # RELIABILITY FIX (one-shot re-fire): fired one-shot items
                # used to stay enabled=True with a stale past next_fire, so
                # any later _load() re-heaped them and they fired AGAIN.
                # Disable + persist BEFORE executing so a crash mid-fire
                # (or any reload) can never resurrect the schedule.
                spec["enabled"] = False
                _save()
        # Fire outside the lock, with the still-enabled snapshot.
        try:
            _fire(sid, snap)
        except Exception as e:
            _log(f"[{sid}] fire raised: {traceback.format_exc()}")
            _record(sid, repr(e))


def _ensure_worker() -> None:
    global _WORKER
    if _WORKER is None or not _WORKER.is_alive():
        _WORKER = threading.Thread(target=_worker, daemon=True)
        _WORKER.start()


def start() -> None:
    """Load persisted schedules and start the background worker."""
    with _SCHED_LOCK:
        _load()
    _ensure_worker()
