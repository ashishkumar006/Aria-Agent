"""The OS folder picker route.

`POST /api/studio/pick-folder` opens a native dialog on the machine running
the agent and returns what the user picked. The tests fake the process so no
window ever opens: they pin the three outcomes the console depends on — a
picked path, a cancel, and the failure modes the console falls back from
(platform without powershell, a dialog left open forever).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
import agent_server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(agent_server.app)


class _FakeProc:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"",
                 hangs: bool = False):
        self._out = stdout
        self._err = stderr
        self._hangs = hangs
        self.killed = False

    async def communicate(self):
        if self._hangs:
            return await asyncio.sleep(3600)  # never resolves within any test timeout
        return self._out, self._err

    def kill(self):
        self.killed = True


def _fake_subprocess(proc):
    async def _create(*args, **kwargs):
        return proc
    return _create


def test_a_picked_path_comes_back(monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _fake_subprocess(_FakeProc(b"C:\\Users\\me\\proj\r\n")))
    r = client.post("/api/studio/pick-folder")
    assert r.status_code == 200
    assert r.json() == {"path": "C:\\Users\\me\\proj"}


def test_a_cancel_comes_back_as_cancelled(monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _fake_subprocess(_FakeProc(b"", b"user cancelled")))
    r = client.post("/api/studio/pick-folder")
    assert r.status_code == 200
    assert r.json() == {"cancelled": True}


def test_a_platform_without_powershell_is_a_clean_501(monkeypatch):
    async def _create(*args, **kwargs):
        raise FileNotFoundError("powershell")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create)
    r = client.post("/api/studio/pick-folder")
    assert r.status_code == 501
    assert "not available" in r.json()["error"]


def test_a_dialog_left_open_forever_is_a_504_and_is_killed(monkeypatch):
    proc = _FakeProc(hangs=True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _fake_subprocess(proc))

    async def _wait_for(coro, timeout=None):
        # Simulate the 300s cap without waiting for it.
        try:
            coro.close()
        except Exception:
            pass
        raise asyncio.TimeoutError()

    monkeypatch.setattr(asyncio, "wait_for", _wait_for)
    r = client.post("/api/studio/pick-folder")
    assert r.status_code == 504
    assert "cancelled" in r.json()["error"]
    assert proc.killed is True


def test_the_dialog_takes_no_input_from_the_caller(monkeypatch):
    """No body means no path injection surface: the dialog starts at
    MyComputer and the only input the OS takes is the user's own click."""
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _fake_subprocess(_FakeProc(b"")))
    r = client.post("/api/studio/pick-folder",
                    json={"path": "C:\\evil\\nowhere"})
    # The route ignores a body entirely: the only thing that can come back
    # is a user-picked path, a cancel, or an availability error — never the
    # caller's string.
    assert r.status_code == 200
    assert r.json() == {"cancelled": True}


@pytest.mark.parametrize("body", [None, {}, {"path": "C:\\x"}])
def test_every_shape_of_post_is_accepted_without_input(monkeypatch, body):
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _fake_subprocess(_FakeProc(b"")))
    r = client.post("/api/studio/pick-folder", json=body)
    assert r.status_code == 200
