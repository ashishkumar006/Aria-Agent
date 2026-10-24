"""Private desktop isolation for gated computer use.

The user's rule: a computer_action must NEVER touch the desktop the user is
working on. Everything the agent does runs on a SECOND, invisible desktop so
the user's windows, focus and clipboard are never disturbed.

Windows gives us that with `CreateProcess(..., lpDesktop)`. The agent process
never switches its own desktop: it only passes the private desktop's name to
the child it launches. `CreateProcess` creates the named desktop if it does
not yet exist and runs the child on it, so the child lives on the private
desktop while the agent's console stays on the user's `Default` desktop. The
cua-driver daemon stays on `Default` and reaches the child through UIA, which
works across desktops.

`CreateDesktopW` was tried first and fails with ERROR_INVALID_PARAMETER
(87) from this process's window station; `CreateProcess` does not, because it
creates the desktop itself as part of the launch.

Env:
  ARIA_PRIVATE_DESKTOP      "true" to create and use the private desktop
  ARIA_PRIVATE_DESKTOP_NAME name of the desktop (default "aria-private")
"""
from __future__ import annotations

import ctypes
import os
import threading
import win32con
import win32gui
import win32process

_LOCK = threading.Lock()
_CREATED: dict[str, bool] = {}
_DEFAULT_NAME = "aria-private"

_USER32 = ctypes.windll.user32
_USER32.CreateDesktopW.restype = ctypes.c_void_p
_USER32.OpenDesktopW.restype = ctypes.c_void_p
_USER32.CloseDesktop.restype = ctypes.c_int
_USER32.EnumDesktopWindows.restype = ctypes.c_bool


def _name() -> str:
    return os.environ.get("ARIA_PRIVATE_DESKTOP_NAME") or _DEFAULT_NAME


def enabled() -> bool:
    return os.environ.get("ARIA_PRIVATE_DESKTOP", "").lower() in (
        "1", "true", "yes", "on")


def ensure_private_desktop(name: str | None = None) -> str:
    """Return the private desktop name.

    `CreateProcess` creates the named desktop on demand, so this is a
    no-op that just records the name. It is idempotent and thread-safe, and
    never raises.
    """
    name = name or _name()
    with _LOCK:
        _CREATED[name] = True
        return name


def restore_default_desktop() -> None:
    """Drop the private desktop. Call it when the agent shuts down so a stale
    desktop does not linger for the next session."""
    name = _name()
    with _LOCK:
        _CREATED.pop(name, None)
    try:
        h = _USER32.OpenDesktopW(name, 0, False, win32con.GENERIC_ALL)
        if h:
            _USER32.CloseDesktop(h)
    except OSError:
        pass


def launch_on_private_desktop(exe: str, args: str | None = None,
                              desktop_name: str | None = None) -> int | None:
    """Launch a process on the private desktop.

    `CreateProcess` creates the named desktop if it does not exist and runs
    the child on it. Returns the new pid, or None if it could not be
    launched.
    """
    name = desktop_name or ensure_private_desktop()
    if not name:
        return None
    si = win32process.STARTUPINFO()
    # STARTF_USEDESKTOP (0x00000100) is not exported by this pywin32 build;
    # set the bit directly. It tells CreateProcess to run the child on the
    # desktop named by lpDesktop instead of the user's `Default` desktop.
    si.dwFlags |= 0x00000100
    si.lpDesktop = name
    try:
        # CreateProcess wants the environment as a NUL-terminated string
        # or None (inherit); a dict raises TypeError.
        _, _, pid, _ = win32process.CreateProcess(
            exe, args, None, None, False,
            win32process.CREATE_NEW_PROCESS_GROUP,
            None, None, si)
        return pid
    except win32process.error:
        return None


def windows_on_private_desktop(desktop_name: str,
                               pid: int | None = None) -> list[int]:
    """Top-level window handles on the private desktop, optionally filtered
    by pid."""
    out: list[int] = []

    def _cb(handle, extra):
        if pid is None:
            out.append(handle)
            return True
        try:
            owner = win32process.GetWindowThreadProcessId(handle)
            if owner[1] == pid:
                out.append(handle)
        except Exception:
            pass
        return True

    try:
        h = _USER32.OpenDesktopW(desktop_name, 0, False,
                                  win32con.GENERIC_ALL)
        if h:
            win32gui.EnumDesktopWindows(h, _cb, None)
            _USER32.CloseDesktop(h)
    except OSError:
        pass
    return out


def bring_to_front(desktop_name: str, pid: int) -> bool:
    """Activate the pid's window ON the private desktop (not the user's)."""
    handles = windows_on_private_desktop(desktop_name, pid)
    if not handles:
        return False
    try:
        win32gui.ShowWindow(handles[0], win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(handles[0])
        return True
    except win32gui.error:
        return False