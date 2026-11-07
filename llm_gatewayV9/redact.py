"""Redaction for secrets in logs, ledger rows and client-visible errors.

Provider errors can embed the request URL. Gemini puts the API key in that
URL's query string, so a raw `httpx.HTTPStatusError` logged by the call ledger
or returned to the agent could disclose the key. This module replaces known
secret values and the common credential shapes with `[redacted]`.

The registry is filled in-process at start-up from the provider-key vault and
the gateway token. Regexes catch credentials from processes that never
registered anything (the agent side of the gateway client).
"""
from __future__ import annotations

import re
import threading

REDACTED = "[redacted]"
_lock = threading.Lock()
_secrets: set[str] = set()

_PATTERNS = (
    re.compile(r"(?i)([?&](?:key|api[_-]?key|apikey|token|access[_-]?token|"
               r"secret|password)=)[^&\s\"']+"),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(x-aria-user-token\s*[:=]\s*)[A-Za-z0-9._\-]+"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\baru_[A-Za-z0-9_\-]{20,}\b"),
)


def register_secret(value: str) -> None:
    s = str(value or "").strip()
    if len(s) < 8 or s.lower().startswith(("mock-", "not-real")):
        return
    with _lock:
        _secrets.add(s)


def register_many(values) -> None:
    for v in values or ():
        register_secret(v)


def redact(text) -> str:
    """Return `text` with known secrets and credential-shaped tokens removed."""
    s = "" if text is None else str(text)
    if not s:
        return s
    with _lock:
        known = sorted(_secrets, key=len, reverse=True)
    for secret in known:
        if secret and secret in s:
            s = s.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        s = pattern.sub(lambda m: m.group(1) + REDACTED
                        if m.groups() else REDACTED, s)
    return s


def has_secret_shape(text: str) -> bool:
    s = str(text or "")
    return any(p.search(s) for p in _PATTERNS)
