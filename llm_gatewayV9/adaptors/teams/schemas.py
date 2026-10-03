"""Bot Framework activity shapes (Teams)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TeamsFrom(BaseModel):
    model_config = {"extra": "ignore"}

    id: str = ""
    name: str = ""


class TeamsConversation(BaseModel):
    model_config = {"extra": "ignore"}

    id: str = ""
    tenantId: str = ""


class TeamsActivity(BaseModel):
    model_config = {"extra": "ignore"}

    type: str = ""
    id: str = ""
    text: str = ""
    speak: str = ""
    replyToId: str = ""
    serviceUrl: str = ""
    channelId: str = ""
    entities: list[dict[str, Any]] = Field(default_factory=list)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    value: dict[str, Any] = Field(default_factory=dict)
    From: TeamsFrom = Field(default_factory=TeamsFrom, alias="from")
    recipient: TeamsFrom = Field(default_factory=TeamsFrom)
    conversation: TeamsConversation = Field(default_factory=TeamsConversation)
