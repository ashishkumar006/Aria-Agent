"""Memory service routes (durable records across seven drawers + legacy).

Mounted from main.py as a single APIRouter. The agent is a thin client
over these endpoints — no memory state lives outside the gateway's
``state/`` dir, and no credentials are involved (embeddings run through
the gateway's own failover ring).

Phase 1: the cabinet (``memory/plane.py``) serves legacy items (frozen,
read-mostly) plus seven drawers for new writes. Paths are unchanged from
the flat era; payloads gain ``principal_role`` (fail-closed writer map),
``scope``/``sources``/``expires_at``/``doc``, and search gains an
optional ``drawers`` scope (``legacy`` is a valid entry).

State dir: ``state/`` next to this file, overridable via
``GATEWAY_MEMORY_STATE`` (tests + ops).
"""
from __future__ import annotations

import os
import re
import time
import json
import threading
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

import db
import embedders as E
from documents import parsers
from documents.registry import (
    # Every status constant, not just the ones the happy path happens to touch.
    # `PENDING` (reindex) and `FAILED` (the error path around line 231) were
    # used but never imported, so those two routes raised NameError instead of
    # doing their job - and an error handler that raises NameError reports
    # "Internal Server Error" with no cause.
    BLOCKED, CHUNKING, EMBEDDING, FAILED, PARSING, PENDING, READY,
    INDEX_FORMAT_VERSION,
    EmbedderUnavailable, Indexer, Registry, Document,
)
from memory.models import (
    DocSpan,
    DrawerName,
    MemoryRecord,
    MemoryScope,
    PayloadTooLarge,
    Principal,
    Role,
    SourceRef,
)
from memory.plane import MemoryPlane
from memory.service import EmbedFn

router = APIRouter()

ROOT = Path(__file__).resolve().parent


def _state_dir() -> Path:
    override = os.getenv("GATEWAY_MEMORY_STATE")
    return Path(override) if override else ROOT / "state"


# One cached plane per state dir. The gateway is the sole writer and the
# FAISS index objects are mutated in place on writes, so cached handles
# stay fresh.
_PLANES: dict[str, MemoryPlane] = {}


def _reset_cache() -> None:
    """Test seam: drop cached planes so tmp state dirs never leak."""
    _PLANES.clear()


def _gateway_embed_fn(embedders: list) -> EmbedFn:
    async def _embed(text: str, task_type: str) -> dict | None:
        """Return {"embedding", "model"} so the record keeps provenance.

        Returning a bare list here is what let vectors land in the store with
        no record of which model produced them.
        """
        try:
            _name, result, _attempts, _latency = await E.embed_with_failover(
                embedders, text, task_type  # type: ignore[arg-type]
            )
            return {"embedding": list(result["embedding"]),
                    "model": result.get("model", ""),
                    "dim": result.get("dim", 0)}
        except Exception as e:
            print(f"[memory] embed ring failed ({e!r}); continuing without vector")
            return None

    return _embed


def _plane(request: Request) -> MemoryPlane:
    key = str(_state_dir())
    plane = _PLANES.get(key)
    if plane is None:
        plane = MemoryPlane(
            _state_dir(),
            embed_fn=_gateway_embed_fn(request.app.state.embedders),
        )
        _PLANES[key] = plane
    return plane


MAX_VALUE_DEPTH = 24


# ── /v1/documents ───────────────────────────────────────────────────────────
# Upload, inspect, enable/disable and delete documents. Indexing runs in a
# background worker so an upload never blocks, and so a document that is
# waiting on Ollama can sit in `blocked` and resume without a request.
_doc_router = router
_registry: Registry | None = None
_registry_lock = threading.Lock()


def _docs() -> Registry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = Registry()
    return _registry


# The event loop that owns the embedders' async resources (the shared httpx
# client, the provider locks). The indexing worker runs on a plain thread, so it
# must submit coroutines back to THIS loop rather than calling `asyncio.run`,
# which built a second loop in the worker thread and left the two fighting over
# the same resources - the gateway's event loop starved and `/health` stopped
# answering while the listener stayed open, so a port check reported a dead
# service as healthy.
_LOOP: "asyncio.AbstractEventLoop | None" = None


_DOC_EMBED_TIMEOUT_S = 120.0
# Largest accepted upload. A 50 MB file was accepted, then took the whole
# gateway down for 10+ minutes while /health stayed green.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
# Search responses ship a bounded preview of each hit's chunk text,
# not the full chunk (which the chunker caps at 8000 chars). 50 hits
# × 1500 chars keeps the worst-case payload in the low hundreds of KB
# instead of the multi-MB responses an unbounded field produced.
_SEARCH_CHUNK_PREVIEW_CHARS = 1500



def set_loop(loop: "asyncio.AbstractEventLoop | None") -> None:
    """Called from the app lifespan. Tests may leave it unset."""
    global _LOOP
    _LOOP = loop


def _doc_embedder(app_state_embedders) -> "Callable[[list[str]], dict]":
    """Bridge the gateway's batch endpoint for the indexing worker."""
    def _embed(texts: list[str]) -> dict:
        async def _go():
            return await E.embed_batch_with_failover(
                app_state_embedders, texts, "retrieval_document")
        loop = _LOOP
        try:
            if loop is not None and loop.is_running():
                # Hand the coroutine to the loop that owns the embedders and
                # block this worker thread on the result. Bounded so a wedged
                # provider surfaces as an error rather than hanging forever.
                fut = asyncio.run_coroutine_threadsafe(_go(), loop)
                return fut.result(timeout=_DOC_EMBED_TIMEOUT_S)
            return asyncio.run(_go())
        except E.EmbedderError as e:
            raise EmbedderUnavailable(str(e)) from e
        except Exception as e:                       # noqa: BLE001
            raise EmbedderUnavailable(str(e)) from e
    return _embed


def _doc_chunks(reg: Registry, doc: Document) -> list:
    """Re-parse the stored source to get chunk texts, matching the order the
    indexer embedded them. Chunking is deterministic, so index N here is the
    same chunk that was embedded at index N.

    PURE CPU and potentially slow - a 120 KB document spends tens of seconds in
    `split_sentences`. It used to run inline on the event loop from
    `documents_detail`, which wedged the entire gateway for that long: every
    route including the pure `/health` timed out, while the TCP listener stayed
    open, so a port check reported a dead service as healthy. Confirmed with a
    live stack dump:

        documents_detail (memory_api.py)
          _doc_chunks -> chunk_blocks -> _chunk_section -> split_sentences

    Memoised per (doc_id, source size) because the detail route and the indexer
    ask for the same chunk list repeatedly while a document is indexing.
    """
    key = (doc.id, _SOURCE_TOKEN(doc))
    hit = _CHUNK_CACHE.get(key)
    if hit is not None:
        return hit
    data = reg.read_source(doc.id)
    if data is None:
        _CHUNK_CACHE[key] = []
        return []
    from documents.chunker import chunk_blocks
    parsed = parsers.parse(doc.filename, data)
    chunks = chunk_blocks(parsed.blocks)
    if len(_CHUNK_CACHE) >= _CHUNK_CACHE_MAX:
        _CHUNK_CACHE.clear()
    _CHUNK_CACHE[key] = chunks
    return chunks


def _SOURCE_TOKEN(doc: Document) -> int:
    """Cheap identity for a document's source bytes."""
    try:
        return len(doc.filename) + int(getattr(doc, "size_bytes", 0) or 0)
    except Exception:                               # noqa: BLE001
        return 0


_CHUNK_CACHE: dict[tuple, list] = {}
_CHUNK_CACHE_MAX = 8


class _PlaneIndexer(Indexer):
    """Writes document chunks into the gateway's `document` drawer, with
    provenance recorded so their vectors are never compared against a
    different model's."""

    def __init__(self, registry, plane, embed, generation=0, generations=None):
        super().__init__(registry, embed=embed)
        self.plane = plane
        self._cache: dict[str, list] = {}
        self.generation = generation
        self.generations = generations if generations is not None else {}

    def _check_generation(self, doc_id: str) -> None:
        """Abandon the batch loop if this worker has been superseded.

        Without this a reindex during an in-flight index left the document
        wedged: the worker finished its current batch, recomputed an empty work
        list against the `chunk_count` the reindex had just zeroed, and returned
        without ever setting a terminal status. The document stayed at
        `embedding` with progress 0.0 and could not be enabled, because
        `ENABLEABLE` is `{READY}`."""
        if self.generations.get(doc_id, self.generation) != self.generation:
            raise _IndexSuperseded(doc_id)

    def _chunk_texts(self, doc_id, indices):
        if doc_id not in self._cache:
            doc = self.registry.get(doc_id)
            self._cache[doc_id] = _doc_chunks(self.registry, doc)
        chunks = self._cache[doc_id]
        return [chunks[i].text for i in indices if i < len(chunks)]

    def _store_chunks(self, doc_id, indices, vectors, result):
        self._check_generation(doc_id)
        # A document deleted mid-index used to have its chunk records
        # re-persisted by the in-flight worker, leaving orphans that retrieval
        # kept serving (with `filename: ""`) after the delete had already
        # returned 200. Checked BEFORE `_doc_chunks`, which dereferences the
        # document and would raise on the now-missing row.
        if self.registry.get(doc_id) is None:
            return
        if doc_id not in self._cache:
            self._cache[doc_id] = _doc_chunks(self.registry,
                                              self.registry.get(doc_id))
        chunks = self._cache[doc_id]
        model = result.get("embed_model") or result.get("model") or ""
        svc = self.plane.drawers["document"]
        # Register this document's current version. Search() hides any chunk
        # whose version is not the registered current one, and that note is
        # written by plane.remember() - which this writer bypasses. Without it
        # every chunk looked stale and recall silently returned nothing.
        self.plane._note_doc_version(doc_id, "1")
        # Overwrite on re-index rather than append, so a resumed run that
        # re-does one batch does not duplicate those chunks.
        existing = {r.value.get("chunk_index"): r
                    for r in svc.store.load()
                    if (r.doc and r.doc.doc_id == doc_id)}
        # Overwrite on re-index rather than append, so a resumed run that
        # re-does one batch does not duplicate those chunks. The window's
        # prior records go in ONE pass: `delete_one` rebuilds the whole
        # vector index per record, so a 40-chunk batch forced 40 full
        # rebuilds, and a 1370-chunk document spent most of its index
        # time rebuilding instead of embedding.
        doomed = [existing[idx].id for idx in indices if idx in existing]
        if doomed:
            svc.delete_where(
                lambda i, _d=set(doomed): i.id in _d)
        self.registry.set_status(doc_id, EMBEDDING)
        stored = skipped_none = skipped_bounds = 0
        for idx, vec in zip(indices, vectors):
            if vec is None:
                skipped_none += 1
                continue
            if idx >= len(chunks):
                # `run`'s `ok` list marks these embedded (it has no
                # bounds check) while this loop skips them - the one
                # path that can leave the registry claiming chunks the
                # drawer never received. Count them loudly.
                skipped_bounds += 1
                continue
            c = chunks[idx]
            rec = MemoryRecord(
                id=f"mem:doc-{doc_id}-{idx}",
                kind="fact",
                drawer="document",
                descriptor=c.text[:200],
                value={"chunk": c.text, "doc_id": doc_id,
                       "chunk_index": idx, "heading_path": list(c.heading_path),
                       "page": c.page},
                doc={"doc_id": doc_id, "version": "1", "chunk_index": idx,
                     "total_chunks": len(chunks)},
                source=f"document:{doc_id}",
                run_id=f"index-{doc_id}",
                embedding=list(vec),
                embed_model=model or None,
                embed_dim=len(vec) if vec else None,
                principal=Principal(id="indexer", role="indexer"),
            )
            svc._persist_embedded(rec, persist=False)
            stored += 1
        if stored:
            # One index-file write for the whole batch instead
            # of one per chunk.
            svc.persist_index()
        if skipped_none or skipped_bounds or stored != len(indices):
            print(f"[index] {doc_id} batch {indices[0]}-{indices[-1]}: "
                  f"stored={stored} skipped_none={skipped_none} "
                  f"skipped_bounds={skipped_bounds} "
                  f"chunks={len(chunks)} vectors={len(vectors)}",
                  flush=True)


_doc_jobs: dict[str, threading.Thread] = {}
# Per-document generation counter. A worker whose generation is stale has been
# superseded by a reindex and must abandon its work - see `_start_index`.
_doc_generation: dict[str, int] = {}
# One document indexes at a time. `_doc_jobs` is keyed per document, so the
# "one document at a time" promise was never enforced: N concurrent uploads
# launched N indexers that all drove the shared embedder at once, every batch
# slowed and gateway latency spiked. The lock is held inside the worker
# thread, so request threads never block on it.
_DOC_SERIAL_LOCK = threading.Lock()
# How long a reindex waits for the in-flight worker to notice it was superseded.
# Long enough for one embed batch (a batch yields between calls), short enough
# that the request does not hang.
_REINDEX_JOIN_S = 30.0


class _IndexSuperseded(Exception):
    """Raised inside a worker whose reindex has replaced it."""


def _start_index(request: Request, doc_id: str) -> None:
    """Run indexing in the background, one document at a time."""
    reg = _docs()
    # Retire any in-flight job for this document FIRST and wait for it to
    # notice. It used to be told nothing and simply returned, which left the
    # reset state below with no worker to advance it: the document sat at
    # `embedding` with chunk_count 0 and progress 0.0 permanently, and since
    # `ENABLEABLE` is {READY} it could not be enabled or cleanly re-driven. A
    # 500-chunk document takes tens of seconds to index, so this window is wide.
    old = _doc_jobs.get(doc_id)
    if old is not None and old.is_alive():
        _doc_generation[doc_id] = _doc_generation.get(doc_id, 0) + 1
        old.join(timeout=_REINDEX_JOIN_S)
    plane = _plane(request)

    # A generation counter the worker checks between batches, so a worker that
    # outlives the join above abandons its stale work rather than writing
    # against a `chunk_count` that no longer exists.
    _doc_generation[doc_id] = _doc_generation.get(doc_id, 0) + 1
    gen = _doc_generation[doc_id]

    def _work():
        # Queue behind any in-flight document indexer. The wait is
        # polled (not a bare acquire) so a worker superseded while
        # queued exits instead of running a full parse it will then
        # abandon on its first batch.
        while True:
            if _doc_generation.get(doc_id) != gen:
                return
            if _DOC_SERIAL_LOCK.acquire(timeout=2.0):
                break
        try:
            _PlaneIndexer(reg, plane,
                          _doc_embedder(request.app.state.embedders),
                          generation=gen, generations=_doc_generation,
                          ).run(doc_id)
        except _IndexSuperseded:
            # A reindex superseded this run. The replacement owns the status
            # now; do not stamp a terminal state over it.
            return
        except Exception as e:                      # noqa: BLE001
            # A worker that dies here used to be silent: the document sat
            # at whatever status it had, with no trace of why. Print
            # before the (possibly suppressed) FAILED stamp.
            print(f"[index] {doc_id} worker failed: {type(e).__name__}: "
                  f"{e}", flush=True)
            if _doc_generation.get(doc_id) != gen:
                return
            reg.set_status(doc_id, FAILED, str(e)[:200])
        finally:
            _DOC_SERIAL_LOCK.release()

    t = threading.Thread(target=_work, daemon=True)
    _doc_jobs[doc_id] = t
    t.start()


# ── registry ↔ drawer reconciliation ──────────────────────────────────
# The registry's `embedded_indices`/`chunk_count` record what WAS
# embedded; the plane's document drawer holds what IS recallable.
# Anything that clears the drawer without touching the registry (an
# unscoped memory wipe, a state wipe, a manual edit) leaves documents
# marked `ready`/`enabled` with zero recallable chunks, and because
# `pending_indices()` reads the registry, a re-drive is a no-op that
# never re-stores anything: search silently returns nothing forever.
# The check below compares the drawer's live record count per document
# against the registry's `chunk_count` and, on a shortfall, resets the
# document to `pending` (embedded state zeroed) and re-drives the
# indexer, which re-parses and re-stores every chunk. `enabled` is left
# untouched so the document comes back online by itself once the
# re-index reaches `ready` (`enabled_ids()` requires READY, so it can
# never serve the half-embedded window in between).
#
# This runs on EVERY document API request, not once per process: the
# drift it heals appears mid-process too. An unscoped `DELETE
# /v1/memory?confirm=wipe` - which a test suite once sent against a
# live gateway - emptied every drawer while the registry still said
# ready/1370-embedded, and the once-per-process guard had already
# fired, so nothing noticed until a user searched. The check is one
# store load (cached on (mtime, size), so free when nothing wrote in
# between) plus an O(records) count.
# Statuses in which an indexer may legitimately be mid-flight. A
# shortfall there is work in progress, not drift.
_INDEXING_STATUSES = {PARSING, CHUNKING, EMBEDDING}


def _reconcile_registry_with_plane(request: Request) -> None:
    """Heal registry ↔ document-drawer drift. Cheap enough to run
    per request: one cached store load, one O(records) count."""
    try:
        reg = _docs()
        svc = _plane(request).drawers["document"]
        live: dict[str, int] = {}
        for r in svc.store.load():
            if r.doc and r.doc.doc_id:
                live[r.doc.doc_id] = live.get(r.doc.doc_id, 0) + 1
        jobs = _doc_jobs
        for doc in reg.list():
            if doc.index_version != INDEX_FORMAT_VERSION:
                if doc.id in jobs and jobs[doc.id].is_alive():
                    # An indexer owns this document; its run()
                    # stamps the current format version when it
                    # records the chunk count. Resetting
                    # underneath it would restart the parse on
                    # every request.
                    continue
                # Its chunks were embedded under an older
                # parser/chunker, so the stored chunk list no
                # longer matches a fresh parse. `Indexer.run`
                # resets the registry on the mismatch but knows
                # nothing about the plane, so the old records
                # would survive as orphans; clear them here,
                # where both systems are in reach.
                print(f"[documents] {doc.id} ({doc.filename}): "
                      f"indexed under parser format "
                      f"{doc.index_version}, current is "
                      f"{INDEX_FORMAT_VERSION} - re-indexing")
                try:
                    svc.delete_where(
                        lambda r, _id=doc.id: bool(
                            r.doc and r.doc.doc_id == _id))
                except Exception as e:               # noqa: BLE001
                    print(f"[documents] could not clear stale "
                          f"chunks for {doc.id}: {e!r}")
                reg.update(doc.id, chunk_count=0, embedded_indices=[],
                           embedded_count=0, status=PENDING, error="")
                _start_index(request, doc.id)
                continue
            if doc.status in _INDEXING_STATUSES:
                # Resume a document whose indexer died with its
                # process: an indexing status with no live worker
                # means a crash left it there, and nothing else
                # would ever re-drive it (the registry's
                # `pending_indices` make the re-run idempotent).
                if doc.id not in jobs or not jobs[doc.id].is_alive():
                    print(f"[documents] {doc.id} ({doc.filename}): "
                          f"status {doc.status} with no live indexer "
                          f"- resuming")
                    _start_index(request, doc.id)
                continue  # an indexer owns this document right now
            # Only a READY document can claim chunks the drawer does
            # not hold. A failed or blocked document is simply not
            # done; re-driving it here would loop (parse fails,
            # reset, parse fails...) on every request. Its healing
            # path is the manual re-index button.
            if doc.status != READY:
                continue
            have = live.get(doc.id, 0)
            if doc.chunk_count and have < doc.chunk_count:
                print(f"[documents] {doc.id} ({doc.filename}): registry "
                      f"claims {doc.chunk_count} chunks but the document "
                      f"drawer holds {have} — resetting and re-indexing")
                reg.update(doc.id, chunk_count=0, embedded_indices=[],
                           embedded_count=0, status=PENDING, error="")
                _start_index(request, doc.id)
    except Exception as e:                           # noqa: BLE001
        print(f"[documents] reconciliation failed: {e!r}")


@_doc_router.get("/v1/documents")
async def documents_list(request: Request):
    # The reconcile can retire a live indexer, and retirement
    # joins the worker thread (bounded, but up to 30s). That
    # must not run ON the event loop - it blocked every route
    # in the gateway, /health included, for the whole join.
    await asyncio.to_thread(_reconcile_registry_with_plane, request)
    return {"documents": [d.to_dict() for d in _docs().list()]}


@_doc_router.post("/v1/documents")
async def documents_upload(request: Request):
    """Accept an upload.

    Size-capped: a 50 MB upload used to be accepted, then wedged the gateway
    for 10+ minutes while `/health` stayed green - the parse ran the request
    off the loop only after the whole body was already buffered, and the
    listener eventually disappeared while the process stayed alive. The cap is
    named in the 413 body so the caller knows exactly which limit it hit.
    """
    # The reconcile can retire a live indexer, and retirement
    # joins the worker thread (bounded, but up to 30s). That
    # must not run ON the event loop - it blocked every route
    # in the gateway, /health included, for the whole join.
    await asyncio.to_thread(_reconcile_registry_with_plane, request)
    try:
        form = await request.form()
    except Exception as e:
        # A malformed multipart body (a part header past python-
        # multipart's ~4KB cap — a ~5KB FILENAME was enough)
        # raised out of request.form() as an unhandled exception:
        # a bare 500 on the upload path. It is a client error.
        return JSONResponse(status_code=400, content={
            "error": f"malformed multipart body: {type(e).__name__}"})
    up = form.get("file")
    if up is None or not hasattr(up, "filename"):
        return JSONResponse(status_code=400,
                            content={"error": "a file part is required"})
    # The client-supplied filename is persisted verbatim and echoed
    # by every listing: strip any directory component (a "../evil"
    # name was stored as-is) and control characters, and cap the
    # length so it can never blow a URL or header limit.
    name = str(getattr(up, "filename", "") or "upload.bin")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name
                   if ord(c) >= 0x20 and ord(c) != 0x7f).strip()
    name = name.lstrip(".") or "upload"
    # Cap the STEM, not the whole name: `name[:200]` cut mid-extension,
    # so a valid 201-char "report.txt" was detected as ".tx" and rejected
    # with a file-TYPE error for what is really a name-length problem.
    if len(name) > 200:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 12:
            keep = 200 - len(dot) - len(ext)
            name = (stem[:max(1, keep)] if len(stem) > keep else stem) + dot + ext
        else:
            name = name[:200]
    data = await up.read()
    if not data:
        return JSONResponse(status_code=400,
                            content={"error": "the uploaded file is empty"})
    if len(data) > MAX_UPLOAD_BYTES:
        return JSONResponse(status_code=413, content={
            "error": f"file is {len(data)} bytes; limit is {MAX_UPLOAD_BYTES} "
                     f"({MAX_UPLOAD_BYTES // (1024 * 1024)} MB). Split the "
                     f"document or raise the limit server-side."})
    try:
        kind = parsers.detect_type(name)
    except parsers.UnsupportedDocument as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    reg = _docs()
    doc = reg.add(filename=name, doc_type=kind, size_bytes=len(data))
    reg.write_source(doc.id, data)
    # Off the event loop: _start_index joins any in-flight
    # indexer for this document (bounded, but up to 30s).
    await asyncio.to_thread(_start_index, request, doc.id)
    return {"document": reg.get(doc.id).to_dict()}


@_doc_router.get("/v1/documents/{doc_id}")
async def documents_detail(doc_id: str, request: Request):
    # The reconcile can retire a live indexer, and retirement
    # joins the worker thread (bounded, but up to 30s). That
    # must not run ON the event loop - it blocked every route
    # in the gateway, /health included, for the whole join.
    await asyncio.to_thread(_reconcile_registry_with_plane, request)
    reg = _docs()
    doc = reg.get(doc_id)
    if doc is None:
        return JSONResponse(status_code=404,
                            content={"error": f"no such document {doc_id}"})
    out = doc.to_dict()
    # Chunk previews let the console show what was actually indexed, and a
    # test-search box can use the same source. Re-parsing is tens of seconds on
    # a large document, so it runs in a thread: inline on the event loop it
    # blocked every route in the gateway, `/health` included, while the listener
    # stayed open and health checks kept reporting success.
    try:
        chunks = await asyncio.to_thread(_doc_chunks, reg, doc)
    except Exception as e:                          # noqa: BLE001
        chunks = []
        out["chunk_error"] = str(e)[:200]
    embedded = set(doc.embedded_indices)
    out["chunks"] = [
        {"index": i, "kind": c.kind, "heading_path": list(c.heading_path),
         "page": c.page, "words": len(c.text.split()),
         "embedded": i in embedded,
         "preview": c.text[:400]}
        for i, c in enumerate(chunks)]
    return {"document": out}


# Upload media types the console's inline preview can hand to the
# browser's native viewer. Anything unmapped still serves, just as
# an opaque download.
_SOURCE_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "md": "text/markdown",
    "markdown": "text/markdown",
    "txt": "text/plain",
    "text": "text/plain",
    "log": "text/plain",
    "html": "text/html",
    "htm": "text/html",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
}


@_doc_router.get("/v1/documents/{doc_id}/source")
async def documents_source(doc_id: str, request: Request):
    """Serve the stored upload bytes for the console's preview.

    The registry keeps every upload as `{doc_id}.source` next to the
    registry file; this route hands those bytes back with the upload's
    original media type, so the console can render the file inline
    instead of only showing its chunks. `Range` is honoured so a viewer
    can stream a large file instead of buffering it whole; a request
    without one gets the full body.
    """
    reg = _docs()
    doc = reg.get(doc_id)
    if doc is None:
        return JSONResponse(status_code=404,
                            content={"error": f"no such document {doc_id}"})
    path = reg.source_path(doc_id)
    if path is None or not path.is_file():
        return JSONResponse(status_code=404,
                            content={"error": "source file is missing"})
    size = path.stat().st_size
    ctype = _SOURCE_CONTENT_TYPES.get(doc.doc_type, "application/octet-stream")
    rng = (request.headers.get("range") or "").strip()
    if rng.startswith("bytes="):
        m = re.fullmatch(r"(\d*)-(\d*)", rng[6:].strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                # `bytes=N-` (open-ended) or `bytes=N-M`.
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                # `bytes=-N`: the last N bytes, a suffix range.
                start = max(0, size - int(m.group(2)))
                end = size - 1
            if 0 <= start <= end < size:
                def _slice(path=path, start=start, end=end):
                    with path.open("rb") as f:
                        f.seek(start)
                        return f.read(end - start + 1)
                data = await asyncio.to_thread(_slice)
                return Response(content=data, status_code=206,
                                media_type=ctype, headers={
                                    "Content-Range": f"bytes {start}-{end}/{size}",
                                    "Accept-Ranges": "bytes",
                                })
    data = await asyncio.to_thread(path.read_bytes)
    return Response(content=data, media_type=ctype,
                    headers={"Accept-Ranges": "bytes"})


@_doc_router.post("/v1/documents/{doc_id}/enabled")
async def documents_set_enabled(doc_id: str, request: Request):
    """Enable/disable. Refuses to enable anything not fully embedded."""
    # The reconcile can retire a live indexer, and retirement
    # joins the worker thread (bounded, but up to 30s). That
    # must not run ON the event loop - it blocked every route
    # in the gateway, /health included, for the whole join.
    await asyncio.to_thread(_reconcile_registry_with_plane, request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be an object"})
    flag = body.get("enabled")
    if not isinstance(flag, bool):
        return JSONResponse(status_code=400,
                            content={"error": "enabled must be true or false"})
    doc, err = _docs().set_enabled(doc_id, flag)
    if doc is None:
        return JSONResponse(status_code=404, content={"error": err})
    if err:
        return JSONResponse(status_code=409,
                            content={"error": err, "document": doc.to_dict()})
    return {"document": doc.to_dict()}


def _retire_index(doc_id: str) -> None:
    """Stop any in-flight indexer for this document and wait for it to notice.

    Bumping the generation makes the worker's between-batch check raise
    `_IndexSuperseded`, so it abandons its stale work instead of continuing to
    write chunks against state that is being torn down. The join is bounded so
    a wedged worker cannot hold up the caller's request.
    """
    job = _doc_jobs.get(doc_id)
    _doc_generation[doc_id] = _doc_generation.get(doc_id, 0) + 1
    if job is not None and job.is_alive():
        job.join(timeout=5.0)


@_doc_router.post("/v1/documents/{doc_id}/reindex")
async def documents_reindex(doc_id: str, request: Request):
    # The reconcile can retire a live indexer, and retirement
    # joins the worker thread (bounded, but up to 30s). That
    # must not run ON the event loop - it blocked every route
    # in the gateway, /health included, for the whole join.
    await asyncio.to_thread(_reconcile_registry_with_plane, request)
    reg = _docs()
    doc = reg.get(doc_id)
    if doc is None:
        return JSONResponse(status_code=404,
                            content={"error": f"no such document {doc_id}"})
    # Retire any in-flight indexer before clearing state, for the same reason as
    # delete: a worker holding a pre-delta work list would otherwise keep writing
    # against a `chunk_count` that no longer exists.
    # Off the event loop: the join inside is bounded but real.
    await asyncio.to_thread(_retire_index, doc_id)
    # Drop chunk records so the next index writes them cleanly. One pass and one
    # index rebuild rather than one full rebuild per chunk.
    try:
        svc = _plane(request).drawers["document"]
        svc.delete_where(
            lambda r: bool(r.doc and r.doc.doc_id == doc_id))
    except Exception:                               # noqa: BLE001
        pass
    reg.update(doc_id, chunk_count=0, embedded_indices=[],
               embedded_count=0, status=PENDING, error="")
    # Off the event loop: _start_index joins any in-flight
    # indexer for this document (bounded, but up to 30s).
    await asyncio.to_thread(_start_index, request, doc_id)
    return {"document": reg.get(doc_id).to_dict()}


@_doc_router.delete("/v1/documents/{doc_id}")
async def documents_delete(doc_id: str, request: Request):
    reg = _docs()
    if reg.get(doc_id) is None:
        return JSONResponse(status_code=404,
                            content={"error": f"no such document {doc_id}"})
    # Retire any in-flight indexer BEFORE deleting. It writes this document's
    # chunks from a work list it built earlier, so deleting underneath it left
    # orphans re-persisted after the delete had already returned 200 - retrieval
    # then kept serving a document that no longer existed.
    # Off the event loop: the join inside is bounded but real.
    await asyncio.to_thread(_retire_index, doc_id)
    try:
        svc = _plane(request).drawers["document"]
        # One pass, one index rebuild. This used to call `delete_one` per chunk
        # and each call rebuilt the entire vector index, so a 40-chunk document
        # meant 40 FAISS rebuilds while an indexer was writing - a 31s block
        # that left the gateway wedged and still passing port-based health
        # checks.
        svc.delete_where(
            lambda r: bool(r.doc and r.doc.doc_id == doc_id))
    except Exception as e:                           # noqa: BLE001
        return JSONResponse(status_code=502, content={
            "error": f"could not delete document chunks: {e}"[:200]})
    reg.delete(doc_id)
    return {"status": "ok", "id": doc_id}


def _document_hit_payload(h, reg) -> dict:
    """Shape one retrieval hit for the response, with its provenance.

    Split out of the route so the field mapping is directly testable - the
    page bug below survived because this was anonymous code inside a handler that the
    suite called only with prose documents, which have no page to report.

    Bounded: `chunk` carried the FULL chunk text (up to 8000 chars each),
    so a 50-hit response shipped megabytes - 8.68 MB observed live - for
    an endpoint whose callers only verify provenance. The console and the
    chat path (which reads the plane directly, not this payload) are
    unaffected; full text stays available per chunk via the detail route.
    """
    doc_id = h.doc.doc_id if h.doc else ""
    doc = reg.get(doc_id) if doc_id else None
    value = h.value or {}
    heading = value.get("heading_path") or []
    return {
        "id": h.id, "doc_id": doc_id,
        # Without the filename a chat answer quoting a chunk cannot say WHICH
        # document it came from, so a model given these hits either cites "a
        # document" or invents a title - both worse than not retrieving at all.
        "filename": (doc.filename if doc else "") or "",
        "chunk_index": h.doc.chunk_index if h.doc else 0,
        # Page lives in the record's `value`, not on the record itself, so
        # `hasattr(h, "page")` was always False and every hit reported
        # `page: null` - a citation with no page number on a paged source.
        "page": value.get("page", getattr(h, "page", None)),
        "heading_path": [str(x)[:120] for x in heading[:8]],
        "descriptor": (h.descriptor or "")[:200],
        "chunk": (value.get("chunk", "") or "")[:_SEARCH_CHUNK_PREVIEW_CHARS],
        "embed_model": h.embed_model,
    }


@_doc_router.post("/v1/documents/search")
async def documents_search(request: Request):
    """Retrieval-only search over ENABLED documents.

    Exists so the console can verify indexing without spending an LLM call,
    and so a user can check that disabling a document really removes it.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be an object"})
    q = str(body.get("query") or "").strip()
    if not q:
        return JSONResponse(status_code=400, content={"error": "query required"})
    # The reconcile can retire a live indexer, and retirement
    # joins the worker thread (bounded, but up to 30s). That
    # must not run ON the event loop - it blocked every route
    # in the gateway, /health included, for the whole join.
    await asyncio.to_thread(_reconcile_registry_with_plane, request)
    # `body.get("top_k") or 8` treated an explicit 0 as absent and answered a
    # request for no results with EIGHT - `top_k: 0` returned 8 hits on a live
    # 260-chunk document. The count must be monotone non-decreasing in k, and
    # k=0 must mean zero.
    raw_k = body.get("top_k")
    if raw_k is None:
        raw_k = 8
    try:
        top_k = max(0, min(int(raw_k), 50))
    except (TypeError, ValueError):
        top_k = 8
    # `doc_ids` scopes the search but must never EXPAND it past what the user
    # enabled. It used to replace the enabled set outright, so passing
    # `doc_ids=[disabled]` returned that document's full content - a caller
    # with a scope allowlist (including the agent's own conversation scope)
    # could read documents the user had switched off, while the endpoint's own
    # docstring promised "retrieval-only search over ENABLED documents".
    allowed = set(_docs().enabled_ids())
    if body.get("doc_ids") is not None:
        if not isinstance(body.get("doc_ids"), list):
            return JSONResponse(status_code=400,
                                content={"error": "doc_ids must be an array"})
        allowed &= set(body["doc_ids"])
    hits = await _plane(request).search(
        q, drawers=["document"], top_k=top_k, doc_ids=allowed)
    reg = _docs()
    # One registry read per response: Registry.get re-reads and
    # re-scans documents.json on every call, so shaping N hits
    # cost N full file reads for at most a handful of distinct
    # documents. A dict exposes the same .get(doc_id) interface
    # the payload shaper already uses.
    by_id = {d.id: d for d in reg.list()}
    return {"hits": [_document_hit_payload(h, by_id) for h in hits]}


def _value_depth(obj, limit: int = MAX_VALUE_DEPTH, _d: int = 0) -> int:
    """Nesting depth of a decoded-JSON value, bounded at `limit + 1` so a
    hostile payload cannot make the check itself expensive.

    A container counts as one level, so `{}` is depth 1 and `{"a": 1}` is 2.
    """
    _d += 1
    if _d > limit:
        return _d
    if isinstance(obj, dict):
        return max((_value_depth(v, limit, _d) for v in obj.values()),
                   default=_d)
    if isinstance(obj, (list, tuple)):
        return max((_value_depth(v, limit, _d) for v in obj),
                   default=_d)
    return _d


def _slim(rec) -> dict:
    """List/search projection without the vector.

    `embedding` is a 768-float list (~15KB of JSON per record). Retrieval
    already has it in the FAISS index and no list consumer reads it, so
    shipping it made a 50-row response ~750KB and dominated latency. Writes
    and the index still carry it; this only affects what leaves the API.
    """
    return rec.model_dump(mode="json", exclude={"embedding"})


def _log_mem(operation: str, t0: float, status: str = "ok",
             error: str | None = None, prompt_chars: int = 0,
             response_chars: int = 0) -> None:
    """Ledger-log a memory-plane operation. Memory used to bypass db entirely
    (embeds were logged, searches/writes were not) — this closes that gap so
    /v1/calls and cost views see the whole plane. Best-effort: the ledger
    must never break a memory request."""
    try:
        db.log_call(provider="memory", model=operation, status=status,
                    error=(error or "")[:300] if error else None,
                    prompt_chars=prompt_chars, response_chars=response_chars,
                    latency_ms=int((time.time() - t0) * 1000),
                    call_role="memory")
    except Exception as e:  # pragma: no cover - ledger outage
        print(f"[memory] ledger log failed ({e!r})")


# ── request models ─────────────────────────────────────────────────────────

class ScopeIn(BaseModel):
    tenant_id: str = "course"
    project_id: str = "s9"
    user_id: str = "local"
    agent_id: str = "assistant"


class SourceIn(BaseModel):
    uri: str = ""
    author: str = ""


class DocIn(BaseModel):
    doc_id: str = ""
    version: str = "1"
    chunk_index: int = 0
    total_chunks: int = 1


class SearchRequest(BaseModel):
    query: str = ""
    history: list[dict[str, Any]] | None = None
    kinds: list[str] | None = None
    top_k: int = 8
    session_id: str | None = None
    drawers: list[str] | None = None  # default: recall set + legacy
    include_stale: bool = False  # Phase 5: also match superseded-version doc chunks
    # Restrict which uploaded documents may answer. Omitted = no filtering;
    # an EMPTY list means no documents at all, which is how a disabled
    # document or a conversation with documents turned off is enforced.
    doc_ids: list[str] | None = None


class RememberRequest(BaseModel):
    kind: Literal["fact", "preference", "tool_outcome", "scratchpad"]
    descriptor: str
    keywords: list[str] = Field(default_factory=list)
    value: dict = Field(default_factory=dict)
    source: str
    run_id: str
    goal_id: str | None = None
    session_id: str | None = None
    # Explicit drawer override (default: kind→drawer route). Permission is
    # enforced on the final drawer — this is how the operator dashboard
    # writes policy records and how consolidation writes playbooks.
    drawer: DrawerName | None = None
    principal_role: Role = "agent"
    principal_id: str = "assistant"
    scope: ScopeIn | None = None
    sources: list[SourceIn] | None = None
    expires_at: datetime | None = None
    supersedes: str | None = None
    doc: DocIn | None = None


class OutcomeRequest(BaseModel):
    tool: str
    arguments: dict = Field(default_factory=dict)
    result_text: str = ""
    artifact_id: str | None = None
    run_id: str
    goal_id: str | None = None
    session_id: str | None = None


# ── routes ─────────────────────────────────────────────────────────────────

@router.post("/v1/memory/search")
async def memory_search(req: SearchRequest, request: Request):
    t0 = time.time()
    try:
        hits = await _plane(request).search(
            req.query, req.history, kinds=req.kinds, top_k=req.top_k,
            session_id=req.session_id, drawers=req.drawers,
            include_stale=req.include_stale,
            doc_ids=None if req.doc_ids is None else set(req.doc_ids))
    except ValueError as e:
        _log_mem("search", t0, status="error", error=str(e),
                 prompt_chars=len(req.query or ""))
        raise HTTPException(400, str(e))
    _log_mem("search", t0, prompt_chars=len(req.query or ""),
             response_chars=sum(len(h.descriptor or "") for h in hits))
    return {"items": [_slim(h) for h in hits]}


@router.post("/v1/memory/remember", response_model=dict)
async def memory_remember(req: RememberRequest, request: Request):
    plane = _plane(request)
    t0 = time.time()
    if not (req.descriptor or "").strip():
        # The agent route rejects an empty descriptor; the gateway
        # accepted it and stored an empty-fact record the Memory
        # panel then showed as a blank row.
        _log_mem("remember", t0, status="error",
                 error="empty descriptor", prompt_chars=0)
        raise HTTPException(400, "descriptor is required")
    if _value_depth(req.value) > MAX_VALUE_DEPTH:
        # pydantic's serialiser raises "Circular reference detected (depth
        # exceeded)" past ~100 levels. That ValueError was reported as
        # embedding-dim drift, and because the store's cache kept the
        # unserialisable record, one such write wedged every later read and
        # write in the drawer. Reject it as the input error it is.
        _log_mem("remember", t0, status="error",
                 error="value too deeply nested",
                 prompt_chars=len(req.descriptor or ""))
        raise HTTPException(400, f"value nests deeper than {MAX_VALUE_DEPTH} "
                                 "levels and cannot be stored")
    try:
        item = await plane.remember(
            kind=req.kind, descriptor=req.descriptor, keywords=req.keywords,
            value=req.value, source=req.source, run_id=req.run_id,
            goal_id=req.goal_id, session_id=req.session_id,
            drawer=req.drawer,
            principal_role=req.principal_role,
            principal_id=req.principal_id,
            scope=MemoryScope(**req.scope.model_dump()) if req.scope else None,
            sources=[SourceRef(**s.model_dump()) for s in req.sources]
            if req.sources else None,
            expires_at=req.expires_at, supersedes=req.supersedes,
            doc=DocSpan(**req.doc.model_dump()) if req.doc else None)
    except PermissionError as e:
        # Fail-closed writer map (audited plane-side before raising).
        _log_mem("remember", t0, status="error", error=str(e),
                 prompt_chars=len(req.descriptor or ""))
        raise HTTPException(403, str(e))
    except LookupError as e:
        # Supersede target missing (legacy targets are frozen by design).
        _log_mem("remember", t0, status="error", error=str(e),
                 prompt_chars=len(req.descriptor or ""))
        raise HTTPException(404, str(e))
    except PayloadTooLarge as e:
        _log_mem("remember", t0, status="error", error=str(e),
                 prompt_chars=len(req.descriptor or ""))
        raise HTTPException(413, str(e))
    except ValueError as e:
        # A ValueError here is not always embedding-dim drift. It is also what
        # pydantic's serialiser raises for a record nested too deeply
        # ("Circular reference detected (depth exceeded)"), and that was being
        # reported as dim drift -- so an operator would go reindex a store that
        # needed the bad payload rejected instead. Serialisation failures are a
        # client input problem, so they are a 400, and the message says which.
        msg = str(e)
        unserialisable = ("Circular reference" in msg
                          or "depth exceeded" in msg
                          or "not serialisable" in msg)
        _log_mem("remember", t0, status="error", error=msg,
                 prompt_chars=len(req.descriptor or ""))
        if unserialisable:
            raise HTTPException(400, f"payload cannot be stored: {msg}")
        # Embedding-dim drift: loud 503 (operator action needed), not a
        # silent unindexed write and not a 500.
        raise HTTPException(503, msg)
    _log_mem("remember", t0, prompt_chars=len(req.descriptor or ""),
             response_chars=len(item.descriptor or ""))
    return {"item": item.model_dump(mode="json")}


@router.post("/v1/memory/record_outcome")
async def memory_record_outcome(req: OutcomeRequest, request: Request):
    plane = _plane(request)
    t0 = time.time()
    try:
        item = await plane.record_outcome(
            tool=req.tool, arguments=req.arguments,
            result_text=req.result_text, artifact_id=req.artifact_id,
            run_id=req.run_id, goal_id=req.goal_id,
            session_id=req.session_id)
    except PermissionError as e:
        _log_mem("record_outcome", t0, status="error", error=str(e))
        raise HTTPException(403, str(e))
    except PayloadTooLarge as e:
        _log_mem("record_outcome", t0, status="error", error=str(e))
        raise HTTPException(413, str(e))
    except ValueError as e:
        _log_mem("record_outcome", t0, status="error", error=str(e))
        raise HTTPException(503, str(e))
    _log_mem("record_outcome", t0,
             response_chars=len(item.descriptor or ""))
    return {"item": item.model_dump(mode="json")}


@router.get("/v1/memory")
async def memory_list(request: Request, limit: int = 50,
                      session_id: str | None = None,
                      drawers: str | None = None,
                      kinds: str | None = None,
                      hide_superseded: bool = False):
    """Newest-first items (backs the agent dashboard's Memory panel).

    ``drawers`` is an optional comma-separated subset (default: recall set
    + legacy). ``kinds`` is an optional comma-separated subset of the
    agent-facing vocabulary (fact / preference / tool_outcome / scratchpad).

    ``kinds`` exists because ``drawers`` cannot express what the dashboard
    actually needs: KIND_TO_DRAWER maps `preference` onto the `fact` drawer,
    so a user preference is stored beside plain facts and there was no way to
    ask for just the preferences. Drawer = cabinet (permissions/lifetime),
    kind = vocabulary. Both filters are useful; only this one answers
    "show me what the agent learned about how the user likes things".
    ``hide_superseded`` drops corrected records (the policy injector uses
    it; the review panel does not).
    """
    plane = _plane(request)
    try:
        drawer_list = [d.strip() for d in drawers.split(",") if d.strip()] \
            if drawers else None
        kind_list = [k.strip() for k in kinds.split(",") if k.strip()] \
            if kinds else None
        items = plane.list_recent(limit=min(limit, 500),
                                  session_id=session_id,
                                  drawers=drawer_list,
                                  kinds=kind_list,
                                  hide_superseded=hide_superseded)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # `embedding` is excluded on purpose: it is a 768-float vector (~15KB of
    # JSON per record) that retrieval already has in the FAISS index and that
    # no list/search consumer reads. Serialising it made a 50-row Memory page
    # response ~750KB and dominated the endpoint's latency.
    return {"items": [_slim(i) for i in items]}


@router.delete("/v1/memory/{memory_id}")
async def memory_delete_one(request: Request, memory_id: str):
    """Delete exactly one record by id.

    Until this existed the only way to remove a memory was
    ``DELETE /v1/memory``, which wipes every drawer. That made a single
    mis-captured preference — which the agent now records automatically —
    impossible to correct without destroying unrelated memory.
    """
    t0 = time.time()
    plane = _plane(request)
    # Search every drawer: the id, not the drawer, identifies the record.
    rec, where = None, None
    for name, svc in plane.drawers.items():
        rec = svc.delete_one(memory_id)
        if rec is not None:
            where = name
            break
    if rec is None:
        rec = plane.legacy.delete_one(memory_id)
        if rec is not None:
            where = "legacy"
    if rec is None:
        _log_mem("delete_one", t0, status="not_found")
        return JSONResponse(status_code=404,
                            content={"error": f"unknown memory {memory_id}"})
    _log_mem("delete_one", t0)
    return {"status": "ok", "drawer": where, "id": rec.id,
            "kind": rec.kind, "deleted": 1}


@router.delete("/v1/memory")
async def memory_clear(request: Request, session_id: str | None = None,
                       drawers: str | None = None,
                       older_than: datetime | None = None,
                       confirm: str | None = None):
    """Wipe queryable memory (legacy + drawers except audit, which trims
    only by retention). ``drawers`` narrows to a comma-separated subset;
    ``older_than`` (ISO datetime) keeps newer records - the run-end
    working purge passes both. The wipe itself is audit-recorded.

    ``confirm=wipe`` is required unless the caller is an internal run-end
    purge. Deleting every drawer from a single unauthenticated DELETE that
    returns 200 is a footgun: a stray ``/v1/memory/`` reached it and wiped
    all memory during adversarial testing.
    """
    t0 = time.time()
    # A *scoped* purge (one session, one drawer, or only-expired) is narrow by
    # construction and is what the run-end retention job uses, so it does not
    # need confirmation. An *unscoped* call deletes every drawer, and that one
    # must be asked for explicitly: a stray `DELETE /v1/memory/` reached this
    # handler during adversarial testing and wiped all memory.
    scoped = bool(session_id or drawers or older_than)
    if not scoped and confirm != "wipe":      # exact token, no trimming
        _log_mem("clear", t0, status="refused")
        return JSONResponse(status_code=400, content={
            "error": "an unscoped memory wipe requires confirm=wipe "
                     "(a session_id/drawers/older_than purge does not)"})
    try:
        drawer_list = [d.strip() for d in drawers.split(",") if d.strip()] \
            if drawers else None
        counts = await _plane(request).clear(session_id=session_id,
                                             drawers=drawer_list,
                                             older_than=older_than)
    except ValueError as e:
        _log_mem("clear", t0, status="error", error=str(e))
        raise HTTPException(400, str(e))
    _log_mem("clear", t0)
    return counts


@router.post("/v1/memory/sweep")
async def memory_sweep(request: Request):
    """Delete expired records (working TTL). For scheduled maintenance; also
    runs at startup and opportunistically on writes.

    ``drawers`` narrows the sweep to a subset. It used to be ignored entirely:
    a caller posting ``{"drawer": "not_a_drawer"}`` got a 200 and a full
    per-drawer report, so an unknown drawer silently looked like a successful
    sweep. An unrecognised name is now a 400.
    """
    t0 = time.time()
    body: dict = {}
    try:
        raw = await request.body()
        if raw:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                return JSONResponse(status_code=400,
                                    content={"error": "body must be an object"})
            body = parsed
    except json.JSONDecodeError:
        return JSONResponse(status_code=400,
                            content={"error": "invalid JSON body"})
    # Distinguish "key absent" (sweep everything) from "key present but empty".
    # An empty list used to fall through to the all-drawers sweep, so
    # `{"drawers": []}` silently deleted expired records from every drawer.
    requested = None
    if "drawers" in body or "drawer" in body:
        requested = body.get("drawers")
        if requested is None:
            requested = body.get("drawer")
    plane = _plane(request)
    if requested is not None:
        names = ([requested] if isinstance(requested, str)
                 else [str(d) for d in requested])
        wanted = [n.strip() for n in names if str(n).strip()]
        if not wanted:
            return JSONResponse(status_code=400, content={
                "error": "drawers must name at least one drawer "
                         f"(one of: {sorted(plane.drawers)})"})
        unknown = [n for n in wanted if n not in plane.drawers]
        if unknown:
            return JSONResponse(status_code=400, content={
                "error": f"unknown drawer(s) {unknown} "
                         f"(one of: {sorted(plane.drawers)})"})
        swept = {n: plane.drawers[n].sweep_expired() for n in wanted}
    else:
        swept = plane.sweep()
    _log_mem("sweep", t0)
    return {"swept": swept}


@router.get("/v1/memory/stats")
async def memory_stats(request: Request):
    return _plane(request).stats()


class ProposeRequest(BaseModel):
    descriptor: str
    procedure: dict = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    source: str
    run_id: str
    principal_role: Role = "agent"
    principal_id: str = "assistant"
    expires_days: int = 7


class ApproveRequest(BaseModel):
    proposal_id: str
    run_id: str
    principal_role: Role = "agent"
    principal_id: str = "assistant"


@router.get("/v1/memory/episodes")
async def memory_episodes(request: Request, session_id: str | None = None,
                          limit: int = 50):
    """Run history without vector work (newest-first episode records).
    Linkage to conversation turns rides on run_id/session_id."""
    return {"items": [i.model_dump(mode="json") for i in
                      _plane(request).episodes(session_id=session_id,
                                               limit=min(limit, 500))]}


@router.post("/v1/memory/playbook/propose")
async def playbook_propose(req: ProposeRequest, request: Request):
    """Phase A consolidation: propose a reusable procedure. The proposal
    is a TTL'd working record; promotion needs a separate approval —
    the model can never self-promote into the playbook drawer."""
    plane = _plane(request)
    t0 = time.time()
    try:
        item = await plane.propose_playbook(
            descriptor=req.descriptor, procedure=req.procedure,
            evidence_ids=req.evidence_ids, source=req.source,
            run_id=req.run_id, principal_role=req.principal_role,
            principal_id=req.principal_id, expires_days=req.expires_days)
    except PermissionError as e:
        _log_mem("propose", t0, status="error", error=str(e),
                 prompt_chars=len(req.descriptor or ""))
        raise HTTPException(403, str(e))
    except LookupError as e:
        _log_mem("propose", t0, status="error", error=str(e),
                 prompt_chars=len(req.descriptor or ""))
        raise HTTPException(404, str(e))
    except PayloadTooLarge as e:
        _log_mem("propose", t0, status="error", error=str(e),
                 prompt_chars=len(req.descriptor or ""))
        raise HTTPException(413, str(e))
    except ValueError as e:
        _log_mem("propose", t0, status="error", error=str(e),
                 prompt_chars=len(req.descriptor or ""))
        raise HTTPException(503, str(e))
    _log_mem("propose", t0, prompt_chars=len(req.descriptor or ""))
    return {"item": item.model_dump(mode="json")}


@router.post("/v1/memory/playbook/approve")
async def playbook_approve(req: ApproveRequest, request: Request):
    """Promote a proposal to a playbook record. System/operator only
    (fail-closed + audited); the proposal is marked consumed."""
    plane = _plane(request)
    t0 = time.time()
    try:
        item = await plane.approve_playbook(
            proposal_id=req.proposal_id, run_id=req.run_id,
            principal_role=req.principal_role,
            principal_id=req.principal_id)
    except PermissionError as e:
        _log_mem("approve", t0, status="error", error=str(e))
        raise HTTPException(403, str(e))
    except LookupError as e:
        _log_mem("approve", t0, status="error", error=str(e))
        raise HTTPException(404, str(e))
    except ValueError as e:
        _log_mem("approve", t0, status="error", error=str(e))
        raise HTTPException(400, str(e))
    _log_mem("approve", t0)
    return {"item": item.model_dump(mode="json")}
