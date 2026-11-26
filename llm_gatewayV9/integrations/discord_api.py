"""Discord Bot API passthrough (read + invite helper).

Key (gateway .env): DISCORD_BOT_TOKEN. Text/JSON endpoints only; message sends
stay on the discord channel adaptor.
"""
from __future__ import annotations

from typing import Any

from adaptors.http import shared as pooled_http

from . import _MissingKey, _fail, _need

API = "https://discord.com/api/v10"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bot {token}", "User-Agent": "AriaGateway/1.0"}


def call(method: str, path: str, params: dict | None = None) -> dict[str, Any]:
    """GET a Discord endpoint. `path` is e.g. '/users/@me'."""
    try:
        token = _need("DISCORD_BOT_TOKEN")
    except _MissingKey:
        return _fail("DISCORD_BOT_TOKEN not set")
    try:
        with pooled_http(20.0) as c:
            r = c.get(f"{API}{path}", headers=_headers(token), params=params)
            r.raise_for_status()
            return {"ok": True, "result": r.json()}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def invite_url(permissions: str = "0") -> dict[str, Any]:
    """Build the OAuth2 bot invite URL. `permissions` is a bitfield string
    (e.g. '8' = Administrator, '2048' = Send Messages)."""
    try:
        token = _need("DISCORD_BOT_TOKEN")
    except _MissingKey:
        return _fail("DISCORD_BOT_TOKEN not set")
    try:
        with pooled_http(20.0) as c:
            r = c.get(f"{API}/users/@me", headers=_headers(token))
            r.raise_for_status()
            bot_id = r.json()["id"]
        return {"ok": True, "client_id": bot_id,
                "url": (f"https://discord.com/oauth2/authorize?client_id={bot_id}"
                        f"&scope=bot&permissions={permissions}")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}