"""One command to make Gmail work.

Tries a silent refresh first (refresh token → new access token). If the refresh
token is dead (`invalid_grant`), it starts the browser consent flow itself:
prints the URL, listens on localhost:8080 for Google's redirect, exchanges the
code, and writes GMAIL_TOKEN + GMAIL_REFRESH_TOKEN to .env.

Run:
    cd llm_gatewayV9 && uv run python gmail_refresh.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from oauth_flows import refresh, start, status  # noqa: E402


def _account() -> None:
    try:
        from integrations import gmail as g
        p = g.profile()
        if p.get("ok"):
            print("gmail account:", p.get("email"),
                  f"({p.get('messages_total')} messages, {p.get('threads_total')} threads)")
        else:
            print("gmail account: could not read —", str(p.get("error"))[:120])
    except Exception as e:  # noqa: BLE001
        print("gmail account: profile failed —", type(e).__name__, str(e)[:120])


def main() -> int:
    r = refresh().get("gmail", {})
    if r.get("ok"):
        print("gmail: refreshed (new access token written to .env)")
        _account()
        return 0

    print("gmail refresh failed:", r.get("error"))
    print("\nStarting browser authorization (opens the consent screen)...\n")
    s = start("gmail")
    if not s.get("ok"):
        print("cannot start:", s.get("error"))
        return 1

    print("Open this URL and approve access:\n")
    print(s["url"])
    print("\nWaiting for authorization (up to 5 min)...")
    flow = s["flow_id"]
    deadline = time.time() + 300
    while time.time() < deadline:
        st = status(flow)
        if st.get("done"):
            if st.get("ok"):
                print("gmail: connected ✓ (token written to .env)")
                return 0
            print("gmail: failed:", st.get("error"))
            return 1
        time.sleep(2)
    print("timed out waiting for authorization")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())