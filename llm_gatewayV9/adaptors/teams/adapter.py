"""Microsoft Teams adaptor (Bot Framework + Azure AD). SEND LIVE.

Setup: Azure portal → app registration (TEAMS_APP_ID / TEAMS_APP_PASSWORD /
TEAMS_TENANT) → bot manifest + tenant install. Token flow is OAuth2
client-credentials against login.microsoftonline.com; activities arrive at
the messaging endpoint. Heaviest setup on this list — do it last. Free
within a tenant.
"""
from __future__ import annotations

import re
import time
from typing import Any

from ..base import BaseAdaptor, Capability, NotIntegrated
from ..envelope import ChannelMessage, TrustLevel
from ..trust import classify
from .schemas import TeamsActivity


class TeamsAdaptor(BaseAdaptor):
    name = "teams"
    capabilities = Capability(send_text=True, send_media=False, receive=True,
                              threads=True, needs_public_url=True)

    def required_keys(self) -> list[str]:
        return ["TEAMS_APP_ID", "TEAMS_APP_PASSWORD", "TEAMS_TENANT"]

    def __init__(self):
        # OAuth2 token cache (token, expiry epoch). Registry-held singleton.
        self._teams_token: tuple[str | None, float] = (None, 0.0)

    def _token(self) -> str:
        # OAuth2 client-credentials; cached in-process with clock-skew margin.
        import time as _t
        from .. import http as _http
        tok, exp = getattr(self, "_teams_token", (None, 0.0))
        if tok and exp - 60 > _t.time():
            return tok
        tenant = self._need("TEAMS_TENANT")
        data = {
            "grant_type": "client_credentials",
            "client_id": self._need("TEAMS_APP_ID"),
            "client_secret": self._need("TEAMS_APP_PASSWORD"),
            "scope": "https://api.botframework.com/.default",
        }
        try:
            r = _http.post(
                f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                data=data, timeout=20.0)
        except Exception as e:
            raise RuntimeError(f"teams token error: {type(e).__name__}") from None
        if r.status_code >= 400:
            raise RuntimeError(f"teams token error {r.status_code}: check TEAMS_APP_ID/PASSWORD/TENANT")
        try:
            body = r.json()
            tok = body.get("access_token", "")
        except ValueError:
            tok = ""
        if not tok:
            raise RuntimeError("teams token error: no access_token in response")
        self._teams_token = (tok, _t.time() + int(body.get("expires_in", 3600) or 3600))
        return tok

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None,
             service_url: str | None = None):
        # LIVE: POST {serviceUrl}/v3/conversations/{to}/activities.
        # service_url comes from a prior inbound activity; without it there
        # is no address to reply to — pass service_url explicitly.
        from .. import http as _http
        from ..envelope import ChannelReply
        if not service_url:
            media_svc = next((m.get("service_url") for m in (media or [])
                              if isinstance(m, dict) and m.get("service_url")), None)
            service_url = media_svc or ""
        if not service_url:
            raise ValueError("teams send needs service_url (from the inbound activity)")
        try:
            r = _http.post(
                f"{service_url.rstrip('/')}/v3/conversations/{to}/activities",
                headers={"Authorization": f"Bearer {self._token()}"},
                json={"type": "message", "text": text[:10000],
                      **({"replyToId": thread_id} if thread_id else {})},
                timeout=20.0)
        except Exception as e:
            raise RuntimeError(f"teams transport error: {type(e).__name__}") from None
        if r.status_code >= 400:
            try:
                detail = str(r.json())[:200]
            except ValueError:
                detail = ""
            raise RuntimeError(f"teams error {r.status_code}: {detail or 'request rejected'}")
        try:
            data = r.json()
        except ValueError:
            data = {}
        return ChannelReply(ok=True, channel=self.name,
                            msg_id=str(data.get("id", "")), raw=data)

    @staticmethod
    def _strip_mentions(text: str) -> str:
        return re.sub(r"<at>.*?</at>", "", text or "").strip()

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        act = TeamsActivity.model_validate(payload)
        if act.type and act.type != "message":
            return ChannelMessage(channel=self.name, raw=payload,
                                  trust_level=TrustLevel.untrusted)
        return ChannelMessage(
            channel=self.name, sender_id=act.From.id,
            chat_id=act.conversation.id,
            text=self._strip_mentions(act.text) or act.speak,
            thread_id=act.replyToId or None,
            msg_id=act.id, ts=time.time(),
            trust_level=classify(self.name, act.From.id), raw=payload)
