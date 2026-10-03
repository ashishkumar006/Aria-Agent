"""Signal adaptor (signal-cli subprocess wrapper). LIVE send + poll (P2).

There is NO official Signal bot API: this adaptor shells out to `signal-cli`
(running as a daemon with JSON-RPC). Pairing is QR-code-based:
`signal-cli link` prints a tsdevice link the operator scans in-app. The
account dir (SIGNAL_DATA_DIR) holds identity keys — treat like a secret.
Free: yes (self-hosted number required).
"""
from __future__ import annotations

import time
from typing import Any

from ..base import BaseAdaptor, Capability, NotIntegrated
from ..envelope import ChannelMessage, TrustLevel
from ..trust import classify
from .schemas import SignalReceive


class SignalAdaptor(BaseAdaptor):
    name = "signal"
    capabilities = Capability(send_text=True, send_media=True, receive=True)

    def required_keys(self) -> list[str]:
        return ["SIGNAL_NUMBER"]

    def _cli(self) -> tuple[str, dict[str, str]]:
        import os
        import shutil
        number = self._need("SIGNAL_NUMBER")
        exe = shutil.which("signal-cli") or "signal-cli"
        data_dir = (os.getenv("SIGNAL_DATA_DIR") or "").strip()
        return exe, {"exe": exe, "number": number, "data_dir": data_dir}

    def send(self, *, to: str, text: str, thread_id: str | None = None,
             media: list[dict[str, Any]] = None):
        # LIVE: signal-cli send (one-shot subprocess; the persistent daemon
        # variant stays an operator option). Never log bodies at info level.
        import os
        import subprocess
        _exe, ctx = self._cli()
        cmd = [_exe, "-u", ctx["number"], "send", "-m", text, to]
        if ctx["data_dir"]:
            cmd[1:1] = ["--config", ctx["data_dir"]]
        atts = [m.get("path") for m in (media or []) if isinstance(m, dict) and m.get("path")]
        for a in atts:
            cmd.extend(["-a", str(a)])
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=int(os.getenv("SIGNAL_TIMEOUT", "60")))
        except FileNotFoundError:
            raise RuntimeError("signal-cli binary not found (set PATH or install it)")
        except subprocess.TimeoutExpired:
            raise RuntimeError("signal send timed out")
        if p.returncode != 0:
            raise RuntimeError(f"signal error: {(p.stderr or p.stdout or '').strip()[:200]}")
        from ..envelope import ChannelReply
        return ChannelReply(ok=True, channel=self.name, raw={"to": to})

    def poll(self, *, timeout: int = 15) -> list[dict[str, Any]]:
        """One-shot receive poll: returns raw JSON envelopes (normalize each
        with normalize()). Operator-run loops call this; no daemon needed."""
        import json
        import subprocess
        _exe, ctx = self._cli()
        cmd = [_exe, "-u", ctx["number"], "--output=json", "receive",
               "-t", str(max(1, timeout))]
        if ctx["data_dir"]:
            cmd[1:1] = ["--config", ctx["data_dir"]]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
        except FileNotFoundError:
            raise RuntimeError("signal-cli binary not found (set PATH or install it)")
        except subprocess.TimeoutExpired:
            return []
        out = []
        for line in (p.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def normalize(self, payload: dict[str, Any]) -> ChannelMessage:
        incoming = SignalReceive.model_validate(payload)
        env = incoming.envelope
        group = env.dataMessage.groupInfo.groupId or None
        return ChannelMessage(
            channel=self.name, sender_id=env.source,
            chat_id=group or env.sourceUuid or env.source,
            text=env.dataMessage.message, thread_id=group,
            msg_id=str(env.timestamp or ""),
            ts=(env.timestamp / 1000.0) if env.timestamp else time.time(),
            trust_level=classify(self.name, env.source), raw=payload)
