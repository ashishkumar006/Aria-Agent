"""FAISS-backed vector index for the gateway memory service.

Wraps ``faiss.IndexFlatIP`` (inner product on L2-normalized vectors, which
equals cosine similarity) with a parallel ``list[str]`` of ``MemoryItem``
ids. FAISS stores vectors by integer position in the order they were
added; the ids list maps integer position back to the application-level
string id.

Persists to two files under the state dir::

    state/index.faiss      the binary FAISS index
    state/index_ids.json   the parallel ids list

This is the canonical copy (moved here from the agent's ``vector_index.py``,
which is retained agent-side only for its static math checks â€” production
traffic never touches it). The index dimension is pinned to the gateway's
embedding model: an on-disk index built by a different-dim model is
cleared with a loud warning rather than silently producing garbage, and
any in-memory add with a mismatched dim raises ``ValueError``.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

import numpy as np

try:
    import faiss  # type: ignore[import-untyped]
except ImportError:
    # Degrade, don't die: importing this module must never kill the gateway.
    # The service runs keyword-only when faiss is unavailable and says so
    # loudly at startup; VectorIndex raises a catchable RuntimeError only if
    # actually instantiated without faiss.
    faiss = None  # type: ignore[assignment]


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    """L2-normalize a 1D vector. After normalization, inner product equals
    cosine similarity."""
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        return vec
    return vec / norm


class VectorIndex:
    """In-memory FAISS index with disk persistence."""

    def __init__(self, store_dir: str | Path, expected_dim: int | None = None):
        if faiss is None:
            raise RuntimeError(
                "faiss-cpu is not installed; vector search is unavailable "
                "(keyword fallback still works). Run: uv add faiss-cpu"
            )
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.store_dir / "index.faiss"
        self.ids_path = self.store_dir / "index_ids.json"
        self._index: faiss.IndexFlatIP | None = None
        self._ids: list[str] = []
        self._dim: int | None = None
        self._load()
        if (expected_dim is not None and self._dim is not None
                and self._dim != expected_dim):
            # Structural guarantee: an index built by a different embedding
            # model would rank garbage. Clear it LOUDLY rather than serve
            # wrong results or crash on every add.
            print(
                f"[memory] index dim {self._dim} != embedder dim {expected_dim} "
                f"(embedding model changed?) â€” clearing stale index at "
                f"{self.index_path} and rebuilding from stored items"
            )
            self.clear()

    # â”€â”€ persistence â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _quarantine(self, reason: str) -> None:
        """Move a damaged index pair aside so it cannot be served again.

        Both files are moved together: recovery from JSON is possible from
        ``memory.json`` (every record carries its embedding), so keeping a
        broken FAISS file around only risks reading it after a later
        half-write.
        """
        stamp = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
        for path in (self.index_path, self.ids_path):
            if not path.exists():
                continue
            target = path.with_name(f"{path.name}.corrupt-{stamp}")
            try:
                os.replace(path, target)
            except OSError:
                try:
                    path.unlink()
                except OSError:
                    pass
        print(f"[memory] vector index at {self.store_dir} is unreadable "
              f"({reason}) - quarantined and rebuilding from stored items")

    def _load(self) -> None:
        index_exists = self.index_path.exists()
        ids_exists = self.ids_path.exists()
        if not index_exists and not ids_exists:
            return
        if not index_exists or not ids_exists:
            self._quarantine("incomplete index pair")
            return
        try:
            index = faiss.read_index(str(self.index_path))
            ids = json.loads(self.ids_path.read_text(encoding="utf-8"))
            if not isinstance(ids, list) or any(
                    not isinstance(i, str) for i in ids):
                raise ValueError("index_ids.json is not a list of strings")
            if len(ids) != index.ntotal:
                raise ValueError(
                    f"index has {index.ntotal} vectors but "
                    f"{len(ids)} ids")
            self._index = index
            self._ids = ids
            self._dim = index.d
        except Exception as e:
            self._quarantine(f"{type(e).__name__}: {e}")

    def persist(self) -> None:
        """Write both files atomically.

        A direct ``write_index`` followed by ``write_text`` is two truncating
        writes: a crash, a full disk or a power loss between them leaves a
        zero-filled pair that used to disable vector search for that drawer
        until someone deleted the files by hand. Temp files + ``os.replace``
        keep every observable state either fully old or fully new.
        """
        if self._index is None:
            return
        suffix = (f"tmp-{os.getpid()}-{threading.get_ident()}-"
                  f"{uuid.uuid4().hex[:8]}")
        tmp_index = self.index_path.with_name(f"{self.index_path.name}.{suffix}")
        tmp_ids = self.ids_path.with_name(f"{self.ids_path.name}.{suffix}")
        try:
            faiss.write_index(self._index, str(tmp_index))
            tmp_ids.write_text(json.dumps(self._ids), encoding="utf-8")
            os.replace(tmp_index, self.index_path)
            os.replace(tmp_ids, self.ids_path)
        finally:
            for path in (tmp_index, tmp_ids):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def clear(self) -> None:
        self._index = None
        self._ids = []
        self._dim = None
        if self.index_path.exists():
            self.index_path.unlink()
        if self.ids_path.exists():
            self.ids_path.unlink()

    # â”€â”€ mutation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def add(self, item_id: str, embedding: list[float]) -> None:
        vec = _l2_normalize(np.array(embedding, dtype=np.float32))
        if self._index is None:
            self._dim = vec.shape[0]
            self._index = faiss.IndexFlatIP(self._dim)
        elif vec.shape[0] != self._dim:
            raise ValueError(
                f"Embedding dim {vec.shape[0]} does not match index dim {self._dim}. "
                "The embedding model must stay fixed for the lifetime of an index."
            )
        self._index.add(vec.reshape(1, -1))
        self._ids.append(item_id)

    # â”€â”€ query â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def search(self, query_embedding: list[float], k: int = 5) -> list[tuple[str, float]]:
        """Return up to k ``(item_id, similarity)`` pairs, ranked by similarity."""
        if self._index is None or self._index.ntotal == 0:
            return []
        vec = _l2_normalize(np.array(query_embedding, dtype=np.float32))
        if vec.shape[0] != self._dim:
            raise ValueError(
                f"Query dim {vec.shape[0]} does not match index dim {self._dim}."
            )
        scores, idxs = self._index.search(vec.reshape(1, -1), min(k, self._index.ntotal))
        out: list[tuple[str, float]] = []
        for score, idx in zip(scores[0].tolist(), idxs[0].tolist()):
            if idx < 0:
                continue
            out.append((self._ids[idx], float(score)))
        return out

    @property
    def size(self) -> int:
        return self._index.ntotal if self._index is not None else 0

    @property
    def dim(self) -> int | None:
        return self._dim

