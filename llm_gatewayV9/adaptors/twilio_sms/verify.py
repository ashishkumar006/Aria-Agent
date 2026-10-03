"""Twilio webhook verification (pure function, fully unit-tested).

X-Twilio-Signature = base64(HMAC-SHA1(auth_token, full_url + sorted(k+v))).
Shared by twilio_sms, whatsapp_twilio and twilio_voice — import from here,
don't reimplement. Needs the exact public webhook URL (TWILIO_WEBHOOK_URL).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from urllib.parse import parse_qsl


def verify(headers: dict[str, str], body: bytes, url: str = "") -> bool:
    auth = (os.getenv("TWILIO_AUTH") or "").encode()
    url = url or (os.getenv("TWILIO_WEBHOOK_URL") or "").strip()
    if not auth or not url:
        return False
    sig = ""
    for k, v in headers.items():
        if k.lower() == "x-twilio-signature":
            sig = v
            break
    if not sig:
        return False
    try:
        params = sorted(parse_qsl(body.decode()))
        data = url + "".join(k + v for k, v in params)
        want = base64.b64encode(hmac.new(auth, data.encode(), hashlib.sha1).digest())
        return hmac.compare_digest(sig, want.decode())
    except Exception:
        return False
