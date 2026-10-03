"""Channel adaptors: every messaging surface speaks one interface.

An adaptor translates between a third-party channel (Telegram, WhatsApp,
Gmail, ...) and the gateway's typed envelopes. The agent never touches a
channel SDK or secret — it calls POST /v1/channels/{name}/send and the
adaptor injects credentials server-side.

Scaffold rule (P0-P2): every adaptor implements capabilities() truthfully,
send() raises NotConfigured without keys, normalize() parses the channel's
real payload shape, and verify_webhook() holds the real verification logic
(pure code, testable offline). No live network calls until integration.
"""
from __future__ import annotations

from .base import BaseAdaptor, Capability, NotConfigured, NotIntegrated, AdaptorError
from .envelope import Attachment, ChannelMessage, ChannelReply, TrustLevel
from . import registry as registry

__all__ = [
    "BaseAdaptor", "Capability", "NotConfigured", "NotIntegrated", "AdaptorError",
    "Attachment", "ChannelMessage", "ChannelReply", "TrustLevel", "registry",
]
