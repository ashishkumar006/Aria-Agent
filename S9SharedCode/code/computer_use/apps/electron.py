"""apps/electron.py — Electron / Chromium CDP escape hatch (charter §9).

A large fraction of modern desktop apps are Chromium in disguise (VS Code,
Cursor, Slack, Discord, Notion, Linear, 1Password, Obsidian). To AX they
look like one opaque AXWebArea. cua-driver's answer: launch the app with a
debugging port and drive its DOM through Chrome DevTools Protocol directly.

This module:
  - detects known Electron apps (pattern match against list_apps)
  - relaunches with electron_debugging_port
  - drives the DOM via the `page` tool (CSS selectors, JS eval, waits)

The Browser cascade from Session 9 already understands CDP, so this path
borrows it directly.
"""
from __future__ import annotations

import time
from computer_use.core.daemon import call, DaemonError

# Known Electron apps and their bundle/executable hints.
KNOWN_ELECTRON = {
    "vscode": "com.microsoft.VSCode",
    "cursor": "com.todesktop.cursor",
    "slack": "Slack",
    "discord": "Discord",
    "notion": "Notion",
    "obsidian": "Obsidian",
    "1password": "1Password",
    "linear": "Linear",
}


def is_electron(app_name: str) -> bool:
    """Pattern-match an app name against the known Electron list.

    Handles spaced-out names like "Visual Studio Code" (where the key
    "vscode" is not a substring) by also checking token-overlap and common
    aliases.
    """
    n = app_name.lower().strip()
    # Direct substring match (e.g. "slack", "discord", "notion").
    if any(k in n for k in KNOWN_ELECTRON):
        return True
    # Alias map for spaced / branded names.
    ALIASES = {
        "visual studio code": "vscode",
        "vs code": "vscode",
        "code": "vscode",
        "cursor": "cursor",
        "obsidian": "obsidian",
        "1password": "1password",
        "linear": "linear",
    }
    if n in ALIASES:
        return True
    # Token overlap: "visual studio code" -> {"visual", "studio", "code"}
    # matches the "vscode" key via the "code" token.
    tokens = set(n.split())
    for k in KNOWN_ELECTRON:
        if k in tokens:
            return True
    return False


def _resolve_app_name(app_name: str) -> str:
    """Map a key/alias to the daemon's expected launch name.

    The daemon's `launch_app` wants the real app name (e.g. "Visual Studio
    Code"), not our internal key ("vscode"). This resolves aliases and
    known bundle hints to a launchable name.
    """
    n = app_name.lower().strip()
    # If it's already a real-looking name (has a space or is a known title),
    # pass it through.
    if " " in app_name or app_name in KNOWN_ELECTRON:
        return app_name
    # Key -> friendly name the daemon understands.
    KEY_TO_NAME = {
        "vscode": "Visual Studio Code",
        "cursor": "Cursor",
        "slack": "Slack",
        "discord": "Discord",
        "notion": "Notion",
        "obsidian": "Obsidian",
        "1password": "1Password",
        "linear": "Linear",
    }
    return KEY_TO_NAME.get(n, app_name)


def launch_with_debug_port(app_name: str, port: int = 9222,
                            timeout: float = 20.0) -> dict:
    """Launch an Electron app with a CDP debugging port exposed.

    Returns {pid, window_id, debug_port} or raises.
    """
    launch_name = _resolve_app_name(app_name)
    bundle = KNOWN_ELECTRON.get(app_name.lower()) or KNOWN_ELECTRON.get(launch_name.lower())
    args = {"name": launch_name, "electron_debugging_port": port}
    if bundle:
        args["bundle_id"] = bundle
    r = call("launch_app", args, timeout=timeout)
    pid = r.get("pid")
    wid = (r.get("windows") or [{}])[0].get("window_id")
    if pid is None:
        raise DaemonError(f"launch_app('{launch_name}') with debug port failed")
    return {"pid": pid, "window_id": wid, "debug_port": port}


def page_click(pid: int, selector: str, timeout: float = 15.0) -> dict:
    """Click a DOM element in the Electron app by CSS selector (via CDP)."""
    return call("page", {"pid": pid, "action": "click", "selector": selector},
                timeout=timeout)


def page_type(pid: int, selector: str, text: str, timeout: float = 15.0) -> dict:
    """Type text into a DOM element by CSS selector (via CDP)."""
    return call("page", {"pid": pid, "action": "type",
                         "selector": selector, "text": text}, timeout=timeout)


def page_eval(pid: int, expression: str, timeout: float = 15.0) -> dict:
    """Evaluate JavaScript in the Electron app's page (via CDP)."""
    return call("page", {"pid": pid, "action": "evaluate",
                         "expression": expression}, timeout=timeout)


def page_navigate(pid: int, url: str, timeout: float = 15.0) -> dict:
    """Navigate the Electron app's page to a URL (via CDP)."""
    return call("page", {"pid": pid, "action": "navigate", "url": url},
                timeout=timeout)
