"""Telegram adaptor (Bot API). LIVE.

Setup: message @BotFather → /newbot → paste token as TELEGRAM_BOT_TOKEN.
Optional TELEGRAM_CHAT_ID allowlist: only that chat is treated as paired.
"""
from __future__ import annotations

from typing import Any

from .. import http as _http
from ..base import BaseAdaptor, Capability
from ..envelope import ChannelMessage, TrustLevel
from ..trust import classify
from .schemas import TelegramUpdate


class TelegramAdaptor(BaseAdaptor):
    name = "telegram"
    capabilities = Capability(send_text=True, send_media=True, receive=True)

    def required_keys(self) -> list[str]:
        return ["TELEGRAM_BOT_TOKEN"]

    def _api(self, method: str, payload: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
        token = self._need("TELEGRAM_BOT_TOKEN")
        try:
            r = _http.post(f"https://api.telegram.org/bot{token}/{method}",
                           json=payload, timeout=timeout)
        except Exception as e:
            # Never leak the request URL: it embeds the bot token.
            raise RuntimeError(f"telegram transport error: {type(e).__name__}") from None
        if r.status_code >= 400:
            try:
                desc = r.json().get("description", "")
            except ValueError:
                desc = ""
            raise RuntimeError(f"telegram error {r.status_code}: {desc or 'request rejected'}")
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram error: {data.get('description', 'unknown')}")
        return data

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None):
        from ..envelope import ChannelReply
        data = self._api("sendMessage", {"chat_id": to, "text": text[:4096]})
        msg = (data.get("result") or {})
        return ChannelReply(ok=True, channel=self.name,
                            msg_id=str(msg.get("message_id", "")), raw=data)

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        upd = TelegramUpdate.model_validate(payload)
        msg = upd.effective()
        if msg is None:  # malformed update: envelope with empty fields, never crash
            return ChannelMessage(channel=self.name, raw=payload,
                                  trust_level=TrustLevel.untrusted)
        sender = str(msg.from_.id or "")
        return ChannelMessage(
            channel=self.name, sender_id=sender,
            chat_id=str(msg.chat.id or ""), text=msg.text or msg.caption,
            msg_id=str(msg.message_id or ""), ts=float(msg.date or 0),
            trust_level=classify(self.name, sender), raw=payload)
