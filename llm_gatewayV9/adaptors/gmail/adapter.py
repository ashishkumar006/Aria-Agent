"""Gmail adaptor (Google API + Pub/Sub later). PARTIAL (P2).

Outbound send + inbound read delegate to integrations.gmail (single
implementation, shared keys). Push delivery via Pub/Sub (watch + push
endpoint) is scaffolded for later; until then reads are pull-based.
Scopes: gmail.send + gmail.readonly. Tokens expire ~hourly; the refresh
triple renews them.
"""
from __future__ import annotations

import base64
import json as _json
import time
from typing import Any

from ..base import BaseAdaptor, Capability, NotIntegrated
from ..envelope import ChannelMessage, ChannelReply, TrustLevel
from ..trust import classify
from .schemas import GmailPushEnvelope, GmailRow


class GmailAdaptor(BaseAdaptor):
    name = "gmail"
    capabilities = Capability(send_text=True, send_media=True, receive=True)

    def required_keys(self) -> list[str]:
        return ["GMAIL_TOKEN"]

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None):
        # Single implementation lives in integrations.gmail (shared keys).
        from integrations import gmail as _g
        from adaptors.base import NotConfigured as _NC
        subject = "Aria"
        for m in media or []:
            if isinstance(m, dict) and m.get("subject"):
                subject = str(m["subject"])[:200]
        try:
            res = _g.send_email(to=to, subject=subject, body=text,
                                thread_id=thread_id)
        except _g._MissingKey:
            # Translate to the adaptor contract: missing creds, not a crash.
            raise _NC("adaptor 'gmail' is not configured")
        if not res.get("ok"):
            err = str(res.get("error", "send failed"))
            if "401" in err or "expired" in err.lower():
                raise PermissionError("gmail token expired (401) — call refresh()")
            if "not set" in err.lower():
                raise _NC("adaptor 'gmail' is not configured")
            raise RuntimeError(err)
        return ChannelReply(ok=True, channel=self.name,
                            msg_id=str(res.get("id", "")),
                            thread_id=thread_id)

    def read(self, *, query: str = "is:unread", max_n: int = 5) -> dict[str, Any]:
        from integrations import gmail as _g
        return _g.query(api_method="list", query=query, max_results=max_n)

    def refresh(self) -> dict[str, Any]:
        from integrations import gmail as _g
        return _g.refresh()

    def watch(self, *, topic: str | None = None,
              labels: list[str] | None = None) -> dict[str, Any]:
        # LIVE: Gmail push registration — watch(userId, labelIds, topicName).
        # Needs a Google Cloud Pub/Sub topic + push subscription pointing at
        # POST /v1/hooks/gmail on a public URL (operator-configured).
        import os
        from .. import http as _http
        topic = topic or (os.getenv("GMAIL_PUBSUB_TOPIC") or "").strip()
        if not topic:
            from ..base import NotConfigured
            raise NotConfigured("gmail watch needs GMAIL_PUBSUB_TOPIC")
        try:
            r = _http.post(
                "https://gmail.googleapis.com/gmail/v1/users/me/watch",
                headers={"Authorization": f"Bearer {self._need('GMAIL_TOKEN')}"},
                json={"topicName": topic,
                      "labelIds": labels or ["INBOX"]}, timeout=30.0)
        except Exception as e:
            raise RuntimeError(f"gmail watch error: {type(e).__name__}") from None
        if r.status_code == 401:
            raise PermissionError("gmail token expired (401) — call refresh()")
        if r.status_code >= 400:
            try:
                detail = r.json().get("error", {}).get("message", "")
            except ValueError:
                detail = ""
            raise RuntimeError(f"gmail watch error {r.status_code}: {detail or 'request rejected'}")
        try:
            data = r.json()
        except ValueError:
            data = {}
        return {"ok": True, "history_id": data.get("historyId"),
                "expiration": data.get("expiration")}

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        # Accepts EITHER a Pub/Sub push envelope OR a read() row.
        if "message" in payload and isinstance(payload["message"], dict):
            env = GmailPushEnvelope.model_validate(payload)
            try:
                inner = _json.loads(
                    base64.b64decode(env.message.data).decode())
            except Exception:
                inner = {}
            sender = str(inner.get("emailAddress", ""))
            return ChannelMessage(
                channel=self.name, sender_id=sender, chat_id=sender,
                text="", msg_id=env.message.messageId,
                ts=time.time(), trust_level=classify(self.name, sender),
                raw=payload)
        row = GmailRow.model_validate(payload)
        sender = row.sender_id or row.sender or row.From
        thread = row.thread_id or row.threadId or row.id
        return ChannelMessage(
            channel=self.name, sender_id=sender, chat_id=thread,
            text=row.snippet or row.subject or row.Subject,
            thread_id=thread or None, msg_id=row.id,
            ts=time.time(), trust_level=classify(self.name, sender),
            raw=payload)
