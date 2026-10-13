"""What actually runs when a reminder fires.

Every scheduled job used to run the full research DAG — planner, researcher,
formatter, action — so "take a break and refocus" produced a REPORT about
taking a break, after 20-30 seconds and four or five billed model calls. The
user had asked to be reminded.

The discriminator is the agent's own `message`. If the model wrote the words
to send, that text is the deliverable and running a pipeline to produce more
text is both slow and wrong. If it left `message` empty, the task really is
"go and do/find something", so the DAG is right.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def sched(tmp_path, monkeypatch):
    """The scheduler with its store, ledger and delivery isolated."""
    import scheduler

    monkeypatch.setattr(scheduler, "_SCHED_PATH",
                        tmp_path / "schedules.json", raising=False)
    monkeypatch.setattr(scheduler, "_SCHEDULES", {}, raising=False)
    monkeypatch.setattr(scheduler, "_HEAP", [], raising=False)
    return scheduler


def _spec(message="", query="take a break and refocus"):
    return {"id": "sch-1", "query": query, "enabled": True, "notify": True,
            "created": 0.0, "next_fire": 0.0, "recurring": None,
            "conversation_id": None,
            "notify_policy": ({"channel": "telegram", "message": message,
                               "lead_fire": 0.0, "lead_message": "",
                               "follow_up": "", "wait_for_ack": False,
                               "ack_timeout_s": 900, "snooze_s": 0,
                               "max_follow_ups": 0} if message else {})}


def test_the_discriminator_reads_the_agents_own_message(sched):
    assert sched._is_nudge_only(_spec(message="Time to stretch.")) is True
    assert sched._is_nudge_only(_spec(message="")) is False
    assert sched._is_nudge_only(_spec(message="   ")) is False


def test_a_nudge_runs_no_agent_and_spends_nothing(sched, monkeypatch):
    """The behaviour that was wrong: a report about taking a break."""
    import flow

    ran = []

    class _ExplodingExecutor:
        def run(self, *a, **k):
            ran.append(a)
            return "a report about taking a break"

    monkeypatch.setattr(flow, "Executor", _ExplodingExecutor)

    sent = []
    import notify
    monkeypatch.setattr(notify, "send_channel_message",
                        lambda ch, text, **k: sent.append(text) or
                        {"delivered": True})
    import agent_server as ag
    monkeypatch.setattr(ag, "_notif_add", lambda *a, **k: None)
    monkeypatch.setattr(ag, "_session_cost_breakdown", lambda *a, **k: None)

    sched._fire("sch-1", _spec(message="Time to stretch."))

    assert ran == [], "a nudge ran the research pipeline"
    assert sent == ["Time to stretch."], (
        f"the reminder text was not delivered verbatim: {sent}")


def test_a_real_task_still_runs_the_dag(sched, monkeypatch):
    """No authored message means the task genuinely has to be done."""
    import flow

    ran = []

    class _FakeExecutor:
        async def run(self, query, session_id=None, resume=False):
            ran.append(query)
            return "three cheap flights found"

    monkeypatch.setattr(flow, "Executor", _FakeExecutor)

    sent = []
    import agent_server as ag
    monkeypatch.setattr(ag, "_notif_add", lambda *a, **k: None)
    monkeypatch.setattr(ag, "_session_cost_breakdown", lambda *a, **k: None)
    monkeypatch.setattr(ag, "resolve_notify_chat",
                        lambda channel="telegram": "60123")
    monkeypatch.setattr(ag, "_notify_telegram",
                        lambda text, chat=None: sent.append(text) or True)

    sched._fire("sch-1", _spec(message="", query="find cheap flights"))

    assert ran == ["find cheap flights"], "a real task was skipped"
    assert sent and "three cheap flights found" in sent[0], (
        "the task's own result was not delivered")


def test_the_in_app_record_does_not_claim_a_failure_for_a_nudge(sched,
                                                                monkeypatch):
    """"(no answer)" on a job that was never meant to have one reads as a
    broken reminder."""
    import notify
    monkeypatch.setattr(notify, "send_channel_message",
                        lambda ch, text, **k: {"delivered": True})
    import agent_server as ag
    monkeypatch.setattr(ag, "_session_cost_breakdown", lambda *a, **k: None)

    entries = []
    monkeypatch.setattr(ag, "_notif_add",
                        lambda kind, text, sid="": entries.append(text))

    sched._fire("sch-1", _spec(message="Time to stretch."))

    assert entries and "delivered" in entries[-1], entries
    assert "(no answer)" not in entries[-1]


def test_the_tool_documents_when_to_set_a_message():
    """The routing rule has to be discoverable, or the model cannot follow it."""
    import inspect

    import mcp_server

    doc = inspect.getdoc(mcp_server.schedule_task) or ""
    assert "NUDGE" in doc, (
        "schedule_task does not tell the model that setting `message` skips "
        "the agent run, so every reminder pays for a research pipeline")
    assert "summarise" in doc.lower() or "research" in doc.lower(), (
        "the doc does not say when to LEAVE message empty, so the model may "
        "write a message for a task whose result matters")