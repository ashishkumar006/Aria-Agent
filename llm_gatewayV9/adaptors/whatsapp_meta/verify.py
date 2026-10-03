"""Meta webhook verification (pure functions, fully unit-tested).

GET handshake: hub.mode=subscribe + hub.verify_token equality → echo
hub.challenge. POST bodies are trusted via HTTPS; with an App Secret set,
X-Hub-Signature-256 (HMAC-SHA256) is additionally checked.
"""
from __future__ import annotations

import hashlib
import hmac
import os


def verify_handshake(params: dict[str, str], verify_token: str) -> str | None:
    """Return hub.challenge iff mode=subscribe and token matches, else None."""
    if params.get("hub.mode") != "subscribe":
        return None
    tok = params.get("hub.verify_token", "")
    if not tok or not hmac.compare_digest(tok, verify_token):
        return None
    return params.get("hub.challenge")


def verify_signature(headers: dict[str, str], body: bytes) -> bool:
    """X-Hub-Signature-256 check when META_APP_SECRET is set; True (trusted
    transport) when it isn't — the route still gates on pairing + policy."""
    secret = (os.getenv("META_APP_SECRET") or "").encode()
    if not secret:
        return True
    sig = ""
    for k, v in headers.items():
        if k.lower() == "x-hub-signature-256":
            sig = v
            break
    if sig.startswith("sha256="):
        sig = sig[len("sha256="):]
    want = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return bool(sig) and hmac.compare_digest(sig, want)
