"""Twilio Voice webhook + media-stream frame shapes."""
from __future__ import annotations

from pydantic import BaseModel, Field


class TwilioVoiceWebhook(BaseModel):
    model_config = {"extra": "ignore"}

    From: str = ""
    Caller: str = ""
    To: str = ""
    CallSid: str = ""
    CallStatus: str = ""
    Digits: str = ""
    TranscriptionText: str = ""


class TwilioMediaFrame(BaseModel):
    model_config = {"extra": "ignore"}

    event: str = ""
    streamSid: str = ""
    media: dict = Field(default_factory=dict)
