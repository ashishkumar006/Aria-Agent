"""
One-time Gmail OAuth setup helper (no Google SDK needed; uses httpx).

Lives on the GATEWAY: Gmail credentials belong to llm_gatewayV9/.env
(the agent never holds them). Run from this directory:
    cd llm_gatewayV9 && uv run python gmail_oauth_setup.py
On success it writes GMAIL_TOKEN + GMAIL_REFRESH_TOKEN into the gateway
.env automatically.

Two ways to complete the flow (whichever works for you):
  A) Auto: the script starts a local server on http://localhost:8080, opens the
     consent URL, you click Allow, Google redirects back, code is captured.
  B) Manual (if localhost redirect is blocked): after clicking Allow, Google
     shows a page that may not reach localhost. Copy the `code` value from the
     browser address bar and write it to gmail_code.txt (just the code, no URL),
     then save. The script picks it up.

Scopes requested: gmail.send + gmail.readonly (so send_email AND gmail_query
both work from the same token). On success it writes GMAIL_TOKEN +
GMAIL_REFRESH_TOKEN into .env automatically.
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

SCOPES = "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly"
REDIRECT_URI = "http://localhost:8080"
ENV_PATH = Path(__file__).resolve().parent / ".env"
CODE_FILE = Path(__file__).resolve().parent / "gmail_code.txt"
TIMEOUT = 300


def _read_env() -> str:
    return ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""


def _write_tokens(access_token: str, refresh_token: str) -> None:
    text = _read_env()
    pairs = {"GMAIL_TOKEN": access_token, "GMAIL_REFRESH_TOKEN": refresh_token}
    for key, val in pairs.items():
        if re.search(rf"^\s*{key}=", text, re.M):
            text = re.sub(rf"^\s*{key}=.*$", f"{key}={val}", text, flags=re.M)
        else:
            text = text.rstrip() + f"\n{key}={val}\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def main() -> None:
    text = _read_env()
    cid = re.search(r"^\s*GMAIL_CLIENT_ID=(.*)$", text, re.M)
    csec = re.search(r"^\s*GMAIL_CLIENT_SECRET=(.*)$", text, re.M)
    client_id = cid.group(1).strip().strip('"').strip("'") if cid else ""
    client_secret = csec.group(1).strip().strip('"').strip("'") if csec else ""
    if not (client_id and client_secret):
        raise SystemExit("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env first.")

    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "scope": SCOPES,
                "access_type": "offline",
                "prompt": "consent",
            }
        )
    )

    got: dict[str, str] = {}
    if CODE_FILE.exists():
        CODE_FILE.unlink()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(q)
            if "code" in params:
                got["code"] = params["code"][0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Gmail OAuth done - you can close this tab.")
            elif "error" in params:
                # User clicked Deny (or Google refused) — fail fast with the
                # reason instead of spinning until the 5-minute timeout.
                got["error"] = params["error"][0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Gmail OAuth denied - see the terminal.")
            else:
                # Favicon / stray hits carry neither code nor error: 404 them
                # WITHOUT consuming the one-shot handler (see serve loop).
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):  # silence
            pass

    srv = HTTPServer(("localhost", 8080), _Handler)
    # Serve until we have a verdict: handle_request() serves exactly ONE
    # request, so loop it — a browser favicon hit must not consume the
    # single slot meant for the OAuth redirect.
    srv.timeout = 1

    def _serve_until_verdict():
        while "code" not in got and "error" not in got:
            srv.handle_request()

    threading.Thread(target=_serve_until_verdict, daemon=True).start()

    print("\n=== Gmail OAuth ===")
    print("Open this URL and click Allow:\n")
    print(auth_url + "\n")
    try:
        webbrowser.open(auth_url)
        print("(Browser opened automatically. If it didn't, copy the URL above.)\n")
    except Exception:
        pass

    print("Waiting for authorization (up to 5 min)...")
    print(" - If the browser redirects to localhost, it's automatic.")
    print(" - If localhost is blocked, copy the `code` from the address bar")
    print("   into gmail_code.txt and save it.\n")

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
        raise SystemExit(f"Authorization denied by Google/user: {got['error']}")
    if "code" not in got:
        raise SystemExit("No authorization code received (timed out).")

    with httpx.Client(timeout=20, follow_redirects=True) as client:
        r = client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": got["code"],
                "grant_type": "authorization_code",
                "redirect_uri": REDIRECT_URI,
            },
        )
        if r.status_code != 200:
            raise SystemExit(f"Token exchange failed ({r.status_code}): {r.text}")
        resp = r.json()

    access = resp.get("access_token")
    refresh = resp.get("refresh_token", "")
    if not access:
        raise SystemExit(f"No access_token in response: {resp}")
    _write_tokens(access, refresh)
    print("\nWrote GMAIL_TOKEN" + (" + GMAIL_REFRESH_TOKEN" if refresh else "") + " to llm_gatewayV9/.env.")
    print("send_email + gmail_query are now ready. Token expires ~1h;")
    print("run gmail_refresh_token() (MCP tool, renews via the gateway) to renew.")


if __name__ == "__main__":
    main()
