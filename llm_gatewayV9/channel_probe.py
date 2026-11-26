"""Live, read-only credential probe for each configured channel.

"Configured" (a token exists) is not "working" (the token is still accepted).
This probes each channel with one cheap READ call and reports ok/detail. It
never sends a message, never writes a file, and never prints a token.

Run standalone:
    cd llm_gatewayV9 && uv run python channel_probe.py
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

# Captured BEFORE secret_vault scrubs provider keys out of os.environ, because
# GITHUB_ACCESS_TOKEN is one of the scrubbed names.
CAPTURED: dict[str, str] = {}

TOKEN_ENVS: dict[str, str] = {
    "telegram": "TELEGRAM_BOT_TOKEN",
    "discord": "DISCORD_BOT_TOKEN",
    "slack": "SLACK_BOT_TOKEN",
    "gmail": "GMAIL_TOKEN",
    "calendar": "GOOGLE_CALENDAR_TOKEN",
    "github": "GITHUB_TOKEN",
    "notion": "NOTION_TOKEN",
}


def capture_tokens() -> None:
    for ch, env in TOKEN_ENVS.items():
        v = (os.environ.get(env) or "").strip()
        if v:
            CAPTURED[ch] = v


def _request(url: str, token: str, *, method: str = "GET",
             headers: dict | None = None, json_body: dict | None = None,
             scheme: str = "Bearer"):
    import httpx
    h = {"Authorization": f"{scheme} {token}"} if token else {}
    if headers:
        h.update(headers)
    with httpx.Client(timeout=8.0) as c:
        r = c.request(method, url, headers=h, json=json_body)
        return r.status_code, r.text[:200]


def _probe(ch: str) -> dict:
    tok = CAPTURED.get(ch)
    if not tok:
        return {"name": ch, "ok": True, "configured": False, "detail": "not configured"}
    try:
        if ch == "telegram":
            # Token lives in the URL path, not a header.
            code, body = _request(f"https://api.telegram.org/bot{tok}/getMe", "")
        elif ch == "discord":
            code, body = _request("https://discord.com/api/v10/users/@me", tok,
                                  headers={"User-Agent": "AriaGateway/1.0"}, scheme="Bot")
        elif ch == "slack":
            code, body = _request("https://slack.com/api/auth.test", tok, method="POST")
            try:
                j = json.loads(body)
                if not j.get("ok"):
                    return {"name": ch, "ok": False, "configured": True,
                            "detail": f"auth.test: {j.get('error')}"}
            except Exception:
                pass
        elif ch == "gmail":
            code, body = _request("https://gmail.googleapis.com/gmail/v1/users/me/profile", tok)
        elif ch == "calendar":
            code, body = _request("https://www.googleapis.com/calendar/v3/users/me/calendarList", tok)
        elif ch == "github":
            code, body = _request("https://api.github.com/user", tok,
                                  headers={"User-Agent": "AriaGateway",
                                           "Accept": "application/vnd.github+json"})
        elif ch == "notion":
            code, body = _request("https://api.notion.com/v1/search", tok, method="POST",
                                  headers={"Notion-Version": "2022-06-28"}, json_body={})
        else:
            return {"name": ch, "ok": True, "configured": True, "detail": "no probe"}
        ok = 200 <= code < 300
        return {"name": ch, "ok": ok, "configured": True,
                "detail": "ok" if ok else f"HTTP {code}: {body[:80]}"}
    except Exception as e:  # noqa: BLE001
        return {"name": ch, "ok": False, "configured": True,
                "detail": f"{type(e).__name__}: {e}"}


def probe_all() -> list[dict]:
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(_probe, TOKEN_ENVS.keys()))


def schedule_startup_report(app, log) -> None:
    """Log one line at boot: which configured channels are actually alive."""
    import asyncio

    async def _run():
        await asyncio.sleep(1.5)
        rows = await asyncio.to_thread(probe_all)
        configured = [r for r in rows if r["configured"]]
        bad = [r for r in configured if not r["ok"]]
        if bad:
            log.warning(
                "channels: %d/%d configured but UNHEALTHY: %s",
                len(bad), len(configured),
                ", ".join(f"{r['name']}({' '.join(r['detail'].split())[:70]})"
                          for r in bad))
        else:
            log.info("channels: %d/%d configured, all healthy",
                     len(configured), len(rows))

    app.state.channel_health_task = asyncio.ensure_future(_run())


def main() -> None:
    import sys
    from pathlib import Path
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except Exception:
        pass
    capture_tokens()
    rows = probe_all()
    for r in rows:
        mark = "OK     " if r["ok"] else "FAIL   "
        cfg = "configured" if r["configured"] else "not configured"
        print(f"{mark} {r['name']:12s} ({cfg}) {r['detail']}")
    bad = [r for r in rows if r["configured"] and not r["ok"]]
    print(f"\n{sum(1 for r in rows if r['configured'])}/{len(rows)} configured, {len(bad)} unhealthy")


if __name__ == "__main__":
    main()