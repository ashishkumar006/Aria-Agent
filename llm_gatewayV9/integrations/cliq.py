"""Zoho Cliq integration — free team chat (Slack/Teams peer).

Auth: a bot webhook token (simplest, no app review) or OAuth. This module uses
the bot message endpoint with a token:
  ZOHO_CLIQ_TOKEN (required)  -> the bot's webhook token
  ZOHO_CLIQ_DC    (optional)  -> data centre: com | eu | in | com.au | jp | ca
`to` is the bot (or channel) unique name the message is posted as.
"""
from __future__ import annotations

import os
from typing import Any

from adaptors.http import shared as pooled_http

from . import _MissingKey, _fail, _need


def _dc() -> str:
    dc = (os.getenv("ZOHO_CLIQ_DC") or "com").strip().strip('"').strip("'").lower()
    return dc or "com"


def send(*, to: str, text: str, title: str = "") -> dict[str, Any]:
    try:
        token = _need("ZOHO_CLIQ_TOKEN")
    except _MissingKey:
        return _fail("ZOHO_CLIQ_TOKEN not set")
    target = (to or "").strip()
    if not target:
        return {"ok": False, "error": "target (bot/channel unique name) is required"}
    url = (f"https://cliq.zoho.{_dc()}/api/v2/bots/{target}/message"
           f"?token={token}")
    payload: dict[str, Any] = {"text": text}
    if title:
        payload["title"] = title[:64]
    try:
        with pooled_http(20.0) as client:
            r = client.post(url, json=payload)
            if r.status_code >= 400:
                return {"ok": False, "error": f"cliq {r.status_code}"}
        return {"ok": True, "to": target}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}