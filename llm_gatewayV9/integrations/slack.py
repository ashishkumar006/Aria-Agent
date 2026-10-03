"""Slack token rotation (gateway-owned).

Slack access tokens never expire by default — but with the app's *token
rotation* toggle ON they die every 12h (expires_in=43200) and Slack issues
a single-use refresh token (xoxe-…). This module mirrors the Gmail refresh
pattern: exchange the refresh triple for a fresh pair and persist it.

Env (all gateway .env):
  SLACK_BOT_TOKEN      current access token (xoxb-… or rotating xoxe-…)
  SLACK_REFRESH_TOKEN  single-use refresh token (rotation only)
  SLACK_CLIENT_ID / SLACK_CLIENT_SECRET  app credentials (rotation only)

Values are never logged — only key names appear in errors.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

ENV_PATH = Path(__file__).parent.parent / ".env"


def _read_env() -> str:
    return ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""


def _write_pair(access_token: str, refresh_token: str | None) -> None:
    text = _read_env()
    pairs = {"SLACK_BOT_TOKEN": access_token}
    if refresh_token:
        pairs["SLACK_REFRESH_TOKEN"] = refresh_token
    for key, val in pairs.items():
        if re.search(rf"^\s*{key}=", text, re.M):
            text = re.sub(rf"^\s*{key}=.*$", f"{key}={val}", text, flags=re.M)
        else:
            text = text.rstrip() + f"\n{key}={val}\n"
    ENV_PATH.write_text(text, encoding="utf-8")
    os.environ["SLACK_BOT_TOKEN"] = access_token
    if refresh_token:
        os.environ["SLACK_REFRESH_TOKEN"] = refresh_token


def refresh(write_env: bool = True) -> dict[str, Any]:
    """Exchange SLACK_REFRESH_TOKEN for a fresh access + refresh pair.

    POSTs oauth.v2.access with grant_type=refresh_token. On success the new
    pair replaces the old in live env and (when write_env) in .env.
    The old refresh token is single-use — Slack revokes it after a grace
    period, so callers must use the NEW pair from now on.
    """
    from adaptors import http as _http

    rt = (os.getenv("SLACK_REFRESH_TOKEN") or "").strip()
    cid = (os.getenv("SLACK_CLIENT_ID") or "").strip()
    csec = (os.getenv("SLACK_CLIENT_SECRET") or "").strip()
    if not (rt and cid and csec):
        return {"ok": False,
                "error": "SLACK_REFRESH_TOKEN / SLACK_CLIENT_ID / SLACK_CLIENT_SECRET not set"}
    r = _http.post("https://slack.com/api/oauth.v2.access",
                   data={"grant_type": "refresh_token",
                         "client_id": cid, "client_secret": csec,
                         "refresh_token": rt},
                   timeout=20.0)
    try:
        d = r.json()
    except Exception:
        return {"ok": False, "error": f"non-JSON from oauth.v2.access (HTTP {r.status_code})"}
    if not d.get("ok"):
        return {"ok": False, "error": f"refresh rejected: {d.get('error', 'unknown')}"}
    access, new_refresh = d.get("access_token", ""), d.get("refresh_token", "")
    if not access:
        return {"ok": False, "error": "no access_token in refresh response"}
    if write_env:
        _write_pair(access, new_refresh or None)
    else:
        os.environ["SLACK_BOT_TOKEN"] = access
        if new_refresh:
            os.environ["SLACK_REFRESH_TOKEN"] = new_refresh
    return {"ok": True, "expires_in": d.get("expires_in", 43200),
            "token_type": d.get("token_type", "bot"),
            "rotated_refresh": bool(new_refresh)}


def history(*, channel: str, limit: int = 20) -> dict[str, Any]:
    """Read recent messages from a channel (read-only) via
    conversations.history. `channel` is a channel ID (C…); names are NOT
    accepted by this endpoint — resolve via conversations.list first (the
    agent should ask the user for the ID or use a known one). Mirrors the
    fail-soft convention: missing token or API error returns ok False."""
    import httpx as _httpx

    token = (os.getenv("SLACK_BOT_TOKEN") or "").strip()
    if not token:
        return {"ok": False, "error": "SLACK_BOT_TOKEN not set"}
    try:
        n = max(1, min(int(limit or 20), 100))
    except (TypeError, ValueError):
        n = 20
    try:
        with _httpx.Client(timeout=20) as client:
            r = client.get("https://slack.com/api/conversations.history",
                           params={"channel": channel, "limit": n},
                           headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            d = r.json()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if not d.get("ok"):
        return {"ok": False, "error": f"slack API error: {d.get('error', 'unknown')}"}
    msgs = []
    for m in (d.get("messages") or [])[:n]:
        text = m.get("text", "")
        if m.get("subtype") == "bot_message":
            who = (m.get("username") or m.get("bot_id", "bot"))
        else:
            who = m.get("user", "unknown")
        msgs.append({"user": who, "text": text[:1000],
                     "ts": m.get("ts", ""),
                     "thread": bool(m.get("thread_ts"))})
    return {"ok": True, "channel": channel, "messages": msgs}
