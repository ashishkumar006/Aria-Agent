"""A run's verdict must survive the stream that delivered it.

Two facts about a finished run are known only at the end, and both used to
exist solely inside the SSE frame that carried them once:

  * "this answer is incomplete, here is what is missing" (a skipped or failed
    branch), and
  * "you stopped this before it finished".

The answer the console recovers on reload comes from the formatter node's own
`final_answer`, which never carries either notice. So reopening a degraded run
from /research or /runs showed a complete-looking report with no warning at
all - verified live: three runs whose live answer carried the note had no
trace of it in `/produced`, in `graph.json`, or in the (truncated) turn log.

The warning now also goes to disk, and `/produced` reports it.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import agent_server
import flow

HDR = {"X-Aria-Token": "test-token"}


@pytest.fixture()
def sessions_root(tmp_path, monkeypatch):
    root = tmp_path / "state" / "sessions"
    root.mkdir(parents=True)
    monkeypatch.setattr(flow, "_SROOT", tmp_path)
    return root


def _graph_with_formatter(sessions_root, sid, *, answer: str,
                          extra_nodes: dict | None = None):
    """A minimal graph whose last node produced `answer`."""
    import networkx as nx
    from schemas import AgentResult

    d = sessions_root / sid
    d.mkdir(parents=True, exist_ok=True)
    # Written through SessionStore.write_graph, which is what handles the
    # AgentResult -> dict conversion and the `_result_typed` marker that
    # read_graph uses to revive it. Hand-rolling node_link_data here produces
    # a file the reader cannot load, which the endpoint silently swallows.
    from persistence import SessionStore

    g = nx.DiGraph()
    g.add_node("n1", skill="formatter", status="complete",
               result=AgentResult(agent_name="formatter", success=True,
                                  output={"final_answer": answer}))
    g.add_node("n2", skill="researcher", status="complete")
    for nid, attrs in (extra_nodes or {}).items():
        g.add_node(nid, **attrs)
    SessionStore(sid, create=False).write_graph(g)
    return d


# ── the writer ──────────────────────────────────────────────────────────

def test_degraded_run_writes_its_status(sessions_root):
    flow._write_run_status("s8-degraded", degraded=True,
                           detail=["researcher: skipped (this part of the "
                                   "question went unanswered)"])
    got = flow.read_run_status("s8-degraded")
    assert got["degraded"] is True
    assert "researcher" in got["detail"][0]


def test_a_clean_run_records_no_degradation(sessions_root):
    flow._write_run_status("s8-clean", degraded=False, detail=[])
    got = flow.read_run_status("s8-clean")
    assert got["degraded"] is False
    assert got["detail"] == []


def test_status_write_never_raises_on_a_hostile_id(sessions_root):
    for bad in ("../escape", "a/b", "a\\b", ".hidden", ""):
        flow._write_run_status(bad, degraded=True, detail=["x"])
        assert flow.read_run_status(bad) == {}


def test_read_run_status_tolerates_corrupt_json(sessions_root):
    d = sessions_root / "s8-corrupt"
    d.mkdir(parents=True, exist_ok=True)
    (d / "run_status.json").write_text("{not json", encoding="utf-8")
    assert flow.read_run_status("s8-corrupt") == {}


def test_read_run_status_of_an_unknown_session_is_empty(sessions_root):
    assert flow.read_run_status("s8-never-ran") == {}


# ── the reader ──────────────────────────────────────────────────────────

def test_produced_reports_the_degradation(sessions_root, monkeypatch):
    import persistence
    monkeypatch.setattr(persistence, "SESSIONS_ROOT", sessions_root)

    _graph_with_formatter(sessions_root, "s8-part", answer="Here is the report.")
    flow._write_run_status("s8-part", degraded=True,
                           detail=["researcher: skipped"])

    client = TestClient(agent_server.app)
    r = client.get("/api/sessions/s8-part/produced", headers=HDR)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["degraded"] is True, (
        "a run that lost a branch reloads looking complete")
    assert body["incomplete_detail"], "the missing part is not named"
    assert body["answer"].startswith("Here is the report")


def test_a_complete_run_reports_not_degraded(sessions_root, monkeypatch):
    import persistence
    monkeypatch.setattr(persistence, "SESSIONS_ROOT", sessions_root)

    _graph_with_formatter(sessions_root, "s8-full", answer="All done.")
    flow._write_run_status("s8-full", degraded=False, detail=[])

    client = TestClient(agent_server.app)
    body = client.get("/api/sessions/s8-full/produced", headers=HDR).json()
    assert body["degraded"] is False
    assert body["incomplete_detail"] == []


def test_a_run_with_no_status_file_is_not_marked_degraded(sessions_root,
                                                           monkeypatch):
    """Runs written before this existed must not all claim to be degraded."""
    import persistence
    monkeypatch.setattr(persistence, "SESSIONS_ROOT", sessions_root)

    _graph_with_formatter(sessions_root, "s8-legacy", answer="Report.")

    client = TestClient(agent_server.app)
    body = client.get("/api/sessions/s8-legacy/produced", headers=HDR).json()
    assert body["degraded"] is False


def test_a_stopped_run_is_recognisable_on_reload(sessions_root, monkeypatch):
    """Cancelled runs left skipped nodes and no answer; the console needs to
    say stopped, not green-done."""
    import persistence
    monkeypatch.setattr(persistence, "SESSIONS_ROOT", sessions_root)

    _graph_with_formatter(
        sessions_root, "s8-stopped", answer="",
        extra_nodes={"n3": {"skill": "formatter", "status": "skipped"}})

    client = TestClient(agent_server.app)
    body = client.get("/api/sessions/s8-stopped/produced", headers=HDR).json()
    assert body["stopped"] is True
    assert body["answer"] == ""
