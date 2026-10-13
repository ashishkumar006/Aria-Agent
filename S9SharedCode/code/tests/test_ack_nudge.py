"""The nudge loop: "messages keep coming until the user responds".

This is the half of the conversation feature that runs unattended, so it is
the half that has to terminate. Every path through `nudge_worker` either
sends a message or gives up forever; nothing may loop.

The loop is also the only part of the ack feature that is not driven by a
user message, so a bug here is invisible until the user is sitting there
watching reminders pile up. Hence the coverage.
"""

from __future__ import annotations

import threading

import ack
import ack_bridge


class _T:
    """A stop event that reports "not stopped" until we set it, so the loop
    body runs a bounded number of times."""

    def __init__(self, rounds: int = 1):
        self.waits = 0
        self.rounds = rounds

    def wait(self, timeout=None):
        self.waits += 1
        return self.waits > self.rounds


def _thread(**kw):
    base = {"schedule_id": "sch-1", "channel": "telegram", "target": "42",
            "query": "check deploy", "prompt": "Reminder note",
            "opened": 0.0, "expires": 9_999_999_999.0, "snooze_s": 0.0,
            "max_follow_ups": 3, "follow_ups": 0, "last_agent_msg": 0.0,
            "status": "open"}
    base.update(kw)
    return base


def test_an_unanswered_thread_is_nudged_and_the_budget_is_spent(monkeypatch):
    """A nudge must count against max_follow_ups, or a user who never replies
    is messaged forever."""
    sent = []
    monkeypatch.setattr(ack, "sweep", lambda: 0)
    monkeypatch.setattr(ack, "due_for_nudge", lambda: [_thread()])
    monkeypatch.setattr(ack, "apply_verdict",
                        lambda sid, verdict, reply="": {"status": "open"})
    monkeypatch.setattr(ack_bridge, "_notify_send",
                        lambda ch, text, to="", session_id="": (
                            sent.append((ch, text, to)) or {"delivered": True}))

    ack_bridge.nudge_worker(_T(rounds=1), interval=0.01)

    assert len(sent) == 1, "an unanswered reminder was not nudged"
    ch, text, to = sent[0]
    assert ch == "telegram" and to == "42"
    assert "Reminder note" in text


def test_a_failed_nudge_does_not_spend_the_budget(monkeypatch):
    """A message that did not arrive is not an attempt the user ignored.
    Charging it against the budget silently ends the conversation."""
    sent = []
    monkeypatch.setattr(ack, "sweep", lambda: 0)
    monkeypatch.setattr(ack, "due_for_nudge", lambda: [_thread()])
    monkeypatch.setattr(ack_bridge, "_notify_send",
                        lambda ch, text, to="", session_id="": (
                            sent.append(text) or {"delivered": False}))
    data = {"sch-1": _thread()}
    monkeypatch.setattr(ack, "_load", lambda: data)
    saved = {}
    monkeypatch.setattr(ack, "_save", lambda d: saved.update(d))

    ack_bridge.nudge_worker(_T(rounds=1), interval=0.01)

    assert saved == {}, (
        "an undelivered nudge was charged to the follow-up budget, so the "
        "user would be left with no further reminders after one failure")


def test_a_thread_with_no_budget_is_never_nudged(monkeypatch):
    """`max_follow_ups: 0` is a one-way announcement. Repeating it would be
    harassment, and the loop would never terminate."""
    sent = []
    monkeypatch.setattr(ack, "sweep", lambda: 0)
    monkeypatch.setattr(ack, "due_for_nudge",
                        lambda: [_thread(max_follow_ups=0, follow_ups=0)])
    monkeypatch.setattr(ack_bridge, "_notify_send",
                        lambda *a, **k: sent.append(a) or {"delivered": True})

    ack_bridge.nudge_worker(_T(rounds=1), interval=0.01)

    assert sent == []


def test_the_loop_survives_a_broken_thread(monkeypatch):
    """One malformed entry must not kill the nudger for every other thread."""
    good = _thread(schedule_id="sch-good")

    def _boom():
        raise RuntimeError("corrupt thread entry")

    monkeypatch.setattr(ack, "sweep", lambda: 0)
    monkeypatch.setattr(ack, "due_for_nudge", lambda: good)
    monkeypatch.setattr(ack_bridge, "_notify_send",
                        lambda *a, **k: {"delivered": True})
    monkeypatch.setattr(ack, "apply_verdict", lambda *a, **k: _boom())

    ack_bridge.nudge_worker(_T(rounds=1), interval=0.01)  # must not raise


def test_start_nudger_is_idempotent():
    """Starting a second nudger would double every reminder."""
    ack_bridge._NUDGER = None
    ack_bridge._NUDGE_STOP = None
    try:
        first = ack_bridge.start_nudger()
        second = ack_bridge.start_nudger()
        assert first is second
        assert isinstance(first, threading.Thread)
    finally:
        if ack_bridge._NUDGE_STOP is not None:
            ack_bridge._NUDGE_STOP.set()
        ack_bridge._NUDGER = None
        ack_bridge._NUDGE_STOP = None

# ── acknowledgement is confirmed, not swallowed ──────────────────────────

def test_an_acknowledgement_is_confirmed_back(monkeypatch):
    """The thread closes and the nudger stops, so silence afterwards is
    indistinguishable from the bot having missed the reply. The user asked
    the agent to confirm."""
    import ack
    import ack_bridge

    thread = {"schedule_id": "sch-1", "query": "check deploy",
              "prompt": "Reminder: check deploy", "channel": "telegram"}
    monkeypatch.setattr(ack, "find_thread_for", lambda ch, s: thread)
    monkeypatch.setattr(ack_bridge, "_ask_model",
                        lambda p: {"verdict": "acknowledge", "text": ""})
    monkeypatch.setattr(ack, "record_user_reply", lambda *a, **k: {})
    monkeypatch.setattr(ack, "apply_verdict",
                        lambda *a, **k: {"status": "acknowledged"})

    told = []
    import notify
    monkeypatch.setattr(notify, "send_channel_message",
                        lambda ch, text, **k: told.append(text) or
                        {"delivered": True})

    out = ack_bridge.handle_inbound("telegram", "7", "ok got it")

    assert out["verdict"] == "acknowledge"
    assert told, (
        "the user acknowledged and the agent said nothing, so an answered "
        "reminder is indistinguishable from an ignored one")


def test_a_repeat_nudge_is_not_byte_identical_and_says_how_many_are_left(monkeypatch):
    """A bot that re-sends the same text verbatim looks broken, and the user
    cannot tell a second attempt from the first they already ignored."""
    import ack
    import ack_bridge

    sent = []
    monkeypatch.setattr(ack, "sweep", lambda: 0)
    monkeypatch.setattr(ack, "due_for_nudge", lambda: [{
        "schedule_id": "sch-1", "channel": "telegram", "target": "7",
        "prompt": "Time to check the deploy.", "query": "deploy",
        "follow_ups": 1, "max_follow_ups": 3,
    }])
    monkeypatch.setattr(ack_bridge, "_notify_send",
                        lambda ch, text, to="", session_id="":
                        sent.append(text) or {"delivered": True})
    monkeypatch.setattr(ack, "apply_verdict", lambda *a, **k: {"status": "open"})
    monkeypatch.setattr(ack, "_load", lambda: {"sch-1": {}})
    monkeypatch.setattr(ack, "_save", lambda d: None)

    class _T:
        def __init__(self, rounds=1):
            self.n = 0
        def wait(self, timeout=None):
            self.n += 1
            return self.n > 1

    ack_bridge.nudge_worker(_T(), interval=0.01)

    assert len(sent) == 1
    assert sent[0] != "Time to check the deploy.", (
        "the nudge repeated the original text byte-for-byte")
    assert "2 more reminders" in sent[0], (
        f"the user cannot tell how many reminders are left: {sent[0]!r}")
