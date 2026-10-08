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
        # A transient read failure (locked file, torn write) must
        # NOT wipe in-memory state: every public op calls _load()
        # before _save(), so clobbering _SCHEDULES here lets one
        # bad read destroy every persisted schedule. Keep the
        # last-known-good state and let the caller proceed.
        return
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
        if n < 1:
            # Zero would re-arm at "now" and fire in a ~1s loop
            # forever, billing every iteration.
            raise ValueError(f"recurring interval must be >= 1{unit}: "
                             f"{cron!r}")
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
    import re as _re
    # Dispatch case-insensitively. _next_fire_from_cron lowercases
    # before parsing, so "DAILY@09:00" / "EVERY 30m" parsed fine in
    # the branch below — but the case-sensitive startswith() here
    # sent them to the one-shot else: a recurring job the operator
    # asked for fired ONCE and never re-armed. Reachable via the MCP
    # schedule_task tool, which passes LLM-generated text verbatim.
    low_when = when.strip().lower()
    try:
        if low_when.startswith("in "):
            # Relative delay: "in 30m", "in 1h", "in 45s" —
            # plus the spelled-out units people actually type
            # ("in 1 hour", "in 30 minutes"). The old parser
            # only matched single letters, so every plural form
            # fell through to int("1 hour") and failed.
            m = _re.fullmatch(
                r"(\d+)\s*(m|min|mins|minutes|minute|h|hr|hrs|"
                r"hour|hours|s|sec|secs|second|seconds)?",
                when[3:].strip().lower())
            if not m:
                raise ValueError(f"unparseable delay: {when!r}")
            n, unit = int(m.group(1)), (m.group(2) or "m")
            if n < 1:
                # "in 0 seconds" used to schedule a job whose
                # fire time is NOW — it executed immediately (and
                # billed) while looking like a deferred task. The
                # "every" branch has had this floor all along.
                raise ValueError("delay must be at least 1 unit")
            per = {"s": 1, "sec": 1, "secs": 1, "second": 1,
                   "seconds": 1,
                   "m": 60, "min": 60, "mins": 60,
                   "minute": 60, "minutes": 60,
                   "h": 3600, "hr": 3600, "hrs": 3600,
                   "hour": 3600, "hours": 3600}[unit]
            fire = now + n * per
            recurring = None
        elif low_when.startswith("tomorrow"):
            # One-shot at that time tomorrow — NOT recurring
            # (the word means the next day only; "daily@" is
            # the recurring form).
            fire = _next_fire_from_cron(when, now)
            recurring = None
        elif low_when.startswith("daily@") or low_when.startswith("every "):
            fire = _next_fire_from_cron(when, now)
            recurring = when
        else:
            # ISO datetime or epoch seconds — _next_fire_from_cron
            # parses both (fromisoformat, then bare float). The
            # old code ran float(when) directly, so the ISO and
            # "tomorrow" forms its own error message advertised
            # were unreachable.
            fire = _next_fire_from_cron(when, now)
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
            if not isinstance(_job, dict):
                # A hand-edited or torn schedules.json can hold
                # non-dict entries. The heap build ignores them,
                # but this scan called .get() on every entry and
                # raised AttributeError — 500ing every POST
                # /api/schedule until the file was hand-fixed.
                continue
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
        # layer decides whether to grey them out. Non-dict entries (see
        # schedule()) are dropped here too — `{**"garbage"}` raised
        # TypeError on every GET /api/schedule.
        return [{"id": k, **v} for k, v in _SCHEDULES.items()
                if isinstance(v, dict)]


def cancel(sid: str) -> bool:
    with _SCHED_LOCK:
        _load()
        if sid in _SCHEDULES:
            spec = _SCHEDULES[sid]
            if not isinstance(spec, dict):
                return False
            spec["enabled"] = False
            # Marker the worker checks between its snapshot and the
            # fire: a cancel landing in that window used to return
            # True while the captured still-enabled snapshot still
            # ran — one stray agent turn after a successful cancel.
            spec["_cancelled"] = True
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


def _epoch_or(v, fallback: float) -> float:
    """float() that treats a corrupt stored value as the fallback.

    A hand-edited string `next_fire` can exist — _load() only
    type-checks for the heap — and `float("soon")` raised ValueError
    inside the purge sweep, 500ing every GET /api/schedule."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return fallback


def _purge_locked(max_age_s: float) -> int:
    """Drop stale disabled one-shots. Caller must hold _SCHED_LOCK."""
    now = time.time()
    dead = [sid for sid, s in _SCHEDULES.items()
            if isinstance(s, dict) and not s.get("enabled", True)
            and not s.get("recurring")
            and (now - _epoch_or(s.get("next_fire"),
                                 _epoch_or(s.get("created"), now)))
            > max_age_s]
    for sid in dead:
        del _SCHEDULES[sid]
    if dead:
        _save()
    return len(dead)


def purge_disabled(max_age_days: float = 7) -> int:
    """Public entry: hard-delete disabled one-shots older than the cutoff."""
    if max_age_days < 0:
        # max(0, ...) used to clamp a negative to 0, making the age
        # test true for EVERY past fire — a purge with max_age_days=-1
        # deleted all disabled one-shots regardless of age.
        raise ValueError("max_age_days must be >= 0")
    with _SCHED_LOCK:
        _load()
        return _purge_locked(float(max_age_days) * 86400)


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
    # A task scheduled FROM a conversation runs IN it. The
    # conversation_id was accepted, persisted and then ignored —
    # _fire always derived a blank session, so a reminder set
    # mid-conversation answered with none of that context.
    derived_sid = spec.get("conversation_id") or f"s8-{short}"
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
        # ALWAYS record the result in the in-app feed first. Telegram may be
        # unconfigured or unreachable, and a reminder whose outcome exists
        # only in a Telegram message that never arrived is a reminder that
        # did not happen.
        try:
            from agent_server import _notif_add
            _notif_add("scheduled",
                       f"{spec['query'][:160]} -> "
                       f"{(answer or '').strip()[:400] or '(no answer)'}",
                       derived_sid)
        except Exception as e:
            _log(f"[{sid}] in-app record failed: {e}")
        try:
            from agent_server import notify_task_done
            notify_task_done(spec["query"], answer or "",
                             round(_t.time() - _start, 1), derived_sid,
                             scheduled=True)
        except Exception as e:
            _log(f"[{sid}] notify failed: {e}")


def _worker() -> None:
    while not _STOP.wait(timeout=1.0):
        try:
            _worker_tick()
        except Exception:
            # One poison entry (an unparseable `recurring`, an
            # unwritable state dir) used to raise out of the loop and
            # kill the thread — and because _ensure_worker only
            # restarts it on the NEXT schedule() call, which re-heaps
            # the same entry from disk, every restarted worker died
            # again immediately: one bad value silently stopped ALL
            # scheduled work. Log, skip the tick, stay alive.
            _log(f"worker tick failed, skipping: {traceback.format_exc()}")


def _worker_tick() -> None:
    with _SCHED_LOCK:
        if not _HEAP:
            return
        fire, sid = _HEAP[0]
        if time.time() < fire:
            return
        heapq.heappop(_HEAP)
        spec = _SCHEDULES.get(sid)
        if not spec or not isinstance(spec, dict) or not spec.get("enabled"):
            return
        # CRITICAL: snapshot BEFORE mutating. The worker used to set
        # enabled=False and then pass that same dict to _fire, whose
        # own enabled guard aborted the run — so every ONE-SHOT
        # schedule silently fired nothing. Capture the still-enabled
        # spec first, then persist the disable (crash safety), then
        # hand the untouched snapshot to _fire.
        snap = dict(spec)
        # Re-arm recurring schedules before firing.
        if spec.get("recurring"):
            try:
                spec["next_fire"] = _next_fire_from_cron(
                    spec["recurring"], time.time())
            except (ValueError, OSError) as e:
                # A corrupt `recurring` value used to raise HERE —
                # inside the loop with no guard — killing the worker
                # (see _worker). Disable the job itself so the
                # scheduler keeps serving every OTHER job, and say
                # why on the job's row.
                spec["enabled"] = False
                spec["last_error"] = f"unparseable recurring: {e}"[:400]
                _save()
                _log(f"[{sid}] disabled — unparseable recurring "
                     f"{spec.get('recurring')!r}: {e}")
                return
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
    # A cancel() that landed between the snapshot and here must not
    # still fire — the snapshot predates the cancel and is still
    # enabled (see cancel()).
    with _SCHED_LOCK:
        live = _SCHEDULES.get(sid)
    if live is not None and isinstance(live, dict) and live.get("_cancelled"):
        return
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
