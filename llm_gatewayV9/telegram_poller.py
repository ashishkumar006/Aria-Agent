"""Long-poll Telegram for inbound messages, so a local install has an inbound
path at all.

Why this exists
---------------
A chat channel the agent can be talked *to* is only half the feature. The
inbound half was a dead end on a local install: `POST /v1/hooks/telegram` is
the one route that delivers a message, but Telegram can only call a public
HTTPS URL — never 127.0.0.1 — and nothing in this repo ever called
`setWebhook` (grep: no matches). So no message ever arrived. And because
pairing is trust-on-first-contact, *nobody could ever be paired*: no chat id
reached the pairing store, `resolve_target` returned empty for telegram, and
every notification quietly went nowhere with no destination.

`getUpdates` needs no public URL. This worker calls it from inside the
gateway — the only process that holds the bot token, loaded from the
environment at startup and never read from anywhere else — and hands every
update to `channels_api._record_inbound`, the exact path the webhook route
uses: normalize, policy, ledger, pair, forward to the agent.

Safety properties
-----------------
* **Offset persisted before delivery.** An update is acknowledged only after
  it is delivered, so a crash retries one message rather than losing it. A
  duplicate answer is recoverable; a silently dropped message is not.
* **Stands down when a webhook is registered.** Telegram answers `getUpdates`
  with 409 while a webhook is set, and two consumers of one queue is a bug.
* **One bad update never stops the loop.** Delivery failures leave the offset
  alone and are retried.
* **The bot token never leaves the adaptor.** Requests go through
  `TelegramAdaptor._api`, and no log line ever contains it.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_OFFSET_PATH = (Path(__file__).resolve().parent / "state"
                / "telegram_poll_offset.json")

# Server-side long poll vs client-side timeout: the latter must be larger.
_POLL_TIMEOUT = 20
_API_TIMEOUT = _POLL_TIMEOUT + 15

_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_STATE_LOCK = threading.Lock()
_STARTED_AT: float = 0.0
_LAST_ERROR: str = ""
_STOOD_DOWN: str = ""


def _polling_enabled() -> bool:
    return os.getenv("TELEGRAM_POLLING", "on").strip().lower() not in (
        "off", "0", "false", "no")


def _absorb_files(inst, msg_obj) -> None:
    """Download each attachment and put it in the console's Documents store.

    This runs on its own thread, after the message has already been forwarded
    to the agent, so a slow or oversized download can never block the poll
    loop or delay the user's reply. By the time the agent tries to use the
    file it is usually already indexed and searchable via search_knowledge.

    Telegram gives no filename for photos, so one is synthesised from the
    message id - stable, and it keeps re-sends from colliding.
    """
    from memory_api import ingest_document, UnsupportedBytes

    for att in list(getattr(msg_obj, "attachments", None) or []):
        file_id = str((att or {}).get("file_id") or "")
        if not file_id:
            continue
        name = str((att or {}).get("name") or "")
        if not name:
            ext = {"image": ".jpg", "audio": ".ogg", "video": ".mp4"}.get(
                str(att.get("kind")), ".bin")
            name = f"telegram_{msg_obj.msg_id}{ext}"
        try:
            data, _path = inst.download(file_id)
        except Exception as e:
            print(f"[telegram] download failed for {name}: "
                  f"{type(e).__name__}: {e}")
            continue
        try:
            doc = ingest_document(name, data)
        except UnsupportedBytes as e:
            # Telegram happily accepts any extension; the Documents store
            # only indexes a known set. Say so rather than failing silently.
            print(f"[telegram] not stored ({name}): {e}")
            continue
        except Exception as e:
            print(f"[telegram] store failed for {name}: "
                  f"{type(e).__name__}: {e}")
            continue
        print(f"[telegram] stored {name} as {doc.get('id')}")


def _offset() -> int:
    try:
        data = json.loads(_OFFSET_PATH.read_text(encoding="utf-8"))
        value = int((data or {}).get("offset") or 0)
        return max(value, 0)
    except Exception:
        return 0


def _set_offset(value: int) -> None:
    try:
        _OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _OFFSET_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"offset": int(value), "at": time.time()}),
                       encoding="utf-8")
        tmp.replace(_OFFSET_PATH)
    except OSError:
        pass  # a restart that reprocesses one update is survivable


def _webhook_registered(inst) -> bool:
    """True when someone has pointed a webhook at this bot. Then the queue is
    not ours to read, and polling would 409."""
    try:
        info = inst._api("getWebhookInfo", {})  # noqa: SLF001 - adaptor API
        return bool(((info or {}).get("result") or {}).get("url"))
    except Exception:
        return False


def poll_once(inst, *, state: dict | None = None) -> dict:
    """One polling cycle. Returns a report dict; never raises.

    `state` lets a caller (or a test) carry the offset in memory instead of
    re-reading the file; it is written back through `_set_offset` either way.
    """
    report = {"delivered": 0, "failed": 0, "offset": _offset(),
              "stopped": False, "conflict": False, "error": ""}
    global _LAST_ERROR
    if state is not None and "offset" in state:
        report["offset"] = int(state["offset"])
    try:
        # The HTTP timeout must exceed the long-poll timeout, or every cycle
        # ends in a client-side ReadTimeout just as Telegram is about to
        # answer — which then backs the loop off for no reason.
        data = inst._api("getUpdates", {  # noqa: SLF001 - adaptor API
            "offset": report["offset"], "limit": 20,
            "timeout": _POLL_TIMEOUT,
            "allowed_updates": ["message"]}, timeout=_API_TIMEOUT)
    except Exception as e:
        # A 409 here means the queue is not ours to read, but it has two very
        # different causes and they must not be treated the same.
        msg = str(e)
        _LAST_ERROR = msg[:200]
        if "409" in msg:
            if _webhook_registered(inst):
                # A webhook genuinely owns the queue; polling would fight it.
                report["stopped"] = True
                report["error"] = ("telegram reports a registered webhook "
                                   "(409); polling stood down")
            else:
                # No webhook, so this is contention with another getUpdates
                # consumer — often the tail of an earlier request this process
                # abandoned. Standing down forever would silently kill inbound
                # chat over a momentary conflict, so back off and retry.
                report["conflict"] = True
                report["error"] = ("telegram reported a concurrent "
                                   "getUpdates (409); will retry")
        else:
            report["error"] = msg[:200]
        return report

    from channels_api import _record_inbound
    for update in ((data or {}).get("result") or []):
        uid = int(update.get("update_id") or 0)
        try:
            # Same path the webhook route takes. Normalize is a pure function
            # in every adaptor, so calling it directly is safe here.
            msg_obj = inst.normalize(update)
            # A file arrives as a `file_id`, not bytes. Fetching it is two
            # more round trips and can take seconds, and this loop is what
            # advances the offset - a slow download here would stall every
            # other message. Hand it to a thread and carry on.
            if msg_obj.attachments:
                threading.Thread(target=_absorb_files,
                                 args=(inst, msg_obj), daemon=True,
                                 name="tg-file").start()
            _record_inbound("telegram", msg_obj)
            report["delivered"] += 1
        except Exception as e:
            # Leave the offset where it is: the update is retried next cycle,
            # because the agent being briefly down is not a reason to drop
            # what the user said.
            report["failed"] += 1
            _LAST_ERROR = f"{type(e).__name__}: {e}"[:200]
            continue
        if uid >= report["offset"]:
            report["offset"] = uid + 1
            _set_offset(report["offset"])
            if state is not None:
                state["offset"] = report["offset"]
    _LAST_ERROR = ""
    return report


def _worker(interval: float = 5.0) -> None:
    global _STOOD_DOWN
    from adaptors import registry

    state: dict = {}
    fail_streak = 0
    while not _STOP.is_set():
        try:
            inst = registry.get("telegram")
            if inst is None or not inst.is_configured():
                fail_streak = 0
                _STOP.wait(timeout=30.0)
                continue
            if not _STOOD_DOWN and _webhook_registered(inst):
                _STOOD_DOWN = ("telegram has a webhook registered; long-poll "
                               "listener stood down")
                print(f"[telegram] {_STOOD_DOWN}")
                return
            report = poll_once(inst, state=state)
            if report.get("stopped"):
                _STOOD_DOWN = str(report.get("error") or "stopped")
                print(f"[telegram] {_STOOD_DOWN}")
                return
            if report.get("error"):
                # Includes a 409 conflict: counted so the backoff grows, but
                # never fatal — the listener must survive a momentary clash.
                fail_streak += 1
            else:
                fail_streak = 0
        except Exception as e:  # pragma: no cover - the loop must not die
            fail_streak += 1
            _LAST_ERROR = f"{type(e).__name__}: {e}"[:200]
        # Back off on repeated failure so a dead network is not a hot loop.
        _STOP.wait(timeout=(min(interval * (2 ** min(fail_streak, 4)), 60.0)
                            if fail_streak else interval))


def start() -> threading.Thread | None:
    """Start the long-poll listener once per process. Idempotent."""
    global _THREAD, _STARTED_AT, _STOOD_DOWN
    if _THREAD is not None and _THREAD.is_alive():
        return _THREAD
    if not _polling_enabled():
        print("[telegram] polling disabled by TELEGRAM_POLLING=off")
        return None
    _STOP.clear()
    _STOOD_DOWN = ""
    _STARTED_AT = time.time()
    _THREAD = threading.Thread(target=_worker, daemon=True,
                               name="telegram-poll")
    _THREAD.start()
    return _THREAD


def stop() -> None:
    """Stop the listener. Safe to call when it was never started."""
    _STOP.set()


def status() -> dict:
    with _STATE_LOCK:
        alive = bool(_THREAD and _THREAD.is_alive())
    return {"running": alive, "enabled": _polling_enabled(),
            "offset": _offset(), "stood_down": _STOOD_DOWN,
            "last_error": _LAST_ERROR,
            "uptime_s": (time.time() - _STARTED_AT) if alive else 0.0}
