"""Mastodon adaptor. Posts a status to the configured instance; `to` is unused.

Auth: MASTODON_TOKEN + MASTODON_INSTANCE.
"""
from __future__ import annotations

import time
from typing import Any

from ..base import BaseAdaptor, Capability, NotConfigured
from ..envelope import ChannelMessage, ChannelReply
from ..trust import classify


class MastodonAdaptor(BaseAdaptor):
    name = "mastodon"
    capabilities = Capability(send_text=True, send_media=False, receive=False)

    def required_keys(self) -> list[str]:
        return ["MASTODON_TOKEN", "MASTODON_INSTANCE"]

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None):
        from integrations import mastodon as _m
        res = _m.send(text=text, to=to)
        if not res.get("ok"):
            err = str(res.get("error", "post failed"))
            if "not set" in err.lower():
                raise NotConfigured("adaptor 'mastodon' is not configured")
            raise RuntimeError(err)
        return ChannelReply(ok=True, channel=self.name,
                            msg_id=str(res.get("id") or ""), raw=res)

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        sender = str((payload.get("account") or {}).get("acct") or "")
        return ChannelMessage(
            channel=self.name, sender_id=sender, chat_id=sender,
            text=str(payload.get("content") or ""),
            msg_id=str(payload.get("id") or ""),
            ts=time.time(), trust_level=classify(self.name, sender),
            raw=payload)