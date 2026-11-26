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
import re as _re
import sys
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

# When this process came up. An interval slot that fell due meaningfully
# before this point was missed while the agent was off: it must not be
# delivered at startup, and it must not move the schedule's phase.
_PROCESS_START = time.time()
# mtime of schedules.json as of our last load; the worker reloads when another
# process (the MCP child, where schedule_task/schedule_self run) writes it.
# None until start() runs, so tests that drive the worker with in-memory state
# are not clobbered by a disk reload.
_WORKER_MTIME = None
_RECUR_INTERVAL = _re.compile(r"every\s+(\d+)\s*([smh]?)\s*$",
                              _re.IGNORECASE)
_RECUR_START_GRACE_S = 60.0


def _load() -> None:
    global _SCHEDULES, _HEAP, _WORKER_MTIME
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
    # Re-baseline the worker's mtime so it only reloads on a GENUINE external
    # change (another process writing the file), never after our own load.
    try:
        _WORKER_MTIME = _SCHED_PATH.stat().st_mtime
    except OSError:
        _WORKER_MTIME = 0.0


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


def _interval_seconds(cron: str) -> int | None:
    """The step for an `every N[s|m|h]` spec, or None for a clock spec."""
    m = _RECUR_INTERVAL.fullmatch((cron or "").strip())
    if not m:
        return None
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[
        (m.group(2) or "m").lower()]


def _advance_recurring(cron: str, scheduled: float, now: float) -> float:
    """The next occurrence after `now`, on the schedule's ORIGINAL grid.

    `every N` is an interval grid anchored at the first fire, not a delay
    measured from whenever the worker last woke. Re-arming from
    `time.time()` turned a 2h reminder created at 00:43 (02:44, 04:44,
    ...) into 03:02, 05:02 after one restart, because the missed slot was
    re-anchored to the restart time. Advancing from the scheduled epoch
    keeps both the interval and the phase.
    """
    step = _interval_seconds(cron)
    if step is not None:
        if step < 1:
            raise ValueError(f"recurring interval must be >= 1: {cron!r}")
        if scheduled > now:
            return scheduled
        # Jump over every slot the process missed in one calculation; a
        # loop would crawl for a long downtime with "every 45s".
        return scheduled + ((int((now - scheduled) // step) + 1) * step)
    nxt = _next_fire_from_cron(cron, scheduled)
    for _ in range(10000):
        if nxt > now:
            return nxt
        nxt = _next_fire_from_cron(cron, nxt)
    return _next_fire_from_cron(cron, now)


class _BadWhen(ValueError):
    """A `when` value whose own message must reach the caller unchanged.

    schedule() rewrites generic parse failures into one list of accepted
    forms; the messages here are already specific ('9:00 AM has already
    passed today, ...') and that rewrite destroyed exactly the detail the
    model needs. Raised when a NAMED clock time is in the past: the model
    used to be handed the generic error, whose first suggestion was a
    relative delay, and it answered a '9am' request by silently scheduling
    'in 1h'.
    """


_CLOCK_AMPM = _re.compile(
    r"^(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)$")
_CLOCK_24H = _re.compile(r"^(?:at\s+)?(\d{1,2}):(\d{2})$")
_CLOCK_TODAY = _re.compile(
    r"^today\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")


def _looks_like_clock(when: str) -> bool:
    """Whether `when` names a clock time rather than a delay/epoch.

    Bare digits stay epochs ('1789...'): only an am/pm marker or a colon
    makes a bare string a time, so '9am', '9:30' and '21:45' are clocks
    while '930' keeps its old epoch meaning. Both word orders work
    ('today 9am', '9am today').
    """
    low = _strip_trailing_today(when.strip().lower())
    return bool(low.startswith("today ")
                or _CLOCK_AMPM.match(low) or _CLOCK_24H.match(low))


def _strip_trailing_today(low: str) -> str:
    return low[:-len(" today")].rstrip() if low.endswith(" today") else low


def _clock_today_epoch(cron: str, base: float) -> tuple[float, str]:
    """Parse a clock time meant for TODAY ('today 9am', '9am', '9:30',
    '21:45', '2:30pm') into an epoch, plus a display string.

    A time that has already passed today is a REFUSAL, not a delay: the
    caller gets _BadWhen naming the time and the two honest ways forward.
    Silently shifting it (the old behaviour, via the generic error that
    suggested 'in 2h') moved a 9am reminder to 10:09.
    """
    import datetime as _dt
    low = _strip_trailing_today(cron.strip().lower())
    m = _CLOCK_TODAY.match(low) or _CLOCK_AMPM.match(low) or _CLOCK_24H.match(low)
    if not m:
        raise _BadWhen(
            f"unparseable time {cron!r}: want 'today 9am' / '9am' / "
            f"'9:30' / '14:45' / '2:30pm'")
    if m.re is _CLOCK_TODAY:
        hh_s, mm_s, ap = m.group(1), m.group(2), m.group(3)
    elif m.re is _CLOCK_AMPM:
        hh_s, mm_s, ap = m.group(1), m.group(2), m.group(3)
    else:
        hh_s, mm_s, ap = m.group(1), m.group(2), None
    hh = int(hh_s)
    mm = int(mm_s or 0)
    if ap == "pm" and hh < 12:
        hh += 12
    elif ap == "am" and hh == 12:
        hh = 0
    if hh > 23 or mm > 59:
        raise _BadWhen(
            f"no such clock time in {cron!r}: hours go to "
            f"{23 if not ap else 12}, minutes to 59")
    now = _dt.datetime.fromtimestamp(base)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    # %-I is a glibc extension; C strftime on Windows renders it literally.
    fmt = "%I:%M %p" if _WIN32 else "%-I:%M %p"
    shown = target.strftime(fmt)
    if target.timestamp() <= base:
        raise _BadWhen(
            f"{shown} has already passed today ({target:%Y-%m-%d %H:%M} "
            f"local; now {now:%H:%M}). A named time must not be turned into "
            f"a relative delay - if the user meant tomorrow, schedule "
            f"'tomorrow {cron.strip().removeprefix('today ').strip()}'; "
            f"otherwise tell the user the time has passed and ask which "
            f"day they meant.")
    return target.timestamp(), shown


# strftime is C on Windows: '%-I' is not a valid directive there.
_WIN32 = sys.platform.startswith("win")


def schedule(query: str, when: str, *,
             conversation_id: str | None = None,
             notify: bool = True,
             channel: str = "",
             lead_s: float = 0,
             lead_message: str = "",
             message: str = "",
             follow_up: str = "",
             wait_for_ack: bool = False,
             ack_timeout_s: float = 900,
             snooze_s: float = 0,
             max_follow_ups: int = 3) -> str:
    """Schedule `query` to run at `when`.

    `when` forms:
      - "in 10m" / "in 1h"  â†’ relative delay
      - "daily@09:00"      â†’ recurring daily at 09:00 local
      - "every 30m"        â†’ recurring every 30 minutes
      - "tomorrow 9am" / "tomorrow 14:30" â†’ one-shot tomorrow
      - "9am" / "today 9am" / "9:30" / "2:30pm" â†’ one-shot TODAY, refused
        (with its own message) if that time has already passed
      - ISO datetime / "<epoch seconds>"  â†’ absolute one-shot
    Returns the schedule id.

    Idempotent on (query, when): posting the identical enabled job twice
    (double-click, client retry, replayed request) returns the EXISTING id
    instead of scheduling a duplicate run that bills twice.

    Notification shape
    ------------------
    `channel` names a gateway adaptor (telegram, slack, discord, gmail, ...).
    `lead_s` fires a PRE-notification that many seconds before the task, with
    its own agent-authored text; `message` is the text at fire time. Both are
    optional and independent, so a reminder can warn first and report later
    without the agent re-scheduling anything by hand.

    `wait_for_ack` makes the pre-notification a conversation rather than an
    announcement: the user is expected to reply, the reply is routed to the
    agent LLM, and the agent may reply back, up to `max_follow_ups` times,
    until the user acknowledges. `snooze_s` is the delay before the next
    unacknowledged nudge. All of it is best-effort - a channel that cannot
    deliver must never stop the task itself from firing.
    """
    sid = f"sch-{uuid.uuid4().hex[:8]}"
    now = time.time()
    import re as _re
    # Dispatch case-insensitively. _next_fire_from_cron lowercases
    # before parsing, so "DAILY@09:00" / "EVERY 30m" parsed fine in
    # the branch below â€” but the case-sensitive startswith() here
    # sent them to the one-shot else: a recurring job the operator
    # asked for fired ONCE and never re-armed. Reachable via the MCP
    # schedule_task tool, which passes LLM-generated text verbatim.
    low_when = when.strip().lower()
    try:
        if low_when.startswith("in "):
            # Relative delay: "in 30m", "in 1h", "in 45s" â€”
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
                # fire time is NOW â€” it executed immediately (and
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
            # One-shot at that time tomorrow â€” NOT recurring
            # (the word means the next day only; "daily@" is
            # the recurring form).
            fire = _next_fire_from_cron(when, now)
            recurring = None
        elif low_when.startswith("daily@") or low_when.startswith("every "):
            fire = _next_fire_from_cron(when, now)
            recurring = when
        elif _looks_like_clock(low_when):
            # A named clock time for today: '9am', 'today 14:30', '2:30pm'.
            # One-shot. A time that has already passed refuses with its own
            # message rather than the generic form list - that list is what
            # nudged the model into answering a 9am reminder with 'in 1h'.
            fire, _ = _clock_today_epoch(when, now)
            recurring = None
        else:
            # ISO datetime or epoch seconds â€” _next_fire_from_cron
            # parses both (fromisoformat, then bare float). The
            # old code ran float(when) directly, so the ISO and
            # "tomorrow" forms its own error message advertised
            # were unreachable.
            fire = _next_fire_from_cron(when, now)
            recurring = None
    except ValueError as e:
        # _next_fire_from_cron already names valid forms for recurring
        # intervals â€” keep its message; otherwise name them all.
        # _BadWhen messages are specific on purpose and pass through too.
        if isinstance(e, _BadWhen) or "want '" in str(e):
            raise
        raise ValueError(
            f"unparseable schedule {when!r}: want 'in 30m' / 'in 1h' / "
            f"'daily@09:00' / 'every 30m' / 'every 2h' / 'tomorrow 9am' / "
            f"'today 9am' / '9am' / '14:30' / ISO datetime / epoch seconds")
    # A one-shot in the past is refused, not fired. The worker disables every
    # one-shot as it fires (so a crash cannot resurrect it), which means a past
    # date produced a job that was born disabled and then consumed itself on
    # the next tick. To the user that reads as "the scheduler is broken": they
    # asked for tomorrow at 9am, were told it was scheduled, found it greyed
    # out, and â€” with no enable tool â€” asked for a second copy. The real fault
    # was an absolute date a model wrote a year in the past, accepted verbatim.
    #
    # Recurring jobs are exempt: "daily@09:00" legitimately rolls forward.
    if recurring is None and fire <= now:
        raise ValueError(
            f"schedule {when!r} is in the past "
            f"({time.strftime('%Y-%m-%d %H:%M', time.localtime(fire))} "
            f"local; now {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}). "
            f"If the user named a time, use 'tomorrow 9am' rather than "
            f"shifting it â€” a relative form like 'in 2h' answers a delay the "
            f"user asked for, never a named clock time. For an absolute "
            f"date, check the year.")
    with _SCHED_LOCK:
        _load()
        # A reminder stored as a re-runnable `query` reproduces itself: the job
        # fires, the agent reads "remind me to stretch", schedules a NEW
        # reminder, and the user gets a confirmation instead of the reminder.
        # Derive the words once, here, so the stored job is a nudge and the
        # DAG never runs. Explicit `message` always wins; a query that asks for
        # real work is left alone.
        if not str(message or "").strip():
            _derived = _reminder_text(query)
            if _derived:
                message = _derived
                # Nudge rows still need a channel to be pushed; a reminder with
                # no channel lands in the console feed, which is where the rest
                # of "tell me later" output goes when nothing was paired.
        # Build and validate the policy BEFORE the idempotency short-circuit.
        # Returning an existing id first meant a repeat of an invalid request
        # (unknown channel, unparseable lead time) came back 200 with the old
        # id, so the caller believed they had set a notification that was never
        # configured.
        policy = _build_notify_policy(
            channel=channel, lead_s=lead_s, lead_message=lead_message,
            message=message, follow_up=follow_up,
            wait_for_ack=wait_for_ack, ack_timeout_s=ack_timeout_s,
            snooze_s=snooze_s, max_follow_ups=max_follow_ups,
            fire_at=fire,
        )
        for _esid, _job in _SCHEDULES.items():
            if not isinstance(_job, dict):
                # A hand-edited or torn schedules.json can hold
                # non-dict entries. The heap build ignores them,
                # but this scan called .get() on every entry and
                # raised AttributeError â€” 500ing every POST
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
        # Notification policy lives beside the job rather than in a second
        # schedule row. Two rows would be independently cancellable, so
        # cancelling a task could leave its pre-notification armed to fire
        # into a reminder the user had already dismissed.
        if policy:
            _SCHEDULES[sid]["notify_policy"] = policy
        heapq.heappush(_HEAP, (fire, sid))
        _save()
    _ensure_worker()
    # Arm the pre-notification as a SEPARATE, derived job keyed to the parent.
    # It carries no agent query, so firing it costs no LLM spend - it only
    # sends the agent-authored lead message.
    if policy and policy.get("lead_fire") and policy["lead_fire"] > now:
        lead_sid = f"{sid}-lead"
        with _SCHED_LOCK:
            _load()
            _SCHEDULES[lead_sid] = {
                "query": "",
                "when": when,
                "next_fire": policy["lead_fire"],
                "recurring": None,
                "conversation_id": conversation_id,
                "notify": False,
                "enabled": True,
                "created": now,
                "parent": sid,
                "notify_policy": policy,
                "phase": "lead",
            }
            heapq.heappush(_HEAP, (policy["lead_fire"], lead_sid))
            _save()
        _log(f"[{sid}] pre-notification armed for "
             f"{round(policy['lead_fire'] - now)}s from now")
    return sid


# ── self-wake ─────────────────────────────────────────────────────────────
# A wake is a scheduled row with a REASON instead of a QUERY. It is a poll with a
# predicate: the tick asks `delta.compute` whether anything it is watching has
# changed, and only a match costs a model turn. Everything below exists to keep
# those two facts separable from the console, from schedules.json and from the
# ledger — a wake that fired and found nothing must be invisible in the cost
# columns and visible in the job's own history.

# Shortest interval a wake may ask for, whatever it proposes. The floor is on the
# POLL, not the turn: "check every minute" is 1,440 external reads a day against
# someone else's API, and that is a rate-limit problem rather than a token one.
WAKE_FLOOR_S = 600
# How long an unmatched watch keeps re-arming itself before it gives up and says
# so. Without it, a wake armed on a condition that never arrives ("watch for the
# reply") re-ticks forever — the boring loop the empty-delta rule does not cover,
# because an empty delta is free but not finite.
WAKE_HORIZON_S = 24 * 3600
# Turns a chain may take. The agent cannot extend that number; only a fresh arm
# from a human turn can start a new chain.
WAKES_LEFT_DEFAULT = 3
WAKES_LEFT_MAX = 10
WAKE_REASON_MAX = 400


def flags_self_wake_on() -> bool:
    """The one switch for the whole capability, read the same way everywhere."""
    try:
        import flags as _flags
        return bool(_flags.is_enabled("agent.self_wake", False))
    except Exception:
        return False


def wake_chain_budget(conversation_id: str) -> tuple[str, int]:
    """The chain already running for this conversation, and what is left of it.

    Read from the store rather than from process memory, because the tool that
    re-arms can be the MCP child — a different process from the agent server
    that armed the first wake. Returns ("", WAKES_LEFT_DEFAULT) when nothing is
    in flight, which is a fresh chain started from a human turn.
    """
    cid = str(conversation_id or "")
    if not cid:
        return "", WAKES_LEFT_DEFAULT
    with _SCHED_LOCK:
        _load()
        rows = [v for v in _SCHEDULES.values()
                if isinstance(v, dict) and v.get("kind") == "wake"
                and str(v.get("conversation_id") or "") == cid]
    if not rows:
        return "", WAKES_LEFT_DEFAULT
    newest = max(rows, key=lambda v: float(v.get("created") or 0))
    left = int(newest.get("wakes_left") or 0) - 1
    return str(newest.get("id") or ""), max(1, min(left, WAKES_LEFT_MAX))


def _wake_interval(every: str) -> int:
    """Seconds between polls, with the floor applied.

    Reuses the schedule parser rather than a second duration grammar: a wake that
    accepts "15m" and one that accepts "15 minutes" would be two features, and
    the model would find the difference.
    """
    now = time.time()
    secs = _next_fire_from_cron(f"every {str(every or '').strip()}", now) - now
    if secs <= 0:
        raise ValueError(f"wake interval {every!r} is not in the future")
    return max(int(secs), WAKE_FLOOR_S)


def schedule_wake(reason: str, every: str = "1h", *, conversation_id: str,
                  watch: dict | None = None,
                  wakes_left: int = WAKES_LEFT_DEFAULT,
                  armed_by: str = "agent", parent: str = "") -> str:
    """Arm one wake: look again in `every`, and only spend a turn if `watch` matched.

    Returns the schedule id. Raises ValueError with a message the model can act
    on, because every one of these refusals is a guardrail doing its job, and a
    generic error would only teach the caller to retry it.
    """
    if not flags_self_wake_on():
        raise ValueError("self-wake is switched off (flag agent.self_wake). "
                         "Tell the user to enable it in Settings rather than "
                         "retrying.")
    text = str(reason or "").strip()
    if not text:
        # The reason is the loop rule. A wake that must state why it will wake
        # again cannot spin on an unspecified impulse, and the sentence is also
        # what the Scheduler page shows the user, so it doubles as the audit.
        raise ValueError("a wake needs a non-empty reason: what it is waiting "
                         "for, and what it should do when that arrives.")
    if not str(conversation_id or "").strip():
        raise ValueError("a wake must name the conversation it reports back to.")
    left = int(wakes_left or 0)
    if left < 1 or left > WAKES_LEFT_MAX:
        raise ValueError(f"wakes_left must be between 1 and {WAKES_LEFT_MAX}")
    step = _wake_interval(every)
    now = time.time()
    sid = f"wake-{uuid.uuid4().hex[:8]}"
    with _SCHED_LOCK:
        _load()
        _SCHEDULES[sid] = {
            # No query, on purpose: `_fire` runs `spec["query"]` through the
            # Executor, and a wake must never reach that path.
            "kind": "wake",
            "query": "",
            "reason": text[:WAKE_REASON_MAX],
            "watch": watch if isinstance(watch, dict) else None,
            "every": str(every or "1h"),
            "step_s": step,
            "when": f"every {step}s",
            "next_fire": now + step,
            # Not `recurring`: the worker's recurring branch re-arms BEFORE
            # firing and would skip the predicate entirely. A wake re-arms
            # itself, deliberately, only when it found nothing.
            "recurring": None,
            "conversation_id": str(conversation_id),
            "notify": False,
            "enabled": True,
            "created": now,
            "wakes_left": left,
            "wakes_fired": 0,
            "armed_by": armed_by if armed_by in ("agent", "user") else "agent",
            "parent": str(parent or ""),
            "watch_until": now + WAKE_HORIZON_S,
        }
        heapq.heappush(_HEAP, (now + step, sid))
        _save()
    _ensure_worker()
    _log(f"[{sid}] wake armed: {text[:80]!r} every {step}s, {left} turn(s) left, "
         f"watch={'yes' if watch else 'delta-non-empty'}")
    return sid


def _wake_predicate(spec: dict) -> tuple[bool, dict]:
    """Model-free: has anything this wake is watching changed since it last looked?

    Returns (matched, delta). No LLM call, no gateway call, nothing a tick could
    bill — the delta is stat-first and bounded, so an unmatched tick costs about
    what a cache read costs.
    """
    import delta
    cid = str(spec.get("conversation_id") or "")
    since = delta.read_cursor(cid) if cid else 0.0
    # A wake can only see what happened WHILE it was armed. Without this floor,
    # a freshly armed "watch for the reply" starts with no watermark, so
    # `since=0` means the whole history — and its first tick matches something
    # from last Tuesday, spends a turn, and reports news the user has already
    # read. After a delivery the advanced cursor is always later than `created`,
    # so this only ever binds the first tick, which is exactly the one that was
    # wrong.
    since = max(float(since or 0.0), float(spec.get("created") or 0.0))
    watch = spec.get("watch") or {}
    surfaces = None
    surface = str(watch.get("surface") or "").strip().lower()
    if surface:
        surfaces = [surface if surface in delta.SURFACES else "documents"]
    out = delta.compute(since=since, surfaces=surfaces, limit=delta.PER_SURFACE)
    changed = out.get("changed") or {}
    if surfaces:
        # Scope enforced here as well as requested above. A wake armed on
        # `documents` must not match a memory entry because a collector returned
        # an extra surface — the predicate's scope is the watch's whole contract.
        changed = {k: v for k, v in changed.items() if k in surfaces}
    entries = [e for group in changed.values() for e in (group or [])]
    if not entries:
        return False, out
    match = watch.get("match") or {}
    if not isinstance(match, dict) or not match:
        # A time-only wake: the predicate IS "the delta is non-empty".
        return True, out
    terms = [str(v).strip().lower() for v in match.values() if str(v).strip()]
    if not terms:
        return True, out
    for e in entries:
        hay = " ".join(str(e.get(k) or "") for k in
                       ("id", "session", "filename", "what", "detail",
                        "path")).lower()
        if all(t in hay for t in terms):
            return True, out
    return False, out


def _wake_rearm(sid: str, spec: dict) -> bool:
    """Re-arm an unmatched wake. False when the watch horizon has run out."""
    horizon = float(spec.get("watch_until") or 0)
    if time.time() >= horizon:
        with _SCHED_LOCK:
            _load()
            row = _SCHEDULES.get(sid)
            if isinstance(row, dict):
                row["enabled"] = False
                row["last_error"] = (
                    "stopped watching: nothing matched within "
                    f"{int(WAKE_HORIZON_S / 3600)}h of being armed")
                _save()
        _log(f"[{sid}] watch expired unmatched; disabled")
        return False
    step = int(spec.get("step_s") or WAKE_FLOOR_S)
    nxt = time.time() + step
    with _SCHED_LOCK:
        _load()
        row = _SCHEDULES.get(sid)
        if not isinstance(row, dict):
            return False
        row["next_fire"] = nxt
        row["enabled"] = True
        _save()
        heapq.heappush(_HEAP, (nxt, sid))
    return True


def _fire_wake(sid: str, spec: dict) -> None:
    """One wake tick. Nothing matched → no turn, no cost, still armed.
    Something matched → one read-only turn in the bound conversation."""
    if not flags_self_wake_on():
        # The kill switch means "never fire", not "cannot be created": rows armed
        # before it was switched off stay stored and stay silent, so switching it
        # back on resumes exactly what was there. Stated on the row, because a
        # watch that is dark and a watch that is armed look identical otherwise.
        _record(sid, "not firing: agent.self_wake is off")
        _log(f"[{sid}] wake skipped — agent.self_wake is off")
        # Pushed forward rather than left past-due: a row the switch is holding
        # would otherwise be re-heaped by the next _load() and skipped again on
        # every scheduler operation, which is churn that proves nothing.
        _wake_rearm(sid, spec)
        return
    t0 = time.time()
    try:
        matched, out = _wake_predicate(spec)
    except Exception as e:  # noqa: BLE001
        _log(f"[{sid}] wake predicate failed: {e!r}")
        _record(sid, f"predicate failed: {e}"[:400])
        _wake_rearm(sid, spec)
        return
    if not matched:
        _record(sid)
        _log(f"[{sid}] nothing new (empty delta) — no turn, no spend "
             f"in {time.time() - t0:.2f}s")
        _wake_rearm(sid, spec)
        return

    left = int(spec.get("wakes_left") or 0)
    if left < 1:
        # Belt and braces: arming past zero is refused by the tool, so reaching
        # here means a hand-edited row. Say so rather than fire it.
        with _SCHED_LOCK:
            _load()
            row = _SCHEDULES.get(sid)
            if isinstance(row, dict):
                row["enabled"] = False
                row["last_error"] = "no wakes left in this chain"
                _save()
        _log(f"[{sid}] chain exhausted; disabled")
        return

    cid = str(spec.get("conversation_id") or "")
    before = None
    try:
        from agent_server import _session_cost_breakdown
        before = _session_cost_breakdown(cid)
    except Exception as e:  # noqa: BLE001
        _log(f"[{sid}] cost snapshot failed (non-fatal): {e}")
    answer, error = "", None
    try:
        from agent_server import _run_wake_turn
        answer = _run_wake_turn(conversation_id=cid, spec=spec, delta=out,
                               schedule_id=sid)
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"[:400]
        _log(f"[{sid}] wake turn failed: {traceback.format_exc()}")
    _record(sid, error)
    if error:
        # A FAILED turn does not consume the chain. The change it was armed for
        # is still unseen because the cursor only moves on delivery, so burning
        # a step here would let a transient failure — a busy conversation, a
        # dead gateway — silently eat the watch and drop the finding. Re-arm and
        # try again on the next tick; the horizon is what bounds that, and the
        # reason is visible on the row.
        _log(f"[{sid}] wake turn failed, re-armed without spending a step")
        _wake_rearm(sid, spec)
        return
    try:
        if before is not None and cid:
            from agent_server import _session_cost_delta, _record_turn_cost
            cost = _session_cost_delta(cid, before)
            if cost:
                _record_turn_cost(cid, f"[wake] {str(spec.get('reason'))[:120]}",
                                  cost)
                _log(f"[{sid}] cost recorded: {cost}")
    except Exception as e:  # noqa: BLE001
        _log(f"[{sid}] cost record failed (non-fatal): {e}")
    # Consume the chain step, and do NOT re-arm: a wake continues only by calling
    # schedule_wake itself, with a reason. The default is that the chain ends
    # here, which is what keeps "watch this forever" something only the user can
    # ask for.
    with _SCHED_LOCK:
        _load()
        row = _SCHEDULES.get(sid)
        if isinstance(row, dict):
            row["enabled"] = False
            row["wakes_left"] = left - 1
            row["wakes_fired"] = int(row.get("wakes_fired") or 0) + 1
            _save()
    _log(f"[{sid}] wake ran in {round(time.time() - t0, 1)}s -> "
         f"{str(answer or '')[:140]!r}")


# â”€â”€ notification policy â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _coerce_seconds(v, field: str, *, minimum: float = 0.0,
                    maximum: float = 7 * 86400) -> float:
    """Accept a number or a duration string ('15m', '2h', '90s').

    The agent passes LLM-authored text straight through, so `lead_s: "fifteen
    minutes"` has to fail loudly here rather than silently becoming 0 (which
    would mean "no pre-notification", the opposite of what was asked for)."""
    if v is None or v == "":
        return 0.0
    if isinstance(v, bool):
        raise ValueError(f"{field} must be a number of seconds or a duration")
    if isinstance(v, (int, float)):
        s = float(v)
    else:
        import re as _re
        m = _re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd])?\s*",
                          str(v), _re.I)
        if not m:
            raise ValueError(
                f"{field}={v!r}: use seconds (900) or a duration like "
                f"'15m', '2h', '1d'")
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        s = float(m.group(1)) * mult.get((m.group(2) or "s").lower(), 1)
    if s < minimum:
        raise ValueError(f"{field} must be >= {minimum:g}")
    return min(s, maximum)


def _build_notify_policy(*, channel: str, lead_s, lead_message: str,
                         message: str, follow_up: str, wait_for_ack: bool,
                         ack_timeout_s, snooze_s, max_follow_ups,
                         fire_at: float) -> dict:
    """Validate and assemble the notification policy for one schedule.

    Returns {} when nothing was requested, so the common case persists exactly
    what it always did."""
    channel = (channel or "").strip()
    if channel:
        # Validate against the registry the gateway uses, at schedule time.
        # An unknown channel used to be accepted silently and then failed at
        # delivery, an hour later, with the task already billed.
        try:
            import notify as _notify
            known = {str(c.get("name") or "") for c in
                     _notify.available_channels()}
        except Exception:
            known = set()
        if known and channel.lower() not in known:
            raise ValueError(
                f"unknown channel {channel!r}; use one of "
                f"{', '.join(sorted(known))}")
    lead_s = _coerce_seconds(lead_s, "lead_s")
    ack_timeout_s = _coerce_seconds(ack_timeout_s or 900, "ack_timeout_s",
                                    minimum=30)
    snooze_s = _coerce_seconds(snooze_s, "snooze_s")
    if not any([channel, lead_s, message, follow_up, wait_for_ack, snooze_s]):
        return {}
    try:
        n = int(max_follow_ups)
    except (TypeError, ValueError):
        raise ValueError("max_follow_ups must be an integer 0-10")
    if not 0 <= n <= 10:
        raise ValueError("max_follow_ups must be between 0 and 10")
    lead_fire = 0.0
    if lead_s:
        lead_fire = fire_at - lead_s
        if lead_fire <= time.time():
            raise ValueError(
                f"lead_s={lead_s:g}s is longer than the time until the task "
                f"({max(0, fire_at - time.time()):.0f}s), so the "
                f"pre-notification would fire in the past")
    return {
        "channel": channel,
        "lead_fire": lead_fire,
        "lead_message": str(lead_message or ""),
        "message": str(message or ""),
        "follow_up": str(follow_up or ""),
        "wait_for_ack": bool(wait_for_ack),
        "ack_timeout_s": ack_timeout_s,
        "snooze_s": snooze_s,
        "max_follow_ups": n,
    }


def list_schedules() -> list[dict]:
    """Read every schedule. This is a READ and must not mutate stored state.

    It used to purge as a side effect, because fired one-shots are marked
    disabled by the worker rather than by cancel(), so ghost rows accumulated.
    But the console polls this endpoint every 15s, which means simply OPENING
    the Scheduler page deleted the operator's execution history: a month-old
    executed one-shot was gone after one page load, with nothing in the
    response saying so. A read that deletes is not a read; the purge already
    has its own endpoint (`purge_disabled`) which reports how many it removed,
    so the caller opts in explicitly.
    """
    with _SCHED_LOCK:
        _load()
        # Return ALL schedules (enabled and disabled). Cancelled schedules
        # remain visible so callers can inspect the `enabled` flag; the UI
        # layer decides whether to grey them out. Non-dict entries (see
        # schedule()) are dropped here too â€” `{**"garbage"}` raised
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
            # ran â€” one stray agent turn after a successful cancel.
            spec["_cancelled"] = True
            _save()
            # No purge here either. Cancelling is a write the user asked for,
            # and it should not silently discard *other* jobs' history as a
            # side effect; `purge_disabled()` remains the explicit, reported
            # way to clear out aged disabled rows.
            return True
    return False


def _reparse_when(when: str, base: float | None = None) -> float:
    """Resolve a stored `when` string to an absolute fire time.

    Handles every form `schedule()` accepts â€” relative delays, `tomorrow
    HH:MM`, recurring specs, ISO and epoch â€” in one place.

    It exists because `schedule()`'s dispatch and `_next_fire_from_cron` are
    not the same parser: the cron helper cannot read "in 2h", so anything that
    re-derives a fire time from stored text (re-enabling a disabled schedule)
    broke on the most common reminder there is. Two parsers for one field is
    how that happened.
    """
    import re as _re
    now = base if base is not None else time.time()
    low = when.strip().lower()
    if low.startswith("in "):
        m = _re.fullmatch(
            r"(\d+)\s*(m|min|mins|minutes|minute|h|hr|hrs|"
            r"hour|hours|s|sec|secs|second|seconds)?", low[3:].strip())
        if not m:
            raise ValueError(f"unparseable delay: {when!r}")
        n, unit = int(m.group(1)), (m.group(2) or "m")
        per = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
               "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
               "h": 3600, "hr": 3600, "hrs": 3600,
               "hour": 3600, "hours": 3600}[unit]
        return now + n * per
    return _next_fire_from_cron(when, now)


def set_enabled(sid: str, enabled: bool) -> dict:
    """Enable or disable a schedule without deleting it.

    There was no way back from a disabled job. `cancel_scheduled` removes one,
    and `schedule_task` creates a new one, so "the reminder is greyed out, turn
    it on" had no answer â€” and the model's only available move was to schedule
    a second copy, which left the user with two identical rows, both still
    disabled, and no better off. The enabled flag is state, not existence, so
    re-enabling has to be possible.

    A one-shot whose fire time has passed is re-armed only if `when` still
    makes sense: the stored `when` is re-parsed, and a job whose time has
    genuinely passed stays disabled with the reason attached rather than
    firing instantly. That is the failure this function exists to undo, so it
    must not reproduce it.
    """
    with _SCHED_LOCK:
        _load()
        spec = _SCHEDULES.get(sid)
        if not isinstance(spec, dict):
            return {"ok": False, "error": f"unknown schedule {sid!r}"}
        if not enabled:
            spec["enabled"] = False
            # A disable can land between the worker's snapshot and its fire.
            # `enabled` alone is not consulted after that snapshot, so mark it
            # cancelled for the same reason `cancel()` does.
            spec["_cancelled"] = True
            _save()
            return {"ok": True, "schedule_id": sid, "enabled": False}
        raw_when = str(spec.get("when") or "").strip()
        if not spec.get("recurring") and raw_when:
            # Re-parse through `schedule()`'s own dispatch, not
            # `_next_fire_from_cron`: that helper only understands cron, ISO
            # and epoch, so a RELATIVE reminder ("in 2h") could not be
            # re-armed at all â€” it raised "could not convert string to float".
            # A user disabling and re-enabling the most common kind of
            # reminder hit an error.
            try:
                fire = _reparse_when(raw_when)
            except ValueError as e:
                spec["enabled"] = False
                spec["last_error"] = f"cannot re-arm {sid}: {e}"[:400]
                _save()
                return {"ok": False, "schedule_id": sid,
                        "error": spec["last_error"],
                        "hint": "cancel it and schedule a new one"}
            if fire <= time.time():
                # Leave it OFF. Returning ok:False while the row stayed
                # `enabled` would report a refusal while leaving the job armed,
                # so the next worker tick would fire a reminder the user was
                # just told was not running.
                spec["enabled"] = False
                spec["last_error"] = (
                    f"not re-enabled: {raw_when!r} is in the past")[:400]
                _save()
                return {"ok": False, "schedule_id": sid,
                        "error": spec["last_error"],
                        "hint": "cancel it and schedule a new one with a "
                                "future time, e.g. 'tomorrow 9am'"}
            spec["next_fire"] = fire
        spec["enabled"] = True
        # Re-enabling is the inverse of cancel/disable: clear the marker or
        # the worker will keep skipping this row forever.
        spec.pop("_cancelled", None)
        spec.pop("last_error", None)
        heapq.heappush(_HEAP, (float(spec.get("next_fire") or time.time()), sid))
        _save()
    _ensure_worker()
    return {"ok": True, "schedule_id": sid, "enabled": True,
            "next_fire": spec.get("next_fire"),
            "when": spec.get("when")}


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

    A hand-edited string `next_fire` can exist â€” _load() only
    type-checks for the heap â€” and `float("soon")` raised ValueError
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
        # test true for EVERY past fire â€” a purge with max_age_days=-1
        # deleted all disabled one-shots regardless of age.
        raise ValueError("max_age_days must be >= 0")
    with _SCHED_LOCK:
        _load()
        return _purge_locked(float(max_age_days) * 86400)


# Tasks that are really just a nudge. The research DAG is the wrong tool for
# these: it runs planner -> researcher -> formatter -> action, bills four or
# five model calls, takes 20-30s, and returns a REPORT about e.g. "take a
# break and refocus". The user asked to be reminded, not researched.
#
# The signal is the agent's own `message`: if it wrote the words to send, the
# text IS the deliverable and running a pipeline to produce more text is both
# slow and wrong. A job with no authored message still runs the DAG, because
# then the task genuinely is "go and do/find something".
# A reminder request is a sentence about the USER ("remind me to stretch"),
# not an instruction to an agent ("check the weather and notify me"). Storing
# the former as `query` is what produced the live loop behind
# sch-14effc0f: every two hours the job re-ran "remind me to stretch and drink
# water", the agent dutifully called schedule_task again, and the user got a
# scheduling CONFIRMATION instead of a reminder — carrying a dead id
# (sch-388b780f) that had already fired once and been disabled.
#
# The scheduler is the last place that can tell the difference, so it does.
_REMINDER_IMPERATIVE = _re.compile(
    r"^\s*(?:please\s+)?(?:can\s+you\s+|could\s+you\s+|would\s+you\s+)?"
    r"(?:remind\s+(?:me|us)|reminder\s+(?:to|for)\b|nudge\s+me|ping\s+me|"
    r"don't\s+let\s+me\s+forget|dont\s+let\s+me\s+forget|"
    r"tell\s+me\s+to\b|prompt\s+me\s+to\b)",
    _re.I)


def _reminder_text(query: str) -> str:
    """The sentence a reminder should SAY, or "" if this is not one.

    "remind me to stretch and drink water" -> "Stretch and drink water."
    A query that asks for work ("check the weather and tell me if it rains")
    returns "", because that job must still run.
    """
    q = str(query or "").strip()
    if not q or not _REMINDER_IMPERATIVE.match(q):
        return ""
    # Strip the imperatives that wrap the actual message.
    body = _REMINDER_IMPERATIVE.sub("", q).strip()
    body = _re.sub(r"^(?:to|that|about)\s+", "", body, flags=_re.I).strip()
    # Drop a trailing clock expression: the timing is already in `when`, and
    # "stretch and drink water in 1 hour" reads wrong when it arrives on time.
    body = _re.sub(
        r"[\s,]+(?:in\s+\d+\s*(?:s|sec|secs|seconds?|m|min|mins|minutes?|"
        r"h|hr|hrs|hours?|d|days?)|at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?|"
        r"tomorrow(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?|"
        r"tonight|every\s+\S+)\s*$",
        "", body, flags=_re.I).strip(" .,:;-")
    if not body:
        return ""
    # A request for the AGENT to go and find something out is work, not a
    # nudge: "notify me if it rains", "summarise yesterday", "look up the
    # price". A reminder to do a HUMAN action keeps its verb - "remind me to
    # check the oven" is you doing the checking, and dropping it to nothing or
    # running a pipeline for it are both wrong.
    if _re.search(r"\b(?:notify\s+me|tell\s+me\s+if|let\s+me\s+know|"
                  r"summari[sz]e|look\s+up|find\s+out|search\s+for|"
                  r"monitor|keep\s+an\s+eye)\b", body, _re.I):
        return ""
    # A bare work verb with no human subject is the agent's job; a verb with a
    # following object the user owns ("check the oven", "read me a story") is
    # a reminder. Only reject when the sentence is nothing but the verb.
    if _re.fullmatch(r"(?:check|search|find|fetch|read|list)\b.*", body,
                     _re.I) and _re.search(
            r"\b(?:the\s+(?:weather|news|price|stock|market|inbox|calendar)|"
            r"yesterday|today\s+console|my\s+(?:inbox|calendar|email))\b",
            body, _re.I):
        return ""
    return body[:1].upper() + body[1:] + ("." if not body.endswith((".", "!", "?")) else "")


def _is_nudge_only(spec: dict) -> bool:
    """A row that exists only to deliver text the agent already wrote.

    The message is the discriminator, and that is the documented contract of
    `schedule_task` (see its NUDGE paragraph) — not an accident of this
    function. An earlier revision of this docstring argued the opposite from
    live row `sch-1d2034f0` (query `whats_new surfaces=documents`, message
    "Checking for new documents..."): the message went out, the query never
    ran, and the user was told a check had happened. That row is real, but the
    fix chosen here was wrong. Every pure nudge also carries a query —
    "take a break and refocus" is stored as one — so "a non-empty query means
    do work" sends every reminder through planner -> researcher -> formatter
    and bills four or five model calls to be told to stretch, which is the
    exact cost this branch exists to avoid. `tests/test_reminder_routing.py`
    caught it.

    So the ambiguity stays where it belongs: at the moment the model chooses.
    `prompts/action.md` and the tool doc both tell it that setting `message`
    means nothing runs, and a job that must produce something leaves it empty.
    """
    pol = spec.get("notify_policy") or {}
    return bool(str(pol.get("message") or "").strip())


def _session_research_context(sid: str) -> str:
    """The most recent research findings filed for this session, as text.

    Reads the session's own graph.json (local, no gateway): the latest
    researcher/retriever/distiller write-up. Empty when the session has no
    research, so a plain reminder is never burdened with unrelated context.
    Bounded so a huge research doc cannot blow up the wake prompt.
    """
    try:
        import json as _json
        from persistence import SESSIONS_ROOT
        p = SESSIONS_ROOT / sid / "graph.json"
        if not p.exists():
            return ""
        data = _json.loads(p.read_text(encoding="utf-8-sig"))
        findings: list[str] = []
        for n in (data.get("nodes") or []):
            if not isinstance(n, dict):
                continue
            if str(n.get("skill") or "") not in ("researcher", "retriever",
                                                  "distiller"):
                continue
            res = n.get("result")
            out = res.get("output") if isinstance(res, dict) else None
            if not isinstance(out, dict):
                continue
            for key in ("findings", "summary", "text"):
                v = out.get(key)
                if isinstance(v, str) and v.strip():
                    findings.append(v.strip())
                    break
        if not findings:
            return ""
        # The LAST filed write-up is the most recent research this session did.
        return ("CONTEXT FROM YOUR PREVIOUS RESEARCH THIS SESSION "
                "(use it; do not redo the work):\n\n" + findings[-1][:2500])
    except Exception:  # noqa: BLE001
        return ""


def _graph_has_unfinished(sid: str) -> bool:
    """Does this session's persisted graph still have pending/running nodes?

    A graph.json in NetworkX node_link_data shape: {"nodes": [{"id", "status",
    ...}], "edges": [...]}. Any node not yet `complete`/`failed`/`skipped` means
    an interrupted run is waiting to be resumed — the only case where
    `Executor.run(resume=True)` should be used.
    """
    try:
        from persistence import SESSIONS_ROOT
        p = SESSIONS_ROOT / sid / "graph.json"
        if not p.exists():
            return False
        import json as _json
        data = _json.loads(p.read_text(encoding="utf-8-sig"))
        for n in (data.get("nodes") or []):
            if isinstance(n, dict) and n.get("status") in ("pending", "running"):
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


def _fire(sid: str, spec: dict | None = None) -> None:
    """Run one scheduled task.

    `spec` is the snapshot taken by the worker under _SCHED_LOCK â€” _fire
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
    # conversation_id was accepted, persisted and then ignored â€”
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
        import asyncio
        if _is_nudge_only(spec):
            # The agent already wrote the message. Do not run a pipeline to
            # generate text nobody asked for: send what it wrote and spend
            # nothing. The body delivered is the reminder's own text, so this
            # path costs zero model calls and finishes in milliseconds.
            answer = ""
            _log(f"[{sid}] nudge-only: delivering the agent's message "
                 f"without running a task ({_t.time() - _start:.2f}s)")
        else:
            from flow import Executor
            # RELIABILITY FIX (resume on missing session): the scheduler used
            # to pass resume=True for a brand-new session id, but Executor.run
            # raises when there is no persisted graph for that id. Detect a
            # missing session and start fresh instead. Probe the graph file
            # directly â€” constructing SessionStore would mkdir empty session
            # dirs that pollute list_sessions().
            # Resume ONLY if the prior run left unfinished work. Resuming a
            # fully-complete graph makes Executor.run find no ready nodes and
            # replay the OLD answer — so a scheduled follow-up (or a self-wake
            # carrying a NEW goal) would silently do nothing. A fresh graph is
            # what actually runs the new query; memory/turnlog still carry
            # continuity through the session id.
            resume = _graph_has_unfinished(derived_sid)
            # CONTEXT INJECTION: a scheduler-fired continuation must not run on a
            # terse query alone. If this session produced research, prepend it so
            # the fresh planner has the full findings inline (no search needed) —
            # this is "waking the future self with full info", not a bare goal.
            _run_query = spec["query"]
            _ctx = _session_research_context(derived_sid)
            if _ctx:
                _run_query = f"{spec['query']}\n\n{_ctx}"
                _log(f"[{sid}] injected {len(_ctx)} chars of prior research "
                     f"into the wake query")
            answer = asyncio.run(
                Executor().run(_run_query,
                               session_id=derived_sid, resume=resume)
            )
            _log(f"[{sid}] ok in {round(_t.time() - _start, 1)}s -> "
                 f"{str(answer or '')[:160]!r}")
    except Exception as e:
        answer = f"[scheduler] run failed: {e}"
        error = str(e)[:400]
        _log(f"[{sid}] run failed: {traceback.format_exc()}")
    # Record the outcome even when notify=False â€” previously the result
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
        # ALWAYS record the result in the in-app feed first. A channel may be
        # unconfigured, unpaired or unreachable, and a reminder whose outcome
        # exists only in a message that never arrived is a reminder that did
        # not happen.
        try:
            from agent_server import _notif_add
            # A nudge-only job produces no answer by design, so recording
            # "(no answer)" there would read as a failure.
            summary = (answer or "").strip()[:400] or "delivered"
            _notif_add("scheduled",
                       f"{spec['query'][:160]} -> {summary}",
                       derived_sid)
        except Exception as e:
            _log(f"[{sid}] in-app record failed: {e}")
        pol = spec.get("notify_policy") or {}
        channel = str(pol.get("channel") or "")
        # The agent writes the message it wants sent when it schedules the
        # task. Both branches below send it verbatim; neither reformats it.
        custom = str(pol.get("message") or "").strip()
        if channel:
            # An agent-authored fire-time message, delivered on the channel the
            # agent chose. When the job also had a query, the answer is part of
            # the delivery — the message alone is a promise, and sending only
            # the promise is what made "checking for new documents" arrive with
            # no check in it.
            #
            # Composed by the channel formatter: the run's answer is console
            # markdown (headings, `[page](url)` links, a Sources list), and a
            # phone renders all of it literally. The old line pasted the raw
            # query in as the headline.
            import channelformat as _cf
            text = _cf.channel_text(answer=answer or "", query=spec["query"],
                                    message=custom)
            _deliver(sid, channel, text, derived_sid,
                     str(pol.get("follow_up") or ""))
        else:
            try:
                from agent_server import notify_task_done
                notify_task_done(spec["query"], answer or "",
                                 round(_t.time() - _start, 1), derived_sid,
                                 scheduled=True, message=custom)
            except Exception as e:
                _log(f"[{sid}] notify failed: {e}")


def _deliver(sid: str, channel: str, text: str, session_id: str,
             follow_up: str = "") -> None:
    """Send a notification and record the outcome, whatever it is.

    Never raises: a channel failure must not turn a completed task into a
    failed one. The delivery attempt is logged either way, because the whole
    reason this exists is that a silent non-delivery is indistinguishable from
    a reminder that was never set."""
    try:
        import notify as _notify
        res = _notify.send_channel_message(
            channel, text, agent="scheduler", session_id=session_id)
    except Exception as e:
        _log(f"[{sid}] deliver raised on {channel}: {e!r}")
        return
    if res.get("delivered"):
        _log(f"[{sid}] -> {channel}: delivered")
        return
    why = str(res.get("error") or "unknown")[:200]
    _log(f"[{sid}] -> {channel}: NOT delivered ({why})")
    hint = ""
    if res.get("reauth_needed"):
        hint = (f" The {channel} credential looks invalid"
                f"{' (' + str(res.get('reauth_hint')) + ')' if res.get('reauth_hint') else ''}"
                f" - refresh it, then re-arm.")
    try:
        from agent_server import _notif_add
        _notif_add("scheduled",
                   f"could not deliver the notification for {sid[:160]} on "
                   f"{channel}: {why}.{hint}", session_id)
    except Exception:
        pass


def _worker() -> None:
    while not _STOP.wait(timeout=1.0):
        try:
            _worker_tick()
        except Exception:
            # One poison entry (an unparseable `recurring`, an
            # unwritable state dir) used to raise out of the loop and
            # kill the thread â€” and because _ensure_worker only
            # restarts it on the NEXT schedule() call, which re-heaps
            # the same entry from disk, every restarted worker died
            # again immediately: one bad value silently stopped ALL
            # scheduled work. Log, skip the tick, stay alive.
            _log(f"worker tick failed, skipping: {traceback.format_exc()}")
        # Event-driven self-wakes (e.g. "wake when an email from X arrives").
        # Cheap: poll_due throttles the actual Gmail call per record via
        # `every_s`, so this is a no-op on most ticks.
        try:
            import sleeps
            sleeps.poll_due()
        except Exception:  # noqa: BLE001
            _log(f"sleeps.poll_due failed, skipping: {traceback.format_exc()}")


def _worker_tick() -> None:
    global _WORKER_MTIME
    now = time.time()
    skip_missed = False
    with _SCHED_LOCK:
        # Pick up schedules written by OTHER processes (the MCP child, where
        # schedule_task/schedule_self actually run) since our last load. Without
        # this the worker's in-memory heap is frozen at startup and a reminder
        # created in chat never fires until the agent restarts.
        try:
            _mt = _SCHED_PATH.stat().st_mtime
        except OSError:
            _mt = 0.0
        if _WORKER_MTIME is not None and _mt != _WORKER_MTIME:
            _WORKER_MTIME = _mt
            _load()
        if not _HEAP:
            return
        fire, sid = _HEAP[0]
        if now < fire:
            return
        heapq.heappop(_HEAP)
        spec = _SCHEDULES.get(sid)
        if not spec or not isinstance(spec, dict) or not spec.get("enabled"):
            return
        # CRITICAL: snapshot BEFORE mutating. The worker used to set
        # enabled=False and then pass that same dict to _fire, whose
        # own enabled guard aborted the run â€” so every ONE-SHOT
        # schedule silently fired nothing. Capture the still-enabled
        # spec first, then persist the disable (crash safety), then
        # hand the untouched snapshot to _fire.
        snap = dict(spec)
        # Re-arm recurring schedules before firing.
        if spec.get("recurring"):
            try:
                spec["next_fire"] = _advance_recurring(
                    spec["recurring"], float(fire), now)
            except (ValueError, OSError) as e:
                # A corrupt `recurring` value used to raise HERE â€”
                # inside the loop with no guard â€” killing the worker
                # (see _worker). Disable the job itself so the
                # scheduler keeps serving every OTHER job, and say
                # why on the job's row.
                spec["enabled"] = False
                spec["last_error"] = f"unparseable recurring: {e}"[:400]
                _save()
                _log(f"[{sid}] disabled â€” unparseable recurring "
                     f"{spec.get('recurring')!r}: {e}")
                return
            heapq.heappush(_HEAP, (spec["next_fire"], sid))
            _save()
            # The most recent slot on the grid is `next_fire - step`.
            # If it fell due well before this process started, it was
            # missed while the agent was down: skip it rather than firing
            # immediately and re-anchoring (that is what turned 02:44 /
            # 04:44 into 03:02 / 05:02). A slot that was merely late
            # because the worker was busy still fires.
            step = _interval_seconds(spec["recurring"])
            if step:
                last_slot = spec["next_fire"] - step
                skip_missed = (
                    last_slot < _PROCESS_START - _RECUR_START_GRACE_S)
        else:
            # RELIABILITY FIX (one-shot re-fire): fired one-shot items
            # used to stay enabled=True with a stale past next_fire, so
            # any later _load() re-heaped them and they fired AGAIN.
            # Disable + persist BEFORE executing so a crash mid-fire
            # (or any reload) can never resurrect the schedule.
            #
            # Wakes are exempt, because the decision to consume the row is
            # theirs: `_fire_wake` disables it after a DELIVERED turn, re-arms on
            # a no-match tick, and re-arms without spending a step when the turn
            # fails. Disarming here would also mean a wake blocked by
            # `agent.self_wake` being off was destroyed by the switch rather than
            # paused — observed: three armed watches read as finished after one
            # restart, with no way to resume them. A crash mid-delivery instead
            # re-fires, which is the intended at-least-once: the cursor has not
            # moved, so the finding is not lost.
            if snap.get("kind") != "wake":
                spec["enabled"] = False
                _save()
    if skip_missed:
        _log(f"[{sid}] skipped a missed {spec['recurring']!r} slot "
             f"({int(now - fire)}s before startup); next "
             f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(spec['next_fire']))}")
        return
    # A cancel() that landed between the snapshot and here must not
    # still fire â€” the snapshot predates the cancel and is still
    # enabled (see cancel()).
    with _SCHED_LOCK:
        live = _SCHEDULES.get(sid)
    if live is not None and isinstance(live, dict) and live.get("_cancelled"):
        _log(f"[{sid}] skipped - cancelled before it fired")
        return
    # Fire outside the lock, with the still-enabled snapshot.
    if snap.get("phase") == "lead":
        # A pre-notification carries no query, so it must not run the agent.
        # Routing it through _fire would bill a full LLM turn to say "this is
        # a reminder", and would record a run in Runs for a message that was
        # never a task.
        _fire_lead(sid, snap)
        return
    if snap.get("kind") == "wake":
        # Routed here rather than inside _fire so the reminder path never sees a
        # row with an empty query. _fire would have handed that empty string to
        # the Executor and billed a turn to ask it nothing.
        _fire_wake(sid, snap)
        return
    try:
        _fire(sid, snap)
    except Exception as e:
        _log(f"[{sid}] fire raised: {traceback.format_exc()}")
        _record(sid, repr(e))


def _fire_lead(sid: str, spec: dict) -> None:
    """Send the pre-notification. Never runs the agent, never blocks the
    task, and always records locally even when the channel is unreachable."""
    pol = spec.get("notify_policy") or {}
    channel = str(pol.get("channel") or "")
    text = str(pol.get("lead_message") or "").strip()
    parent = spec.get("parent") or ""
    _record(sid, "pre-notification sent")
    if not channel or not text:
        return
    try:
        from notify import send_channel_message
        res = send_channel_message(channel, text,
                                    agent="scheduler", session_id=parent or sid)
    except Exception as e:
        _log(f"[{sid}] lead notify failed: {e!r}")
        try:
            from agent_server import _notif_add
            _notif_add("scheduled",
                       f"pre-notification for {parent} could not be sent "
                       f"via {channel}: {e}"[:400], parent or sid)
        except Exception:
            pass
        return
    _log(f"[{sid}] lead -> {channel}: {res}")
    if pol.get("wait_for_ack"):
        try:
            import ack as _ack
            import ack_bridge as _bridge
            _ack.open_thread(parent or sid, channel,
                             str(res.get("target") or ""),
                             query=str(spec.get("query") or ""),
                             ack_timeout_s=float(pol.get("ack_timeout_s") or 900),
                             snooze_s=float(pol.get("snooze_s") or 0),
                             max_follow_ups=int(pol.get("max_follow_ups") or 0),
                             prompt=str(pol.get("lead_message") or text))
            _bridge.start_nudger()
            _log(f"[{sid}] ack thread opened for {parent}")
        except Exception as e:
            _log(f"[{sid}] ack thread failed: {e!r}")


def _ensure_worker() -> None:
    global _WORKER
    if _WORKER is None or not _WORKER.is_alive():
        _WORKER = threading.Thread(target=_worker, daemon=True)
        _WORKER.start()


def start() -> None:
    """Load persisted schedules and start the background worker."""
    global _WORKER_MTIME
    with _SCHED_LOCK:
        _load()
        try:
            _WORKER_MTIME = _SCHED_PATH.stat().st_mtime
        except OSError:
            _WORKER_MTIME = 0.0
    _ensure_worker()

