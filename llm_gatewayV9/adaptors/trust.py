"""Trust store + classification (glc pattern: pairing outside the LLM).

pairing.json maps channel-specific sender IDs to a trust role:
  {"telegram": {"12345": "owner"}, "discord": {...}, ...}
Anything not listed classifies as `untrusted`. The policy engine defaults
to deny-all-tools for untrusted. Owner pairing happens via the control
plane (/v1/control/pair) — never via chat.
"""
from __future__ import annotations

import threading
from pathlib import Path

from atomic_json import load_json, save_json

from .envelope import TrustLevel

STORE_PATH = Path(__file__).resolve().parent.parent / "state" / "pairing.json"
_LOCK = threading.Lock()


def _load() -> dict:
    data = load_json(STORE_PATH, {})
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    save_json(STORE_PATH, data)


def classify(channel: str, sender_id: str) -> TrustLevel:
    """sender_id "" (unknown) is always untrusted."""
    if not sender_id:
        return TrustLevel.untrusted
    with _LOCK:
        role = (_load().get(channel) or {}).get(str(sender_id), "")
    if role == "owner":
        return TrustLevel.owner
    if role in ("paired", "user"):
        return TrustLevel.paired
    return TrustLevel.untrusted


def pair(channel: str, sender_id: str, role: str = "paired") -> dict:
    """Add/overwrite a pairing. role in {owner, paired}. Returns the entry."""
    if role not in ("owner", "paired"):
        raise ValueError("role must be 'owner' or 'paired'")
    if not sender_id:
        raise ValueError("sender_id is required")
    with _LOCK:
        data = _load()
        data.setdefault(channel, {})[str(sender_id)] = role
        _save(data)
    return {"channel": channel, "sender_id": str(sender_id), "role": role}


def unpair(channel: str, sender_id: str) -> bool:
    with _LOCK:
        data = _load()
        if str(sender_id) in (data.get(channel) or {}):
            del data[channel][str(sender_id)]
            _save(data)
            return True
    return False


def list_paired(channel: str | None = None) -> dict:
    """Return pairings with sender IDs masked (last 4) for safe display."""
    with _LOCK:
        data = _load()
    if channel:
        data = {channel: data.get(channel, {})}
    out: dict[str, dict[str, str]] = {}
    for ch, entries in data.items():
        out[ch] = {("…" + sid[-4:] if len(sid) > 4 else sid): role
                   for sid, role in (entries or {}).items()}
    return out
