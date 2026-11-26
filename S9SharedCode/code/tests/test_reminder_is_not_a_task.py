"""A reminder is words to say, not work to run.

Live defect (`sch-14effc0f`, 2026-11-03 → 11-15): the recurring row stored
"remind me to stretch and drink water" as its `query`. Every two hours the job
re-ran that sentence, the agent dutifully called `schedule_task` again, and the
user received a scheduling CONFIRMATION — carrying `sch-388b780f`, an id that
had already fired once and been disabled — instead of the reminder itself.

Two layers are pinned here:
  1. `_reminder_text` classifies a reminder imperative and extracts the
     sentence the user should read, while refusing to swallow real work.
  2. `schedule()` turns a bare reminder into a nudge (message set, so
     `_is_nudge_only` is True and the DAG never runs).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import scheduler as S  # noqa: E402


# ── classification ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("query,expected", [
    ("remind me to stretch and drink water", "Stretch and drink water."),
    ("Remind me to take my medicine in 1 hour", "Take my medicine."),
    ("reminder to submit the report tomorrow", "Submit the report."),
    ("nudge me to stand up every 2h", "Stand up."),
    ("remind me to check the oven", "Check the oven."),
    ("please remind me to call mum at 5pm", "Call mum."),
    ("tell me to breathe", "Breathe."),
])
def test_a_reminder_is_reduced_to_its_sentence(query, expected):
    assert S._reminder_text(query) == expected


@pytest.mark.parametrize("query", [
    # Real work: the agent must go and find something out.
    "check the weather and notify me if temp exceeds 15c",
    "summarise yesterday console errors",
    "remind me to look up the price of gold",
    "whats_new surfaces=documents",
    "Board briefing",
    "",
])
def test_work_is_not_turned_into_a_nudge(query):
    assert S._reminder_text(query) == ""


def test_the_timing_is_stripped_because_when_carries_it():
    """The sentence arrives on time; repeating the delay reads wrong."""
    got = S._reminder_text("remind me to stretch in 1 hour")
    assert "1 hour" not in got.lower()
    assert got == "Stretch."


# ── schedule() stores a nudge ───────────────────────────────────────────────

@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Point the scheduler at a private in-memory store.

    `_load()` REBINDS `_SCHEDULES` from disk, so patching the dict alone is not
    enough - the loader itself is stubbed or every schedule() would reload the
    operator's real file."""
    monkeypatch.setattr(S, "_SCHEDULES", {})
    monkeypatch.setattr(S, "_HEAP", [])
    monkeypatch.setattr(S, "_load", lambda: None)
    monkeypatch.setattr(S, "_save", lambda: None)
    return S._SCHEDULES


def test_a_reminder_query_becomes_a_nudge(store):
    sid = S.schedule("remind me to stretch and drink water", "in 1h")
    job = S._SCHEDULES[sid]
    pol = job.get("notify_policy") or {}
    assert pol.get("message") == "Stretch and drink water.", \
        "the reminder text was not derived into a message"
    assert S._is_nudge_only(job), \
        "a reminder row would run the DAG and re-schedule itself"


def test_an_explicit_message_is_never_overwritten(store):
    sid = S.schedule("remind me to stretch", "in 1h",
                     message="Stand up and roll your shoulders.")
    pol = S._SCHEDULES[sid]["notify_policy"]
    assert pol["message"] == "Stand up and roll your shoulders."


def test_a_work_task_still_runs(store):
    sid = S.schedule("check the weather and notify me if it rains", "in 1h")
    job = S._SCHEDULES[sid]
    assert not S._is_nudge_only(job), \
        "a real task was downgraded to a nudge and would never run"


def test_the_recurring_shape_that_looped_cannot_recur(store):
    """The exact live row: every 2h, imperative query, no message."""
    sid = S.schedule("remind me to stretch and drink water", "every 2h")
    job = S._SCHEDULES[sid]
    assert job["recurring"]
    assert (job.get("notify_policy") or {}).get("message")
    assert S._is_nudge_only(job)