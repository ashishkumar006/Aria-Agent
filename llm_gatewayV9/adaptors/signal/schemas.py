"""signal-cli JSON-RPC receive/send payloads."""
from __future__ import annotations

from pydantic import BaseModel, Field


class SignalGroupInfo(BaseModel):
    model_config = {"extra": "ignore"}

    groupId: str = ""


class SignalDataMessage(BaseModel):
    model_config = {"extra": "ignore"}

    message: str = ""
    groupInfo: SignalGroupInfo = Field(default_factory=SignalGroupInfo)


class SignalEnvelope(BaseModel):
    model_config = {"extra": "ignore"}

    source: str = ""
    sourceUuid: str = ""
    timestamp: int = 0
    dataMessage: SignalDataMessage = Field(default_factory=SignalDataMessage)


class SignalReceive(BaseModel):
    model_config = {"extra": "ignore"}

    method: str = ""
    envelope: SignalEnvelope = Field(default_factory=SignalEnvelope)
