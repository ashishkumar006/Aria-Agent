"""Generic webhook verification (pure function, fully unit-tested).

Optional HMAC-SHA256 shared secret (WEBHOOK_SECRET) checked against
X-Signature / X-Hub-Signature-256 / X-Webhook-Signature headers.
Without a secret the endpoint is open — everything arrives untrusted.
"""
from __future__ import annotations

import hashlib
import hmac
import os


def verify(headers: dict[str, str], body: bytes) -> bool:
    secret = (os.getenv("WEBHOOK_SECRET") or "").encode()
    if not secret:
        return True  # open endpoint: everything arrives untrusted
    sig = ""
    for k, v in headers.items():
        if k.lower() in ("x-signature", "x-hub-signature-256", "x-webhook-signature"):
            sig = v
            break
    if sig.startswith("sha256="):
        sig = sig[len("sha256="):]
    want = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return bool(sig) and hmac.compare_digest(sig, want)
