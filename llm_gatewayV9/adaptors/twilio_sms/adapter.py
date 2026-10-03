"""Twilio SMS adaptor. SEND LIVE.

Setup: Twilio console → buy/rent a number (TWILIO_FROM), API key pair
(TWILIO_SID / TWILIO_AUTH). US A2P 10DLC registration applies to
production US traffic; trial credits cover dev. Inbound SMS arrives via
webhook (X-Twilio-Signature validated against the webhook URL — see
verify.py, shared with the WhatsApp and Voice adaptors).
"""
from __future__ import annotations

import time
from typing import Any

from ..base import BaseAdaptor, Capability, NotIntegrated
from ..envelope import ChannelMessage, TrustLevel
from ..trust import classify
from . import verify as _verify
from .schemas import TwilioSmsWebhook


class TwilioSmsAdaptor(BaseAdaptor):
    name = "twilio_sms"
    capabilities = Capability(send_text=True, send_media=True, receive=True)

    def required_keys(self) -> list[str]:
        return ["TWILIO_SID", "TWILIO_AUTH", "TWILIO_FROM"]

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        return _verify.verify(headers, body)

    def _twilio_post(self, sid: str, auth: str, path: str,
                     data: dict[str, Any]) -> dict[str, Any]:
        from .. import http as _http
        import base64
        creds = base64.b64encode(f"{sid}:{auth}".encode()).decode()
        try:
            r = _http.post(f"https://api.twilio.com/2010-04-01{path}",
                           headers={"Authorization": f"Basic {creds}"},
                           data=data, timeout=30.0)
        except Exception as e:
            raise RuntimeError(f"twilio transport error: {type(e).__name__}") from None
        if r.status_code >= 400:
            try:
                detail = r.json().get("message", "")
            except ValueError:
                detail = ""
            raise RuntimeError(f"twilio error {r.status_code}: {detail or 'request rejected'}")
        try:
            return r.json()
        except ValueError:
            return {}

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None):
        # LIVE: Messages API (+ MediaUrl[] for MMS).
        import os
        from ..envelope import ChannelReply
        sid = self._need("TWILIO_SID")
        auth = self._need("TWILIO_AUTH")
        sender = (os.getenv("TWILIO_FROM") or "").strip()
        if not sender:
            from ..base import NotConfigured
            raise NotConfigured("adaptor 'twilio_sms' missing TWILIO_FROM")
        form: dict[str, Any] = {"From": sender, "To": to, "Body": text[:1600]}
        for m in media or []:
            if isinstance(m, dict) and m.get("url"):
                form.setdefault("MediaUrl", []).append(str(m["url"]))
        data = self._twilio_post(sid, auth, f"/Accounts/{sid}/Messages.json", form)
        return ChannelReply(ok=True, channel=self.name,
                            msg_id=str(data.get("sid", "")), raw=data)

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        m = TwilioSmsWebhook.model_validate(payload)
        return ChannelMessage(
            channel=self.name, sender_id=m.From,
            chat_id=m.From, text=m.Body,
            msg_id=m.MessageSid,
            ts=time.time(), trust_level=classify(self.name, m.From),
            raw=payload)

    def cost_hint_usd(self, *, media: bool = False) -> float:
        return 0.02 if media else 0.008  # rough US segment rate
