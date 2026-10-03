"""Wire schema for the gateway memory service.

Mirrors the agent's ``schemas.MemoryItem`` field-for-field so responses
validate cleanly on the agent side, plus an optional ``session_id`` that
scopes an item to one conversation. Items with ``session_id=None`` are
global and visible to every session (this is also how pre-migration items
behave).

Phase 1 (seven drawers): new writes are ``MemoryRecord`` — the spec's
record shape (drawer + scope + sources + principal + lifetime fields) —
while ``kind`` stays first-class as the agent-facing vocabulary (the
kind→drawer table routes new writes; pre-drawer items keep validating
with ``drawer=None`` and their stored bytes are never rewritten).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def new_id(prefix: str = "mem") -> str:
    return f"{prefix}:{uuid4().hex[:8]}"


MemoryKind = Literal["fact", "preference", "tool_outcome", "scratchpad"]

# Phase 1 drawers: the cabinet. Permissions and lifetimes differ per
# drawer (see plane.DRAWER_WRITERS); retrieval never brute-forces across
# all of them.
DrawerName = Literal[
    "working", "episode", "fact", "playbook", "policy", "audit", "document",
]

# Who is asking for the write. The call path sets it (remember→agent,
# record_outcome→runtime, add_fact→indexer, policy UI→operator,
# service-internals→gateway); the plane enforces per-drawer maps.
# Localhost trust boundary: a local caller could lie, so this is a
# contract + bug-catcher, not authentication (stated openly).
Role = Literal["runtime", "agent", "indexer", "operator", "system", "gateway"]


class MemoryScope(BaseModel):
    """Single-scope default (no multi-tenancy machinery in Phase 1)."""

    tenant_id: str = "course"
    project_id: str = "s9"
    user_id: str = "local"
    agent_id: str = "assistant"


class SourceRef(BaseModel):
    uri: str = ""
    author: str = ""


class Principal(BaseModel):
    id: str = "assistant"
    role: Role = "agent"


class DocSpan(BaseModel):
    """Sourced span for document-drawer records (Phase 5 fills neighbours)."""

    doc_id: str = ""
    version: str = "1"
    chunk_index: int = 0
    total_chunks: int = 1


# ── drawer policy tables ───────────────────────────────────────────────────
# Kept here (not in plane/service) so both import them without a cycle.
# kind = content vocabulary (agent-facing, frozen); drawer = governance
# (permissions, lifetime, retrieval routing).
# New-write routing: classifier kinds → drawers. Chunk payloads (indexer
# document spans) always land in the document drawer regardless of kind.
KIND_DRAWER: dict[str, DrawerName] = {
    "fact": "fact",
    "preference": "fact",
    "tool_outcome": "episode",
    "scratchpad": "working",
}

# Fail-closed writer map. Call paths set the role (remember→agent,
# record_outcome→runtime, add_fact→indexer, policy UI→operator,
# service-internals→gateway); the plane enforces before any write.
DRAWER_WRITERS: dict[DrawerName, frozenset] = {
    "working": frozenset({"runtime", "agent", "operator", "system", "gateway"}),
    "episode": frozenset({"runtime", "agent", "operator", "system", "gateway"}),
    "fact": frozenset({"agent", "indexer", "operator", "system", "gateway"}),
    "playbook": frozenset({"operator", "system", "gateway"}),
    "policy": frozenset({"operator", "system"}),
    "audit": frozenset({"gateway"}),
    "document": frozenset({"indexer", "operator", "system", "gateway"}),
}


class PayloadTooLarge(ValueError):
    """Structured payload over the per-write cap (413, not silent cut)."""


class MemoryItem(BaseModel):
    """One record in memory. Bytes never live here; they live in the
    agent-side artifact store (referenced via ``artifact_id``)."""

    id: str
    kind: MemoryKind
    keywords: list[str] = Field(default_factory=list)
    descriptor: str                              # one short human-readable line
    value: dict = Field(default_factory=dict)    # structured payload
    artifact_id: str | None = None
    embedding: list[float] | None = None         # set by the service at write time
    # Which model produced `embedding`, and at what width.
    #
    # Without this a vector carries no provenance: if the embedder ever
    # changes, records written by the old model sit in the same FAISS index
    # as new ones and get compared against each other silently. `embed_dim`
    # already had an ad-hoc drift check in the service; recording the model
    # makes the same guarantee explicit and per-record.
    embed_model: str | None = None
    embed_dim: int | None = None
    source: str
    run_id: str
    goal_id: str | None = None
    session_id: str | None = None                # None = global across sessions
    confidence: float = 1.0
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MemoryRecord(MemoryItem):
    """Phase 1 record: everything MemoryItem carries, plus the drawer
    plane (governance). ``kind`` stays as the content vocabulary;
    ``drawer`` decides permissions, lifetime and retrieval routing.

    ``drawer=None`` marks pre-drawer (legacy) provenance — those stored
    bytes are never rewritten, they just validate here for reads.
    """

    drawer: DrawerName | None = None
    scope: MemoryScope = Field(default_factory=MemoryScope)
    sources: list[SourceRef] = Field(default_factory=list)
    principal: Principal = Field(default_factory=Principal)
    expires_at: datetime | None = None          # working TTL (Phase 2 sweeps)
    supersedes: str | None = None               # id this record corrects (P3)
    superseded_by: str | None = None            # set on the old record (P3)
    doc: DocSpan | None = None                  # document span (P5)
