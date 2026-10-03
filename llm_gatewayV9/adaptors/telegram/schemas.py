"""Telegram Bot API wire payloads (inbound Update + outbound send).

extra="ignore": Telegram adds fields without warning; unknown keys must
never break normalization.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TelegramUser(BaseModel):
    model_config = {"extra": "ignore"}

    id: int | str = ""
    username: str = ""


class TelegramChat(BaseModel):
    model_config = {"extra": "ignore"}

    id: int | str = ""
    type: str = ""


class TelegramMessage(BaseModel):
    model_config = {"extra": "ignore"}

    message_id: int | str = ""
    date: int = 0
    from_: TelegramUser = Field(default_factory=TelegramUser, alias="from")
    chat: TelegramChat = Field(default_factory=TelegramChat)
    text: str = ""
    caption: str = ""


class TelegramUpdate(BaseModel):
    model_config = {"extra": "ignore"}

    update_id: int = 0
    message: TelegramMessage | None = None
    edited_message: TelegramMessage | None = None

    def effective(self) -> TelegramMessage | None:
        return self.message or self.edited_message


class TelegramSendBody(BaseModel):
    model_config = {"extra": "ignore"}

    chat_id: str
    text: str
    extra: dict[str, Any] = Field(default_factory=dict)
