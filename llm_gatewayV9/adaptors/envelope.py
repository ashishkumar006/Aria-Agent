"""Typed channel envelopes (glc pattern: adapters never hand raw payloads up).

Every inbound message from any channel is normalised to a ChannelMessage
carrying a TrustLevel BEFORE anything else (policy, ledger, agent) sees it.
Outbound traffic uses ChannelReply. Raw provider payloads stay inside the
adaptor that parsed them.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class TrustLevel(str, Enum):
    """Who vouches for this message. Policy defaults: owner allow, paired
    allow (rate-limited), untrusted deny-all-tools."""
    owner = "owner"          # paired owner device / operator allowlist
    paired = "paired"        # known, paired sender (user chat, own number)
    untrusted = "untrusted"  # everyone/everything else (default)


class Attachment(BaseModel):
    kind: Literal["image", "audio", "video", "file"] = "file"
    url: str | None = None       # remote URL (download via gateway, SSRF-guarded)
    data_b64: str | None = None  # small inline payloads
    mime: str | None = None
    name: str | None = None


class ChannelMessage(BaseModel):
    channel: str
    sender_id: str = ""
    chat_id: str = ""
    text: str = ""
    attachments: list[Attachment] = Field(default_factory=list)
    msg_id: str = ""
    ts: float = 0.0
    trust_level: TrustLevel = TrustLevel.untrusted
    thread_id: str | None = None   # Discord thread / Slack thread_ts / ...
    raw: dict[str, Any] = Field(default_factory=dict)  # original payload (audit only)


class ChannelReply(BaseModel):
    ok: bool = True
    channel: str = ""
    msg_id: str = ""
    thread_id: str | None = None  # set by threaded sends (gmail, discord…)
    error: str | None = None
    cost_hint_usd: float = 0.0  # Twilio/Meta per-message cost when known
    raw: dict[str, Any] = Field(default_factory=dict)
