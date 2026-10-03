"""Slack Events API payloads (app_mention / message events)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class SlackEvent(BaseModel):
    model_config = {"extra": "ignore"}

    type: str = ""
    user: str = ""
    channel: str = ""
    text: str = ""
    ts: str = ""
    event_ts: str = ""
    thread_ts: str = ""
    client_msg_id: str = ""


class SlackEnvelope(BaseModel):
    model_config = {"extra": "ignore"}

    type: str = ""
    challenge: str = ""
    event: SlackEvent = Field(default_factory=SlackEvent)
