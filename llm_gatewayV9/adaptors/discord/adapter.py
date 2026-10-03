"""Discord adaptor (Bot API + Gateway WebSocket). SEND LIVE; inbound WebSocket
listener remains a deployment concern (P1).

Setup: developer portal → application → Bot → token → DISCORD_BOT_TOKEN.
Invite with applications.commands scope for slash commands (registration
has quirks: global commands take ~1h to propagate; use guild commands
while developing). Inbound runs over the Gateway WebSocket (op 10 hello →
heartbeat → identify → dispatch MESSAGE_CREATE); REST covers send.
Free tier: yes.
"""
from __future__ import annotations

from typing import Any

from ..base import BaseAdaptor, Capability, NotIntegrated
from ..envelope import ChannelMessage, TrustLevel
from ..trust import classify
from .schemas import DiscordMessageCreate

GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"


class DiscordAdaptor(BaseAdaptor):
    name = "discord"
    capabilities = Capability(send_text=True, send_media=True, receive=True,
                              threads=True, reactions=True)

    def required_keys(self) -> list[str]:
        return ["DISCORD_BOT_TOKEN"]

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None):
        # LIVE: REST POST /channels/{to}/messages (+ message_reference).
        # (The Gateway WebSocket listener for inbound traffic is a separate
        # deployment concern — see README. Sends need no socket.)
        from .. import http as _http
        from ..envelope import ChannelReply
        # Credential check stays OUTSIDE try: missing creds must surface as
        # NotConfigured, never masked as a transport error.
        token = self._need("DISCORD_BOT_TOKEN")
        body: dict[str, Any] = {"content": text[:2000]}
        if thread_id:
            body["message_reference"] = {"message_id": str(thread_id)}
        try:
            r = _http.post(f"https://discord.com/api/v10/channels/{to}/messages",
                           headers={"Authorization": f"Bot {token}"},
                           json=body, timeout=20.0)
        except Exception as e:
            raise RuntimeError(f"discord transport error: {type(e).__name__}") from None
        if r.status_code >= 400:
            try:
                detail = r.json().get("message", "")
            except ValueError:
                detail = ""
            raise RuntimeError(f"discord error {r.status_code}: {detail or 'request rejected'}")
        data = r.json()
        return ChannelReply(ok=True, channel=self.name,
                            msg_id=str(data.get("id", "")), raw=data)

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        m = DiscordMessageCreate.model_validate(payload)
        return ChannelMessage(
            channel=self.name, sender_id=m.author.id,
            chat_id=m.channel_id, text=m.content,
            thread_id=m.message_reference.message_id or None,
            msg_id=m.id, ts=self._snowflake_ts(m.id),
            trust_level=classify(self.name, m.author.id), raw=payload)

    @staticmethod
    def _snowflake_ts(snowflake: str) -> float:
        try:
            return ((int(snowflake) >> 22) + 1420070400000) / 1000.0
        except (TypeError, ValueError):
            return 0.0
