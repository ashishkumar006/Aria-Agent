"""Matrix Client-Server wire payloads (/sync events + send body)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MatrixRelatesTo(BaseModel):
    model_config = {"extra": "ignore"}

    event_id: str = ""
    rel_type: str = ""


class MatrixContent(BaseModel):
    model_config = {"extra": "ignore"}

    body: str = ""
    msgtype: str = ""
    relates_to: MatrixRelatesTo = Field(default_factory=MatrixRelatesTo,
                                        alias="m.relates_to")


class MatrixTimelineEvent(BaseModel):
    model_config = {"extra": "ignore"}

    event_id: str = ""
    sender: str = ""
    room_id: str = ""
    origin_server_ts: int = 0
    type: str = ""
    content: MatrixContent = Field(default_factory=MatrixContent)
