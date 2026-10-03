"""Gateway memory service: durable fact store + vector retrieval.

Single-writer home for everything the agent used to keep in
``S9SharedCode/code/state/memory.json`` + ``index.faiss``:

- :mod:`memory.models` — wire schema (mirrors the agent's
  ``schemas.MemoryItem`` plus an optional ``session_id`` scope).
- :mod:`memory.store` — locked, atomic JSON persistence.
- :mod:`memory.vector` — FAISS index (canonical copy; the agent's
  ``vector_index.py`` is retained only for its static math checks).
- :mod:`memory.service` — retrieval strategy (vector-first, keyword
  fallback) and all write paths.

The embedding model is pinned gateway-wide (see ``embedders.EMBED_DIM``);
vectors of any other dim are refused loudly, never silently dropped.
"""
from __future__ import annotations

from .models import (
    DRAWER_WRITERS,
    KIND_DRAWER,
    DocSpan,
    DrawerName,
    MemoryItem,
    MemoryRecord,
    MemoryScope,
    Principal,
    Role,
    SourceRef,
)
from .plane import ALL_DRAWERS, RECALL_ORDER, SESSION_DRAWERS, MemoryPlane
from .service import RRF_K, MemoryService, check_writer, route_drawer, rrf_fuse
from .store import MemoryStore

__all__ = [
    "ALL_DRAWERS",
    "DRAWER_WRITERS",
    "KIND_DRAWER",
    "RECALL_ORDER",
    "RRF_K",
    "SESSION_DRAWERS",
    "DocSpan",
    "DrawerName",
    "MemoryItem",
    "MemoryPlane",
    "MemoryRecord",
    "MemoryScope",
    "MemoryService",
    "MemoryStore",
    "Principal",
    "Role",
    "SourceRef",
    "check_writer",
    "route_drawer",
    "rrf_fuse",
]
