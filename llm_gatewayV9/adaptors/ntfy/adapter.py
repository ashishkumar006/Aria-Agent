"""ntfy adaptor — free phone push. Keyless (hosted ntfy.sh); `to` is the topic.

Outbound only (no inbound webhook): a POST to <base>/<topic> reaches every
subscriber. Optional NTFY_URL (self-host) / NTFY_TOKEN (reserved topic).
"""
from __future__ import annotations

import time
from typing import Any

from ..base import BaseAdaptor, Capability
from ..envelope import ChannelMessage, ChannelReply
from ..trust import classify


class NtfyAdaptor(BaseAdaptor):
    name = "ntfy"
    capabilities = Capability(send_text=True, send_media=False, receive=False)

    def required_keys(self) -> list[str]:
        # Keyless: hosted ntfy.sh needs no credential.
        return []

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None):
        from integrations import ntfy as _n
        res = _n.send(topic=to, message=text)
        if not res.get("ok"):
            raise RuntimeError(str(res.get("error", "ntfy send failed")))
        return ChannelReply(ok=True, channel=self.name,
                            msg_id=str(res.get("id") or ""))

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        # ntfy has no inbound webhook; this accepts a stored event shape only.
        sender = str(payload.get("sender") or "")
        return ChannelMessage(
            channel=self.name, sender_id=sender, chat_id=sender,
            text=str(payload.get("message") or ""),
            msg_id=str(payload.get("id") or ""),
            ts=time.time(), trust_level=classify(self.name, sender),
            raw=payload)