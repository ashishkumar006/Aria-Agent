"""Google Calendar integration (REST + OAuth2).

Keys (gateway .env): GOOGLE_CALENDAR_TOKEN (OAuth access token, ~1h) +
GOOGLE_CALENDAR_REFRESH_TOKEN / GOOGLE_CALENDAR_CLIENT_ID /
GOOGLE_CALENDAR_CLIENT_SECRET for refresh. Run calendar_oauth_setup.py once
to mint the pair. refresh(write_env=True) rewrites GOOGLE_CALENDAR_TOKEN in
the GATEWAY .env (never the agent's).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx

from . import _MissingKey, _fail, _need

GW_ENV = Path(__file__).resolve().parent.parent / ".env"
CALENDAR_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


def _get_events(token: str, params: dict):
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        return client.get(CALENDAR_URL, params=params,
                          headers={"Authorization": f"Bearer {token}"})


def _post_event(token: str, event: dict):
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        return client.post(CALENDAR_URL, json=event,
                           headers={"Authorization": f"Bearer {token}"})


def list_events(*, time_min: str = "", time_max: str = "",
                max_results: int = 10) -> dict[str, Any]:
    """List upcoming events (read-only). `time_min`/`time_max` are RFC3339
    datetimes; defaults to now → +7 days. Mirrors create_event's token +
    401-refresh discipline."""
    import datetime as _dt
    try:
        token = _need("GOOGLE_CALENDAR_TOKEN")
    except _MissingKey:
        return _fail("GOOGLE_CALENDAR_TOKEN not set (run calendar_oauth_setup.py)")
    now = _dt.datetime.now(_dt.timezone.utc)
    params = {
        "timeMin": time_min or now.isoformat(),
        "timeMax": time_max or (now + _dt.timedelta(days=7)).isoformat(),
        "maxResults": max(1, min(int(max_results or 10), 50)),
        "singleEvents": True,
        "orderBy": "startTime",
    }
    try:
        r = _get_events(token, params)
        if r.status_code == 401:
            res = refresh(write_env=True)
            if not res.get("ok"):
                return {"ok": False,
                        "error": f"GOOGLE_CALENDAR_TOKEN expired; refresh failed: {res.get('error')}"}
            r = _get_events(res["access_token"], params)
        r.raise_for_status()
        items = (r.json().get("items") or [])[:params["maxResults"]]
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "events": [
        {"id": e.get("id", ""), "summary": e.get("summary", ""),
         "start": ((e.get("start") or {}).get("dateTime")
                   or (e.get("start") or {}).get("date", "")),
         "end": ((e.get("end") or {}).get("dateTime")
                 or (e.get("end") or {}).get("date", "")),
         "location": e.get("location", ""),
         "link": e.get("htmlLink", "")}
        for e in items]}


def create_event(*, summary: str, start: str, end: str, description: str = "",
                 location: str = "", timezone: str = "UTC") -> dict[str, Any]:
    try:
        token = _need("GOOGLE_CALENDAR_TOKEN")
    except _MissingKey:
        return _fail("GOOGLE_CALENDAR_TOKEN not set (run calendar_oauth_setup.py)")
    event = {
        "summary": summary,
        "description": description,
        "location": location,
        "start": {"dateTime": start, "timeZone": timezone},
        "end": {"dateTime": end, "timeZone": timezone},
    }
    try:
        r = _post_event(token, event)
        if r.status_code == 401:
            # Hourly expiry: renew once from the refresh triple and retry.
            res = refresh(write_env=True)
            if not res.get("ok"):
                return {"ok": False,
                        "error": f"GOOGLE_CALENDAR_TOKEN expired; refresh failed: {res.get('error')}"}
            r = _post_event(res["access_token"], event)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "id": data.get("id"), "htmlLink": data.get("htmlLink")}


def refresh(*, write_env: bool = True) -> dict[str, Any]:
    import os
    vals = {k: (os.getenv(k) or "").strip().strip('"').strip("'")
            for k in ("GOOGLE_CALENDAR_REFRESH_TOKEN", "GOOGLE_CALENDAR_CLIENT_ID",
                      "GOOGLE_CALENDAR_CLIENT_SECRET")}
    if not all(vals.values()):
        return {"ok": False,
                "error": "GOOGLE_CALENDAR_REFRESH_TOKEN / GOOGLE_CALENDAR_CLIENT_ID / "
                         "GOOGLE_CALENDAR_CLIENT_SECRET not set"}
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            r = client.post("https://oauth2.googleapis.com/token", data={
                "client_id": vals["GOOGLE_CALENDAR_CLIENT_ID"],
                "client_secret": vals["GOOGLE_CALENDAR_CLIENT_SECRET"],
                "refresh_token": vals["GOOGLE_CALENDAR_REFRESH_TOKEN"],
                "grant_type": "refresh_token",
            })
            r.raise_for_status()
            resp = r.json()
        new_token = resp.get("access_token")
        if not new_token:
            return {"ok": False, "error": f"no access_token in response: {resp}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if write_env:
        try:
            text = GW_ENV.read_text(encoding="utf-8") if GW_ENV.exists() else ""
            if re.search(r"^\s*GOOGLE_CALENDAR_TOKEN=", text, re.M):
                text = re.sub(r"^\s*GOOGLE_CALENDAR_TOKEN=.*$",
                              f"GOOGLE_CALENDAR_TOKEN={new_token}", text, flags=re.M)
            else:
                text = text.rstrip() + f"\nGOOGLE_CALENDAR_TOKEN={new_token}\n"
            GW_ENV.write_text(text, encoding="utf-8")
        except Exception as e:
            return {"ok": True, "access_token": new_token,
                    "warning": f"token refreshed but gateway .env write failed: {e}"}
    # The running process reads process env (loaded at boot), NOT the file —
    # without this the refreshed token only takes effect after a restart.
    import os as _os
    _os.environ["GOOGLE_CALENDAR_TOKEN"] = new_token
    return {"ok": True, "access_token": new_token, "expires_in": resp.get("expires_in")}
