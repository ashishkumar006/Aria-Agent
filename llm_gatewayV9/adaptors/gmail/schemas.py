"""Gmail payloads: Pub/Sub push envelope + read() row shape."""
from __future__ import annotations

from pydantic import BaseModel, Field


class GmailPushMessage(BaseModel):
    model_config = {"extra": "ignore"}

    data: str = ""
    messageId: str = ""


class GmailPushEnvelope(BaseModel):
    model_config = {"extra": "ignore"}

    message: GmailPushMessage = Field(default_factory=GmailPushMessage)
    subscription: str = ""


class GmailRow(BaseModel):
    """What integrations.gmail.read() rows look like (also accepted here)."""

    model_config = {"extra": "ignore"}

    id: str = ""
    thread_id: str = ""
    threadId: str = ""
    sender_id: str = ""
    sender: str = ""
    From: str = ""
    subject: str = ""
    Subject: str = ""
    snippet: str = ""
