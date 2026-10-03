"""Local-mic transcript shapes (output of /v1/stt)."""
from __future__ import annotations

from pydantic import BaseModel


class MicTranscript(BaseModel):
    model_config = {"extra": "ignore"}

    text: str = ""
    sender_id: str = "local-operator"
    chat_id: str = "mic"
    msg_id: str = ""
    ts: float = 0.0
    lang: str = ""
    duration_s: float = 0.0
