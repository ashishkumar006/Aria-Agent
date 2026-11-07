"""The review store: snapshots, hunks with identity, and named checkpoints.

The console sends `auto_approve: true`: the write lands without a dialog, and
the safety net moves to the review step — Keep drops the snapshot, Revert
restores the bytes as they were before the agent FIRST touched the file. These
tests pin the net itself, which is `studio_review.py` since slice 4 moved it out
of `agent_server` and onto disk:

- the snapshot is taken BEFORE the write, and the first one wins: a second
  write to the same file must not overwrite the revert target, or Revert would
  undo only the last hop and silently keep the earlier ones
- a file the agent created reverts by DELETION, a file it edited reverts by
  restoring bytes
- a declined write drops only this attempt's snapshot - never someone else's
  pending review of the same file
- Keep resolves the review without touching the bytes on disk
- a hunk is addressed by its CONTENT, so deciding one cannot make another
  revert the wrong region (slice 4's reason for existing)
- keeping one hunk folds it into the baseline, so what stays pending is exactly
  what is still unreviewed
- snapshots and checkpoints survive a restart, and a checkpoint restore is
  itself undoable
"""
from __future__ import annotations

import difflib

import pytest

import studio_review as sr

SID = ""       # the bucket of a caller that names no session


@pytest.fixture()
def ws(tmp_path):
    """A fake workspace in the shipped shape: base = parent, one root.

    `abs_for` stands in for `agent_server._code_agent_abs` — the module never
    decides confinement, it asks, so the tests get to hand it the same question
    the routes do. Client paths are written against the BASE, which is why a
    file at `root/m.py` is `ws/m.py` here."""
    root = tmp_path / "ws"
    root.mkdir()
    inside, base = root.resolve(), tmp_path.resolve()

    def abs_for(p: str, recorded_abs: str = ""):
        from pathlib import Path
        if recorded_abs:
            try:
                q = Path(recorded_abs).resolve()
            except OSError:
                return None
            return q if str(q).startswith(str(inside)) else None
        if not p:
            return None
        q = (base / p).resolve()
        return q if str(q).startswith(str(inside)) else None

    sr.clear(SID)
    yield root, abs_for
    sr.clear(SID)


def diff_of(before: str, after: str, path: str = "ws/m.py") -> str:
    return "\n".join(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="", n=3))


# ── the snapshot discipline the net hangs on ──────────────────────────────

def test_snapshot_is_taken_before_the_write_and_first_wins(ws):
    root, abs_for = ws
    f = root / "m.py"
    f.write_bytes(b"v1\n")

    t1 = sr.take(SID, "ws/m.py", "edit_file", "s1", "d1", abs_for)
    f.write_bytes(b"v2\n")
    t2 = sr.take(SID, "ws/m.py", "edit_file", "s2", "d2", abs_for)
    f.write_bytes(b"v3\n")

    entry = sr.get(SID, "ws/m.py")
    assert entry["baseline"] == b"v1\n", (
        "the second write clobbered the revert target; Revert would keep v2")
    assert entry["diff"] == "d2", "the listing must show the latest change"
    # Same token: still one review item, and a decline of the LAST attempt
    # must not drop the FIRST snapshot.
    assert t1 == t2
    sr.drop_token(SID, "ws/m.py", t2)
    assert sr.get(SID, "ws/m.py") is None


def test_a_stale_decline_never_cancels_a_newer_review(ws):
    root, abs_for = ws
    (root / "m.py").write_text("v1\n", encoding="utf-8")
    t_old = sr.take(SID, "ws/m.py", "edit_file", "s", "d", abs_for)
    # Someone keeps the file, then a NEW write arrives and parks a new review.
    sr.pop(SID, "ws/m.py")
    t_new = sr.take(SID, "ws/m.py", "edit_file", "s2", "d2", abs_for)
    assert t_new != t_old
    # The old run's decline finally arrives - it must not free the new entry.
    sr.drop_token(SID, "ws/m.py", t_old)
    assert sr.get(SID, "ws/m.py") is not None


def test_revert_restores_the_bytes(ws):
    root, abs_for = ws
    f = root / "m.py"
    f.write_bytes(b"print('bye from Aria v4')\n")
    sr.take(SID, "ws/m.py", "edit_file", "s", "d", abs_for)
    f.write_bytes(b"print('ruined')\n")

    entry = sr.pop(SID, "ws/m.py")
    ok, err = sr.restore_bytes(entry, abs_for)
    assert ok, err
    assert f.read_bytes() == b"print('bye from Aria v4')\n"


def test_revert_deletes_a_file_the_agent_created(ws):
    root, abs_for = ws
    sr.take(SID, "ws/new.py", "create_file", "s", "d", abs_for)
    f = root / "new.py"
    _write(f, "created\n")

    entry = sr.pop(SID, "ws/new.py")
    ok, err = sr.restore_bytes(entry, abs_for)
    assert ok, err
    assert not f.exists(), "reverting a created file must remove it"


def test_revert_removes_an_empty_folder_the_agent_created(ws):
    root, abs_for = ws
    sr.take(SID, "ws/newpkg", "make_dir", "s", "", abs_for)
    d = root / "newpkg"
    d.mkdir()

    entry = sr.pop(SID, "ws/newpkg")
    ok, err = sr.restore_bytes(entry, abs_for)
    assert ok, err
    assert not d.exists(), "an empty created folder reverts by removal"


def test_revert_refuses_a_non_empty_created_folder(ws):
    root, abs_for = ws
    sr.take(SID, "ws/newpkg", "make_dir", "s", "", abs_for)
    d = root / "newpkg"
    d.mkdir()
    (d / "keep.py").write_text("x\n", encoding="utf-8")

    entry = sr.pop(SID, "ws/newpkg")
    ok, err = sr.restore_bytes(entry, abs_for)
    assert not ok and "not empty" in err
    assert d.exists(), "files inside are their own review items; never deleted"


def test_keep_keeps_the_bytes_and_drops_the_snapshot(ws):
    root, abs_for = ws
    f = root / "m.py"
    _write(f, "v1\n")
    sr.take(SID, "ws/m.py", "edit_file", "s", "d", abs_for)
    _write(f, "v2\n")

    assert sr.pop(SID, "ws/m.py") is not None
    assert _read(f) == "v2\n"


def test_unresolvable_paths_snapshot_to_nothing(ws):
    root, abs_for = ws
    assert sr.take(SID, "", "edit_file", "s", "d", abs_for) is None
    assert sr.take(SID, "ws/../outside.py", "edit_file", "s", "d", abs_for) is None
    # A directory is not a reviewable write target either.
    (root / "pkg").mkdir()
    assert sr.take(SID, "ws/pkg", "edit_file", "s", "d", abs_for) is None
    assert sr.list_entries(SID) == []


def test_restore_refuses_when_the_path_left_the_workspace(ws):
    root, abs_for = ws
    ok, err = sr.restore_bytes(
        {"path": "ws/../outside.py", "existed": True, "baseline": b"x"}, abs_for)
    assert not ok and err


def test_snapshot_eviction_is_bounded(ws, monkeypatch):
    root, abs_for = ws
    monkeypatch.setattr(sr, "MAX_ENTRIES", 2)
    for name in ("a.py", "b.py", "c.py"):
        (root / name).write_text("x\n", encoding="utf-8")
        sr.take(SID, f"ws/{name}", "edit_file", "s", "d", abs_for)
    assert len(sr.list_entries(SID)) == 2
    assert sr.get(SID, "ws/c.py") is not None


# ── phantom entries: a write that never landed must not be reviewable ────────

def test_a_failed_write_leaves_no_review_entry(ws):
    """The snapshot is taken BEFORE dispatch, so a failed write used to leave
    an entry the panel listed and Revert 'restored' - to the state that was
    already there."""
    root, abs_for = ws
    (root / "m.py").write_text("v1\n", encoding="utf-8")
    token = sr.take(SID, "ws/m.py", "edit_file", "s", "d", abs_for)
    # The tool raised; the file is untouched.
    sr.still_pending(SID, "ws/m.py", token, abs_for)
    assert sr.get(SID, "ws/m.py") is None


def test_a_noop_write_is_not_a_reviewable_change(ws):
    """`update_file` with identical bytes is a success, not a change."""
    root, abs_for = ws
    (root / "m.py").write_text("same\n", encoding="utf-8")
    token = sr.take(SID, "ws/m.py", "update_file", "s", "d", abs_for)
    sr.still_pending(SID, "ws/m.py", token, abs_for)
    assert sr.get(SID, "ws/m.py") is None


def test_settle_keeps_an_earlier_change_still_on_disk(ws):
    """A later failed write must not cancel an earlier unreviewed one."""
    root, abs_for = ws
    f = root / "m.py"
    _write(f, "v1\n")
    before = f.read_bytes()
    token = sr.take(SID, "ws/m.py", "edit_file", "s", "d", abs_for)
    _write(f, "v2\n")        # an earlier write landed
    sr.still_pending(SID, "ws/m.py", token, abs_for)   # this one failed
    entry = sr.get(SID, "ws/m.py")
    assert entry is not None and entry["baseline"] == before


def test_settle_keeps_a_file_the_agent_created(ws):
    root, abs_for = ws
    token = sr.take(SID, "ws/new.py", "create_file", "s", "d", abs_for)
    (root / "new.py").write_text("created\n", encoding="utf-8")
    sr.still_pending(SID, "ws/new.py", token, abs_for)
    entry = sr.get(SID, "ws/new.py")
    assert entry is not None and entry["existed"] is False


# ── hunks with identity ──────────────────────────────────────────────────────

def _write(f, text: str) -> None:
    """Bytes, not `write_text`: on Windows text mode rewrites every "\\n" to
    "\\r\\n", which would make half these tests accidentally about endings."""
    f.write_bytes(text.encode("utf-8"))


def _read(f) -> str:
    return f.read_bytes().decode("utf-8")


def _two_hunk_case(root, abs_for, turn="t-1"):
    """A file the agent changed in two places, snappedshotted and diffed the
    way the write path does it — through the store, so a restart sees the same
    thing a live server does."""
    f = root / "m.py"
    original = "\n".join(f"line{i}" for i in range(1, 21)) + "\n"
    _write(f, original)
    sr.take(SID, "ws/m.py", "edit_file", "s", "", abs_for, turn=turn)
    changed = original.replace("line5", "FIVE edited by agent") \
                       .replace("line15", "FIFTEEN edited by agent")
    _write(f, changed)
    pending = diff_of(original, changed)
    sr.take(SID, "ws/m.py", "edit_file", "s", pending, abs_for, turn=turn)
    assert sr.get(SID, "ws/m.py")["baseline"] == original.encode("utf-8"), (
        "the second take must not move the revert target")
    return f, original, changed


def test_hunk_ids_fingerprint_content_not_position(ws):
    root, abs_for = ws
    hs = sr.hunks(diff_of("a\nb\nc\n", "a\nB\nc\n"))
    assert len(hs) == 1
    h = hs[0]
    assert h["added"] == 1 and h["removed"] == 1
    assert h["id"].startswith("h") and len(h["id"]) == 13
    # The same change written twice fingerprints the same — that is what makes
    # it safe to click a hunk after other hunks have been decided.
    assert sr.hunks(diff_of("a\nb\nc\n", "a\nB\nc\n"))[0]["id"] == h["id"]
    # Same edit, later in the file: the @@ numbers differ and the id does not,
    # because position is exactly what must NOT be part of an identity. Both
    # copies need three lines of their own on each side of the change, because
    # the context a hunk carries IS part of its fingerprint — which is what
    # stops two unrelated regions that both say `x = 1` claiming to be each
    # other, and is also why a hunk near the top of a file is a different
    # object from the same text in the middle of it.
    core = ["a", "b", "c"]
    tail = ["q1", "q2", "q3"]
    head = ["p1", "p2", "p3"]

    def pair(lines):
        out = list(lines)
        out[out.index("b")] = "B"
        return "\n".join(lines) + "\n", "\n".join(out) + "\n"

    moved = sr.hunks(diff_of(*pair(head + core + tail)))[0]
    far = sr.hunks(diff_of(*pair(["x1", "x2", "x3", "x4", "x5"]
                                 + head + core + tail)))[0]
    edge = sr.hunks(diff_of("a\nb\nc\n", "a\nB\nc\n"))[0]
    assert far["old_start"] != moved["old_start"]
    assert far["id"] == moved["id"]
    assert edge["id"] != moved["id"], (
        "same text, different context window — a different hunk, and the ids "
        "must not pretend otherwise")


def test_a_revert_on_a_crlf_file_finds_its_hunk_and_keeps_the_ending(ws):
    """A unified diff carries no line endings, so searching a CRLF file for the
    hunk's lines used to match NOTHING: every Windows checkout came back
    "hunk-moved", and a revert that did happen rewrote the file as LF and
    dirtied every line in git."""
    root, abs_for = ws
    f = root / "m.py"
    original = "\r\n".join(f"line{i}" for i in range(1, 11)) + "\r\n"
    _write(f, original)
    sr.take(SID, "ws/m.py", "edit_file", "s", "", abs_for, turn="t-1")
    changed = original.replace("line5", "FIVE edited")
    _write(f, changed)
    d = diff_of(original, changed)
    sr.take(SID, "ws/m.py", "edit_file", "s", d, abs_for, turn="t-1")

    hs = sr.hunks(d)
    assert len(hs) == 1
    got = sr.apply_hunk(SID, "ws/m.py", abs_for, hunk_id=hs[0]["id"],
                        action="revert")
    assert got["ok"], got
    assert f.read_bytes().decode("utf-8") == original, (
        "the ending must survive the revert, not be normalised away")


def test_two_identical_hunks_are_distinguishable(ws):
    root, abs_for = ws
    before = "\n".join(f"line{i}" for i in range(1, 21)) + "\n"
    after = before.replace("line3", "line3 EDIT").replace("line13", "line13 EDIT")
    hs = sr.hunks(diff_of(before, after))
    assert len(hs) == 2
    ids = [h["id"] for h in hs]
    assert len(set(ids)) == 2, (
        "identical content in two places collapsed to one id; reverting either "
        "would then revert the other")


def test_revert_by_id_undoes_one_hunk_and_leaves_the_other(ws):
    root, abs_for = ws
    f, original, changed = _two_hunk_case(root, abs_for)
    hs = sr.hunks(sr.get(SID, "ws/m.py")["diff"])
    assert len(hs) == 2

    got = sr.apply_hunk(SID, "ws/m.py", abs_for, hunk_id=hs[0]["id"],
                        action="revert")
    assert got["ok"], got
    now = _read(f)
    assert "line5" in now and "FIVE edited by agent" not in now
    assert "FIFTEEN edited by agent" in now, "the other hunk must survive"
    assert got["hunks_remaining"] == 1
    left = sr.hunks(sr.get(SID, "ws/m.py")["diff"])
    assert [h["id"] for h in left] == [hs[1]["id"]]

    got2 = sr.apply_hunk(SID, "ws/m.py", abs_for, hunk_id=hs[1]["id"],
                         action="revert")
    assert got2["ok"] and got2["hunks_remaining"] == 0
    assert _read(f) == original
    assert sr.get(SID, "ws/m.py") is None, "an empty pending set ends the review"


def test_keep_folds_the_hunk_into_the_baseline(ws):
    """Keeping one hunk used to be unrepresentable: there was one baseline and
    one diff, so accepting part of a change either threw the rest away or left
    it pending against a baseline that no longer described the file."""
    root, abs_for = ws
    f, original, changed = _two_hunk_case(root, abs_for)
    hs = sr.hunks(sr.get(SID, "ws/m.py")["diff"])

    got = sr.apply_hunk(SID, "ws/m.py", abs_for, hunk_id=hs[0]["id"],
                        action="keep")
    assert got["ok"], got
    assert _read(f) == changed, "keep must not touch the file"
    entry = sr.get(SID, "ws/m.py")
    assert "FIVE edited by agent" in entry["baseline"].decode("utf-8"), (
        "the kept hunk belongs to the baseline now")
    assert "line5" not in entry["baseline"].decode("utf-8")
    left = sr.hunks(entry["diff"])
    assert [h["id"] for h in left] == [hs[1]["id"]], (
        "what is still pending is exactly what is still unreviewed")

    # And the whole-file revert that follows honours the keep.
    sr.pop(SID, "ws/m.py")
    ok, err = sr.restore_bytes(entry, abs_for)
    assert ok, err
    after = _read(f)
    assert "FIVE edited by agent" in after, "keep survives a later file revert"
    assert "FIFTEEN edited by agent" not in after


def test_a_moved_hunk_is_refused_not_guessed(ws):
    """A hunk clicked after someone rewrote that region must fail loudly; the
    alternative is a review tool that edits the wrong lines."""
    root, abs_for = ws
    f, original, changed = _two_hunk_case(root, abs_for)
    hs = sr.hunks(sr.get(SID, "ws/m.py")["diff"])
    # A human edits the first hunk's text away, then clicks Revert on it.
    _write(f, changed.replace("FIVE edited by agent", "five by hand"))

    got = sr.apply_hunk(SID, "ws/m.py", abs_for, hunk_id=hs[0]["id"],
                        action="revert")
    assert not got["ok"] and got["code"] == 409
    assert "hunk-moved" in got["error"]
    assert _read(f).count("five by hand") == 1, (
        "a refused revert must write nothing at all")


def test_an_unknown_hunk_id_is_refused(ws):
    root, abs_for = ws
    _two_hunk_case(root, abs_for)
    got = sr.apply_hunk(SID, "ws/m.py", abs_for, hunk_id="h000000000000",
                        action="revert")
    assert not got["ok"] and got["code"] == 409 and "hunk-moved" in got["error"]


def test_a_revert_of_a_file_with_no_entry_says_so(ws):
    root, abs_for = ws
    got = sr.apply_hunk(SID, "ws/m.py", abs_for, hunk_id="hwhatever",
                        action="revert")
    assert not got["ok"] and got["code"] == 404


# ── surviving a restart ──────────────────────────────────────────────────────

def test_pending_review_and_its_revert_target_survive_a_restart(ws, monkeypatch):
    """The in-memory dict used to be the only copy of the bytes Revert
    restores. Restarting the server therefore turned every unreviewed change
    into an unreviewable one — marks stayed on screen and Revert 404'd."""
    root, abs_for = ws
    f, original, changed = _two_hunk_case(root, abs_for)
    hs = [h["id"] for h in sr.hunks(sr.get(SID, "ws/m.py")["diff"])]

    # "Restart": forget every cache; the disk is the store now.
    monkeypatch.setattr(sr, "_BUCKETS", {})
    monkeypatch.setattr(sr, "_LOADED", set())
    monkeypatch.setattr(sr, "_SEQ", [0])

    listed = sr.changes_payload(SID, "ws/m.py")
    row = listed["changes"][0]
    assert [h["id"] for h in row["hunks"]] == hs, "ids must be stable across a restart"
    assert row["baseline"] == original

    got = sr.apply_hunk(SID, "ws/m.py", abs_for, hunk_id=hs[1], action="revert")
    assert got["ok"], got
    assert "line15" in _read(f)


def test_a_checkpoint_survives_a_restart(ws, monkeypatch):
    root, abs_for = ws
    _two_hunk_case(root, abs_for, turn="t-persist")
    monkeypatch.setattr(sr, "_BUCKETS", {})
    monkeypatch.setattr(sr, "_LOADED", set())
    assert [c["name"] for c in sr.checkpoints(SID)] == ["before task 1"]


# ── checkpoints ──────────────────────────────────────────────────────────────

def test_a_checkpoint_names_the_turn_and_holds_its_before_bytes(ws):
    root, abs_for = ws
    f = root / "m.py"
    _write(f, "v1\n")
    tok = sr.take(SID, "ws/m.py", "edit_file", "s", "d", abs_for, turn="t-1")
    assert sr.get(SID, "ws/m.py")["checkpoint"].startswith("cp-")
    _write(f, "v2\n")
    # The same turn writing again must not add a second copy of the path.
    sr.take(SID, "ws/m.py", "edit_file", "s", "d2", abs_for, turn="t-1")

    cps = sr.checkpoints(SID)
    assert [c["name"] for c in cps] == ["before task 1"]
    detail = sr.checkpoint_detail(SID, cps[0]["id"])
    assert len(detail["files"]) == 1, "one record per path per turn, not per write"
    assert detail["files"][0]["path"] == "ws/m.py"

    # Two turns, two named checkpoints.
    g = root / "n.py"
    _write(g, "only\n")
    sr.take(SID, "ws/n.py", "create_file", "s", "d", abs_for, turn="t-2")
    names = [c["name"] for c in sr.checkpoints(SID)]
    assert names == ["before task 2", "before task 1"], (
        "newest first, and numbered by turn rather than by wall clock")


def test_restoring_a_checkpoint_undoes_the_turn_and_is_undoable(ws):
    root, abs_for = ws
    f = root / "m.py"
    _write(f, "v1\n")
    sr.take(SID, "ws/m.py", "edit_file", "s", diff_of("v1\n", "v2\n"), abs_for,
            turn="t-1")
    _write(f, "v2\n")
    cp = sr.checkpoints(SID)[0]

    got = sr.restore_checkpoint(SID, cp["id"], abs_for)
    assert got["ok"], got
    assert _read(f) == "v1\n"
    assert sr.get(SID, "ws/m.py") is None, (
        "a restored file has no pending change; leaving the marks up would "
        "offer a Revert that undoes the restore")
    # And the restore itself is recoverable.
    undo = got["undo"]
    assert undo["name"].startswith("before restoring")
    again = sr.restore_checkpoint(SID, undo["id"], abs_for)
    assert again["ok"]
    assert _read(f) == "v2\n"


def test_restoring_a_checkpoint_removes_a_created_file(ws):
    root, abs_for = ws
    sr.take(SID, "ws/new.py", "create_file", "s", "", abs_for, turn="t-1")
    (root / "new.py").write_text("created\n", encoding="utf-8")
    cp = sr.checkpoints(SID)[0]
    got = sr.restore_checkpoint(SID, cp["id"], abs_for)
    assert got["ok"], got
    assert not (root / "new.py").exists()


def test_restore_refuses_a_file_that_left_the_session(ws):
    """A checkpoint records the absolute file it came from. When that folder is
    no longer one this session holds, the restore must say so rather than
    resolve the same NAME against whatever root set exists now."""
    root, abs_for = ws
    f = root / "m.py"
    _write(f, "v1\n")
    sr.take(SID, "ws/m.py", "edit_file", "s", "", abs_for, turn="t-1")
    _write(f, "v2\n")
    cp = sr.checkpoints(SID)[0]

    def outside(client_path, recorded_abs=""):
        return None            # the session no longer holds this folder

    got = sr.restore_checkpoint(SID, cp["id"], outside)
    assert got["skipped"] and not got["restored"]
    assert _read(f) == "v2\n", "a refused file is untouched"


# ── sessions and storage ─────────────────────────────────────────────────────

def test_two_sessions_do_not_share_pending_reviews(ws, tmp_path):
    """Keyed by client path alone, a second session's snapshot for the same
    relative name silently replaced the first's — and its Revert wrote the wrong
    project's bytes."""
    root, abs_for = ws
    (root / "m.py").write_text("mine\n", encoding="utf-8")
    sr.take("s-one", "ws/m.py", "edit_file", "s", "d", abs_for, turn="t-1")
    sr.take("s-two", "ws/m.py", "edit_file", "s", "d", abs_for, turn="t-1")

    assert [e["path"] for e in sr.list_entries("s-one")] == ["ws/m.py"]
    assert len(sr.list_entries("s-two")) == 1
    assert len(sr.list_entries("")) == 0
    sr.clear("s-one")
    sr.clear("s-two")
    assert sr.list_entries("s-two") == []
    assert sr.list_entries("s-one") == []


def test_eviction_does_not_delete_bytes_a_checkpoint_still_lists(ws, monkeypatch):
    """Entry blobs are shared with checkpoints by content. Deleting an evicted
    entry's files on the way out is how a named undo loses its bytes."""
    root, abs_for = ws
    monkeypatch.setattr(sr, "MAX_ENTRIES", 1)
    for name in ("a.py", "b.py"):
        p = root / name
        _write(p, f"{name} original\n")
        sr.take(SID, f"ws/{name}", "edit_file", "s", "d", abs_for, turn="t-1")
        _write(p, f"{name} edited\n")

    cp = sr.checkpoints(SID)[0]
    assert cp["files"], "the checkpoint captured both turns' originals"
    got = sr.restore_checkpoint(SID, cp["id"], abs_for)
    assert got["ok"], got
    assert _read(root / "a.py") == "a.py original\n", (
        f"restore came up empty: {got}")


def test_clear_pending_keeps_checkpoints_and_drops_the_marks(ws):
    """Adding a folder can move the base every client path is written against,
    so the pending marks go — but the named undo history is not the thing that
    broke, and throwing it away is a penalty for something the user did right."""
    root, abs_for = ws
    (root / "m.py").write_text("v1\n", encoding="utf-8")
    sr.take(SID, "ws/m.py", "edit_file", "s", "d", abs_for, turn="t-1")
    assert sr.list_entries(SID)
    assert sr.checkpoints(SID)

    sr.clear_pending(SID)
    assert sr.list_entries(SID) == []
    assert sr.checkpoints(SID), "clear_pending must not eat the undo history"


def test_two_hops_on_one_file_review_as_one_change(ws):
    """The entry stores each hop's own patch; what the user reviews is the FILE.

    Live 2026-11-06: a turn that edited line 5 and then line 15 of the same
    file listed "1 hunk +1 −1" in the panel, because the listing read the
    stored diff — the last hop's patch — instead of what separates the file
    from its baseline. The second edit was invisible, and the file's own
    Revert button was going to undo both.
    """
    root, abs_for = ws
    f = root / "m.py"
    original = "\n".join(f"line{i}" for i in range(1, 21)) + "\n"
    _write(f, original)
    sr.take(SID, "ws/m.py", "edit_file", "s", "", abs_for, turn="t-hops")

    first = original.replace("line5", "FIVE")
    _write(f, first)
    sr.take(SID, "ws/m.py", "edit_file", "s",
            diff_of(original, first), abs_for, turn="t-hops")

    second = first.replace("line15", "FIFTEEN")
    _write(f, second)
    sr.take(SID, "ws/m.py", "edit_file", "s",
            diff_of(first, second), abs_for, turn="t-hops")

    row = sr.changes_payload(SID, "ws/m.py", abs_for)["changes"][0]
    assert len(row["hunks"]) == 2, (
        f"the listing showed {len(row['hunks'])} hunk(s) for a two-edit turn: "
        "one edit was unreviewable and Revert would have taken both")
    assert row["added"] == 2 and row["removed"] == 2
    assert row["baseline"] == original

    # Deciding them one at a time still works, in either order.
    got = sr.apply_hunk(SID, "ws/m.py", abs_for,
                        hunk_id=row["hunks"][1]["id"], action="revert")
    assert got["ok"], got
    assert got["hunks_remaining"] == 1
    assert "line15" in _read(f) and "FIVE" in _read(f)
