"""IMAP/SMTP message shapes (parsed RFC822 headers + snippet)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class ImapMessage(BaseModel):
    model_config = {"extra": "ignore"}

    From: str = ""
    from_: str = Field(default="", alias="from")
    to: str = ""
    subject: str = ""
    Subject: str = ""
    date: str = ""
    message_id: str = ""
    messageId: str = ""
    thread: str = ""
    snippet: str = ""
    ts: float = 0.0
