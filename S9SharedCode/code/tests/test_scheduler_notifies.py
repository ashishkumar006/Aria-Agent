"""A reminder has to actually reach someone.

The scheduler ran the task, wrote `last_fire`, and then called
`notify_task_done` - which returned immediately unless TWO env vars were set
on the agent, and needed a chat id the agent could not obtain from anywhere:
the gateway keeps a pairing store written by inbound messages, but it was only
ever read back masked, for display. So a reminder with a bot token configured
had nowhere to go and failed silently. Silence is the one outcome a reminder
must never produce.
"""
import agent_server
import scheduler


def test_a_fired_task_is_always_recorded_in_app(monkeypatch):
    """Zero configuration: the result is recorded even with Telegram off."""
    seen = []
    monkeypatch.setattr(agent_server, "_notif_add",
                        lambda kind, text, sid="": seen.append((kind, text, sid))
                        or {})
    monkeypatch.setattr(agent_server, "notify_task_done",
                        lambda *a, **k: seen.append(("notified", a[0], a[3])))

    spec = {"query": "check the build", "when": "in 1m", "enabled": True,
            "notify": True, "conversation_id": None}
    # Stop the run itself from doing any work; this test is about the tail.
    import flow

    class _Ex:
        def run(self, q, session_id=None, resume=False):
            return "the build is green"

    orig = flow.Executor
    flow.Executor = _Ex
    try:
        scheduler._fire("sch-notify-test", spec)
    finally:
        flow.Executor = orig

    assert any(k == "scheduled" for k, _t, _s in seen), \
        f"the outcome was not recorded: {seen}"
    assert any("check the build" in t for _k, t, _s in seen), seen


def test_a_reminder_is_not_gated_behind_an_env_flag(monkeypatch):
    """`scheduled=True` must notify on its own.

    The opt-in flag exists so ordinary CHAT replies stay quiet. A fired
    reminder is the opposite case: the user asked to be told.
    """
    monkeypatch.delenv("AGENT_TELEGRAM_NOTIFY", raising=False)
    sent = []
    monkeypatch.setattr(agent_server, "resolve_notify_chat", lambda *a: "12345")
    monkeypatch.setattr(agent_server, "threading", _FakeThread(sent))
    agent_server.notify_task_done("remind me", "done", 1.2, "s8-x",
                                  scheduled=True)
    assert sent, "a fired reminder produced no notification"
    assert "12345" == sent[0][1], "sent to the wrong chat"


def test_a_chat_reply_is_still_opt_in(monkeypatch):
    """The noise regression must not come back."""
    monkeypatch.delenv("AGENT_TELEGRAM_NOTIFY", raising=False)
    sent = []
    monkeypatch.setattr(agent_server, "resolve_notify_chat", lambda *a: "12345")
    monkeypatch.setattr(agent_server, "threading", _FakeThread(sent))
    agent_server.notify_task_done("hello", "hi", 0.1, "s8-x")
    assert not sent, "every chat reply was pushed to Telegram"


def test_an_explicit_chat_id_wins_over_resolution(monkeypatch):
    monkeypatch.setattr(agent_server, "_NOTIFY_CHAT", "explicit-id")
    monkeypatch.setattr(agent_server, "_NOTIFY_RESOLVED", None)
    assert agent_server.resolve_notify_chat() == "explicit-id"


def test_no_destination_is_reported_rather_than_dropped(monkeypatch):
    """Silence must leave a trace, or the user cannot tell a working reminder
    from a broken one."""
    recorded = []
    monkeypatch.setattr(agent_server, "_notif_add",
                        lambda kind, text, sid="": recorded.append(text) or {})
    monkeypatch.setattr(agent_server, "resolve_notify_chat", lambda *a: "")
    agent_server.notify_task_done("remind me", "x", 1.0, "s8-x", scheduled=True)
    assert recorded, "an undeliverable reminder left no record"
    assert "destination" in recorded[0], recorded[0]


class _FakeThread:
    def __init__(self, sink):
        self.sink = sink

    def Thread(self, target=None, args=(), daemon=None):  # noqa: N802
        self.sink.append(args)
        return self

    def start(self):
        return None
