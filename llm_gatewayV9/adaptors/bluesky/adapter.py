"""Bluesky adaptor (AT Protocol). Posts are public; `to` is unused (top-level).

Auth: BSKY_HANDLE + BSKY_APP_PASSWORD (an app password, not the account one).
"""
from __future__ import annotations

import time
from typing import Any

from ..base import BaseAdaptor, Capability, NotConfigured
from ..envelope import ChannelMessage, ChannelReply
from ..trust import classify


class BlueskyAdaptor(BaseAdaptor):
    name = "bluesky"
    capabilities = Capability(send_text=True, send_media=False, receive=False)

    def required_keys(self) -> list[str]:
        return ["BSKY_HANDLE", "BSKY_APP_PASSWORD"]

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None):
        from integrations import bluesky as _b
        res = _b.send(text=text, to=to)
        if not res.get("ok"):
            err = str(res.get("error", "post failed"))
            if "not set" in err.lower():
                raise NotConfigured("adaptor 'bluesky' is not configured")
            raise RuntimeError(err)
        return ChannelReply(ok=True, channel=self.name, msg_id=str(res.get("uri") or ""),
                            raw=res)

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        sender = str(payload.get("author") or payload.get("did") or "")
        return ChannelMessage(
            channel=self.name, sender_id=sender, chat_id=sender,
            text=str(payload.get("text") or payload.get("record", {}).get("text") or ""),
            msg_id=str(payload.get("uri") or payload.get("cid") or ""),
            ts=time.time(), trust_level=classify(self.name, sender),
            raw=payload)