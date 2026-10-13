"""Feature flags + dynamic config for the console, backed by Prefab
(prefab.cloud) with a local JSON fallback.

- Cloud mode: PREFAB_API_KEY is set → the prefab-cloud-python client
  evaluates flags/configs in-process (fast; it syncs in the background and
  returns defaults until unlocked). Evaluation NEVER raises: any error
  falls back to the local default.
- Local mode: no key → definitions + state/flags.json overrides only.
- Operator overrides (Apps page) win in BOTH modes: a local override beats
  the cloud value until cleared, so the console always has a kill-switch
  even when Prefab is unreachable.

Conventions match agent_server: S9_STATE_DIR overrides the state dir so
tests never touch live flags.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).parent
STATE_DIR = Path(os.environ.get("S9_STATE_DIR") or (ROOT / "state"))
FLAGS_PATH = STATE_DIR / "flags.json"

DEFINITIONS: dict[str, dict] = {
    "chat.tools": {
        "type": "bool",
        "default": True,
        # Kept honest against `_CHAT_TOOLS` in agent_server.py. This text used
        # to name `get_time` and `currency_convert`, which do not exist, and
        # call the catalog "read-only" when it includes create_file, edit_file,
        # schedule_task and render_document. A kill switch whose description
        # misstates its blast radius is worse than no description: the user
        # toggles it expecting to hide lookups and instead has no idea whether
        # file writes are about to be switched off too.
        "description": "Whether Chat may use its tools (web search and "
                       "fetch, Gmail/GitHub/Notion/Slack/Calendar lookups, "
                       "file create/edit/search, memory write, artifact read, "
                       "document rendering and filing, task scheduling, and "
                       "reading the behavioural profile). Off = plain "
                       "text answers with no tools at all. The full list is "
                       "GET /api/skills; the authoritative catalog is "
                       "_CHAT_TOOLS in agent_server.py. Chat is read-only on "
                       "GitHub and Notion: the write api_methods are stripped "
                       "from the schema it is handed.",
    },
    "chat.agui": {
        "type": "bool",
        "default": True,
        "description": "POST /api/agui serves chat turns in AG-UI's event "
                       "vocabulary (RUN_STARTED / TEXT_MESSAGE_* / "
                       "TOOL_CALL_* / RUN_FINISHED) for standards-compliant "
                       "clients. Additive; the console does not use it. "
                       "GET /api/capabilities stays available either way.",
    },
    "apps.a2ui": {
        "type": "bool",
        "default": True,
        "description": "Apps can ask the agent for a generated view "
                       "(POST /api/a2ui/generate). The surface is validated "
                       "against a closed catalog before it is returned, so "
                       "nothing executable can reach the client. Turn off to "
                       "return to hand-built views only.",
    },
    "apps.views.table": {
        "type": "bool",
        "default": True,
        "description": "Table view available in tracker app detail. "
                       "If all views are off, table is used as fallback.",
    },
    "apps.views.cards": {
        "type": "bool",
        "default": True,
        "description": "Cards (tile grid) view available in tracker app "
                       "detail. Free-tier safe: evaluated locally.",
    },
    "apps.views.prefab": {
        "type": "bool",
        "default": True,
        "description": "PrefectHQ/prefab bundled render of tracker apps "
                       "(spike, side-by-side). Free, offline-capable.",
    },
    "memory.observations": {
        "type": "bool",
        # OFF by default. This is the only flag that changes how much of your
        # conversations is retained: on, each chat turn is read for durable
        # statements about how you like to work, and each one is stored with a
        # verbatim quote from you as evidence. It records behaviour ("wants
        # publication dates on every claim"), never personality labels,
        # diagnosis or inferred intent. Turn it on deliberately.
        "default": False,
        "description": "Keep an evidence-backed behavioural profile of the "
                       "user. Every entry stores a verbatim quote from the "
                       "conversation as proof; an entry whose quote cannot be "
                       "found in the source is discarded rather than saved. "
                       "Observations are behavioural only - no personality "
                       "type, no mental-health or motive inference, which the "
                       "writer rejects outright. Off by default because it "
                       "retains conversation text you never asked to be "
                       "kept. Stored in the 'profile' drawer: listable, "
                       "deletable and wipeable from the Memory view without "
                       "touching your facts.",
    },
}

_lock = threading.Lock()
_client = None
_client_failed = False


def _cloud_client():
    """Prefab client singleton, or None when unkeyed/unusable. Never raises."""
    global _client, _client_failed
    if _client is not None or _client_failed:
        return _client
    with _lock:
        if _client is not None or _client_failed:
            return _client
        key = (os.environ.get("PREFAB_API_KEY") or "").strip()
        if not key:
            return None
        try:
            import prefab_cloud_python as _pc
            _pc.set_options(_pc.Options(api_key=key))
            _client = _pc.get_client()
        except Exception:
            _client_failed = True
            _client = None
        return _client


def prefab_status() -> dict:
    """Cloud wiring state for the Apps board. Never raises."""
    keyed = bool((os.environ.get("PREFAB_API_KEY") or "").strip())
    client = _cloud_client() if keyed else None
    return {"configured": keyed, "live": client is not None}


def _read_overrides() -> dict:
    try:
        data = json.loads(FLAGS_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_overrides(data: dict) -> None:
    FLAGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = FLAGS_PATH.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, FLAGS_PATH)


def _coerce(ftype: str, value):
    if ftype == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("1", "true", "yes", "on"):
                return True
            if v in ("0", "false", "no", "off"):
                return False
        raise ValueError(f"not a bool: {value!r}")
    if ftype == "int":
        return int(value)
    if ftype == "float":
        return float(value)
    return str(value)


def _cloud_value(name: str):
    """Raw Prefab value or None when absent/unusable. Never raises."""
    try:
        client = _cloud_client()
        if client is None:
            return None
        return client.get(name, None)
    except Exception:
        return None


def _definition(name: str) -> dict | None:
    return DEFINITIONS.get(name)


def get_value(name: str, default=None):
    """Effective value: local override > Prefab cloud > definition default
    > explicit default. Never raises."""
    try:
        ov = _read_overrides()
        if name in ov:
            return ov[name]
        definition = _definition(name)
        fallback = definition["default"] if definition else default
        cv = _cloud_value(name)
        if cv is None:
            return fallback
        if definition:
            try:
                return _coerce(definition["type"], cv)
            except (ValueError, TypeError):
                return fallback
        return cv
    except Exception:
        definition = _definition(name)
        return definition["default"] if definition else default


def is_enabled(name: str, default: bool = False) -> bool:
    """Boolean gate. Never raises."""
    definition = _definition(name)
    if definition is None:
        return bool(get_value(name, default))
    try:
        return bool(get_value(name, definition["default"]))
    except Exception:
        return bool(definition["default"])


def set_override(name: str, value) -> dict:
    """Operator override (Apps page). Validated against the definition;
    unknown names and mistyped values raise ValueError (→ HTTP 400)."""
    definition = _definition(name)
    if definition is None:
        raise ValueError(f"unknown flag '{name}'")
    coerced = _coerce(definition["type"], value)
    with _lock:
        ov = _read_overrides()
        ov[name] = coerced
        _write_overrides(ov)
    return {"name": name, "value": coerced}


def clear_override(name: str) -> bool:
    with _lock:
        ov = _read_overrides()
        if name not in ov:
            return False
        del ov[name]
        _write_overrides(ov)
        return True


def all_flags() -> list[dict]:
    """Merged board view: every definition plus any stray local overrides."""
    try:
        ov = _read_overrides()
    except Exception:
        ov = {}
    rows = []
    for name in sorted(set(DEFINITIONS) | set(ov)):
        definition = _definition(name)
        ftype = definition["type"] if definition else "str"
        default = definition["default"] if definition else None
        if name in ov:
            rows.append({"name": name, "type": ftype, "value": ov[name],
                         "default": default, "source": "override",
                         "description": (definition or {}).get("description", "")})
            continue
        cv = _cloud_value(name)
        if cv is not None and definition:
            try:
                cv = _coerce(ftype, cv)
            except (ValueError, TypeError):
                cv = None
        if cv is not None:
            rows.append({"name": name, "type": ftype, "value": cv,
                         "default": default, "source": "prefab",
                         "description": (definition or {}).get("description", "")})
        else:
            rows.append({"name": name, "type": ftype, "value": default,
                         "default": default, "source": "default",
                         "description": (definition or {}).get("description", "")})
    return rows
