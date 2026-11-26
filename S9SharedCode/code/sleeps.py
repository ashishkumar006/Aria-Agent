"""Self-scheduled continuations — the main agent forwards a goal to its future self.

The DAG orchestrator (the "main agent" in chat) can set itself a task and end the
turn, then be woken later by a TIMER or an EVENT (an email arriving from someone).
A "sleep" is a durable record of three things:

  * goal     — what the future self must do
  * context  — what was already done, so the future planner does not redo it
  * trigger  — a time ("in 1h", "9am") OR an email to watch for

When the trigger fires, the record becomes an ordinary scheduled query whose text
is ``goal + context``. The freshly-spun planner therefore receives BOTH the goal
and a summary of what came before, exactly as if the agent had briefed its future
self. The conversation's own memory and turn log carry the rest.

Nothing here calls a model. The scheduler owns execution (see scheduler._fire);
this module owns the *records* and the *event poll*.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

_LOCK = threading.RLock()


def _state_dir() -> Path:
    root = os.environ.get("S9_STATE_DIR")
    base = Path(root) if root else (Path(__file__).resolve().parent / "state")
    d = base / "sleeps"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file() -> Path:
    return _state_dir() / "sleeps.json"


def _load() -> dict:
    try:
        got = json.loads(_file().read_text(encoding="utf-8-sig"))
        return got if isinstance(got, dict) else {"sleeps": []}
    except (OSError, ValueError):
        return {"sleeps": []}


def _save(doc: dict) -> None:
    p = _file()
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def compose(goal: str, context: str) -> str:
    """The query the future planner receives: the goal, then what came before."""
    goal = (goal or "").strip()
    context = (context or "").strip()
    if not context:
        return goal
    return (f"{goal}\n\nCONTEXT FROM BEFORE — what you already did this session, "
            f"so you do not redo it:\n{context}")


def arm(*, goal: str, conversation_id: str = "", session_id: str = "",
        when: str = "", email_from: str = "", context: str = "",
        interim: str = "", every_s: int = 120) -> dict:
    """Arm a continuation. Give a `when` (time) OR an `email_from` (event).

    `when` wins if both are set. Returns the stored record (never raises on a
    bad `when`; the error lands on the record so the caller can report it).
    """
    rec = {
        "id": f"sl-{uuid.uuid4().hex[:8]}",
        "conversation_id": str(conversation_id or ""),
        "session_id": str(session_id or ""),
        "goal": (goal or "").strip()[:2000],
        "context": (context or "").strip()[:4000],
        "interim": (interim or "").strip()[:500],
        "email_from": (email_from or "").strip()[:200],
        "every_s": max(30, int(every_s or 120)),
        "trigger": "",
        "status": "armed",
        "schedule_id": "",
        "last_poll": 0.0,
        "created": time.time(),
    }
    if when:
        try:
            import scheduler
            rec["schedule_id"] = scheduler.schedule(
                compose(rec["goal"], rec["context"]), when,
                conversation_id=(rec["conversation_id"] or None), notify=True)
            rec["trigger"] = "time"
        except Exception as e:  # noqa: BLE001 — a bad `when` must not crash arming
            rec["status"] = "error"
            rec["error"] = f"{type(e).__name__}: {e}"[:300]
    else:
        rec["trigger"] = "email"
    with _LOCK:
        doc = _load()
        doc["sleeps"] = [r for r in (doc.get("sleeps") or [])
                         if r.get("id") != rec["id"]]
        doc["sleeps"].append(rec)
        doc["updated"] = time.time()
        _save(doc)
    return rec


def list_sleeps() -> list[dict]:
    with _LOCK:
        return [dict(r) for r in (_load().get("sleeps") or [])]


def get(sleep_id: str) -> dict | None:
    with _LOCK:
        for r in (_load().get("sleeps") or []):
            if r.get("id") == sleep_id:
                return dict(r)
    return None


def cancel(sleep_id: str) -> bool:
    with _LOCK:
        doc = _load()
        rows = doc.get("sleeps") or []
        hit = next((r for r in rows if r.get("id") == sleep_id), None)
        if not hit:
            return False
        if hit.get("schedule_id"):
            try:
                import scheduler
                scheduler.cancel(hit["schedule_id"])
            except Exception:  # noqa: BLE001
                pass
        doc["sleeps"] = [r for r in rows if r.get("id") != sleep_id]
        doc["updated"] = time.time()
        _save(doc)
        return True


def _mark(sleep_id: str, **fields) -> None:
    with _LOCK:
        doc = _load()
        for r in (doc.get("sleeps") or []):
            if r.get("id") == sleep_id:
                r.update(fields)
        doc["updated"] = time.time()
        _save(doc)


def fire_now(rec: dict) -> str:
    """Turn an armed record into a scheduled continuation ~5s from now."""
    import scheduler
    sched_id = scheduler.schedule(
        compose(rec.get("goal", ""), rec.get("context", "")), "in 5s",
        conversation_id=(rec.get("conversation_id") or None), notify=True)
    _mark(rec["id"], status="fired", schedule_id=sched_id, fired=time.time())
    return sched_id


# ── email trigger ──────────────────────────────────────────────────────────
def _gmail_has_new_from(address: str, since: float) -> bool:
    """Read-only: did a message from `address` arrive after `since`?

    Calls the gateway's Gmail list (no model, no side effect). Returns False on
    any failure — a poll that cannot reach Gmail must not fire a false wake.
    """
    try:
        import httpx
        tok = ""
        gp = (Path(__file__).resolve().parents[2]
              / "llm_gatewayV9" / "state" / "gateway.token")
        try:
            tok = gp.read_text(encoding="utf-8").strip()
        except OSError:
            tok = ""
        if not tok:
            return False
        gw = os.environ.get("LLM_GATEWAY_V9_URL", "http://127.0.0.1:8109").rstrip("/")
        q = f"from:{address} after:{int(since)}"
        with httpx.Client(timeout=20) as c:
            r = c.post(f"{gw}/v1/integrations/gmail/query",
                       headers={"X-Gateway-Token": tok},
                       json={"args": {"api_method": "list", "query": q}})
            if r.status_code != 200:
                return False
            data = r.json()
            return bool(data.get("ok")) and bool(data.get("message_ids"))
    except Exception:  # noqa: BLE001
        return False


def poll_due() -> int:
    """Fire any email-triggered sleeps whose address has new mail.

    Called from the scheduler worker each tick; per-record `every_s` throttles the
    actual Gmail call. Returns how many were fired.
    """
    now = time.time()
    fired = 0
    with _LOCK:
        recs = [dict(r) for r in (_load().get("sleeps") or [])
                if r.get("status") == "armed" and r.get("trigger") == "email"
                and r.get("email_from")]
    for rec in recs:
        if now - float(rec.get("last_poll") or 0) < int(rec.get("every_s") or 120):
            continue
        _mark(rec["id"], last_poll=now)
        try:
            if _gmail_has_new_from(rec["email_from"], float(rec.get("created") or now)):
                fire_now(rec)
                fired += 1
        except Exception:  # noqa: BLE001
            pass
    return fired