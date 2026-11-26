"""Google Chat adaptor. Posts to a space via its incoming webhook; `to` unused.

Auth: GOOGLE_CHAT_WEBHOOK (space -> Apps & integrations -> Webhooks).
"""
from __future__ import annotations

import time
from typing import Any

from ..base import BaseAdaptor, Capability, NotConfigured
from ..envelope import ChannelMessage, ChannelReply
from ..trust import classify


class GoogleChatAdaptor(BaseAdaptor):
    name = "google_chat"
    capabilities = Capability(send_text=True, send_media=False, receive=False)

    def required_keys(self) -> list[str]:
        return ["GOOGLE_CHAT_WEBHOOK"]

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None):
        from integrations import google_chat as _g
        res = _g.send(text=text, to=to)
        if not res.get("ok"):
            err = str(res.get("error", "send failed"))
            if "not set" in err.lower():
                raise NotConfigured("adaptor 'google_chat' is not configured")
            raise RuntimeError(err)
        return ChannelReply(ok=True, channel=self.name, msg_id="", raw=res)

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        sender = str(payload.get("sender") or "")
        return ChannelMessage(
            channel=self.name, sender_id=sender, chat_id=sender,
            text=str(payload.get("text") or ""),
            msg_id=str(payload.get("message" or {}).get("name") or "")
            if isinstance(payload.get("message"), dict) else "",
            ts=time.time(), trust_level=classify(self.name, sender),
            raw=payload)