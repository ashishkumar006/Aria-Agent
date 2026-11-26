"""Zoho Calendar integration (REST + OAuth2). Shares the Zoho OAuth token with
integrations.zoho_mail (same consent, same gateway .env).

Keys: same as Zoho Mail (ZOHO_TOKEN / ZOHO_REFRESH_TOKEN / ZOHO_CLIENT_ID /
ZOHO_CLIENT_SECRET / ZOHO_DC). The consent must include the ZohoCalendar.*
scopes (calendar.READ, event.CREATE, event.READ).

Endpoints (Zoho Calendar REST v1):
  GET  /api/v1/calendars                         -> list calendars
  GET  /api/v1/calendars/{id}/events             -> list events
  POST /api/v1/calendars/{id}/events             -> create event (eventdata)
"""
from __future__ import annotations

import json
import time
from typing import Any

from adaptors.http import shared as pooled_http

from . import _fail
from .zoho_mail import _access_token, _dc


def _base() -> str:
    return f"https://calendar.zoho.{_dc()}/api/v1"


def _to_ms(value: Any) -> int:
    """Accept epoch ms (int/str) or an ISO-8601 string; return epoch ms."""
    s = str(value or "").strip()
    if s.isdigit():
        return int(s)
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:  # noqa: BLE001
        return int(time.time() * 1000)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def list_calendars() -> dict[str, Any]:
    try:
        token = _access_token()
    except Exception as e:  # noqa: BLE001
        return _fail(f"ZOHO_TOKEN not set ({e})")
    try:
        with pooled_http(20.0) as c:
            r = c.get(f"{_base()}/calendars", headers=_headers(token))
            if r.status_code == 401:
                return {"ok": False, "error": "calendar scope missing — re-consent with ZohoCalendar scopes"}
            r.raise_for_status()
            j = r.json()
            data = j.get("calendars") or j.get("data") or []
        return {"ok": True, "calendars": [
            {"id": str(x.get("id") or x.get("calendarId") or ""),
             "name": x.get("name") or x.get("calendarName") or ""}
            for x in data]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def list_events(*, calendar_id: str = "primary", max_results: int = 10) -> dict[str, Any]:
    try:
        token = _access_token()
    except Exception as e:  # noqa: BLE001
        return _fail(f"ZOHO_TOKEN not set ({e})")
    cid = (calendar_id or "primary").strip() or "primary"
    try:
        with pooled_http(25.0) as c:
            r = c.get(f"{_base()}/calendars/{cid}/events", headers=_headers(token))
            if r.status_code == 401:
                return {"ok": False, "error": "calendar scope missing — re-consent with ZohoCalendar scopes"}
            r.raise_for_status()
            j = r.json()
            data = j.get("events") or j.get("data") or []
        out = []
        for x in data:
            if not isinstance(x, dict) or ("title" not in x and "eventId" not in x):
                continue  # status rows like {"message": "No events modified."}
            out.append({"id": str(x.get("eventId") or x.get("id") or ""),
                        "title": x.get("title") or "",
                        "start": x.get("startDate") or x.get("start") or "",
                        "end": x.get("endDate") or x.get("end") or "",
                        "all_day": bool(x.get("isAllDayEvent"))})
            if len(out) >= max_results:
                break
        return {"ok": True, "calendar": cid, "events": out}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def create_event(*, title: str, start: str, end: str, description: str = "",
                 location: str = "", calendar_id: str = "primary",
                 all_day: bool = False) -> dict[str, Any]:
    try:
        token = _access_token()
    except Exception as e:  # noqa: BLE001
        return _fail(f"ZOHO_TOKEN not set ({e})")
    cid = (calendar_id or "primary").strip() or "primary"
    start_ms = _to_ms(start)
    end_ms = _to_ms(end)
    # Zoho Calendar eventdata wants `dateandtime` (start, epoch ms) + `duration`
    # (minutes), not startDate/endDate. All-day events use a 1440-minute duration.
    event: dict[str, Any] = {
        "title": title,
        "dateandtime": start_ms,
        "isAllDayEvent": bool(all_day),
        "duration": 1440 if all_day else max(1, int((end_ms - start_ms) / 60000)),
    }
    if description:
        event["description"] = description
    if location:
        event["location"] = location
    try:
        with pooled_http(30.0) as c:
            r = c.post(f"{_base()}/calendars/{cid}/events",
                       headers=_headers(token),
                       data={"eventdata": json.dumps(event)})
            if r.status_code == 401:
                return {"ok": False, "error": "calendar scope missing — re-consent with ZohoCalendar scopes"}
            if r.status_code >= 400:
                return {"ok": False, "error": f"create failed ({r.status_code}): {r.text[:200]}"}
            data = r.json().get("data") or {}
            if isinstance(data, list) and data:
                data = data[0]
        return {"ok": True, "title": title, "calendar": cid,
                "id": str(data.get("eventId") or ""), "start": start_ms,
                "end": end_ms}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}