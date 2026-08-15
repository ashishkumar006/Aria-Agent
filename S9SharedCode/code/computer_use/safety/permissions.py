"""safety/permissions.py — permission pre-flight (charter §8.1).

Front-load the trap table: tell the operator AX ok?, screenshot ok?,
daemon up?, elevated?, and list running apps. On Windows most apps need no
special setup, but apps that elevate (installers, system settings) require
the agent to run elevated too.

This module provides:
  - check_permissions() → structured report with actionable errors
  - grant_permissions() → run cua-driver permissions grant (Windows)
  - user_friendly_error() → maps a failure to a fix instruction
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass

from computer_use.core.daemon import call, DaemonError, _binary


@dataclass
class PermissionReport:
    binary_present: bool
    daemon_running: bool
    ax_ok: bool
    screenshot_ok: bool
    elevated: bool
    platform: str
    apps: list[str]
    note: str = ""

    def ok(self) -> bool:
        return (self.binary_present and self.daemon_running
                and self.ax_ok and self.screenshot_ok)

    def error_message(self) -> str:
        """Return a user-friendly error with fix steps, or '' if ok."""
        if not self.binary_present:
            return ("cua-driver binary not found. Install it:\n"
                    "  pip install cua-driver\n"
                    "  (or uv add cua-driver)")
        if not self.daemon_running:
            return ("cua-driver daemon is not running. Start it:\n"
                    "  cua-driver serve")
        if not self.ax_ok:
            if self.platform == "darwin":
                return ("Accessibility permission not granted on macOS.\n"
                        "  1. Run: cua-driver permissions grant\n"
                        "  2. Accept the system dialogs (Accessibility + Screen Recording)\n"
                        "  3. Re-run this agent")
            if self.platform.startswith("linux"):
                return ("AX tree empty on Linux. If the target is a Qt app, launch it with:\n"
                        "  QT_ACCESSIBILITY=1 <yourapp>\n"
                        "On Wayland you may also need a portal grant (interactive).")
            # Windows
            return ("AX tree empty. On Windows this usually means:\n"
                    "  - The app is still launching (wait a moment, re-scan)\n"
                    "  - The app is elevated and the agent is not (run agent as Admin)\n"
                    "  - The app is a game/canvas app (no AX tree — use vision)")
        if not self.screenshot_ok:
            return ("Screenshot capture failed. On macOS grant Screen Recording;\n"
                    "on Windows/Linux this is rare — restart the daemon.")
        return ""


def check_permissions() -> PermissionReport:
    """Probe the full permission surface and return a structured report."""
    bin_ = _binary()
    report = PermissionReport(
        binary_present=bin_ is not None,
        daemon_running=False,
        ax_ok=False,
        screenshot_ok=False,
        elevated=_is_elevated(),
        platform=sys.platform,
        apps=[],
    )
    if bin_ is None:
        report.note = "cua-driver not installed"
        return report

    # Daemon up?
    try:
        from computer_use.core.daemon import _is_running
        report.daemon_running = _is_running(bin_)
    except Exception:
        report.daemon_running = False
    if not report.daemon_running:
        report.note = "daemon not running"
        return report

    # AX tree readable?
    try:
        desktop = call("get_accessibility_tree", {}, timeout=15)
        wins = desktop.get("windows") or []
        report.ax_ok = bool(wins)
        report.apps = [w.get("title", "") for w in wins[:20]]
    except Exception as e:
        report.note = f"AX probe failed: {e}"

    # Screenshot works?
    try:
        st = call("get_window_state",
                  {"pid": (desktop.get("windows") or [{}])[0].get("pid", 0),
                   "window_id": (desktop.get("windows") or [{}])[0].get("window_id", 0),
                   "include_screenshot": True}, timeout=15)
        report.screenshot_ok = bool(
            st.get("screenshot") or st.get("image") or st.get("screenshot_png_b64"))
    except Exception:
        report.screenshot_ok = False

    return report


def grant_permissions() -> bool:
    """Run cua-driver permissions grant (Windows/macOS). Returns success."""
    bin_ = _binary()
    if bin_ is None:
        return False
    try:
        r = subprocess.run([bin_, "permissions", "grant"],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def _is_elevated() -> bool:
    """Best-effort: detect if the current process is elevated (Admin)."""
    if sys.platform == "win32":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    # POSIX: check EUID == 0
    try:
        return os.geteuid() == 0
    except Exception:
        return False
