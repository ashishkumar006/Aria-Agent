"""The studio as one journey, not six slices.

Every other studio file tests a unit against its own refusals. This one does
what a person does, in order, and checks the state at the end:

    new session → two folders → a real agent turn with an @mention →
    hunks in review → Keep one / Revert one → the file on disk says so →
    checkpoint → restore → the undo of that restore →
    the run listed, replayable, its cost read from the ledger →
    a project-wide replace → undo it → a rename and a delete → restore them →
    the journal holds the human's actions as well as the agent's →
    two Ctrl+S from one buffer → a switched-off surface that still answers.

It exists because the defects in this surface were never inside one component.
They were at the seams: a body-only `sid` that bound nothing, a snapshot taken
on the wrong file's path, a pending set that showed one hunk for a two-edit
turn, a review list that looked empty right after the change that filled it.
None of those are visible in a unit test that stops at its own boundary.

The model call is stubbed, but the *writes are real*: the stub dispatches
through the route's own approval path, so the review entries, the hunks, the
checkpoints and the disk are produced by the same code a live run produces.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import agent_server as A          # noqa: E402
import studio_review as R         # noqa: E402
import studio_runs as RUNS        # noqa: E402
import studio_workspace as sw     # noqa: E402
from agent_server import app      # noqa: E402

SID = "s-journey"


@pytest.fixture(autouse=True)
def _clean():
    sw.bind("")
    RUNS.forget(SID)
    R.clear(SID)
    yield
    RUNS.forget(SID)
    R.clear(SID)
    sw.clear(SID)
    sw.bind("")


@pytest.fixture()
def project(tmp_path):
    """Two folders, so nothing in this run can pass by being single-root.

    `app.py` is long on purpose. Two edits inside six lines become ONE hunk in
    any real diff tool, because the three-line context overlaps — so a file
    that short cannot tell "one patch for two edits" apart from "the last
    hop's patch only". The edit sites below are a dozen lines apart, which is
    the only spacing that proves the review lists per-hunk state.
    """
    a = tmp_path / "alpha"
    b = tmp_path / "beta"
    (a / "src").mkdir(parents=True)
    b.mkdir()
    (a / "src" / "app.py").write_text(
        "def greet(name):\n"
        "    return 'hi ' + name\n"
        "\n\n"
        "def add(x, y):\n"
        "    return x + y\n"
        "\n\n"
        "def shout(text):\n"
        "    return text.upper()\n"
        "\n\n"
        "def slug(title):\n"
        "    return title.lower().replace(' ', '-')\n", encoding="utf-8")
    (a / "src" / "old.py").write_text("DEAD = 1\n", encoding="utf-8")
    (b / "tool.ts").write_text("export const OLD = 1;\n", encoding="utf-8")
    sw.clear(SID)
    for root in (a, b):
        state, why = sw.add_root(SID, str(root))
        assert state is not None, why
    return {"a": a, "b": b}


def _edits(path: str, find: str, replace: str) -> list:
    """The tool calls a real model would make, expressed as one turn.

    The read is not decoration. The route refuses an edit to a file this run
    has not seen — "editing unseen content is not allowed" — so a script that
    skipped the read scripts a turn no real run could ever have had, and every
    assertion downstream of it would be about an untouched file.
    """
    return [("read_file", {"path": path}),
            ("edit_file", {"path": path, "find": find, "replace": replace})]


@pytest.fixture()
def live_turn(monkeypatch, project):
    """Drive a turn through the route with the MODEL replaced by a script.

    Only the model is stubbed. The approval gate, the review snapshot, the
    file write, the verification pass and the run recorder are the route's own
    code, and the write lands through `mcp_server` — the same function the MCP
    child runs — with the child's sandbox pointed at this session's first
    folder. That is what makes the assertions below meaningful: the hunks, the
    checkpoints and the bytes on disk are produced by the pipeline, not by the
    test.
    """
    import mcp_runner
    import mcp_server as MS

    class _FakeWriteError(Exception):
        pass

    def _tool(name: str, args: dict) -> str:
        try:
            if name == "edit_file":
                return json.dumps(MS.edit_file(args.get("path", ""),
                                               args.get("find", ""),
                                               args.get("replace", ""),
                                               bool(args.get("replace_all"))))
            if name == "create_file":
                return json.dumps(MS.create_file(args.get("path", ""),
                                                 args.get("content", "")))
            if name == "read_file":
                return json.dumps(MS.read_file(args.get("path", ""),
                                               int(args.get("offset") or 1),
                                               int(args.get("limit") or 0)))
            if name == "delete_file":
                return json.dumps(MS.delete_file(args.get("path", "")))
            if name == "make_dir":
                return json.dumps(MS.make_dir(args.get("path", "")))
            return json.dumps({"ok": True, "note": f"{name} stubbed"})
        except Exception as e:
            return json.dumps({"error": f"{type(e).__name__}: {e}"})

    def _run(question: str, calls: list, *, auto: bool = True) -> dict:
        async def fake_run(**kwargs):
            gate = kwargs["approve_fn"]
            outcome = kwargs.get("on_outcome")
            event = kwargs.get("on_event")
            for name, args in calls:
                verdict = await gate(name, args)
                allowed = verdict is True
                if event is not None:
                    event("tool_call", {"name": name, "arguments": args,
                                        "id": f"c-{name}"})
                result = _tool(name, args) if allowed else str(verdict)
                ok = allowed and '"error"' not in result
                if event is not None:
                    event("tool_result", {"name": name, "ok": ok,
                                          "result" if ok else "error": result,
                                          "id": f"c-{name}"})
                if outcome is not None:
                    outcome(name, args, ok, result, 0.01)
            return {"text": "made the change", "provider": "test",
                    "input_tokens": 500, "output_tokens": 40,
                    "tool_calls": len(calls)}

        # The child's view of the workspace, pointed at this session.
        monkeypatch.setattr(MS, "SANDBOX", project["a"])
        monkeypatch.setattr(MS, "_roots", lambda: [project["a"], project["b"]])
        monkeypatch.setattr(mcp_runner, "run_with_tools", fake_run)
        sw.bind(SID)
        frames = []
        with TestClient(app) as c:
            with c.stream("POST", "/api/code/agent", json={
                    "question": question, "path": "", "text": "", "sid": SID,
                    "auto_approve": auto}) as r:
                body = "".join(r.iter_text())
        for chunk in body.split("\n\n"):
            line = [x for x in chunk.splitlines() if x.startswith("data:")]
            if line:
                try:
                    frames.append(json.loads(line[0][5:].strip()))
                except json.JSONDecodeError:
                    pass
        return {"frames": frames, "body": body,
                "kinds": [f.get("type") for f in frames],
                "run": next((f.get("run") for f in frames
                             if f.get("type") == "started"), "")}
    return _run


# ── the journey ──────────────────────────────────────────────────────
def test_step_1_the_session_reads_back_its_two_folders(project):
    state = sw.resolve(SID)
    assert state is not None
    _base, roots = state
    assert {p.name for p in roots} == {"alpha", "beta"}
    with TestClient(app) as c:
        got = c.get(f"/api/studio/roots?sid={SID}").json()
    assert {r["name"] for r in got["roots"]} == {"alpha", "beta"}


def test_step_2_a_turn_with_two_edits_lists_two_hunks_not_one(
        project, live_turn):
    """The live defect this step exists for: the pending set was the last
    hop's patch, so a turn that edited two places reported one hunk and the
    review list looked half-empty right after the work that filled it."""
    out = live_turn(
        "modernise the messages in @alpha/src/app.py",
        _edits("src/app.py", "return 'hi ' + name", "return 'hello ' + name")
        + _edits("src/app.py", "return title.lower().replace(' ', '-')",
                 "return title.strip().lower().replace(' ', '-')"))
    assert "done" in out["kinds"], out["kinds"]
    with TestClient(app) as c:
        changes = c.get(f"/api/studio/changes?sid={SID}").json()
    entries = _entries(changes)
    blob = json.dumps(changes)
    assert "hello" in blob and "title.strip()" in blob, blob[:400]
    hunks = [h for e in entries for h in (e.get("hunks") or [])]
    assert len(hunks) == 2, f"two edits a dozen lines apart, {len(hunks)} hunks: {blob[:400]}"
    assert len({h["id"] for h in hunks}) == 2, "each hunk addressed by its own content"
    assert [h["old_start"] for h in hunks] == sorted(h["old_start"] for h in hunks), \
        "hunks read top-down, like the diff the editor paints"


def _entries(payload: dict) -> list:
    """One shape for both forms `/api/studio/changes` answers with: a list of
    entries, or a map keyed by path. The route has been both; the tests should
    not have to care which."""
    got = payload.get("changes")
    if isinstance(got, dict):
        return list(got.values())
    return list(got or [])


def test_step_3_keep_one_and_revert_one_and_the_disk_agrees(
        project, live_turn):
    """Every decision addressed on its own, and the disk read back after each.

    Reverting the first hunk must leave the second one still pending: a tool
    that "reverts" by restoring the whole baseline gets the same end state at
    the end of the session and the wrong state in the middle, so the checks
    are in the middle. `keep` is then tested as what it actually is — a hunk
    folded into the baseline, file untouched — on a second turn, after the
    first turn's file has been put back exactly.
    """
    path = project["a"] / "src" / "app.py"
    pristine = path.read_text(encoding="utf-8")
    live_turn(
        "two changes to review",
        _edits("src/app.py", "return 'hi ' + name", "return 'hello ' + name")
        + _edits("src/app.py", "return title.lower().replace(' ', '-')",
                 "return title.strip().lower().replace(' ', '-')"))

    def _hunks(c):
        got = next((e for e in _entries(
            c.get(f"/api/studio/changes?sid={SID}").json())
            if e.get("path", "").endswith("app.py")), None)
        return (got or {}).get("hunks") or []

    def _decide(c, hunk_id, action):
        return c.post("/api/studio/changes/apply", json={
            "path": "alpha/src/app.py", "hunk_id": hunk_id,
            "action": action, "sid": SID}).json()

    with TestClient(app) as c:
        hs = _hunks(c)
        assert len(hs) == 2, json.dumps(hs)[:400]
        first, second = hs

        one = _decide(c, first["id"], "revert")
        assert one.get("ok"), one
        assert one.get("hunks_remaining") == 1, \
            f"reverting one hunk must not decide the other: {one}"
        mid = path.read_text(encoding="utf-8")
        assert "hi " in mid, "the reverted hunk is off the disk"
        assert "title.strip()" in mid, "the other hunk is still on the disk"

        two = _decide(c, second["id"], "revert")
        assert two.get("ok"), two
        assert two.get("hunks_remaining") == 0, two
    assert path.read_text(encoding="utf-8") == pristine, \
        "two reverts put the file back byte for byte"
    with TestClient(app) as c:
        assert _hunks(c) == [], "a fully decided change leaves the review list"

        # And now `keep`, on a file that is exactly as it started.
        assert "hello" not in pristine
    live_turn("one more, kept",
              _edits("src/app.py", "return text.upper()",
                     "return text.strip().upper()"))
    with TestClient(app) as c:
        kept = _hunks(c)
        assert len(kept) == 1, json.dumps(kept)[:400]
        decided = _decide(c, kept[0]["id"], "keep")
        assert decided.get("ok"), decided
        assert _hunks(c) == [], "a kept hunk has nothing left to review"
    text = path.read_text(encoding="utf-8")
    assert "text.strip().upper()" in text, "keep is an accept, not a no-op"
    assert "hi " in text, "the untouched lines are still the untouched lines"


def test_step_4_a_moved_hunk_is_refused_not_applied_nearby(project, live_turn):
    """The damage available here is a wrong line destroyed with no warning."""
    path = project["a"] / "src" / "app.py"
    live_turn("edit it", _edits("src/app.py", "return 'hi ' + name",
                                "return 'hello ' + name"))
    with TestClient(app) as c:
        entry = next(e for e in _entries(c.get(f"/api/studio/changes?sid={SID}").json())
                     if e.get("path", "").endswith("app.py"))
        hunk = entry["hunks"][0]
        # Someone else edits the same line under the review.
        moved = path.read_text(encoding="utf-8").replace(
            "return 'hello ' + name", "return 'howdy ' + name")
        path.write_text(moved, encoding="utf-8")
        refused = c.post("/api/studio/changes/apply", json={
            "path": "alpha/src/app.py", "hunk_id": hunk["id"],
            "action": "revert", "sid": SID})
    assert refused.status_code == 409, refused.text
    assert "moved" in json.dumps(refused.json()), refused.text
    assert path.read_text(encoding="utf-8") == moved, \
        "a refusal must not have edited the file either"


def test_step_5_checkpoints_and_the_undo_of_a_restore(project, live_turn):
    out = live_turn("change it", _edits("src/app.py", "return 'hi ' + name",
                                        "return 'hello ' + name"))
    with TestClient(app) as c:
        cps = c.get(f"/api/studio/checkpoints?sid={SID}").json()["checkpoints"]
        assert cps, out
        target = cps[0]
        assert target["files"] >= 1, target
        restored = c.post("/api/studio/checkpoint/restore",
                          json={"sid": SID, "id": target["id"]}).json()
        assert restored.get("ok"), restored
        assert restored.get("undo"), "restoring must itself be undoable"
    text = (project["a"] / "src" / "app.py").read_text(encoding="utf-8")
    assert "hi " in text and "hello" not in text
    back = TestClient(app).post("/api/studio/checkpoint/restore",
                                 json={"sid": SID, "id": restored["undo"]["id"]}).json()
    assert back.get("ok"), back
    assert "hello" in (project["a"] / "src" / "app.py").read_text(
        encoding="utf-8")


def test_step_6_the_run_is_listed_replayable_and_costed(project, live_turn,
                                                        monkeypatch):
    out = live_turn("what changed", _edits("src/old.py", "DEAD = 1",
                                           "DEAD = 2"))
    assert out["run"], out
    with TestClient(app) as c:
        runs = c.get(f"/api/studio/runs?sid={SID}").json()
        assert [r["id"] for r in runs["runs"]][:1] == [out["run"]]
        assert runs["runs"][0]["status"] in ("finished", "stopped"), runs
        replay = c.get(f"/api/studio/runs/{out['run']}?sid={SID}").json()
        kinds = [f["t"] for f in replay["frames"]]
        assert "started" in kinds and "done" in kinds, kinds
        started = next(f for f in replay["frames"] if f["t"] == "started")
        assert started["max_hops"] > 0 and started["gateway_session"]\
            .startswith("code-run-"), started
        monkeypatch.setattr(A, "_session_cost_breakdown",
                            lambda s: {"code": {"usd": 0.0031, "in_tok": 500,
                                                "out_tok": 40, "calls": 1}})
        cost = c.get(f"/api/studio/runs/{out['run']}/cost?sid={SID}").json()
    assert cost["usd"] == pytest.approx(0.0031) and cost["unknown"] is False
    assert cost["calls"] == 1


def test_step_7_replace_then_undo_then_rename_then_restore(project):
    """The human-initiated half of the studio, in the order a person would do
    it, with the undo checked at each step rather than at the end.

    No agent turn here on purpose: `old.py` must be exactly what the fixture
    wrote, or the byte-for-byte assertions below would be measuring whichever
    edit happened to land first."""
    with TestClient(app) as c:
        hits = c.get(f"/api/studio/search?sid={SID}&q=DEAD&regex=false").json()
        assert hits["count"] == 1, hits
        dry = c.post("/api/studio/replace", json={
            "sid": SID, "hits": hits["matches"], "pattern": "DEAD",
            "replacement": "ALIVE", "regex": False}).json()
        assert dry["dry_run"] and dry["count"] == 1, dry
        applied = c.post("/api/studio/replace", json={
            "sid": SID, "hits": hits["matches"], "pattern": "DEAD",
            "replacement": "ALIVE", "regex": False, "apply": True,
            "token": dry["token"]}).json()
        assert applied["ok"] and applied["checkpoint"], applied
    assert "ALIVE = 1" in (project["a"] / "src" / "old.py").read_text(
        encoding="utf-8")
    with TestClient(app) as c:
        c.post("/api/studio/checkpoint/restore",
               json={"sid": SID, "id": applied["checkpoint"]})
    assert "DEAD = 1" in (project["a"] / "src" / "old.py").read_text(
        encoding="utf-8"), "a replace is undoable as one act"

    with TestClient(app) as c:
        ops = c.post("/api/studio/files", json={"sid": SID, "ops": [
            {"op": "rename", "from": "alpha/src/old.py",
             "to": "alpha/src/kept.py"},
            {"op": "delete", "from": "beta/tool.ts"}]}).json()
    assert ops["applied"] == 2, ops
    assert not (project["b"] / "tool.ts").exists()
    assert (project["a"] / "src" / "kept.py").exists()
    with TestClient(app) as c:
        cps = c.get(f"/api/studio/checkpoints?sid={SID}").json()["checkpoints"]
        entry = next((x for x in cps if x["files"] >= 2), cps[0])
        back = c.post("/api/studio/checkpoint/restore",
                      json={"sid": SID, "id": entry["id"]}).json()
    assert back.get("ok"), back
    assert (project["b"] / "tool.ts").exists(), "the delete came back"


def test_step_8_the_journal_holds_both_authors(project, live_turn):
    """The agent's actions are in its transcript; the person's are in the
    journal. A question like "what changed my repository" needs both."""
    live_turn("touch it", _edits("src/old.py", "DEAD = 1", "DEAD = 3"))
    with TestClient(app) as c:
        c.post("/api/studio/files", json={"sid": SID, "ops": [
            {"op": "mkdir", "from": "alpha/src/docs"}]})
        rows = c.get(f"/api/studio/journal?sid={SID}").json()["entries"]
    kinds = [r["kind"] for r in rows]
    assert "files" in kinds, rows
    assert rows[0]["at"] >= rows[-1]["at"]


def test_step_9_a_second_session_cannot_see_or_touch_the_first(
        project, live_turn):
    """Confinement is per session, and it has to hold with two live sessions
    in one process — which is the whole reason the studio exists."""
    live_turn("edit", _edits("src/old.py", "DEAD = 1", "DEAD = 9"))
    other = "s-journey-other"
    sw.clear(other)
    RUNS.forget(other)
    with TestClient(app) as c:
        runs = c.get(f"/api/studio/runs?sid={other}").json()
        assert runs["runs"] == [], runs
        changes = c.get(f"/api/studio/changes?sid={other}").json()
        assert not (changes.get("changes") or {}), changes
        refused = c.get(f"/api/studio/mentions?sid={other}&q=old").json()
        assert refused["matches"] == [], refused
    sw.clear(other)
    RUNS.forget(other)


def test_step_10_no_workspace_means_refusals_not_fallbacks(tmp_path):
    """A session whose folders are gone must not start reading and writing the
    shipped playground. An empty workspace is the safe state, not the broken
    one."""
    orphan = "s-journey-orphan"
    sw.clear(orphan)
    sw.add_root(orphan, str(tmp_path / "gone"))
    (tmp_path / "gone").mkdir()
    import shutil
    shutil.rmtree(tmp_path / "gone")
    sw.bind(orphan)
    with TestClient(app) as c:
        got = c.get(f"/api/studio/mentions?sid={orphan}&q=app").json()
        files = c.get(f"/api/code/files?sid={orphan}").json()
        refused = c.get(f"/api/code/file?path=app.py&sid={orphan}").json()
    assert got["matches"] == [] or got.get("error"), got
    assert (files.get("files") or []) == [], files
    assert refused.get("error"), refused
    sw.clear(orphan)



def test_step_13_a_change_the_user_undid_themself_stops_being_counted(project, live_turn):
    """The mirror image of the half-empty pending set: an entry with nothing
    left in it must not keep being counted.

    Found in the browser, not in a unit test. The rail read "4 files
    unreviewed", every row offered `review`, and clicking one opened the editor
    in SOURCE mode with no hunks to decide and "+0 −0" where the numbers should
    have been — because the file had been put back by hand (a checkpoint
    restore, another tool, a git checkout) and nothing re-settled the entry.
    """
    path = project["a"] / "src" / "app.py"
    pristine = path.read_text(encoding="utf-8")
    live_turn("change it", _edits("src/app.py", "return 'hi ' + name",
                                  "return 'hello ' + name"))
    with TestClient(app) as c:
        listed = [e for e in _entries(c.get(f"/api/studio/changes?sid={SID}").json())
                  if e.get("path", "").endswith("app.py")]
        assert listed and listed[0]["hunks"], listed
        # Somebody else puts the file back. The agent is not involved.
        path.write_text(pristine, encoding="utf-8")
        after = [e for e in _entries(c.get(f"/api/studio/changes?sid={SID}").json())
                 if e.get("path", "").endswith("app.py")]
        assert not after, (
            f"the file matches its baseline again, so there is nothing to "
            f"review, yet it is still counted: {json.dumps(after)[:300]}")

def test_step_11_saving_twice_from_one_buffer_still_works(project):
    """Ctrl+S twice is the most ordinary thing an editor does, and the
    changed-on-disk guard used to make it impossible.

    The guard compares the mtime the buffer sends against the file. So a save
    has to hand back the stamp of what it just wrote — if it does not, the
    client has only its own clock to send, the second save 409s, and every
    save after that 409s forever. Found live in the browser, not in a unit
    test: the first save of `app.py` landed and the second was refused.
    """
    with TestClient(app) as c:
        sw.bind(SID)
        first = c.get(f"/api/code/file?sid={SID}&path=alpha/src/app.py").json()
        stamp = first.get("mtime")
        assert isinstance(stamp, (int, float)), \
            f"the read gave no stamp to save with: {list(first)}"

        saved = c.post(f"/api/code/write?sid={SID}", json={
            "sid": SID, "path": "alpha/src/app.py", "confirm": True, "mtime": stamp,
            "text": "# one\ndef greet(name):\n    return 'hi ' + name\n"}).json()
        assert saved.get("ok") and saved.get("written"), saved
        again = saved.get("mtime")
        assert isinstance(again, (int, float)), \
            f"a save that returns no mtime poisons the next one: {saved}"

        second = c.post(f"/api/code/write?sid={SID}", json={
            "sid": SID, "path": "alpha/src/app.py", "confirm": True, "mtime": again,
            "text": "# two\ndef greet(name):\n    return 'hi ' + name\n"}).json()
        assert second.get("ok"), \
            f"the second save of one buffer was refused: {second}"

        noop = c.post(f"/api/code/write?sid={SID}", json={
            "sid": SID, "path": "alpha/src/app.py", "confirm": True,
            "mtime": second["mtime"],
            "text": "# two\ndef greet(name):\n    return 'hi ' + name\n"}).json()
        assert noop.get("unchanged"), noop
        assert isinstance(noop.get("mtime"), (int, float)), \
            f"an unchanged save must still hand back a stamp: {noop}"

        # …and the guard still guards: something else touches the file.
        (project["a"] / "src" / "app.py").write_text("# someone else\n",
                                                     encoding="utf-8")
        raced = c.post(f"/api/code/write?sid={SID}", json={
            "sid": SID, "path": "alpha/src/app.py", "confirm": True,
            "mtime": noop["mtime"],
            "text": "# mine\ndef greet(name):\n    return 'hi ' + name\n"})
        assert raced.status_code == 409, raced.text
        assert "changed on disk" in json.dumps(raced.json()), raced.text
        assert "someone else" in (project["a"] / "src" / "app.py").read_text(
            encoding="utf-8"), "a refused save must not have written anything"


def test_step_12_a_route_the_console_polls_must_exist_whatever_the_flags_say(
        project):
    """`/api/studio/browser/status` answers even with `browser.tools` off,
    because the console asks unconditionally and a 404 there is a red console
    error on every focus of the composer — which is exactly how the cold
    studio looked broken while being fine. A switch that turns a surface off
    must still tell the truth about being off."""
    import flags as F
    assert F.is_enabled("browser.tools") is False, \
        "this step is about the off state; flip it back before running"
    with TestClient(app) as c:
        sw.bind(SID)
        got = c.get(f"/api/studio/browser/status?sid={SID}")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body.get("disabled") is True and body.get("running") is False, body
    assert "off" in str(body.get("note", "")).lower(), body
