"""The review model Studio slice 4 sits on: hunks that have an identity, a
baseline that survives a "keep", and checkpoints that survive a restart.

Why this is a module and not more code in `agent_server.py`
-----------------------------------------------------------
The review store started as a dict of `{client_path: snapshot}` and grew three
routes around it. Two things were wrong with it that only show up once hunks
are *clickable*:

* A hunk was addressed by its POSITION in the diff. That is stable only while
  nothing else changes, so "undo hunk 2" after another hunk was kept or
  reverted silently targets a different region of the file. Here a hunk is
  addressed by a fingerprint of its own content, and a mismatch is refused
  with `hunk-moved` rather than guessed at.
* Keeping one hunk had no meaning. There was one baseline (the bytes from
  before the FIRST write) and one diff, so accepting part of a change either
  threw the rest away or left it pending against a baseline that no longer
  described the file. The baseline is now a real, mutable state: keeping a
  hunk folds exactly that hunk into it, so the pending diff always shows what
  is still unreviewed and whole-file Revert still lands somewhere honest.

Persistence is the second reason. `_CODE_REVIEW` held the only copy of the
bytes that Revert restores, so a server restart turned "unreviewed change"
into "unreviewable change" — the marks stayed in the UI and every Revert 404'd.
Snapshots and diffs now live as content-addressed blobs under
`state/code-studio/`, keyed by session, and checkpoints give the same storage
a name and a turn.

Confinement is deliberately NOT reimplemented here. Every path is resolved by
the `abs_for` callback the caller supplies (`agent_server._code_agent_abs`),
which is the same session-aware, allow-list-checked function the human's own
routes use. Two confinement mechanisms is how one gets forgotten.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

import studio_workspace as _ws

# Same ceiling the in-memory dict had, so nothing about a live session's
# capacity changes. Entries are keyed by (session, path) now, which means the
# budget is per session rather than shared-and-stolen-by-whichever-tab-wrote-
# last — the older behaviour quietly dropped one project's snapshots because a
# second project filled the dict.
MAX_ENTRIES = 200

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# bucket (a session id, or "_default" for callers that name no session)
# -> {client_path: entry}. Entries are lazily hydrated from disk on first
# touch so a restart costs nothing until someone opens the Changes panel.
_BUCKETS: dict[str, dict[str, dict]] = {}
_LOADED: set[str] = set()
_SEQ = [0]
_LOCK = threading.RLock()

# Routes call these from `asyncio.to_thread`, so two reviews of the same file
# can genuinely run at once. Every mutation takes the lock; readers that only
# touch in-memory fields do not need it.


# ── storage layout ──────────────────────────────────────────────────────
def _bucket(sid: str) -> str:
    """The filesystem-safe name for a session's store."""
    clean = (sid or "").strip()
    return clean if _ws.valid_sid(clean) else "_default"


def review_dir() -> Path:
    d = _ws.state_dir() / "review"
    d.mkdir(parents=True, exist_ok=True)
    return d


def checkpoint_dir() -> Path:
    d = _ws.state_dir() / "checkpoints"
    d.mkdir(parents=True, exist_ok=True)
    return d


def blob_dir(bucket: str) -> Path:
    d = _ws.state_dir() / "blobs" / bucket
    d.mkdir(parents=True, exist_ok=True)
    return d


def _review_file(bucket: str) -> Path:
    return review_dir() / f"{bucket}.json"


def _checkpoint_file(bucket: str) -> Path:
    return checkpoint_dir() / f"{bucket}.json"


def _blob_name(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:32]


def sha_of(data: bytes) -> str:
    """The fingerprint a checkpoint keeps for a file it expects to remove.

    Public because the caller that performs a rename records the destination's
    fingerprint, and two spellings of one hash is how a guard silently stops
    matching."""
    return hashlib.sha256(data).hexdigest()[:32]


def _put_blob(bucket: str, data: bytes) -> str:
    """Content-addressed write; returns the name. Identical bytes across two
    turns or two checkpoints cost the disk one copy."""
    name = _blob_name(data)
    path = blob_dir(bucket) / name
    if path.exists():
        return name
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return name


def _get_blob(bucket: str, name: str) -> bytes | None:
    if not name:
        return None
    try:
        return (blob_dir(bucket) / name).read_bytes()
    except OSError:
        return None


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        got = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


# ── hunks ───────────────────────────────────────────────────────────────
def hunks(diff: str) -> list[dict]:
    """Split a unified diff into addressable hunks.

    `old` is context + removed, `new` is context + added; keeping them as line
    lists is what lets one hunk be applied or reversed on its own by content.
    The `id` is a fingerprint of (removed+context, added+context, occurrence) —
    NOT the line numbers, because line numbers move the moment an earlier hunk
    is kept or reverted and an id that moves is a lie about identity.
    """
    raw: list[dict] = []
    cur: dict | None = None
    for line in (diff or "").splitlines():
        if line.startswith("@@"):
            if cur is not None:
                raw.append(cur)
            cur = {"header": line[:160], "old": [], "new": [],
                   "plus": 0, "minus": 0}
            continue
        if cur is None or line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("+"):
            cur["new"].append(line[1:])
            cur["plus"] += 1
        elif line.startswith("-"):
            cur["old"].append(line[1:])
            cur["minus"] += 1
        elif line.startswith("\\"):          # "\ No newline at end of file"
            continue
        elif line.startswith(" "):
            cur["old"].append(line[1:])
            cur["new"].append(line[1:])
        else:                                 # bare context line
            cur["old"].append(line)
            cur["new"].append(line)
    if cur is not None:
        raw.append(cur)

    seen: dict[str, int] = {}
    out: list[dict] = []
    for i, h in enumerate(raw):
        key = "\x1f".join(("\n".join(h["old"]), "\n".join(h["new"])))
        occ = seen.get(key, 0)
        seen[key] = occ + 1
        m = _HUNK_RE.match(h["header"])
        old_start = int(m.group(1)) if m else 1
        # A one-line hunk writes `@@ -4 +4,2 @@` — the count is optional.
        old_lines = int(m.group(2)) if (m and m.group(2) is not None) else 1
        new_start = int(m.group(3)) if m else 1
        new_lines = int(m.group(4)) if (m and m.group(4) is not None) else 1
        out.append({
            "id": "h" + hashlib.sha1(f"{key}\x1f{occ}".encode("utf-8")).hexdigest()[:12],
            "index": i,
            "header": h["header"],
            "old_start": old_start, "old_lines": old_lines,
            "new_start": new_start, "new_lines": new_lines,
            "added": h["plus"], "removed": h["minus"],
            "old": h["old"], "new": h["new"],
        })
    return out


def find_block(lines: list[str], block: list[str]) -> int | None:
    """Index of the first exact run of `block` in `lines`, or None."""
    n = len(block)
    if not n or len(lines) < n:
        return None
    for i in range(len(lines) - n + 1):
        if lines[i:i + n] == block:
            return i
    return None


def apply_block(lines: list[str], h: dict, direction: str) -> tuple[list[str] | None, str]:
    """Move one hunk between the two sides, against `lines` as it stands.

    `direction` "revert" puts the removed text back (search for the NEW block,
    replace with the OLD one); "keep" accepts the change into a baseline
    (search for the OLD block, replace with the NEW one). Either way the search
    is by content: if the block is not there the honest answer is an error, not
    a re-anchoured guess that mangles the file.
    """
    want, put = (h["new"], h["old"]) if direction == "revert" else (h["old"], h["new"])
    if want:
        at = find_block(lines, want)
        if at is None:
            return None, ("this hunk is not in the file any more - it was "
                          "already applied, or the text around it has moved")
        out = list(lines)
        out[at:at + len(want)] = put
        return out, ""
    # One-sided hunk: a pure insertion has nothing to find on the old side, a
    # pure deletion nothing on the new. The header's own start line is the only
    # position on offer, and it is fresh because every caller re-diffs after
    # each single-hunk change.
    hint = h["new_start"] if direction == "revert" else h["old_start"]
    at = max(0, int(hint) - 1)
    out = list(lines)
    out[at:at] = put
    return out, ""


# ── the store ───────────────────────────────────────────────────────────
def _hydrate(entry: dict) -> None:
    """Read this entry's snapshot/diff blobs into it, once."""
    bucket = entry.get("_bucket") or "_default"
    if "baseline" not in entry:
        name = entry.get("baseline_blob") or ""
        entry["baseline"] = _get_blob(bucket, name) if name else None
        entry["_had_blob"] = bool(name)
    if "diff" not in entry:
        entry["diff"] = (_get_blob(bucket, entry.get("diff_blob") or "")
                         or b"").decode("utf-8", "replace")


def _meta(entry: dict) -> dict:
    """The JSON-safe half of an entry — what survives a restart."""
    return {k: entry.get(k) for k in (
        "token", "path", "existed", "baseline_blob", "diff_blob", "tool",
        "summary", "when", "turn", "checkpoint", "bytes")}


def _load_bucket(bucket: str) -> dict[str, dict]:
    if bucket in _LOADED:
        return _BUCKETS.setdefault(bucket, {})
    got = _read_json(_review_file(bucket))
    table: dict[str, dict] = {}
    for m in (got.get("entries") or []):
        if not isinstance(m, dict) or not m.get("path"):
            continue
        entry = dict(m)
        entry["_bucket"] = bucket
        _hydrate(entry)
        table[str(entry["path"])] = entry
    _SEQ[0] = max([int(entry.get("token") or 0) for entry in table.values()]
                  + [int(got.get("seq") or 0), _SEQ[0]])
    _BUCKETS[bucket] = table
    _LOADED.add(bucket)
    return table


def _persist(bucket: str) -> None:
    table = _BUCKETS.get(bucket, {})
    _write_json(_review_file(bucket), {
        "bucket": bucket, "updated": time.time(), "seq": _SEQ[0],
        "entries": [_meta(e) for e in table.values()]})


def _touch_diff(entry: dict, baseline_text: str, current_text: str,
                path: str) -> None:
    """Re-diff the entry against its baseline so it describes what is STILL
    unreviewed. Left as it was, the panel kept offering Keep for a hunk that
    had already been folded in, and the next apply was indexed against a hunk
    list that no longer matched the file."""
    entry["diff"] = "\n".join(difflib.unified_diff(
        baseline_text.splitlines(), current_text.splitlines(),
        fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="", n=3))
    entry["when"] = time.time()
    bucket = entry.get("_bucket") or "_default"
    entry["diff_blob"] = _put_blob(bucket, entry["diff"].encode("utf-8"))


def _store_baseline(entry: dict, text: str | None) -> None:
    """Write the baseline as a blob and point the entry at it."""
    bucket = entry.get("_bucket") or "_default"
    if text is None:
        entry["baseline"] = None
        entry["baseline_blob"] = ""
        return
    data = text.encode("utf-8")
    entry["baseline"] = data
    entry["baseline_blob"] = _put_blob(bucket, data)
    entry["bytes"] = len(data)


def read_file_text(target: Path) -> tuple[str, str]:
    try:
        data = target.read_bytes()
    except OSError as exc:
        return "", f"cannot be read: {exc}"
    try:
        return data.decode("utf-8"), ""
    except UnicodeDecodeError:
        return "", "is not UTF-8 text, so it cannot be reviewed hunk by hunk"


def split_lines(text: str) -> tuple[list[str], str]:
    """(the lines, the joiner that writes them back).

    A unified diff carries no line endings — difflib compares `splitlines()` —
    so the blocks this module searches for are bare. On a CRLF file, splitting
    on "\\n" alone leaves a `\\r` glued to every line and NOT ONE hunk ever
    matches, which would make Windows checkouts unreviewable. So the endings
    come off for the comparison and the file's own dominant ending goes back on
    when the text is written. A file that mixes both is answered by whichever
    appears first; there is no honest third option in a line-based model."""
    joiner = "\r\n" if "\r\n" in text else "\n"
    if not text:
        # Not `[""]`: a hunk applied to that gains a phantom trailing line, and
        # the two places it bites are a file the change created and a file a
        # revert empties.
        return [], joiner
    return [(l[:-1] if l.endswith("\r") else l) for l in text.split("\n")], joiner


def take(sid: str, client_path: str, tool: str, summary: str, diff: str,
         abs_for, turn: str = "") -> int | None:
    """Snapshot `client_path` before a write; return this attempt's token.

    The FIRST snapshot for a path wins, so Revert undoes everything the agent
    did since the human last reviewed it rather than one hop of it. Best
    effort by design: a failed snapshot must never block the write itself.
    """
    if not client_path:
        return None
    try:
        target = abs_for(client_path)
    except Exception:
        target = None
    if target is None or (target.exists() and not target.is_file()):
        return None
    try:
        existed = target.is_file()
        before = target.read_bytes() if existed else None
    except OSError:
        return None
    bucket = _bucket(sid)
    with _LOCK:
        table = _load_bucket(bucket)
        entry = table.get(client_path)
        if entry is None:
            while len(table) >= MAX_ENTRIES:
                oldest = next(iter(table))
                table.pop(oldest)
            _SEQ[0] += 1
            entry = {"token": _SEQ[0], "path": client_path, "existed": existed,
                     "_bucket": bucket, "baseline": before,
                     "baseline_blob": (_put_blob(bucket, before)
                                       if before is not None else ""),
                     "bytes": len(before or b"")}
            entry["turn"] = turn or ""
            entry["checkpoint"] = checkpoint_record(sid, turn, client_path,
                                                     before, existed, str(target))
            table[client_path] = entry
        else:
            _hydrate(entry)
            if turn and not entry.get("turn"):
                entry["turn"] = turn
            # "before task N" is the file as it stood when task N started, which
            # for a path still pending from an EARLIER task is the content just
            # read — not that earlier snapshot, and not the earlier turn's
            # checkpoint. Keying this on the running turn is what makes a
            # checkpoint restore undo one task rather than two.
            entry["checkpoint"] = checkpoint_record(sid, turn, client_path,
                                                     before, existed, str(target))
        entry.update(tool=tool, summary=summary, diff=diff, when=time.time())
        entry["diff_blob"] = _put_blob(bucket, diff.encode("utf-8"))
        _persist(bucket)
        sweep_blobs(bucket)
        return entry["token"]


def still_pending(sid: str, client_path: str, token: int | None, abs_for) -> bool:
    """False when the entry no longer describes a real change.

    Called AFTER a write was attempted, whatever the outcome. A write that
    failed, was refused, or put back identical bytes must not leave an entry:
    the panel would list a phantom change and Revert would "restore" a state
    nobody altered. Comparing disk against the baseline is the honest test,
    and it also preserves an EARLIER unreviewed change to the same file,
    because the disk still differs from that first snapshot.
    """
    if not client_path or token is None:
        return True
    bucket = _bucket(sid)
    with _LOCK:
        entry = _load_bucket(bucket).get(client_path)
        if entry is None or entry.get("token") != token:
            return False
        _hydrate(entry)
        target = abs_for(client_path)
        try:
            now_exists = bool(target is not None and target.is_file())
            now = target.read_bytes() if now_exists else None
        except OSError:
            return True
        if now_exists == bool(entry.get("existed")) and now == entry.get("baseline"):
            table = _BUCKETS.get(bucket, {})
            table.pop(client_path, None)
            _persist(bucket)
            sweep_blobs(bucket)
            return False
        return True


def drop_token(sid: str, client_path: str, token: int | None) -> None:
    """Drop this attempt's snapshot after a DECLINED write — but never an
    earlier unreviewed change to the same file."""
    bucket = _bucket(sid)
    with _LOCK:
        table = _load_bucket(bucket)
        entry = table.get(client_path)
        if entry is not None and token is not None and entry.get("token") == token:
            table.pop(client_path, None)
            _persist(bucket)
            sweep_blobs(bucket)


def get(sid: str, client_path: str) -> dict | None:
    bucket = _bucket(sid)
    with _LOCK:
        entry = _load_bucket(bucket).get(client_path)
        if entry is not None:
            _hydrate(entry)
        return entry


def pop(sid: str, client_path: str) -> dict | None:
    bucket = _bucket(sid)
    with _LOCK:
        table = _load_bucket(bucket)
        entry = table.pop(client_path, None)
        if entry is not None:
            _hydrate(entry)
            _persist(bucket)
        return entry


def put(sid: str, entry: dict) -> None:
    """Put a popped entry back, so a FAILED revert stays retryable.

    `pop` deliberately does not sweep: the bytes a revert could not write are
    still the only copy of the pre-change state, and deleting them because the
    index momentarily stopped listing them is how an undo becomes a data loss."""
    bucket = str(entry.get("_bucket") or _bucket(sid))
    with _LOCK:
        _load_bucket(bucket)[str(entry.get("path") or "")] = entry
        _persist(bucket)


def list_entries(sid: str) -> list[dict]:
    bucket = _bucket(sid)
    with _LOCK:
        table = _load_bucket(bucket)
        for entry in table.values():
            _hydrate(entry)
        return list(table.values())


def clear(sid: str) -> None:
    """Forget a session's pending review, its checkpoints and its blobs.

    A root being REMOVED or a workspace being reset changes what a client path
    MEANS, so carrying snapshots across it would let Revert write one project's
    bytes into a different file that happens to share the name.
    """
    bucket = _bucket(sid)
    clear_pending(sid)
    with _LOCK:
        _cp_save(bucket, {"bucket": bucket, "turns": 0, "checkpoints": []})
        sweep_blobs(bucket)


def clear_pending(sid: str) -> None:
    """Drop only the unreviewed changes, keeping the checkpoint history.

    Adding a root is the case this exists for: the undo history of work already
    done is worth more than the pending marks, which are about to be indexed
    against a path table that has just shifted."""
    bucket = _bucket(sid)
    with _LOCK:
        _load_bucket(bucket)
        _BUCKETS[bucket] = {}
        _persist(bucket)
        sweep_blobs(bucket)


def sweep_blobs(bucket: str) -> None:
    """Delete blob names no live entry or checkpoint still lists.

    Reference counting is done by scanning the two index files rather than
    storing a counter: a counter drifts the first time a write fails halfway,
    and drift here means deleting bytes somebody can still revert to."""
    keep: set[str] = set()
    for m in _read_json(_review_file(bucket)).get("entries") or []:
        if isinstance(m, dict):
            for key in ("baseline_blob", "diff_blob"):
                if m.get(key):
                    keep.add(str(m[key]))
    for cp in _read_json(_checkpoint_file(bucket)).get("checkpoints") or []:
        if not isinstance(cp, dict):
            continue
        for f in cp.get("files") or []:
            if isinstance(f, dict) and f.get("blob"):
                keep.add(str(f["blob"]))
    for path in blob_dir(bucket).glob("*"):
        if path.name in keep or path.suffix == ".tmp":
            continue
        try:
            path.unlink()
        except OSError:
            pass


# ── applying a hunk ─────────────────────────────────────────────────────
def pending_diff(entry: dict, target) -> str:
    """What separates the file on disk from its baseline, right now.

    The stored `diff` is the diff of ONE hop — the patch the tool said it was
    about to apply — and a turn that edits the same file twice has two of them.
    Reviewing "the change" means the union, so the pending set is recomputed
    against the snapshot rather than read back from whichever hop wrote last.
    Live 2026-11-06: a two-edit turn showed as "1 hunk +1 −1" in the panel,
    and the edit nobody could see was still going to be reverted by the file's
    own Revert button.

    Falls back to the stored diff when the file cannot be read or is not text:
    an unreviewable file must still be LISTED, with a reason attached.
    """
    path = str(entry.get("path") or "")
    base = entry.get("baseline")
    if target is None or base is None:
        return str(entry.get("diff") or "")
    try:
        if not target.is_file():
            return str(entry.get("diff") or "")
        current = target.read_bytes()
    except OSError:
        return str(entry.get("diff") or "")
    try:
        return "\n".join(difflib.unified_diff(
            base.decode("utf-8").splitlines(),
            current.decode("utf-8").splitlines(),
            fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="", n=3))
    except UnicodeDecodeError:
        return str(entry.get("diff") or "")


def apply_hunk(sid: str, client_path: str, abs_for, hunk_id: str = "",
               index: int | None = None, action: str = "revert") -> dict:
    """Keep or revert ONE hunk of an unreviewed change.

    revert: the file moves back toward the baseline, the pending diff is
      recomputed against the baseline, and an entry with nothing left in it is
      dropped (a created file is deleted, the same as a whole-file revert).
    keep: the hunk is folded INTO the baseline. The file is not touched. What
      is left pending is exactly the hunks nobody has decided yet, which is the
      part the old positional model could not express at all.

    A hunk whose content no longer matches returns `hunk-moved` rather than
    editing the wrong region.
    """
    bucket = _bucket(sid)
    entry = get(sid, client_path)
    if entry is None:
        return {"ok": False, "code": 404,
                "error": "no unreviewed change for this path"}
    try:
        file_target = abs_for(client_path)
    except Exception:
        file_target = None
    hs = hunks(pending_diff(entry, file_target))
    if not hs:
        return {"ok": False, "code": 400,
                "error": "this change has no per-hunk detail"}
    target_h = None
    if hunk_id:
        target_h = next((h for h in hs if h["id"] == hunk_id), None)
        if target_h is None:
            return {"ok": False, "code": 409,
                    "error": "hunk-moved: that hunk is not in this change any "
                             "more - refresh and review what is left"}
    elif isinstance(index, int) and 0 <= index < len(hs):
        target_h = hs[index]
    else:
        return {"ok": False, "code": 400,
                "error": f"hunk {index!r} is out of range ({len(hs)} hunks)"}

    if file_target is None:
        return {"ok": False, "code": 400,
                "error": "the path no longer resolves inside the workspace"}

    with _LOCK:
        entry = _load_bucket(bucket).get(client_path)
        if entry is None:
            return {"ok": False, "code": 404,
                    "error": "no unreviewed change for this path"}
        _hydrate(entry)
        baseline_text = ((entry.get("baseline") or b"").decode("utf-8", "replace")
                         if entry.get("existed") else "")

        if action == "keep":
            if not file_target.is_file():
                return {"ok": False, "code": 400,
                        "error": "the file is gone - review it from disk first"}
            current_text, err = read_file_text(file_target)
            if err:
                return {"ok": False, "code": 400, "error": err}
            current_lines, current_joiner = split_lines(current_text)
            baseline_lines, baseline_joiner = split_lines(baseline_text)
            folded, err = apply_block(baseline_lines, target_h, "keep")
            if err:
                return {"ok": False, "code": 409,
                        "error": "hunk-moved: " + err}
            folded_text = baseline_joiner.join(folded)
            _store_baseline(entry, folded_text)
            _touch_diff(entry, folded_text,
                        current_joiner.join(current_lines), client_path)
            left = len(hunks(pending_diff(entry, file_target)))
            if not left:
                table = _BUCKETS.get(bucket, {})
                table.pop(client_path, None)
            _persist(bucket)
            sweep_blobs(bucket)
            return {"ok": True, "action": "keep", "path": client_path,
                    "hunk": target_h["id"], "index": target_h["index"],
                    "hunks_remaining": left}

        # revert
        if not file_target.is_file():
            return {"ok": False, "code": 400,
                    "error": "the file is gone - revert the whole file instead"}
        current_text, err = read_file_text(file_target)
        if err:
            return {"ok": False, "code": 400, "error": err}
        current_lines, current_joiner = split_lines(current_text)
        reverted, err = apply_block(current_lines, target_h, "revert")
        if err:
            return {"ok": False, "code": 409, "error": "hunk-moved: " + err}
        data = current_joiner.join(reverted).encode("utf-8")
        try:
            tmp = file_target.with_name(f".{file_target.name}.aria-hunk-{os.getpid()}")
            tmp.write_bytes(data)
            os.replace(tmp, file_target)
        except OSError as exc:
            return {"ok": False, "code": 500, "error": f"write failed: {exc}"}
        new_current = data.decode("utf-8", "replace")
        _touch_diff(entry, baseline_text, new_current, client_path)
        left = len(hunks(pending_diff(entry, file_target)))
        if left:
            _persist(bucket)
            return {"ok": True, "action": "revert", "path": client_path,
                    "hunk": target_h["id"], "index": target_h["index"],
                    "hunks_remaining": left}
        # Nothing separates the file from its baseline, so the review ends. This
        # is a content test on purpose — comparing rebuilt bytes against the
        # snapshot misses line endings the undo put back the other way, and left
        # such an entry behind.
        table = _BUCKETS.get(bucket, {})
        table.pop(client_path, None)
        _persist(bucket)
        sweep_blobs(bucket)
        if not entry.get("existed"):
            ok, err2 = restore_bytes({"path": client_path, "existed": False,
                                      "baseline": None}, abs_for)
            return {"ok": ok, "action": "revert", "path": client_path,
                    "hunk": target_h["id"], "index": target_h["index"], "hunks_remaining": 0,
                    "deleted": True, "error": err2}
        return {"ok": True, "action": "revert", "path": client_path,
                "hunk": target_h["id"], "index": target_h["index"], "hunks_remaining": 0}


def restore_bytes(entry: dict, abs_for) -> tuple[bool, str]:
    """Put the file back exactly as its baseline found it: bytes restored, or
    removed when the change created it."""
    target = abs_for(str(entry.get("path") or ""))
    if target is None:
        return False, "the path no longer resolves inside the workspace"
    try:
        if entry.get("existed"):
            data = entry.get("baseline")
            if data is None:
                return False, "the snapshot has no content to restore"
            tmp = target.with_name(f".{target.name}.aria-revert-{os.getpid()}")
            tmp.write_bytes(data)
            os.replace(tmp, target)
            return True, ""
        if target.is_file():
            target.unlink()
        elif target.is_dir():
            # A folder the agent created reverts like a file it created: it is
            # removed — but only while empty. Anything inside carries its own
            # review entry and must never be deleted as a side effect.
            try:
                target.rmdir()
            except OSError:
                return False, "the folder is not empty - revert its files first"
        return True, ""
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ── what the panel is shown ─────────────────────────────────────────────
def _matches_baseline(entry: dict, target) -> bool:
    """True only when the file was opened and its bytes equal the snapshot.

    Deliberately conservative: an unreadable path, a missing baseline, or a
    file that is not UTF-8 text answers False, so the entry stays listed with
    whatever reason the caller can show. Dropping a change nobody can review
    because the disk could not be read is how work disappears.
    """
    base = entry.get("baseline")
    if target is None or base is None:
        return False
    try:
        if not target.is_file():
            return False
        return target.read_bytes() == (base if isinstance(base, bytes)
                                       else str(base).encode("utf-8"))
    except (OSError, UnicodeError):
        return False


def changes_payload(sid: str, client_path: str = "", abs_for=None) -> dict:
    """Per-file, per-hunk review state. `path` adds the baseline text so the
    caller can render a real diff of one file without fetching every other
    snapshot in the session.

    The hunks are recomputed from disk for the same reason `apply_hunk` is: the
    stored diff is one hop's patch, and what the user is reviewing is the file.
    """
    out = []
    for entry in list_entries(sid):
        try:
            target = abs_for(str(entry.get("path") or "")) if abs_for else None
        except Exception:
            target = None
        diff = pending_diff(entry, target) if target is not None \
            else str(entry.get("diff") or "")
        hs = hunks(diff)
        # An entry whose file now matches its baseline is not a change anybody
        # can decide, and leaving it in the list cost three things: the footer
        # counted "4 files unreviewed" when nothing was, clicking one opened the
        # editor in SOURCE mode (the review promise with no hunks behind it),
        # and the rail printed "+0 −0" where it should have printed nothing.
        # Only a file that was actually READ can be judged identical —
        # `pending_diff` falls back to the stored diff for anything it could
        # not open, and an unreviewable file stays listed with a reason.
        if not hs and _matches_baseline(entry, target):
            continue
        row = {
            "path": entry.get("path"),
            "tool": entry.get("tool"),
            "summary": entry.get("summary"),
            "when": entry.get("when"),
            "created": not entry.get("existed"),
            "token": entry.get("token"),
            "checkpoint": entry.get("checkpoint") or "",
            "turn": entry.get("turn") or "",
            "hunks": [{"id": h["id"], "index": h["index"],
                       "header": h["header"],
                       "old_start": h["old_start"], "old_lines": h["old_lines"],
                       "new_start": h["new_start"], "new_lines": h["new_lines"],
                       "added": h["added"], "removed": h["removed"],
                       "old": h["old"], "new": h["new"]} for h in hs],
            "added": sum(h["added"] for h in hs),
            "removed": sum(h["removed"] for h in hs),
            "diff": diff,
        }
        if client_path and entry.get("path") == client_path:
            row["baseline"] = ((entry.get("baseline") or b"").decode("utf-8", "replace")
                               if entry.get("existed") else "")
            row["baseline_missing"] = bool(entry.get("existed")
                                           and entry.get("baseline") is None)
        out.append(row)
    return {"changes": out, "sid": sid if _bucket(sid) != "_default" else ""}


# ── checkpoints ─────────────────────────────────────────────────────────
def _cp_load(bucket: str) -> dict:
    got = _read_json(_checkpoint_file(bucket))
    if not isinstance(got.get("checkpoints"), list):
        got = {"bucket": bucket, "turns": 0, "checkpoints": []}
    got.setdefault("turns", 0)
    got.setdefault("checkpoints", [])
    got.setdefault("acted", [])
    return got


def _cp_save(bucket: str, data: dict) -> None:
    data["updated"] = time.time()
    _write_json(_checkpoint_file(bucket), data)


def checkpoint_record(sid: str, turn: str, client_path: str,
                      before: bytes | None, existed: bool,
                      abs_path: str = "", name: str = "") -> str:
    """Add one file's pre-change bytes to this turn's checkpoint, creating the
    checkpoint on first use. Returns its id.

    A checkpoint is therefore "the folder as it stood before task N", which is
    what a human means by the phrase, and it is built from bytes that were
    already read for the review snapshot — capturing it costs no extra IO.

    `name` overrides that label for the operations that are not a task: a
    project-wide replace or a rename has to read as itself in the undo list,
    because "before task 7" tells a person nothing about what they are
    restoring to.

    The absolute path is recorded next to the client path because a client path
    is only meaningful against the root set it was written in; adding a folder
    to a session can move the base and relabel every file. Restore then asks
    "is this still one of my session's folders" about the real target instead of
    resolving a name that now points somewhere else."""
    if not turn:
        return ""
    bucket = _bucket(sid)
    with _LOCK:
        data = _cp_load(bucket)
        found = next((c for c in data["checkpoints"]
                      if c.get("turn") == turn), None)
        if found is None:
            data["turns"] = int(data.get("turns") or 0) + 1
            found = {"id": "cp-" + hashlib.sha1(
                        f"{bucket}\x1f{turn}\x1f{time.time()}".encode("utf-8")
                    ).hexdigest()[:10],
                     "turn": turn,
                     "name": (name.strip() if name else
                              f"before task {data['turns']}"),
                     "when": time.time(), "files": []}
            data["checkpoints"].append(found)
            # New work ends the undo/redo walk: the cursor goes back to the
            # newest checkpoint and a redo no longer has anything to replay.
            # A restore's own snapshot is not new work, so it keeps the walk
            # intact — that is what makes undo → redo → undo work.
            if not turn.startswith("undo-"):
                data["acted"] = []
        names = {str(f.get("path") or "") for f in found["files"]}
        if client_path and client_path not in names:
            blob = (_put_blob(bucket, before)
                    if before is not None else "")
            found["files"].append({"path": client_path, "existed": bool(existed),
                                   "abs": str(abs_path or ""),
                                   "blob": blob,
                                   "bytes": len(before or b"")})
        _cp_save(bucket, data)
        return str(found["id"])


def checkpoint_mark_removed(sid: str, turn: str, client_path: str,
                            removed: str, removed_sha: str = "",
                            abs_removed: str = "") -> str:
    """Note that restoring this checkpoint must ALSO remove another path.

    A rename is one act, not two: the file at `client_path` is restored and the
    name it moved to goes away. A checkpoint that only recorded the first half
    made `/undo` after a rename leave BOTH names on disk — a duplicate, which is
    the opposite of undoing. The hash is the guard: if the moved file was edited
    after the rename, restore leaves it alone rather than deleting work nobody
    reviewed.

    Called right after the rename, when the destination finally exists."""
    if not turn or not removed:
        return ""
    bucket = _bucket(sid)
    with _LOCK:
        data = _cp_load(bucket)
        found = next((c for c in data["checkpoints"]
                      if c.get("turn") == turn), None)
        if found is None:
            return ""
        entry = next((f for f in (found.get("files") or [])
                      if isinstance(f, dict)
                      and str(f.get("path") or "") == client_path), None)
        if entry is None:
            return ""
        entry["removed"] = removed
        entry["removed_abs"] = str(abs_removed or "")
        entry["removed_sha"] = removed_sha or ""
        _cp_save(bucket, data)
        return str(found["id"])


def checkpoints(sid: str) -> list[dict]:
    bucket = _bucket(sid)
    data = _cp_load(bucket)
    return [{"id": str(c.get("id") or ""), "name": str(c.get("name") or ""),
             "turn": str(c.get("turn") or ""), "when": c.get("when") or 0,
             # "" = a task's own "before" state, "redo" = a state a later redo
             # can replay, "undo" = the state a redo came from, undoable.
             "capture": str(c.get("capture") or ""),
             "files": len(c.get("files") or []),
             "bytes": sum(int(f.get("bytes") or 0) for f in (c.get("files") or [])
                          if isinstance(f, dict))}
            for c in reversed(data["checkpoints"])]


def _cp_set_capture(bucket: str, turn: str, kind: str) -> None:
    """Tag a restore's snapshot with the direction it can be taken in.

    Both directions write a snapshot, and without a tag they are
    indistinguishable: `/redo` would offer the snapshot a redo just wrote and
    undo the redo instead of finishing the replay."""
    if not turn:
        return
    with _LOCK:
        data = _cp_load(bucket)
        found = next((c for c in data["checkpoints"]
                      if c.get("turn") == turn), None)
        if found is None:
            return
        found["capture"] = kind
        _cp_save(bucket, data)


def acted(sid: str) -> list[str]:
    """Checkpoint ids the undo/redo walk has already passed.

    Checkpoints are never removed, so without a cursor every `/undo` would pick
    the same newest entry and the second one would be a no-op. The list answers
    "where is the walk": `/undo` takes the newest entry that is not in it,
    `/redo` takes the newest restore-snapshot that is not in it."""
    return [str(x) for x in (_cp_load(_bucket(sid)).get("acted") or [])]


def _mark_acted(bucket: str, cp_id: str) -> None:
    if not cp_id:
        return
    with _LOCK:
        data = _cp_load(bucket)
        if cp_id not in data["acted"]:
            data["acted"].append(cp_id)
            _cp_save(bucket, data)


def checkpoint_detail(sid: str, cp_id: str) -> dict:
    bucket = _bucket(sid)
    found = next((c for c in _cp_load(bucket)["checkpoints"]
                  if str(c.get("id") or "") == cp_id), None)
    if found is None:
        return {}
    return {**found, "files": [{"path": str(f.get("path") or ""),
                                "existed": bool(f.get("existed")),
                                "abs": str(f.get("abs") or ""),
                                "removed": str(f.get("removed") or ""),
                                "bytes": int(f.get("bytes") or 0)}
                               for f in (found.get("files") or [])
                               if isinstance(f, dict)]}


def restore_checkpoint(sid: str, cp_id: str, resolve) -> dict:
    """Put every file in one checkpoint back, and make that undoable first.

    Restoring overwrites live work, so the bytes being replaced are captured as
    a checkpoint of their own before anything is written: a mistaken restore is
    a two-click recovery rather than a lost afternoon.

    `resolve(client_path, recorded_abs)` is the caller's one seam — it answers
    "may this session touch this file", preferring the path the checkpoint was
    actually taken in. A restore must never write into a file that merely shares
    a name with one this session used to hold."""
    bucket = _bucket(sid)
    data = _cp_load(bucket)
    found = next((c for c in data["checkpoints"]
                  if str(c.get("id") or "") == cp_id), None)
    if found is None:
        return {"ok": False, "code": 404, "error": "no such checkpoint"}
    files = [f for f in (found.get("files") or []) if isinstance(f, dict)]
    if not files:
        return {"ok": False, "code": 400, "error": "this checkpoint holds no files"}

    def _target(f: dict):
        path = str(f.get("path") or "")
        try:
            return resolve(path, str(f.get("abs") or ""))
        except Exception:
            return None

    undo_turn = f"undo-{cp_id}-{int(time.time())}"
    restored, removed, failed, skipped = [], [], [], []

    def _read(target):
        try:
            return target.read_bytes() if target.is_file() else None
        except OSError:
            return None

    def _undo_target(path: str, abs_rec: str):
        try:
            return resolve(path, abs_rec)
        except Exception:
            return None

    # Capture the state a redo would need, inverting each entry. A rename entry
    # means "restore this path and remove the name it moved to", so its snapshot
    # has to mean the opposite — put the moved file back and remove this path.
    # Getting the inversion wrong is not a harmless cosmetic slip: capturing the
    # removal side as a deletion left a redo that deleted both files.
    for f in files:
        path = str(f.get("path") or "")
        target = _target(f)
        if target is None:
            skipped.append({"path": path, "reason": "outside this session's folders"})
            continue
        removed_path = str(f.get("removed") or "")
        if not removed_path:
            current = _read(target)
            checkpoint_record(sid, undo_turn, path, current,
                              current is not None, str(target))
            continue
        rm_target = _undo_target(removed_path, str(f.get("removed_abs") or ""))
        current = _read(target)
        if rm_target is None:
            checkpoint_record(sid, undo_turn, path, current,
                              current is not None, str(target))
            continue
        moved = _read(rm_target)
        checkpoint_record(sid, undo_turn, removed_path, moved,
                          moved is not None, str(rm_target))
        checkpoint_mark_removed(sid, undo_turn, removed_path, path,
                                sha_of(current or b"") if current is not None
                                else "", str(target))
    undo_id = next((str(c.get("id")) for c in _cp_load(bucket)["checkpoints"]
                    if c.get("turn") == undo_turn), "")
    # Direction: undoing a task leaves a state to replay forward; redoing a
    # replay leaves the state to come back to. Same snapshot shape, opposite
    # meaning, so the tag is what keeps the two walks from stealing each other's
    # steps.
    _cp_set_capture(bucket, undo_turn,
                    "undo" if str(found.get("capture") or "") == "redo" else "redo")

    for f in files:
        path = str(f.get("path") or "")
        target = _target(f)
        if target is None:
            continue
        # A rename has a second half: the name the file moved to. Undo it only
        # when the file still holds what the rename put there — an edit after
        # the rename means this is now somebody's work, not the rename's result.
        removed_path = str(f.get("removed") or "")
        if removed_path:
            rm_target = _undo_target(removed_path, str(f.get("removed_abs") or ""))
            if rm_target is not None and rm_target.is_file():
                want = str(f.get("removed_sha") or "")
                if want and sha_of(_read(rm_target) or b"") != want:
                    skipped.append({"path": removed_path,
                                    "reason": "changed after the rename, so "
                                              "it was left alone"})
                else:
                    try:
                        rm_target.unlink()
                    except OSError as exc:
                        failed.append({"path": removed_path, "reason": str(exc)})
                    else:
                        removed.append(removed_path)
        data_bytes = _get_blob(bucket, str(f.get("blob") or ""))
        if not f.get("existed"):
            try:
                if target.is_file():
                    target.unlink()
            except OSError as exc:
                failed.append({"path": path, "reason": str(exc)})
            else:
                restored.append(path)
            continue
        if data_bytes is None:
            failed.append({"path": path,
                           "reason": "the stored bytes are no longer on disk"})
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.aria-cp-{os.getpid()}")
            tmp.write_bytes(data_bytes)
            os.replace(tmp, target)
        except OSError as exc:
            failed.append({"path": path, "reason": str(exc)})
        else:
            restored.append(path)

    # Anything still pending review for those paths now describes a file that
    # no longer differs from its own snapshot; leaving the marks up would offer
    # a Revert that restores the state we just restored away.
    with _LOCK:
        table = _load_bucket(bucket)
        for path in restored + removed:
            entry = table.pop(path, None)
            if entry is not None:
                _hydrate(entry)
        _persist(bucket)
        _cp_save(bucket, _cp_load(bucket))
        sweep_blobs(bucket)
    # The walk moves past this checkpoint, so `/undo` cannot land on it again.
    _mark_acted(bucket, cp_id)
    return {"ok": not failed, "checkpoint": cp_id, "name": found.get("name"),
            "restored": restored, "removed": removed, "failed": failed,
            "skipped": skipped, "acted": acted(sid),
            "undo": {"id": undo_id, "turn": undo_turn,
                     "name": f"before restoring {found.get('name')}"}}
