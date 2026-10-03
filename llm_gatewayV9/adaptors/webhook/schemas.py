"""Generic webhook payload shapes (accepts several common layouts)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class WebhookInbound(BaseModel):
    model_config = {"extra": "ignore"}

    sender_id: str = ""
    chat_id: str = ""
    text: str = ""
    message: str = ""
    msg_id: str = ""
    id: str = ""
    ts: float = 0.0
    to: str = ""
    sender: str = ""
