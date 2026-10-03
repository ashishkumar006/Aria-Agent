"""Locked, atomic JSON persistence for memory items.

Same discipline as the agent's turnlog: every mutation is a
load-modify-save under a single ``threading.Lock`` (uvicorn runs sync
endpoints on a thread pool, so concurrent writes are real), and saves go
through a uniquely-named temp file + ``os.replace`` so a crash mid-write
can never leave a half-written ``memory.json`` behind.

Corrupt/empty/non-list files load as ``[]`` — a bad disk state must
degrade to empty memory, never kill the service.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

from .models import MemoryRecord


class MemoryStore:
    """Thread-safe JSON list store for :class:`MemoryRecord`.

    Phase 1: validates records (pre-drawer items validate with
    ``drawer=None``); stored bytes of legacy items are never rewritten by
    reads — only append/clear rewrite the file.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Parsed-store cache. Every record embeds a 768-float vector (~15KB of
        # JSON), so the fact+episode+document drawers are ~2.6MB and ~120
        # records. Re-reading and re-pydantic-validating that on EVERY list /
        # search call put ~1.8s on the Memory page, and the dashboard polls
        # it. Keyed on (mtime_ns, size), which is safe here because every
        # writer goes through _save_locked's atomic os.replace — a
        # concurrently replaced file changes both fields, so a stale entry
        # cannot be served.
        self._cache_key: tuple[int, int] | None = None
        self._cache: list[MemoryRecord] = []

    # ── internals (caller must hold _lock) ─────────────────────────────────

    def _load_locked(self) -> list[MemoryRecord]:
        if not self.path.exists():
            return []
        try:
            st = self.path.stat()
        except OSError:
            return []
        key = (st.st_mtime_ns, st.st_size)
        if key == self._cache_key:
            return self._cache
        items = self._parse_locked()
        self._cache_key = key
        self._cache = items
        return items

    def _parse_locked(self) -> list[MemoryRecord]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        if not text.strip():
            return []
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            return []
        if not isinstance(raw, list):
            return []
        items: list[MemoryRecord] = []
        for r in raw:
            try:
                items.append(MemoryRecord.model_validate(r))
            except Exception:
                # One bad record must not poison the whole store.
                continue
        return items

    def _save_locked(self, items: list[MemoryRecord]) -> None:
        # Serialise BEFORE touching the cache or the temp file. A record whose
        # value is nested too deeply makes model_dump raise ValueError; when
        # that happened mid-save the temp file was left half-written, the
        # cache was never invalidated, and the in-memory list kept serving the
        # unserialisable record -- so one bad append bricked every later read
        # and write in that drawer until the process restarted.
        try:
            payload = json.dumps([i.model_dump(mode="json") for i in items],
                                 indent=2)
        except Exception as e:
            self._cache_key = None      # force a re-parse from disk
            self._cache = []
            raise ValueError(
                f"memory record is not serialisable: {e}") from e
        tmp = self.path.with_name(
            f"{self.path.name}.tmp-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}"
        )
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            self._cache_key = None
            self._cache = []
            raise
        # Invalidate: the on-disk file is now a different (mtime, size), and
        # the next _load_locked must re-parse rather than serve the old list.
        self._cache_key = None
        self._cache = []

    # ── public API ─────────────────────────────────────────────────────────

    def load(self) -> list[MemoryRecord]:
        with self._lock:
            return self._load_locked()

    def append(self, item: MemoryRecord) -> MemoryRecord:
        """Append one item atomically. Returns the item for convenience.

        ``_load_locked`` may hand back the cached list object itself, so it is
        copied before mutation. Appending in place used to mutate the shared
        cache even when the save then failed, leaving a record in memory that
        was never on disk and could never be written again.
        """
        with self._lock:
            items = list(self._load_locked())
            items.append(item)
            self._save_locked(items)
            return item

    def replace(self, items: list[MemoryRecord]) -> None:
        """Overwrite the whole store atomically (sweeps, retention trims)."""
        with self._lock:
            self._save_locked(list(items))

    def list_recent(self, limit: int = 50,
                    session_id: str | None = None) -> list[MemoryRecord]:
        """Newest-first items, optionally scoped to one session (+ globals)."""
        with self._lock:
            items = self._load_locked()
        if session_id is not None:
            items = [i for i in items
                     if i.session_id is None or i.session_id == session_id]
        return list(reversed(items))[:max(0, limit)]

    def remove(self, predicate) -> list[MemoryRecord]:
        """Remove every item matching ``predicate`` atomically. Returns the
        removed items (sweeps need to know if vectors must be rebuilt)."""
        with self._lock:
            items = self._load_locked()
            removed = [i for i in items if predicate(i)]
            if removed:
                doomed = {i.id for i in removed}
                self._save_locked([i for i in items if i.id not in doomed])
            return removed
