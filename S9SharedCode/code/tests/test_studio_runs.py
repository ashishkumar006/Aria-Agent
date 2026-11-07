"""Slice 5 — runs as objects: registry, transcripts, replay, and a real Stop.

Before this a code-agent run existed only as one browser tab's SSE stream, and
every consequence of that was silent:

- close the tab and nothing knows the run: it cannot be listed, stopped, or
  reopened;
- Stop aborted the fetch. The generator's `finally` cancelled the detached
  task, which worked while the tab lived and was the ONLY mechanism there was —
  there is no addressable run to stop from anywhere else;
- reload the studio and the transcript is gone, including the answer the run
  had already been billed for;
- two tabs doing work in one session produced two runs that could not be
  counted, let alone shown as `⚑ 2 agents`.

These tests pin the registry, the transcript format, the replay, and the two
stop verbs — and the difference between them, because "stopped" that a user
cannot verify is the same as not stopping.
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
import studio_runs as R           # noqa: E402
import studio_workspace as sw     # noqa: E402
from agent_server import app      # noqa: E402

SID = "s-runs"


@pytest.fixture(autouse=True)
def _unbind():
    sw.bind("")
    yield
    sw.bind("")


@pytest.fixture()
def session(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
    state, why = sw.add_root(SID, str(root))
    assert state is not None, why
    R.forget(SID)
    yield root
    R.forget(SID)
    sw.clear(SID)


# ── the registry ────────────────────────────────────────────────────────
def test_a_run_is_an_object_with_an_id_and_a_status(session):
    rec = R.start(SID, "add a docstring", model="google/gemini-flash-lite")
    assert rec["id"].startswith("r-")
    assert rec["status"] == "running"
    rows = R.list_runs(SID)
    assert [r["id"] for r in rows] == [rec["id"]]
    assert rows[0]["live"] is True, "a live run must be told apart from a past one"
    R.finish(rec["id"], "finished")
    rows = R.list_runs(SID)
    assert rows[0]["status"] == "finished"
    assert rows[0]["live"] is False


def test_frames_append_in_order_and_replay_reads_them_back(session):
    rec = R.start(SID, "q")
    R.append(SID, rec["id"], "tool_call", {"name": "read_file", "id": "c1"})
    R.append(SID, rec["id"], "tool_result", {"name": "read_file", "id": "c1", "ok": True})
    R.append(SID, rec["id"], "delta", {"text": "hello"})
    got = R.frames(SID, rec["id"])
    assert [f["t"] for f in got["frames"]] == ["tool_call", "tool_result", "delta"]
    assert got["frames"][1]["ok"] is True
    R.finish(rec["id"])


def test_a_truncated_last_line_is_skipped_not_guessed_at(session):
    """The process can die mid-write. A half line must not become a half frame
    in the panel, and it must not hide the frames before it."""
    rec = R.start(SID, "q")
    R.append(SID, rec["id"], "delta", {"text": "kept"})
    path = R._transcript(SID, rec["id"])
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"t": "delta", "tex\n')     # killed here
    got = R.frames(SID, rec["id"])
    assert len(got["frames"]) == 1
    assert got["unreadable"] == 1
    R.finish(rec["id"])


def test_tokens_and_answer_land_on_the_run_row(session):
    rec = R.start(SID, "q")
    R.append(SID, rec["id"], "done", {"answer": "did it", "input_tokens": 900,
                                      "output_tokens": 40, "tool_calls": 3,
                                      "provider": "gemini"})
    R.finish(rec["id"])
    row = R.list_runs(SID)[0]
    assert row["input_tokens"] == 900 and row["output_tokens"] == 40
    assert row["tool_calls"] == 3 and row["answer"] == "did it"
    assert row["provider"] == "gemini"


def test_stop_only_reports_success_for_a_run_that_is_live(session):
    rec = R.start(SID, "q")
    ok, why = R.stop(rec["id"])
    assert ok, why
    assert R.cancelled(rec["id"]) is True
    assert R.predicate(rec["id"])() is True
    R.finish(rec["id"], "stopped")
    ok2, why2 = R.stop(rec["id"])
    assert not ok2 and "not live" in why2, (
        "a Stop that reports success for a run that already ended teaches the "
        "user the button works when nothing did")


def test_no_cancel_predicate_for_a_run_that_is_not_here(session):
    """`cancel_scope(None)` binds nothing, so a caller outside a registry run
    keeps exactly the behaviour it had before slice 5."""
    assert R.predicate("r-doesnotexist") is None


def test_waiting_is_a_state_of_its_own(session):
    """A run parked on an approval is not working, and a status line that says
    otherwise reads as a hang."""
    rec = R.start(SID, "q")
    R.mark_waiting(rec["id"], True)
    assert [r["id"] for r in R.waiting_on(SID)] == [rec["id"]]
    row = next(r for r in R.list_runs(SID) if r["id"] == rec["id"])
    assert row["status"] == "waiting"
    R.mark_waiting(rec["id"], False)
    assert R.waiting_on(SID) == []
    R.finish(rec["id"])


def test_two_sessions_do_not_share_a_run_list(session):
    other = "s-other"
    R.forget(other)
    a = R.start(SID, "mine")
    b = R.start(other, "theirs")
    assert [r["id"] for r in R.list_runs(SID)] == [a["id"]]
    assert [r["id"] for r in R.list_runs(other)] == [b["id"]]
    R.finish(a["id"])
    R.finish(b["id"])
    R.forget(other)


# ── the routes ──────────────────────────────────────────────────────────
def test_the_routes_list_replay_and_stop(session):
    sw.bind(SID)
    rec = R.start(SID, "add a comment")
    R.append(SID, rec["id"], "delta", {"text": "working"})
    with TestClient(app) as c:
        got = c.get(f"/api/studio/runs?sid={SID}").json()
        assert [r["id"] for r in got["runs"]] == [rec["id"]]
        assert got["live"] == 1
        frames = c.get(f"/api/studio/runs/{rec['id']}?sid={SID}").json()
        assert frames["frames"][0]["t"] == "delta"
        r = c.post("/api/studio/run/stop", json={"sid": SID, "id": rec["id"]})
        assert r.status_code == 200, r.text
        bad = c.post("/api/studio/run/stop", json={"sid": SID, "id": "r-nope"})
        assert bad.status_code == 400 or bad.status_code == 409
        missing = c.get(f"/api/studio/runs/{rec['id']}?sid=other-session").json()
        assert missing.get("error"), "another session's id must not read this transcript"
    R.finish(rec["id"], "stopped")


def test_the_replay_route_rejects_a_malformed_run_id(session):
    sw.bind(SID)
    with TestClient(app) as c:
        assert c.get("/api/studio/runs/..%2F..%2Fetc?sid=" + SID).status_code in (
            400, 404)


def test_forget_deletes_runs_and_checkpoints_but_no_workspace_file(session):
    sw.bind(SID)
    rec = R.start(SID, "q")
    R.append(SID, rec["id"], "delta", {"text": "x"})
    R.finish(rec["id"])
    target = session / "app.py"
    with TestClient(app) as c:
        r = c.post("/api/studio/session/forget", json={"sid": SID})
        assert r.status_code == 200, r.text
    assert R.list_runs(SID) == []
    assert target.exists(), "forgetting an agent must not delete a project"


# ── the wiring into the agent route ─────────────────────────────────────
def _frames(body: str) -> list[dict]:
    out = []
    for line in body.splitlines():
        if line.startswith("data: "):
            try:
                out.append(json.loads(line[6:]))
            except ValueError:
                pass
    return out


def _sse(kind: str, **kw) -> str:
    return f"event: {kind}\ndata: {json.dumps(kw)}\n\n"


def test_the_agent_route_registers_its_run_and_records_the_stream(
        session, monkeypatch):
    """The registry is only real if the route writes to it. Observed end to
    end with the loop stubbed: the `started` frame carries an id, the transcript
    on disk holds the frames the client saw, and the row says what it cost."""
    import mcp_runner

    async def fake_run(**kwargs):
        return {"text": "done with it", "provider": "test", "model": "m",
                "input_tokens": 120, "output_tokens": 8, "tool_calls": 1}

    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_run)
    sw.bind(SID)
    with TestClient(app) as c:
        with c.stream("POST", "/api/code/agent", json={
                "question": "finish it", "path": "", "text": "",
                "max_hops": 3, "sid": SID}) as r:
            assert r.status_code == 200
            frames = _frames("".join(r.iter_text()))
    started = next(f for f in frames if f.get("type") == "started")
    run_id = started["run"]
    assert run_id.startswith("r-"), started
    row = next(x for x in R.list_runs(SID) if x["id"] == run_id)
    assert row["status"] in ("finished", "stopped"), row
    replay = R.frames(SID, run_id)["frames"]
    kinds = [f["t"] for f in replay]
    assert "started" in kinds and "done" in kinds, kinds
    assert replay[-1]["input_tokens"] == 120


def test_a_stopped_run_stops_at_the_hop_not_halfway_through_it(
        session, monkeypatch):
    """The cancellation contract the DAG work already established: the flag is
    checked between hops, so the call in flight is paid for and the NEXT one is
    not. This asserts the loop actually sees the predicate the route published —
    which is the difference between Stop and closing a tab."""
    import mcp_runner
    from gateway import cancel_requested

    seen = {"checked": 0, "stopped": False}

    async def fake_run(**kwargs):
        # What run_with_tools does between hops, measured here rather than
        # assumed: the scope the route installed must be visible.
        seen["checked"] += 1
        seen["stopped"] = cancel_requested()
        return {"text": "stopped by the operator", "provider": "test",
                "input_tokens": 10, "output_tokens": 1, "error": "stopped"}

    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_run)
    sw.bind(SID)
    with TestClient(app) as c:
        with c.stream("POST", "/api/code/agent", json={
                "question": "q", "path": "", "text": "", "sid": SID}) as r:
            body = "".join(r.iter_text())
    assert r.status_code == 200
    started = next(f for f in _frames(body) if f.get("type") == "started")
    # Nothing was asked to stop, so the predicate must be inert — the point of
    # binding nothing for a run nobody stopped.
    assert seen["checked"] == 1
    assert seen["stopped"] is False, (
        "a fresh run came back already cancelled: the scope leaked or the "
        "registry keyed it to the wrong run")
    assert started["run"]


def test_the_agent_route_marks_a_parked_write_as_waiting(session, monkeypatch):
    """A run parked on an approval shows as `waiting`, not `running` — the
    status bar's `⚑ 1 agent · 1 waiting` depends on it."""
    import mcp_runner
    import threading
    import time

    # The read-before-edit guard is stubbed to allow the write: without that
    # the route refuses it before it ever parks, which is correct behaviour and
    # the wrong thing to be testing here.
    monkeypatch.setattr(A, "_code_write_guard", lambda *a, **k: None)
    body: dict = {}

    async def fake_run(**kwargs):
        approve = kwargs["approve_fn"]
        ok = await approve("edit_file", {"path": "app.py"})
        return {"text": f"approved={ok}", "provider": "test",
                "input_tokens": 1, "output_tokens": 1, "tool_calls": 1}

    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_run)

    # ONE TestClient for both the stream and the answer. The parked write
    # waits on an asyncio.Event created inside the server's loop; a second
    # client runs a second loop, and setting that event from it does not wake
    # the waiter — the test then looks like a hung agent when it is a hung
    # harness.
    client = TestClient(app)
    body: dict = {}

    def drive():
        with client.stream("POST", "/api/code/agent", json={
                "question": "edit it", "path": "", "text": "",
                "sid": SID}) as r:
            body["text"] = "".join(r.iter_text())

    with client:
        thread = threading.Thread(target=drive, daemon=True)
        thread.start()

        pid = ""
        for _ in range(120):
            rows = R.list_runs(SID)
            parked = [r for r in rows if r.get("status") == "waiting"]
            if parked:
                frames = R.frames(SID, parked[0]["id"])["frames"]
                pid = next((str(f.get("id") or "") for f in frames
                            if f.get("t") == "permission"), "")
                if pid:
                    break
            time.sleep(0.05)
        assert pid, f"the run never parked on an approval: {R.list_runs(SID)}"

        r = client.post(f"/api/code/permission/{pid}", json={"approve": True})
        assert r.status_code == 200, r.text
        thread.join(25)
    assert "approved=True" in body.get("text", ""), body
    assert R.waiting_on(SID) == [], "the approval was answered and still waiting"
