"""Console event feed (Mission page) + the startup log tee.

`GET /api/events` merges run sessions, scheduler jobs and the server log
tail (logs/agent.out / logs/agent.err). The feed used to be permanently
missing its "server" source because nothing ever wrote those files; the
`_Tee` installed in `__main__` now mirrors stdout/stderr into them.

Run:  pytest tests/test_events_feed.py -q
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_server as ag
from fastapi.testclient import TestClient

LEVELS = {"run", "sched", "tool", "info", "err"}


def test_events_feed_shape_and_levels():
    client = TestClient(ag.app)
    r = client.get("/api/events?limit=50")
    assert r.status_code == 200
    data = r.json()
    assert "events" in data
    for ev in data["events"]:
        assert set(ev) >= {"t", "iso", "level", "src", "msg"}
        assert ev["level"] in LEVELS, f"unexpected level {ev['level']!r}"
        assert isinstance(ev["msg"], str) and len(ev["msg"]) <= 300


def test_events_feed_is_newest_first_and_bounded():
    client = TestClient(ag.app)
    r = client.get("/api/events?limit=7")
    evs = r.json()["events"]
    assert len(evs) <= 7
    ts = [e["t"] for e in evs]
    assert ts == sorted(ts, reverse=True), "feed must be newest-first"


def test_tee_mirrors_writes_and_survives_a_closed_file(tmp_path: Path):
    real_out = io.StringIO()
    log = tmp_path / "sub" / "agent.out"     # parent dir does not exist yet
    tee = ag._Tee(real_out, log)
    assert tee.write("hello\n") == 6
    tee.flush()
    assert real_out.getvalue() == "hello\n"  # primary stream still works
    assert log.read_text(encoding="utf-8") == "hello\n"
    # Nested dirs were created for the log.
    assert (tmp_path / "sub").is_dir()
    # Rotation: an oversized log is moved aside and a fresh one opened, so
    # the file can't grow without bound between restarts.
    big = tmp_path / "big.log"
    big.write_bytes(b"x" * 64)
    ag._Tee(io.StringIO(), big, cap_bytes=16)
    assert big.exists() and big.stat().st_size == 0          # fresh log
    assert (tmp_path / "big.log.1").read_bytes() == b"x" * 64  # rotated copy


def test_tee_is_a_transparent_stdout_proxy(tmp_path: Path):
    # REGRESSION (s8-36d8578d): every tool-using skill (researcher,
    # retriever, ...) failed instantly with
    #   AttributeError: '_Tee' object has no attribute 'fileno'
    # because the MCP stdio client's `errlog` default binds sys.stderr at
    # first lazy import (already teed), and anyio.open_process(stderr=...)
    # needs a real fd. The tee must therefore proxy the full TextIO
    # surface, not just write/flush/isatty.
    real_out = io.StringIO()
    tee = ag._Tee(real_out, tmp_path / "agent.out")
    with open(tmp_path / "real.txt", "w", encoding="utf-8") as real_fh:
        fd_tee = ag._Tee(real_fh, tmp_path / "agent2.out")
        assert fd_tee.fileno() == real_fh.fileno()  # real fd, not AttributeError
    assert tee.encoding == real_out.encoding
    assert tee.errors == real_out.errors
    assert tee.writable() is True
    tee.writelines(["a\n", "b\n"])   # must route through write, not bypass it
    assert real_out.getvalue() == "a\nb\n"
    assert (tmp_path / "agent.out").read_text(encoding="utf-8") == "a\nb\n"
    # Dunders must NOT delegate (copy/pickle/interpreter probing safety).
    import pytest as _pt
    with _pt.raises(AttributeError):
        tee.__wrapped__
    with _pt.raises(AttributeError):
        ag._Tee.__new__(ag._Tee).__getattr__("__deepcopy__")


def test_tee_is_installed_only_when_run_as_script():
    # Tests (and `uvicorn agent_server:app`) must NOT tee into the repo's
    # logs/ dir — only the `__main__` entrypoint installs the wrapper.
    assert not isinstance(sys.stdout, ag._Tee)
