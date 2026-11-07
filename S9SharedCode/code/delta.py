"""The change delta: what has happened since a given moment.

Two consumers, one implementation.

* The **main agent** calls it as the `whats_new` tool — in an ordinary turn
  ("what changed since this morning?") and after waking from a self-scheduled
  watch, where it is the whole reason a wake is cheap: the agent reads a bounded
  change index instead of re-reading the world to find out whether anything
  moved.
* The **wake predicate** (§4 of SELF_WAKE_DELTA_DESIGN.md) uses the same
  collectors without a model at all, so a tick that matches nothing spends
  nothing.

The shape is deliberate: a *pointer plus a one-line description per change*,
never content. Every entry says where to look, and the agent's own read tools
fetch what it decides matters. That is what keeps a delta of a busy day at a
few hundred tokens instead of a re-read of the corpus.

What this module must never do
-----------------------------
Read the gateway's memory files directly. `state/memory/audit/memory.json` is
138 MB and `state/memory/document/memory.json` is 91 MB, and the store rewrites
the WHOLE drawer on every mutation — the mechanism that made a document search
take 35 seconds. Both are reached only through the gateway's own read APIs,
which serve bounded, newest-first pages, and `audit` is not requested at all.
The `document` drawer is not requested either: it holds one record per *chunk*
of every upload (4,824 of them), so an upload would otherwise read as 139
separate memory changes and bury the two facts that were actually added.

Everything here is stat-first. A file whose `mtime_ns` and size are unchanged
since the last pass is not opened, which is what makes calling this every few
minutes free rather than merely fast.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("S9_STATE_DIR") or (ROOT / "state"))
CURSOR_PATH = STATE_DIR / "wake_cursors.json"
# Sidecar for the write lock. Deliberately not inside CURSOR_PATH's own fd: an
# `os.replace` of the data file swaps the inode out from under a held lock, so
# the lock has to live on a file that is never replaced.
LOCK_PATH = STATE_DIR / "wake_cursors.lock"
# How long a cursor write waits for another process before giving up and
# writing anyway. See _FileLock: contention here is rare and the cost of being
# wrong is a re-shown delta, so the budget is "briefly".
LOCK_WAIT_S = 2.0

SURFACES = ("documents", "memory", "runs", "apps", "artifacts")
# Entries per surface before the rest are counted rather than listed. Reported
# in `truncated`, so "41 new, showing 25" survives as a fact the agent can act
# on instead of becoming a silent cut.
PER_SURFACE = 25
DETAIL_MAX = 120
# The drawers that hold something a user would call a change. See the module
# docstring for why `document` and `audit` are absent.
MEMORY_DRAWERS = ("fact", "profile")

_CURSOR_LOCK = threading.Lock()


class _FileLock:
    """Advisory cross-process lock on the cursor sidecar, `LOCK_PATH`.

    `advance_cursor` is a read-modify-write of the whole map, and its writer is
    the MCP tool child — a different process from the agent server and from the
    child that ran the previous turn. Without a lock, two children advancing two
    different conversations can have the second `os.replace` publish a map built
    from a stale read and drop the first row.

    Acquisition waits up to `LOCK_WAIT_S` and then writes unlocked anyway. The
    alternative — raising — turns a delivered answer into a tool error, and the
    race it protects against is benign: a lost watermark makes the next delta
    come back full-window, never empty. So the lock is an optimisation and must
    never become a failure mode.
    """

    def __init__(self):
        self._fh = None
        self.held = False

    def __enter__(self):
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = open(LOCK_PATH, "a+", encoding="utf-8")
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                _byte_trylock(fh)
                self.held = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        self._fh = fh
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            if self.held:
                try:
                    _byte_unlock(self._fh)
                except OSError:
                    pass
            self._fh.close()
            self._fh = None
        return False


def _byte_trylock(fh) -> None:
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _byte_unlock(fh) -> None:
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

# path -> (mtime_ns, size, parsed). Bounded and shared by every collector, so
# ten calls a day over an unchanged store parse it once. Keyed on the tuple
# every writer changes, which is the same rule the memory store uses.
_STAT_CACHE: dict[str, tuple[int, int, Any]] = {}
_STAT_CACHE_MAX = 64


# ── small helpers ─────────────────────────────────────────────────────────
def _now() -> float:
    return time.time()


def parse_cursor(value: Any) -> float:
    """Accept `v1:<epoch>`, a bare epoch, an ISO string, or None (meaning
    "everything"). Unparseable input is 0.0, not an error: a lost cursor should
    make the next delta large, not break the tool."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if s.startswith("v1:"):
        s = s[3:]
    try:
        return float(s)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return 0.0


def format_cursor(ts: float) -> str:
    return f"v1:{ts:.3f}"


def _epoch(value: Any) -> float:
    """Timestamps arrive as epoch numbers, ISO strings, and (from the memory
    models) `datetime` objects that are naive-but-UTC. One reader for all
    three, because mixing them is how a delta quietly reports nothing."""
    if value is None:
        return 0.0
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    try:
        return float(s)
    except ValueError:
        pass
    try:
        return _epoch(datetime.fromisoformat(s.replace("Z", "+00:00")))
    except ValueError:
        return 0.0


def _detail(text: Any) -> str:
    s = re.sub(r"\s+", " ", str(text if text is not None else "")).strip()
    return s[:DETAIL_MAX] + ("…" if len(s) > DETAIL_MAX else "")


def _load_json(path: Path) -> Any:
    """Parsed file contents, re-parsed only when the file actually changed."""
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size)
    hit = _STAT_CACHE.get(key)
    if hit and hit[:2] == sig:
        return hit[2]
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = None
    if len(_STAT_CACHE) >= _STAT_CACHE_MAX:
        # Oldest-inserted first; the map is small enough that order is enough
        # of a proxy for recency, and the cost of being wrong is a re-parse.
        _STAT_CACHE.pop(next(iter(_STAT_CACHE)), None)
    _STAT_CACHE[key] = (sig[0], sig[1], data)
    return data


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _bounded(items: list[dict], limit: int) -> tuple[list[dict], int]:
    """The page plus how many were dropped. `truncated` in the payload is
    reported from here so a cut is always visible."""
    if len(items) <= limit:
        return items, 0
    return items[:limit], len(items) - limit


def _newest(entries: list[dict]) -> float:
    return max((float(e.get("at") or 0.0) for e in entries), default=0.0)


# ── collectors ────────────────────────────────────────────────────────────
# Each returns (entries, dropped, error). A surface that fails is named in the
# payload and costs the rest of the delta nothing: "apps: unreachable" beats no
# delta at all, and beats a silent gap.

def _documents(since: float, limit: int) -> tuple[list[dict], int, str]:
    try:
        import memory as _mem
        data = _mem._get("/v1/documents") or {}
    except Exception as e:
        return [], 0, f"documents unavailable ({type(e).__name__})"
    rows = data.get("documents") if isinstance(data, dict) else data
    out: list[dict] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        created = _epoch(r.get("created_at"))
        updated = _epoch(r.get("updated_at")) or created
        if updated <= since:
            continue
        status = str(r.get("status") or "unknown")
        chunks = int(r.get("chunk_count") or 0)
        if status not in ("ready", "enabled"):
            what = "failed" if status in ("failed", "error") else status
        elif created > since:
            what = "added"
        elif chunks:
            what = "re-indexed"
        else:
            what = "changed"
        out.append({
            "id": r.get("id"), "what": what, "at": updated,
            "filename": r.get("filename"),
            "detail": _detail(f"{chunks} chunks, {status}"
                              + (f", error: {r.get('error')}" if r.get("error") else "")),
        })
    out.sort(key=lambda e: -e["at"])
    page, dropped = _bounded(out, limit)
    return page, dropped, ""


def _memory(since: float, limit: int) -> tuple[list[dict], int, str]:
    try:
        import memory as _mem
        items = _mem.list_recent(limit=limit * 4, drawers=list(MEMORY_DRAWERS),
                                 hide_superseded=False)
    except Exception as e:
        return [], 0, f"memory unavailable ({type(e).__name__})"
    out: list[dict] = []
    for it in items or []:
        at = _epoch(getattr(it, "created_at", None))
        if at <= since:
            continue
        superseded = bool(getattr(it, "superseded_by", None))
        kind = str(getattr(it, "kind", "") or "")
        out.append({
            "id": getattr(it, "id", None),
            "drawer": getattr(it, "drawer", None),
            # A corrected record shows as "superseded", not "added": the new
            # fact is the change, and the old row going quiet is the same news.
            "what": ("superseded" if superseded else
                     ("preference" if kind == "preference" else
                      "observation" if kind == "observation" else "added")),
            "at": at,
            "detail": _detail(getattr(it, "descriptor", "")),
        })
    out.sort(key=lambda e: -e["at"])
    page, dropped = _bounded(out, limit)
    return page, dropped, ""


def _runs(since: float, limit: int) -> tuple[list[dict], int, str]:
    try:
        from persistence import SESSIONS_ROOT
    except Exception as e:
        return [], 0, f"session store unavailable ({type(e).__name__})"
    out: list[dict] = []
    try:
        dirs = sorted((p for p in SESSIONS_ROOT.iterdir() if p.is_dir()),
                      key=lambda p: -_mtime(p))
    except OSError as e:
        return [], 0, f"sessions unreadable ({e.__class__.__name__})"
    for d in dirs:
        # Stat first: a session that has not moved since the cursor is skipped
        # without opening anything, which is the whole reason this is cheap.
        if _mtime(d) <= since:
            continue
        status = _load_json(d / "run_status.json") or {}
        nodes = 0
        try:
            nodes = sum(1 for p in (d / "nodes").iterdir() if p.suffix == ".json")
        except OSError:
            pass
        detail = str(status.get("detail") or "")
        out.append({
            "id": d.name,
            "what": "degraded" if status.get("degraded") else "updated",
            "at": max(_mtime(d), _epoch(status.get("updated"))),
            "detail": _detail(f"{nodes} nodes" + (f", {detail}" if detail else "")),
        })
    out.sort(key=lambda e: -e["at"])
    page, dropped = _bounded(out, limit)
    return page, dropped, ""


def _apps(since: float, limit: int) -> tuple[list[dict], int, str]:
    out: list[dict] = []
    try:
        specs = sorted((STATE_DIR / "apps").glob("*.json"), key=lambda p: -_mtime(p))
    except OSError as e:
        return [], 0, f"apps unreadable ({e.__class__.__name__})"
    for spec_path in specs:
        name = spec_path.name
        if name.endswith(".data.json") or name == "index.json":
            continue
        app_id = name[:-len(".json")]
        data_path = STATE_DIR / "apps" / f"{app_id}.data.json"
        at = max(_mtime(spec_path), _mtime(data_path))
        if at <= since:
            continue
        data = _load_json(data_path) or {}
        added = data.get("added") or []
        removed = data.get("removed") or []
        what = ("error" if data.get("error") else
                "items added" if added else
                "items removed" if removed else "changed")
        bits = [f"{int(data.get('count') or 0)} items"]
        if added:
            bits.append(f"+{len(added)}")
        if removed:
            bits.append(f"-{len(removed) if isinstance(removed, list) else removed}")
        if data.get("error"):
            bits.append(f"error: {data['error']}")
        out.append({"id": app_id, "what": what, "at": at,
                    "detail": _detail(", ".join(bits))})
    out.sort(key=lambda e: -e["at"])
    page, dropped = _bounded(out, limit)
    return page, dropped, ""


_ART_PRIMARY = re.compile(r"^[0-9a-f]{16}\.json$")


def _artifacts(since: float, limit: int) -> tuple[list[dict], int, str]:
    """Produced deliverables.

    Only the primary sidecar (`<sha16>.json`) counts as an artifact. The same
    directory also holds derived caches — `<sha>.raster.<key>.json`,
    `<sha>.<name>.json` — written by the preview paths; treating those as
    artifacts would make *looking at a document* look like a change to the
    world, which is the opposite of a delta.
    """
    out: list[dict] = []
    try:
        files = [p for p in (STATE_DIR / "artifacts").iterdir()
                 if _ART_PRIMARY.match(p.name)]
    except OSError as e:
        return [], 0, f"artifacts unreadable ({e.__class__.__name__})"
    for p in sorted(files, key=lambda p: -_mtime(p)):
        at = _mtime(p)
        if at <= since:
            continue
        meta = _load_json(p) or {}
        # `descriptor` is what the artifact is ABOUT ("Project Brief (pdf)", the
        # opening line of the brief), `content_type` and `origin` where it came
        # from. The sha alone tells the agent nothing it can act on, and it
        # already appears in `id`.
        bits = [str(meta.get("descriptor") or "").strip(),
                str(meta.get("content_type") or "").strip(),
                f"{meta.get('size_bytes')} B" if meta.get("size_bytes") else "",
                f"via {meta.get('origin')}" if meta.get("origin") else ""]
        size = int(meta.get("size_bytes") or 0)
        out.append({
            "id": meta.get("id") or f"art:{p.stem}", "what": "produced",
            "at": at, "origin": meta.get("origin"),
            "session_id": meta.get("session_id"),
            "detail": _detail(", ".join(b for b in bits if b) or p.stem)
                      if size or meta.get("descriptor") else _detail(p.stem),
        })
    out.sort(key=lambda e: -e["at"])
    page, dropped = _bounded(out, limit)
    return page, dropped, ""


COLLECTORS: dict[str, Callable[[float, int], tuple[list[dict], int, str]]] = {
    "documents": _documents,
    "memory": _memory,
    "runs": _runs,
    "apps": _apps,
    "artifacts": _artifacts,
}


# ── the delta ─────────────────────────────────────────────────────────────
def compute(since: Any = None, surfaces: tuple[str, ...] | list[str] | None = None,
            limit: int = PER_SURFACE, until: float | None = None) -> dict[str, Any]:
    """The change index.

    `since` is a cursor from a previous call, a `conversation_id`'s stored
    cursor, an epoch, or None. `until` is pinned BEFORE the collectors run and
    reported as the next cursor: using `time.time()` afterwards would skip
    anything that landed during the pass, and a wake that silently misses a
    change is worse than one that sees it twice.
    """
    start = parse_cursor(since)
    end = float(until if until is not None else _now())
    wanted = [s for s in (surfaces or SURFACES) if s in COLLECTORS]
    changed: dict[str, list[dict]] = {}
    counts: dict[str, int] = {}
    truncated: dict[str, int] = {}
    errors: dict[str, str] = {}
    for name in wanted:
        try:
            entries, dropped, err = COLLECTORS[name](start, limit)
        except Exception as e:      # a collector must not take the delta down
            entries, dropped, err = [], 0, f"{type(e).__name__}: {str(e)[:120]}"
        changed[name] = entries
        counts[name] = len(entries) + dropped
        if dropped:
            truncated[name] = dropped
        if err:
            errors[name] = err
    # An outage is not an empty world. With `errors` present the answer is "I
    # could not look", and a wake that concluded "nothing happened" from a dead
    # gateway would re-arm, find nothing again, and go quiet forever — silently.
    empty = not any(changed.values()) and not errors
    return {
        "since": start,
        "until": end,
        "cursor": format_cursor(end),
        "changed": changed,
        "counts": counts,
        # Kept separate from `counts` rather than folded into it: a caller that
        # reads only counts still learns that something was left out.
        **({"truncated": truncated} if truncated else {}),
        **({"errors": errors} if errors else {}),
        "empty": empty,
        "summary": _summary(counts, errors),
    }


def _summary(counts: dict[str, int], errors: dict[str, str]) -> str:
    """One line, because the wake predicate and the console header both want a
    human-readable answer without re-deriving one from `counts`."""
    parts = [f"{n} {name}" for name, n in counts.items() if n]
    if not parts:
        return "nothing has changed" + (f" ({', '.join(sorted(errors))} unavailable)"
                                        if errors else "")
    line = ", ".join(parts)
    if errors:
        line += f"; unavailable: {', '.join(sorted(errors))}"
    return line


# ── cursors ───────────────────────────────────────────────────────────────
def _load_cursors() -> dict[str, Any]:
    data = _load_json(CURSOR_PATH)
    return data if isinstance(data, dict) else {}


def read_cursor(conversation_id: str) -> float:
    row = _load_cursors().get(conversation_id)
    return parse_cursor(row.get("cursor") if isinstance(row, dict) else row)


def advance_cursor(conversation_id: str, cursor: Any) -> dict[str, Any]:
    """Record how far this conversation has seen.

    Atomic, and called only AFTER a wake's turn has been delivered. Advancing
    before delivery means a crash loses the changes permanently — the next wake
    would look at an empty world and conclude nothing happened.
    """
    ts = parse_cursor(cursor)
    with _CURSOR_LOCK, _FileLock():
        all_rows = _load_cursors()
        prev = all_rows.get(conversation_id) or {}
        row = {**prev,
               "cursor": format_cursor(ts),
               "updated": _now(),
               "wakes": int(prev.get("wakes") or 0) + 1,
               "last_wake": ts}
        all_rows[conversation_id] = row
        _write_json(CURSOR_PATH, all_rows)
    return row


def forget_cursor(conversation_id: str) -> bool:
    with _CURSOR_LOCK, _FileLock():
        all_rows = _load_cursors()
        if conversation_id not in all_rows:
            return False
        all_rows.pop(conversation_id, None)
        _write_json(CURSOR_PATH, all_rows)
    return True


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{id(data)}")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
