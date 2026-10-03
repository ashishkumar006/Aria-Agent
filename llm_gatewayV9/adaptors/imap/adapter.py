"""IMAP/SMTP fallback adaptor (RFC-standard mail). LIVE send + poll (P2).

For mailboxes without Gmail OAuth: app passwords, one-shot IMAP poll for
inbound, SMTP_SSL submission for outbound, careful attachment encoding.
Boring, standard, and exactly what you want when OAuth is unavailable.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

MAX_ATTACH_BYTES = 5_000_000  # per-attachment cap for SMTP sends

from ..base import BaseAdaptor, Capability, NotIntegrated
from ..envelope import ChannelMessage, TrustLevel
from ..trust import classify
from .schemas import ImapMessage


class ImapSmtpAdaptor(BaseAdaptor):
    name = "imap"
    capabilities = Capability(send_text=True, send_media=True, receive=True)

    def required_keys(self) -> list[str]:
        return ["IMAP_HOST", "IMAP_USER", "IMAP_PASS",
                "SMTP_HOST", "SMTP_USER", "SMTP_PASS"]

    def _cfg(self) -> dict[str, str]:
        import os
        cfg = {k: (os.getenv(k) or "").strip() for k in self.required_keys()}
        missing = [k for k, v in cfg.items() if not v]
        if missing:
            from ..base import NotConfigured
            raise NotConfigured(f"adaptor 'imap' missing: {','.join(missing)}")
        return cfg

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None,
             subject: str = "Aria"):
        # LIVE: SMTP_SSL submission (RFC 2045/2231 encoding via EmailMessage).
        import os
        import smtplib
        import ssl
        from email.message import EmailMessage
        from email.utils import make_msgid
        from ..envelope import ChannelReply
        cfg = self._cfg()
        port = int(os.getenv("SMTP_PORT", "465") or 465)
        msg = EmailMessage()
        msg["From"] = cfg["SMTP_USER"]
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(domain="aria-gateway")
        if thread_id:
            msg["In-Reply-To"] = thread_id
            msg["References"] = thread_id
        msg.set_content(text)
        skipped_attachments: list[str] = []
        for m in media or []:
            if not isinstance(m, dict) or not m.get("path"):
                continue
            try:
                size = Path(m["path"]).stat().st_size
            except OSError:
                continue
            if size > MAX_ATTACH_BYTES:
                # Never silently truncate: oversized files are skipped and
                # named in the reply so the caller can split or link them.
                skipped_attachments.append(
                    f"{m.get('name') or Path(m['path']).name} "
                    f"({size} bytes > {MAX_ATTACH_BYTES} cap)")
                continue
            try:
                with open(m["path"], "rb") as f:
                    data = f.read()
                msg.add_attachment(
                    data, maintype="application", subtype="octet-stream",
                    filename=str(m.get("name") or Path(m["path"]).name))
            except OSError:
                continue
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg["SMTP_HOST"], port, context=ctx,
                                  timeout=30) as s:
                s.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
                s.send_message(msg)
        except (OSError, smtplib.SMTPException) as e:
            raise RuntimeError(f"smtp error: {type(e).__name__}: {e}") from None
        raw: dict[str, Any] = {"to": to}
        if skipped_attachments:
            # Surfaced by channels_api as a top-level "warning" (ChannelReply
            # carries no warning field by design — raw is the side channel).
            raw["warning"] = ("attachments skipped over "
                              f"{MAX_ATTACH_BYTES}-byte cap: "
                              + "; ".join(skipped_attachments))
        return ChannelReply(ok=True, channel=self.name,
                            msg_id=str(msg["Message-ID"]), raw=raw)

    def poll(self, *, mailbox: str = "INBOX", limit: int = 10,
             unseen_only: bool = True) -> list[dict[str, Any]]:
        """One-shot IMAP poll returning RFC822-parsed dicts (normalize each
        with normalize()). Operator-run loops call this; no IDLE daemon yet."""
        import imaplib
        import os
        cfg = self._cfg()
        port = int(os.getenv("IMAP_PORT", "993") or 993)
        try:
            box = imaplib.IMAP4_SSL(cfg["IMAP_HOST"], port)
            box.login(cfg["IMAP_USER"], cfg["IMAP_PASS"])
        except (OSError, imaplib.IMAP4.error) as e:
            raise RuntimeError(f"imap login failed: {type(e).__name__}") from None
        try:
            typ, _ = box.select(f'"{mailbox}"', readonly=True)
            if typ != "OK":
                raise RuntimeError(f"imap select failed: {mailbox}")
            crit = "(UNSEEN)" if unseen_only else "ALL"
            typ, data = box.search(None, crit)
            if typ != "OK":
                return []
            out = []
            for num in (data[0].split() or [])[-max(1, limit):]:
                typ, msg_data = box.fetch(num, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                if isinstance(raw, tuple):
                    raw = raw[1]
                try:
                    import email as _email
                    from email import policy as _policy
                    parsed = _email.message_from_bytes(raw, policy=_policy.default)
                    out.append({
                        "message-id": str(parsed.get("Message-ID", "")),
                        "from": str(parsed.get("From", "")),
                        "to": str(parsed.get("To", "")),
                        "subject": str(parsed.get("Subject", "")),
                        "date": str(parsed.get("Date", "")),
                        "snippet": str(parsed.get_body(preferencelist=("plain",))
                                       .get_content()[:500]
                                       if parsed.get_body(preferencelist=("plain",))
                                       else ""),
                    })
                except Exception:
                    continue
            return out
        finally:
            try:
                box.logout()
            except Exception:
                pass

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        payload = dict(payload or {})
        # Accept poll() output shape too (hyphenated RFC header names).
        if "message-id" in payload and "message_id" not in payload:
            payload["message_id"] = payload["message-id"]
        m = ImapMessage.model_validate(payload)
        sender = m.From or m.from_
        subject = m.subject or m.Subject
        mid = m.message_id or m.messageId
        return ChannelMessage(
            channel=self.name, sender_id=sender,
            chat_id=m.thread or mid,
            text=(subject + "\n" + m.snippet).strip(),
            msg_id=mid, ts=m.ts or time.time(),
            trust_level=classify(self.name, sender), raw=payload)
