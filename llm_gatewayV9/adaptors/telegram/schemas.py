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


class TelegramFileRef(BaseModel):
    """A file Telegram is holding for us.

    Telegram never gives us bytes with the update - it gives a `file_id`,
    which is only meaningful to this bot, and the bytes have to be fetched
    separately via getFile. Modelling the ref (rather than the bytes) is what
    Telegram's API forces, and it is why `normalize` can record that a file
    arrived without downloading it on the poll thread.
    """
    model_config = {"extra": "ignore"}

    file_id: str = ""
    file_unique_id: str = ""
    file_name: str = ""
    mime_type: str = ""
    file_size: int = 0
    duration: int = 0


class TelegramPhotoSize(TelegramFileRef):
    width: int = 0
    height: int = 0


class TelegramMessage(BaseModel):
    model_config = {"extra": "ignore"}

    message_id: int | str = ""
    date: int = 0
    from_: TelegramUser = Field(default_factory=TelegramUser, alias="from")
    chat: TelegramChat = Field(default_factory=TelegramChat)
    text: str = ""
    caption: str = ""
    # Media. These were silently dropped by extra="ignore" until now, which is
    # why sending a document produced an empty message the agent refused.
    document: TelegramFileRef | None = None
    audio: TelegramFileRef | None = None
    voice: TelegramFileRef | None = None
    video: TelegramFileRef | None = None
    video_note: TelegramFileRef | None = None
    animation: TelegramFileRef | None = None
    sticker: TelegramFileRef | None = None
    photo: list[TelegramPhotoSize] = Field(default_factory=list)


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
