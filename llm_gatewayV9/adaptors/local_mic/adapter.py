"""Local-mic adaptor (OS microphone + gateway voice services). POINTER (P3).

There is no "mic SDK": capture happens wherever the microphone lives
(browser getUserMedia today, OS/device later) and audio is POSTed to the
gateway's /v1/stt; replies render via /v1/tts. This adaptor documents that
split and normalises transcripts into envelopes. OS audio permissions
(TCC on macOS, per-app mic consent) are an operator concern. Free: yes.
"""
from __future__ import annotations

import time
from typing import Any

from ..base import BaseAdaptor, Capability
from ..envelope import ChannelMessage, ChannelReply, TrustLevel
from .schemas import MicTranscript


class LocalMicAdaptor(BaseAdaptor):
    name = "local_mic"
    capabilities = Capability(send_text=True, send_media=True, receive=True)

    def required_keys(self) -> list[str]:
        return []  # local path; voice services gate on their own models

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] | None = None):
        # "Sending" to the mic means speaking: callers should use /v1/tts
        # and play the WAV on the device. Recorded here for the ledger.
        return ChannelReply(ok=True, channel=self.name,
                            msg_id=f"mic-{int(time.time() * 1000)}",
                            raw={"to": to, "via": "/v1/tts"})

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        m = MicTranscript.model_validate(payload)
        return ChannelMessage(
            channel=self.name, sender_id=m.sender_id, chat_id=m.chat_id,
            text=m.text, msg_id=m.msg_id,
            ts=m.ts or time.time(),
            trust_level=TrustLevel.owner, raw=payload)
