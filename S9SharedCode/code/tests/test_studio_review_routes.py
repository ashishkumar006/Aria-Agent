"""Studio slice 4's HTTP surface: hunks you can click and checkpoints you can name.

The store's own behaviour is pinned in `test_code_agent_review.py`. What lives
here is the JOINT — what the studio panel actually receives, because every one
of these routes is a button, and a button that posts the wrong shape fails in
front of a user who has no way to tell a refused hunk from a broken feature.

Two rules these tests exist to keep honest:

- a hunk is addressed by `hunk_id`, and an id the server cannot find is a 409
  that writes NOTHING (the console shows "refresh and review what is left").
  The alternative is a review tool that edits a region nobody clicked.
- `keep` and `revert` are different verbs with different blast radius: keep
  never touches the file, revert always does. If a keep ever wrote, the user's
  own unsaved editor state and the disk would silently diverge.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import agent_server as A          # noqa: E402
import studio_review as R         # noqa: E402
import studio_workspace as sw     # noqa: E402
from agent_server import app      # noqa: E402

SID = "s-review"


@pytest.fixture(autouse=True)
def _unbind():
    sw.bind("")
    yield
    sw.bind("")


@pytest.fixture()
def session(tmp_path):
    """One session holding one real folder, with a file the agent has changed
    in two places — the smallest state in which "undo one hunk" means anything."""
    root = tmp_path / "proj"
    root.mkdir()
    original = "\n".join(f"line{i}" for i in range(1, 21)) + "\n"
    f = root / "m.py"
    f.write_text(original, encoding="utf-8")
    state, why = sw.add_root(SID, str(root))
    assert state is not None, why
    R.clear(SID)
    yield root, f, original
    R.clear(SID)
    sw.clear(SID)


def _bind(sid: str = SID):
    sw.bind(sid)


def _stage(diff_path: str = "proj/m.py", turn: str = "t-1") -> tuple[str, list[str]]:
    """Snapshot, change the file, record the diff — as one hop does. Returns
    (client path, hunk ids)."""
    _bind()
    f = A._code_agent_abs(diff_path)
    before = f.read_text(encoding="utf-8")
    A._code_review_take(diff_path, "edit_file", "s", "", turn=turn)
    after = before.replace("line5", "FIVE edited").replace("line15", "FIFTEEN edited")
    f.write_text(after, encoding="utf-8")
    import difflib
    d = "\n".join(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile=f"a/{diff_path}", tofile=f"b/{diff_path}", lineterm="", n=3))
    A._code_review_take(diff_path, "edit_file", "s", d, turn=turn)
    return diff_path, [h["id"] for h in R.hunks(R.get(SID, diff_path)["diff"])]


def test_changes_lists_hunks_with_ids_and_counts(session):
    path, ids = _stage()
    _bind()
    with TestClient(app) as c:
        got = c.get(f"/api/studio/changes?sid={SID}").json()
    row = next(r for r in got["changes"] if r["path"] == path)
    assert [h["id"] for h in row["hunks"]] == ids
    assert row["added"] == 2 and row["removed"] == 2
    assert row["created"] is False
    assert "baseline" not in row, "the list must not ship every snapshot in the session"


def test_changes_with_a_path_hands_back_the_baseline(session):
    """The diff view renders baseline against the buffer, so it needs the same
    text the server is measuring hunks against."""
    path, _ids = _stage()
    _bind()
    with TestClient(app) as c:
        got = c.get(f"/api/studio/changes?sid={SID}&path={path}").json()
    row = got["changes"][0]
    assert "line5" in row["baseline"] and "FIVE edited" not in row["baseline"]


def test_apply_revert_undoes_one_hunk_over_http(session):
    path, ids = _stage()
    _bind()
    with TestClient(app) as c:
        r = c.post("/api/studio/changes/apply",
                   json={"sid": SID, "path": path, "hunk_id": ids[0],
                         "action": "revert"})
        assert r.status_code == 200, r.text
        assert r.json()["hunks_remaining"] == 1
        now = session[1].read_text(encoding="utf-8")
    assert "line5" in now, "the clicked hunk is still applied"
    assert "FIFTEEN edited" in now, "the other hunk must survive"


def test_apply_keep_never_touches_the_file(session):
    path, ids = _stage()
    _bind()
    disk = session[1].read_bytes()
    with TestClient(app) as c:
        r = c.post("/api/studio/changes/apply",
                   json={"sid": SID, "path": path, "hunk_id": ids[0],
                         "action": "keep"})
        assert r.status_code == 200, r.text
        assert session[1].read_bytes() == disk, "keep wrote to the file"
        got = c.get(f"/api/studio/changes?sid={SID}&path={path}").json()
    row = got["changes"][0]
    assert [h["id"] for h in row["hunks"]] == [ids[1]], (
        "what stays pending is what is still unreviewed")
    assert "FIVE edited" in row["baseline"], "the kept hunk is in the baseline now"


def test_apply_of_a_moved_hunk_is_a_409_that_writes_nothing(session):
    path, ids = _stage()
    _bind()
    # The human rewrote that region after the agent left it.
    session[1].write_text(
        session[1].read_text(encoding="utf-8").replace("FIVE edited", "by hand"),
        encoding="utf-8")
    with TestClient(app) as c:
        r = c.post("/api/studio/changes/apply",
                   json={"sid": SID, "path": path, "hunk_id": ids[0],
                         "action": "revert"})
    assert r.status_code == 409, r.text
    assert "hunk-moved" in r.json()["error"]
    assert "by hand" in session[1].read_text(encoding="utf-8")


def test_apply_validates_its_body(session):
    path, ids = _stage()
    _bind()
    with TestClient(app) as c:
        assert c.post("/api/studio/changes/apply", json={}).status_code == 400
        assert c.post("/api/studio/changes/apply",
                      json={"path": path}).status_code == 400, "hunk_id required"
        assert c.post("/api/studio/changes/apply",
                      json={"path": path, "hunk_id": ids[0],
                            "action": "approve"}).status_code == 400
        r = c.post("/api/studio/changes/apply",
                   json={"path": "proj/nobody.py", "hunk_id": ids[0],
                         "action": "revert"})
        assert r.status_code == 404, r.text


def test_checkpoints_are_named_by_turn_over_http(session):
    _stage(turn="t-one")
    _stage(diff_path="proj/m.py", turn="t-two")
    _bind()
    with TestClient(app) as c:
        got = c.get(f"/api/studio/checkpoints?sid={SID}").json()
    names = [k["name"] for k in got["checkpoints"]]
    assert names[:2] == ["before task 2", "before task 1"], names


def test_restore_over_http_puts_the_turn_back_and_stays_undoable(session):
    path, _ids = _stage(turn="t-1")
    _bind()
    with TestClient(app) as c:
        cps = c.get(f"/api/studio/checkpoints?sid={SID}").json()["checkpoints"]
        r = c.post("/api/studio/checkpoint/restore",
                   json={"sid": SID, "id": cps[0]["id"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["restored"] == [path]
        assert session[1].read_text(encoding="utf-8").startswith("line1\nline2")
        assert "line5" in session[1].read_text(encoding="utf-8")
        assert "FIVE edited" not in session[1].read_text(encoding="utf-8")
        # Restoring cleared the review, and the restore itself is a checkpoint.
        assert c.get(f"/api/studio/changes?sid={SID}").json()["changes"] == []
        again = c.post("/api/studio/checkpoint/restore",
                       json={"sid": SID, "id": body["undo"]["id"]})
        assert again.status_code == 200, again.text
    assert "FIVE edited" in session[1].read_text(encoding="utf-8")


def test_restore_of_an_unknown_checkpoint_is_refused(session):
    _bind()
    with TestClient(app) as c:
        assert c.post("/api/studio/checkpoint/restore",
                      json={"sid": SID, "id": "cp-nope"}).status_code == 404
        assert c.post("/api/studio/checkpoint/restore",
                      json={"sid": SID}).status_code == 400


def test_the_legacy_route_still_reports_hunks(session):
    """`/code` is still the page a flag-off user sees. Its rail reads this
    route, so the ids are additive and nothing it used to get is gone."""
    path, ids = _stage()
    _bind()
    with TestClient(app) as c:
        got = c.get(f"/api/code/agent/changes?sid={SID}").json()
    row = got["changes"][0]
    assert {"path", "tool", "summary", "diff", "when", "created"} <= set(row)
    assert [h["id"] for h in row["hunks"]] == ids
