"""Matrix adaptor (Client-Server API). SEND + SYNC-POLL LIVE; a permanent
sync loop remains an operator-run concern.

Setup: pick a homeserver (matrix.org or self-hosted) → login with user +
password → MATRIX_USER/MATRIX_PASSWORD → persist the access token. Inbound
is /sync long-poll; outbound is PUT /rooms/{room}/send/m.room.message/{txn}.
Choose the homeserver carefully (federation, retention, trust). Free: yes.
"""
from __future__ import annotations

import time
from typing import Any

from ..base import BaseAdaptor, Capability, NotIntegrated
from ..envelope import ChannelMessage, TrustLevel
from ..trust import classify
from .schemas import MatrixTimelineEvent


class MatrixAdaptor(BaseAdaptor):
    name = "matrix"
    capabilities = Capability(send_text=True, send_media=True, receive=True,
                              threads=True, reactions=True)

    def __init__(self):
        # Login token + single-retry flag (re-login once per 401). Kept on
        # the instance because the registry holds exactly one per channel.
        self._mx_token: str | None = None
        self._mx_retried: bool = False

    def required_keys(self) -> list[str]:
        return ["MATRIX_HOMESERVER", "MATRIX_USER", "MATRIX_PASSWORD"]

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None):
        # LIVE: login → PUT .../send/m.room.message/{txnId}. Token cached
        # in-process (registry holds one instance); re-login on 401 once.
        import os
        import time as _t
        import uuid as _uuid
        from .. import http as _http
        from ..envelope import ChannelReply
        base = (os.getenv("MATRIX_HOMESERVER") or "").rstrip("/")
        if not base:
            from ..base import NotConfigured
            raise NotConfigured("adaptor 'matrix' is not configured")
        user = self._need("MATRIX_USER")
        password = self._need("MATRIX_PASSWORD")
        if getattr(self, "_mx_token", None) is None:
            try:
                r = _http.post(f"{base}/_matrix/client/v3/login",
                               json={"type": "m.login.password", "identifier":
                                     {"type": "m.id.user", "user": user},
                                     "password": password}, timeout=20.0)
                r.raise_for_status()
                self._mx_token = r.json().get("access_token", "")
            except Exception as e:
                raise RuntimeError(f"matrix login failed: {type(e).__name__}") from None
            if not self._mx_token:
                raise RuntimeError("matrix login failed: no access token")
        body: dict[str, Any] = {"msgtype": "m.text", "body": text}
        if thread_id:
            body["m.relates_to"] = {"rel_type": "m.thread",
                                    "event_id": thread_id}
        txn = f"aria-{int(_t.time() * 1000)}-{_uuid.uuid4().hex[:6]}"
        try:
            r = _http.post(
                f"{base}/_matrix/client/v3/rooms/{to}/send/m.room.message/{txn}",
                headers={"Authorization": f"Bearer {self._mx_token}"},
                json=body, timeout=20.0)
        except Exception as e:
            raise RuntimeError(f"matrix transport error: {type(e).__name__}") from None
        if r.status_code == 401 and not getattr(self, "_mx_retried", False):
            self._mx_token = None
            self._mx_retried = True
            return self.send(to=to, text=text, thread_id=thread_id, media=media)
        self._mx_retried = False
        if r.status_code >= 400:
            try:
                detail = r.json().get("error", "")
            except ValueError:
                detail = ""
            raise RuntimeError(f"matrix error {r.status_code}: {detail or 'request rejected'}")
        return ChannelReply(ok=True, channel=self.name,
                            msg_id=str(r.json().get("event_id", "")), raw={})

    def sync_once(self, *, since: str | None = None,
                  timeout_ms: int = 10000) -> list[dict[str, Any]]:
        """One /sync poll returning raw timeline events (for operator-run
        poller loops; the gateway does not run a permanent Matrix loop)."""
        import os
        from .. import http as _http
        base = (os.getenv("MATRIX_HOMESERVER") or "").rstrip("/")
        token = getattr(self, "_mx_token", None)
        if not base or not token:
            from ..base import NotConfigured
            raise NotConfigured("adaptor 'matrix' is not configured")
        params: dict[str, Any] = {"timeout": timeout_ms}
        if since:
            params["since"] = since
        r = _http.get(f"{base}/_matrix/client/v3/sync",
                      params=params,
                      headers={"Authorization": f"Bearer {token}"},
                      timeout=(timeout_ms / 1000.0) + 10.0)
        r.raise_for_status()
        data = r.json()
        out = []
        for _room_id, room in ((data.get("rooms") or {}).get("join") or {}).items():
            for ev in ((room.get("timeline") or {}).get("events") or []):
                ev = dict(ev)
                ev["room_id"] = _room_id
                out.append(ev)
        out.append({"_sync_batch": data.get("next_batch", "")})
        return out

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        ev = MatrixTimelineEvent.model_validate(payload)
        thread = ev.content.relates_to.event_id or None
        ts = (ev.origin_server_ts / 1000.0) if ev.origin_server_ts else time.time()
        return ChannelMessage(
            channel=self.name, sender_id=ev.sender, chat_id=ev.room_id,
            text=ev.content.body, thread_id=thread, msg_id=ev.event_id,
            ts=ts, trust_level=classify(self.name, ev.sender), raw=payload)
