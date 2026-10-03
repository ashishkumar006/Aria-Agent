"""Crash-safe JSON file helpers (single copy).

Every gateway JSON sidecar (approvals, pairing, usage caps, doc versions)
goes through here: unique temp file + os.replace, so a crash mid-write
can never leave a half-written file behind. Callers that need mutual
exclusion hold their own lock around load/save (same discipline as
memory/store.py, which keeps its richer list-store API).
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any


def load_json(path: str | Path, default: Any):
    """Read a JSON file, returning `default` on missing/corrupt content."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return data


def save_json(path: str | Path, data: Any) -> None:
    """Write JSON atomically via a uniquely-named temp file + os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
