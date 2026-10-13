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

    def _api_multipart(self, method: str, *, fields: dict[str, Any],
                       filename: str, content: bytes,
                       field_name: str = "document",
                       timeout: float = 60.0) -> dict[str, Any]:
        """POST a file to the Bot API.

        Telegram's upload methods take multipart/form-data and reject a JSON
        body outright, so `_api` cannot be reused for them.
        """
        token = self._need("TELEGRAM_BOT_TOKEN")
        try:
            r = _http.post(
                f"https://api.telegram.org/bot{token}/{method}",
                files={field_name: (filename or "file.bin", content,
                                    "application/octet-stream")},
                data=fields, timeout=timeout)
        except Exception as e:
            raise RuntimeError(
                f"telegram transport error: {type(e).__name__}") from None
        if r.status_code >= 400:
            try:
                desc = r.json().get("description", "")
            except ValueError:
                desc = ""
            raise RuntimeError(
                f"telegram error {r.status_code}: {desc or 'request rejected'}")
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(
                f"telegram error: {data.get('description', 'unknown')}")
        return data

    def download(self, file_id: str, timeout: float = 60.0) -> tuple[bytes, str]:
        """Fetch the bytes behind a Telegram `file_id`.

        Two hops: getFile returns a `file_path` scoped to this bot, and the
        bytes live at /file/bot<token>/<file_path>. Returns (bytes, file_path).

        Deliberately separate from `normalize`: a download on the poll thread
        would stall the offset-advance for every other message.
        """
        token = self._need("TELEGRAM_BOT_TOKEN")
        meta = self._api("getFile", {"file_id": file_id}, timeout=timeout)
        file_path = str((meta.get("result") or {}).get("file_path") or "")
        if not file_path:
            raise RuntimeError("telegram getFile returned no file_path")
        try:
            r = _http.get(
                f"https://api.telegram.org/file/bot{token}/{file_path}",
                timeout=timeout)
        except Exception as e:
            raise RuntimeError(
                f"telegram download error: {type(e).__name__}") from None
        if r.status_code >= 400:
            raise RuntimeError(f"telegram download failed {r.status_code}")
        return r.content, file_path

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None):
        """Send text, or a document when one is attached.

        Falls back to sendMessage when the media is unusable, so a failed
        upload still delivers the words rather than nothing at all.
        """
        from ..envelope import ChannelReply

        def _reply(data: dict[str, Any]) -> ChannelReply:
            msg = (data.get("result") or {})
            return ChannelReply(ok=True, channel=self.name,
                                msg_id=str(msg.get("message_id", "")), raw=data)

        item = (media or [None])[0]
        if item:
            content = _decode_media(item)
            if content:
                data = self._api_multipart(
                    "sendDocument",
                    fields={"chat_id": to,
                            "caption": (item.get("caption") or text or "")[:1024]},
                    filename=str(item.get("filename") or "document.bin"),
                    content=content)
                return _reply(data)

        data = self._api("sendMessage", {"chat_id": to, "text": text[:4096]})
        return _reply(data)

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        upd = TelegramUpdate.model_validate(payload)
        msg = upd.effective()
        if msg is None:  # malformed update: envelope with empty fields, never crash
            return ChannelMessage(channel=self.name, raw=payload,
                                  trust_level=TrustLevel.untrusted)
        sender = str(msg.from_.id or "")
        return ChannelMessage(
            channel=self.name, sender_id=sender,
            chat_id=str(msg.chat.id or ""),
            text=msg.text or msg.caption,
            attachments=_attachments_of(msg),
            msg_id=str(msg.message_id or ""), ts=float(msg.date or 0),
            trust_level=classify(self.name, sender), raw=payload)


def _attachments_of(msg: Any) -> list[dict[str, Any]]:
    """Everything Telegram is holding for us on this message.

    Recorded as refs, not bytes: the download is a second round trip and must
    not sit on the poll thread. `kind` is what the agent's document pipeline
    branches on, so a voice note is not mistaken for a document.
    """
    out: list[dict[str, Any]] = []
    singles = (
        ("document", msg.document, "file"),
        ("audio", msg.audio, "audio"),
        ("voice", msg.voice, "audio"),
        ("video", msg.video, "video"),
        ("video_note", msg.video_note, "video"),
        ("animation", msg.animation, "video"),
        ("sticker", msg.sticker, "image"),
    )
    for _field, ref, kind in singles:
        if ref is not None and getattr(ref, "file_id", ""):
            out.append({"kind": kind, "file_id": ref.file_id,
                        "name": ref.file_name or "", "mime": ref.mime_type or "",
                        "size": int(ref.file_size or 0)})
    # Telegram hands back up to ten sizes; the last is the largest.
    if msg.photo:
        big = msg.photo[-1]
        out.append({"kind": "image", "file_id": big.file_id, "name": "",
                    "mime": big.mime_type or "image/jpeg",
                    "size": int(big.file_size or 0)})
    return out


def _decode_media(item: dict[str, Any]) -> bytes | None:
    """Bytes for an outbound attachment, from either bytes or base64.

    Returns None when there is nothing usable, which tells `send` to fall back
    to plain text rather than uploading an empty file.
    """
    raw = item.get("data") or item.get("bytes")
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    b64 = item.get("data_b64")
    if isinstance(b64, str) and b64:
        import base64
        try:
            return base64.b64decode(b64, validate=True)
        except Exception:
            return None
    return None
