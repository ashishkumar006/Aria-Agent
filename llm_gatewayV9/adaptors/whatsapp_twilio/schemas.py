"""WhatsApp-via-Twilio payloads (Twilio form fields, whatsapp: addresses)."""
from __future__ import annotations

from pydantic import BaseModel


class WhatsAppTwilioWebhook(BaseModel):
    model_config = {"extra": "ignore"}

    From: str = ""
    To: str = ""
    Body: str = ""
    MessageSid: str = ""
    ContentSid: str = ""
