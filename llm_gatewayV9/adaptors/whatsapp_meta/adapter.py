"""WhatsApp via Meta Cloud API adaptor. SEND LIVE (P5 — verification is slow).

Setup (days, not hours): Meta App Dashboard → WhatsApp use-case → business
portfolio → WhatsApp Business account + phone number → system user with
whatsapp_business_messaging + whatsapp_business_management → permanent
token (WA_TOKEN) → webhook verify-token handshake (GET hub.mode=subscribe
+ hub.verify_token, echo hub.challenge) → subscribe to `messages` field.
Meta retries non-200 deliveries with backoff for up to 7 days; outside the
24h customer-service window only template messages may be sent.
"""
from __future__ import annotations

import time
from typing import Any

from ..base import BaseAdaptor, Capability, NotIntegrated
from ..envelope import ChannelMessage, TrustLevel
from ..trust import classify
from . import verify as _verify
from .schemas import WaWebhook


class WhatsAppMetaAdaptor(BaseAdaptor):
    name = "whatsapp_meta"
    capabilities = Capability(send_text=True, send_media=True, receive=True,
                              needs_public_url=True, needs_verification=True)

    def required_keys(self) -> list[str]:
        return ["WA_TOKEN", "WA_PHONE_ID", "WA_VERIFY_TOKEN"]

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        # GET handshake is handled in the route; POST trust comes from HTTPS
        # (+ App Secret signature when META_APP_SECRET is set).
        return _verify.verify_signature(headers, body)

    @staticmethod
    def verify_handshake(params: dict[str, str], verify_token: str) -> str | None:
        """Back-compat alias for verify.verify_handshake()."""
        return _verify.verify_handshake(params, verify_token)

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None):
        # LIVE once verified: POST graph.facebook.com/{WA_PHONE_ID}/messages.
        # Plain text needs an open 24h window; otherwise pass a template via
        # media=[{"template": "<name>", "language": "en_US",
        #         "variables": ["..."]}].
        import os
        from .. import http as _http
        from ..envelope import ChannelReply
        token = self._need("WA_TOKEN")
        phone_id = (os.getenv("WA_PHONE_ID") or "").strip()
        if not phone_id:
            from ..base import NotConfigured
            raise NotConfigured("adaptor 'whatsapp_meta' missing WA_PHONE_ID")
        tpl = next((m for m in (media or [])
                    if isinstance(m, dict) and m.get("template")), None)
        if tpl is not None:
            params = {"type": "template",
                      "template": {"name": str(tpl["template"]),
                                   "language": {"code": str(tpl.get("language", "en_US"))}}}
            if tpl.get("variables"):
                params["template"]["components"] = [{
                    "type": "body",
                    "parameters": [{"type": "text", "text": str(v)}
                                   for v in tpl["variables"]]}]
        else:
            params = {"type": "text", "text": {"body": text[:4096]}}
        if thread_id:
            params["context"] = {"message_id": str(thread_id)}
        try:
            r = _http.post(
                f"https://graph.facebook.com/v21.0/{phone_id}/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={"messaging_product": "whatsapp", "to": to, **params},
                timeout=30.0)
        except Exception as e:
            raise RuntimeError(f"whatsapp transport error: {type(e).__name__}") from None
        if r.status_code >= 400:
            try:
                err = (r.json().get("error") or {})
                detail = err.get("message", "")
            except ValueError:
                detail = ""
            raise RuntimeError(f"whatsapp error {r.status_code}: {detail or 'request rejected'}")
        try:
            data = r.json()
        except ValueError:
            data = {}
        msgs = data.get("messages") or [{}]
        return ChannelReply(ok=True, channel=self.name,
                            msg_id=str(msgs[0].get("id", "")), raw=data)

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        hook = WaWebhook.model_validate(payload)
        msg = None
        for entry in hook.entry:
            for change in entry.changes:
                if change.value.messages:
                    msg = change.value.messages[0]
                    break
            if msg is not None:
                break
        if msg is None:
            # statuses[] and other fields: envelope, no text.
            return ChannelMessage(channel=self.name, raw=payload,
                                  trust_level=TrustLevel.untrusted)
        try:
            ts = float(msg.timestamp or 0)
        except (TypeError, ValueError):
            ts = 0.0
        return ChannelMessage(
            channel=self.name, sender_id=msg.from_, chat_id=msg.from_,
            text=msg.text.body if msg.type == "text" else "",
            msg_id=msg.id, ts=ts or time.time(),
            trust_level=classify(self.name, msg.from_), raw=payload)
