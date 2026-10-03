"""LINE webhook verification (pure function, fully unit-tested).

X-Line-Signature = base64(HMAC-SHA256(channel_secret, raw_body)).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os


def verify(headers: dict[str, str], body: bytes) -> bool:
    secret = (os.getenv("LINE_CHANNEL_SECRET") or "").encode()
    if not secret:
        return False
    sig = ""
    for k, v in headers.items():
        if k.lower() == "x-line-signature":
            sig = v
            break
    if not sig:
        return False
    want = base64.b64encode(hmac.new(secret, body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(sig, want)
