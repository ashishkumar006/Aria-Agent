"""Slack request verification (pure function, fully unit-tested).

X-Slack-Signature v0 HMAC over `v0:{timestamp}:{raw_body}` with the signing
secret, plus a 5-minute replay window on the timestamp. Without
SLACK_SIGNING_SECRET nothing can be verified (returns False).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time


def verify(headers: dict[str, str], body: bytes) -> bool:
    secret = (os.getenv("SLACK_SIGNING_SECRET") or "").encode()
    if not secret:
        return False  # can't verify without the signing secret
    h = {k.lower(): v for k, v in headers.items()}
    ts, sig = h.get("x-slack-request-timestamp", ""), h.get("x-slack-signature", "")
    try:
        if abs(time.time() - int(ts)) > 300:
            return False  # replay guard: 5-minute window
    except ValueError:
        return False
    want = "v0=" + hmac.new(secret, f"v0:{ts}:".encode() + body,
                            hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, want)
