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
    "policy", "fact", "playbook", "document", "episode", "working",
]
ALL_DRAWERS: list[DrawerName] = [
    "working", "episode", "fact", "playbook", "policy", "audit", "document",
]

# Phase 2: the session-start drawer set. Working notes are run-scoped by
# definition (a new run has no notes yet), so session recall is everything
# else. The agent passes this list explicitly — the contract is pinned
# here, not inferred per query.
SESSION_DRAWERS: list[str] = [
    "policy", "fact", "playbook", "document", "episode", "legacy",
]

# Working notes disappear: default TTL applied server-side when the write
# carries no explicit expires_at (Phase 2; sweeps enforce it).
WORKING_TTL_MINUTES = 60


# Maximum JSON-encoded structured payload per write. Descriptors are
# already capped at 200 chars; this bounds `value`/`arguments` so one
# runaway writer can't bloat the store files (413, not silent truncation).
MAX_PAYLOAD_BYTES = 100_000


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
                       doc: DocSpan | None = None) -> MemoryRecord:
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
        item = await svc.remember(
            kind=kind, descriptor=descriptor, keywords=keywords,
            value=value, source=source, run_id=run_id, goal_id=goal_id,
            session_id=session_id, drawer=routed,
            principal_role=principal_role,  # type: ignore[arg-type]
            principal_id=principal_id, scope=scope, sources=sources,
            expires_at=expires_at, supersedes=supersedes, doc=doc)
        if supersedes:
            await self._mark_superseded(supersedes, item.id, routed)
        if routed == "document" and doc and doc.doc_id:
            self._note_doc_version(doc.doc_id, doc.version)
        return item

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
        try:
            res = await self._embed(query, "retrieval_query")
            if not res:
                return None
            if hasattr(res, "get") and hasattr(res, "keys"):
                return list(res.get("embedding") or []) or None
            return list(res)
        except Exception as e:
            print(f"[memory] query embed failed ({e!r}); keyword-only recall")
            return None

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
        merged: list[MemoryRecord] = []
        for svc in svcs:
            merged.extend(await svc.read(
                query, history, kinds=kinds, top_k=per,
                session_id=session_id, qvec=qvec))
        legacy_hits: list[MemoryRecord] = []
        if include_legacy:
            legacy_hits = await self.legacy.read(
                query, history, kinds=kinds, top_k=per,
                session_id=session_id, qvec=qvec)
        seen: set[str] = set()
        out: list[MemoryRecord] = []
        versions = self._doc_versions()
        for hit in merged + legacy_hits:
            if hit.id in seen:
                continue
            seen.add(hit.id)
            if (not include_stale and hit.drawer == "document" and hit.doc
                    and hit.doc.doc_id
                    and versions.get(hit.doc.doc_id) != hit.doc.version):
                continue  # stale document version: review only
            # Document allowlist. Applied here, AFTER the vector search, so a
            # disabled document is excluded without harming recall for the
            # others. `None` disables the filter entirely; an empty set means
            # no documents at all.
            if doc_ids is not None and hit.doc is not None \
                    and hit.doc.doc_id:
                if hit.doc.doc_id not in doc_ids:
                    continue
            out.append(self._with_neighbors(hit) if hit.drawer == "document"
                       else hit)
            if len(out) >= top_k:
                break
        return out

    def _with_neighbors(self, hit: MemoryRecord) -> MemoryRecord:
        """Attach adjacent-chunk previews (same doc + version, index±1) to
        a document hit — downstream synthesis gets context without another
        round trip. Returns a COPY; stored records are never mutated."""
        if not hit.doc or not hit.doc.doc_id:
            return hit
        spans = [r for r in self.drawers["document"].store.load()
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
                    ) -> list[MemoryRecord]:
        svcs, include_legacy = self._sources(drawers)
        pool: list[MemoryRecord] = []
        if include_legacy:
            pool.extend(self.legacy.list_recent(limit=limit,
                                                session_id=session_id))
        for svc in svcs:
            pool.extend(svc.list_recent(limit=limit, session_id=session_id))
        if kinds:
            want = {k.strip() for k in kinds if k and k.strip()}
            bad = want - set(MemoryKind.__args__)
            if bad:
                raise ValueError(
                    f"unknown kind(s) {sorted(bad)} (one of: "
                    f"{list(MemoryKind.__args__)})")
            pool = [r for r in pool if r.kind in want]
        if hide_superseded:
            pool = [r for r in pool if r.superseded_by is None]
        pool.sort(key=lambda r: r.created_at, reverse=True)
        return pool[:max(0, limit)]

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
