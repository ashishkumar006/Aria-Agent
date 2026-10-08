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


def is_paired(channel: str, sender_id: str) -> bool:
    """Is this sender already known on this channel?"""
    with _LOCK:
        return str(sender_id) in (_load().get(channel) or {})


def has_pairings(channel: str) -> bool:
    """Is anyone paired on this channel yet?

    Trust-on-first-contact uses this to decide whether an inbound sender may
    become the `owner`, so it needs the answer without the ids.
    """
    with _LOCK:
        return bool((_load().get(channel) or {}))


def resolve_notify_target(channel: str = "telegram",
                          prefer: tuple[str, ...] = ("owner", "paired")
                          ) -> str | None:
    """The UNMASKED sender id a local notification should be delivered to.

    `list_paired` deliberately masks ids (last 4 only) because it feeds a
    display panel. A notification needs the real id, and there was no way to
    get one: the pairing store was only ever written by a loopback-only
    control call and read back masked, so a scheduled reminder had nowhere to
    go even with a bot token configured.

    Preference order is explicit, then newest. Returns "" for nothing, so
    callers can treat "" and None the same.
    """
    with _LOCK:
        data = _load()
    entries = data.get(channel) or {}
    for role in prefer:
        for sid, r in entries.items():
            if str(r or "").lower() == role:
                return str(sid)
    # No owner/paired role recorded: fall back to anything at all, so a chat
    # that paired under a different role is still reachable.
    for sid in entries:
        return str(sid)
    return ""


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
