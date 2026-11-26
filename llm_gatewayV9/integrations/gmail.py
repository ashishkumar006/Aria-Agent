"""Gmail integration (REST + OAuth2). Moved verbatim from agent mcp_server.

Keys (gateway .env): GMAIL_TOKEN (+ GMAIL_REFRESH_TOKEN/GMAIL_CLIENT_ID/
GMAIL_CLIENT_SECRET for refresh). gmail_refresh_token(write_env=True)
rewrites GMAIL_TOKEN in the GATEWAY .env (never the agent's).
"""
from __future__ import annotations

import base64
import re
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx
from adaptors.http import shared as pooled_http


from . import _MissingKey, _fail, _need

GW_ENV = Path(__file__).resolve().parent.parent / ".env"


def send_email(*, to: str, subject: str, body: str,
               thread_id: str | None = None, attachment_b64: str = "",
               attachment_name: str = "",
               attachments: list[dict] | None = None) -> dict[str, Any]:
    try:
        token = _need("GMAIL_TOKEN")
    except _MissingKey:
        return _fail("GMAIL_TOKEN not set (Gmail OAuth access token)")
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    # Build the attachment list: an explicit `attachments` list wins; otherwise
    # the single attachment_b64/attachment_name pair (legacy shape).
    atts: list[dict] = list(attachments or [])
    if attachment_b64 and not atts:
        atts = [{"name": attachment_name or "attachment",
                 "data_b64": attachment_b64}]
    if atts:
        import mimetypes
        for att in atts[:10]:
            if not isinstance(att, dict):
                continue
            data_b64 = str(att.get("data_b64") or "")
            if not data_b64:
                continue
            try:
                data = base64.b64decode(data_b64)
            except Exception:
                continue
            name = (str(att.get("name") or "") or "attachment").strip()
            ctype, _ = mimetypes.guess_type(name)
            maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
            msg.add_attachment(data, maintype=maintype or "application",
                               subtype=subtype or "octet-stream", filename=name)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    payload: dict[str, Any] = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    try:
        with pooled_http(20.0) as client:
            r = client.post(url, headers={"Authorization": f"Bearer {token}"},
                            json=payload)
            if r.status_code == 401:
                # Hourly expiry: renew once from the refresh triple and retry
                # (same self-healing contract as the Calendar adaptor — a
                # 401 here must not need a human).
                res = refresh(write_env=True)
                if not res.get("ok"):
                    return {"ok": False,
                            "error": f"GMAIL_TOKEN expired; refresh failed: {res.get('error')}"}
                r = client.post(url, headers={"Authorization": f"Bearer {res['access_token']}"},
                                json=payload)
            if r.status_code == 401:
                return {"ok": False, "error": "GMAIL_TOKEN expired or invalid (re-auth needed)"}
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "to": to, "subject": subject, "id": data.get("id"),
            "thread_id": thread_id, "attached": bool(atts),
            "attachment_count": len(atts),
            "attachment_name": attachment_name if (attachment_b64 and not attachments) else ""}


def profile() -> dict[str, Any]:
    """The signed-in mailbox: emailAddress, messagesTotal, threadsTotal.

    This is how you answer "which account is connected?" — the address the
    OAuth token actually belongs to, not the client id.
    """
    try:
        token = _need("GMAIL_TOKEN")
    except _MissingKey:
        return _fail("GMAIL_TOKEN not set (Gmail OAuth access token)")
    try:
        with pooled_http(20.0) as client:
            r = client.get("https://gmail.googleapis.com/gmail/v1/users/me/profile",
                           headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 401:
                # Hourly expiry: renew once, then retry (same as query/send).
                res = refresh(write_env=True)
                if not res.get("ok"):
                    return {"ok": False,
                            "error": f"GMAIL_TOKEN expired; refresh failed: {res.get('error')}"}
                r = client.get("https://gmail.googleapis.com/gmail/v1/users/me/profile",
                               headers={"Authorization": f"Bearer {res['access_token']}"})
            r.raise_for_status()
            x = r.json()
        return {"ok": True, "email": x.get("emailAddress"),
                "messages_total": x.get("messagesTotal"),
                "threads_total": x.get("threadsTotal")}
    except Exception as e:  # noqa: BLE001
        return _fail(f"{type(e).__name__}: {e}")


def query(*, api_method: str, query: str = "", max_results: int = 5) -> dict[str, Any]:
    try:
        token = _need("GMAIL_TOKEN")
    except _MissingKey:
        return _fail("GMAIL_TOKEN not set")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with pooled_http(20.0) as client:
            if api_method == "list":
                r = client.get("https://gmail.googleapis.com/gmail/v1/users/me/messages",
                               headers=headers, params={"q": query, "maxResults": max_results})
                if r.status_code == 401:
                    # Hourly expiry: renew once, then retry (see send_email).
                    res = refresh(write_env=True)
                    if not res.get("ok"):
                        return {"ok": False,
                                "error": f"GMAIL_TOKEN expired; refresh failed: {res.get('error')}"}
                    headers = {"Authorization": f"Bearer {res['access_token']}"}
                    r = client.get("https://gmail.googleapis.com/gmail/v1/users/me/messages",
                                   headers=headers,
                                   params={"q": query, "maxResults": max_results})
                r.raise_for_status()
                ids = [m["id"] for m in r.json().get("messages", [])]
                return {"ok": True, "message_ids": ids}
            if api_method == "read":
                r = client.get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{query}",
                               headers=headers, params={"format": "full"})
                if r.status_code == 401:
                    res = refresh(write_env=True)
                    if not res.get("ok"):
                        return {"ok": False,
                                "error": f"GMAIL_TOKEN expired; refresh failed: {res.get('error')}"}
                    headers = {"Authorization": f"Bearer {res['access_token']}"}
                    r = client.get(
                        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{query}",
                        headers=headers, params={"format": "full"})
                r.raise_for_status()
                msg = r.json()
                return {"ok": True, "id": query, "snippet": msg.get("snippet", ""),
                        "headers": {h["name"]: h["value"] for h in
                                    msg.get("payload", {}).get("headers", []) if h["name"] in
                                    ("Subject", "From", "Date")}}
            return {"ok": False, "error": f"unknown api_method '{api_method}'"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def refresh(*, write_env: bool = True) -> dict[str, Any]:
    import os
    vals = {k: (os.getenv(k) or "").strip().strip('"').strip("'")
            for k in ("GMAIL_REFRESH_TOKEN", "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET")}
    if not all(vals.values()):
        return {"ok": False, "error": "GMAIL_REFRESH_TOKEN / GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET not set"}
    try:
        r = None
        with pooled_http(20.0) as client:
            r = client.post("https://oauth2.googleapis.com/token", data={
                "client_id": vals["GMAIL_CLIENT_ID"],
                "client_secret": vals["GMAIL_CLIENT_SECRET"],
                "refresh_token": vals["GMAIL_REFRESH_TOKEN"],
                "grant_type": "refresh_token",
            })
            r.raise_for_status()
            resp = r.json()
        new_token = resp.get("access_token")
        if not new_token:
            return {"ok": False, "error": f"no access_token in response: {resp}"}
    except Exception as e:
        # Fold the upstream body in: "400 Bad Request" alone does not say
        # whether the refresh token was revoked (invalid_grant) or the client
        # credentials are wrong — and those need different fixes.
        detail = ""
        try:
            detail = (r.text[:200] if r is not None else "")
        except Exception:
            detail = ""
        return {"ok": False,
                "error": f"{type(e).__name__}: {e}" + (f" | {detail}" if detail else "")}
    if write_env:
        try:
            text = GW_ENV.read_text(encoding="utf-8") if GW_ENV.exists() else ""
            if re.search(r"^\s*GMAIL_TOKEN=", text, re.M):
                text = re.sub(r"^\s*GMAIL_TOKEN=.*$", f"GMAIL_TOKEN={new_token}",
                              text, flags=re.M)
            else:
                text = text.rstrip() + f"\nGMAIL_TOKEN={new_token}\n"
            GW_ENV.write_text(text, encoding="utf-8")
        except Exception as e:
            return {"ok": True, "access_token": new_token,
                    "warning": f"token refreshed but gateway .env write failed: {e}"}
    # The running process reads process env (loaded at boot), NOT the file —
    # without this the refreshed token only takes effect after a restart and
    # every call keeps 401-ing until then.
    import os as _os
    _os.environ["GMAIL_TOKEN"] = new_token
    return {"ok": True, "access_token": new_token, "expires_in": resp.get("expires_in")}
