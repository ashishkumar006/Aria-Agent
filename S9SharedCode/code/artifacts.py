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
    meta = Artifact(
        id=art_id,
        content_type=content_type,
        size_bytes=len(blob),
        source=source,
        descriptor=descriptor,
    )
    _atomic_write(meta_path, meta.model_dump_json(indent=2).encode("utf-8"))
    return art_id


def get_bytes(artifact_id: str) -> bytes:
    return (STORE / f"{_digest_for(artifact_id)}.bin").read_bytes()


def get_meta(artifact_id: str) -> Artifact:
    digest = _digest_for(artifact_id)
    raw = json.loads((STORE / f"{digest}.json").read_text())
    return Artifact.model_validate(raw)


def exists(artifact_id: str) -> bool:
    try:
        digest = _digest_for(artifact_id)
    except ValueError:
        return False
    return (STORE / f"{digest}.bin").exists()
