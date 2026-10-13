"""Acknowledgement threads: a pre-notification the user can answer.

The problem this solves
-----------------------
A one-way reminder ("standup in 10 minutes") is fire-and-forget. If it arrives
while the user is in a meeting, it is simply lost, and nothing tells either
side that. This makes the pre-notification a conversation:

    agent -> user   the lead message, N minutes before the task
    user  -> agent  whatever they reply
    agent -> user   a reply, only if the agent judges one is warranted
                    (repeating up to `max_follow_ups`, spaced by `snooze_s`)
    ...             until the user acknowledges, or the thread expires

Design decisions worth stating
------------------------------
* **The agent decides, this module does not judge.** `decide` is what the LLM
  is asked, and its structured answer is `acknowledge | reply | silent`. The
  bookkeeping here only enforces the budget and the spacing, so a model that
  ignores the instruction cannot produce an unbounded message loop.

* **Bounded by construction.** `max_follow_ups` is capped at 10 and every
  expiry is absolute. A thread that is never acknowledged expires; it does not
  nudge forever.

* **Persist to disk.** A reminder that must survive an agent restart is no use
  if its thread lives in memory.

* **Trust is enforced at the edge.** `handle_inbound` only accepts a thread
  from the exact channel+sender the thread was opened for. An acknowledgement
  from a stranger is recorded as rejected, never acted on — otherwise anyone
  who can message the bot could drive the agent's LLM.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

STATE_DIR = Path(os.environ.get("S9_STATE_DIR")
                 or (Path(__file__).parent / "state"))
STORE_PATH = STATE_DIR / "ack_threads.json"
_LOCK = threading.Lock()
_MAX = 200

# What the LLM is allowed to say. Anything else is treated as `silent`, so a
# malformed or hostile completion cannot invent an action.
VERDICTS = ("acknowledge", "reply", "silent")


def _load() -> dict:
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STORE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(STORE_PATH)
    except Exception:
        pass


def open_thread(schedule_id: str, channel: str, target: str, *,
                query: str = "", prompt: str = "",
                ack_timeout_s: float = 900,
                snooze_s: float = 0,
                max_follow_ups: int = 0) -> dict:
    """Start a conversation for one pre-notification.

    `ack_timeout_s` bounds the whole thread, not each gap: a thread that keeps
    getting replies still ends."""
    now = time.time()
    try:
        n = max(0, min(10, int(max_follow_ups)))
    except (TypeError, ValueError):
        n = 0
    thread = {
        "schedule_id": schedule_id,
        "channel": channel,
        "target": target,
        "query": query,
        "prompt": prompt,
        "opened": now,
        "expires": now + max(60.0, float(ack_timeout_s or 900)),
        "snooze_s": float(snooze_s or 0),
        "max_follow_ups": n,
        "follow_ups": 0,
        "last_agent_msg": now,
        "status": "open",
        "history": [],
    }
    with _LOCK:
        data = _load()
        data[schedule_id] = thread
        # Bound the store; oldest first.
        if len(data) > _MAX:
            keep = sorted(data.items(), key=lambda kv: kv[1].get("opened", 0))
            data = dict(keep[-_MAX:])
        _save(data)
    return thread


def get_thread(schedule_id: str) -> dict | None:
    with _LOCK:
        t = _load().get(schedule_id)
    if not isinstance(t, dict):
        return None
    if t.get("status") == "open" and time.time() > float(t.get("expires", 0)):
        t["status"] = "expired"
    return t


def find_thread_for(channel: str, sender: str) -> dict | None:
    """The open thread this inbound message belongs to, if any.

    Matching is on channel AND sender, because a thread is a private
    conversation. Falling back to "any open thread on this channel" would let
    the first person to message the bot answer somebody else's reminder."""
    if not sender:
        return None
    now = time.time()
    with _LOCK:
        data = _load()
    best = None
    for t in data.values():
        if not isinstance(t, dict):
            continue
        if t.get("channel") != channel or t.get("target") != sender:
            continue
        if t.get("status") != "open":
            continue
        if now > float(t.get("expires", 0)):
            continue
        if best is None or float(t.get("opened", 0)) > float(best.get("opened", 0)):
            best = t
    return best


def record_user_reply(schedule_id: str, text: str, *, sender: str = "") -> dict:
    """Append the user's reply. Returns the thread after the update."""
    now = time.time()
    with _LOCK:
        data = _load()
        t = data.get(schedule_id)
        if not isinstance(t, dict):
            return {}
        t["history"].append({"role": "user", "text": str(text)[:2000],
                             "ts": now})
        # Any reply resets the snooze clock: the user is engaged, so the next
        # nudge is scheduled from now rather than from the previous silence.
        t["last_agent_msg"] = now
        t["last_user_reply"] = now
        data[schedule_id] = t
        _save(data)
    return t


def acknowledge(schedule_id: str, note: str = "") -> dict:
    """Close a thread because the user confirmed. Idempotent."""
    with _LOCK:
        data = _load()
        t = data.get(schedule_id)
        if not isinstance(t, dict):
            return {}
        t["status"] = "acknowledged"
        t["closed"] = time.time()
        if note:
            t["history"].append({"role": "system", "text": str(note)[:500],
                                 "ts": time.time()})
        data[schedule_id] = t
        _save(data)
    return t


def apply_verdict(schedule_id: str, verdict: Any, reply_text: str = "") -> dict:
    """Apply the LLM's decision, enforcing the budget.

    This is the guard that makes an LLM-driven loop safe: whatever the model
    asks for, the number of agent messages is capped at `max_follow_ups`, so a
    model that answers `reply` forever cannot spam the user."""
    now = time.time()
    v = str(verdict or "").strip().lower()
    if v not in VERDICTS:
        v = "silent"
    with _LOCK:
        data = _load()
        t = data.get(schedule_id)
        if not isinstance(t, dict):
            return {"status": "missing"}
        budget_left = int(t.get("max_follow_ups", 0)) - int(
            t.get("follow_ups", 0))
        if v == "acknowledge":
            t["status"] = "acknowledged"
            t["closed"] = now
        elif v == "reply" and reply_text and budget_left > 0:
            t["follow_ups"] = int(t.get("follow_ups", 0)) + 1
            t["history"].append({"role": "agent",
                                 "text": str(reply_text)[:2000], "ts": now})
            t["last_agent_msg"] = now
        elif v == "reply":
            # Asked to reply but either no text or the budget is spent.
            if budget_left <= 0:
                t["status"] = "budget_exhausted"
            t["history"].append({
                "role": "system", "ts": now,
                "text": "reply suppressed: follow-up budget exhausted"
                if budget_left <= 0 else "reply suppressed: no text"})
        data[schedule_id] = t
        _save(data)
    return t


def due_for_nudge() -> list[dict]:
    """Open threads whose snooze has elapsed and which have budget left."""
    now = time.time()
    with _LOCK:
        data = _load()
    out = []
    for t in data.values():
        if not isinstance(t, dict) or t.get("status") != "open":
            continue
        if now > float(t.get("expires", 0)):
            continue
        if int(t.get("follow_ups", 0)) >= int(t.get("max_follow_ups", 0)):
            continue
        snooze = float(t.get("snooze_s", 0))
        if now - float(t.get("last_agent_msg", 0)) >= snooze:
            out.append(t)
    return out


def list_threads(limit: int = 50) -> list[dict]:
    """Newest first, for the UI."""
    with _LOCK:
        data = _load()
    rows = [t for t in data.values() if isinstance(t, dict)]
    rows.sort(key=lambda t: -float(t.get("opened", 0)))
    return rows[:max(1, int(limit))]


def sweep() -> int:
    """Expire finished threads. Returns how many were closed."""
    now = time.time()
    n = 0
    with _LOCK:
        data = _load()
        for t in data.values():
            if isinstance(t, dict) and t.get("status") == "open" \
                    and now > float(t.get("expires", 0)):
                t["status"] = "expired"
                t["closed"] = now
                n += 1
        if n:
            _save(data)
    return n
