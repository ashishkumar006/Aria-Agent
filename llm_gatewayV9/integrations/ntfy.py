"""ntfy integration — free phone push (hosted ntfy.sh or self-hosted).

Keyless by default (hosted ntfy.sh, 250 msgs/day per IP). Optional:
  NTFY_URL   -> self-hosted base (default https://ntfy.sh)
  NTFY_TOKEN -> reserved-topic auth token (optional)
Outbound only: one HTTP POST to <base>/<topic> pushes to every subscriber.
"""
from __future__ import annotations

import json
import os
from typing import Any

from adaptors.http import shared as pooled_http

from . import _fail


def _base() -> str:
    return ((os.getenv("NTFY_URL") or "https://ntfy.sh").strip().rstrip("/")
            or "https://ntfy.sh")


def send(*, topic: str, message: str, title: str = "", priority: str = "",
         tags: str = "", actions: str = "") -> dict[str, Any]:
    topic = (topic or "").strip().strip("/")
    if not topic:
        return {"ok": False, "error": "topic is required"}
    headers: dict[str, str] = {}
    tok = (os.getenv("NTFY_TOKEN") or "").strip().strip('"').strip("'")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    if title:
        headers["Title"] = title[:64]
    if priority:
        headers["Priority"] = priority[:16]
    if tags:
        headers["Tags"] = tags[:32]
    if actions:
        headers["Actions"] = actions[:200]
    try:
        with pooled_http(20.0) as client:
            r = client.post(f"{_base()}/{topic}",
                            content=(message or "").encode("utf-8"), headers=headers)
            if r.status_code >= 400:
                return {"ok": False, "error": f"ntfy {r.status_code}"}
            data: dict = {}
            try:
                data = r.json()
            except Exception:  # noqa: BLE001
                data = {}
        return {"ok": True, "topic": topic, "id": data.get("id")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def history(*, topic: str, since: str = "", max_results: int = 20) -> dict[str, Any]:
    """Read recent messages posted to a topic (poll mode). `since` can be a
    message id, a duration (e.g. '10m', '1h'), or a timestamp; empty = latest."""
    topic = (topic or "").strip().strip("/")
    if not topic:
        return {"ok": False, "error": "topic is required"}
    headers: dict[str, str] = {}
    tok = (os.getenv("NTFY_TOKEN") or "").strip().strip('"').strip("'")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    url = f"{_base()}/{topic}/json?poll=1"
    if since:
        url += "&since=" + since
    try:
        with pooled_http(20.0) as client:
            r = client.get(url, headers=headers)
            if r.status_code >= 400:
                return {"ok": False, "error": f"ntfy {r.status_code}"}
            text = r.text or ""
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            out.append({"id": d.get("id"), "time": d.get("time"),
                        "event": d.get("event"), "title": d.get("title"),
                        "message": d.get("message")})
            if len(out) >= max_results:
                break
        return {"ok": True, "topic": topic, "messages": out}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}