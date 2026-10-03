"""Slack adaptor (Web API + Events API). PARTIAL (P2).

Outbound chat.postMessage is LIVE. Inbound Events API is scaffold: OAuth
scopes are a maze (chat:write, channels:history, app_mentions:read, …) and
receiving events needs a PUBLIC HTTPS endpoint, so inbound waits on
deployment. Signing-secret verification is real code (see verify.py).
Free: yes.
"""
from __future__ import annotations

import time
from typing import Any

from .. import http as _http
from ..base import BaseAdaptor, Capability
from ..envelope import ChannelMessage, TrustLevel
from ..trust import classify
from . import verify as _verify
from .schemas import SlackEnvelope


class SlackAdaptor(BaseAdaptor):
    name = "slack"
    capabilities = Capability(send_text=True, send_media=False, receive=True,
                              threads=True, reactions=True, needs_public_url=True)

    def required_keys(self) -> list[str]:
        return ["SLACK_BOT_TOKEN"]

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        return _verify.verify(headers, body)

    def _post(self, to: str, text: str, thread_id: str | None, token: str):
        return _http.post("https://slack.com/api/chat.postMessage",
                          headers={"Authorization": f"Bearer {token}"},
                          json={"channel": to, "text": text,
                                **({"thread_ts": thread_id} if thread_id else {})},
                          timeout=20.0)

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None):
        # LIVE: chat.postMessage. thread_ts routes into threads.
        # Rotation-aware: on token_expired (rotation toggle ON, 12h tokens)
        # renew once via integrations.slack.refresh() and retry the send.
        r = self._post(to, text, thread_id, self._need("SLACK_BOT_TOKEN"))
        r.raise_for_status()
        data = r.json()
        if not data.get("ok") and data.get("error") == "token_expired":
            from integrations import slack as _slack
            res = _slack.refresh(write_env=True)
            if not res.get("ok"):
                raise RuntimeError(f"slack error: token_expired; refresh failed: {res.get('error')}")
            import os as _os
            r = self._post(to, text, thread_id, (_os.getenv("SLACK_BOT_TOKEN") or "").strip())
            r.raise_for_status()
            data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"slack error: {data.get('error', 'unknown')}")
        from ..envelope import ChannelReply
        return ChannelReply(ok=True, channel=self.name,
                            msg_id=str(data.get("ts", "")), raw=data)

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        env = SlackEnvelope.model_validate(payload)
        ev = env.event
        try:
            ts = float(ev.event_ts or 0) or time.time()
        except (TypeError, ValueError):
            ts = time.time()
        return ChannelMessage(
            channel=self.name, sender_id=ev.user,
            chat_id=ev.channel, text=ev.text,
            thread_id=ev.thread_ts or None,
            msg_id=ev.client_msg_id or ev.ts,
            ts=ts, trust_level=classify(self.name, ev.user), raw=payload)
