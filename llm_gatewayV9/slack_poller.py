"""Poll Slack for inbound chat messages (DMs + channels the bot is in).

Why this exists
---------------
Slack's Events API needs a public HTTPS endpoint, so on a local install the
inbound half of "chat with the agent over Slack" was unreachable — the agent
could post but never received a reply. This mirrors telegram_poller.py: it reads
recent messages from inside the gateway (the only process that holds the token)
and hands each to `channels_api._record_inbound`.

It watches the bot's DM channels (`im`) and any `mpim`/public/private channel it
has been added to, so "DM the bot" just works.

Safety properties
-----------------
* **The bot's own messages are skipped** (by bot_id and by the bot's user id), so
  a reply never re-triggers a turn.
* **Per-channel watermark persisted.** A restart reprocesses at most one message.
* **One bad message/channel never stops the loop.**
* The poller stands idle when Slack is not configured or the token is rejected.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_WM_PATH = (Path(__file__).resolve().parent / "state" / "slack_poll_wm.json")

_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_STARTED_AT = 0.0
_LAST_ERROR = ""

# Conversation types worth reading: DMs, group DMs, and the channels the bot is
# in. `im` is what makes "DM the bot" work with no configuration.
_TYPES = "public_channel,private_channel,im,mpim"


def _polling_enabled() -> bool:
    return os.getenv("SLACK_POLLING", "on").strip().lower() not in (
        "off", "0", "false", "no")


def _token() -> str:
    return (os.getenv("SLACK_BOT_TOKEN") or "").strip()


def _wm() -> dict:
    try:
        return json.loads(_WM_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_wm(data: dict) -> None:
    try:
        _WM_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _WM_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(_WM_PATH)
    except OSError:
        pass


def _api(method: str, **params) -> dict:
    """One Slack Web API call. Returns the parsed JSON (or {"ok": False})."""
    import httpx
    r = httpx.post(f"https://slack.com/api/{method}",
                   headers={"Authorization": f"Bearer {_token()}"},
                   json=params or {}, timeout=20.0)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "error": f"HTTP {r.status_code}"}


def _bot_user_id() -> str:
    try:
        return str((_api("auth.test").get("user_id")) or "")
    except Exception:
        return ""


def _channels(inst) -> list[str]:
    try:
        data = _api("conversations.list", types=_TYPES, limit=200)
        if not data.get("ok"):
            return []
        return [str(c.get("id")) for c in (data.get("channels") or [])
                if c.get("id")]
    except Exception:
        return []


def poll_once(inst=None) -> dict:
    """One polling cycle. Returns a report dict; never raises."""
    global _LAST_ERROR
    report = {"delivered": 0, "failed": 0, "channels": 0, "error": ""}
    if not _token():
        report["error"] = "SLACK_BOT_TOKEN not set"
        return report

    # Fail loud on a rejected token: the bot is configured but cannot read, and
    # a silent no-op is indistinguishable from "nobody has messaged".
    auth = _api("auth.test")
    if not auth.get("ok"):
        report["error"] = str(auth.get("error") or "auth.test failed")
        return report

    if inst is None:
        try:
            from adaptors import registry
            inst = registry.get("slack")
        except Exception:
            inst = None
    if inst is None:
        report["error"] = "slack adaptor unavailable"
        return report

    me = str(auth.get("user_id") or "")
    wm = _wm()
    try:
        chans = _channels(inst)
    except Exception as e:
        report["error"] = f"{type(e).__name__}: {e}"[:200]
        return report
    report["channels"] = len(chans)

    from channels_api import _record_inbound
    for ch in chans:
        try:
            hist = _api("conversations.history", channel=ch, limit=50)
            if not hist.get("ok"):
                continue
            last = float(wm.get(ch) or 0)
            newest = last
            # Slack returns newest-first; walk oldest→newest for stable order.
            for m in reversed(hist.get("messages") or []):
                try:
                    ts = float(m.get("ts") or 0)
                except (TypeError, ValueError):
                    continue
                if ts <= last:
                    continue
                newest = max(newest, ts)
                # Skip the bot's own messages.
                if m.get("bot_id") or (me and m.get("user") == me):
                    continue
                if not (m.get("text") or "").strip():
                    continue
                payload = {"event": {
                    "type": "message", "user": m.get("user", ""),
                    "channel": ch, "text": m.get("text", ""),
                    "ts": m.get("ts", ""), "thread_ts": m.get("thread_ts", "")}}
                msg = inst.normalize(payload)
                _record_inbound("slack", msg)
                report["delivered"] += 1
            if newest > last:
                wm[ch] = newest
        except Exception as e:
            report["failed"] += 1
            _LAST_ERROR = f"{type(e).__name__}: {e}"[:200]
            continue
    _save_wm(wm)
    if report["delivered"] or not report["failed"]:
        _LAST_ERROR = ""
    return report


def _worker(interval: float = 10.0, app=None) -> None:
    fail_streak = 0
    while not _STOP.is_set():
        try:
            from adaptors import registry
            inst = registry.get("slack")
            if inst is None or not inst.is_configured():
                fail_streak = 0
                _STOP.wait(timeout=30.0)
                continue
            report = poll_once(inst)
            # A rejected token (invalid_auth) is not a loop-the-loop error.
            if "invalid_auth" in str(report.get("error") or "").lower():
                print("[slack] inbound idle: token rejected (invalid_auth) — "
                      "paste a valid xoxb- Bot User OAuth Token")
                _STOP.wait(timeout=300.0)
                continue
            fail_streak = fail_streak + 1 if report.get("error") else 0
        except Exception as e:  # pragma: no cover - the loop must not die
            fail_streak += 1
            _LAST_ERROR = f"{type(e).__name__}: {e}"[:200]
        _STOP.wait(timeout=(min(interval * (2 ** min(fail_streak, 4)), 120.0)
                            if fail_streak else interval))


def start(app=None) -> threading.Thread | None:
    global _THREAD, _STARTED_AT
    if _THREAD is not None and _THREAD.is_alive():
        return _THREAD
    if not _polling_enabled():
        print("[slack] polling disabled by SLACK_POLLING=off")
        return None
    _STOP.clear()
    _STARTED_AT = time.time()
    _THREAD = threading.Thread(target=_worker, kwargs={"app": app},
                               daemon=True, name="slack-poll")
    _THREAD.start()
    return _THREAD


def stop() -> None:
    _STOP.set()


def status() -> dict:
    alive = bool(_THREAD and _THREAD.is_alive())
    return {"running": alive, "enabled": _polling_enabled(),
            "last_error": _LAST_ERROR,
            "uptime_s": (time.time() - _STARTED_AT) if alive else 0.0}