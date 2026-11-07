"""Slice 7 — the dock: a shell inside the session, and the log the agent made.

Two things the dock promises that the editor cannot vouch for:

- **a terminal in THIS folder, and only in the session's folders.** A PTY is
  the widest capability in the product — it runs whatever is typed, with the
  agent's own file permissions. The `?cwd=` query used to be answered with
  `os.path.isdir()`, which means any directory on the machine. Confining the
  editor while leaving that open would have made the confinement decorative.
- **the command output a person reads is the output the shell printed.** The
  model's `tool_result` frame is clipped to 1500 chars for prompt reasons; the
  transcript now records the command's own text at a human size, and says when
  it had to cut.
"""
from __future__ import annotations

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

SID = "s-dock"


@pytest.fixture(autouse=True)
def _unbind():
    sw.bind("")
    yield
    sw.bind("")


@pytest.fixture()
def session(tmp_path, monkeypatch):
    a = tmp_path / "proj"
    (a / "src").mkdir(parents=True)
    (a / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    state, why = sw.add_root(SID, str(a))
    assert state is not None, why
    R.forget(SID)
    yield a, outside
    R.forget(SID)
    sw.clear(SID)


# ── where a shell is allowed to start ─────────────────────────────────
def test_a_shell_may_start_inside_a_session_folder(session):
    sw.bind(SID)
    a, _ = session
    got = A._terminal_cwd_ok(str(a / "src"))
    assert got and Path(got).resolve() == (a / "src").resolve()


def test_a_shell_may_not_start_anywhere_else(session):
    """The check that used to be here was `os.path.isdir`, and every directory
    on this machine passes that."""
    sw.bind(SID)
    _a, outside = session
    assert A._terminal_cwd_ok(str(outside)) is None
    assert A._terminal_cwd_ok("C:\\Windows\\System32" if sys.platform == "win32"
                              else "/etc") is None


def test_a_parent_traversal_in_the_cwd_is_refused(session):
    sw.bind(SID)
    a, _ = session
    assert A._terminal_cwd_ok(str(a / "src" / ".." / "..")) is None


def test_a_symlink_out_of_the_root_is_refused(session):
    """`resolve()` is what makes this hold: joining the raw string would say
    the path is inside the root because the LINK is."""
    import os
    sw.bind(SID)
    a, outside = session
    link = a / "escape"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("this user cannot create symlinks")
    assert A._terminal_cwd_ok(str(link)) is None


def test_a_broken_or_absurd_cwd_is_refused_not_crashed(session):
    sw.bind(SID)
    for bad in ("", "   ", "\x00", "not a path" * 400):
        assert A._terminal_cwd_ok(bad) is None


def test_the_shell_is_started_at_the_checked_path(session, monkeypatch):
    """End to end through the handshake: what `PtySession` is actually asked
    for. A guard on the helper that the route does not call is no guard."""
    sw.bind(SID)
    a, outside = session
    seen: dict = {}

    class FakePty:
        def __init__(self, shell, cwd=None, cols=80, rows=24):
            seen["cwd"] = cwd
            raise RuntimeError("stop here — the session was constructed")

        def is_alive(self):
            return False

    import terminal
    monkeypatch.setattr(terminal, "PtySession", FakePty)
    with TestClient(app) as c:
        with c.websocket_connect(f"/api/terminal?sid={SID}") as ws:
            try:
                ws.receive_text()
            except Exception:
                pass
    assert seen["cwd"] is None, "no cwd asked for, server default used"

    seen.clear()
    with TestClient(app) as c:
        with c.websocket_connect(
                f"/api/terminal?sid={SID}&cwd={outside}") as ws:
            try:
                ws.receive_text()
            except Exception:
                pass
    assert seen["cwd"] is None, (
        f"the outside folder reached the shell: {seen['cwd']!r}")

    seen.clear()
    with TestClient(app) as c:
        with c.websocket_connect(
                f"/api/terminal?sid={SID}&cwd={a / 'src'}") as ws:
            try:
                ws.receive_text()
            except Exception:
                pass
    assert seen["cwd"] and Path(seen["cwd"]).name == "src", seen


# ── the run's own log ─────────────────────────────────────────────────
def test_a_command_lands_in_the_transcript_with_its_full_output(
        session, monkeypatch):
    """The model sees a 1500-char clip because that is what a prompt can
    carry. A person reading the dock wants the shell's output, and the
    transcript is the only place that can still have it after a reload."""
    import mcp_runner
    _a, _o = session

    async def fake_run(**kwargs):
        outcome = kwargs["on_outcome"]
        outcome("run_command", {"command": "pytest -q"}, True,
                "line\n" * 30_000, 2.34)
        outcome("read_file", {"path": "src/app.py"}, True, "print('hi')\n", 0.1)
        return {"text": "done", "provider": "test", "input_tokens": 1,
                "output_tokens": 1, "tool_calls": 1}

    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_run)
    sw.bind(SID)
    with TestClient(app) as c:
        with c.stream("POST", "/api/code/agent", json={
                "question": "run the tests", "path": "", "text": "",
                "sid": SID}) as r:
            body = "".join(r.iter_text())
    assert '"type": "output"' in body or "type\":\"output" in body, body[:600]
    rows = R.list_runs(SID)
    assert rows, "the run was never registered"
    got = R.frames(SID, rows[0]["id"])
    out = [f for f in got["frames"] if f.get("t") == "output"]
    assert len(out) == 1, "only the command is an output line, not every tool"
    row = out[0]
    assert row["command"] == "pytest -q" and row["ok"] is True
    assert len(row["text"]) == 20_000, "the human clip is 20k, not the prompt clip"
    assert row["clipped"] is True and row["chars"] == 150_000, row  # "line\n" x 30k
    assert row["latency"] == pytest.approx(2.34)


def test_a_short_command_is_not_marked_clipped(session, monkeypatch):
    import mcp_runner

    async def fake_run(**kwargs):
        kwargs["on_outcome"]("run_command", {"command": "echo hi"}, True,
                            "hi\n", 0.02)
        return {"text": "ok", "provider": "test", "input_tokens": 1,
                "output_tokens": 1, "tool_calls": 1}

    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_run)
    sw.bind(SID)
    with TestClient(app) as c:
        with c.stream("POST", "/api/code/agent", json={
                "question": "echo", "path": "", "text": "", "sid": SID}) as r:
            "".join(r.iter_text())
    run_id = R.list_runs(SID)[0]["id"]
    out = [f for f in R.frames(SID, run_id)["frames"] if f.get("t") == "output"]
    assert out[0]["clipped"] is False and out[0]["text"] == "hi\n", out
