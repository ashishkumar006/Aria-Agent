"""Bluesky integration (AT Protocol) — free social posting, no app review.

Auth: an app password (Settings -> App passwords), not the account password.
  BSKY_HANDLE       (required) -> e.g. name.bsky.social
  BSKY_APP_PASSWORD (required) -> the app password
Posts are public (app.bsky.feed.post). `to` is unused for a top-level post.
"""
from __future__ import annotations

import time
from typing import Any

from adaptors.http import shared as pooled_http

from . import _MissingKey, _fail, _need

_PDS = "https://bsky.social"


def _session(client) -> tuple[str, str]:
    handle = _need("BSKY_HANDLE")
    password = _need("BSKY_APP_PASSWORD")
    r = client.post(f"{_PDS}/xrpc/com.atproto.server.createSession",
                    json={"identifier": handle, "password": password})
    r.raise_for_status()
    d = r.json()
    return str(d.get("accessJwt") or ""), str(d.get("did") or "")


def send(*, text: str, to: str = "") -> dict[str, Any]:
    try:
        with pooled_http(30.0) as client:
            jwt, did = _session(client)
            if not jwt or not did:
                return {"ok": False, "error": "bsky: no accessJwt/did from createSession"}
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            r = client.post(
                f"{_PDS}/xrpc/com.atproto.repo.createRecord",
                headers={"Authorization": f"Bearer {jwt}"},
                json={"repo": did, "collection": "app.bsky.feed.post",
                      "record": {"text": text, "createdAt": now}})
            if r.status_code >= 400:
                return {"ok": False, "error": f"bsky {r.status_code}"}
            d = r.json()
        return {"ok": True, "uri": d.get("uri"), "cid": d.get("cid")}
    except _MissingKey as e:
        return _fail(str(e))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}