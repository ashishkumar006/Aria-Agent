"""Twilio SMS webhook form fields + REST send body."""
from __future__ import annotations

from pydantic import BaseModel, Field


class TwilioSmsWebhook(BaseModel):
    model_config = {"extra": "ignore"}

    From: str = ""
    To: str = ""
    Body: str = ""
    MessageSid: str = ""
    NumMedia: str = "0"
    extra: dict = Field(default_factory=dict)


class TwilioSmsSendBody(BaseModel):
    model_config = {"extra": "ignore"}

    From: str
    To: str
    Body: str
