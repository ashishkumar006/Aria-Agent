"""Gateway-side approval store for policy `approve` verdicts.

When the policy engine (armed, i.e. not dry-run) returns `approve` for a
send/integration call, the call is NOT executed. Instead a pending approval
is recorded here and the caller gets {ok:false, status:"pending",
approval_id}. A human resolves it via POST /v1/approvals/{id}; the caller
retries afterwards. File-backed (state/approvals.json), thread-safe.
"""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

from atomic_json import load_json, save_json

STORE_PATH = Path(__file__).resolve().parent / "state" / "approvals.json"
_LOCK = threading.Lock()


def _load() -> list[dict[str, Any]]:
    data = load_json(STORE_PATH, [])
    return data if isinstance(data, list) else []


def _save(items: list[dict[str, Any]]) -> None:
    save_json(STORE_PATH, items[-500:])


def create(*, tool: str, channel: str | None, args: dict[str, Any],
           agent: str | None, session: str | None,
           rule_id: str | None) -> dict[str, Any]:
    entry = {
        "id": f"gw-{uuid.uuid4().hex[:8]}",
        "ts": time.time(),
        "tool": tool,
        "channel": channel,
        "args": args,
        "agent": agent,
        "session": session,
        "rule_id": rule_id,
        "status": "pending",
    }
    with _LOCK:
        items = _load()
        items.append(entry)
        _save(items)
    return entry


def list_pending() -> list[dict[str, Any]]:
    with _LOCK:
        return [e for e in _load() if e.get("status") == "pending"]


def resolve(approval_id: str, approve: bool) -> dict[str, Any] | None:
    with _LOCK:
        items = _load()
        for e in items:
            if e.get("id") == approval_id:
                if e.get("status") != "pending":
                    return dict(e)
                e["status"] = "approved" if approve else "rejected"
                e["resolved_ts"] = time.time()
                _save(items)
                return dict(e)
    return None
