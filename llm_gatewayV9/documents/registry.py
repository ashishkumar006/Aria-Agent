"""The document registry: uploads, status, chunk progress, enable/disable.

State machine:

    pending -> parsing -> chunking -> embedding -> ready
                                            \\-> blocked  (Ollama down)
                                            \\-> failed   (parse error)

Two rules the rest of the feature depends on:

* **Enable is only permitted at `ready`.** A half-embedded document is worse
  than none: the user would enable it and get partial answers with no error.
* **Resume is idempotent and its unit is one batch.** A crash re-does at most
  one batch of chunks, never the document.

Storage is a single JSON file written atomically, matching the rest of the
gateway's state handling. No size caps are imposed on uploads; pacing the
embedding worker is what protects the shared Ollama model.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

# ── statuses ─────────────────────────────────────────────────────────────────
PENDING = "pending"
PARSING = "parsing"
CHUNKING = "chunking"
EMBEDDING = "embedding"
READY = "ready"
BLOCKED = "blocked"
FAILED = "failed"

TERMINAL_OK = {READY}
# Statuses in which a document may be turned on for retrieval. A blocked
# document has chunks but incomplete vectors; a failed one has neither.
ENABLABLE = {READY}

UPLOAD_DIR = Path(os.environ.get(
    "DOCUMENT_STORE", str(Path(__file__).resolve().parent.parent
                          / "state" / "documents")))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Document:
    id: str
    filename: str
    doc_type: str
    size_bytes: int
    status: str = PENDING
    enabled: bool = False
    chunk_count: int = 0
    embedded_count: int = 0
    # Chunk indices already embedded. Persisted so a restart resumes exactly
    # where it stopped instead of redoing the whole document.
    embedded_indices: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    embed_model: str = ""
    embed_dim: int = 0
    title: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @property
    def is_ready(self) -> bool:
        return self.status == READY

    def progress(self) -> float:
        if not self.chunk_count:
            return 0.0
        return round(self.embedded_count / self.chunk_count, 4)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["progress"] = self.progress()
        d["can_enable"] = self.status in ENABLABLE
        return d


class Registry:
    """Thread-safe, atomically persisted document table."""

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root or UPLOAD_DIR)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "documents.json"
        self._lock = threading.RLock()

    # ── persistence ─────────────────────────────────────────────────────────
    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            # A corrupt registry must not wedge the whole feature. Keep the
            # bad file for inspection rather than deleting evidence.
            try:
                (self.root / "documents.corrupt.json").write_text(
                    self.path.read_text(encoding="utf-8", errors="replace"),
                    encoding="utf-8")
            except Exception:
                pass
            return []

    def _save(self, docs: list[dict]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(docs, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # ── CRUD ────────────────────────────────────────────────────────────────
    def add(self, *, filename: str, doc_type: str, size_bytes: int,
            title: str = "") -> Document:
        doc = Document(id=f"doc-{uuid.uuid4().hex[:10]}",
                       filename=filename, doc_type=doc_type,
                       size_bytes=size_bytes, title=title or filename)
        with self._lock:
            docs = self._load()
            docs.append(asdict(doc))
            self._save(docs)
        return doc

    def get(self, doc_id: str) -> Document | None:
        with self._lock:
            for d in self._load():
                if d.get("id") == doc_id:
                    return Document(**_known(d, Document))
        return None

    def list(self) -> list[Document]:
        with self._lock:
            return [Document(**_known(d, Document)) for d in self._load()]

    def enabled_ids(self) -> set[str]:
        with self._lock:
            return {d["id"] for d in self._load()
                    if d.get("enabled") and d.get("status") == READY}

    def update(self, doc_id: str, **fields) -> Document | None:
        with self._lock:
            docs = self._load()
            for d in docs:
                if d.get("id") == doc_id:
                    d.update(fields)
                    d["updated_at"] = _now()
                    self._save(docs)
                    return Document(**_known(d, Document))
        return None

    def set_status(self, doc_id: str, status: str, error: str = "") -> Document | None:
        fields: dict = {"status": status}
        if error:
            fields["error"] = error
        if status in (READY, FAILED):
            # A finished document that was somehow enabled stays off; only a
            # completed one may be used.
            with self._lock:
                docs = self._load()
                for d in docs:
                    if d.get("id") == doc_id:
                        if status == READY:
                            d["error"] = ""
                        else:
                            d["enabled"] = False
                self._save(docs)
        return self.update(doc_id, **fields)

    def set_enabled(self, doc_id: str, enabled: bool) -> tuple[Document | None, str]:
        """Returns (document, error). Refuses to enable anything not ready."""
        doc = self.get(doc_id)
        if doc is None:
            return None, f"no such document {doc_id}"
        if enabled:
            if doc.status not in ENABLABLE:
                return doc, (
                    f"cannot enable a document that is {doc.status}"
                    + (f" ({doc.error})" if doc.error else "")
                    + " - wait until it finishes embedding, or fix the error")
            if doc.chunk_count and doc.embedded_count < doc.chunk_count:
                return doc, ("cannot enable a partially embedded document "
                              f"({doc.embedded_count}/{doc.chunk_count} chunks)")
        return self.update(doc_id, enabled=enabled), ""

    def mark_embedded(self, doc_id: str, indices: list[int],
                      model: str = "", dim: int = 0) -> Document | None:
        """Record a batch's worth of embedded chunks. Idempotent."""
        with self._lock:
            docs = self._load()
            for d in docs:
                if d.get("id") != doc_id:
                    continue
                seen = set(d.get("embedded_indices") or [])
                seen.update(indices)
                d["embedded_indices"] = sorted(seen)
                d["embedded_count"] = len(d["embedded_indices"])
                if model:
                    d["embed_model"] = model
                if dim:
                    d["embed_dim"] = dim
                d["updated_at"] = _now()
                if (d["chunk_count"] and
                        d["embedded_count"] >= d["chunk_count"]):
                    d["status"] = READY
                    d["error"] = ""
                self._save(docs)
                return Document(**_known(d, Document))
        return None

    def pending_indices(self, doc_id: str) -> list[int]:
        doc = self.get(doc_id)
        if doc is None:
            return []
        done = set(doc.embedded_indices)
        return [i for i in range(doc.chunk_count) if i not in done]

    def delete(self, doc_id: str) -> bool:
        with self._lock:
            docs = self._load()
            keep = [d for d in docs if d.get("id") != doc_id]
            if len(keep) == len(docs):
                return False
            self._save(keep)
        try:
            src = self.root / f"{doc_id}.source"
            if src.exists():
                src.unlink()
        except Exception:
            pass
        return True

    # ── uploaded bytes ──────────────────────────────────────────────────────
    def source_path(self, doc_id: str) -> Path | None:
        for d in self._load():
            if d.get("id") == doc_id:
                return self.root / f"{doc_id}.source"
        return None

    def write_source(self, doc_id: str, data: bytes) -> Path:
        p = self.root / f"{doc_id}.source"
        p.write_bytes(data)
        return p

    def read_source(self, doc_id: str) -> bytes | None:
        p = self.root / f"{doc_id}.source"
        return p.read_bytes() if p.exists() else None


def _known(raw: dict, cls):
    """Build a dataclass from stored dict, ignoring keys added by later
    versions so an older registry file still loads."""
    fields = {f for f in cls.__dataclass_fields__}
    return {k: v for k, v in raw.items() if k in fields}


# ── the indexing worker ──────────────────────────────────────────────────────

BATCH_DEFAULT = 16
BATCH_MAX = 32
YIELD_BETWEEN_BATCHES_S = float(
    os.getenv("DOCUMENT_EMBED_YIELD_S", "1.5") or 1.5)
# After this many consecutive batches blocked by an unavailable embedder, the
# worker stops trying so it does not spin. A later job picks it up again.
MAX_BLOCKED_ROUNDS = 3


class EmbedderUnavailable(RuntimeError):
    """The embedding backend is down; the document should go to `blocked`
    and resume later, NOT to `failed` and not to `ready`."""


class Indexer:
    """Parses, chunks and embeds one document at a time."""

    def __init__(self, registry: Registry, *, batch_size: int = BATCH_DEFAULT,
                 embed=None):
        self.registry = registry
        self.batch_size = max(1, min(batch_size, BATCH_MAX))
        # Injected for tests; production passes the gateway's batch client.
        self._embed = embed

    def _embed_batch(self, texts: list[str]):
        if self._embed is None:
            raise EmbedderUnavailable("no embedder configured")
        return self._embed(texts)

    def run(self, doc_id: str, *, stop_after_batches: int | None = None) -> Document | None:
        """Index (or resume indexing) one document.

        Order matters: parse and chunk BEFORE any embedding is attempted, so a
        parse failure is reported as `failed` immediately rather than after a
        long embed run that was always going to fail.
        """
        reg = self.registry
        doc = reg.get(doc_id)
        if doc is None:
            return None

        if not doc.chunk_count:
            reg.set_status(doc_id, PARSING)
            data = reg.read_source(doc_id)
            if data is None:
                return reg.set_status(doc_id, FAILED,
                                      "the uploaded file is missing")
            from documents import parsers as P
            from documents.chunker import chunk_blocks
            try:
                parsed = P.parse(doc.filename, data)
            except P.UnsupportedDocument as e:
                return reg.set_status(doc_id, FAILED, str(e))
            except Exception as e:
                return reg.set_status(doc_id, FAILED,
                                      f"could not parse the file: {e}")
            reg.update(doc_id, warnings=parsed.warnings)
            if not parsed.blocks:
                return reg.set_status(
                    doc_id, FAILED,
                    "no readable text found" +
                    (" (a scanned PDF needs OCR)" if doc.doc_type == "pdf"
                     else ""))

            reg.set_status(doc_id, CHUNKING)
            chunks = chunk_blocks(parsed.blocks)
            if not chunks:
                return reg.set_status(doc_id, FAILED, "produced no chunks")
            reg.update(doc_id, chunk_count=len(chunks))

        todo = reg.pending_indices(doc_id)
        if not todo:
            return reg.set_status(doc_id, READY)

        reg.set_status(doc_id, EMBEDDING)
        blocked_rounds = 0
        done_batches = 0
        while todo:
            window = todo[:self.batch_size]
            texts = self._chunk_texts(doc_id, window)
            try:
                result = self._embed_batch(texts)
            except EmbedderUnavailable as e:
                # `blocked`, never `failed` and never `ready`: the work done
                # so far is kept and resumes when the embedder returns.
                blocked_rounds += 1
                if blocked_rounds >= MAX_BLOCKED_ROUNDS:
                    return reg.set_status(doc_id, BLOCKED, str(e)[:200])
                time.sleep(YIELD_BETWEEN_BATCHES_S)
                continue
            vectors = result.get("embeddings") or []
            ok = [window[j] for j, v in enumerate(vectors) if v]
            if not ok:
                blocked_rounds += 1
                if blocked_rounds >= MAX_BLOCKED_ROUNDS:
                    return reg.set_status(doc_id, BLOCKED,
                                          "the embedder returned nothing")
                time.sleep(YIELD_BETWEEN_BATCHES_S)
                continue
            blocked_rounds = 0
            self._store_chunks(doc_id, window, vectors, result)
            reg.mark_embedded(doc_id, ok,
                              model=(result.get("embed_model")
                                     or result.get("model") or ""),
                              dim=int(result.get("embed_dim")
                                      or result.get("dim") or 0))
            todo = reg.pending_indices(doc_id)
            done_batches += 1
            if stop_after_batches is not None and done_batches >= stop_after_batches:
                # Deliberate pause: status stays `embedding` with progress
                # recorded, and a later run resumes from here.
                return reg.get(doc_id)
            if todo:
                # Yield the shared embedding model between batches so memory
                # recall and other work can interleave.
                time.sleep(YIELD_BETWEEN_BATCHES_S)
        return reg.get(doc_id)

    # ── hooks the registry subclasses / tests supply ────────────────────────
    def _chunk_texts(self, doc_id: str, indices: list[int]) -> list[str]:
        raise NotImplementedError

    def _store_chunks(self, doc_id: str, indices: list[str],
                      vectors: list, result: dict) -> None:
        raise NotImplementedError