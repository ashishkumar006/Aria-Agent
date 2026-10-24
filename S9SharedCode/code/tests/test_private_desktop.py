"""Private-desktop isolation for gated computer use.

The user's rule: a computer_action must NEVER touch the desktop the user is
working on. Everything runs on a SECOND, invisible desktop. This module
creates it with Windows `CreateProcess(..., lpDesktop)` and launches the
daemon and any app the agent opens onto it, so the user's windows, focus and
clipboard are never disturbed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import computer_use.private_desktop as pd


def test_enabled_reads_env(monkeypatch):
    monkeypatch.setenv("ARIA_PRIVATE_DESKTOP", "true")
    assert pd.enabled() is True
    monkeypatch.setenv("ARIA_PRIVATE_DESKTOP", "1")
    assert pd.enabled() is True
    monkeypatch.setenv("ARIA_PRIVATE_DESKTOP", "no")
    assert pd.enabled() is False


def test_ensure_desktop_is_idempotent(monkeypatch):
    monkeypatch.setenv("ARIA_PRIVATE_DESKTOP", "true")
    monkeypatch.setattr(pd, "_CREATED", {})
    assert pd.ensure_private_desktop() == "aria-private"
    # Second call must not re-create the desktop.
    assert pd.ensure_private_desktop() == "aria-private"


def test_restore_default_desktop_is_safe_when_not_created(monkeypatch):
    monkeypatch.setenv("ARIA_PRIVATE_DESKTOP", "true")
    monkeypatch.setattr(pd, "_CREATED", {})
    monkeypatch.setattr(pd._USER32, "OpenDesktopW",
                        lambda n, *a, **k: 0)
    monkeypatch.setattr(pd._USER32, "CloseDesktop", lambda h: None)
    # Must not raise.
    pd.restore_default_desktop()


def test_daemon_starts_on_the_private_desktop(monkeypatch):
    """ensure_daemon must hand the daemon a STARTUPINFO pointing at the
    private desktop. Without this the daemon reads and clicks the user's
    windows."""
    import computer_use.core.daemon as D
    import computer_use.private_desktop as PD

    monkeypatch.setenv("ARIA_PRIVATE_DESKTOP", "true")
    monkeypatch.setattr(PD, "enabled", lambda: True)
    monkeypatch.setattr(PD, "ensure_private_desktop", lambda: "aria-private")
    monkeypatch.setattr(PD, "restore_default_desktop", lambda: None)
    monkeypatch.setattr(PD, "_CREATED", {})
    monkeypatch.setattr(D, "_binary", lambda: "fake-cua-driver")
    monkeypatch.setattr(D, "_is_running", lambda b: False)
    monkeypatch.setattr(D.subprocess, "run", lambda *a, **k: None)

    captured = {}

    class FakePopen:
        def __init__(self, args, stdout=None, stderr=None, stdin=None,
                     startupinfo=None, creationflags=0, close_fds=True):
            captured["startupinfo"] = startupinfo
            captured["args"] = args

    monkeypatch.setattr(D.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(D, "_pipe_responsive", lambda b: True)

    assert D.ensure_daemon() is True
    assert captured["args"] == ["fake-cua-driver", "serve"]
    assert captured["startupinfo"] is not None
    assert captured["startupinfo"].lpDesktop == "aria-private"


def test_daemon_starts_on_default_desktop_when_isolation_off(monkeypatch):
    import computer_use.core.daemon as D
    import computer_use.private_desktop as PD

    monkeypatch.delenv("ARIA_PRIVATE_DESKTOP", raising=False)
    monkeypatch.setattr(PD, "enabled", lambda: False)
    monkeypatch.setattr(D, "_binary", lambda: "fake-cua-driver")
    monkeypatch.setattr(D, "_is_running", lambda b: False)
    monkeypatch.setattr(D.subprocess, "run", lambda *a, **k: None)

    captured = {}

    class FakePopen:
        def __init__(self, args, stdout=None, stderr=None, stdin=None,
                     startupinfo=None, creationflags=0, close_fds=True):
            captured["startupinfo"] = startupinfo

    monkeypatch.setattr(D.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(D, "_pipe_responsive", lambda b: True)

    assert D.ensure_daemon() is True
    assert captured["startupinfo"] is None


def test_launch_on_private_desktop_passes_the_desktop(monkeypatch):
    monkeypatch.setenv("ARIA_PRIVATE_DESKTOP", "true")
    monkeypatch.setattr(pd, "_CREATED", {})
    import win32process
    captured = {}

    def fake_create_process(exe, args, a1, a2, a3, flags, env, cwd, si):
        captured["exe"] = exe
        captured["args"] = args
        captured["startup"] = si
        return 0, 0, 999, 0

    monkeypatch.setattr(win32process, "CreateProcess", fake_create_process)
    pid = pd.launch_on_private_desktop("cmd.exe", '/c start "" "Spotify"')
    assert pid == 999
    assert captured["exe"] == "cmd.exe"
    assert captured["startup"].lpDesktop == "aria-private"


def test_launch_on_private_desktop_falls_back_when_isolation_off(monkeypatch):
    monkeypatch.delenv("ARIA_PRIVATE_DESKTOP", raising=False)
    monkeypatch.setattr(pd, "_CREATED", {})
    import win32process
    captured = {}

    def fake_create_process(exe, args, a1, a2, a3, flags, env, cwd, si):
        captured["exe"] = exe
        captured["args"] = args
        captured["startup"] = si
        return 0, 0, 999, 0

    monkeypatch.setattr(win32process, "CreateProcess", fake_create_process)
    # When isolation is off the caller does not call this function at all,
    # but if it is called it must still work without crashing.
    pid = pd.launch_on_private_desktop("cmd.exe", "x")
    assert pid == 999


if __name__ == "__main__":
    pytest.main([__file__, "-q"])