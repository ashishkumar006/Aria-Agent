"""WebUI session-bus message shapes (chat page today, PWA later)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class WebUIMessage(BaseModel):
    model_config = {"extra": "ignore"}

    sender_id: str = "local-operator"
    conversation_id: str = ""
    chat_id: str = ""
    text: str = ""
    query: str = ""
    msg_id: str = ""
    ts: float = 0.0
    local: bool = True
