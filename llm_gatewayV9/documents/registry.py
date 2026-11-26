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


def _enable_when_ready(row: dict) -> None:
    """Honour the auto-enable intent on a row that has just reached READY.

    Two places drive a document to READY — `set_status`, and `mark_embedded`
    when the last batch lands — so the rule lives in one helper both call.
    Putting it in only one of them is how an "auto-enable" document stays off:
    the normal indexing path never calls `set_status(READY)` at all.

    The partial-embed guard is the reason enable was manual in the first place:
    a document with 17 of 50 chunks embedded answers questions in pieces and
    reports nothing, so intent alone is not enough.

    `is False`, not falsy: a row written before this field existed has no key at
    all, and absence means "nobody ever declined" — the truth for every document
    already in the library when this landed. Reading a missing key as
    `not auto_enable` is what kept 21 finished, embedded documents switched off
    with no switch anyone had touched."""
    if str(row.get("status") or "") != READY or row.get("auto_enable") is False:
        return
    chunks = int(row.get("chunk_count") or 0)
    done = int(row.get("embedded_count") or 0)
    if chunks == 0 or done >= chunks:
        row["enabled"] = True


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
    # Parser/chunker version this document's chunk_count was
    # computed under (see INDEX_FORMAT_VERSION). 0 means
    # "unknown/legacy" and forces a re-index.
    index_version: int = 0
    title: str = ""
    # Empty means the virtual "unfiled" bucket shown as the root of the
    # Documents tree. A document can be moved between folders without
    # re-indexing: chunks are keyed by the stable document id.
    folder_id: str = ""
    # Where this document came from, so the agent and the user can tell a
    # generated report from a hand-uploaded file: "upload", "authoring",
    # "research", "chat", "web", "telegram", "persona". "" is treated as
    # "upload" — the pre-provenance rows in an existing registry.
    origin: str = ""
    # The thing it came from: an artifact handle (`art:...`), a URL, or a
    # document it was derived from. This is the link that lets the agent go
    # from a produced document back to the stored bytes, instead of
    # re-fetching the web or re-rendering the file.
    origin_ref: str = ""
    session_id: str = ""
    # A new document joins retrieval as soon as it is fully embedded. The
    # switch is deliberately not "at upload": enabling a half-embedded
    # document answers questions partially and reports nothing, so the intent
    # is recorded here and honoured by `set_status` when READY arrives.
    auto_enable: bool = True
    # Content hash of the stored bytes. Artifacts are content-addressed and the
    # agent re-derives the same file repeatedly — a re-render, a re-fetch, the
    # same finding spilled from two runs — and each upload used to create
    # another document with another copy of every chunk. Retrieval then answers
    # the same source twice, and the library grows rows nobody asked for.
    sha256: str = ""
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


@dataclass
class Folder:
    """A logical document folder. parent_id="" is a top-level folder.

    Folders are metadata only: a document's chunks never depend on them, so
    moving a file between folders costs one registry write, not a re-index.
    """

    id: str
    name: str
    parent_id: str = ""
    # A system folder is one Aria files its own output into (Authoring,
    # Research, Web sources...). It is created on demand and recreated if
    # deleted, so marking it is about telling the user "this one is ours",
    # not about protecting it.
    system: bool = False
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return asdict(self)


FOLDER_MAX_DEPTH = 5


class Registry:
    """Thread-safe, atomically persisted document table."""

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root or UPLOAD_DIR)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "documents.json"
        self.folders_path = self.root / "folders.json"
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

    def _write_json(self, path: Path, payload: str) -> None:
        # On Windows the atomic replace is racy: any reader
        # holding the destination open — even for
        # microseconds — turns `os.replace` into
        # Access Denied, and the indexer saves after every
        # batch while API handlers keep reading the file.
        # Retry with a per-save temp name so a retry can
        # never clobber another writer's in-flight file.
        last: Exception | None = None
        for attempt in range(5):
            tmp = path.with_name(
                f"{path.stem}.{os.getpid()}.{attempt}.tmp")
            try:
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, path)
                return
            except OSError as e:
                last = e
                time.sleep(0.05 * (attempt + 1))
        raise last

    def _save(self, docs: list[dict]) -> None:
        self._write_json(self.path, json.dumps(docs, indent=2))

    def _load_folders(self) -> list[dict]:
        if not self.folders_path.exists():
            return []
        try:
            data = json.loads(self.folders_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            try:
                (self.root / "folders.corrupt.json").write_text(
                    self.folders_path.read_text(encoding="utf-8",
                                                errors="replace"),
                    encoding="utf-8")
            except Exception:
                pass
            return []

    def _save_folders(self, folders: list[dict]) -> None:
        self._write_json(self.folders_path, json.dumps(folders, indent=2))

    # ── CRUD ────────────────────────────────────────────────────────────────
    def add(self, *, filename: str, doc_type: str, size_bytes: int,
            title: str = "", folder_id: str = "", origin: str = "",
            origin_ref: str = "", session_id: str = "",
            auto_enable: bool = True, sha256: str = "") -> Document:
        if folder_id and not self.folder_exists(folder_id):
            raise ValueError("folder not found")
        doc = Document(id=f"doc-{uuid.uuid4().hex[:10]}",
                       filename=filename, doc_type=doc_type,
                       size_bytes=size_bytes, title=title or filename,
                       folder_id=folder_id or "",
                       origin=origin or "upload",
                       origin_ref=origin_ref or "",
                       session_id=session_id or "",
                       auto_enable=bool(auto_enable),
                       sha256=str(sha256 or ""))
        with self._lock:
            docs = self._load()
            docs.append(asdict(doc))
            self._save(docs)
        return doc

    def find_by_sha(self, sha256: str) -> Document | None:
        """The live document already holding these exact bytes, if any.

        A FAILED row is not an answer: that upload did not produce chunks, and
        the caller is trying again on purpose.
        """
        want = str(sha256 or "")
        if not want:
            return None
        with self._lock:
            for d in self._load():
                if str(d.get("sha256") or "") == want and d.get("status") != FAILED:
                    return Document(**_known(d, Document))
        return None

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
                            # The helper reads the row's own status, and
                            # `update()` below writes it again — set it here so
                            # the enable decision sees the state it is deciding
                            # for.
                            d["status"] = READY
                            _enable_when_ready(d)
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
        else:
            # Switching a document off is a decision about that document, not
            # a temporary state: the auto-enable intent has to go with it, or
            # the next re-index would turn it back on against the user's wish.
            return self.update(doc_id, enabled=False, auto_enable=False), ""
        return self.update(doc_id, enabled=enabled, auto_enable=True), ""

    def auto_enable_ready(self) -> int:
        """Switch on every completed document that was never switched off.

        Auto-enable is decided at READY, so a document that finished indexing
        BEFORE the rule existed stays off forever — and the library then looks
        like a pile of files the agent cannot see, which is the complaint this
        replaces. This is the backfill for those rows: `enabled` follows the
        same rule `_enable_when_ready` applies to a fresh one, and a document
        the user turned off deliberately is skipped, because turning one off
        also clears its `auto_enable` intent.

        Returns how many rows were switched on.
        """
        with self._lock:
            docs = self._load()
            changed = 0
            for d in docs:
                if d.get("enabled") or d.get("auto_enable") is False:
                    continue
                before = d.get("enabled")
                _enable_when_ready(d)
                if d.get("enabled") and d.get("enabled") != before:
                    # Record the intent the row was missing, so the ambiguity
                    # cannot be inherited: from here a switch-off writes False
                    # and means it.
                    d["auto_enable"] = True
                    changed += 1
            if changed:
                self._save(docs)
            return changed

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
                    # This is the path a normal index actually reaches READY
                    # through, so the auto-enable decision has to be here as
                    # well as in `set_status`.
                    _enable_when_ready(d)
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
        try:
            cap = self.root / f"{doc_id}.captions.json"
            if cap.exists():
                cap.unlink()
        except Exception:
            pass
        return True

    # ── folders ─────────────────────────────────────────────────────────────
    def list_folders(self) -> list[Folder]:
        with self._lock:
            return [Folder(**_known(d, Folder)) for d in self._load_folders()]

    def folder_exists(self, folder_id: str) -> bool:
        if not folder_id:
            return False
        with self._lock:
            return any(f.get("id") == folder_id for f in self._load_folders())

    def _folder_depth(self, folders: list[dict], parent_id: str) -> int:
        depth = 0
        seen: set[str] = set()
        current = parent_id
        while current:
            if current in seen:
                raise ValueError("folder tree contains a cycle")
            seen.add(current)
            row = next((f for f in folders if f.get("id") == current), None)
            if row is None:
                raise ValueError("parent folder not found")
            depth += 1
            current = str(row.get("parent_id") or "")
        return depth

    def folder_path(self, folder_id: str) -> str:
        with self._lock:
            folders = self._load_folders()
            names: list[str] = []
            seen: set[str] = set()
            current = folder_id
            while current:
                if current in seen:
                    break
                seen.add(current)
                row = next((f for f in folders if f.get("id") == current), None)
                if row is None:
                    break
                names.append(str(row.get("name") or ""))
                current = str(row.get("parent_id") or "")
            return " / ".join(reversed([n for n in names if n]))

    def create_folder(self, name: str, parent_id: str = "") -> Folder:
        clean = self._clean_folder_name(name)
        parent_id = str(parent_id or "")
        with self._lock:
            folders = self._load_folders()
            if parent_id and not any(f.get("id") == parent_id for f in folders):
                raise ValueError("parent folder not found")
            if parent_id and self._folder_depth(folders, parent_id) >= FOLDER_MAX_DEPTH:
                raise ValueError(f"folders can be nested at most {FOLDER_MAX_DEPTH} deep")
            if any((f.get("parent_id") or "") == parent_id
                   and str(f.get("name") or "").casefold() == clean.casefold()
                   for f in folders):
                raise ValueError("a folder with that name already exists here")
            folder = Folder(id=f"folder-{uuid.uuid4().hex[:10]}", name=clean,
                            parent_id=parent_id)
            folders.append(asdict(folder))
            self._save_folders(folders)
        return folder

    def ensure_folder(self, name: str, *, system: bool = False) -> Folder:
        """The top-level folder called `name`, created on first use.

        Generated documents are filed by the section that made them, so the
        agent can tell a research report from a user's upload without reading
        either. Idempotent by name, and a folder the user deleted is simply
        recreated the next time something needs it."""
        clean = self._clean_folder_name(name)
        with self._lock:
            folders = self._load_folders()
            for f in folders:
                if (str(f.get("parent_id") or "") == ""
                        and str(f.get("name") or "").casefold() == clean.casefold()):
                    return Folder(**_known(f, Folder))
            folder = Folder(id=f"folder-{uuid.uuid4().hex[:10]}", name=clean,
                            system=bool(system))
            folders.append(asdict(folder))
            self._save_folders(folders)
        return folder

    def rename_folder(self, folder_id: str, name: str) -> Folder:
        clean = self._clean_folder_name(name)
        with self._lock:
            folders = self._load_folders()
            row = next((f for f in folders if f.get("id") == folder_id), None)
            if row is None:
                raise ValueError("folder not found")
            parent = str(row.get("parent_id") or "")
            if any(f.get("id") != folder_id
                   and (f.get("parent_id") or "") == parent
                   and str(f.get("name") or "").casefold() == clean.casefold()
                   for f in folders):
                raise ValueError("a folder with that name already exists here")
            row["name"] = clean
            self._save_folders(folders)
            return Folder(**_known(row, Folder))

    def delete_folder(self, folder_id: str) -> bool:
        """Delete an EMPTY folder. Moving/deleting its contents is explicit:
        silently cascading into documents would destroy indexed chunks and
        source bytes from one click on a tree row."""
        with self._lock:
            folders = self._load_folders()
            if not any(f.get("id") == folder_id for f in folders):
                return False
            if any(str(f.get("parent_id") or "") == folder_id for f in folders):
                raise ValueError("folder has subfolders; delete or move them first")
            if any(str(d.get("folder_id") or "") == folder_id for d in self._load()):
                raise ValueError("folder contains documents; move or delete them first")
            self._save_folders([f for f in folders if f.get("id") != folder_id])
        return True

    def move_document(self, doc_id: str, folder_id: str) -> Document | None:
        folder_id = str(folder_id or "")
        if folder_id and not self.folder_exists(folder_id):
            raise ValueError("folder not found")
        with self._lock:
            docs = self._load()
            for d in docs:
                if d.get("id") == doc_id:
                    d["folder_id"] = folder_id
                    d["updated_at"] = _now()
                    self._save(docs)
                    return Document(**_known(d, Document))
        return None

    def folder_subtree(self, folder_id: str) -> set[str]:
        """The folder and every descendant id, cycle-safe."""
        with self._lock:
            folders = self._load_folders()
            children: dict[str, list[str]] = {}
            for f in folders:
                children.setdefault(str(f.get("parent_id") or ""), []).append(
                    str(f.get("id") or ""))
            out: set[str] = set()
            stack = [folder_id] if folder_id else []
            while stack:
                current = stack.pop()
                if not current or current in out:
                    continue
                out.add(current)
                stack.extend(children.get(current, []))
            return out

    def document_ids_in_folders(self, folder_ids: set[str],
                                enabled_only: bool = True) -> set[str]:
        wanted: set[str] = set()
        for fid in folder_ids:
            wanted |= self.folder_subtree(fid)
        with self._lock:
            return {
                str(d.get("id") or "") for d in self._load()
                if str(d.get("folder_id") or "") in wanted
                and (not enabled_only
                     or (d.get("enabled") and d.get("status") == READY))
                and d.get("id")
            }

    @staticmethod
    def _clean_folder_name(name: str) -> str:
        clean = " ".join(str(name or "").split())
        if not clean:
            raise ValueError("folder name is required")
        if len(clean) > 80:
            raise ValueError("folder name is too long (max 80)")
        if any(ord(c) < 0x20 or ord(c) == 0x7f for c in clean):
            raise ValueError("folder name contains control characters")
        if "/" in clean or "\\" in clean:
            raise ValueError("folder name cannot contain slashes")
        return clean

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

    # ── image captions (index-time, persisted) ──────────────────────────────
    # A caption is generated once by the indexing worker (alt-text or a vision
    # call) and persisted here, keyed by the picture's content hash. The chunk
    # list is rebuilt from these on every re-parse, so a non-deterministic model
    # answer never shifts chunk indices between the indexer and the detail
    # route. Reading is unconditional: once a caption is part of a document's
    # index it stays part of it; the master flag gates GENERATION only.
    def read_captions(self, doc_id: str) -> list[dict]:
        p = self.root / f"{doc_id}.captions.json"
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def write_captions(self, doc_id: str, rows: list[dict]) -> None:
        with self._lock:
            self._write_json(self.root / f"{doc_id}.captions.json",
                             json.dumps(rows, indent=2))


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
# Bump whenever the parser or chunker changes in a way that alters
# chunk boundaries. A document indexed under an older version carries
# that version here; on resume, a mismatch resets it to a full
# re-parse instead of silently mixing the stored chunk_count with a
# fresh parse that no longer matches it (observed live: a parser
# fix changed a 69-chunk document into a 257-chunk one, and a
# resumed run embedded the first 69 chunks of the new parse while
# the registry still claimed the old ones).
INDEX_FORMAT_VERSION = 3
# After this many consecutive batches blocked by an unavailable embedder, the
# worker stops trying so it does not spin. A later job picks it up again.
MAX_BLOCKED_ROUNDS = 3


class EmbedderUnavailable(RuntimeError):
    """The embedding backend is down; the document should go to `blocked`
    and resume later, NOT to `failed` and not to `ready`."""


class ParseTimeout(RuntimeError):
    """The parser did not finish inside the deadline.

    A pathological file (a 7.9 MB PDF with a broken xref, a zip bomb, a
    document with a million pages) can make a parser spin forever. The
    symptom was a document parked on `parsing` with zero chunks, no error
    and no retry - and because the indexer serialises documents, it also
    blocked every other upload behind it.
    """


# How long a single parse may take before the document is failed with a
# clear reason. Parsing is CPU-bound and normally takes seconds; 5 minutes
# is far past anything healthy.
PARSE_TIMEOUT_S = float(os.getenv("DOCUMENT_PARSE_TIMEOUT_S", "300") or 300)


# ── image-content retrieval: the one central master switch ───────────────────
# Off by default. When off, the indexing worker generates no captions and a
# fresh document chunks exactly as it always did — no extra blocks, no sidecar,
# no residue. When on, embedded pictures (and bare image uploads) are described
# at index time — alt-text first, a vision call otherwise — and the description
# becomes a searchable chunk. Read fresh each call so a test (or an operator
# restarting with a different env) flips it without code changes.
def image_caption_enabled() -> bool:
    return str(os.getenv("DOCUMENT_IMAGE_CAPTION", "0") or "0").strip().lower() in (
        "1", "true", "yes", "on")


def image_caption_blocks(reg: "Registry", doc: "Document") -> list:
    """Persisted image captions as searchable blocks, in stable order.

    Pure read of the sidecar — no network, no generation — so the indexer and
    the detail route build the identical chunk list from it. Empty when the
    document has no captions (the flag-off / pre-feature case), which is what
    keeps the feature residue-free."""
    from documents.chunker import Block
    rows = reg.read_captions(doc.id)
    out: list = []
    for r in rows:
        text = str(r.get("caption") or "").strip()
        if not text:
            continue
        out.append(Block(kind="para", text=text,
                         page=int(r.get("page") or 0),
                         meta={"image": True,
                               "image_sha": str(r.get("sha") or ""),
                               "image_caption_source": str(r.get("source") or "")}))
    return out


def _parse_with_timeout(filename: str, data: bytes,
                        timeout_s: float | None = None):
    """Parse `data`, giving up after `timeout_s`.

    The parse runs on its own daemon thread so a parser that never returns
    cannot hold the document hostage: the caller gets a `ParseTimeout`, the
    document is marked failed with a reason, and the indexer moves on. The
    abandoned thread is a daemon, so it cannot keep the process alive.
    """
    from documents import parsers as P

    limit = PARSE_TIMEOUT_S if timeout_s is None else timeout_s
    box: dict = {}

    def _run():
        try:
            box["parsed"] = P.parse(filename, data)
        except BaseException as e:          # noqa: BLE001 - re-raised below
            box["error"] = e

    t = threading.Thread(target=_run, daemon=True, name="doc-parse")
    t.start()
    t.join(timeout=limit if limit > 0 else None)
    if t.is_alive():
        raise ParseTimeout(
            f"parsing {filename} took longer than {int(limit)}s and was "
            f"abandoned")
    if "error" in box:
        raise box["error"]
    return box.get("parsed")



class Indexer:
    """Parses, chunks and embeds one document at a time."""

    def __init__(self, registry: Registry, *, batch_size: int = BATCH_DEFAULT,
                 embed=None, caption=None):
        self.registry = registry
        self.batch_size = max(1, min(batch_size, BATCH_MAX))
        # Injected for tests; production passes the gateway's batch client.
        self._embed = embed
        # Optional index-time image captioner: `caption(doc, parsed)` generates
        # and persists descriptions for embedded pictures. None (the default,
        # and every test) means no captioning — the chunk list is the parse
        # alone, exactly as before the feature existed.
        self._caption = caption

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

        if doc.chunk_count and doc.index_version != INDEX_FORMAT_VERSION:
            # The chunker or parser changed since this document was
            # indexed. The stored chunk_count describes a chunk list
            # a fresh parse no longer produces, so resuming would
            # embed the wrong chunks under the wrong indices. Reset
            # and re-parse from scratch.
            reg.update(doc_id, chunk_count=0, embedded_indices=[],
                       embedded_count=0, status=PENDING, error="")
            doc = reg.get(doc_id)

        if not doc.chunk_count:
            reg.set_status(doc_id, PARSING)
            data = reg.read_source(doc_id)
            if data is None:
                return reg.set_status(doc_id, FAILED,
                                      "the uploaded file is missing")
            from documents import parsers as P
            from documents.chunker import chunk_blocks
            try:
                parsed = _parse_with_timeout(doc.filename, data)
            except P.UnsupportedDocument as e:
                return reg.set_status(doc_id, FAILED, str(e))
            except ParseTimeout as e:
                return reg.set_status(doc_id, FAILED, str(e)[:200])
            except Exception as e:
                return reg.set_status(doc_id, FAILED,
                                      f"could not parse the file: {e}")
            reg.update(doc_id, warnings=parsed.warnings)
            # Image-content retrieval: generate captions once (worker thread,
            # best-effort) then read them back as blocks. Generation is gated by
            # the master flag inside the captioner; the read is unconditional so
            # an already-captioned document keeps its chunks. A caption failure
            # must never fail the document — the text still indexes.
            if self._caption is not None:
                try:
                    self._caption(doc, parsed)
                except Exception as e:              # noqa: BLE001
                    print(f"[index] {doc_id} captioning skipped: "
                          f"{type(e).__name__}: {e}", flush=True)
            blocks = list(parsed.blocks) + image_caption_blocks(reg, doc)
            if not blocks:
                if doc.doc_type == "image":
                    # An image is a figure source, not a text source: having no
                    # blocks is what it means to succeed. Marking it failed made
                    # the upload impossible to enable, which put the figure out
                    # of reach of the one writer that could place it.
                    #
                    # The version stamp is not decoration. The reconcile treats
                    # an unversioned document as "indexed under an old parser"
                    # and re-queues it, so a READY image with index_version 0
                    # was reset to PENDING on every documents request and could
                    # never be enabled — observed live the minute this landed.
                    reg.update(doc_id, chunk_count=0, embedded_count=0,
                               index_version=INDEX_FORMAT_VERSION)
                    return reg.set_status(doc_id, READY)
                return reg.set_status(
                    doc_id, FAILED,
                    "no readable text found" +
                    (" (a scanned PDF needs OCR)" if doc.doc_type == "pdf"
                     else ""))

            reg.set_status(doc_id, CHUNKING)
            chunks = chunk_blocks(blocks)
            if not chunks:
                return reg.set_status(doc_id, FAILED, "produced no chunks")
            reg.update(doc_id, chunk_count=len(chunks),
                       index_version=INDEX_FORMAT_VERSION)

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
            if len(ok) != len(window):
                # The embedder returned fewer vectors than the
                # window asked for (or some came back falsy).
                # Those chunks are neither stored nor marked, so
                # they stay pending and this batch is retried -
                # loud, because a persistently short response
                # looks like a slow embed.
                print(f"[index] {doc_id} short batch {window[0]}-"
                      f"{window[-1]}: ok={len(ok)}/{len(window)} "
                      f"vectors={len(vectors)}", flush=True)
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