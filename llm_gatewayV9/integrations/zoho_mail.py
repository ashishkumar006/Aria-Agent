"""Zoho Mail integration (REST + OAuth2). Gateway-owned credentials.

Keys (gateway .env):
  ZOHO_REFRESH_TOKEN + ZOHO_CLIENT_ID + ZOHO_CLIENT_SECRET  -> minted by
      zoho_mail_oauth_setup.py (or the Zoho API Console self-client flow).
  ZOHO_TOKEN       -> the short-lived access token (written by refresh()).
  ZOHO_DC          -> data centre: com | eu | in | com.au | jp | ca (default com).
  ZOHO_USER        -> optional: the mailbox address to send FROM / read.
  ZOHO_ACCOUNT_ID  -> optional: pin the numeric account id (skips /accounts lookup).

Endpoints (Zoho Mail REST):
  POST /api/accounts/{id}/messages                 -> send (JSON body)
  POST /api/accounts/{id}/messages/attachments      -> upload one attachment
  GET  /api/accounts/{id}/messages/view             -> list messages in a folder
  GET  /api/accounts/{id}/folders                   -> folders
Free plan: 5 users, 5 GB, custom domain, India DC. REST works on the free plan.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
from adaptors.http import shared as pooled_http

from . import _MissingKey, _fail, _need

GW_ENV = Path(__file__).resolve().parent.parent / ".env"

SCOPES = ("ZohoMail.accounts.READ ZohoMail.messages.READ "
          "ZohoMail.messages.CREATE ZohoMail.folders.READ "
          "ZohoMail.messages.UPDATE "
          "ZohoCalendar.calendar.READ ZohoCalendar.event.CREATE "
          "ZohoCalendar.event.READ")

_DC_RE = re.compile(r"^[a-z]{2,3}(\.[a-z]{2})?$")


def _dc() -> str:
    import os
    dc = (os.getenv("ZOHO_DC") or "com").strip().strip('"').strip("'").lower()
    return dc if _DC_RE.match(dc) else "com"


def _mail_base() -> str:
    return f"https://mail.zoho.{_dc()}"


def _accounts_base() -> str:
    return f"https://accounts.zoho.{_dc()}"


def _token_url() -> str:
    return f"{_accounts_base()}/oauth/v2/token"


def _client() -> tuple[str, str]:
    cid = _need("ZOHO_CLIENT_ID")
    csec = _need("ZOHO_CLIENT_SECRET")
    return cid, csec


def refresh(*, write_env: bool = True) -> dict[str, Any]:
    """Exchange ZOHO_REFRESH_TOKEN for a fresh access token (no browser)."""
    import os
    try:
        cid, csec = _client()
    except _MissingKey:
        return {"ok": False, "error": "ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET not set"}
    rtok = (os.getenv("ZOHO_REFRESH_TOKEN") or "").strip().strip('"').strip("'")
    if not rtok:
        return {"ok": False, "error": "ZOHO_REFRESH_TOKEN not set"}
    r = None
    try:
        with pooled_http(20.0) as client:
            r = client.post(_token_url(), data={
                "refresh_token": rtok, "client_id": cid,
                "client_secret": csec, "grant_type": "refresh_token"})
            r.raise_for_status()
            resp = r.json()
        new_token = resp.get("access_token")
        if not new_token:
            return {"ok": False, "error": f"no access_token in response: {resp}"}
    except Exception as e:  # noqa: BLE001
        detail = ""
        try:
            detail = (r.text[:200] if r is not None else "")
        except Exception:  # noqa: BLE001
            detail = ""
        return {"ok": False,
                "error": f"{type(e).__name__}: {e}" + (f" | {detail}" if detail else "")}
    if write_env:
        try:
            text = GW_ENV.read_text(encoding="utf-8") if GW_ENV.exists() else ""
            if re.search(r"^\s*ZOHO_TOKEN=", text, re.M):
                text = re.sub(r"^\s*ZOHO_TOKEN=.*$", f"ZOHO_TOKEN={new_token}",
                              text, flags=re.M)
            else:
                text = text.rstrip() + f"\nZOHO_TOKEN={new_token}\n"
            GW_ENV.write_text(text, encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            return {"ok": True, "access_token": new_token,
                    "warning": f"token refreshed but gateway .env write failed: {e}"}
    import os as _os
    _os.environ["ZOHO_TOKEN"] = new_token
    return {"ok": True, "access_token": new_token, "expires_in": resp.get("expires_in")}


def _access_token() -> str:
    import os
    tok = (os.getenv("ZOHO_TOKEN") or "").strip().strip('"').strip("'")
    if tok:
        return tok
    res = refresh(write_env=True)
    if res.get("ok"):
        return str(res.get("access_token") or "")
    raise _MissingKey("ZOHO_TOKEN")


_ACCT_CACHE: dict[str, str] = {}


def _resolve_account(token: str) -> tuple[str, str]:
    """Return (accountId, emailAddress). ZOHO_ACCOUNT_ID pins it; else ZOHO_USER
    selects by address; else the first account. Cached per process."""
    import os
    pinned = (os.getenv("ZOHO_ACCOUNT_ID") or "").strip().strip('"').strip("'")
    want = (os.getenv("ZOHO_USER") or "").strip().strip('"').strip("'").lower()
    if pinned and pinned in _ACCT_CACHE:
        return pinned, _ACCT_CACHE[pinned]
    with pooled_http(20.0) as client:
        r = client.get(f"{_mail_base()}/api/accounts",
                       headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 401:
            res = refresh(write_env=True)
            if not res.get("ok"):
                raise PermissionError("zoho token expired; refresh failed")
            token = str(res["access_token"])
            r = client.get(f"{_mail_base()}/api/accounts",
                           headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        data = r.json().get("data") or []
    if not data:
        raise RuntimeError("no Zoho Mail account on this token")
    chosen = None
    if want:
        for a in data:
            if want in (str(a.get("emailAddress", "")).lower(),
                        str(a.get("primaryEmailAddress", "")).lower()):
                chosen = a
                break
    if chosen is None and pinned:
        for a in data:
            if str(a.get("accountId")) == pinned:
                chosen = a
                break
    if chosen is None:
        chosen = data[0]
    acct = str(chosen.get("accountId"))
    email = str(chosen.get("primaryEmailAddress")
                or chosen.get("emailAddress") or "")
    _ACCT_CACHE[acct] = email
    return acct, email


def _upload_attachment(client, account_id: str, token: str,
                       name: str, data: bytes) -> str | None:
    """Upload one attachment; return its Zoho attachmentName (or None)."""
    import mimetypes
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    try:
        r = client.post(
            f"{_mail_base()}/api/accounts/{account_id}/messages/attachments",
            headers={"Authorization": f"Bearer {token}"},
            files={"attachment": (name, data, ctype)})
        if r.status_code >= 400:
            return None
        d = r.json()
        data_obj = d.get("data")
        if isinstance(data_obj, dict):
            return str(data_obj.get("attachmentName") or name) or None
        if isinstance(data_obj, str) and data_obj:
            return data_obj
        return name
    except Exception:  # noqa: BLE001
        return None


def send_email(*, to: str, subject: str, body: str,
               thread_id: str | None = None, attachment_b64: str = "",
               attachment_name: str = "",
               attachments: list[dict] | None = None,
               mail_format: str = "html") -> dict[str, Any]:
    """Send an email via Zoho Mail REST (POST /messages, JSON body).
    Attachments of any type are uploaded first, then referenced by name."""
    import base64 as _b64
    try:
        token = _access_token()
    except _MissingKey:
        return _fail("ZOHO_TOKEN not set (run zoho_mail_oauth_setup.py)")
    atts: list[dict] = list(attachments or [])
    if attachment_b64 and not atts:
        atts = [{"name": attachment_name or "attachment", "data_b64": attachment_b64}]
    try:
        account_id, from_addr = _resolve_account(token)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"could not resolve Zoho account: {type(e).__name__}: {e}"}
    fmt = "plaintext" if str(mail_format).lower() in ("text", "plaintext", "plain") else "html"
    payload: dict[str, Any] = {
        "fromAddress": from_addr, "toAddress": to, "subject": subject,
        "content": body, "mailFormat": fmt,
    }
    attached_names: list[str] = []
    with pooled_http(60.0) as client:
        for att in atts[:10]:
            if not isinstance(att, dict):
                continue
            data_b64 = str(att.get("data_b64") or "")
            if not data_b64:
                continue
            try:
                raw = _b64.b64decode(data_b64)
            except Exception:  # noqa: BLE001
                continue
            nm = (str(att.get("name") or "") or "attachment").strip()
            an = _upload_attachment(client, account_id, token, nm, raw)
            if an:
                attached_names.append(an)
        if attached_names:
            payload["attachments"] = ",".join(attached_names)
        try:
            r = client.post(
                f"{_mail_base()}/api/accounts/{account_id}/messages",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json=payload)
            if r.status_code == 401:
                res = refresh(write_env=True)
                if not res.get("ok"):
                    return {"ok": False, "error": f"ZOHO_TOKEN expired; refresh failed: {res.get('error')}"}
                token = str(res["access_token"])
                r = client.post(
                    f"{_mail_base()}/api/accounts/{account_id}/messages",
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json"}, json=payload)
            if r.status_code >= 400:
                return {"ok": False, "error": f"send failed ({r.status_code}): {r.text[:200]}"}
            data = r.json()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    msg_id = ""
    try:
        d = data.get("data")
        if isinstance(d, dict):
            msg_id = str(d.get("messageId") or "")
        elif isinstance(d, list) and d:
            msg_id = str(d[0].get("messageId") or "")
    except Exception:  # noqa: BLE001
        msg_id = ""
    return {"ok": True, "to": to, "subject": subject, "id": msg_id,
            "from": from_addr, "thread_id": thread_id,
            "attached": bool(attached_names), "attachment_count": len(attached_names)}


def reply(*, message_id: str, body: str, to: str = "", subject: str = "") -> dict[str, Any]:
    """Reply to a message. If `to`/`subject` are not given, they are read from
    the original (a "Re:" subject is added). Uses messages.CREATE."""
    import base64 as _b64  # noqa: F401
    try:
        token = _access_token()
    except _MissingKey:
        return _fail("ZOHO_TOKEN not set (run zoho_mail_oauth_setup.py)")
    try:
        account_id, from_addr = _resolve_account(token)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"could not resolve Zoho account: {type(e).__name__}: {e}"}
    if not to or not subject:
        try:
            with pooled_http(25.0) as c:
                r = c.get(f"{_mail_base()}/api/accounts/{account_id}/messages/view",
                          headers={"Authorization": f"Bearer {token}"},
                          params={"includeDetails": "true", "limit": 100})
                data = r.json().get("data") or []
            for m in data:
                if str(m.get("messageId") or "") == str(message_id):
                    to = to or (m.get("fromAddress") or "")
                    subj = m.get("subject") or ""
                    subject = subject or (subj if subj.lower().startswith("re:") else f"Re: {subj}")
                    break
        except Exception:  # noqa: BLE001
            pass
    payload = {"fromAddress": from_addr, "toAddress": to, "subject": subject,
               "content": body, "mailFormat": "html", "inReplyTo": str(message_id),
               "action": "reply"}
    try:
        with pooled_http(30.0) as c:
            r = c.post(f"{_mail_base()}/api/accounts/{account_id}/messages/{message_id}",
                       headers={"Authorization": f"Bearer {token}",
                                "Content-Type": "application/json"}, json=payload)
            if r.status_code == 401:
                res = refresh(write_env=True)
                if not res.get("ok"):
                    return {"ok": False, "error": "ZOHO_TOKEN expired; refresh failed"}
                token = str(res["access_token"])
                r = c.post(f"{_mail_base()}/api/accounts/{account_id}/messages/{message_id}",
                           headers={"Authorization": f"Bearer {token}",
                                    "Content-Type": "application/json"}, json=payload)
            if r.status_code >= 400:
                return {"ok": False, "error": f"reply failed ({r.status_code}): {r.text[:200]}"}
            data = r.json()
        mid = ""
        try:
            d = data.get("data")
            if isinstance(d, dict):
                mid = str(d.get("messageId") or "")
        except Exception:  # noqa: BLE001
            mid = ""
        return {"ok": True, "to": to, "subject": subject, "id": mid,
                "in_reply_to": str(message_id)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _update_message(token: str, account_id: str, payload: dict):
    """PUT /updatemessage (mark/move/delete). Returns (data, error)."""
    with pooled_http(25.0) as c:
        r = c.put(f"{_mail_base()}/api/accounts/{account_id}/updatemessage",
                  headers={"Authorization": f"Bearer {token}",
                           "Content-Type": "application/json"}, json=payload)
        if r.status_code == 401:
            res = refresh(write_env=True)
            if not res.get("ok"):
                return None, "ZOHO_TOKEN expired; refresh failed"
            token = str(res["access_token"])
            r = c.put(f"{_mail_base()}/api/accounts/{account_id}/updatemessage",
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json"}, json=payload)
        if r.status_code >= 400:
            return None, f"update failed ({r.status_code}): {r.text[:180]}"
        try:
            return r.json(), None
        except Exception:  # noqa: BLE001
            return {}, None


def mark(*, message_id: str, mode: str = "read") -> dict[str, Any]:
    """Mark a message read/unread. `mode` is 'read' or 'unread'. Needs the
    ZohoMail.messages.UPDATE scope."""
    try:
        token = _access_token()
        account_id, _e = _resolve_account(token)
    except _MissingKey:
        return _fail("ZOHO_TOKEN not set (run zoho_mail_oauth_setup.py)")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"could not resolve account: {e}"}
    mode = "unread" if str(mode).lower() == "unread" else "read"
    _d, err = _update_message(token, account_id,
                              {"mode": mode, "messageId": [str(message_id)]})
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "message_id": str(message_id), "mode": mode}


def move(*, message_id: str, dest_folder_id: str) -> dict[str, Any]:
    """Move a message to a folder (dest_folder_id from list_folders). Needs
    ZohoMail.messages.UPDATE."""
    try:
        token = _access_token()
        account_id, _e = _resolve_account(token)
    except _MissingKey:
        return _fail("ZOHO_TOKEN not set (run zoho_mail_oauth_setup.py)")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"could not resolve account: {e}"}
    _d, err = _update_message(token, account_id,
                              {"mode": "move", "messageId": [str(message_id)],
                               "destfolderId": str(dest_folder_id)})
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "message_id": str(message_id), "to_folder": str(dest_folder_id)}


def delete(*, message_id: str) -> dict[str, Any]:
    """Move a message to Trash (recoverable). Needs ZohoMail.messages.UPDATE."""
    try:
        token = _access_token()
        account_id, _e = _resolve_account(token)
    except _MissingKey:
        return _fail("ZOHO_TOKEN not set (run zoho_mail_oauth_setup.py)")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"could not resolve account: {e}"}
    _d, err = _update_message(token, account_id,
                              {"mode": "delete", "messageId": [str(message_id)]})
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "message_id": str(message_id), "moved_to": "Trash"}


def _msg_summary(m: dict) -> dict:
    return {"id": str(m.get("messageId") or ""),
            "from": m.get("fromAddress") or "",
            "subject": m.get("subject") or "",
            "snippet": m.get("summary") or m.get("snippet") or "",
            "received": m.get("receivedTime") or "",
            "status": m.get("status") or ""}


def query(*, api_method: str, query: str = "", max_results: int = 5) -> dict[str, Any]:
    """api_method 'list' -> message summaries (GET /messages/view);
    'read' -> one message (found in the view by id)."""
    try:
        token = _access_token()
    except _MissingKey:
        return _fail("ZOHO_TOKEN not set (run zoho_mail_oauth_setup.py)")
    try:
        account_id, _email = _resolve_account(token)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"could not resolve Zoho account: {type(e).__name__}: {e}"}
    with pooled_http(30.0) as client:
        try:
            _lim = max(1, min(int(max_results or 5), 200))
            if api_method == "read":
                _lim = 200  # search a wide window to find a by-id read
            params = {"limit": _lim, "start": 1, "status": "all"}
            if api_method == "list" and str(query).lower() in ("read", "unread", "all"):
                params["status"] = str(query).lower()
            r = client.get(f"{_mail_base()}/api/accounts/{account_id}/messages/view",
                           headers={"Authorization": f"Bearer {token}"}, params=params)
            if r.status_code == 401:
                res = refresh(write_env=True)
                if not res.get("ok"):
                    return {"ok": False, "error": "ZOHO_TOKEN expired; refresh failed"}
                token = str(res["access_token"])
                r = client.get(f"{_mail_base()}/api/accounts/{account_id}/messages/view",
                               headers={"Authorization": f"Bearer {token}"}, params=params)
            r.raise_for_status()
            data = r.json().get("data") or []
            if api_method == "list":
                out = [_msg_summary(m) for m in data[:max_results]]
                return {"ok": True, "message_ids": [m["id"] for m in out], "messages": out}
            if api_method == "read":
                chosen = None
                for m in data:
                    if str(m.get("messageId") or "") == query:
                        chosen = m
                        break
                if chosen is None:
                    return {"ok": False, "error": f"message {query} not found "
                            f"(not in the most recent {_lim})"}
                s = _msg_summary(chosen)
                s["content"] = (chosen.get("content") or chosen.get("summary")
                              or chosen.get("snippet") or "")[:8000]
                return {"ok": True, **s}
            return {"ok": False, "error": f"unknown api_method '{api_method}'"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def profile() -> dict[str, Any]:
    """The connected mailbox: email address + account id + folder count."""
    try:
        token = _access_token()
    except _MissingKey:
        return _fail("ZOHO_TOKEN not set (run zoho_mail_oauth_setup.py)")
    try:
        account_id, email = _resolve_account(token)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"could not resolve Zoho account: {type(e).__name__}: {e}"}
    folders = 0
    try:
        with pooled_http(20.0) as client:
            r = client.get(f"{_mail_base()}/api/accounts/{account_id}/folders",
                           headers={"Authorization": f"Bearer {token}"})
            if r.status_code < 400:
                folders = len(r.json().get("data") or [])
    except Exception:  # noqa: BLE001
        folders = 0
    return {"ok": True, "email": email, "account_id": account_id,
            "folders_total": folders, "dc": _dc()}


def list_folders() -> dict[str, Any]:
    """The mailbox's folders (Inbox, Sent, Drafts, custom)."""
    try:
        token = _access_token()
        account_id, _email = _resolve_account(token)
        with pooled_http(20.0) as client:
            r = client.get(f"{_mail_base()}/api/accounts/{account_id}/folders",
                           headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            data = r.json().get("data") or []
        return {"ok": True, "folders": [
            {"id": str(f.get("folderId") or ""), "name": f.get("folderName") or ""}
            for f in data]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}