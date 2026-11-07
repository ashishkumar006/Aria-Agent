"""Append-only, hash-chained security audit log.

Security events (refused auth, user-token scope denials, SSRF blocks, budget
denials, user/key administration) are written to a dedicated JSONL file. Each
record includes the previous record's HMAC, so deleting, reordering or editing
a row breaks the chain at the first changed entry.

The key lives in `state/security_audit.key` (0600) or `GATEWAY_AUDIT_KEY`.
This is tamper-EVIDENT against edits made outside the process. It is not a
defence against code already running inside the gateway that can read the key;
process isolation is required for that.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from pathlib import Path

STATE = Path(__file__).resolve().parent / "state"
AUDIT_PATH = STATE / "security_audit.jsonl"
KEY_PATH = STATE / "security_audit.key"
ENV_KEY = "GATEWAY_AUDIT_KEY"

_lock = threading.RLock()
_key: bytes | None = None
_last_hash = ""
_dedupe: dict[tuple[str, str, str], float] = {}


def _get_key() -> bytes:
    global _key
    if _key is not None:
        return _key
    configured = (os.getenv(ENV_KEY) or "").strip()
    raw = configured.encode("utf-8") if configured else b""
    if not raw:
        try:
            raw = KEY_PATH.read_bytes().strip()
        except OSError:
            raw = b""
        if not raw:
            raw = hashlib.sha256(os.urandom(48)).hexdigest().encode("ascii")
            KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
            KEY_PATH.write_bytes(raw)
            try:
                KEY_PATH.chmod(0o600)
            except OSError:
                pass
    _key = hashlib.sha256(raw).digest()
    return _key


def _canonical(record: dict) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _digest(record: dict) -> str:
    return hmac.new(_get_key(), _canonical(record), hashlib.sha256).hexdigest()


def _tail_hash() -> str:
    """The hash of the last line ON DISK, read fresh on every append.

    This used to be a once-only cache: `_load_last()` set `_last_loaded = True`
    and every later record chained from this process's own `_last_hash`. That is
    correct only while exactly one process writes the file, and that assumption
    is false here — the gateway, a test client sharing the real state dir, and a
    second gateway launched by `ensure_gateway()` all append to the same log.
    Each one chained from its own last write, so the chain FORKED.

    Found in the live log (`state/security_audit.jsonl`, 273 records): two breaks,
    both `broken previous-hash link`, at lines 122 and 236, each immediately
    after records written by a different process. `verify()` returned
    `{"ok": false}`. A tamper-evident log with a forked chain is not evidence of
    anything, and invariant 7 is exactly the promise this module exists to keep.

    Reading the tail per append costs one small seek plus one line, and the whole
    append already pays `os.fsync()`, so it is not the expensive part. On any
    read failure this falls back to the in-memory value rather than starting a
    new chain at "" — a log that silently restarts its lineage is the bug again.
    """
    global _last_hash
    try:
        with AUDIT_PATH.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size == 0:
                return _last_hash or ""
            # One record is one line and lines are small; 64 KB reaches the
            # last line of any log this writes.
            back = min(size, 65536)
            fh.seek(size - back)
            tail = fh.read().decode("utf-8", "replace").splitlines()
            for line in reversed(tail):
                if not line.strip():
                    continue
                try:
                    h = json.loads(line).get("hash")
                except ValueError:
                    break
                if isinstance(h, str) and h:
                    _last_hash = h
                    return h
                break
    except OSError:
        pass
    return _last_hash or ""


def _load_last() -> str:
    """Kept for `init()`: warm the in-memory value from the file's tail."""
    return _tail_hash()


def init() -> None:
    _get_key()
    with _lock:
        _load_last()
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not AUDIT_PATH.exists():
            AUDIT_PATH.touch()
        try:
            AUDIT_PATH.chmod(0o600)
        except OSError:
            pass


def record(event: str, *, actor: str = "", user_id: str = "",
           ip: str = "", path: str = "", reason: str = "",
           target: str = "", **detail) -> dict:
    """Append one event. Never pass a secret as a field; detail is redacted."""
    try:
        import redact
        reason = redact.redact(reason)
        target = redact.redact(target)
        if detail:
            detail = {k: redact.redact(v) if isinstance(v, str) else v
                      for k, v in detail.items()}
    except Exception:
        pass
    with _lock:
        # A flood of identical failed-auth probes must not fill the disk or
        # fsync on every request. One row per (event, path, ip) per 5s.
        now = time.time()
        key = (str(event)[:80], str(path)[:200], str(ip)[:80])
        last = _dedupe.get(key, 0.0)
        if event in ("auth_denied", "authz_denied") and now - last < 5.0:
            return {}
        _dedupe[key] = now
        if len(_dedupe) > 2000:
            cutoff = now - 30
            for k in [k for k, t in _dedupe.items() if t < cutoff]:
                _dedupe.pop(k, None)
        prev = _tail_hash()
        rec = {
            "ts": round(time.time(), 3),
            "event": str(event)[:80],
            "actor": str(actor)[:80],
            "user_id": str(user_id)[:80],
            "ip": str(ip)[:80],
            "path": str(path)[:200],
            "reason": str(reason)[:300],
            "target": str(target)[:200],
            "detail": detail,
            "prev": prev,
        }
        rec["hash"] = _digest(rec)
        try:
            with AUDIT_PATH.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            return rec
        global _last_hash
        _last_hash = rec["hash"]
        return rec


def verify() -> dict:
    """Recompute the whole chain. Returns ok, entries, first_bad."""
    entries = 0
    prev = ""
    try:
        with AUDIT_PATH.open("r", encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    return {"ok": False, "entries": entries, "first_bad": i,
                            "error": "unparseable line"}
                body = {k: v for k, v in rec.items() if k != "hash"}
                if body.get("prev") != prev:
                    return {"ok": False, "entries": entries, "first_bad": i,
                            "error": "broken previous-hash link"}
                if not hmac.compare_digest(str(rec.get("hash") or ""),
                                           _digest(body)):
                    return {"ok": False, "entries": entries, "first_bad": i,
                            "error": "record hash mismatch"}
                prev = rec["hash"]
                entries += 1
    except FileNotFoundError:
        return {"ok": True, "entries": 0, "first_bad": None}
    return {"ok": True, "entries": entries, "first_bad": None}


def recent(limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit or 100), 1000))
    try:
        lines = AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            rec.pop("hash", None)
            out.append(rec)
        except ValueError:
            continue
    return out
