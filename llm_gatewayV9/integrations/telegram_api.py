"""Telegram Bot API passthrough (read + text send).

Key (gateway .env): TELEGRAM_BOT_TOKEN. `call(method, params)` posts JSON to
`https://api.telegram.org/bot<token>/<method>`. Text methods work; file uploads
(sendPhoto/sendDocument with a local file) need multipart and are not handled
here — use the agent's send_telegram tool for documents.

Response shape: {"ok": bool, "result": <Telegram result>, "description": str}
"""
from __future__ import annotations

from typing import Any

from adaptors.http import shared as pooled_http

from . import _MissingKey, _fail, _need


def call(method: str, params: dict | None = None) -> dict[str, Any]:
    try:
        token = _need("TELEGRAM_BOT_TOKEN")
    except _MissingKey:
        return _fail("TELEGRAM_BOT_TOKEN not set")
    try:
        with pooled_http(20.0) as c:
            r = c.post(f"https://api.telegram.org/bot{token}/{method}",
                       json=params or {})
            r.raise_for_status()
            x = r.json()
            return {"ok": bool(x.get("ok")),
                    "result": x.get("result"),
                    "description": x.get("description")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}