"""A run's age and status must be truthful even when it has no graph.

24 rows in the live store have no `graph.json`. They were reported as
`updated: 0` with `updated_ago: null`, and the client rendered that as
"0s ago" — a confident age of zero seconds for runs up to 119 hours old. The
status pill fell back to `queued`, asserting work was waiting to start on runs
that had finished, or failed, hours ago. Because the list sorts by session
directory mtime while the pill used graph mtime, these rows sorted to the very
top of the page.

Nine of them carry `run_status.json` with `degraded: true`, so they were real
failed runs presented as fresh queued work — the worst combination, because it
invents both a lifecycle and a recency.

These tests drive the real endpoint against synthetic sessions rather than
asserting on source text.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agent_server
import persistence

HDR = {"X-Aria-Token": "test-token"}


@pytest.fixture()
def sessions(tmp_path, monkeypatch):
    """Point the session store at a temp dir; yield its root."""
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(persistence, "SESSIONS_ROOT", root)
    yield root
    monkeypatch.setattr(persistence, "SESSIONS_ROOT",
                        persistence.SESSIONS_ROOT)


def _make_graphless(root: Path, sid: str, age_days: float,
                    degraded: bool = False) -> Path:
    """A session directory with node files but no graph.json."""
    d = root / sid
    d.mkdir(parents=True)
    (d / "nodes").mkdir(exist_ok=True)
    old = time.time() - age_days * 86400
    if degraded:
        (d / "run_status.json").write_text(json.dumps({
            "degraded": True, "detail": ["researcher: failed"],
            "updated": old}), encoding="utf-8")
    # utime LAST: writing a file inside the directory bumps its mtime back to
    # now, which silently turns this fixture into a brand-new run.
    import os
    os.utime(d, (old, old))
    return d


def _make_with_graph(root: Path, sid: str, age_days: float,
                     status: str = "complete") -> Path:
    import networkx as nx
    d = root / sid
    d.mkdir(parents=True)
    g = nx.DiGraph()
    g.add_node("n1", skill="planner", status=status)
    (d / "graph.json").write_text(json.dumps(nx.node_link_data(g)),
                                  encoding="utf-8")
    old = time.time() - age_days * 86400
    import os
    # utime after writing, for the same reason as the graphless fixture.
    os.utime(d / "graph.json", (old, old))
    os.utime(d, (old, old))
    return d


@pytest.fixture()
def client():
    return TestClient(agent_server.app)


def _row(body, sid):
    return next(r for r in body["runs"] if r["session_id"] == sid)


def test_a_graphless_run_reports_a_real_age(client, sessions):
    _make_graphless(sessions, "s8-graphless", age_days=3.0)

    body = client.get("/api/runs/summary?limit=50", headers=HDR).json()
    row = _row(body, "s8-graphless")

    assert row["updated"], "updated stayed 0 for a session directory with an mtime"
    assert row["updated_ago"] is not None, (
        "no relative age, so the client has nothing but the falsy `updated` "
        "and renders '0s ago'")
    assert row["updated_ago"] > 2 * 86400, (
        f"age {row['updated_ago']}s does not reflect a 3-day-old directory")


def test_a_graphless_run_is_not_reported_as_live_work(client, sessions):
    """`has_graph: false` is what stops the pill claiming 'queued'."""
    _make_graphless(sessions, "s8-nograph", age_days=1.0)

    body = client.get("/api/runs/summary?limit=50", headers=HDR).json()
    row = _row(body, "s8-nograph")

    assert row["has_graph"] is False
    # No lifecycle may be asserted about a run with nothing recorded.
    assert row["status_counts"] == {}, row["status_counts"]


def test_a_run_with_a_graph_reports_has_graph_true(client, sessions):
    _make_with_graph(sessions, "s8-real", age_days=1.0)

    body = client.get("/api/runs/summary?limit=50", headers=HDR).json()
    row = _row(body, "s8-real")

    assert row["has_graph"] is True
    assert row["nodes"] == 1
    assert row["updated_ago"] and row["updated_ago"] > 0


def test_the_falsy_zero_trap_is_gone(client, sessions):
    """`updated` being 0 was the whole mechanism. Nothing in the summary may
    report an age of zero for a directory that has one."""
    _make_graphless(sessions, "s8-a", age_days=5.0)
    _make_with_graph(sessions, "s8-b", age_days=5.0)

    body = client.get("/api/runs/summary?limit=50", headers=HDR).json()
    for sid in ("s8-a", "s8-b"):
        row = _row(body, sid)
        assert row["updated"] > 0, f"{sid} still reports updated: 0"
        assert row["updated_ago"] > 0, f"{sid} still reports a zero age"


def test_degraded_graphless_runs_are_still_identifiable(client, sessions):
    """They must not be relabelled as queued, but the degradation itself has
    to remain discoverable — that is what run_status.json is for."""
    _make_graphless(sessions, "s8-degraded", age_days=2.0, degraded=True)

    body = client.get("/api/runs/summary?limit=50", headers=HDR).json()
    row = _row(body, "s8-degraded")

    assert row["has_graph"] is False
    assert row["updated_ago"] > 0
    # The sidecar is untouched by this change.
    st = json.loads((sessions / "s8-degraded" / "run_status.json")
                    .read_text(encoding="utf-8"))
    assert st["degraded"] is True
