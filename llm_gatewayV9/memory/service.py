"""Retrieval strategy + write paths for gateway memory.

Moved here from the agent's ``memory.py`` (Session 7 logic, carried
forward unchanged in behaviour):

- reads run vector + keyword in parallel over one shared query vector
  and fuse with RRF (k=60): exact tokens and paraphrases both count,
  neither path can starve the other;
- writes embed the descriptor at insert time for kinds ``fact``,
  ``preference`` and ``tool_outcome``; ``scratchpad`` skips embedding;
- the embedding model is a project-level constant (``embedders.EMBED_DIM``);
  a vector of any other dim raises instead of silently corrupting recall.

What did NOT move: the LLM classifier prompt (fast-moving retrieval
strategy — the agent classifies free-form text into a structured record
and posts it here), the per-turn ``turnlog``, and prompt rendering.

Concurrency: embeddings are awaited OUTSIDE the lock (they are pure I/O
with no shared state). The lock guards only the synchronous
validate-dim + JSON-persist + index-mutate + index-persist sequence, so a
slow embed can never block other writers.
"""
from __future__ import annotations

import json
import re
import threading
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from embedders import EMBED_DIM

from .models import (
    DRAWER_WRITERS,
    KIND_DRAWER,
    DocSpan,
    DrawerName,
    MemoryRecord,
    MemoryScope,
    Principal,
    Role,
    SourceRef,
    new_id,
)
from .store import MemoryStore
from .vector import VectorIndex

# Kinds for which an embedding is computed at write time. Scratchpad items
# are run-scoped and skip the vector path.
EMBEDDABLE_KINDS = {"fact", "preference", "tool_outcome"}

# Descriptors are one human-readable line; cap defensively so a runaway
# classifier reply can never bloat the store or the prompt.
DESCRIPTOR_MAX = 200

_STOPWORDS = {
    "the", "is", "a", "an", "of", "to", "and", "or", "in", "on", "for", "at",
    "with", "by", "from", "what", "how", "when", "where", "why", "this", "that",
    "it", "be", "as", "are", "was", "were", "i", "you", "me", "my", "your",
}

# Async embed hook: (text, task_type) -> vector | None. Routes pass the
# gateway failover ring; tests pass a deterministic fake. None means
# "embedder unavailable" — writes persist without a vector, reads fall
# back to keywords.
EmbedFn = Callable[[str, str], Awaitable[list[float] | None]]


def tokens(text: str) -> set[str]:
    return {
        w for w in re.findall(r"\w+", text.lower())
        if w not in _STOPWORDS and len(w) > 2
    }


# Sentinel distinguishing "no vector attempted yet" (embed inside) from
# "vector attempted, none available" (skip vector path). The plane embeds
# once and fans the result out to every drawer service.
_UNSET: object = object()


def route_drawer(kind: str, value: dict | None) -> DrawerName:
    """Phase 1 write routing: classifier kinds → drawers; indexer document
    spans (``value.chunk``) always land in the document drawer."""
    if value and value.get("chunk") is not None:
        return "document"
    try:
        return KIND_DRAWER[kind]
    except KeyError:
        raise ValueError(f"unknown memory kind {kind!r}") from None


def check_writer(drawer: str, role: str) -> None:
    """Fail-closed writer enforcement. Raises PermissionError naming the
    drawer, the role, and who may write."""
    allowed = DRAWER_WRITERS.get(drawer)  # type: ignore[arg-type]
    if allowed is None:
        raise ValueError(f"unknown drawer {drawer!r}")
    if role not in allowed:
        raise PermissionError(
            f"role {role!r} may not write drawer {drawer!r} "
            f"(allowed: {sorted(allowed)})"
        )


def _expired(rec: MemoryRecord, now: datetime) -> bool:
    if rec.expires_at is None:
        return False
    exp = rec.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)  # stored naive = UTC
    return exp <= now


# Reciprocal Rank Fusion constant (2009 SIGIR default, holds up across
# domains — deliberately untuned; tune only against a labeled eval set).
RRF_K = 60


def rrf_fuse(vec_hits: list[MemoryRecord],
             kw_hits: list[MemoryRecord],
             top_k: int) -> list[MemoryRecord]:
    """Fuse one vector ranking + one keyword ranking by rank position
    (never raw scores — cosine and token-overlap live on incomparable
    scales). An item high in both lists outranks an item high in one;
    single-list items still score, so fused recall is never worse than
    either path alone. Ties break toward vector order (stable sort over
    vector-first insertion). Deterministic: no randomness, no tuning."""
    scores: dict[str, float] = {}
    order: dict[str, MemoryRecord] = {}
    for rank, item in enumerate(vec_hits, start=1):
        scores[item.id] = scores.get(item.id, 0.0) + 1.0 / (RRF_K + rank)
        order.setdefault(item.id, item)
    for rank, item in enumerate(kw_hits, start=1):
        scores[item.id] = scores.get(item.id, 0.0) + 1.0 / (RRF_K + rank)
        order.setdefault(item.id, item)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    return [order[i] for i, _ in ranked[:max(0, top_k)]]


class MemoryService:
    """Single-writer memory service over one state dir."""

    def __init__(self, state_dir: str | Path,
                 embed_fn: EmbedFn | None = None,
                 expected_dim: int = EMBED_DIM):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.store = MemoryStore(self.state_dir / "memory.json")
        self._embed = embed_fn
        self._write_lock = threading.Lock()
        try:
            self.index: VectorIndex | None = VectorIndex(
                self.state_dir, expected_dim=expected_dim)
        except RuntimeError as e:
            # faiss missing — keyword-only mode, said loudly once.
            print(f"[memory] vector search disabled: {e}")
            self.index = None
        if self.index is not None:
            self._reconcile_index()

    def _reconcile_index(self) -> None:
        """Heal index drift: the pre-migration writer never pruned vectors
        for deleted items, so the on-disk index can reference ids that no
        longer exist (they are silently skipped at read time, which
        degrades recall without a word). If any orphan ids are found,
        rebuild the index from the currently-embedded items — loudly."""
        assert self.index is not None
        with self._write_lock:
            live_ids = {i.id for i in self.store.load() if i.embedding}
            orphans = [i for i in self.index._ids if i not in live_ids]
            if not orphans:
                return
            print(f"[memory] index holds {len(orphans)} orphan vector(s) for "
                  f"deleted items — rebuilding from {len(live_ids)} live "
                  f"embedded items")
            self._rebuild_locked()
            print(f"[memory] index rebuilt: size={self.index.size}")

    def _rebuild_locked(self) -> None:
        """Clear the index and re-add every live embedded item. Caller must
        hold ``_write_lock``."""
        assert self.index is not None
        self.index.clear()
        for item in self.store.load():
            if item.embedding and item.kind in EMBEDDABLE_KINDS:
                try:
                    self.index.add(item.id, item.embedding)
                except ValueError as e:
                    print(f"[memory] skipping unindexable item {item.id}: {e}")
        self.index.persist()

    def delete_one(self, memory_id: str):
        """Delete a single record by id. Rebuilds the vector index when the
        removed record carried an embedding, so a deleted memory cannot still
        be returned by search. Returns the removed record, or None.

        Lock order mirrors ``sweep_expired`` on purpose: the store takes its
        own lock inside ``remove`` and releases it before we take
        ``_write_lock``, so the two are never held at once.
        """
        removed = self.store.remove(lambda i: i.id == memory_id)
        if removed and self.index is not None and any(
                r.embedding for r in removed):
            with self._write_lock:
                self._rebuild_locked()
        return removed[0] if removed else None

    def sweep_expired(self) -> int:
        """Delete expired records (working TTL). Rebuilds the index when a
        removed record carried a vector. Returns the removed count."""
        now = datetime.now(timezone.utc)
        removed = self.store.remove(lambda i: _expired(i, now))
        if removed and self.index is not None and any(
                r.embedding for r in removed):
            with self._write_lock:
                self._rebuild_locked()
        return len(removed)

    # ── embedding ──────────────────────────────────────────────────────────

    async def _embed_text(self, text: str,
                          task_type: str) -> tuple[list[float], str] | None:
        """Return (vector, model_name), or None if unavailable.

        The model name comes back with the vector so it can be stored on the
        record. Losing it is how mixed-model vectors end up in one index.
        """
        if self._embed is None:
            return None
        try:
            res = await self._embed(text, task_type)
        except Exception as e:
            print(f"[memory] embedding failed ({e!r}); continuing without vector")
            return None
        if not res:
            return None
        # An embedder may return a bare vector (legacy) or a mapping carrying
        # provenance. Accept both rather than breaking existing callers --
        # and check for the KEYS rather than isinstance, because a Mapping
        # that is not a dict would otherwise fall into the vector branch and
        # be stored as a vector made of its own key names.
        if hasattr(res, "get") and hasattr(res, "keys"):
            vec = res.get("embedding") or []
            model = str(res.get("model") or res.get("embed_model") or "")
        else:
            vec, model = list(res), ""
        if not vec:
            return None
        return list(vec), model

    def _matches_index(self, item: MemoryRecord) -> bool:
        """Is this record's vector comparable with the live index?

        Rejects a record whose vector came from a different model. Comparing
        two embedding spaces is meaningless and quietly returns nonsense
        neighbours, so a mismatch excludes the record rather than degrading
        the whole search.
        """
        if not item.embedding:
            return False
        if item.embed_dim is not None and \
                item.embed_dim != self._embed_dim():
            return False
        if item.embed_model and self._embed_model() and \
                item.embed_model != self._embed_model():
            return False
        return True

    def _embed_model(self) -> str:
        try:
            return str(os.environ.get("EMBED_OLLAMA_MODEL",
                                       "nomic-embed-text"))
        except Exception:
            return ""

    def _embed_dim(self) -> int:
        return EMBED_DIM

    # ── scoping ────────────────────────────────────────────────────────────

    @staticmethod
    def _visible(items: list[MemoryRecord],
                 session_id: str | None) -> list[MemoryRecord]:
        now = datetime.now(timezone.utc)
        out = []
        for i in items:
            if _expired(i, now):
                continue
            if session_id is not None and not (
                    i.session_id is None or i.session_id == session_id):
                continue
            out.append(i)
        return out

    # ── writes ─────────────────────────────────────────────────────────────

    def _check_dim(self, vec: list[float]) -> None:
        """Refuse a differently-dimmed vector LOUDLY, before anything is
        persisted — a mixed-dim index ranks garbage."""
        if (self.index is not None and self.index.dim is not None
                and len(vec) != self.index.dim):
            raise ValueError(
                f"Embedding dim {len(vec)} does not match index dim "
                f"{self.index.dim}. The embedding model must stay fixed for "
                f"the lifetime of an index."
            )

    def _persist_embedded(self, item: MemoryRecord) -> MemoryRecord:
        with self._write_lock:
            if item.embedding is not None and item.kind in EMBEDDABLE_KINDS:
                self._check_dim(item.embedding)
            self.store.append(item)
            if (item.embedding is not None and item.kind in EMBEDDABLE_KINDS
                    and self.index is not None):
                self.index.add(item.id, item.embedding)
                self.index.persist()
        return item

    async def remember(self, *, kind: str, descriptor: str,
                       keywords: list[str] | None = None,
                       value: dict | None = None,
                       source: str, run_id: str,
                       goal_id: str | None = None,
                       session_id: str | None = None,
                       drawer: DrawerName | None = None,
                       principal_role: Role = "agent",
                       principal_id: str = "assistant",
                       scope: MemoryScope | None = None,
                       sources: list[SourceRef] | None = None,
                       expires_at: datetime | None = None,
                       supersedes: str | None = None,
                       doc: DocSpan | None = None) -> MemoryRecord:
        """Structured write. The caller (agent) has already classified
        free-form content; ``kind`` arrives decided. ``drawer`` defaults to
        the kind→drawer route (the plane passes it explicitly after
        enforcing the writer map; direct callers get routing + a default
        ``agent`` principal).

        Raises PermissionError (fail-closed) when the role may not write
        the routed drawer.
        """
        resolved = drawer or route_drawer(kind, value)
        check_writer(resolved, principal_role)
        descriptor = (descriptor or "")[:DESCRIPTOR_MAX]
        kws = [k.lower() for k in (keywords or [])][:12]
        embedding: list[float] | None = None
        embed_model: str | None = None
        embed_dim: int | None = None
        if kind in EMBEDDABLE_KINDS:
            got = await self._embed_text(descriptor, "retrieval_document")
            if got:
                embedding, embed_model = got[0], (got[1] or None)
                embed_dim = len(embedding)
        return self._persist_embedded(MemoryRecord(
            id=new_id("mem"), kind=kind,  # type: ignore[arg-type]
            drawer=resolved,
            scope=scope or MemoryScope(),
            keywords=kws, descriptor=descriptor, value=value or {},
            sources=sources or [],
            principal=Principal(id=principal_id, role=principal_role),
            embedding=embedding, source=source, run_id=run_id,
            embed_model=embed_model, embed_dim=embed_dim,
            goal_id=goal_id, session_id=session_id,
            expires_at=expires_at, supersedes=supersedes, doc=doc,
        ))

    async def add_fact(self, descriptor: str, *,
                       value: dict | None = None,
                       keywords: list[str] | None = None,
                       source: str, run_id: str,
                       goal_id: str | None = None,
                       session_id: str | None = None,
                       principal_role: Role = "agent",
                       doc: DocSpan | None = None) -> MemoryRecord:
        """Direct fact write (document indexing, seeded tests). Skips the
        LLM classifier — kind is known — but still embeds the descriptor.
        Chunk payloads route to the document drawer."""
        if not keywords:
            keywords = list(tokens(descriptor))[:10]
        return await self.remember(
            kind="fact", descriptor=descriptor, keywords=keywords,
            value=value, source=source, run_id=run_id, goal_id=goal_id,
            session_id=session_id, principal_role=principal_role, doc=doc)

    async def record_outcome(self, *, tool: str, arguments: dict,
                             result_text: str,
                             artifact_id: str | None,
                             run_id: str, goal_id: str | None,
                             session_id: str | None = None,
                             principal_role: Role = "runtime") -> MemoryRecord:
        """Zero-LLM-classify write for a deterministic tool outcome. Kind is
        ``tool_outcome`` by construction, drawer is ``episode``."""
        check_writer("episode", principal_role)
        arg_words: list[str] = []
        for v in arguments.values():
            if isinstance(v, str):
                arg_words += tokens(v)
            elif isinstance(v, (int, float)):
                arg_words.append(str(v))
        keywords = list({tool.lower(), *arg_words})[:10]
        descriptor = f"{tool}({json.dumps(arguments)[:80]}) -> "
        if artifact_id:
            descriptor += f"artifact {artifact_id}"
        else:
            descriptor += result_text[:120].replace("\n", " ")
        got = await self._embed_text(descriptor, "retrieval_document")
        embedding = got[0] if got else None
        embed_model = (got[1] or None) if got else None
        embed_dim = len(embedding) if embedding else None
        return self._persist_embedded(MemoryRecord(
            id=new_id("mem"), kind="tool_outcome", drawer="episode",
            keywords=keywords,
            descriptor=descriptor[:DESCRIPTOR_MAX],
            value={"tool": tool, "arguments": arguments,
                   "result_preview": result_text[:400]},
            principal=Principal(role=principal_role),
            artifact_id=artifact_id, embedding=embedding,
            embed_model=embed_model, embed_dim=embed_dim,
            source="action", run_id=run_id, goal_id=goal_id,
            session_id=session_id,
        ))

    # ── reads ──────────────────────────────────────────────────────────────

    def _keyword_search(self, query: str, history: list[dict] | None, *,
                        kinds: list[str] | None,
                        top_k: int,
                        session_id: str | None) -> list[MemoryRecord]:
        items = self._visible(self.store.load(), session_id)
        if kinds:
            items = [i for i in items if i.kind in kinds]
        qtoks = tokens(query)
        if history:
            for h in history[-3:]:
                qtoks |= tokens(json.dumps(h, default=str))
        scored: list[tuple[int, MemoryRecord]] = []
        for item in items:
            if item.superseded_by is not None:
                continue  # corrected claims never surface in recall
            itoks = {w.lower() for w in item.keywords} | tokens(item.descriptor)
            score = len(qtoks & itoks)
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda x: -x[0])
        return [i for _, i in scored[:top_k]]

    async def _vector_search(self, query: str, *,
                             kinds: list[str] | None, top_k: int,
                             session_id: str | None,
                             qvec: list[float] | None | object = _UNSET,
                             ) -> list[MemoryRecord]:
        idx = self.index
        if idx is None or idx.size == 0:
            return []
        if qvec is _UNSET:
            # Direct use: embed inside (legacy behaviour). The plane passes
            # an already-attempted vector (or None) so one query costs one
            # embed across all drawers instead of one per drawer.
            got = await self._embed_text(query, "retrieval_query")
            qvec = got[0] if got else None
        elif isinstance(qvec, tuple):
            # The plane may hand over (vector, model) so the query and the
            # index can be checked for the same model.
            qvec = qvec[0] if qvec else None
        if qvec is None:
            return []
        try:
            with self._write_lock:
                hits = idx.search(qvec, k=top_k * 2 if kinds else top_k)  # type: ignore[arg-type]
        except ValueError as e:
            # Query vector dim drifted from the index (model swap without a
            # rebuild) — loud skip to the keyword path, never a 500.
            print(f"[memory] vector search skipped ({e})")
            return []
        if not hits:
            return []
        # Drop any hit whose vector came from a DIFFERENT embedding model.
        # The index stores ids only, so this is the point where a mixed-model
        # record would otherwise be returned as a neighbour.
        live = self._embed_model()
        if live:
            by_id = {i.id: i for i in self.store.load()}
            hits = [(score, hid) for score, hid in hits
                    if (by_id.get(hid) is None
                        or self._matches_index(by_id[hid]))]
        by_id = {item.id: item
                 for item in self._visible(self.store.load(), session_id)}
        out: list[MemoryRecord] = []
        for item_id, _score in hits:
            item = by_id.get(item_id)
            if item is None:
                continue
            if item.superseded_by is not None:
                continue  # corrected claims never surface in recall
            if kinds and item.kind not in kinds:
                continue
            out.append(item)
            if len(out) >= top_k:
                break
        return out

    async def read(self, query: str, history: list[dict] | None = None, *,
                   kinds: list[str] | None = None, top_k: int = 8,
                   session_id: str | None = None,
                   qvec: list[float] | None | object = _UNSET,
                   ) -> list[MemoryRecord]:
        """Hybrid recall: vector + keyword run in parallel (one shared
        query vector) and fuse with RRF. Replaces the old vector-first
        fallback chain, which served vector misses with zero keyword
        input and keyword queries with zero vector input."""
        if not (query or "").strip():
            return []
        # Over-fetch candidates: fusion needs room to disagree with either
        # path before the final cut. Cheap locally (JSON already loaded).
        fetch = max(top_k * 2, 10)
        vec_hits = await self._vector_search(query, kinds=kinds, top_k=fetch,
                                             session_id=session_id, qvec=qvec)
        kw_hits = self._keyword_search(query, history, kinds=kinds,
                                       top_k=fetch, session_id=session_id)
        return rrf_fuse(vec_hits, kw_hits, top_k)

    # ── maintenance ────────────────────────────────────────────────────────

    def list_recent(self, limit: int = 50,
                    session_id: str | None = None) -> list[MemoryRecord]:
        return self.store.list_recent(limit=limit, session_id=session_id)

    def clear(self, session_id: str | None = None,
              older_than: datetime | None = None) -> int:
        """Wipe records, optionally scoped to one session and/or to records
        created before ``older_than`` (run-end working purge uses both so a
        just-written note from the ending run is never caught in its own
        purge). With a session filter, global records survive; without one,
        everything goes. Rebuilds the index when vectors were removed."""
        if older_than is not None and older_than.tzinfo is None:
            older_than = older_than.replace(tzinfo=timezone.utc)

        def _created(rec: MemoryRecord) -> datetime:
            c = rec.created_at
            return c if c.tzinfo is not None else c.replace(tzinfo=timezone.utc)

        def _match(rec: MemoryRecord) -> bool:
            if session_id is not None and rec.session_id != session_id:
                return False
            if older_than is not None and _created(rec) >= older_than:
                return False
            return True

        removed = self.store.remove(_match)
        if session_id is None and self.index is not None:
            with self._write_lock:
                self.index.clear()
        elif removed and self.index is not None and any(
                r.embedding for r in removed):
            with self._write_lock:
                self._rebuild_locked()
        return len(removed)

    def stats(self) -> dict:
        items = self.store.load()
        by_kind: dict[str, int] = {}
        embedded = 0
        sessions: set[str] = set()
        for i in items:
            by_kind[i.kind] = by_kind.get(i.kind, 0) + 1
            if i.embedding:
                embedded += 1
            if i.session_id:
                sessions.add(i.session_id)
        return {
            "items": len(items),
            "by_kind": by_kind,
            "embedded": embedded,
            "sessions": len(sessions),
            "index_size": self.index.size if self.index is not None else 0,
            "index_dim": self.index.dim if self.index is not None else None,
            "vector_enabled": self.index is not None,
            "embed_dim": EMBED_DIM,
        }
