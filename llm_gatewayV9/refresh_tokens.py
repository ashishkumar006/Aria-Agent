"""Refresh the gateway's OAuth access tokens without a browser.

Run:
    cd llm_gatewayV9 && uv run python refresh_tokens.py

Refreshes, when configured:
  - Gmail          (needs GMAIL_REFRESH_TOKEN + GMAIL_CLIENT_ID + GMAIL_CLIENT_SECRET)
  - Google Calendar(needs GOOGLE_CALENDAR_REFRESH_TOKEN + GOOGLE_CALENDAR_CLIENT_ID + GOOGLE_CALENDAR_CLIENT_SECRET)
  - Slack          (needs SLACK_REFRESH_TOKEN + SLACK_CLIENT_ID + SLACK_CLIENT_SECRET; rotation ON)

Each integration writes the new access token back to .env and prints only
status — never the token. First-time setup is a browser flow:
`gmail_oauth_setup.py` / `calendar_oauth_setup.py`.
"""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")


def _report(name: str, fn) -> None:
    try:
        r = fn()
    except Exception as e:  # noqa: BLE001
        detail = ""
        resp = getattr(e, "response", None)
        if resp is not None:
            try:
                detail = resp.text[:200]
            except Exception:
                detail = ""
        print(f"  FAIL {name}: {type(e).__name__}: {e}"
              + (f" | {detail}" if detail else ""))
        return
    ok = r.get("ok") if isinstance(r, dict) else bool(r)
    if ok:
        print(f"  OK   {name}")
    else:
        err = (r.get("error") if isinstance(r, dict) else "") or "not configured"
        print(f"  --   {name}: {err}")


def main() -> None:
    print("Refreshing OAuth tokens (values never printed)...")
    try:
        from integrations import gmail as gmail_int
        _report("gmail", lambda: gmail_int.refresh(write_env=True))
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL gmail: import error: {e}")
    try:
        from integrations import calendar as cal_int
        _report("calendar", lambda: cal_int.refresh(write_env=True))
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL calendar: import error: {e}")
    try:
        from integrations import slack as slack_int
        _report("slack", lambda: slack_int.refresh(write_env=True))
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL slack: import error: {e}")


if __name__ == "__main__":
    main()