"""Zoho Cliq adaptor — free team chat. `to` is the bot/channel unique name.

Auth: ZOHO_CLIQ_TOKEN (bot webhook token). Outbound only for now (inbound would
need a per-bot webhook + a poller, added later).
"""
from __future__ import annotations

import time
from typing import Any

from ..base import BaseAdaptor, Capability, NotConfigured
from ..envelope import ChannelMessage, ChannelReply
from ..trust import classify


class ZohoCliqAdaptor(BaseAdaptor):
    name = "zoho_cliq"
    capabilities = Capability(send_text=True, send_media=False, receive=True)

    def required_keys(self) -> list[str]:
        return ["ZOHO_CLIQ_TOKEN"]

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None):
        from integrations import cliq as _c
        res = _c.send(to=to, text=text)
        if not res.get("ok"):
            err = str(res.get("error", "send failed"))
            if "not set" in err.lower():
                raise NotConfigured("adaptor 'zoho_cliq' is not configured")
            raise RuntimeError(err)
        return ChannelReply(ok=True, channel=self.name, msg_id="", raw=res)

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        sender = str(payload.get("sender") or payload.get("from") or "")
        return ChannelMessage(
            channel=self.name, sender_id=sender, chat_id=sender,
            text=str(payload.get("text") or payload.get("message") or ""),
            msg_id=str(payload.get("id") or ""),
            ts=time.time(), trust_level=classify(self.name, sender),
            raw=payload)