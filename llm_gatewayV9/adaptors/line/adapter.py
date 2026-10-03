"""LINE adaptor (Messaging API webhook). SEND LIVE; webhook receiver was
already live. Full inbound flow needs the public URL deployment (P1).

Setup: LINE Developers → channel → channel secret + channel access token
(LINE_CHANNEL_SECRET / LINE_ACCESS_TOKEN). Webhooks carry X-Line-Signature
(HMAC-SHA256 over the raw body with the channel secret) — see verify.py.
Mostly straightforward otherwise. Free: yes.
"""
from __future__ import annotations

import time
from typing import Any

from ..base import BaseAdaptor, Capability, NotIntegrated
from ..envelope import ChannelMessage, TrustLevel
from ..trust import classify
from . import verify as _verify
from .schemas import LineWebhook


class LineAdaptor(BaseAdaptor):
    name = "line"
    capabilities = Capability(send_text=True, send_media=True, receive=True)

    def required_keys(self) -> list[str]:
        return ["LINE_CHANNEL_SECRET", "LINE_ACCESS_TOKEN"]

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        return _verify.verify(headers, body)

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None):
        # LIVE: push API. Prefer replyToken when the caller passes one as
        # thread_id="reply:<token>" (quota-free); else push to `to`.
        import os
        from .. import http as _http
        from ..envelope import ChannelReply
        token = self._need("LINE_ACCESS_TOKEN")
        base = os.getenv("LINE_API_BASE", "https://api.line.me")
        try:
            if thread_id and thread_id.startswith("reply:"):
                r = _http.post(
                    f"{base}/v2/bot/message/reply",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"replyToken": thread_id[len("reply:"):],
                          "messages": [{"type": "text", "text": text[:5000]}]},
                    timeout=20.0)
            else:
                r = _http.post(
                    f"{base}/v2/bot/message/push",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"to": to,
                          "messages": [{"type": "text", "text": text[:5000]}]},
                    timeout=20.0)
        except Exception as e:
            raise RuntimeError(f"line transport error: {type(e).__name__}") from None
        if r.status_code >= 400:
            try:
                detail = r.json().get("message", "")
            except ValueError:
                detail = ""
            raise RuntimeError(f"line error {r.status_code}: {detail or 'request rejected'}")
        try:
            data = r.json()
        except ValueError:
            data = {}
        return ChannelReply(ok=True, channel=self.name, raw=data)

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        hook = LineWebhook.model_validate(payload)
        ev = hook.events[0] if hook.events else None
        if ev is None:
            return ChannelMessage(channel=self.name, raw=payload,
                                  trust_level=TrustLevel.untrusted)
        chat = ev.source.groupId or ev.source.roomId or ev.source.userId
        text = ev.message.text if ev.message.type == "text" else ""
        return ChannelMessage(
            channel=self.name, sender_id=ev.source.userId, chat_id=chat,
            text=text, msg_id=ev.webhookEventId or ev.message.id,
            ts=(ev.timestamp / 1000.0) if ev.timestamp else time.time(),
            trust_level=classify(self.name, ev.source.userId), raw=payload)
