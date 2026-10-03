"""WhatsApp Cloud API webhook shapes (entry → changes → messages)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class WaText(BaseModel):
    model_config = {"extra": "ignore"}

    body: str = ""


class WaMessage(BaseModel):
    model_config = {"extra": "ignore"}

    from_: str = Field(default="", alias="from")
    id: str = ""
    timestamp: str = ""
    type: str = ""
    text: WaText = Field(default_factory=WaText)


class WaValue(BaseModel):
    model_config = {"extra": "ignore"}

    messaging_product: str = ""
    messages: list[WaMessage] = Field(default_factory=list)
    statuses: list[dict] = Field(default_factory=list)


class WaChange(BaseModel):
    model_config = {"extra": "ignore"}

    value: WaValue = Field(default_factory=WaValue)
    field: str = ""


class WaEntry(BaseModel):
    model_config = {"extra": "ignore"}

    id: str = ""
    changes: list[WaChange] = Field(default_factory=list)


class WaWebhook(BaseModel):
    model_config = {"extra": "ignore"}

    object: str = ""
    entry: list[WaEntry] = Field(default_factory=list)
