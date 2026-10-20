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
def _is_nudge_only(spec: dict) -> bool:
    pol = spec.get("notify_policy") or {}
    return bool(str(pol.get("message") or "").strip())


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
            # agent chose.
            text = custom or (
                f"{spec['query'][:160]}\n\n"
                f"{(answer or '').strip()[:900] or '(no answer)'}")
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
        # own enabled guard aborted the run â€” so every ONE-SHOT
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
        else:
            # RELIABILITY FIX (one-shot re-fire): fired one-shot items
            # used to stay enabled=True with a stale past next_fire, so
            # any later _load() re-heaped them and they fired AGAIN.
            # Disable + persist BEFORE executing so a crash mid-fire
            # (or any reload) can never resurrect the schedule.
            spec["enabled"] = False
            _save()
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
    with _SCHED_LOCK:
        _load()
    _ensure_worker()

