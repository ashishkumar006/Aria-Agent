"""WhatsApp via Twilio adaptor (sandbox-first path). SEND LIVE.

Setup: Twilio console → Messaging → WhatsApp sandbox → join code from your
phone (each test recipient must opt in). Same API + same signature scheme
as SMS, with `whatsapp:`-prefixed addresses and a WhatsApp template regime
outside the 24h window. Free trial credits cover dev. Recommended FIRST
WhatsApp path (Meta Cloud verification takes days — see whatsapp_meta).
"""
from __future__ import annotations

import time
from typing import Any

from ..base import BaseAdaptor, Capability, NotIntegrated
from ..envelope import ChannelMessage, TrustLevel
from ..trust import classify
from ..twilio_sms import verify as _twilio_verify
from .schemas import WhatsAppTwilioWebhook


class WhatsAppTwilioAdaptor(BaseAdaptor):
    name = "whatsapp_twilio"
    capabilities = Capability(send_text=True, send_media=True, receive=True)

    def required_keys(self) -> list[str]:
        return ["TWILIO_SID", "TWILIO_AUTH", "WHATSAPP_FROM"]

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        return _twilio_verify.verify(headers, body)

    @staticmethod
    def _wa(num: str) -> str:
        num = (num or "").strip()
        return num if num.startswith("whatsapp:") else f"whatsapp:{num}"

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None):
        # LIVE: same Messages API with whatsapp: addresses. Outside the 24h
        # window, Meta requires templates: pass
        # media=[{"content_sid": "...", "content_variables": {...}}].
        import base64
        import json as _json
        import os
        from .. import http as _http
        from ..base import NotConfigured
        from ..envelope import ChannelReply
        sid = self._need("TWILIO_SID")
        auth = self._need("TWILIO_AUTH")
        sender = self._wa(os.getenv("WHATSAPP_FROM") or "")
        if sender == "whatsapp:":
            raise NotConfigured("adaptor 'whatsapp_twilio' missing WHATSAPP_FROM")
        form: dict[str, Any] = {"From": sender, "To": self._wa(to)}
        tpl = next((m for m in (media or [])
                    if isinstance(m, dict) and m.get("content_sid")), None)
        if tpl is not None:
            form["ContentSid"] = str(tpl["content_sid"])
            if tpl.get("content_variables") is not None:
                form["ContentVariables"] = _json.dumps(tpl["content_variables"])
        else:
            form["Body"] = text[:1600]
        creds = base64.b64encode(f"{sid}:{auth}".encode()).decode()
        try:
            r = _http.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                headers={"Authorization": f"Basic {creds}"},
                data=form, timeout=30.0)
        except Exception as e:
            raise RuntimeError(f"twilio transport error: {type(e).__name__}") from None
        if r.status_code >= 400:
            try:
                detail = r.json().get("message", "")
            except ValueError:
                detail = ""
            raise RuntimeError(f"twilio error {r.status_code}: {detail or 'request rejected'}")
        try:
            resp = r.json()
        except ValueError:
            resp = {}
        return ChannelReply(ok=True, channel=self.name,
                            msg_id=str(resp.get("sid", "")), raw=resp)

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        m = WhatsAppTwilioWebhook.model_validate(payload)
        sender = m.From.replace("whatsapp:", "")
        return ChannelMessage(
            channel=self.name, sender_id=sender,
            chat_id=sender, text=m.Body,
            msg_id=m.MessageSid,
            ts=time.time(), trust_level=classify(self.name, sender),
            raw=payload)

    def cost_hint_usd(self, *, media: bool = False) -> float:
        return 0.03 if media else 0.01
