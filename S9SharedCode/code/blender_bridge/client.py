"""Small synchronous client for the in-Blender bridge.

The bridge is a loopback JSON-line socket owned by a running Blender process.
This client is used by the agent's section service and by headless jobs; it
never touches bpy directly, so it cannot corrupt Blender's main thread.
"""
from __future__ import annotations

import json
import socket
from typing import Any


class BlenderBridgeError(RuntimeError):
    pass


class BlenderClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 0,
                 token: str = "", timeout: float = 300.0):
        self.host = host
        self.port = int(port)
        self.token = token
        self.timeout = float(timeout)

    def call(self, cmd: str, args: dict | None = None,
             timeout: float | None = None) -> dict:
        request = {"token": self.token, "cmd": cmd, "args": args or {}}
        try:
            with socket.create_connection((self.host, self.port),
                                          timeout=timeout or self.timeout) as sock:
                sock.settimeout(timeout or self.timeout)
                sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
                buf = bytearray()
                while b"\n" not in buf:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
        except OSError as exc:
            raise BlenderBridgeError(
                f"Blender bridge unreachable on {self.host}:{self.port}: {exc}") from None
        line = bytes(buf).split(b"\n", 1)[0]
        if not line:
            raise BlenderBridgeError("Blender bridge closed the connection")
        try:
            data = json.loads(line.decode("utf-8"))
        except ValueError as exc:
            raise BlenderBridgeError(f"Blender bridge returned invalid JSON: {exc}") from None
        if not isinstance(data, dict):
            raise BlenderBridgeError("Blender bridge returned a non-object response")
        if not data.get("ok"):
            raise BlenderBridgeError(str(data.get("error") or "Blender command failed"))
        return data

    def ping(self, timeout: float = 5.0) -> dict:
        return self.call("ping", timeout=timeout)

    def close(self) -> None:
        try:
            self.call("shutdown", timeout=5.0)
        except Exception:
            pass


def call(port: int, token: str, cmd: str, args: dict | None = None,
         timeout: float = 300.0) -> dict:
    return BlenderClient(port=port, token=token, timeout=timeout).call(
        cmd, args, timeout=timeout)
