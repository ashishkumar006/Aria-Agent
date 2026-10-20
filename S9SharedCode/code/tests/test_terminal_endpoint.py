"""Tests for the integrated terminal: terminal.py + the /api/terminal socket.

The socket is a real shell, so these tests keep the shell work minimal — the
PTY round-trip drives a short `python -c`, and the socket tests only prove the
auth gate (which is the security-critical part: HTTP middleware does not run
for a WebSocket handshake, so the route must refuse on its own).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_server
import auth
import terminal


def test_default_shell_is_a_nonempty_argv():
    argv = terminal.default_shell()
    assert isinstance(argv, list) and argv and isinstance(argv[0], str)


def test_pty_session_runs_a_command_and_returns_its_output():
    """The wrapper must deliver real process output, byte for byte."""
    code = "import sys; sys.stdout.write('aria-pty-ok')"
    sess = terminal.PtySession([sys.executable, "-c", code], cols=80, rows=24)
    out = b""
    try:
        for _ in range(200):
            chunk = sess.read(4096)
            if not chunk:
                break
            out += chunk
    finally:
        sess.close()
    assert b"aria-pty-ok" in out


def test_terminal_ws_without_token_is_refused(monkeypatch):
    """No token -> close 1008. This is what stops a drive-by web page, which
    can open a WebSocket to loopback but cannot learn the per-launch token."""
    monkeypatch.setattr(auth, "_token", "secret-token")
    code = None
    with TestClient(agent_server.app) as client:
        with client.websocket_connect("/api/terminal") as ws:
            try:
                ws.receive_bytes()
            except WebSocketDisconnect as exc:
                code = exc.code
    assert code == 1008


def test_terminal_ws_with_wrong_token_is_refused(monkeypatch):
    monkeypatch.setattr(auth, "_token", "secret-token")
    code = None
    with TestClient(agent_server.app) as client:
        with client.websocket_connect("/api/terminal?token=not-it") as ws:
            try:
                ws.receive_bytes()
            except WebSocketDisconnect as exc:
                code = exc.code
    assert code == 1008


def test_terminal_ws_with_token_streams_shell_output(monkeypatch):
    """The token is the door: with it, the shell runs and prints something
    (its prompt/banner) that streams back to the client."""
    monkeypatch.setattr(auth, "_token", "secret-token")
    data = b""
    with TestClient(agent_server.app) as client:
        with client.websocket_connect(
                "/api/terminal?token=secret-token&cols=80&rows=24") as ws:
            for _ in range(80):
                try:
                    msg = ws.receive()
                except WebSocketDisconnect:
                    break
                if msg.get("type") == "websocket.send" and msg.get("bytes"):
                    data += msg["bytes"]
                    break
    assert data, "shell produced no output"
