"""Tests for sleeps.py — the main agent's self-scheduled continuations."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sleeps  # noqa: E402


def test_compose_joins_goal_and_context():
    out = sleeps.compose("Write the report", "I researched X and found Y")
    assert out.startswith("Write the report")
    assert "I researched X and found Y" in out
    # No context -> just the goal
    assert sleeps.compose("Do the thing", "") == "Do the thing"
    assert sleeps.compose("Do the thing", "   ") == "Do the thing"


def test_arm_time_schedules_goal_plus_context(monkeypatch, tmp_path):
    monkeypatch.setenv("S9_STATE_DIR", str(tmp_path))
    captured = {}

    def _fake_schedule(query, when, **kw):
        captured["query"] = query
        captured["when"] = when
        captured["kw"] = kw
        return "sch-test123"

    import scheduler
    monkeypatch.setattr(scheduler, "schedule", _fake_schedule)

    rec = sleeps.arm(goal="Finish the report", when="in 1h",
                     context="I gathered the data", conversation_id="ct-1")
    assert rec["status"] == "armed"
    assert rec["trigger"] == "time"
    assert rec["schedule_id"] == "sch-test123"
    # The future planner gets BOTH goal and context in one query.
    assert "Finish the report" in captured["query"]
    assert "I gathered the data" in captured["query"]
    assert captured["when"] == "in 1h"


def test_arm_email_does_not_schedule(monkeypatch, tmp_path):
    monkeypatch.setenv("S9_STATE_DIR", str(tmp_path))

    import scheduler
    monkeypatch.setattr(scheduler, "schedule",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no")))

    rec = sleeps.arm(goal="Tell me when they write", email_from="boss@example.com")
    assert rec["status"] == "armed"
    assert rec["trigger"] == "email"
    assert rec["email_from"] == "boss@example.com"
    assert rec["schedule_id"] == ""


def test_list_and_cancel(monkeypatch, tmp_path):
    monkeypatch.setenv("S9_STATE_DIR", str(tmp_path))
    import scheduler
    monkeypatch.setattr(scheduler, "schedule", lambda *a, **k: "sch-x")
    cancelled = {}
    monkeypatch.setattr(scheduler, "cancel",
                        lambda sid: cancelled.setdefault("sid", sid) or True)

    rec = sleeps.arm(goal="G", when="in 1h", conversation_id="ct-1")
    assert any(r["id"] == rec["id"] for r in sleeps.list_sleeps())
    assert sleeps.cancel(rec["id"]) is True
    assert all(r["id"] != rec["id"] for r in sleeps.list_sleeps())
    assert cancelled.get("sid") == "sch-x"  # the scheduler job was cancelled too
    assert sleeps.cancel("nope") is False


def test_arm_bad_when_records_error(monkeypatch, tmp_path):
    monkeypatch.setenv("S9_STATE_DIR", str(tmp_path))

    def _boom(*a, **k):
        raise ValueError("unparseable delay: 'not-a-time'")

    import scheduler
    monkeypatch.setattr(scheduler, "schedule", _boom)
    rec = sleeps.arm(goal="G", when="not-a-time")
    assert rec["status"] == "error"
    assert "unparseable" in rec.get("error", "")


def test_graph_has_unfinished(monkeypatch, tmp_path):
    import persistence
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(persistence, "SESSIONS_ROOT", sessions)
    import scheduler

    def _write(sid, statuses):
        d = sessions / sid
        d.mkdir()
        nodes = [{"id": f"n{i}", "status": s} for i, s in enumerate(statuses)]
        (d / "graph.json").write_text(json.dumps({"nodes": nodes, "edges": []}))

    # Missing graph -> False
    assert scheduler._graph_has_unfinished("nope") is False
    # All complete -> False (so a scheduled follow-up runs FRESH)
    _write("done", ["complete", "complete", "failed"])
    assert scheduler._graph_has_unfinished("done") is False
    # A pending node -> True (an interrupted run waiting to resume)
    _write("partial", ["complete", "pending"])
    assert scheduler._graph_has_unfinished("partial") is True
    _write("running", ["complete", "running"])
    assert scheduler._graph_has_unfinished("running") is True


def test_poll_due_fires_email_wake(monkeypatch, tmp_path):
    monkeypatch.setenv("S9_STATE_DIR", str(tmp_path))
    import scheduler
    scheduled = {}
    monkeypatch.setattr(scheduler, "schedule",
                        lambda q, w, **k: scheduled.setdefault("q", q) or "sch-poll")

    rec = sleeps.arm(goal="Tell me what they said", email_from="boss@example.com",
                     context="I was waiting for their reply", every_s=30)
    # No new mail -> no fire. (poll_due stamps last_poll, so reset it to
    # simulate the 30s interval elapsing before the next check.)
    monkeypatch.setattr(sleeps, "_gmail_has_new_from", lambda a, s: False)
    assert sleeps.poll_due() == 0
    sleeps._mark(rec["id"], last_poll=0.0)
    # New mail -> fires once (schedules a continuation ~5s out) and marks fired.
    monkeypatch.setattr(sleeps, "_gmail_has_new_from", lambda a, s: True)
    assert sleeps.poll_due() == 1
    assert "Tell me what they said" in scheduled.get("q", "")
    assert "I was waiting for their reply" in scheduled.get("q", "")
    assert sleeps.get(rec["id"])["status"] == "fired"
    # Already fired -> not polled again (status filter excludes it).
    assert sleeps.poll_due() == 0


def test_session_research_context_injects_findings(monkeypatch, tmp_path):
    """A scheduler-fired continuation must carry the session's research inline."""
    import json
    import persistence
    import scheduler
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(persistence, "SESSIONS_ROOT", sessions)

    sid = "s8-res"
    d = sessions / sid
    d.mkdir()
    graph = {"nodes": [
        {"id": "n:1", "skill": "planner", "status": "complete",
         "result": {"output": {}}},
        {"id": "n:2", "skill": "researcher", "status": "complete",
         "result": {"output": {"findings":
                               "RAG best practices: hybrid search, chunking, "
                               "reranking, evaluation."}}},
    ], "edges": []}
    (d / "graph.json").write_text(json.dumps(graph), encoding="utf-8")

    ctx = scheduler._session_research_context(sid)
    assert "RAG best practices" in ctx
    assert "PREVIOUS RESEARCH" in ctx
    # No research -> empty (a plain reminder is not burdened).
    assert scheduler._session_research_context("no-such-session") == ""