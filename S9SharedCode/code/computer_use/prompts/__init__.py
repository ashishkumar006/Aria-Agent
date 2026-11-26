"""Computer-use LLM judgment prompts.

The PROSE prompts live in the repo's main prompts folder
(``S9SharedCode/code/prompts/``) so every prompt is in one place and easy to
track — this module only LOADS them:

  prompts/computer_use_a11y.md        -> SYSTEM_A11Y   (L2b a11y judge)
  prompts/computer_use_vision.md      -> SYSTEM_VISION (L3 vision judge)
  prompts/computer_use_user_a11y.md   -> USER_A11Y_TEMPLATE (goal + AX tree)
  prompts/computer_use_user_vision.md -> USER_VISION_TEMPLATE (goal + legend)

The JSON action schema and escalation reasons are structured data, not prose, so
they stay here.
"""
from __future__ import annotations

from pathlib import Path

# Repo prompts folder: computer_use/prompts/__init__.py -> parents[2] is `code`.
_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def _load(name: str, fallback: str = "") -> str:
    try:
        return (_PROMPTS_DIR / name).read_text(encoding="utf-8")
    except OSError:
        return fallback


# Shared action vocabulary (subset of the Browser ACTION_SCHEMA that maps to
# cua-driver primitives).
ACTION_SCHEMA_JSON = {
    "type": "object",
    "additionalProperties": False,
    "required": ["thinking", "verdict", "action"],
    "properties": {
        "thinking": {"type": "string",
                     "description": "1-2 sentences of reasoning about the current state vs the goal"},
        "verdict": {"type": "string", "enum": ["act", "escalate", "done"],
                    "description": "act=perform the action; escalate=AX insufficient, go to vision; done=goal met"},
        "action": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type"],
            "properties": {
                "type": {"type": "string",
                         # Dispatch-supported verbs ONLY (engine._dispatch_action).
                         # double_click/right_click/drag/zoom/set_window_frame/
                         # invoke_menu/clipboard_*/done are rejected — the SYSTEM
                         # text says the same; keep both lists in sync.
                         "enum": ["click", "type",
                                  "replace_text", "press_key", "hotkey", "scroll",
                                  "wait", "open_app"]},
                "app": {"type": "string",
                        "description": "for open_app: the application to launch (e.g. 'notepad', 'spotify', 'calc'). Deterministic — no Start-menu hunting."},
                "element_index": {"type": "integer", "description": "from get_window_state; required for click/scroll, optional for type (omit to type into the focused window)"},
                "value": {"type": "string", "description": "text for type/replace_text, or key name for press_key (e.g. 'Delete', 'End')"},
                "clear": {"type": "boolean", "description": "DEPRECATED — use replace_text to overwrite"},
                "modifiers": {"type": "array", "items": {"type": "string"},
                              "description": "for press_key chords, e.g. ['ctrl']"},
                "keys": {"type": "array", "items": {"type": "string"},
                         "description": "for hotkey chord, e.g. ['ctrl','a']"},
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                "amount": {"type": "integer"},
                "from_x": {"type": "number"}, "from_y": {"type": "number"},
                "to_x": {"type": "number"}, "to_y": {"type": "number"},
                "x1": {"type": "number"}, "y1": {"type": "number"},
                "x2": {"type": "number"}, "y2": {"type": "number"},
                "x": {"type": "number"}, "y": {"type": "number"},
                "width": {"type": "number"}, "height": {"type": "number"},
                "seconds": {"type": "number"},
                "success": {"type": "boolean"},
                "note": {"type": "string"},
            },
        },
    },
}

# Prose prompts — loaded from the repo prompts/ folder (single source of truth).
# A short fallback keeps a missing file from crashing the run; the canonical
# text lives in prompts/computer_use_*.md.
SYSTEM_A11Y = _load("computer_use_a11y.md",
                    "You are a desktop-driving agent. Emit ONE action toward the goal.")
SYSTEM_VISION = _load("computer_use_vision.md",
                      "You are a desktop-driving agent using a screenshot. Emit ONE click.")
USER_A11Y_TEMPLATE = _load("computer_use_user_a11y.md",
                           "GOAL: {goal}\nWINDOW: {app_name} (pid={pid})\n{tree_markdown}")
USER_VISION_TEMPLATE = _load("computer_use_user_vision.md",
                             "GOAL: {goal}\nLEGEND:\n{legend}")

ESCALATION_REASONS = [
    "ax_tree_empty",
    "element_missing",
    "visual_only_goal",
    "dialog_blocking",
    "unexpected_modal",
]