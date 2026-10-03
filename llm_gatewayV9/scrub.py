"""Secret-shape scrubbing for errors and logs (single copy).

Bot tokens in URLs, key=value pairs, and bearer tokens must never reach
the ledger DB, the dashboard, or API responses. Both the channel plane
and the integrations plane scrub through here so the patterns can't
drift apart.
"""
from __future__ import annotations

import re


def scrub(text: str | None) -> str:
    """Strip secret shapes from error/log strings (defense-in-depth)."""
    s = str(text or "")
    # Telegram-style bot tokens embedded in URLs.
    s = re.sub(r"\d{8,10}:[A-Za-z0-9_-]{30,}", "***REDACTED***", s)
    # key=value / key: value secret pairs.
    s = re.sub(r"(?i)\b(token|api_key|apikey|secret|password|passwd|pwd|"
               r"client_secret|access_token|refresh_token|authorization|auth)"
               r"['\"]?\s*[:=]\s*['\"]?([^\s,'\"]+)",
               lambda m: m.group(0)[:m.start(3) - m.start(0)] + "***REDACTED***", s)
    # Bearer tokens.
    s = re.sub(r"(?i)\bearer\s+[A-Za-z0-9_\-\.~+/=]+", "Bearer ***REDACTED***", s)
    # Bare well-known provider key shapes (kept in sync with the agent's
    # computer_use redactor).
    for pat in (r"sk-[A-Za-z0-9_\-]{8,}", r"sk-or-[A-Za-z0-9_\-]+",
                r"AIza[0-9A-Za-z_\-]{10,}", r"xox[bpaser]-[A-Za-z0-9\-]+",
                r"gh[op]_[A-Za-z0-9_]+", r"gsk_[A-Za-z0-9_\-]+",
                r"nvapi-[A-Za-z0-9_\-]+", r"csk-[A-Za-z0-9_\-]+",
                r"secret_[A-Za-z0-9_\-]+"):
        s = re.sub(pat, "***REDACTED***", s)
    return s
