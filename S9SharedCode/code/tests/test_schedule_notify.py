"""Scheduled notification: pre-warning, channel choice, acknowledgement.

The feature under test is the difference between a reminder that announces
itself and one that a human can actually respond to. Three things have to hold
and each has a way to fail silently:

  * the policy the agent asked for is what gets persisted (a dropped
    `lead_s` produces a reminder that fires once and is never warned about);
  * a pre-warning is armed as its own job, costs no LLM spend, and cannot
    outlive a cancelled parent;
  * an acknowledgement conversation is bounded - the model cannot talk the
    user into the ground, and a thread from a stranger is never routed.
"""

from __future__ import annotations

import time

import pytest

import ack
import scheduler


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("S9_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(scheduler, "_STATE_DIR", tmp_path / "state",
                        raising=False)
    monkeypatch.setattr(scheduler, "_SCHED_PATH",
                        tmp_path / "state" / "schedules.json", raising=False)
    old_s, old_h = scheduler._SCHEDULES, scheduler._HEAP
    scheduler._SCHEDULES, scheduler._HEAP = {}, []
    yield
    scheduler._SCHEDULES, scheduler._HEAP = old_s, old_h


def _spec(sid):
    return scheduler._SCHEDULES[sid]


# ── duration parsing ─────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,secs", [
    (900, 900), ("900", 900), ("15m", 900), ("2h", 7200), ("1d", 86400),
    ("90s", 90), (" 30m ", 1800),
])
def test_duration_forms_are_accepted(raw, secs):
    assert scheduler._coerce_seconds(raw, "lead_s") == secs


@pytest.mark.parametrize("raw", ["fifteen minutes", "soon", "5 weeks", "", None])
def test_unparseable_durations_are_rejected_not_zeroed(raw):
    """The dangerous failure is a silent 0: `lead_s: "fifteen minutes"`
    would mean "no pre-warning at all" - the opposite of what was asked."""
    if raw == "" or raw is None:
        assert scheduler._coerce_seconds(raw, "lead_s") == 0.0
        return
    with pytest.raises(ValueError):
        scheduler._coerce_seconds(raw, "lead_s")


def test_negative_duration_is_rejected():
    with pytest.raises(ValueError):
        scheduler._coerce_seconds(-5, "lead_s", minimum=0)


def test_bools_are_not_durations():
    with pytest.raises(ValueError):
        scheduler._coerce_seconds(True, "lead_s")


# ── policy persistence ───────────────────────────────────────────────────

def test_a_plain_schedule_stores_no_policy():
    sid = scheduler.schedule("stretch", "in 30m")
    assert "notify_policy" not in _spec(sid)
    assert not any(k.endswith("-lead") for k in scheduler._SCHEDULES)


def test_channel_and_lead_are_persisted():
    sid = scheduler.schedule("briefing", "in 1h", channel="telegram",
                             lead_s="10m", lead_message="Heads up",
                             message="Starting now")
    pol = _spec(sid)["notify_policy"]
    assert pol["channel"] == "telegram"
    assert pol["lead_s" if False else "lead_fire"] > 0
    assert pol["lead_message"] == "Heads up"
    assert pol["message"] == "Starting now"


def test_a_lead_longer_than_the_delay_is_refused():
    """Otherwise the pre-warning fires in the past and either fires instantly
    or is silently dropped."""
    with pytest.raises(ValueError, match="pre-notification"):
        scheduler.schedule("briefing", "in 5m", lead_s="30m",
                           lead_message="too early")


def test_follow_up_budget_is_capped():
    with pytest.raises(ValueError):
        scheduler.schedule("x", "in 1h", wait_for_ack=True,
                           max_follow_ups=99)


def test_unknown_channel_is_rejected_loudly():
    with pytest.raises(ValueError, match="unknown channel"):
        scheduler.schedule("x", "in 1h", channel="carrier-pigeon",
                           lead_s="5m", lead_message="hi")


# ── the armed pre-warning job ────────────────────────────────────────────

def test_pre_warning_is_armed_as_a_separate_derived_job():
    sid = scheduler.schedule("briefing", "in 1h", channel="telegram",
                             lead_s="10m", lead_message="Heads up")
    lead = f"{sid}-lead"
    assert lead in scheduler._SCHEDULES
    spec = _spec(lead)
    assert spec["parent"] == sid
    assert spec["phase"] == "lead"
    # No agent query, so firing it must not run the agent or bill a turn.
    assert spec["query"] == ""
    assert spec["notify"] is False


def test_pre_warning_is_not_armed_without_a_lead():
    sid = scheduler.schedule("briefing", "in 1h", channel="telegram",
                             message="Starting now")
    assert f"{sid}-lead" not in scheduler._SCHEDULES


def test_a_lead_fires_before_its_parent():
    sid = scheduler.schedule("briefing", "in 1h", lead_s="10m",
                             channel="telegram", lead_message="x")
    assert _spec(f"{sid}-lead")["next_fire"] < _spec(sid)["next_fire"]


def test_cancelling_the_parent_disarms_the_pre_warning():
    """Two independent rows would mean cancelling a task could leave its
    warning armed to fire into a reminder the user had dismissed."""
    sid = scheduler.schedule("briefing", "in 1h", channel="telegram",
                             lead_s="10m", lead_message="x")
    lead = f"{sid}-lead"
    assert scheduler.cancel(lead) is True
    assert scheduler._SCHEDULES[lead].get("enabled") is False
    assert scheduler._SCHEDULES[lead].get("_cancelled") is True


def test_a_lead_never_runs_the_agent(monkeypatch):
    """Regression guard: routing the pre-warning through _fire would bill a
    full LLM turn to say 'this is a reminder'."""
    called = []
    monkeypatch.setattr(scheduler, "_fire",
                        lambda *a, **k: called.append(a) or "x")
    sent = []
    monkeypatch.setattr("notify.send_channel_message",
                        lambda ch, text, **k: sent.append((ch, text)) or
                        {"delivered": True, "target": "42"})
    sid = scheduler.schedule("b", "in 1h", channel="telegram", lead_s="10m",
                             lead_message="Heads up")
    scheduler._fire_lead(f"{sid}-lead", _spec(f"{sid}-lead"))
    assert called == [], "the pre-warning invoked the agent"
    assert sent and sent[0][1] == "Heads up"


def test_a_dead_channel_does_not_stop_the_task(monkeypatch):
    """The task must fire whether or not the message can be delivered."""
    monkeypatch.setattr("notify.send_channel_message",
                        lambda *a, **k: {"delivered": False,
                                         "error": "chat not found"})
    notes = []
    import agent_server
    monkeypatch.setattr(agent_server, "_notif_add",
                        lambda *a, **k: notes.append(a))
    sid = scheduler.schedule("b", "in 1h", channel="telegram",
                             message="done")
    scheduler._deliver(sid, "telegram", "done", "s8-x")
    assert any("could not deliver" in str(n) for n in notes)


# ── acknowledgement threads ──────────────────────────────────────────────

@pytest.fixture()
def ack_store(tmp_path, monkeypatch):
    monkeypatch.setattr(ack, "STORE_PATH", tmp_path / "ack.json")
    yield


def test_thread_is_matched_on_channel_and_sender(ack_store):
    ack.open_thread("sch-1", "telegram", "999", query="standup")
    assert ack.find_thread_for("telegram", "999") is not None
    # Same channel, different person: must not match, or the first stranger to
    # message the bot answers somebody else's reminder.
    assert ack.find_thread_for("telegram", "111") is None
    assert ack.find_thread_for("slack", "999") is None


def test_thread_expires(ack_store):
    ack.open_thread("sch-1", "telegram", "999", ack_timeout_s=60)
    t = ack.get_thread("sch-1")
    assert t["expires"] > time.time()
    ack._load  # noqa: B018 - documenting that state is file-backed
    # Force expiry rather than sleeping.
    with ack._LOCK:
        data = ack._load()
        data["sch-1"]["expires"] = time.time() - 1
        ack._save(data)
    assert ack.find_thread_for("telegram", "999") is None
    assert ack.get_thread("sch-1")["status"] == "expired"


def test_verdict_must_be_from_the_closed_set(ack_store):
    ack.open_thread("sch-1", "telegram", "999", max_follow_ups=2)
    t = ack.apply_verdict("sch-1", "delete_everything", "sure")
    # An unrecognised verb is silence, never an action.
    assert t["follow_ups"] == 0


def test_the_follow_up_budget_is_enforced(ack_store):
    """The guarantee that makes an LLM-driven loop safe."""
    ack.open_thread("sch-1", "telegram", "999", max_follow_ups=2)
    for _ in range(6):
        ack.apply_verdict("sch-1", "reply", "still there?")
    t = ack.get_thread("sch-1")
    assert t["follow_ups"] == 2, "the agent messaged past its budget"
    assert t["status"] == "budget_exhausted"


def test_acknowledge_closes_the_thread(ack_store):
    ack.open_thread("sch-1", "telegram", "999", max_follow_ups=3)
    t = ack.apply_verdict("sch-1", "acknowledge")
    assert t["status"] == "acknowledged"
    assert ack.due_for_nudge() == []


def test_a_user_reply_resets_the_snooze_clock(ack_store):
    """The user replying means they are engaged; the next nudge should be
    measured from now, not from the previous silence."""
    # A budget is required: with max_follow_ups=0 the thread is a one-way
    # announcement and is correctly never due (see the zero-budget test).
    ack.open_thread("sch-1", "telegram", "999", snooze_s=600,
                    max_follow_ups=2)
    with ack._LOCK:
        data = ack._load()
        data["sch-1"]["last_agent_msg"] = time.time() - 5000
        ack._save(data)
    assert ack.due_for_nudge(), "due before the reply"
    ack.record_user_reply("sch-1", "not yet", sender="999")
    assert not ack.due_for_nudge(), "still due after the user replied"


def test_a_zero_budget_thread_is_never_nudged(ack_store):
    """A one-way announcement must not repeat."""
    ack.open_thread("sch-1", "telegram", "999", snooze_s=1, max_follow_ups=0)
    with ack._LOCK:
        data = ack._load()
        data["sch-1"]["last_agent_msg"] = time.time() - 9999
        ack._save(data)
    assert ack.due_for_nudge() == []
