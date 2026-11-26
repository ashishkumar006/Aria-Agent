"""The cabinet: legacy store + seven drawers under one roof.

Phase 1 layout (``root`` is the gateway ``state/`` dir)::

    state/memory.json + index.faiss   legacy (pre-drawer items, frozen:
                                      reads only + global clear; stored
                                      bytes are never rewritten)
    state/memory/<drawer>/           one JSON + FAISS per drawer, all new
                                      writes land here

The plane owns what no single drawer can: kind→drawer routing, the
fail-closed writer map, merged recall (one query embed fanned out to
every drawer instead of one brute-force scan), and the audit trail.
``MemoryService`` stays a single-drawer engine; this class is the only
place that knows there are eight stores.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from pathlib import Path

from embedders import EMBED_DIM

from atomic_json import load_json, save_json

from .models import (
    DocSpan,
    DrawerName,
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    PayloadTooLarge,
    SourceRef,
    new_id,
)
from .service import MemoryService, check_writer, route_drawer

# Recall priority for merged reads. Audit is review-only: searchable when
# named explicitly, never injected into prompts by default.
RECALL_ORDER: list[DrawerName] = [
    "policy", "profile", "fact", "playbook", "document", "episode", "working",
]
ALL_DRAWERS: list[DrawerName] = [
    "working", "episode", "fact", "playbook", "policy", "audit", "document",
    "profile",
]

# Phase 2: the session-start drawer set. Working notes are run-scoped by
# definition (a new run has no notes yet), so session recall is everything
# else. The agent passes this list explicitly — the contract is pinned
# here, not inferred per query.
SESSION_DRAWERS: list[str] = [
    "policy", "profile", "fact", "playbook", "document", "episode", "legacy",
]

# Working notes disappear: default TTL applied server-side when the write
# carries no explicit expires_at (Phase 2; sweeps enforce it).
WORKING_TTL_MINUTES = 60


# How deep each drawer is searched when the caller restricts results to a set
# of `doc_ids`. The allowlist is applied AFTER the drawer's global top-K, so a
# scoped search that fetched only `top_k*2` could miss a genuinely-matching
# chunk ranked below that cut and return nothing (a single common term matched
# 73 chunks across the library; the wanted one ranked ~22nd and was never
# fetched). Bounded so the deep fetch stays cheap (FAISS/BM25 top-N only).
_DOC_SCOPED_FETCH = 200


# Minimum per-drawer candidate pool for every search. The per-document
# diversity pass can only pick from what the drawer returned, so fetching
# exactly top_k often yields top_k chunks of ONE document and leaves nothing
# to diversify into ("radiation" gave 10 hits from 2 docs). Cheap locally:
# both the vector and BM25 paths are top-N over an in-memory index.
_DOC_FETCH_FLOOR = 40


# Most chunks one document may contribute to a single result set before the
# selection moves on to other documents. Purely a diversity cap: it is
# backfilled from the same ranked list when fewer documents match, so it never
# shrinks a result below top_k - it only stops one document's many near-
# duplicate chunks from filling every slot.
_DOC_CHUNK_CAP = 2


# Maximum JSON-encoded structured payload per write. Descriptors are
# already capped at 200 chars; this bounds `value`/`arguments` so one
# runaway writer can't bloat the store files (413, not silent truncation).
MAX_PAYLOAD_BYTES = 100_000


# Deadline for the ONE query embed a merged recall needs. The embedder's own
# socket timeout is 60s, which made a cold Ollama the caller's problem: of
# 15,917 recorded searches the median was 1.4s, 1,164 passed 20s and the worst
# hit 78.5s — and a two-character "hi" paid 35.5s of that. Keyword recall
# answers in milliseconds without a vector, so this trades a slow search for a
# shallower one rather than making a stranger wait.
QUERY_EMBED_BUDGET_S = 8.0


def _check_payload_size(value: dict | None,
                        arguments: dict | None = None) -> None:
    import json as _json
    for label, payload in (("value", value), ("arguments", arguments)):
        if payload is None:
            continue
        try:
            size = len(_json.dumps(payload, default=str).encode("utf-8"))
        except Exception:
            size = 0
        if size > MAX_PAYLOAD_BYTES:
            raise PayloadTooLarge(
                f"{label} payload is {size} bytes (cap "
                f"{MAX_PAYLOAD_BYTES}) — chunk the content instead")


# Kinds that describe a standing claim about the user, where a second
# identical write is a REINFORCEMENT rather than a second memory. Facts are
# deliberately excluded: two identical facts can be legitimate (the same
# sentence quoted in two documents), and document spans must never merge.
_CONVERGING_KINDS = frozenset({"observation", "preference"})


def _descriptor_key(text: str) -> str:
    """Case/punctuation/whitespace-insensitive key for exact repeated claims.

    This is intentionally EQUALITY, not similarity: the agent already has a
    fuzzy topic matcher for paraphrases. The gateway only guarantees that an
    identical claim arriving twice (a retry, two extractor threads, a client
    re-post) cannot become two rows.
    """
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _merge_repeat(prior: MemoryRecord, descriptor: str,
                  keywords: list[str] | None, value: dict | None,
                  confidence: float) -> tuple[str, list[str], dict, float]:
    """Fold a repeat sighting into the shape of the existing record.

    Keeps the established descriptor, evidence and keywords; counts the new
    sighting; records the newest quote for review; never lowers confidence.
    """
    incoming = dict(value or {})
    merged = dict(prior.value or {})
    for k, v in incoming.items():
        if k not in merged or merged[k] in (None, "", [], {}):
            merged[k] = v
    old_seen = int((prior.value or {}).get("seen_count") or 0) or 1
    new_seen = int(incoming.get("seen_count") or 0) or 1
    merged["seen_count"] = old_seen + new_seen
    if incoming.get("evidence"):
        # `evidence` stays the quote that established the claim; the newest
        # one is added beside it so a reviewer can see the fresh support.
        merged.setdefault("evidence", incoming["evidence"])
        merged["latest_quote"] = str(incoming["evidence"])[:600]
    kws = list(dict.fromkeys([*(prior.keywords or []),
                              *(keywords or [])]))[:12]
    return (prior.descriptor or descriptor, kws, merged,
            max(float(prior.confidence or 0.0), float(confidence or 0.0)))


class MemoryPlane:
    """Single-writer cabinet over one state root."""

    def __init__(self, root: str | Path,
                 embed_fn=None,
                 expected_dim: int = EMBED_DIM):
        self.root = Path(root)
        self._embed = embed_fn
        self.legacy = MemoryService(self.root, embed_fn=embed_fn,
                                    expected_dim=expected_dim)
        self.drawers: dict[str, MemoryService] = {
            d: MemoryService(self.root / "memory" / d, embed_fn=embed_fn,
                             expected_dim=expected_dim)
            for d in ALL_DRAWERS
        }
        import threading as _th
        self._versions_lock = _th.Lock()
        # Serialises "is this an exact repeat?" + write for profile claims.
        # Two extractor threads two seconds apart used to read the same
        # snapshot and both append; the agent's lock only covers one process.
        # Embeds are awaited inside this section, but observation/preference
        # writes are rare and one short descriptor each, so the trade is
        # deliberate: correctness on a standing claim beats a few ms.
        self._converge_lock = asyncio.Lock()
        # Startup sweep: drop already-expired records (working TTL) before
        # serving traffic, loudly so expiry is observable, not silent.
        swept = self.sweep()
        if any(swept.values()):
            print(f"[memory] startup sweep removed {swept}")

    # ── Phase 5: document versions ─────────────────────────────────────────
    # "Current" = the most recently WRITTEN version per doc (recency, not
    # ordering — version strings are opaque). A re-index under a new
    # version flips current; older chunks hide from recall (unless
    # include_stale) and stay listed for review. Sidecar lives next to the
    # document store; flips are logged loudly.

    def _versions_path(self) -> Path:
        return self.root / "memory" / "document" / "versions.json"

    def _doc_versions(self) -> dict[str, str]:
        with self._versions_lock:
            data = load_json(self._versions_path(), None)
            return dict(data) if isinstance(data, dict) else {}

    def _note_doc_version(self, doc_id: str, version: str) -> None:
        if not doc_id:
            return
        with self._versions_lock:
            data = load_json(self._versions_path(), None)
            versions = dict(data) if isinstance(data, dict) else {}
            if versions.get(doc_id) != version:
                print(f"[memory] document {doc_id!r} current version → "
                      f"{version!r} (was {versions.get(doc_id)!r})")
                versions[doc_id] = version
                save_json(self._versions_path(), versions)

    # ── writes ─────────────────────────────────────────────────────────────

    async def _regulated(self, drawer: DrawerName, role: str) -> None:
        """Enforce the writer map; audit denials, then fail closed. A
        failing audit trail must never mask the verdict itself (fail-closed
        on the permission, fail-open on the logging)."""
        try:
            check_writer(drawer, role)
        except PermissionError:
            try:
                await self._audit(
                    f"write denied: role {role!r} → drawer {drawer!r}",
                    {"drawer": drawer, "role": role},
                )
            except Exception as e:  # pragma: no cover - disk failure
                print(f"[memory] denial audit failed ({e!r})")
            raise

    async def remember(self, *, kind: str, descriptor: str,
                       keywords: list[str] | None = None,
                       value: dict | None = None,
                       source: str, run_id: str,
                       goal_id: str | None = None,
                       session_id: str | None = None,
                       drawer: DrawerName | None = None,
                       principal_role: str = "agent",
                       principal_id: str = "assistant",
                       scope: MemoryScope | None = None,
                       sources: list[SourceRef] | None = None,
                       expires_at: datetime | None = None,
                       supersedes: str | None = None,
                       doc: DocSpan | None = None,
                       confidence: float = 1.0) -> MemoryRecord:
        routed = drawer or route_drawer(kind, value)
        if value and value.get("chunk") is not None:
            routed = "document"  # indexer spans must stay spans
        if routed not in ALL_DRAWERS:
            raise ValueError(f"unknown drawer {routed!r}")
        if isinstance(doc, dict):
            doc = DocSpan(**doc)  # direct callers may pass raw dicts
        _check_payload_size(value, arguments=None)
        await self._regulated(routed, principal_role)
        if routed == "working" and expires_at is None:
            # Working notes disappear by default (spec: minutes or one run).
            # Naive UTC, matching created_at — mixed naive/aware datetimes
            # crash sorts and comparisons, so the plane stays naive and the
            # service normalizes defensively wherever it compares.
            expires_at = datetime.utcnow() + timedelta(
                minutes=WORKING_TTL_MINUTES)
        svc = self.drawers[routed]
        svc.sweep_expired()  # opportunistic: one cheap load, keeps TTL honest
        # An exact repeat of a standing claim is one memory seen twice, not
        # two memories. This used to depend entirely on the agent's fuzzy
        # matcher, so any other writer (a retry, a test, a direct API client)
        # could append a duplicate - and once one existed, the matcher kept
        # finding only the newest, leaving the rest live. Convergence belongs
        # here, where every writer passes.
        converge = kind in _CONVERGING_KINDS and not supersedes
        if converge:
            await self._converge_lock.acquire()
        try:
            if converge:
                prior = self._live_duplicate(routed, kind, descriptor)
                if prior is not None:
                    descriptor, keywords, value, confidence = _merge_repeat(
                        prior, descriptor, keywords, value, confidence)
                    supersedes = prior.id
            if supersedes:
                # The target must exist BEFORE the new record is written.
                #
                # It used to be linked afterwards, so a write whose target was
                # missing persisted the new record and then raised: the client
                # saw a 404 and believed nothing was stored, while the drawer
                # kept the orphan. Every repeat sighting of one observation
                # therefore left a duplicate behind - the profile drawer held
                # 80 copies of a single claim, and because they all superseded
                # the same vanished record, the duplicates crowded every
                # search (including exact keyword matches) out of the results.
                self._require_supersede_target(supersedes)
            item = await svc.remember(
                kind=kind, descriptor=descriptor, keywords=keywords,
                value=value, source=source, run_id=run_id, goal_id=goal_id,
                session_id=session_id, drawer=routed,
                principal_role=principal_role,  # type: ignore[arg-type]
                principal_id=principal_id, scope=scope, sources=sources,
                expires_at=expires_at, supersedes=supersedes, doc=doc,
                confidence=confidence)
            if supersedes:
                await self._mark_superseded(supersedes, item.id, routed)
            if routed == "document" and doc and doc.doc_id:
                self._note_doc_version(doc.doc_id, doc.version)
            return item
        finally:
            if converge:
                self._converge_lock.release()

    def _live_duplicate(self, drawer: str, kind: str,
                        descriptor: str) -> MemoryRecord | None:
        """Newest live record in `drawer` with an identical descriptor.

        Equality is normalised (case/punctuation/whitespace), never fuzzy:
        "prefers short answers" and "prefers brief answers" are the agent
        matcher's job, and merging them here would be a guess. A record that
        already has a successor is skipped - it is history, not a live claim.
        """
        key = _descriptor_key(descriptor)
        if not key:
            return None
        newest: MemoryRecord | None = None
        for rec in self.drawers[drawer].store.load():
            if rec.kind != kind or rec.superseded_by is not None:
                continue
            if _descriptor_key(rec.descriptor) != key:
                continue
            if newest is None or rec.created_at > newest.created_at:
                newest = rec
        return newest

    def _require_supersede_target(self, old_id: str) -> None:
        """Raise if the record a write wants to supersede is not there.

        Checked BEFORE the write, so a missing target cannot leave a
        persisted orphan behind: the caller gets a 404 and the drawer stays
        exactly as it was.
        """
        for _name, svc in self.drawers.items():
            for rec in svc.store.load():
                if rec.id == old_id:
                    return
        raise LookupError(
            f"supersedes target {old_id!r} not found in any drawer "
            f"(legacy records are frozen - re-add the corrected claim as "
            f"a new fact instead)")

    async def _mark_superseded(self, old_id: str, new_id: str,
                               new_drawer: str) -> None:
        """Link a correction: the new record supersedes the old one, which
        drops out of recall (search) while staying visible for review
        (list). Legacy records are frozen — they cannot be superseded;
        re-add the corrected claim as a drawer fact instead."""
        for name, svc in self.drawers.items():
            items = svc.store.load()
            for i, rec in enumerate(items):
                if rec.id == old_id:
                    if rec.superseded_by and rec.superseded_by != new_id:
                        print(f"[memory] {old_id} already superseded by "
                              f"{rec.superseded_by}; re-linking to {new_id}")
                    items[i] = rec.model_copy(
                        update={"superseded_by": new_id})
                    svc.store.replace(items)
                    await self._audit(
                        f"record {old_id} superseded by {new_id}",
                        {"old_id": old_id, "new_id": new_id,
                         "drawer": new_drawer})
                    return
        raise LookupError(
            f"supersedes target {old_id!r} not found in any drawer "
            f"(legacy records are frozen — re-add the corrected claim as "
            f"a new fact instead)")

    async def record_outcome(self, *, tool: str, arguments: dict,
                             result_text: str,
                             artifact_id: str | None,
                             run_id: str, goal_id: str | None,
                             session_id: str | None = None) -> MemoryRecord:
        _check_payload_size(None, arguments=arguments)
        await self._regulated("episode", "runtime")
        return await self.drawers["episode"].record_outcome(
            tool=tool, arguments=arguments, result_text=result_text,
            artifact_id=artifact_id, run_id=run_id, goal_id=goal_id,
            session_id=session_id)

    async def _audit(self, text: str, detail: dict) -> None:
        """Gateway-only append. Audit items are kind=fact (embedded for
        later review queries); the JSON append + index add are millisecond
        local ops on the caller's loop."""
        svc = self.drawers["audit"]
        vec = None
        if self._embed is not None:
            try:
                res = await self._embed(text, "retrieval_document")
                # The embedder now returns {"embedding", "model"}; taking
                # list() of the mapping stored its KEY NAMES as a vector.
                if res and hasattr(res, "get") and hasattr(res, "keys"):
                    vec = list(res.get("embedding") or []) or None
                else:
                    vec = list(res) if res else None
            except Exception:
                vec = None
        rec = MemoryRecord(
            id=new_id("mem"), kind="fact", drawer="audit",
            keywords=["audit"], descriptor=text[:200], value=detail,
            embedding=vec, source="gateway", run_id="audit",
            principal={"id": "gateway", "role": "gateway"},  # type: ignore[dict-item]
        )
        svc._persist_embedded(rec)

    # ── reads ──────────────────────────────────────────────────────────────

    async def _query_vector(self, query: str) -> list[float] | None:
        if self._embed is None:
            return None
        # The recall HTTP route awaits this, so the embedder's own 60s socket
        # timeout was the user-visible timeout: 1,164 of 15,917 recorded
        # searches passed 20s, the worst at 78.5s, and the median sat at 1.4s
        # because nomic-embed-text is fast when Ollama has it resident and
        # takes 30-40s when it does not. Keyword recall answers in milliseconds
        # without a vector, so a search that cannot embed in time now degrades
        # to it instead of stalling the caller.
        try:
            res = await asyncio.wait_for(
                self._embed(query, "retrieval_query"), QUERY_EMBED_BUDGET_S)
        except asyncio.TimeoutError:
            print(f"[memory] query embed exceeded {QUERY_EMBED_BUDGET_S}s; "
                  "keyword-only recall")
            return None
        except Exception as e:
            print(f"[memory] query embed failed ({e!r}); keyword-only recall")
            return None
        if not res:
            return None
        if hasattr(res, "get") and hasattr(res, "keys"):
            return list(res.get("embedding") or []) or None
        return list(res)

    def _sources(self, drawers: list[str] | None
                 ) -> tuple[list[MemoryService], bool]:
        """Resolve the drawer list to services. Returns (services,
        include_legacy). Unknown names raise ValueError (400, not silent)."""
        if drawers is None:
            return [self.drawers[d] for d in RECALL_ORDER], True
        svcs: list[MemoryService] = []
        include_legacy = False
        for d in drawers:
            if d == "legacy":
                include_legacy = True
            elif d in self.drawers:
                svcs.append(self.drawers[d])
            else:
                raise ValueError(
                    f"unknown drawer {d!r} (one of: {ALL_DRAWERS + ['legacy']})")
        return svcs, include_legacy

    async def search(self, query: str, history: list[dict] | None = None, *,
                     kinds: list[str] | None = None, top_k: int = 8,
                     session_id: str | None = None,
                     drawers: list[str] | None = None,
                     include_stale: bool = False,
                     doc_ids: set[str] | None = None,
                     ) -> list[MemoryRecord]:
        """Merged recall: one query embed fanned out to every selected
        drawer + legacy. Drawer hits rank before legacy hits (governed
        records outrank ungoverned ones); within a tier, vector order
        rules; ids dedupe. Stale document versions hide unless
        ``include_stale``; document hits carry neighbor previews.

        ``doc_ids`` restricts which uploaded documents may answer. It is
        applied to the MERGED result, after the vector search: filtering the
        candidate set first would take the disabled vectors out of the query
        itself and would degrade recall for the enabled documents too.
        ``None`` means "no document filtering"; an empty set means "no
        documents at all", which is how a disabled document (or a
        conversation with documents turned off) stops being visible."""
        if not (query or "").strip():
            return []
        # A request for zero results returns zero results. This used to read
        # `max(1, min(int(top_k or 8), 100))`, so an explicit `top_k: 0` was
        # coerced through `0 or 8` to 8 and then floored to 1 - the caller's
        # "give me nothing" came back with hits. The API layer already passes
        # 0 through; the plane must honour it rather than re-coerce.
        try:
            want = int(top_k if top_k is not None else 8)
        except (TypeError, ValueError):
            want = 8
        if want <= 0:
            return []
        # Bound the fan-out: absurd top_k values would otherwise multiply
        # into per-drawer fetches (fetch = 2×top_k each). The panel caps at
        # 100; anything above is clamped, never trusted.
        top_k = min(want, 100)
        svcs, include_legacy = self._sources(drawers)
        # Phase 2 routing: never touch drawers that were never written (no
        # store file and no vectors) — no JSON parse, no FAISS call. The
        # query embed still runs once: an unindexed drawer that later gains
        # items is picked up on the next search with zero config.
        svcs = [s for s in svcs if s.store.path.exists()]
        if include_legacy and not self.legacy.store.path.exists():
            include_legacy = False
        if not svcs and not include_legacy:
            return []  # nothing was ever written to the selected drawers
        qvec = await self._query_vector(query)
        per = max(1, top_k)
        # Fetch deeper than the final cut, always. The per-document diversity
        # pass below can only choose among what the drawer returned, so a
        # shallow fetch of exactly top_k items (often all from one document)
        # leaves nothing to diversify into. A doc_ids scope needs a much
        # deeper fetch again, because the allowlist is applied AFTER this
        # fetch and a genuine match can rank far below the cut.
        floor = _DOC_FETCH_FLOOR
        if doc_ids is not None:
            floor = max(floor, _DOC_SCOPED_FETCH)
        fetch_per = max(per, floor)
        merged: list[MemoryRecord] = []
        for svc in svcs:
            merged.extend(await svc.read(
                query, history, kinds=kinds, top_k=fetch_per,
                session_id=session_id, qvec=qvec))
        legacy_hits: list[MemoryRecord] = []
        if include_legacy:
            legacy_hits = await self.legacy.read(
                query, history, kinds=kinds, top_k=fetch_per,
                session_id=session_id, qvec=qvec)
        seen: set[str] = set()
        picked: list[MemoryRecord] = []
        # Per-document diversity: at most this many chunks from one document
        # in a result set. Without it a single long document's many matching
        # chunks fill every slot and genuinely-matching chunks from other
        # documents never surface - "radiation" returned 10 hits from 2 docs.
        # Backfilled below, so a corpus with fewer matching documents still
        # returns a full top_k.
        per_doc: dict[str, int] = {}
        versions = self._doc_versions()

        def _doc_id(hit: MemoryRecord) -> str | None:
            return hit.doc.doc_id if hit.doc else None

        def _admit(hit: MemoryRecord, respect_cap: bool) -> bool:
            d = _doc_id(hit)
            if not respect_cap:
                return True  # backfill pass: no cap, and it must not count
            if d is not None and per_doc.get(d, 0) >= _DOC_CHUNK_CAP:
                return False
            if d is not None:
                per_doc[d] = per_doc.get(d, 0) + 1
            return True

        # Diverse pass: take at most _DOC_CHUNK_CAP per document, and only as
        # many as the cap can justify (cap x distinct docs). Without this
        # inner limit, a top_k larger than the corpus lets the capped pass
        # fill every slot from one document and the backfill adds nothing.
        distinct = {d for d in (_doc_id(h) for h in merged + legacy_hits)
                    if d is not None}
        diverse_limit = min(top_k, _DOC_CHUNK_CAP * max(1, len(distinct)))
        for pass_respect_cap in (True, False):
            limit = diverse_limit if pass_respect_cap else top_k
            for hit in merged + legacy_hits:
                if len(picked) >= limit:
                    break
                if hit.id in seen:
                    continue
                if (not include_stale and hit.drawer == "document" and hit.doc
                        and hit.doc.doc_id
                        and versions.get(hit.doc.doc_id) != hit.doc.version):
                    continue  # stale document version: review only
                # Document allowlist. Applied here, AFTER the vector search, so
                # a disabled document is excluded without harming recall for
                # the others. `None` disables the filter entirely; an empty set
                # means no documents at all.
                if doc_ids is not None and hit.doc is not None \
                        and hit.doc.doc_id:
                    if hit.doc.doc_id not in doc_ids:
                        continue
                if not _admit(hit, pass_respect_cap):
                    continue
                seen.add(hit.id)
                picked.append(hit)
            if len(picked) >= top_k:
                break
        if not any(h.drawer == "document" for h in picked):
            return picked
        # Neighbour previews used to re-load the document store once per hit —
        # eight hits is eight reads of an 86 MB drawer, and during an indexing
        # run each one also re-parses it, because a save invalidates the cache.
        # One read now serves the whole result.
        doc_records = self.drawers["document"].store.load()
        return [self._with_neighbors(h, doc_records)
                if h.drawer == "document" else h for h in picked]

    def _with_neighbors(self, hit: MemoryRecord,
                        records: list[MemoryRecord] | None = None
                        ) -> MemoryRecord:
        """Attach adjacent-chunk previews (same doc + version, index±1) to
        a document hit — downstream synthesis gets context without another
        round trip. Returns a COPY; stored records are never mutated.

        `records` is the drawer, read once by the caller. Left to read it
        itself it did so once per hit, and each read of an 86 MB store is a
        re-parse whenever an indexing write has invalidated the cache."""
        if not hit.doc or not hit.doc.doc_id:
            return hit
        spans = [r for r in (records
                             if records is not None
                             else self.drawers["document"].store.load())
                 if r.doc and r.doc.doc_id == hit.doc.doc_id
                 and r.doc.version == hit.doc.version
                 and abs(r.doc.chunk_index - hit.doc.chunk_index) == 1]
        spans.sort(key=lambda r: r.doc.chunk_index if r.doc else 0)
        previews = [{
            "chunk_index": (s.doc.chunk_index if s.doc else 0),
            "preview": str((s.value or {}).get("chunk", ""))[:500],
        } for s in spans[:2]]
        if not previews:
            return hit
        value = dict(hit.value or {})
        value["neighbor_previews"] = previews
        return hit.model_copy(update={"value": value})

    def list_recent(self, limit: int = 50,
                    session_id: str | None = None,
                    drawers: list[str] | None = None,
                    kinds: list[str] | None = None,
                    hide_superseded: bool = False,
                    offset: int = 0,
                    ) -> list[MemoryRecord]:
        offset = max(0, int(offset))
        limit = max(0, int(limit))
        if limit == 0:
            return []
        want: set[str] | None = None
        if kinds:
            want = {k.strip() for k in kinds if k and k.strip()}
            bad = want - set(MemoryKind.__args__)
            if bad:
                raise ValueError(
                    f"unknown kind(s) {sorted(bad)} (one of: "
                    f"{list(MemoryKind.__args__)})")
        # Each source is asked for `offset + limit` rows AFTER its own
        # filters, then the merged newest-first pool is sliced. Filtering
        # after the slice was a real bug: once the fact drawer held 393
        # superseded rows newer than 7 live ones, `hide_superseded=True,
        # limit=20` saw only corrections and reported an empty fact list
        # while stats said 7 live. Asking each drawer only for `limit` rows
        # would also make page 2 skip records that belong on it.
        take = offset + limit
        svcs, include_legacy = self._sources(drawers)
        pool: list[MemoryRecord] = []
        if include_legacy:
            pool.extend(self.legacy.list_recent(
                limit=take, session_id=session_id, kinds=want,
                hide_superseded=hide_superseded))
        for svc in svcs:
            pool.extend(svc.list_recent(
                limit=take, session_id=session_id, kinds=want,
                hide_superseded=hide_superseded))
        if want:
            pool = [r for r in pool if r.kind in want]
        if hide_superseded:
            pool = [r for r in pool if r.superseded_by is None]
        pool.sort(key=lambda r: r.created_at, reverse=True)
        return pool[offset:take]

    def active_policies(self, limit: int = 50) -> list[MemoryRecord]:
        """Policies that actually constrain behavior right now: the policy
        drawer minus superseded (revoked) records. Injected into EVERY
        skill prompt with no exclusions."""
        return self.list_recent(limit=limit, drawers=["policy"],
                                hide_superseded=True)

    # ── Phase 4: episodes + playbook proposals ─────────────────────────────

    def episodes(self, session_id: str | None = None,
                 limit: int = 50) -> list[MemoryRecord]:
        """Run history without vector work: newest-first episode records,
        optionally one session's. This is the replay/history path — linkage
        to conversation turns rides on run_id/session_id, which record
        paths already stamp (flow passes sid as both)."""
        items = self.drawers["episode"].store.load()
        if session_id is not None:
            items = [i for i in items if i.session_id == session_id]
        items.sort(key=lambda r: r.created_at, reverse=True)
        return items[:max(0, limit)]

    def _resolve(self, record_id: str) -> MemoryRecord:
        """Find a record by id across drawers + legacy (evidence links,
        review tools). Raises LookupError on ghosts."""
        for svc in list(self.drawers.values()) + [self.legacy]:
            for rec in svc.store.load():
                if rec.id == record_id:
                    return rec
        raise LookupError(f"record {record_id!r} not found in any drawer")

    async def propose_playbook(self, *, descriptor: str,
                               procedure: dict | None = None,
                               evidence_ids: list[str] | None = None,
                               source: str, run_id: str,
                               principal_role: str = "agent",
                               principal_id: str = "assistant",
                               expires_days: int = 7) -> MemoryRecord:
        """Phase A consolidation: propose a reusable procedure. Proposals
        are working-drawer records (TTL'd, agent-writable) — the promotion
        to playbook needs a system/operator approval (no autonomous
        self-promotion, ever). Evidence ids must resolve."""
        evidence_ids = evidence_ids or []
        for eid in evidence_ids:
            self._resolve(eid)  # LookupError on ghosts, before any write
        await self._regulated("working", principal_role)
        _check_payload_size({"procedure": procedure or {},
                             "evidence_ids": evidence_ids})
        expires_at = datetime.utcnow() + timedelta(days=expires_days)
        return await self.drawers["working"].remember(
            kind="fact", descriptor=descriptor,
            keywords=["playbook-proposal"],
            value={"proposal": True, "procedure": procedure or {},
                   "evidence_ids": evidence_ids},
            source=source, run_id=run_id, drawer="working",
            principal_role=principal_role,  # type: ignore[arg-type]
            principal_id=principal_id, expires_at=expires_at)

    async def approve_playbook(self, *, proposal_id: str, run_id: str,
                               principal_role: str = "agent",
                               principal_id: str = "assistant",
                               ) -> MemoryRecord:
        """Promote a proposal to a playbook record. Only system/operator
        (fail-closed + audited denial otherwise). The proposal is marked
        consumed (superseded link: drops from recall, stays for review)."""
        await self._regulated("playbook", principal_role)
        proposal = self._resolve(proposal_id)
        if proposal.drawer != "working" or not (
                proposal.value or {}).get("proposal"):
            raise ValueError(
                f"{proposal_id!r} is not an open playbook proposal")
        if proposal.superseded_by is not None:
            raise ValueError(f"{proposal_id!r} was already consumed by "
                             f"{proposal.superseded_by}")
        value = proposal.value or {}
        item = await self.drawers["playbook"].remember(
            kind="fact", descriptor=proposal.descriptor,
            keywords=list(proposal.keywords),
            value={"procedure": value.get("procedure", {}),
                   "evidence_ids": value.get("evidence_ids", []),
                   "promoted_from": proposal_id},
            source=proposal.source, run_id=run_id, drawer="playbook",
            principal_role=principal_role,  # type: ignore[arg-type]
            principal_id=principal_id)
        await self._mark_superseded(proposal_id, item.id, "playbook")
        await self._audit(f"playbook promoted: {proposal_id} → {item.id}",
                          {"proposal_id": proposal_id,
                           "playbook_id": item.id,
                           "approver": principal_id})
        return item

    # ── maintenance ────────────────────────────────────────────────────────

    async def clear(self, session_id: str | None = None,
                    drawers: list[str] | None = None,
                    older_than: datetime | None = None) -> dict:
        """Wipe queryable memory: legacy + selected drawers except audit
        (the audit trail survives wipes by design; it trims only by
        retention). ``drawers`` narrows the wipe (run-end working purge
        passes ``["working"]``); ``older_than`` keeps records created
        after the cutoff. The wipe itself is audit-recorded."""
        svcs, include_legacy = self._sources(drawers) if drawers else (
            [s for n, s in self.drawers.items() if n != "audit"], True)
        counts: dict[str, int] = {}
        if include_legacy:
            counts["legacy"] = self.legacy.clear(session_id=session_id,
                                                 older_than=older_than)
        for name, svc in self.drawers.items():
            if name == "audit":
                continue
            if svc in svcs:
                counts[name] = svc.clear(session_id=session_id,
                                         older_than=older_than)
        try:
            await self._audit(f"memory cleared (session={session_id})",
                              {"cleared": counts, "session_id": session_id})
        except Exception as e:  # pragma: no cover - disk failure
            print(f"[memory] clear audit failed ({e!r})")
        counts["audit_kept"] = len(self.drawers["audit"].store.load())
        return counts

    def sweep(self) -> dict[str, int]:
        """Delete expired records in every drawer (working TTL). Returns
        per-drawer removed counts. Runs at startup, opportunistically on
        writes, and via POST /v1/memory/sweep for scheduled maintenance."""
        return {name: svc.sweep_expired()
                for name, svc in self.drawers.items()}

    def stats(self) -> dict:
        return {
            "legacy": self.legacy.stats(),
            "drawers": {name: svc.stats()
                        for name, svc in self.drawers.items()},
        }
