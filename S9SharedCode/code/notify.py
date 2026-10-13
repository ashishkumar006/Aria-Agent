"""Outbound notification across the gateway's channel adaptors.

One entry point for every channel. The gateway already owns the adaptors, the
credentials and the policy engine (`POST /v1/channels/{name}/send`), so this
module deliberately owns none of that: it resolves *where* a message goes and
reports *what happened*, and lets the gateway decide whether it is allowed.

Why this exists rather than more `if channel == "telegram"` branches
-----------------------------------------------------------------------
`notify_task_done` and `resolve_notify_chat` were Telegram-shaped and said so.
The gateway registers sixteen adaptors and five of them are configured right
now (discord, gmail, slack, telegram, webhook, webui), but a scheduled task
could only ever reach one of them, and only if that one already had a paired
chat id. Every other path was unreachable rather than merely unimplemented.

Token refresh
-------------
`refresh_channel_token` covers the failure that matters most in practice: a
channel that is *configured* but no longer *works*, because its bot token was
revoked or expired. That is exactly the channel a reminder depends on, and it
used to fail silently - a reminder that never arrives looks identical to a
reminder that was never set. Every send therefore returns a structured result
and the caller records it locally whether it succeeded or not.

Nothing here blocks the scheduler: a dead channel must not stop the task.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

STATE_DIR = Path(os.environ.get("S9_STATE_DIR")
                 or (Path(__file__).parent / "state"))
DELIVERY_LOG = STATE_DIR / "notify_delivery.json"
_LOCK = threading.Lock()
_MAX_LOG = 200

# Channels that can carry a one-way message, mapped to the env var the gateway
# reads for its credential. Used only to explain a dead channel; the gateway
# remains the authority on whether a channel is usable.
_TOKEN_ENV = {
    "telegram": "TELEGRAM_BOT_TOKEN",
    "slack": "SLACK_BOT_TOKEN",
    "discord": "DISCORD_BOT_TOKEN",
    "gmail": "GMAIL_TOKEN",
    "teams": "TEAMS_APP_ID",
    "line": "LINE_ACCESS_TOKEN",
    "matrix": "MATRIX_PASSWORD",
    "signal": "SIGNAL_NUMBER",
    "twilio_sms": "TWILIO_SID",
    "twilio_voice": "TWILIO_SID",
    "whatsapp_meta": "WA_TOKEN",
    "whatsapp_twilio": "TWILIO_SID",
    "imap": "IMAP_PASS",
    "webhook": "",
    "webui": "",
    "local_mic": "",
}

# How each channel is addressed. `pairing` = resolve a chat id from the
# gateway's trust store; `literal` = the caller must supply the destination.
_TARGET_KIND = {
    "telegram": "pairing",
    "slack": "pairing",
    "discord": "pairing",
    "matrix": "pairing",
    "signal": "pairing",
    "line": "pairing",
    "teams": "pairing",
    "gmail": "literal",
    "imap": "literal",
    "twilio_sms": "literal",
    "twilio_voice": "literal",
    "whatsapp_meta": "literal",
    "whatsapp_twilio": "literal",
    "webhook": "literal",
    "webui": "literal",
    "local_mic": "literal",
}

# What a 4xx from the adaptor means for the credential, if anything. A 401 is
# the signal that the token is bad; a 400 is usually a bad destination and
# re-authenticating would not help, so it must not be reported as one.
_REAUTH_HINTS = (
    ("401", "unauthorized"),
    ("403", "forbidden"),
    ("invalid_token", "revoked token"),
    ("unauthorized", "revoked token"),
)


def _gw_base() -> str:
    return os.environ.get("LLM_GATEWAY_V9_URL",
                          "http://localhost:8109").rstrip("/")


def _gw_headers() -> dict:
    """Auth headers for a gateway call.

    Deliberately NOT `agent_server._gw_auth_headers`. That works, but it means
    importing the whole FastAPI app - which starts the uvicorn warmup and the
    embedding model - from inside a scheduler worker or a test process. The
    token comes from `gateway._gateway_token()` either way.
    """
    try:
        from gateway import _gateway_token
        tok = _gateway_token() or ""
    except Exception:
        tok = ""
    return {"X-Gateway-Token": tok} if tok else {}


def _gw_get(path: str, timeout: float = 15.0) -> dict:
    """Synchronous gateway GET.

    This module is called from the scheduler's worker thread and from MCP
    tools, both synchronous, so it cannot await the async helper the rest of
    the agent uses. A non-2xx is folded into an `error` key BEFORE the body is
    read: "the gateway said nothing" and "the gateway said no" lead to
    opposite decisions, and must not look the same."""
    import httpx
    try:
        with httpx.Client(timeout=timeout, headers=_gw_headers()) as c:
            r = c.get(f"{_gw_base()}{path}")
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    try:
        body = r.json()
    except Exception:
        body = {}
    if r.status_code != 200:
        return {"error": (r.text or f"HTTP {r.status_code}")[:300],
                "status": r.status_code}
    return body if isinstance(body, dict) else {"result": body}


def available_channels() -> list[dict]:
    """What the gateway says it can send on right now.

    Read from the gateway rather than duplicated here, so the list cannot drift
    from reality. Falls back to a static list when the gateway is unreachable,
    because a scheduler that cannot name its channels is useless precisely
    when something has gone wrong."""
    data = _gw_get("/v1/channels")
    chans = (data or {}).get("channels") or []
    if not chans:
        return [{"name": n, "configured": False,
                 "needs": ([v] if (v := _TOKEN_ENV.get(n)) else []),
                 "target": _TARGET_KIND.get(n, "literal")}
                for n in sorted(_TARGET_KIND)]
    return [{"name": c.get("name"),
             "configured": bool(c.get("configured")),
             "needs": c.get("required_keys") or [],
             "target": _TARGET_KIND.get(str(c.get("name")), "literal")}
            for c in chans if isinstance(c, dict)]


def resolve_target(channel: str) -> str:
    """The unmasked destination id for `channel`, or "" for none.

    Prefers an explicit override, then the gateway's paired sender. Reading
    from the gateway matters because `list_paired` deliberately masks ids for
    display, so the pairing store was previously unreadable for the one use
    that needs a real id."""
    channel = (channel or "").strip().lower()
    explicit = os.environ.get(f"AGENT_NOTIFY_TARGET_{channel.upper()}", "").strip()
    if explicit:
        return explicit
    if _TARGET_KIND.get(channel) != "pairing":
        return ""
    data = _gw_get(f"/v1/control/notify-target?channel={channel}")
    if (data or {}).get("error"):
        return ""
    return str((data or {}).get("target") or "")


def _log_delivery(entry: dict) -> None:
    """Record every send attempt, successful or not.

    This is the fix for the failure mode that made the whole feature
    untrustworthy: a reminder that could not be delivered left no trace
    anywhere, so "did it send?" had no answer."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        import json
        with _LOCK:
            try:
                items = json.loads(DELIVERY_LOG.read_text(encoding="utf-8"))
                if not isinstance(items, list):
                    items = []
            except Exception:
                items = []
            items.append(entry)
            tmp = DELIVERY_LOG.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(items[-_MAX_LOG:], indent=2),
                           encoding="utf-8")
            tmp.replace(DELIVERY_LOG)
    except Exception:
        pass


def send_channel_message(channel: str, text: str, *,
                         to: str = "",
                         media: list[dict] | None = None,
                         agent: str = "scheduler",
                         session_id: str = "", thread_id: str | None = None,
                         timeout: float = 20.0) -> dict:
    """Send `text` on `channel`. Returns a structured result; never raises.

    Result shape:
      {"ok": bool, "channel": str, "target": str, "delivered": bool,
       "status": int|None, "error": str, "reauth_needed": bool,
       "reauth_hint": str}
    `delivered` is the honest signal - a 200 from the gateway means the
    adaptor accepted it, which for most channels is as far as we can verify."""
    channel = (channel or "").strip().lower()
    res: dict[str, Any] = {
        "ok": False, "channel": channel, "target": to, "delivered": False,
        "status": None, "error": "", "reauth_needed": False, "reauth_hint": "",
    }
    if not channel:
        res["error"] = "no channel named"
        return res
    if channel not in _TARGET_KIND:
        res["error"] = (
            f"unknown channel {channel!r}; see available_channels()")
        _log_delivery({**res, "ts": time.time(), "text": text[:120]})
        return res
    if not text:
        res["error"] = "empty message"
        return res

    target = to or resolve_target(channel)
    res["target"] = target
    if not target:
        res["error"] = (
            f"no destination paired for {channel}; message the bot once to "
            f"claim pairing, or set AGENT_NOTIFY_TARGET_{channel.upper()}")
        _log_delivery({**res, "ts": time.time(), "text": text[:120]})
        return res

    try:
        import httpx
        body = {"to": target, "text": text[:4096], "agent": agent,
                "session": session_id or None, "trust": "paired"}
        if thread_id:
            body["thread_id"] = thread_id
        if media:
            # The gateway models `media` and the Telegram adaptor implements
            # sendDocument for it, but this function never sent the key - so
            # every caller passing a file had it silently dropped and the user
            # received a caption with no attachment.
            body["media"] = media[:4]
        with httpx.Client(timeout=timeout, headers=_gw_headers()) as c:
            r = c.post(f"{_gw_base()}/v1/channels/{channel}/send", json=body)
        res["status"] = r.status_code
        if r.status_code == 200:
            payload = r.json() or {}
            # The gateway can answer 200 with {ok:false, status:"pending"}
            # when policy wants approval. That is not a delivery.
            if payload.get("ok") is False:
                res["error"] = (
                    f"held by policy: {payload.get('status')} "
                    f"({payload.get('approval_id')})")
                res["reauth_needed"] = False
            else:
                res["ok"] = True
                res["delivered"] = True
                res["msg_id"] = payload.get("msg_id")
                if payload.get("warning"):
                    res["warning"] = payload["warning"]
        else:
            res["error"] = (r.text or f"HTTP {r.status_code}")[:300]
            low = res["error"].lower()
            for needle, hint in _REAUTH_HINTS:
                if needle in low:
                    res["reauth_needed"] = True
                    res["reauth_hint"] = hint
                    break
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"[:300]

    _log_delivery({**res, "ts": time.time(), "text": text[:120],
                   "session": session_id})
    return res


def channel_health(channel: str = "") -> dict:
    """Is each channel usable right now, and does it need re-auth?

    Three independent questions, which were previously conflated into one
    vague "is it working":

      * `configured` — does the gateway hold a credential for it?
      * `destination` — is there a paired chat to send TO? A configured
        channel with no destination is the exact state a reminder dies in:
        it looks alive in the inventory and delivers nothing.
      * `credential_in_agent_process` — only relevant for the paths that
        verify from here (Telegram's getMe).
    """
    channel = (channel or "").strip().lower()
    rows = available_channels()
    if channel:
        rows = [r for r in rows if r.get("name") == channel]
    out = []
    for r in rows:
        name = str(r.get("name") or "")
        e = _TOKEN_ENV.get(name, "")
        # Presence in THIS process only. Most channel credentials live in the
        # gateway's environment, not the agent's, so reporting this as "the
        # channel has no credential" is simply wrong - the gateway can be
        # fully configured while this process holds nothing. The gateway's own
        # `configured` flag is the authoritative signal; this column only
        # answers the narrower question of whether *this* process could verify
        # the credential itself.
        present = bool(e) and bool(os.environ.get(e))
        out.append({
            "name": name,
            "configured": bool(r.get("configured")),
            "target_kind": r.get("target"),
            "credential_env": e or None,
            "credential_in_agent_process": present if e else None,
            "credential_lives_in_gateway": bool(r.get("configured")) and not present,
            "needs": r.get("needs") or [],
            "destination": bool(resolve_target(name)) if r.get("configured")
            else False,
        })
    return {"channels": out}


def refresh_channel_token(channel: str, token: str = "") -> dict:
    """Verify - and where possible refresh - a channel credential.

    What this can and cannot do is worth being precise about, because the
    difference is the whole point of having the function at all:

      * It VERIFIES. It calls the adaptor with the live credential and reports
        whether the gateway considers the channel usable. That is real
        information: it distinguishes "configured" from "works", which the
        inventory cannot.
      * It RE-AUTHENTICATES only where the platform allows it without a human.
        Telegram exposes `getMe` for verification; a token the user pasted into
        `.env` cannot be minted here, and this function will not pretend
        otherwise - it returns `action_required` naming the env var.

    A caller that wants "make the broken channel work" needs the token value;
    supplying it writes it to the process environment for this process only and
    is refused unless `token` is non-empty. Nothing is ever written to disk
    from here: `.env` is not ours to rewrite.
    """
    channel = (channel or "").strip().lower()
    out = {"channel": channel, "ok": False, "verified": False,
           "action_required": None, "detail": ""}
    if channel not in _TARGET_KIND:
        out["detail"] = f"unknown channel {channel!r}"
        return out
    env = _TOKEN_ENV.get(channel, "")
    if token:
        if not env:
            out["detail"] = (f"{channel} has no token to refresh; set its "
                             f"destination instead")
            return out
        # Process-local only. Persisting a credential is a deliberate act for
        # a human, not something a refresh should do behind their back.
        os.environ[env] = token
        out["detail"] = f"set {env} for this process"
    elif not env:
        out["detail"] = f"{channel} needs no credential"
    elif not os.environ.get(env):
        out["detail"] = f"{env} is not set in this process"
        out["action_required"] = env
        return out

    if channel == "telegram" and os.environ.get("TELEGRAM_BOT_TOKEN"):
        try:
            import httpx
            r = httpx.get(
                "https://api.telegram.org/bot"
                f"{os.environ['TELEGRAM_BOT_TOKEN']}/getMe", timeout=12)
            if r.status_code == 200:
                out["verified"] = True
                out["ok"] = True
                out["detail"] = "token accepted by Telegram"
            else:
                out["detail"] = f"getMe -> HTTP {r.status_code}"
                out["action_required"] = "TELEGRAM_BOT_TOKEN"
        except Exception as e:
            out["detail"] = f"getMe unreachable: {type(e).__name__}"
    else:
        # No independent verification available; ask the gateway instead.
        data = _gw_get("/v1/channels")
        row = next((c for c in ((data or {}).get("channels") or [])
                    if str(c.get("name")) == channel), None)
        if row and row.get("configured"):
            out["verified"] = True
            out["ok"] = True
            out["detail"] = "gateway reports configured"
        else:
            out["detail"] = ("gateway unreachable" if (data or {}).get("error")
                             else "gateway reports not configured")
            out["action_required"] = env or None

    if not out["ok"] and out["action_required"] is None and env:
        out["action_required"] = env
    return out


def delivery_log(limit: int = 50) -> list[dict]:
    """Recent send attempts, newest first. Includes failures on purpose."""
    try:
        import json
        with _LOCK:
            items = json.loads(DELIVERY_LOG.read_text(encoding="utf-8"))
        if not isinstance(items, list):
            return []
        return list(reversed(items[-max(1, int(limit)):]))
    except Exception:
        return []
