"""Saved task templates / workflows (Todo 10).

A template is a named, parameterized query the operator can replay later
with new inputs. Stored on disk as JSON. Replaying a template substitutes
``{var}`` placeholders in the query with provided values and runs the
orchestrator exactly like a normal /api/chat call (sharing the same
conversation thread if a conversation_id is supplied).

Example template:
  {"name": "daily_brief", "query": "Summarize my calendar and unread email",
   "vars": []}
  {"name": "research_topic", "query": "Research {topic} and write a summary",
   "vars": ["topic"]}
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_STATE_DIR = Path(os.environ.get("S9_STATE_DIR") or (ROOT / "state"))
_TPL_PATH = _STATE_DIR / "templates.json"
_TPL_LOCK = threading.Lock()
_TEMPLATES: dict[str, dict] = {}


def _load() -> None:
    global _TEMPLATES
    try:
        data = json.loads(_TPL_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        data = {}
    # Guard against legacy/alien shapes (e.g. a bare list); store is a dict.
    _TEMPLATES = data if isinstance(data, dict) else {}


def _save() -> None:
    _TPL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _TPL_PATH.with_name(f"{_TPL_PATH.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(_TEMPLATES, indent=2), encoding="utf-8")
    os.replace(tmp, _TPL_PATH)


def save(name: str, query: str, vars: list[str] | None = None) -> dict:
    """Save (or overwrite) a template. `vars` lists placeholder names.

    Names and queries are bounded. An 8-million-character name was once
    accepted and stored, after which every `GET /api/templates` returned 16 MB
    in 18 s - and the object could never be deleted through the API, because
    its 8 MB URL exceeded the HTTP parser's limit. Caps keep one bad input from
    becoming permanent.
    """
    name = str(name or "")
    query = str(query or "")
    if not name or len(name) > 200:
        raise ValueError("template name must be 1-200 characters")
    if len(query) > 8192:
        raise ValueError("template query must be at most 8192 characters")
    with _TPL_LOCK:
        _load()
        _TEMPLATES[name] = {"name": name, "query": query,
                            "vars": list(vars or [])}
        _save()
    return _TEMPLATES[name]


def list_templates() -> list[dict]:
    with _TPL_LOCK:
        _load()
        return list(_TEMPLATES.values())


def get(name: str) -> dict | None:
    with _TPL_LOCK:
        _load()
        return _TEMPLATES.get(name)


def delete(name: str) -> bool:
    with _TPL_LOCK:
        _load()
        if name in _TEMPLATES:
            del _TEMPLATES[name]
            _save()
            return True
    return False


def render(name: str, values: dict[str, str]) -> str | None:
    """Substitute ``{var}`` placeholders; returns the concrete query or None
    if the template is missing."""
    tpl = get(name)
    if not tpl:
        return None
    q = tpl["query"]
    for k, v in values.items():
        q = q.replace("{" + k + "}", str(v))
    return q
