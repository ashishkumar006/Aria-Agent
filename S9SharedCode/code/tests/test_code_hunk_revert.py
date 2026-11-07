"""Hunk-level revert: undo one part of an agent change, keep the rest.

File-level Revert is all-or-nothing. In a turn that touched four files and got
three hunks right, the user's only lever was "throw away all four" — which is
the same as having no lever at all. This reverses ONE hunk of the diff the
server snapshotted, by searching for that hunk's own lines in the file, and
refuses when they are not there rather than guessing at a merge.

`seed()` stages each scenario the way the write path does it — snapshot, write,
snapshot again — because the store moved to disk in slice 4 and a test that
poked a dict full of keys the real code no longer reads would prove nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_server  # noqa: E402
import studio_review  # noqa: E402


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(agent_server, "_CODE_BASE", tmp_path)
    monkeypatch.setattr(agent_server, "_CODE_ALLOWED", (root.resolve(),))
    # "" is the bucket a caller that names no session writes into — this one.
    studio_review.clear("")
    yield root
    studio_review.clear("")


def _cp(rel):
    return agent_server._code_client_path(rel)


def _put(target: Path, text) -> None:
    """Bytes, not text mode: on Windows `write_text` rewrites every "\n" to
    "\r\n", and half these scenarios are about line endings already."""
    data = text if isinstance(text, bytes) else text.encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _read(target: Path) -> str:
    return target.read_bytes().decode("utf-8")


def _entry(client_path: str) -> dict:
    return studio_review.get("", client_path) or {}


def seed(client_path: str, pre, post, diff: str | None = None,
         tool: str = "edit_file") -> Path:
    """One file's review state: `pre` before the change, `post` after it.

    Two `_code_review_take` calls, which is what a hop does: the first
    snapshots the file as it stands (nothing at all for a created file) and
    the second is the write whose patch the panel then shows. `pre=None` means
    the file did not exist yet, and `diff` defaults to what separates `pre`
    from `post` — a hand-written patch in a fixture is how a test ends up
    asserting a hunk structure difflib would never produce."""
    target = agent_server._code_agent_abs(client_path)
    assert target is not None, client_path
    if pre is None:
        target.unlink(missing_ok=True)
    else:
        _put(target, pre)
    agent_server._code_review_take(client_path, tool, "s", "", turn="t-seed")
    if post is not None:
        _put(target, post)
    import difflib
    agent_server._code_review_take(client_path, tool, "s", diff if diff is not None
                                   else "\n".join(difflib.unified_diff(
                                       (pre or "").splitlines(),
                                       (post or "").splitlines(),
                                       fromfile=f"a/{client_path}",
                                       tofile=f"b/{client_path}",
                                       lineterm="", n=3)),
                                   turn="t-seed")
    return target


# Twenty lines, two edits far enough apart that a three-line context window
# cannot merge them into one hunk. A five-line file CAN only produce one, and
# the fixtures used to be five lines while asserting two hunks — the hand
# written patch below hid that. Zero-padded names on purpose: "line2" is a
# substring of "line20", and an un-padded fixture edits two lines by accident.
BEFORE = "\n".join(f"line{i:02d}" for i in range(1, 21)) + "\n"
AFTER = BEFORE.replace("line02", "LINE-TWO-EDIT").replace("line15", "LINE-FIFTEEN-EDIT")

# A hand-written patch, for the parser test alone: its content is what the
# parser is being shown, not something the store produced.
DIFF = (
    "--- a/ws/m.py\n"
    "+++ b/ws/m.py\n"
    "@@ -1,5 +1,5 @@\n"
    " alpha\n"
    "-bravo\n"
    "+BRAVO-EDIT\n"
    " charlie\n"
    " delta\n"
    "@@ -4,2 +4,3 @@\n"
    " delta\n"
    " echo\n"
    "+extra\n"
)


def test_hunks_split_into_their_own_line_blocks():
    hunks = agent_server._code_hunks(DIFF)
    assert len(hunks) == 2, hunks
    assert hunks[0]["old"] == ["alpha", "bravo", "charlie", "delta"]
    assert hunks[0]["new"] == ["alpha", "BRAVO-EDIT", "charlie", "delta"]
    # The second hunk is a pure insertion at the end: nothing removed.
    assert hunks[1]["old"] == ["delta", "echo"]
    assert hunks[1]["new"] == ["delta", "echo", "extra"]


def test_reverting_one_hunk_leaves_the_other_alone(workspace):
    key = _cp("m.py")
    f = seed(key, BEFORE, AFTER)

    ok, err, remaining = agent_server._code_revert_hunk(key, 0)
    assert ok, err
    assert remaining == 1
    out = _read(f)
    assert "line02" in out, out                      # hunk 0 undone
    assert "LINE-TWO-EDIT" not in out
    assert "LINE-FIFTEEN-EDIT" in out, out           # hunk 1 untouched


def test_reverting_every_hunk_ends_the_review(workspace):
    key = _cp("m.py")
    f = seed(key, BEFORE, AFTER)

    assert agent_server._code_revert_hunk(key, 1)[0] is True
    assert agent_server._code_revert_hunk(key, 0) == (True, "", 0)
    assert _read(f) == BEFORE
    assert not _entry(key), \
        "the file is back to the snapshot but the panel would still offer Revert"


def test_a_stale_hunk_id_is_refused_and_writes_nothing(workspace):
    """The protection slice 4 exists to provide, stated against the handle the
    studio actually uses.

    The panel listed a hunk; the human then rewrote that region and saved. The
    id the panel is holding is a fingerprint of text that is no longer there,
    so the apply is refused. Reverse-applying by LINE NUMBER instead — which is
    what the positional rail does — would have written over the human's own
    edit and reported success.
    """
    key = _cp("m.py")
    f = seed(key, BEFORE, AFTER)
    stale = studio_review.hunks(studio_review.pending_diff(
        _entry(key), agent_server._code_agent_abs(key)))[0]["id"]

    _put(f, AFTER.replace("LINE-TWO-EDIT", "I rewrote this line myself"))
    before_bytes = f.read_bytes()

    got = studio_review.apply_hunk("", key, agent_server._code_review_abs,
                                   hunk_id=stale, action="revert")
    assert not got["ok"] and got["code"] == 409, got
    assert "hunk-moved" in got["error"]
    assert f.read_bytes() == before_bytes, "a refused revert still wrote"
    assert _entry(key), "the review was dropped anyway"


def test_a_positional_revert_returns_the_region_to_the_baseline(workspace):
    """What the legacy rail's "undo hunk 0" means now, said out loud.

    The pending list is recomputed from the file, so after a human rewrites a
    region that region is what hunk 0 names — and reverting it takes the line
    back to the baseline. That is the honest reading of "undo this hunk" (the
    diff view shows exactly that region as it now stands), and it replaces the
    old behaviour of refusing everything the moment the file moved, which left
    the user with a rail that could not undo anything at all. The safe handle
    for a click that must not land on a moved region is `hunk_id`, above.
    """
    key = _cp("m.py")
    f = seed(key, BEFORE, AFTER)
    _put(f, AFTER.replace("LINE-TWO-EDIT", "I rewrote this line myself"))

    ok, err, remaining = agent_server._code_revert_hunk(key, 0)
    assert ok, err
    out = _read(f)
    assert "I rewrote this line myself" not in out
    assert "line02" in out, out
    assert "LINE-FIFTEEN-EDIT" in out, "the other region must survive"
    assert remaining == 1


def test_out_of_range_and_missing_entries_are_clean_errors(workspace):
    key = _cp("m.py")
    seed(key, BEFORE, AFTER)
    ok, err, _ = agent_server._code_revert_hunk(key, 9)
    assert ok is False and "out of range" in err
    ok2, err2, _ = agent_server._code_revert_hunk(_cp("nobody.py"), 0)
    assert ok2 is False and "no unreviewed change" in err2


def test_a_path_outside_the_workspace_is_refused(workspace):
    ok, err, _ = agent_server._code_revert_hunk("../escape.py", 0)
    assert ok is False


def test_the_stored_diff_describes_what_is_still_unreviewed(workspace):
    """Live 2026-11-02: after an undo the rail still showed BOTH hunks, with
    `+2 −2`, because the entry kept the patch as it was written. The panel then
    offered to undo a hunk that had already gone back.

    Read through `pending_diff`, which is what the panel is shown: the entry's
    own field is one hop's patch, and the pending set is the file against its
    baseline."""
    key = _cp("m.py")
    seed(key, BEFORE, AFTER)

    ok, err, remaining = agent_server._code_revert_hunk(key, 0)
    assert ok, err
    entry = _entry(key)
    pending = studio_review.pending_diff(
        entry, agent_server._code_agent_abs(key))
    hunks = agent_server._code_hunks(pending)
    assert len(hunks) == remaining == 1, (pending, remaining)
    assert "LINE-TWO-EDIT" not in pending, "the undone hunk is still listed"
    assert "LINE-FIFTEEN-EDIT" in pending, hunks


def test_the_last_undo_ends_the_review_even_when_line_endings_differ(workspace):
    """Live 2026-11-02: the file was byte-for-byte back to its content but the
    review row stayed, offering "Keep" for a change that no longer existed —
    the snapshot was CRLF and the undo rebuilt LF. What matters is whether any
    line still differs."""
    key = _cp("m.py")
    f = seed(key, b"one\r\ntwo\r\nthree\r\n", "one\nTWO\nthree\n",
             "--- a/ws/m.py\n+++ b/ws/m.py\n@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three",
             tool="update_file")

    ok, err, remaining = agent_server._code_revert_hunk(key, 0)
    assert ok, err
    assert remaining == 0
    assert not _entry(key), "an empty review row was left"
    assert _read(f) == "one\ntwo\nthree\n"


def test_undoing_the_only_hunk_of_a_created_file_removes_it(workspace):
    """The snapshot of a created file is 'not there', so that is where the last
    undo has to put it back — not an empty file nobody is reviewing."""
    key = _cp("new.py")
    f = seed(key, None, "line one\nline two\n",
             "--- a/ws/new.py\n+++ b/ws/new.py\n@@ -0,0 +1,2 @@\n+line one\n+line two",
             tool="create_file")

    ok, err, remaining = agent_server._code_revert_hunk(key, 0)
    assert ok, err
    assert remaining == 0
    assert not _entry(key)
    assert not f.exists(), "the undo left an empty file the agent had created"


def test_a_hunk_id_and_a_position_address_the_same_hunk(workspace):
    """Slice 4 made hunks clickable, which is only safe while the two ways of
    naming one cannot disagree about which region they mean."""
    key = _cp("m.py")
    f = seed(key, BEFORE, AFTER)
    hs = studio_review.hunks(_entry(key)["diff"])
    assert len(hs) == 2

    by_id = studio_review.apply_hunk("", key, agent_server._code_review_abs,
                                     hunk_id=hs[1]["id"], action="revert")
    assert by_id["ok"], by_id
    assert by_id["index"] == 1
    assert "LINE-FIFTEEN-EDIT" not in _read(f), "the wrong hunk was undone"
    # What is left is the FIRST hunk, so index 0 means the same thing to the
    # old rail that it means to the store now.
    ok, err, remaining = agent_server._code_revert_hunk(key, 0)
    assert ok, err
    assert remaining == 0
    assert _read(f) == BEFORE


def test_route_validates_its_body(workspace):
    from fastapi.testclient import TestClient
    from agent_server import app
    key = _cp("m.py")
    f = seed(key, BEFORE, AFTER)
    with TestClient(app) as client:
        assert client.post("/api/code/agent/revert-hunk",
                           json={"hunk": 0}).status_code == 400
        assert client.post("/api/code/agent/revert-hunk",
                           json={"path": key, "hunk": "x"}).status_code == 400
        r = client.post("/api/code/agent/revert-hunk",
                        json={"path": key, "hunk": 0})
        assert r.status_code == 200, r.text
        assert r.json()["hunks_remaining"] == 1
        # Index 0 now names the hunk that is left, so undoing it ends the
        # review rather than failing - the list the user clicked is the list
        # the server holds.
        r2 = client.post("/api/code/agent/revert-hunk",
                         json={"path": key, "hunk": 0})
        assert r2.status_code == 200, r2.text
        assert r2.json()["hunks_remaining"] == 0
        assert _read(f) == BEFORE
        assert not _entry(key)
        # A path nobody is reviewing is refused, not silently a no-op.
        r3 = client.post("/api/code/agent/revert-hunk",
                         json={"path": key, "hunk": 0})
        assert r3.status_code == 400, r3.text
        assert "no unreviewed change" in r3.json()["error"]
