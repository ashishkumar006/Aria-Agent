"""A reminder that was born disabled, and a transcript that vanished.

Both reported as "the scheduler is broken" and "the chat ate my message".

1. The model wrote `2025-10-12T09:00:00` for "tomorrow at 9am" — a year in
   the past. `fromisoformat` accepted it verbatim, the worker fired it on the
   next tick, and because every one-shot is disabled as it fires, the job
   appeared greyed out and consumed itself. The user found it disabled, asked
   the model to check, was told it was disabled, and asked for it to be
   enabled — and the model's only available move was to schedule a SECOND
   copy, leaving two identical disabled rows.

2. `cid` in Chat.tsx was plain component state. Navigating to another section
   unmounted the view, `cid` reset to '', and the transcript came back empty
   even though the thread store still had every message. An empty pane beside
   a full sidebar reads as data loss.
"""

from __future__ import annotations

import time

import pytest


@pytest.fixture()
def sched(tmp_path, monkeypatch):
    import scheduler

    monkeypatch.setattr(scheduler, "_SCHED_PATH",
                        tmp_path / "schedules.json", raising=False)
    monkeypatch.setattr(scheduler, "_SCHEDULES", {}, raising=False)
    monkeypatch.setattr(scheduler, "_HEAP", [], raising=False)
    monkeypatch.setattr(scheduler, "_ensure_worker", lambda: None,
                        raising=False)
    return scheduler


def _tomorrow_iso_hour(hour=9):
    import datetime

    t = (datetime.datetime.fromtimestamp(time.time())
         + datetime.timedelta(days=1)).replace(hour=hour, minute=0,
                                               second=0, microsecond=0)
    return t.isoformat()


# ── a past date is refused, not fired ────────────────────────────────────

def test_a_past_absolute_date_is_refused(sched):
    """The exact value the model produced. It was accepted, fired instantly,
    and disabled itself."""
    with pytest.raises(ValueError) as e:
        sched.schedule("Important meeting", "2025-10-12T09:00:00")
    assert "past" in str(e.value).lower(), str(e.value)
    assert "tomorrow" in str(e.value), (
        "the error must name a usable alternative or the model cannot correct "
        "itself")


def test_tomorrow_at_nine_is_accepted(sched):
    """The thing the user actually asked for must still work."""
    sid = sched.schedule("Important meeting", "tomorrow 9am")
    assert sched._SCHEDULES[sid]["enabled"] is True


def test_a_future_iso_datetime_is_accepted(sched):
    sid = sched.schedule("Important meeting", _tomorrow_iso_hour())
    assert sched._SCHEDULES[sid]["enabled"] is True


def test_a_recurring_job_is_exempt_from_the_past_check(sched):
    """"daily@09:00" rolls forward to tomorrow when today's has passed. That is
    the whole point of the form and must not be refused."""
    sid = sched.schedule("standup", "daily@09:00")
    assert sched._SCHEDULES[sid]["enabled"] is True


def test_a_refused_schedule_leaves_nothing_behind(sched):
    with pytest.raises(ValueError):
        sched.schedule("x", "2025-10-12T09:00:00")
    assert sched._SCHEDULES == {}, (
        "a refused schedule was persisted anyway")


# ── there is a way back from disabled ────────────────────────────────────

def test_a_disabled_schedule_can_be_re_enabled(sched):
    """The capability that was missing. Without it the model's only move was a
    duplicate."""
    sid = sched.schedule("morning briefing", "in 2h")
    assert sched.set_enabled(sid, False)["enabled"] is False
    assert sched._SCHEDULES[sid]["enabled"] is False

    out = sched.set_enabled(sid, True)
    assert out["ok"] is True
    assert sched._SCHEDULES[sid]["enabled"] is True


def test_re_enabling_does_not_fire_a_one_shot_whose_time_has_passed(sched):
    """The failure this function exists to undo must not be reproduced by it.

    Uses an ABSOLUTE past date, because a relative one ("in 1s") re-parses to
    the future when re-enabled, which is correct: a relative reminder means
    "this much from now", so re-enabling it should mean that again.
    """
    sid = sched._SCHEDULES.setdefault("sch-old", {})
    sid.update({"id": "sch-old", "query": "old", "when": "2025-10-12T09:00:00",
                "enabled": False, "notify": True, "recurring": None,
                "next_fire": 0.0, "created": 0.0, "conversation_id": None})

    out = sched.set_enabled("sch-old", True)
    assert out["ok"] is False, "re-enabled a schedule whose time had passed"
    assert sid["enabled"] is False, (
        "it reported a refusal while leaving the row armed, so the next "
        "worker tick would fire a reminder the user was told was off")
    assert out.get("hint"), "must say what to do instead"


def test_re_enabling_clears_the_cancel_marker(sched):
    """Cancel sets `_cancelled`; if enable leaves it, the row reports armed
    but `_worker_tick` skips it forever."""
    sid = sched.schedule("morning briefing", "in 2h")
    assert sched.cancel(sid) is True
    assert sched._SCHEDULES[sid]["_cancelled"] is True

    out = sched.set_enabled(sid, True)
    assert out["ok"] is True
    assert "_cancelled" not in sched._SCHEDULES[sid]


def test_manual_disable_also_marks_cancelled(sched):
    """A disable can race an in-flight worker snapshot, which checks only
    `_cancelled` after the snapshot."""
    sid = sched.schedule("morning briefing", "in 2h")
    assert sched.set_enabled(sid, False)["enabled"] is False
    assert sched._SCHEDULES[sid]["_cancelled"] is True


def test_a_re_enabled_schedule_actually_reaches_the_worker(sched, monkeypatch):
    sid = sched.schedule("morning briefing", "in 2h")
    sched.cancel(sid)
    assert sched.set_enabled(sid, True)["ok"] is True

    spec = sched._SCHEDULES[sid]
    spec["next_fire"] = time.time() - 1
    sched._HEAP = [(spec["next_fire"], sid)]
    fired = []
    monkeypatch.setattr(sched, "_fire", lambda s, _snap: fired.append(s))

    sched._worker_tick()
    assert fired == [sid], "the worker skipped a re-enabled schedule"


def test_set_enabled_reports_an_unknown_id(sched):
    out = sched.set_enabled("sch-nope", True)
    assert out["ok"] is False
    assert "unknown" in out["error"].lower()


def test_the_tool_surfaces_a_bad_date_instead_of_raising(monkeypatch):
    """A ValueError escaping the tool kills the turn, and the user is told the
    reminder is set when it is not."""
    import mcp_server

    out = mcp_server.schedule_task("x", "2025-10-12T09:00:00")
    assert out["ok"] is False
    assert "past" in out["error"].lower()
    assert out.get("accepted_when"), (
        "the model needs the valid forms to correct itself")


def test_enable_scheduled_is_registered_and_advertised():
    import asyncio

    import agent_server
    import mcp_server
    from skills import _TOOL_CATALOG

    registered = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert "enable_scheduled" in registered, "the tool is not callable"
    assert "enable_scheduled" in _TOOL_CATALOG, (
        "it is callable but invisible in the schema the model reads")
    assert "enable_scheduled" in agent_server._CHAT_TOOLS, (
        "chat cannot reach it, so this is the chat bug again")
    props = _TOOL_CATALOG["enable_scheduled"]["input_schema"]["properties"]
    assert set(props) == {"schedule_id", "enabled"}


# ── the chat transcript must survive navigation ──────────────────────────

def test_the_chat_view_persists_its_conversation_id():
    """Source-level pin. `cid` was `useState('')`, so unmounting the view on
    navigation reset it and the transcript came back empty even though the
    thread store still held every message."""
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent
           / "console-frontend" / "src" / "views" / "Chat.tsx").read_text(
        encoding="utf-8")

    assert "localStorage.getItem(LAST_CID_KEY)" in src, (
        "the open conversation id is not read back on mount")
    assert "localStorage.setItem(LAST_CID_KEY" in src, (
        "the open conversation id is not persisted when it changes")

    # Every write must go through the helper that persists. A single bare
    # `setCid(` outside `rememberCid` is a path that silently skips the write.
    raw = [m.start() for m in re.finditer(r"^\s*setCid\(", src, re.M)]
    assert len(raw) == 1, (
        f"{len(raw)} setCid( calls sit outside rememberCid, so those paths do "
        f"not persist the conversation id")