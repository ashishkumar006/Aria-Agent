"""Google Chat integration — free with Google Workspace.

Auth: a space's incoming webhook URL (Google Chat space -> Apps & integrations
-> Webhooks). No OAuth needed for a webhook.
  GOOGLE_CHAT_WEBHOOK (required) -> the space webhook URL
`to` is unused: the webhook is bound to one space.
"""
from __future__ import annotations

from typing import Any

from adaptors.http import shared as pooled_http

from . import _MissingKey, _fail, _need


def send(*, text: str, to: str = "") -> dict[str, Any]:
    try:
        hook = _need("GOOGLE_CHAT_WEBHOOK")
    except _MissingKey:
        return _fail("GOOGLE_CHAT_WEBHOOK not set")
    try:
        with pooled_http(20.0) as client:
            r = client.post(hook, json={"text": text})
            if r.status_code >= 400:
                return {"ok": False, "error": f"google_chat {r.status_code}"}
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}