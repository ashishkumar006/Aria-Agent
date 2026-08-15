"""apps/ — per-vertical app drivers (charter §5).

  native.py   — AX-tree apps (Calculator, Notepad, Settings, Office)
  electron.py — Electron/Chromium CDP escape hatch (VS Code, Slack, ...)
"""
from .native import (
    launch, find_element, get_state, click_element, type_text,
    calculator_compute, settings_open_display,
)
from .electron import (
    is_electron, launch_with_debug_port,
    page_click, page_type, page_eval, page_navigate,
)

__all__ = [
    "launch", "find_element", "get_state", "click_element", "type_text",
    "calculator_compute", "settings_open_display",
    "is_electron", "launch_with_debug_port",
    "page_click", "page_type", "page_eval", "page_navigate",
]
