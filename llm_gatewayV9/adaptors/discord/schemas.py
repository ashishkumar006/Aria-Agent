"""Discord Gateway MESSAGE_CREATE + REST send payloads.

extra="ignore": Discord ships many more fields (embeds, flags, nonce…).
Snowflake ids stay strings (64-bit overflow in float consumers).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DiscordAuthor(BaseModel):
    model_config = {"extra": "ignore"}

    id: str = ""
    username: str = ""
    bot: bool = False


class DiscordMessageRef(BaseModel):
    model_config = {"extra": "ignore"}

    message_id: str = ""
    channel_id: str = ""


class DiscordMessageCreate(BaseModel):
    model_config = {"extra": "ignore"}

    id: str = ""
    channel_id: str = ""
    guild_id: str = ""
    content: str = ""
    timestamp: str = ""
    author: DiscordAuthor = Field(default_factory=DiscordAuthor)
    mentions: list[dict[str, Any]] = Field(default_factory=list)
    message_reference: DiscordMessageRef = Field(default_factory=DiscordMessageRef)


class DiscordSendBody(BaseModel):
    model_config = {"extra": "ignore"}

    content: str
    message_reference: dict[str, str] = Field(default_factory=dict)
