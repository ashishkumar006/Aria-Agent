"""Twilio Voice adaptor (calls + media streams). CALLS LIVE (P5 — hardest).

Voice is a different beast from messaging: sub-second latency budget,
codec negotiation (mulaw/8kHz), barge-in, hold behavior, and streaming
audio in/out over WebSockets (<Stream> TwiML → wss media endpoint).
Trial credits cover dev; production needs numbers + compliance.
Do this last, after every messaging adaptor is live. Signature scheme is
shared with SMS (see twilio_sms.verify).
"""
from __future__ import annotations

import time
from typing import Any

from ..base import BaseAdaptor, Capability, NotIntegrated
from ..envelope import ChannelMessage, TrustLevel
from ..trust import classify
from ..twilio_sms import verify as _twilio_verify
from .schemas import TwilioVoiceWebhook


class TwilioVoiceAdaptor(BaseAdaptor):
    name = "twilio_voice"
    capabilities = Capability(send_text=False, send_media=False, receive=True,
                              voice_calls=True, needs_public_url=True)

    def required_keys(self) -> list[str]:
        return ["TWILIO_SID", "TWILIO_AUTH", "TWILIO_VOICE_FROM", "VOICE_WS_URL"]

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        return _twilio_verify.verify(headers, body)

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None,
             stream: bool = False):
        # LIVE call creation: POST Calls API. Default TwiML speaks `text`
        # as the greeting; stream=True instead connects <Stream> to
        # VOICE_WS_URL for the (separately-hosted) media loop.
        import os
        from .. import http as _http
        from ..envelope import ChannelReply
        sid = self._need("TWILIO_SID")
        auth = self._need("TWILIO_AUTH")
        sender = (os.getenv("TWILIO_VOICE_FROM") or "").strip()
        if not sender:
            from ..base import NotConfigured
            raise NotConfigured("adaptor 'twilio_voice' missing TWILIO_VOICE_FROM")
        import base64
        creds = base64.b64encode(f"{sid}:{auth}".encode()).decode()
        if stream:
            ws = (os.getenv("VOICE_WS_URL") or "").strip()
            if not ws:
                from ..base import NotConfigured
                raise NotConfigured("streaming calls need VOICE_WS_URL")
            twiml = (f"<Response><Connect><Stream url=\"{ws}\">"
                     f"<Parameter name=\"caller\" value=\"{to}\"/></Stream>"
                     f"</Connect></Response>")
        else:
            safe = text[:1000].replace("&", "&amp;").replace("<", "&lt;")
            twiml = f"<Response><Say>{safe}</Say></Response>"
        try:
            r = _http.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json",
                headers={"Authorization": f"Basic {creds}"},
                data={"From": sender, "To": to, "Twiml": twiml}, timeout=30.0)
        except Exception as e:
            raise RuntimeError(f"twilio transport error: {type(e).__name__}") from None
        if r.status_code >= 400:
            try:
                detail = r.json().get("message", "")
            except ValueError:
                detail = ""
            raise RuntimeError(f"twilio error {r.status_code}: {detail or 'request rejected'}")
        try:
            data = r.json()
        except ValueError:
            data = {}
        return ChannelReply(ok=True, channel=self.name,
                            msg_id=str(data.get("sid", "")), raw=data)

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        m = TwilioVoiceWebhook.model_validate(payload)
        sender = m.From or m.Caller
        return ChannelMessage(
            channel=self.name, sender_id=sender, chat_id=sender,
            text=m.TranscriptionText or m.Digits,
            msg_id=m.CallSid,
            ts=time.time(), trust_level=classify(self.name, sender),
            raw=payload)

    def cost_hint_usd(self, *, media: bool = False) -> float:
        return 0.05  # rough per-minute voice leg
