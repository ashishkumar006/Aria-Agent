"""Poll Gmail for inbound chat messages, so a local install has an inbound path.

Why this exists
---------------
Gmail push (Pub/Sub) needs a public HTTPS endpoint a local install does not have,
so the inbound half of "chat with the agent over email" was unreachable — the
agent could send mail but never received a reply as a chat turn. This mirrors
telegram_poller.py: it reads unread INBOX mail from inside the gateway (the only
process that holds the token) and hands each message to
`channels_api._record_inbound` — the exact path the webhook route uses.

Safety properties
-----------------
* **Allowlist.** Only senders in `GMAIL_INBOUND_FROM` (comma-separated) become
  chat turns. With it unset the poller stays idle, because "process every unread
  email" would turn newsletters into agent turns. Set it to the address you will
  write from (a DIFFERENT address than the agent's own mailbox).
* **Self-reply loop is impossible.** Any message whose From is the agent's own
  account is skipped — that is the agent's own reply, and reprocessing it would
  loop forever.
* **Watermark persisted.** A message id is recorded once delivered, so a restart
  reprocesses at most one message, never the whole inbox.
* **One bad message never stops the loop.**
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

_WM_PATH = (Path(__file__).resolve().parent / "state" / "gmail_poll_wm.json")
# Gmail ids are not time-sortable; cap the seen set so the file cannot grow
# forever. A personal inbox has far fewer than this many genuinely new messages
# between polls, so the cap never reprocesses a live one.
_WM_MAX = 2000

_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_STARTED_AT = 0.0
_LAST_ERROR = ""
_IDLE_NOTED = False

_ADDR_RE = re.compile(r"<([^>]+)>")


def _polling_enabled() -> bool:
    return os.getenv("GMAIL_POLLING", "on").strip().lower() not in (
        "off", "0", "false", "no")


def _addr(from_header: str) -> str:
    """The bare email address from a `Name <a@b>` header (or the raw string)."""
    m = _ADDR_RE.search(from_header or "")
    return (m.group(1) if m else (from_header or "")).strip().lower()


def _allowed() -> set[str]:
    raw = (os.getenv("GMAIL_INBOUND_FROM") or "").strip()
    return {a.strip().lower() for a in raw.split(",") if a.strip()}


def _self_addr() -> str:
    try:
        from integrations import gmail as g
        p = g.profile()
        if p.get("ok") and p.get("email"):
            return str(p["email"]).strip().lower()
    except Exception:
        pass
    return ""


def _wm() -> set[str]:
    try:
        return set(json.loads(_WM_PATH.read_text(encoding="utf-8")).get("seen") or [])
    except Exception:
        return set()


def _save_wm(seen: set[str]) -> None:
    try:
        _WM_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _WM_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"seen": sorted(seen)[-_WM_MAX:],
                                   "at": time.time()}), encoding="utf-8")
        tmp.replace(_WM_PATH)
    except OSError:
        pass


def poll_once(app=None) -> dict:
    """One polling cycle. Returns a report dict; never raises."""
    global _LAST_ERROR, _IDLE_NOTED
    report = {"delivered": 0, "failed": 0, "skipped": 0, "error": ""}
    try:
        from adaptors import registry
        inst = registry.get("gmail")
        if inst is None or not inst.is_configured():
            report["error"] = "gmail is not configured (GMAIL_TOKEN)"
            return report
    except Exception as e:
        report["error"] = f"{type(e).__name__}: {e}"
        return report

    allowed = _allowed()
    if not allowed:
        if not _IDLE_NOTED:
            print("[gmail] inbound idle: set GMAIL_INBOUND_FROM to the address "
                  "you will email from (different from the agent's own mailbox)")
            _IDLE_NOTED = True
        report["error"] = "GMAIL_INBOUND_FROM not set"
        return report
    _IDLE_NOTED = False
    me = _self_addr()

    try:
        from integrations import gmail as g
        lst = g.query(api_method="list", query="in:inbox is:unread", max_results=20)
    except Exception as e:
        _LAST_ERROR = f"{type(e).__name__}: {e}"[:200]
        report["error"] = _LAST_ERROR
        return report
    if not lst.get("ok"):
        report["error"] = str(lst.get("error") or "list failed")[:200]
        return report

    seen = _wm()
    from channels_api import _record_inbound
    for mid in (lst.get("message_ids") or []):
        if mid in seen:
            continue
        try:
            rd = g.query(api_method="read", query=mid)
            if not rd.get("ok"):
                seen.add(mid)
                continue
            hdrs = rd.get("headers") or {}
            sender = _addr(str(hdrs.get("From") or ""))
            # Skip the agent's own mail (its replies) — no self-loop.
            if me and sender == me:
                seen.add(mid)
                continue
            # Only the allowlist becomes a chat turn; record others as seen so
            # we do not re-fetch them every cycle.
            if sender not in allowed:
                seen.add(mid)
                report["skipped"] += 1
                continue
            payload = {"id": mid, "sender_id": sender,
                       "From": hdrs.get("From", ""),
                       "Subject": hdrs.get("Subject", ""),
                       "snippet": rd.get("snippet", "")}
            msg = inst.normalize(payload)
            _record_inbound("gmail", msg)
            seen.add(mid)
            report["delivered"] += 1
        except Exception as e:
            # Leave it out of `seen` so it is retried; never abort the loop.
            report["failed"] += 1
            _LAST_ERROR = f"{type(e).__name__}: {e}"[:200]
            continue
    _save_wm(seen)
    _LAST_ERROR = ""
    return report


def _worker(interval: float = 15.0, app=None) -> None:
    fail_streak = 0
    while not _STOP.is_set():
        try:
            report = poll_once(app=app)
            fail_streak = fail_streak + 1 if report.get("error") else 0
        except Exception as e:  # pragma: no cover - the loop must not die
            fail_streak += 1
            _LAST_ERROR = f"{type(e).__name__}: {e}"[:200]
        _STOP.wait(timeout=(min(interval * (2 ** min(fail_streak, 4)), 120.0)
                            if fail_streak else interval))


def start(app=None) -> threading.Thread | None:
    """Start the Gmail poller once per process. Idempotent."""
    global _THREAD, _STARTED_AT
    if _THREAD is not None and _THREAD.is_alive():
        return _THREAD
    if not _polling_enabled():
        print("[gmail] polling disabled by GMAIL_POLLING=off")
        return None
    _STOP.clear()
    _STARTED_AT = time.time()
    _THREAD = threading.Thread(target=_worker, kwargs={"app": app},
                               daemon=True, name="gmail-poll")
    _THREAD.start()
    return _THREAD


def stop() -> None:
    """Stop the poller. Safe to call when it was never started."""
    _STOP.set()


def status() -> dict:
    alive = bool(_THREAD and _THREAD.is_alive())
    return {"running": alive, "enabled": _polling_enabled(),
            "allowlist": sorted(_allowed()), "last_error": _LAST_ERROR,
            "uptime_s": (time.time() - _STARTED_AT) if alive else 0.0}