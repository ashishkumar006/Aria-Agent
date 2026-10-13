"""A reminder that fires, bills, and is never delivered.

Reproduced live: a reminder scheduled without an explicit channel fired
normally â€” the agent ran, the cost was recorded, an in-app entry appeared â€”
and no Telegram message was ever sent. No error, no log line, nothing.

The cause: `_notify_telegram` posted to `/v1/channels/telegram/send` with no
`X-Gateway-Token`. The gateway requires that header on every `/v1/*` route, so
the post came back **401**, and an unchecked status code plus `except: pass`
discarded it. The channel-less reminder path runs entirely through this
function, which is why every such reminder went missing.
"""

from __future__ import annotations

import json

import pytest

import agent_server


class _Resp:
    def __init__(self, status_code=200, text="ok", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload if payload is not None else {"ok": True}

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _reset_resolve_cache(monkeypatch):
    """`resolve_notify_chat` memoises in a module global. Without this, one
    test's cached result decides the next one's outcome — which is exactly the
    bug these tests exist to pin, so it must not also be their confounder.

    Reset both before and after: the "miss is not cached" test deliberately
    leaves a cached miss behind, and the next test must not inherit it."""
    def _clear():
        monkeypatch.setattr(agent_server, "_NOTIFY_CHAT", "", raising=False)
        monkeypatch.setattr(agent_server, "_NOTIFY_RESOLVED", None,
                            raising=False)

    _clear()
    yield
    agent_server._NOTIFY_RESOLVED = None


@pytest.fixture()
def sent(monkeypatch):
    """Record every outbound telegram POST with its headers."""
    calls: list[dict] = []

    def _post(url, **kwargs):
        calls.append({"url": url, "headers": kwargs.get("headers") or {},
                      "json": kwargs.get("json")})
        return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(agent_server, "resolve_notify_chat",
                        lambda channel="telegram": "60123")
    return calls


def test_a_scheduled_reminder_sends_the_gateway_token(sent):
    """The regression itself. A missing header is a 401 the old code threw
    away, so this must fail if the header is ever dropped again."""
    agent_server._notify_telegram("reminder text", "60123")

    assert len(sent) == 1
    headers = {str(k).lower(): v for k, v in sent[0]["headers"].items()}
    assert "x-gateway-token" in headers, (
        "the telegram send carried no gateway token, so the gateway answers "
        "401 and the reminder is silently dropped")


def test_a_failed_send_is_reported_not_swallowed(sent, monkeypatch, capsys):
    """A 401 that is returned rather than raised is exactly what was lost."""
    import httpx

    monkeypatch.setattr(httpx, "post",
                        lambda url, **k: _Resp(401, "missing or invalid token"))

    ok = agent_server._notify_telegram("reminder text", "60123")

    assert ok is False
    assert "401" in capsys.readouterr().out, (
        "a failed send produced no log line, which is how this stayed hidden")


def test_notify_task_done_records_an_undelivered_reminder(monkeypatch):
    """The outcome must reach the notification feed, so a reminder that could
    not be delivered is visible instead of merely absent."""
    monkeypatch.setattr(agent_server, "resolve_notify_chat",
                        lambda channel="telegram": "60123")
    monkeypatch.setattr(agent_server, "_notify_telegram",
                        lambda text, chat=None: False)
    entries: list[dict] = []
    monkeypatch.setattr(agent_server, "_notif_add",
                        lambda kind, text, session_id="": entries.append(
                            {"kind": kind, "text": text, "session": session_id}))

    agent_server.notify_task_done("check the deploy", "all good", 3.0,
                                  "s8-x", scheduled=True)

    assert entries, "an undelivered reminder was recorded nowhere"
    assert "could not be delivered" in entries[-1]["text"]


def test_a_delivered_reminder_does_not_log_a_failure(monkeypatch):
    monkeypatch.setattr(agent_server, "resolve_notify_chat",
                        lambda channel="telegram": "60123")
    monkeypatch.setattr(agent_server, "_notify_telegram",
                        lambda text, chat=None: True)
    entries: list[dict] = []
    monkeypatch.setattr(agent_server, "_notif_add",
                        lambda kind, text, session_id="": entries.append(text))

    agent_server.notify_task_done("check the deploy", "all good", 3.0,
                                  "s8-x", scheduled=True)

    assert not any("could not be delivered" in e for e in entries)


def test_an_unresolved_destination_is_reported_as_such(monkeypatch):
    """The outcome must be recorded, and must not blame the user's pairing
    when the real cause was an unreachable or unauthorised gateway.

    The old wording ("no notification destination is paired") sent the user
    off to message the bot about a gateway that was simply refusing the read
    â€” which is how a transient 401 hid for an entire release.
    """
    monkeypatch.setattr(agent_server, "resolve_notify_chat",
                        lambda channel="telegram": "")
    entries: list[dict] = []
    monkeypatch.setattr(agent_server, "_notif_add",
                        lambda kind, text, session_id="": entries.append(
                            {"kind": kind, "text": text}))

    agent_server.notify_task_done("check the deploy", "all good", 3.0,
                                  "s8-x", scheduled=True)

    assert entries, "an undelivered reminder was recorded nowhere"
    assert "destination could be read from the gateway" in " ".join(
        entries[-1]["text"].split()), entries[-1]["text"]
    assert "not paired" not in entries[-1]["text"], (
        "do not tell the user their pairing is missing when the gateway read "
        "is what failed")


def test_the_agents_own_message_is_sent_verbatim(sent, monkeypatch):
    """The agent decides at scheduling time what the user should read.

    This used to be discarded on the no-channel path, so a reminder set
    through Telegram arrived as a fixed template — a green tick, a wall-clock
    line, a session id and the raw run output — no matter what the model
    actually wrote. Observed live: a two-minute reminder arrived as
    "✅ Reminder / time ± 32.5s · session s8-… / 🎙 <raw output>".
    """
    monkeypatch.setattr(agent_server, "resolve_notify_chat",
                        lambda channel="telegram": "60123")
    monkeypatch.setattr(agent_server, "_notif_add", lambda *a, **k: None)

    agent_server.notify_task_done(
        "check the deploy", "raw run output", 32.5, "s8-x", scheduled=True,
        message="Your staging deploy is ready to check.")

    assert len(sent) == 1
    body = sent[0]["json"]["text"]
    assert body == "Your staging deploy is ready to check.", (
        f"the agent's message was reformatted into: {body!r}")
    for junk in ("✅", "🎙", "time ±", "session s8-", "Reminder"):
        assert junk not in body, f"template debris {junk!r} leaked into the text"


def test_with_no_authored_message_the_fallback_is_plain_prose(sent, monkeypatch):
    """No decoration either way. The old fallback was three lines of emoji,
    timing and a session id, which is not what anyone asked to be reminded
    about."""
    monkeypatch.setattr(agent_server, "resolve_notify_chat",
                        lambda channel="telegram": "60123")
    monkeypatch.setattr(agent_server, "_notif_add", lambda *a, **k: None)

    agent_server.notify_task_done("stretch", "you have been at it an hour",
                                  1.0, "s8-x", scheduled=True)

    body = sent[0]["json"]["text"]
    assert "stretch" in body and "an hour" in body
    for junk in ("✅", "🎙", "time ±", "session s8-"):
        assert junk not in body, f"template debris {junk!r} leaked into the text"


def test_the_snooze_nudge_path_carries_the_token(sent, monkeypatch):
    """The nudge that re-messages an unanswered reminder goes through
    `notify.send_channel_message`, not `_notify_telegram` â€” confirm that path
    really is authenticated, since the same failure would silence snoozes."""
    import notify

    seen: list[dict] = []
    import httpx

    class _Client:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kwargs):
            seen.append({"url": url, "headers": kwargs.get("headers")})
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(notify, "resolve_target", lambda channel: "60123")

    res = notify.send_channel_message("telegram", "still there?", to="60123")

    assert res["delivered"] is True
    authed = [c for c in seen if isinstance(c.get("headers"), dict)]
    assert any("X-Gateway-Token" in c["headers"] for c in authed), (
        "the nudge path is unauthenticated; a snooze would be dropped the same "
        "way this reminder was")


# â”€â”€ the poisoned cache â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class _GetResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"target": "60123"}

    def json(self):
        return self._payload


def _gateway_says(monkeypatch, statuses):
    """Answer `statuses` in order; then keep answering 200."""
    seq = list(statuses)

    class _C:
        def __init__(self, **kwargs):
            # `resolve_notify_chat` updates `c.headers` before its retry.
            self.headers = dict(kwargs.get("headers") or {})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kwargs):
            code = seq.pop(0) if seq else 200
            return _GetResp(code, {"target": ""} if code != 200 else None)

    import httpx
    monkeypatch.setattr(httpx, "Client", _C)
    monkeypatch.setattr(agent_server, "_NOTIFY_CHAT", "")
    monkeypatch.setattr(agent_server, "_NOTIFY_RESOLVED", None)


def test_a_transient_gateway_failure_is_not_cached_as_no_destination(monkeypatch):
    """The bug that ate every reminder.

    A gateway that is DOWN (connection refused, or restarting) left the
    resolution cache holding an EMPTY chat. Every reminder firing in that
    window was told "no notification destination is paired" — advice that is
    actively wrong, because a destination was paired and merely unreadable.
    Live: 77 such entries before the fix.

    A 401 is a different case and is retried immediately (see the next test),
    so this uses a transport failure, which cannot be recovered in-call.
    """
    state = {"fail": True}

    class _C:
        def __init__(self, **kwargs):
            self.headers = dict(kwargs.get("headers") or {})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kwargs):
            if state["fail"]:
                raise RuntimeError("gateway is down")
            return _GetResp(200, {"target": "60123"})

    import httpx
    monkeypatch.setattr(httpx, "Client", _C)
    monkeypatch.setattr(agent_server, "_NOTIFY_CHAT", "")
    monkeypatch.setattr(agent_server, "_NOTIFY_RESOLVED", None)

    assert agent_server.resolve_notify_chat() == "", (
        "an unreachable gateway reported a destination anyway")

    state["fail"] = False
    # Force past the miss-TTL: the point is that a MISS is not cached like a
    # HIT. The TTL itself is a deliberate burst guard and is allowed to hold a
    # miss briefly; what must never happen is a miss living as long as a hit.
    monkeypatch.setattr(agent_server, "_NOTIFY_RESOLVED", None)
    assert agent_server.resolve_notify_chat() == "60123", (
        "a gateway failure was cached as 'no destination paired', so every "
        "reminder in the TTL window was silently dropped")


def test_a_401_is_retried_with_freshly_read_credentials(monkeypatch):
    """Matches the self-heal `_gw_json` already performs elsewhere.

    The gateway can rotate its token under a long-lived agent. A single 401
    must not disable reminders until the next restart, so the read is retried
    once with freshly-read credentials — counted by GET, not by client
    construction, because one client serves both attempts.
    """
    gets = []

    class _C:
        def __init__(self, **kwargs):
            self.headers = dict(kwargs.get("headers") or {})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kwargs):
            gets.append(dict(self.headers))
            if len(gets) == 1:
                return _GetResp(401)
            return _GetResp(200, {"target": "60123"})

    import httpx
    monkeypatch.setattr(httpx, "Client", _C)
    monkeypatch.setattr(agent_server, "_NOTIFY_CHAT", "")
    monkeypatch.setattr(agent_server, "_NOTIFY_RESOLVED", None)

    assert agent_server.resolve_notify_chat() == "60123", (
        "a 401 was not retried, so a token rotation silently disabled "
        "reminders until the agent was restarted")
    assert len(gets) == 2, f"expected one retry, saw {len(gets)} attempts"


def test_the_scheduled_message_reaches_the_send_verbatim(monkeypatch, tmp_path):
    """End to end through the scheduler: what the agent wrote at schedule time
    is what the channel receives. Before this, the no-channel branch dropped
    it and sent a fixed template instead."""
    import scheduler

    store = tmp_path / "schedules.json"
    monkeypatch.setattr(scheduler, "_SCHED_PATH", store, raising=False)
    monkeypatch.setattr(scheduler, "_SCHEDULES", {}, raising=False)

    sent = []

    class _FakeExec:
        def run(self, *a, **k):
            return "raw run output"

    import flow
    monkeypatch.setattr(flow, "Executor", _FakeExec)

    import agent_server as ag
    monkeypatch.setattr(ag, "_session_cost_breakdown", lambda *a, **k: None)
    monkeypatch.setattr(ag, "_notif_add", lambda *a, **k: None)
    monkeypatch.setattr(ag, "resolve_notify_chat",
                        lambda channel="telegram": "60123")
    monkeypatch.setattr(ag, "_notify_telegram",
                        lambda text, chat=None: sent.append(text) or True)

    sid = scheduler.schedule(
        "check the deploy", "in 5 minutes",
        message="Time to check the deploy.")
    spec = dict(scheduler._SCHEDULES[sid])
    scheduler._fire(sid, spec)

    assert sent == ["Time to check the deploy."], (
        f"the agent's message was not delivered verbatim: {sent}")


def test_the_channel_branch_also_sends_the_agents_message_verbatim(monkeypatch,
                                                                   tmp_path):
    """The named-channel branch already preferred the agent's words; keep it
    that way and keep the emoji/timing template out."""
    import scheduler

    monkeypatch.setattr(scheduler, "_SCHED_PATH", tmp_path / "schedules.json", raising=False)
    monkeypatch.setattr(scheduler, "_SCHEDULES", {}, raising=False)

    sent = []
    import notify
    monkeypatch.setattr(notify, "send_channel_message",
                        lambda ch, text, **k: sent.append(text) or
                        {"delivered": True})

    sid = scheduler.schedule(
        "check the deploy", "in 5 minutes", channel="telegram",
        message="Time to check the deploy.")
    spec = dict(scheduler._SCHEDULES[sid])

    class _FakeExec:
        def run(self, *a, **k):
            return "raw run output"

    import flow
    monkeypatch.setattr(flow, "Executor", _FakeExec)
    import agent_server as ag
    monkeypatch.setattr(ag, "_session_cost_breakdown", lambda *a, **k: None)
    monkeypatch.setattr(ag, "_notif_add", lambda *a, **k: None)

    scheduler._fire(sid, spec)

    assert sent == ["Time to check the deploy."], sent

