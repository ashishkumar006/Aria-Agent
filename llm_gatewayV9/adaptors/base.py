"""BaseAdaptor: the one interface every channel implements."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .envelope import ChannelMessage, ChannelReply


class AdaptorError(Exception):
    """Base class for adaptor failures (auth, rate-limit, provider 5xx)."""


class NotConfigured(AdaptorError):
    """Raised when required credentials are absent. Never leaks key names."""


class NotIntegrated(AdaptorError):
    """Raised when the adaptor is scaffolded but live traffic is not wired
    yet (integration phase). Distinct from NotConfigured: keys may exist,
    but outbound calls are deliberately disabled."""


@dataclass(frozen=True)
class Capability:
    """What an adaptor can do. All flags default False — adaptors opt in."""
    send_text: bool = False
    send_media: bool = False
    receive: bool = False          # inbound path implemented
    threads: bool = False          # reply-in-thread supported
    reactions: bool = False
    voice_calls: bool = False      # real-time voice (Twilio Voice only)
    needs_public_url: bool = False  # requires a public HTTPS endpoint for webhooks
    needs_verification: bool = False  # business verification / approval (slow)


class BaseAdaptor:
    """Subclass per channel. All methods are sync; routes run them in a
    worker thread. Network I/O uses httpx with short timeouts."""

    name: str = "base"
    capabilities: Capability = Capability()

    # ── config ──────────────────────────────────────────────────────────
    def required_keys(self) -> list[str]:
        """Env var names this adaptor needs. Used by /v1/channels + check_keys."""
        return []

    def is_configured(self) -> bool:
        import os
        return all(os.getenv(k) for k in self.required_keys())

    def _need(self, key: str) -> str:
        import os
        v = (os.getenv(key) or "").strip().strip('"').strip("'")
        if not v:
            raise NotConfigured(f"adaptor '{self.name}' is not configured")
        return v

    # ── traffic ─────────────────────────────────────────────────────────
    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None) -> ChannelReply:
        """Send a message. Override per channel. Must raise NotConfigured
        when credentials are absent (never attempt unauthenticated calls)."""
        raise NotImplementedError

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        """Parse the channel's real inbound payload shape into an envelope.
        Pure function — no network, fully unit-testable with fixtures."""
        raise NotImplementedError

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        """Verify an inbound webhook (HMAC/signature/token). Pure function."""
        return False

    def health(self) -> dict[str, Any]:
        """Cheap, credential-free status + expensive live probe (never raises)."""
        return {"name": self.name, "configured": self.is_configured(),
                "capabilities": self.capabilities.__dict__}

    def cost_hint_usd(self, *, media: bool = False) -> float:
        """Marginal send cost for spend attribution (0.0 = free)."""
        return 0.0
