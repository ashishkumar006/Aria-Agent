"""WebUI chat adaptor (the agent's own chat page + future PWA). LIVE (local).

No credentials: localhost traffic is inherently paired-or-owner by virtue
of the dashboard auth gate (non-loopback binds require auth). Normalises
the chat UI's message shape so the session bus, ledger and policy see the
same envelope as every other channel. Trust is resolved by the caller:
loopback operator traffic defaults to owner, anything else pairs via
the control plane.
"""
from __future__ import annotations

import time
from typing import Any

from ..base import BaseAdaptor, Capability
from ..envelope import ChannelMessage, ChannelReply, TrustLevel
from .schemas import WebUIMessage


class WebUIAdaptor(BaseAdaptor):
    name = "webui"
    capabilities = Capability(send_text=True, send_media=True, receive=True,
                              threads=True)

    def required_keys(self) -> list[str]:
        return []

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None):
        # Delivery to a browser session happens over that session's SSE
        # stream (owned by the agent server). The gateway records the reply
        # for the ledger; transport is a no-op here.
        return ChannelReply(ok=True, channel=self.name,
                            msg_id=f"webui-{int(time.time() * 1000)}",
                            raw={"to": to, "thread_id": thread_id})

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        m = WebUIMessage.model_validate(payload)
        trust = TrustLevel.owner if m.local else TrustLevel.paired
        return ChannelMessage(
            channel=self.name, sender_id=m.sender_id,
            chat_id=m.conversation_id or m.chat_id,
            text=m.text or m.query,
            msg_id=m.msg_id,
            ts=m.ts or time.time(),
            trust_level=trust, raw=payload)
