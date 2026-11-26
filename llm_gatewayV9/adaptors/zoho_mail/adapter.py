"""Zoho Mail adaptor (REST + OAuth2).

Outbound send + inbound read delegate to integrations.zoho_mail (single
implementation, shared keys). No push webhook: Zoho Mail has no simple inbound
webhook, so reads are pull-based (query) — a poller can land later using the
same `read`/`normalize` pair the Gmail adaptor uses.

Scopes: ZohoMail.accounts.READ + ZohoMail.messages.READ + .CREATE. The access
token expires hourly; the refresh triple renews it (zoho_mail_oauth_setup.py
mints the refresh token once).
"""
from __future__ import annotations

import time
from typing import Any

from ..base import BaseAdaptor, Capability
from ..envelope import ChannelMessage, ChannelReply
from ..trust import classify


class ZohoMailAdaptor(BaseAdaptor):
    name = "zoho_mail"
    capabilities = Capability(send_text=True, send_media=True, receive=True)

    def required_keys(self) -> list[str]:
        return ["ZOHO_TOKEN"]

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None):
        # Single implementation lives in integrations.zoho_mail (shared keys).
        from integrations import zoho_mail as _z
        from adaptors.base import NotConfigured as _NC
        subject = "Aria"
        atts: list[dict] = []
        for m in media or []:
            if isinstance(m, dict) and m.get("subject"):
                subject = str(m["subject"])[:200]
            if isinstance(m, dict) and m.get("data_b64"):
                atts.append({"name": str(m.get("name") or "attachment"),
                             "data_b64": m.get("data_b64")})
        try:
            res = _z.send_email(to=to, subject=subject, body=text,
                                thread_id=thread_id,
                                attachments=atts or None)
        except _z._MissingKey:
            raise _NC("adaptor 'zoho_mail' is not configured")
        if not res.get("ok"):
            err = str(res.get("error", "send failed"))
            if "401" in err or "expired" in err.lower():
                raise PermissionError("zoho token expired (401) — call refresh()")
            if "not set" in err.lower():
                raise _NC("adaptor 'zoho_mail' is not configured")
            raise RuntimeError(err)
        return ChannelReply(ok=True, channel=self.name,
                            msg_id=str(res.get("id", "")),
                            thread_id=thread_id,
                            raw={"from": res.get("from")})

    def read(self, *, query: str = "", max_n: int = 5) -> dict[str, Any]:
        from integrations import zoho_mail as _z
        return _z.query(api_method="list", query=query, max_results=max_n)

    def refresh(self) -> dict[str, Any]:
        from integrations import zoho_mail as _z
        return _z.refresh()

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        # Accepts a read()/query() row (list entry or message detail).
        sender = str(payload.get("fromAddress") or payload.get("from") or "")
        subject = str(payload.get("subject") or "")
        snippet = str(payload.get("snippet") or payload.get("content") or "")
        mid = str(payload.get("messageId") or payload.get("id") or "")
        return ChannelMessage(
            channel=self.name, sender_id=sender, chat_id=sender,
            text=(snippet or subject), msg_id=mid,
            ts=time.time(), trust_level=classify(self.name, sender),
            raw=payload)