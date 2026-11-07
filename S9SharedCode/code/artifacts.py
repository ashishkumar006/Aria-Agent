"""Content-addressable artifact store.

Raw bytes the agent fetches or produces live here, keyed by sha256 of the
content. Memory holds the handle + a short descriptor; this module owns the
bytes. Perception sees handles, Decision sees bytes only when Perception
attaches them. The 50 KB of HTML touches exactly one LLM call across a run.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from schemas import Artifact

# Honours S9_STATE_DIR like every other store in this codebase
# (flags.py, auth.py, templates.py all do). It did not, so
# `tests/conftest.py` — which redirects S9_STATE_DIR to a tmp dir precisely
# so tests "do not pollute or corrupt the live dashboard" — did not cover
# this one, and every test that called `put` without repointing STORE wrote
# into production. Three `source=test` fixtures were found sitting in the live
# store. Hardcoding also made the store the one place a state reset could not
# clear.
STORE = Path(os.environ.get("S9_STATE_DIR")
             or (Path(__file__).parent / "state")) / "artifacts"
STORE.mkdir(parents=True, exist_ok=True)


# Two skills fan out and can call put() on the same content at the same time.
# Without a lock, both see `not exists()` and both write; with a plain
# write-then-rename, a reader could still see the window. Windows makes this
# worse: os.replace fails with AccessDenied when another process holds the
# destination open, so the retry matters.
_PUT_LOCK = threading.Lock()


def _atomic_write(path: Path, blob: bytes) -> None:
    """Write `blob` to `path` so a reader never sees a partial file."""
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        tmp.write_bytes(blob)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                import time as _t
                _t.sleep(0.05 * (attempt + 1))
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _digest_for(artifact_id: str) -> str:
    """Validate an `art:<hex16>` handle and return the digest.

    Raises ValueError on malformed handles instead of leaking a
    FileNotFoundError from deep in pathlib.
    """
    digest = artifact_id.removeprefix("art:")
    if len(digest) != 16 or any(c not in "0123456789abcdef" for c in digest.lower()):
        raise ValueError(f"malformed artifact handle: {artifact_id!r}")
    return digest.lower()


# Where each kind of stored output belongs in the user's Documents library, and
# the origin recorded with it. The folder is the human-facing half ("which
# section made this?"), the origin is the machine-readable one. Kept here, next
# to the store, because both the orchestrator and the skills layer file things
# and must not drift apart on names.
ARTIFACT_FILING: dict[str, tuple[str, str]] = {
    "author": ("Authoring", "authoring"),
    "deck": ("Decks", "deck"),
    "formatter": ("Authoring", "authoring"),
    "researcher": ("Research", "research"),
    "planner": ("Research", "research"),
    "coder": ("Run outputs", "code"),
    "sandbox_executor": ("Run outputs", "code"),
    "web": ("Web sources", "web"),
    "chat": ("Chat", "chat"),
}


def filing_for(skill: str) -> tuple[str, str]:
    """(folder name, origin) for a producing skill name.

    A source may carry a qualifier — `skill:author`, `web:https://example.com`
    — and the family before the first colon is what decides where it lives.
    Without that split every fetched page filed under `Run outputs`, because no
    key containing a URL is ever in the table.
    """
    key = str(skill or "").strip().lower().removeprefix("skill:")
    head = key.split(":", 1)[0].strip()
    return ARTIFACT_FILING.get(head, ("Run outputs", head or "run"))


def put(blob: bytes, *, content_type: str, source: str, descriptor: str) -> str:
    """Write blob (deduped by content hash) and return its handle."""
    digest = hashlib.sha256(blob).hexdigest()[:16]
    art_id = f"art:{digest}"
    bin_path = STORE / f"{digest}.bin"
    meta_path = STORE / f"{digest}.json"
    # `STORE.mkdir` only runs at import. Anything that repoints STORE at a
    # fresh directory (a test's tmp_path, a state reset) then found a
    # missing directory on the first write, so the spill path failed with
    # FileNotFoundError instead of storing the bytes.
    STORE.mkdir(parents=True, exist_ok=True)
    # Atomic, and serialised. `write_bytes` in place meant a crash mid-write
    # left a truncated .bin under the hash of the FULL content, and content
    # addressing cannot detect that: every later put of the same bytes sees
    # `exists()` and skips the write, so the corruption is permanent and
    # unrecoverable. Write beside the target, then swap.
    with _PUT_LOCK:
        if not bin_path.exists():
            _atomic_write(bin_path, blob)
    # Always (re)write meta: a same-bytes re-put with a new descriptor or
    # source must not keep serving the stale first-seen metadata.
    #
    # The document link carries over, though. Re-storing identical bytes is a
    # different caller describing the same content, and dropping `document_id`
    # here would orphan an artifact that is already indexed and searchable.
    prior: dict = {}
    if meta_path.exists():
        try:
            prior = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            prior = {}
    meta = Artifact(
        id=art_id,
        content_type=content_type,
        size_bytes=len(blob),
        source=source,
        descriptor=descriptor,
        document_id=str(prior.get("document_id") or ""),
        origin=str(prior.get("origin") or ""),
        session_id=str(prior.get("session_id") or ""),
    )
    _atomic_write(meta_path, meta.model_dump_json(indent=2).encode("utf-8"))
    return art_id


def link_document(artifact_id: str, document_id: str, *, origin: str = "",
                  session_id: str = "") -> None:
    """Record which Documents-store entry this artifact's bytes live in."""
    try:
        meta = get_meta(artifact_id)
    except Exception:
        return
    update = {"document_id": str(document_id or "")}
    if origin:
        update["origin"] = str(origin)[:32]
    if session_id:
        update["session_id"] = str(session_id)[:80]
    patched = meta.model_copy(update=update)
    digest = _digest_for(artifact_id)
    try:
        _atomic_write(STORE / f"{digest}.json",
                      patched.model_dump_json(indent=2).encode("utf-8"))
    except Exception:
        pass


def register_with_documents(artifact_id: str, *, origin: str,
                            folder_name: str, title: str = "",
                            session_id: str = "", filename: str = "",
                            origin_ref: str = "") -> str:
    """Index an artifact into the gateway's Documents plane and link it back.

    Every artifact the agent keeps should be chunked and embedded, so the next
    turn can re-read, explain or cross-check it from disk instead of fetching
    the web again or re-rendering the file. The gateway's document plane is the
    one place that parses, chunks, embeds, files and enables — so that is where
    an artifact goes, with its provenance.

    Returns the document id, or "" if the artifact could not be registered.
    Never raises: an unindexed artifact is still an artifact, and a run must
    not fail because an index call was slow or the gateway was away."""
    import gateway

    try:
        meta = get_meta(artifact_id)
    except Exception:
        return ""
    # Content-addressed, so the same bytes fetched or rendered twice are the
    # same artifact — and it is already indexed. Re-uploading would create a
    # second document with the same chunks and no way to tell them apart.
    #
    # Checked against the plane, not just trusted: when the user deletes the
    # document, the link in this sidecar outlives it. Honouring a dead id is
    # worse than re-filing — the agent reports the file as searchable, binds a
    # discussion to a document that no longer exists, and gets nothing back.
    # Unreachable gateway is treated as "still linked": a failed lookup is not
    # evidence the row is gone, and a duplicate index is the cheaper mistake.
    linked = getattr(meta, "document_id", "")
    if linked:
        import gateway
        if gateway.document_exists(linked) is not False:
            return linked
        link_document(artifact_id, "")
    try:
        blob = get_bytes(artifact_id)
    except Exception:
        return ""
    if not blob:
        return ""
    name = (filename or meta.descriptor or artifact_id.removeprefix("art:")
            ).strip().replace("/", "-")[:120] or "artifact"
    if "." not in name:
        ext = {"application/pdf": ".pdf", "application/vnd.openxmlformats-"
               "officedocument.presentationml.presentation": ".pptx"}.get(
            meta.content_type, "")
        name = name + (ext or ".txt")
    fields = {"origin": origin or "artifact",
              "origin_ref": origin_ref or artifact_id,
              "folder_name": folder_name or "Run outputs"}
    if session_id:
        fields["session_id"] = session_id
    if title:
        fields["title"] = title[:200]
    out = gateway.post_multipart(
        "/v1/documents",
        files={"file": (name, blob, meta.content_type or "application/octet-stream")},
        data=fields)
    doc = (out or {}).get("document") or {}
    did = str(doc.get("id") or "")
    if did:
        link_document(artifact_id, did, origin=origin, session_id=session_id)
    return did


def get_bytes(artifact_id: str) -> bytes:
    return (STORE / f"{_digest_for(artifact_id)}.bin").read_bytes()


def get_meta(artifact_id: str) -> Artifact:
    digest = _digest_for(artifact_id)
    raw = json.loads((STORE / f"{digest}.json").read_text())
    return Artifact.model_validate(raw)


# The console reads a delivered file to describe it honestly — page text, word
# count, thin-document warnings — because the run's own metadata was repeatedly
# wrong. That read is not cheap: extracting the text of all 40 pages of a report
# measured 0.45-0.80 s, and it ran again on every single open.
#
# An artifact id is content-addressed, so its description can never go stale:
# the same id always means the same bytes. That makes it safe to keep the
# computed result beside the file instead of recomputing it, which is the whole
# reason this store can cache at all.

def get_cache(artifact_id: str, name: str):
    """A cached derivation of this artifact, or None if never computed.

    `name` namespaces the kind of derivation — "preview" for the text
    description, "raster" for the server-drawn page images — because the same
    immutable bytes can be expensive to describe in more than one way.
    """
    try:
        return json.loads((STORE / f"{_digest_for(artifact_id)}.{name}.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def put_cache(artifact_id: str, name: str, payload: dict) -> None:
    """Store a derivation. A cache that cannot write must not fail the
    request — the caller can always recompute it."""
    try:
        _atomic_write(STORE / f"{_digest_for(artifact_id)}.{name}.json",
                      json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except OSError:
        pass


def exists(artifact_id: str) -> bool:
    try:
        digest = _digest_for(artifact_id)
    except ValueError:
        return False
    return (STORE / f"{digest}.bin").exists()
