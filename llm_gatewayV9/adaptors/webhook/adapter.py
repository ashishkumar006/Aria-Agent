"""Generic webhook adaptor (HTTP in, HTTP out). LIVE.

The simplest adaptor and the hardest to test: accepts any JSON POST,
optionally HMAC-verified with WEBHOOK_SECRET (see verify.py), and
normalises {text, sender_id, chat_id} fields. Outbound delivery POSTs to
a pre-registered URL (WEBHOOK_OUT_URL).
"""
from __future__ import annotations

import time
from typing import Any

from .. import http as _http
from ..base import BaseAdaptor, Capability, NotConfigured
from ..envelope import ChannelMessage, TrustLevel
from ..trust import classify
from . import verify as _verify
from .schemas import WebhookInbound


class GenericWebhookAdaptor(BaseAdaptor):
    name = "webhook"
    capabilities = Capability(send_text=True, receive=True)

    def required_keys(self) -> list[str]:
        return []  # secret optional; works open (untrusted) without it

    def health(self) -> dict[str, Any]:
        # No keys exist to check, but an out-URL is required to actually
        # send — report that honestly instead of a blanket configured=True.
        import os
        h = super().health()
        h["configured"] = bool((os.getenv("WEBHOOK_OUT_URL") or "").strip())
        h["required_keys"] = ["WEBHOOK_OUT_URL"]
        return h

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        return _verify.verify(headers, body)

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None):
        import os
        from ..envelope import ChannelReply
        url = (os.getenv("WEBHOOK_OUT_URL") or "").strip()
        if not url:
            raise NotConfigured("adaptor 'webhook' is not configured")
        r = _http.post(url, json={"to": to, "text": text, "ts": time.time()},
                       timeout=20.0)
        r.raise_for_status()
        return ChannelReply(ok=True, channel=self.name, raw={"status": r.status_code})

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        m = WebhookInbound.model_validate(payload)
        sender = m.sender_id or m.sender
        return ChannelMessage(
            channel=self.name, sender_id=sender,
            chat_id=m.chat_id or m.to,
            text=m.text or m.message,
            msg_id=m.msg_id or m.id,
            ts=m.ts or time.time(),
            trust_level=classify(self.name, sender), raw=payload)
