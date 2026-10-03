"""LINE Messaging API webhook payloads."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LineSource(BaseModel):
    model_config = {"extra": "ignore"}

    type: str = ""
    userId: str = ""
    groupId: str = ""
    roomId: str = ""


class LineMessage(BaseModel):
    model_config = {"extra": "ignore"}

    id: str = ""
    type: str = ""
    text: str = ""


class LineEvent(BaseModel):
    model_config = {"extra": "ignore"}

    type: str = ""
    replyToken: str = ""
    source: LineSource = Field(default_factory=LineSource)
    message: LineMessage = Field(default_factory=LineMessage)
    timestamp: int = 0
    webhookEventId: str = ""


class LineWebhook(BaseModel):
    model_config = {"extra": "ignore"}

    destination: str = ""
    events: list[LineEvent] = Field(default_factory=list)
