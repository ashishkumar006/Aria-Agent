"""Auto-apply writes must leave a reviewable, revertable trace.

The console sends `auto_approve: true`: the write lands without a dialog, and
the safety net moves to the review step - Keep drops the snapshot, Revert
restores the bytes as they were before the agent FIRST touched the file. These
tests pin the store that net hangs on:

- the snapshot is taken BEFORE the write, and the first one wins: a second
  write to the same file must not overwrite the revert target, or Revert would
  undo only the last hop and silently keep the earlier ones
- a file the agent created reverts by DELETION, a file it edited reverts by
  restoring bytes
- a declined write drops only this attempt's snapshot - never someone else's
  pending review of the same file
- Keep resolves the review without touching the bytes on disk
"""
from pathlib import Path

import agent_server
import pytest


@pytest.fixture()
def ws(tmp_path):
    """A fake workspace in the shipped shape: base = parent, one root."""
    root = tmp_path / "ws"
    root.mkdir()
    old_base, old_allowed = agent_server._CODE_BASE, agent_server._CODE_ALLOWED
    old_review = dict(agent_server._CODE_REVIEW)
    agent_server._CODE_BASE = tmp_path
    agent_server._CODE_ALLOWED = (root.resolve(),)
    agent_server._CODE_REVIEW.clear()
    yield tmp_path, root
    agent_server._CODE_BASE, agent_server._CODE_ALLOWED = old_base, old_allowed
    agent_server._CODE_REVIEW.clear()
    agent_server._CODE_REVIEW.update(old_review)


def test_snapshot_is_taken_before_the_write_and_first_wins(ws):
    tmp_path, root = ws
    f = root / "m.py"
    f.write_bytes(b"v1\n")

    t1 = agent_server._code_review_take("ws/m.py", "edit_file", "s1", "d1")
    f.write_bytes(b"v2\n")
    t2 = agent_server._code_review_take("ws/m.py", "edit_file", "s2", "d2")
    f.write_bytes(b"v3\n")

    entry = agent_server._CODE_REVIEW["ws/m.py"]
    assert entry["before"] == b"v1\n", (
        "the second write clobbered the revert target; Revert would keep v2")
    assert entry["diff"] == "d2", "the listing must show the latest change"
    # Same token: still one review item, and a decline of the LAST attempt
    # must not drop the FIRST snapshot.
    assert t1 == t2
    agent_server._code_review_drop_token("ws/m.py", t2)
    assert "ws/m.py" not in agent_server._CODE_REVIEW


def test_a_stale_decline_never_cancels_a_newer_review(ws):
    tmp_path, root = ws
    (root / "m.py").write_text("v1\n", encoding="utf-8")
    t_old = agent_server._code_review_take("ws/m.py", "edit_file", "s", "d")
    # Someone keeps the file, then a NEW write arrives and parks a new review.
    agent_server._CODE_REVIEW.pop("ws/m.py")
    t_new = agent_server._code_review_take("ws/m.py", "edit_file", "s2", "d2")
    assert t_new != t_old
    # The old run's decline finally arrives - it must not free the new entry.
    agent_server._code_review_drop_token("ws/m.py", t_old)
    assert "ws/m.py" in agent_server._CODE_REVIEW


def test_revert_restores_the_bytes(ws):
    tmp_path, root = ws
    f = root / "m.py"
    f.write_bytes(b"print('bye from Aria v4')\n")
    agent_server._code_review_take(
        "ws/m.py", "edit_file", "s", "d")
    f.write_bytes(b"print('ruined')\n")

    entry = agent_server._CODE_REVIEW.pop("ws/m.py")
    ok, err = agent_server._code_review_restore(entry)
    assert ok, err
    assert f.read_bytes() == b"print('bye from Aria v4')\n"


def test_revert_deletes_a_file_the_agent_created(ws):
    tmp_path, root = ws
    agent_server._code_review_take("ws/new.py", "create_file", "s", "d")
    f = root / "new.py"
    f.write_text("created\n", encoding="utf-8")

    entry = agent_server._CODE_REVIEW.pop("ws/new.py")
    ok, err = agent_server._code_review_restore(entry)
    assert ok, err
    assert not f.exists(), "reverting a created file must remove it"


def test_revert_removes_an_empty_folder_the_agent_created(ws):
    tmp_path, root = ws
    agent_server._code_review_take("ws/newpkg", "make_dir", "s", "")
    d = root / "newpkg"
    d.mkdir()

    entry = agent_server._CODE_REVIEW.pop("ws/newpkg")
    ok, err = agent_server._code_review_restore(entry)
    assert ok, err
    assert not d.exists(), "an empty created folder reverts by removal"


def test_revert_refuses_a_non_empty_created_folder(ws):
    tmp_path, root = ws
    agent_server._code_review_take("ws/newpkg", "make_dir", "s", "")
    d = root / "newpkg"
    d.mkdir()
    (d / "keep.py").write_text("x\n", encoding="utf-8")

    entry = agent_server._CODE_REVIEW.pop("ws/newpkg")
    ok, err = agent_server._code_review_restore(entry)
    assert not ok and "not empty" in err
    assert d.exists(), "files inside are their own review items; never deleted"


def test_keep_keeps_the_bytes_and_drops_the_snapshot(ws):
    tmp_path, root = ws
    f = root / "m.py"
    f.write_text("v1\n", encoding="utf-8")
    agent_server._code_review_take("ws/m.py", "edit_file", "s", "d")
    f.write_text("v2\n", encoding="utf-8")

    assert agent_server._CODE_REVIEW.pop("ws/m.py", None) is not None
    assert f.read_text(encoding="utf-8") == "v2\n"


def test_unresolvable_paths_snapshot_to_nothing(ws):
    tmp_path, root = ws
    assert agent_server._code_review_take("", "edit_file", "s", "d") is None
    assert agent_server._code_review_take(
        "ws/../outside.py", "edit_file", "s", "d") is None
    # A directory is not a reviewable write target either.
    (root / "pkg").mkdir()
    assert agent_server._code_review_take(
        "ws/pkg", "edit_file", "s", "d") is None
    assert agent_server._CODE_REVIEW == {}


def test_restore_refuses_when_the_path_left_the_workspace(ws):
    tmp_path, root = ws
    ok, err = agent_server._code_review_restore(
        {"path": "ws/../outside.py", "existed": True, "before": b"x"})
    assert not ok and err


def test_snapshot_eviction_is_bounded(ws, monkeypatch):
    tmp_path, root = ws
    monkeypatch.setattr(agent_server, "_CODE_REVIEW_MAX", 2)
    for name in ("a.py", "b.py", "c.py"):
        (root / name).write_text("x\n", encoding="utf-8")
        agent_server._code_review_take(f"ws/{name}", "edit_file", "s", "d")
    assert len(agent_server._CODE_REVIEW) == 2
    assert "ws/c.py" in agent_server._CODE_REVIEW


# ── phantom entries: a write that never landed must not be reviewable ────────

def test_a_failed_write_leaves_no_review_entry(ws):
    """The snapshot is taken BEFORE dispatch, so a failed write used to leave
    an entry the panel listed and Revert 'restored' - to the state that was
    already there."""
    tmp_path, root = ws
    (root / "m.py").write_text("v1\n", encoding="utf-8")
    token = agent_server._code_review_take("ws/m.py", "edit_file", "s", "d")
    # The tool raised; the file is untouched.
    agent_server._code_review_settle("ws/m.py", token)
    assert "ws/m.py" not in agent_server._CODE_REVIEW


def test_a_noop_write_is_not_a_reviewable_change(ws):
    """`update_file` with identical bytes is a success, not a change."""
    tmp_path, root = ws
    (root / "m.py").write_text("same\n", encoding="utf-8")
    token = agent_server._code_review_take("ws/m.py", "update_file", "s", "d")
    agent_server._code_review_settle("ws/m.py", token)
    assert "ws/m.py" not in agent_server._CODE_REVIEW


def test_settle_keeps_an_earlier_change_still_on_disk(ws):
    """A later failed write must not cancel an earlier unreviewed one."""
    tmp_path, root = ws
    f = root / "m.py"
    f.write_text("v1\n", encoding="utf-8")
    before = f.read_bytes()
    token = agent_server._code_review_take("ws/m.py", "edit_file", "s", "d")
    f.write_text("v2\n", encoding="utf-8")        # an earlier write landed
    agent_server._code_review_settle("ws/m.py", token)   # this one failed
    entry = agent_server._CODE_REVIEW.get("ws/m.py")
    assert entry is not None and entry["before"] == before


def test_settle_keeps_a_file_the_agent_created(ws):
    tmp_path, root = ws
    token = agent_server._code_review_take("ws/new.py", "create_file", "s", "d")
    (root / "new.py").write_text("created\n", encoding="utf-8")
    agent_server._code_review_settle("ws/new.py", token)
    entry = agent_server._CODE_REVIEW.get("ws/new.py")
    assert entry is not None and entry["existed"] is False
