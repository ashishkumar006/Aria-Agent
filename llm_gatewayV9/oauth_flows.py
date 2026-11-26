"""Browser OAuth for Gmail / Calendar / Zoho Mail, runnable from the gateway UI.

`start(provider)` returns a consent URL and spins a one-shot local listener on
127.0.0.1:8080 — the redirect URI registered with the provider. `status(flow_id)`
reports whether the code came back and the token was written. `refresh()` renews
access tokens without a browser.

The same logic backs `gmail_oauth_setup.py` / `calendar_oauth_setup.py` /
`zoho_mail_oauth_setup.py`; this module is the importable version so the console
can drive it.
"""
from __future__ import annotations

import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / ".env"
REDIRECT_URI = "http://localhost:8080"

_FLOWS: dict[str, dict] = {}
_LOCK = threading.Lock()

PROVIDERS: dict[str, dict] = {
    "gmail": dict(
        kind="google",
        scopes="https://www.googleapis.com/auth/gmail.send "
               "https://www.googleapis.com/auth/gmail.readonly",
        cid="GMAIL_CLIENT_ID", csec="GMAIL_CLIENT_SECRET",
        access="GMAIL_TOKEN", refresh="GMAIL_REFRESH_TOKEN"),
    "calendar": dict(
        kind="google",
        scopes="https://www.googleapis.com/auth/calendar",
        cid="GOOGLE_CALENDAR_CLIENT_ID", csec="GOOGLE_CALENDAR_CLIENT_SECRET",
        access="GOOGLE_CALENDAR_TOKEN", refresh="GOOGLE_CALENDAR_REFRESH_TOKEN"),
    "zoho_mail": dict(
        kind="zoho",
        scopes="ZohoMail.accounts.READ ZohoMail.messages.READ ZohoMail.messages.CREATE "
               "ZohoMail.folders.READ ZohoMail.messages.UPDATE "
               "ZohoCalendar.calendar.READ "
               "ZohoCalendar.event.CREATE ZohoCalendar.event.READ",
        cid="ZOHO_CLIENT_ID", csec="ZOHO_CLIENT_SECRET",
        access="ZOHO_TOKEN", refresh="ZOHO_REFRESH_TOKEN"),
}


def _urls(provider: str) -> tuple[str, str]:
    """(authorize_url, token_url) for the provider, region-aware for Zoho."""
    cfg = PROVIDERS[provider]
    if cfg["kind"] == "google":
        return ("https://accounts.google.com/o/oauth2/v2/auth",
                "https://oauth2.googleapis.com/token")
    dc = (os.getenv("ZOHO_DC") or "com").strip().strip('"').strip("'").lower() or "com"
    return (f"https://accounts.zoho.{dc}/oauth/v2/auth",
            f"https://accounts.zoho.{dc}/oauth/v2/token")


def _env() -> str:
    return ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""


def _write_tokens(provider: str, access: str, refresh: str) -> None:
    cfg = PROVIDERS[provider]
    text = _env()
    for k, v in ((cfg["access"], access), (cfg["refresh"], refresh)):
        if not v:
            continue
        if re.search(rf"^\s*{re.escape(k)}=", text, re.M):
            text = re.sub(rf"^\s*{re.escape(k)}=.*$", f"{k}={v}", text, flags=re.M)
        else:
            text = text.rstrip() + f"\n{k}={v}\n"
    ENV_PATH.write_text(text, encoding="utf-8")
    os.environ[cfg["access"]] = access
    if refresh:
        os.environ[cfg["refresh"]] = refresh


def start(provider: str) -> dict:
    cfg = PROVIDERS.get(provider)
    if not cfg:
        return {"ok": False, "error": f"unknown provider {provider!r}"}
    cid = (os.getenv(cfg["cid"]) or "").strip()
    csec = (os.getenv(cfg["csec"]) or "").strip()
    if not (cid and csec):
        return {"ok": False,
                "error": f"set {cfg['cid']} and {cfg['csec']} first"}
    with _LOCK:
        if any(f.get("done") is False for f in _FLOWS.values()):
            return {"ok": False, "error": "another OAuth flow is already running"}
    flow_id = f"{provider}-{int(time.time() * 1000)}"
    st = {"provider": provider, "done": False, "ok": False, "error": ""}
    with _LOCK:
        _FLOWS[flow_id] = st

    authorize_url, token_url = _urls(provider)
    auth_url = authorize_url + "?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": REDIRECT_URI, "response_type": "code",
        "scope": cfg["scopes"], "access_type": "offline", "prompt": "consent",
    })

    def _serve() -> None:
        got: dict[str, str] = {}

        class _H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if "code" in q:
                    got["code"] = q["code"][0]
                elif "error" in q:
                    got["error"] = q["error"][0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Authorization complete - close this tab.")
            def log_message(self, *a):  # noqa: A003
                pass

        try:
            srv = HTTPServer(("localhost", 8080), _H)
        except OSError as e:
            st["error"] = f"cannot listen on 8080: {e}"
            st["done"] = True
            return
        srv.timeout = 1
        deadline = time.time() + 300
        while time.time() < deadline and not got:
            srv.handle_request()
        srv.server_close()
        if "code" not in got:
            st["error"] = got.get("error") or "timed out waiting for authorization"
            st["done"] = True
            return
        import httpx
        try:
            with httpx.Client(timeout=20) as c:
                r = c.post(token_url, data={
                    "client_id": cid, "client_secret": csec,
                    "code": got["code"], "grant_type": "authorization_code",
                    "redirect_uri": REDIRECT_URI})
                r.raise_for_status()
                resp = r.json()
            access = resp.get("access_token")
            refresh = resp.get("refresh_token", "")
            if not access:
                st["error"] = "no access_token in response"
                st["done"] = True
                return
            _write_tokens(provider, access, refresh)
            st.update(done=True, ok=True)
        except Exception as e:  # noqa: BLE001
            st["error"] = f"{type(e).__name__}: {e}"
            st["done"] = True

    threading.Thread(target=_serve, daemon=True, name=f"oauth-{flow_id}").start()
    return {"ok": True, "flow_id": flow_id, "url": auth_url}


def status(flow_id: str) -> dict:
    with _LOCK:
        st = _FLOWS.get(flow_id)
        if st is None:
            return {"ok": False, "error": "unknown flow"}
        # Do not leak the token through the status endpoint.
        return {"ok": True, "done": st["done"], "ok": st["ok"],
                "error": st["error"]}


def refresh() -> dict:
    """Refresh Gmail + Calendar + Zoho Mail access tokens without a browser."""
    out: dict = {}
    try:
        from integrations import gmail as _g
        out["gmail"] = _g.refresh(write_env=True)
    except Exception as e:  # noqa: BLE001
        out["gmail"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    try:
        from integrations import calendar as _c
        out["calendar"] = _c.refresh(write_env=True)
    except Exception as e:  # noqa: BLE001
        out["calendar"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    try:
        from integrations import zoho_mail as _z
        out["zoho_mail"] = _z.refresh(write_env=True)
    except Exception as e:  # noqa: BLE001
        out["zoho_mail"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return out