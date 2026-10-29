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


# ── the end-of-run verdict, driven through the real Executor ────────────────
#
# The reader tests above hand-write `run_status.json`. These drive the REAL
# Executor with a scripted fake LLM, because the defect being pinned was in
# the verdict computation itself: a skipped node whose recovery replacement
# had completed still counted as "unanswered", and a produced document's
# receipt dropped the warning entirely.

class _FakeLLM:
    """Canned JSON per skill; `fail_calls` names per-skill call indexes."""

    def __init__(self, script: dict, fail_calls=()):
        def _queue(v):
            return list(v) if isinstance(v, list) else [v]
        self._queues = {k: _queue(v) for k, v in script.items()}
        self._fail_calls = set(fail_calls)
        self._n: dict[str, int] = {}

    def chat(self, prompt=None, *, messages=None, system=None, agent=None,
             session=None, **kw):
        skill = agent or "formatter"
        i = self._n.get(skill, 0)
        self._n[skill] = i + 1
        if (skill, i) in self._fail_calls:
            raise RuntimeError(
                "researcher returned no usable JSON: empty reply")
        q = self._queues.get(skill)
        payload = (q.pop(0) if len(q) > 1 else q[0]) if q else {
            "final_answer": f"answer from {skill}"}
        return {"text": json.dumps(payload), "provider": "fake",
                "input_tokens": 10, "output_tokens": 10}


def _run(query: str, fake: _FakeLLM, *, session_id: str) -> str:
    import asyncio
    from unittest import mock

    import mcp_runner

    async def _fake_run_with_tools(*, prompt=None, tools_payload=None,
                                   agent=None, session_id=None, **kw):
        reply = fake.chat(prompt=prompt, agent=agent, session=session_id)
        return {**reply, "tool_calls": []}

    with mock.patch.object(flow, "ensure_gateway", lambda: None), \
         mock.patch("skills.LLM", lambda: fake), \
         mock.patch.object(mcp_runner, "run_with_tools",
                           _fake_run_with_tools):
        return asyncio.run(
            flow.Executor().run(query, session_id=session_id))


_RECOVERED = {
    "planner": [
        {"nodes": [
            {"skill": "researcher", "inputs": [],
             "metadata": {"label": "a", "question": "facet A"}},
            {"skill": "researcher", "inputs": [],
             "metadata": {"label": "b", "question": "facet B"}},
            {"skill": "researcher", "inputs": [],
             "metadata": {"label": "c", "question": "facet C"}},
            {"skill": "formatter", "inputs": ["n:a", "n:b", "n:c"],
             "metadata": {}},
        ]},
        {"nodes": [
            {"skill": "researcher", "inputs": ["USER_QUERY"],
             "metadata": {"label": "fix", "question": "the missing facets"}},
            {"skill": "formatter", "inputs": ["n:fix"], "metadata": {}},
        ]},
    ],
    "researcher": {"question": "q", "findings": "evidence"},
    "formatter": {"final_answer": "Report complete."},
}


def test_a_recovered_branch_does_not_degrade_the_run(sessions_root,
                                                     monkeypatch):
    """Two of three researchers die, the recovery researcher succeeds. The
    originals are `skipped` only because the splice replaced them; calling
    that "this part of the question went unanswered" marked a complete report
    degraded and persisted it for every reload."""
    fake = _FakeLLM(_RECOVERED, fail_calls={("researcher", 0),
                                            ("researcher", 1)})
    answer = _run("make a report", fake, session_id="s8-recovered")

    assert "Report complete." in answer
    assert "incomplete" not in answer.lower()
    st = flow.read_run_status("s8-recovered")
    assert st.get("degraded") is False, st
    assert st.get("detail") == []


def test_a_replacement_that_fails_still_degrades(sessions_root, monkeypatch):
    """The recovery researcher dies too. The originals point at it through
    `replaced_by`, and that chain ends unanswered - so the hole is real."""
    fake = _FakeLLM(_RECOVERED, fail_calls={("researcher", 0),
                                            ("researcher", 1),
                                            ("researcher", 3)})
    answer = _run("make a report", fake, session_id="s8-still-hole")

    assert "incomplete" in answer.lower(), answer
    st = flow.read_run_status("s8-still-hole")
    assert st.get("degraded") is True, st
    assert any("researcher" in d for d in st.get("detail") or []), st


def test_a_clean_run_clears_a_stale_degraded_status(sessions_root,
                                                     monkeypatch):
    """The status file is per session, and a session can be re-run. A clean
    second run that leaves the first run's `degraded: true` on disk keeps the
    console warning about a branch that no longer exists."""
    flow._write_run_status("s8-rerun", degraded=True,
                           detail=["researcher: skipped"])
    assert flow.read_run_status("s8-rerun")["degraded"] is True

    fake = _FakeLLM({
        "planner": {"nodes": [
            {"skill": "formatter", "inputs": ["USER_QUERY"], "metadata": {}}]},
        "formatter": {"final_answer": "done"},
    })
    _run("hi", fake, session_id="s8-rerun")

    st = flow.read_run_status("s8-rerun")
    assert st.get("degraded") is False, st
    assert st.get("detail") == []


def test_a_rendered_document_receipt_carries_the_incomplete_note(
        sessions_root, monkeypatch):
    """A file was rendered, so the answer is the receipt - which used to drop
    the failed-branch warning on the floor. The warning lived only in the
    `elif failed` branch, and `if produced` is checked first."""
    script = {
        "planner": {"nodes": [
            {"skill": "researcher", "inputs": [],
             "metadata": {"label": "a", "question": "facet A"}},
            {"skill": "researcher", "inputs": [],
             "metadata": {"label": "b", "question": "facet B"}},
            {"skill": "formatter", "inputs": ["n:a", "n:b"], "metadata": {}},
        ]},
        "researcher": {"question": "q", "findings": "evidence"},
        "formatter": {
            "final_answer": "Report body.",
            "produced": [{
                "artifact": "art:" + "a" * 16,
                "filename": "Report.pdf",
                "format": "pdf",
                "stats": {"words": 1200, "sections": 4},
            }],
        },
    }
    fake = _FakeLLM(script, fail_calls={("researcher", 0)})
    answer = _run("make a report", fake, session_id="s8-doc-degraded")

    assert "Created 1 document" in answer, answer
    assert "**This answer is incomplete.**" in answer, answer
    assert "researcher" in answer, answer
    st = flow.read_run_status("s8-doc-degraded")
    assert st.get("degraded") is True, st


# ── the replacement chain, as a pure function ───────────────────────────────

def test_a_cancelled_run_is_not_recorded_as_degraded(sessions_root):
    """Stop leaves nodes `skipped`, but the operator's stop is not missing
    evidence. Persisting `degraded: true` would blame the user for their own
    cancellation - the console already shows `stopped` separately."""
    fake = _FakeLLM({
        "planner": {"nodes": [
            {"skill": "formatter", "inputs": ["USER_QUERY"], "metadata": {}}]},
        "formatter": {"final_answer": "never reached"},
    })
    calls = {"n": 0}

    def _cancel():
        calls["n"] += 1
        return calls["n"] > 1   # planner runs, formatter is stopped

    import asyncio
    from unittest import mock

    import mcp_runner

    async def _fake_run_with_tools(*, prompt=None, tools_payload=None,
                                   agent=None, session_id=None, **kw):
        reply = fake.chat(prompt=prompt, agent=agent, session=session_id)
        return {**reply, "tool_calls": []}

    with mock.patch.object(flow, "ensure_gateway", lambda: None), \
         mock.patch("skills.LLM", lambda: fake), \
         mock.patch.object(mcp_runner, "run_with_tools",
                           _fake_run_with_tools):
        asyncio.run(flow.Executor().run(
            "hi", session_id="s8-cancelled", should_cancel=_cancel))

    st = flow.read_run_status("s8-cancelled")
    assert st.get("degraded") is False, st
    assert st.get("detail") == []


def test_replacement_answered_follows_the_chain():
    from flow import Graph

    g = Graph()
    a = g.add_node("researcher", inputs=[])
    b = g.add_node("researcher", inputs=[])
    g.g.nodes[a].update(status="skipped", replaced_by=[b])
    g.g.nodes[b].update(status="complete")
    assert flow._replacement_answered(g, a) is True

    c = g.add_node("researcher", inputs=[])
    g.g.nodes[b].update(status="skipped", replaced_by=[c])
    g.g.nodes[c].update(status="failed")
    assert flow._replacement_answered(g, a) is False, (
        "a replacement that failed did not answer anything")


def test_a_skipped_node_with_no_replacement_is_unanswered():
    from flow import Graph

    g = Graph()
    a = g.add_node("researcher", inputs=[])
    g.g.nodes[a].update(status="skipped", error="absorbed")
    assert flow._replacement_answered(g, a) is False

