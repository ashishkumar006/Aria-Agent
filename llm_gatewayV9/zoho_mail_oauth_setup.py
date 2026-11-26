"""
One-time Zoho Mail OAuth setup helper (no Zoho SDK needed; uses httpx).

Lives on the GATEWAY: Zoho Mail credentials belong to llm_gatewayV9/.env (the
agent never holds them). Run from this directory:
    cd llm_gatewayV9 && uv run python zoho_mail_oauth_setup.py

Prereq: a Zoho API Console "Server-based" OAuth client (https://api-console.zoho.com)
with a redirect URI of http://localhost:8080 and the scopes:
    ZohoMail.accounts.READ  ZohoMail.messages.READ  ZohoMail.messages.CREATE  ZohoMail.folders.READ  ZohoCalendar.calendar.READ  ZohoCalendar.event.CREATE  ZohoCalendar.event.READ
Put its client id/secret in .env first:
    ZOHO_CLIENT_ID=...
    ZOHO_CLIENT_SECRET=...
    ZOHO_DC=com        # your data centre: com | eu | in | com.au | jp | ca

On success it writes ZOHO_TOKEN + ZOHO_REFRESH_TOKEN into the gateway .env.

Two ways to complete the flow (whichever works for you):
  A) Auto: the script starts a local server on http://localhost:8080, opens the
     consent URL, you click Accept, Zoho redirects back, the code is captured.
  B) Manual (if localhost redirect is blocked): after Accept, copy the `code`
     value from the browser address bar into zoho_code.txt (just the code),
     then save. The script picks it up.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

REDIRECT_URI = "http://localhost:8080"
ENV_PATH = Path(__file__).resolve().parent / ".env"
CODE_FILE = Path(__file__).resolve().parent / "zoho_code.txt"
TIMEOUT = 300

_DC_RE = re.compile(r"^[a-z]{2,3}(\.[a-z]{2})?$")


def _read_env() -> str:
    return ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""


def _env_val(text: str, key: str) -> str:
    m = re.search(rf"^\s*{key}=(.*)$", text, re.M)
    return m.group(1).strip().strip('"').strip("'") if m else ""


def _write_tokens(access_token: str, refresh_token: str) -> None:
    text = _read_env()
    pairs = {"ZOHO_TOKEN": access_token, "ZOHO_REFRESH_TOKEN": refresh_token}
    for key, val in pairs.items():
        if re.search(rf"^\s*{key}=", text, re.M):
            text = re.sub(rf"^\s*{key}=.*$", f"{key}={val}", text, flags=re.M)
        else:
            text = text.rstrip() + f"\n{key}={val}\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def main() -> None:
    text = _read_env()
    client_id = _env_val(text, "ZOHO_CLIENT_ID")
    client_secret = _env_val(text, "ZOHO_CLIENT_SECRET")
    if not (client_id and client_secret):
        raise SystemExit("Set ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET in .env first.")
    dc = _env_val(text, "ZOHO_DC") or "com"
    if not _DC_RE.match(dc):
        dc = "com"
    accounts = f"https://accounts.zoho.{dc}"
    scope = ("ZohoMail.accounts.READ,ZohoMail.messages.READ,"
             "ZohoMail.messages.CREATE,ZohoMail.folders.READ,"
             "ZohoMail.messages.UPDATE,"
             "ZohoCalendar.calendar.READ,ZohoCalendar.event.CREATE,"
             "ZohoCalendar.event.READ")

    auth_url = (f"{accounts}/oauth/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "scope": scope,
        "redirect_uri": REDIRECT_URI,
        "access_type": "offline",
        "prompt": "consent",
    }))

    got: dict[str, str] = {}
    if CODE_FILE.exists():
        CODE_FILE.unlink()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in params:
                got["code"] = params["code"][0]
                self.send_response(200); self.end_headers()
                self.wfile.write(b"Zoho OAuth done - you can close this tab.")
            elif "error" in params:
                got["error"] = params["error"][0]
                self.send_response(200); self.end_headers()
                self.wfile.write(b"Zoho OAuth denied - see the terminal.")
            else:
                self.send_response(404); self.end_headers()

        def log_message(self, *a):  # silence
            pass

    srv = HTTPServer(("localhost", 8080), _Handler)
    srv.timeout = 1

    def _serve_until_verdict():
        while "code" not in got and "error" not in got:
            srv.handle_request()

    threading.Thread(target=_serve_until_verdict, daemon=True).start()

    print("\n=== Zoho Mail OAuth ===")
    print("Open this URL and click Accept:\n")
    print(auth_url + "\n")
    try:
        webbrowser.open(auth_url)
        print("(Browser opened automatically. If it didn't, copy the URL above.)\n")
    except Exception:
        pass
    print("Waiting for authorization (up to 5 min)...")
    print(" - If the browser redirects to localhost, it's automatic.")
    print(" - If localhost is blocked, copy the `code` from the address bar")
    print("   into zoho_code.txt and save it.\n")

    deadline = time.time() + TIMEOUT
    while time.time() < deadline and "code" not in got and "error" not in got:
        if CODE_FILE.exists():
            raw = CODE_FILE.read_text(encoding="utf-8").strip()
            m = re.search(r"[?&]code=([^&\s]+)", raw) or re.search(r"^([A-Za-z0-9_\-]{20,})$", raw)
            if m:
                got["code"] = m.group(1)
            CODE_FILE.unlink(missing_ok=True)
        if "code" not in got:
            time.sleep(1)

    if "error" in got:
        raise SystemExit(f"Authorization denied: {got['error']}")
    if "code" not in got:
        raise SystemExit("No authorization code received (timed out).")

    with httpx.Client(timeout=20, follow_redirects=True) as client:
        r = client.post(f"{accounts}/oauth/v2/token", data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": got["code"],
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        })
        if r.status_code != 200:
            raise SystemExit(f"Token exchange failed ({r.status_code}): {r.text}")
        resp = r.json()

    access = resp.get("access_token")
    refresh = resp.get("refresh_token", "")
    if not access:
        raise SystemExit(f"No access_token in response: {resp}")
    _write_tokens(access, refresh)
    print("\nWrote ZOHO_TOKEN" + (" + ZOHO_REFRESH_TOKEN" if refresh else "") + " to llm_gatewayV9/.env.")
    print("zoho_mail_send + zoho_mail_query are now ready. Access token expires ~1h;")
    print("run zoho_mail_refresh_token() (MCP tool) or this script again to renew.")


if __name__ == "__main__":
    main()