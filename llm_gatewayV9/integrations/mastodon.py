"""Mastodon integration — free federated social (any instance).

Auth: an app token (Preferences -> Development) + the instance host.
  MASTODON_TOKEN     (required) -> the app access token
  MASTODON_INSTANCE  (required) -> e.g. mastodon.social (scheme optional)
`to` is unused for a top-level status (visibility defaults to public).
"""
from __future__ import annotations

import os
from typing import Any

from adaptors.http import shared as pooled_http

from . import _MissingKey, _fail, _need


def _instance() -> str:
    inst = (os.getenv("MASTODON_INSTANCE") or "").strip().strip('"').strip("'")
    if not inst:
        raise _MissingKey("MASTODON_INSTANCE")
    if not inst.startswith("http"):
        inst = "https://" + inst
    return inst.rstrip("/")


def send(*, text: str, to: str = "", visibility: str = "public") -> dict[str, Any]:
    try:
        token = _need("MASTODON_TOKEN")
        inst = _instance()
    except _MissingKey as e:
        return _fail("MASTODON_TOKEN not set" if "MASTODON_TOKEN" in str(e)
                     else "MASTODON_INSTANCE not set")
    try:
        with pooled_http(30.0) as client:
            r = client.post(f"{inst}/api/v1/statuses",
                            headers={"Authorization": f"Bearer {token}"},
                            data={"status": text, "visibility": visibility})
            if r.status_code >= 400:
                return {"ok": False, "error": f"mastodon {r.status_code}"}
            d = r.json()
        return {"ok": True, "id": str(d.get("id") or ""), "url": d.get("url")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}