"""Talking to the agent from a chat channel.

The feature: a message typed into Telegram becomes an ordinary chat turn, and
the reply goes back over the same channel. The part that must not be wrong is
the trust gate — a channel the agent can be talked to is also a channel a
stranger can type into, and the pairing store is the only thing that tells the
two apart.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import channel_chat


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(channel_chat, "MAP_PATH", tmp_path / "chan.json")
    for k in list(channel_chat._INFLIGHT):
        channel_chat._INFLIGHT.pop(k, None)
    yield
    for k in list(channel_chat._INFLIGHT):
        channel_chat._INFLIGHT.pop(k, None)


# ── the trust gate ───────────────────────────────────────────────────────

def test_an_unpaired_sender_gets_no_turn(monkeypatch):
    monkeypatch.setattr(channel_chat, "trust_level",
                        lambda ch, s: "untrusted")
    turn = []
    monkeypatch.setattr(channel_chat, "_run_turn",
                        lambda *a, **k: turn.append(a))

    out = channel_chat.handle_inbound("telegram", "111", "hello")

    assert out["accepted"] is False
    assert "not paired" in out["reason"]
    assert turn == [], "an unpaired sender started an agent turn"


def test_an_unknown_trust_store_refuses_rather_than_permits(monkeypatch):
    """'Could not read the pairing store' is not 'feel free to spend tokens'."""
    monkeypatch.setattr(channel_chat, "trust_level",
                        lambda ch, s: "unknown")
    turn = []
    monkeypatch.setattr(channel_chat, "_run_turn",
                        lambda *a, **k: turn.append(a))

    out = channel_chat.handle_inbound("telegram", "111", "hello")

    assert out["accepted"] is False
    assert turn == []


def test_a_paired_sender_is_accepted(monkeypatch):
    monkeypatch.setattr(channel_chat, "trust_level",
                        lambda ch, s: "paired")
    monkeypatch.setattr(channel_chat, "_run_turn",
                        lambda *a, **k: {"ok": True})

    out = channel_chat.handle_inbound("telegram", "222", "what is 2+2?")

    assert out["accepted"] is True
    assert out["trust"] == "paired"


def test_an_owner_is_accepted(monkeypatch):
    monkeypatch.setattr(channel_chat, "trust_level",
                        lambda ch, s: "owner")
    monkeypatch.setattr(channel_chat, "_run_turn",
                        lambda *a, **k: {"ok": True})

    assert channel_chat.handle_inbound("telegram", "1", "hi")["accepted"]


def test_a_missing_field_is_refused_before_any_trust_lookup(monkeypatch):
    """No sender or no text is a malformed request, not a chat turn."""
    lookup = []
    monkeypatch.setattr(channel_chat, "trust_level",
                        lambda ch, s: lookup.append(s) or "owner")

    assert channel_chat.handle_inbound("telegram", "", "hi")["accepted"] is False
    assert channel_chat.handle_inbound("telegram", "1", "")["accepted"] is False
    assert channel_chat.handle_inbound("", "1", "hi")["accepted"] is False
    assert lookup == [], "trust was consulted for a malformed request"


# ── conversation identity ────────────────────────────────────────────────

def test_the_conversation_id_is_stable_across_messages(monkeypatch):
    monkeypatch.setattr(channel_chat, "trust_level",
                        lambda ch, s: "paired")
    monkeypatch.setattr(channel_chat, "_run_turn",
                        lambda *a, **k: {"ok": True})

    first = channel_chat.handle_inbound("telegram", "777", "hi")
    second = channel_chat.handle_inbound("telegram", "777", "again")

    assert first["conversation_id"] == second["conversation_id"], (
        "a returning user lost their history: the id is not stable")


def test_the_conversation_id_survives_a_restart():
    """`hash()` of a str is salted per interpreter, so an id derived from it
    changed on every restart and a returning Telegram user silently got a new,
    empty thread. The id must be a pure function of (channel, sender)."""
    import pathlib
    import subprocess
    import sys

    first = channel_chat.conversation_id_for("telegram", "5551234567")
    code_dir = str(pathlib.Path(channel_chat.__file__).parent)
    # A fresh interpreter re-salts hash(); a stable digest does not change.
    again = subprocess.run(
        [sys.executable, "-c",
         "import channel_chat; "
         "print(channel_chat.conversation_id_for('telegram', '5551234567'))"],
        capture_output=True, text=True, cwd=code_dir)
    assert first == again.stdout.strip(), (
        "the conversation id is not stable across processes — a restart "
        "would hand the user an empty history")
    assert channel_chat.conversation_id_for("telegram", "5551234567") == first


def test_two_senders_never_share_a_conversation(monkeypatch):
    monkeypatch.setattr(channel_chat, "trust_level",
                        lambda ch, s: "paired")
    monkeypatch.setattr(channel_chat, "_run_turn",
                        lambda *a, **k: {"ok": True})

    a = channel_chat.handle_inbound("telegram", "aaa", "hi")
    b = channel_chat.handle_inbound("telegram", "bbb", "hi")

    assert a["conversation_id"] != b["conversation_id"]


def test_the_same_id_on_two_channels_stays_two_conversations(monkeypatch):
    monkeypatch.setattr(channel_chat, "trust_level",
                        lambda ch, s: "paired")
    monkeypatch.setattr(channel_chat, "_run_turn",
                        lambda *a, **k: {"ok": True})

    tg = channel_chat.handle_inbound("telegram", "same-id", "hi")
    sl = channel_chat.handle_inbound("slack", "same-id", "hi")

    assert tg["conversation_id"] != sl["conversation_id"]


def test_the_mapping_persists(monkeypatch, tmp_path):
    monkeypatch.setattr(channel_chat, "trust_level",
                        lambda ch, s: "paired")
    monkeypatch.setattr(channel_chat, "_run_turn",
                        lambda *a, **k: {"ok": True})
    channel_chat.handle_inbound("telegram", "999", "hi")

    # Recreate the module's view of the file the way a restart would.
    import json
    data = json.loads((tmp_path / "chan.json").read_text(encoding="utf-8"))
    assert "telegram:999" in data


# ── in-flight guard ──────────────────────────────────────────────────────

def test_a_second_message_while_a_turn_runs_is_refused(monkeypatch):
    """Three rapid replies must not start three paid runs."""
    import threading as _threading

    monkeypatch.setattr(channel_chat, "trust_level",
                        lambda ch, s: "paired")
    started = []

    class _FakeThread:
        def __init__(self, *a, **k):
            started.append(k.get("args") or a)

        def start(self):
            # Deliberately does NOT run the body: the point is that the second
            # message is refused before a thread is even created.
            started.append("started")

    monkeypatch.setattr(_threading, "Thread", _FakeThread)

    first = channel_chat.handle_inbound("telegram", "555", "one")
    second = channel_chat.handle_inbound("telegram", "555", "two")

    assert first["accepted"] is True
    assert second["accepted"] is False
    assert "already running" in second["reason"]
    # One thread created, and it never ran.
    assert started.count("started") == 1


def test_the_inflight_entry_is_cleared_when_the_turn_ends(monkeypatch):
    monkeypatch.setattr(channel_chat, "trust_level",
                        lambda ch, s: "paired")
    monkeypatch.setattr(channel_chat, "_run_turn",
                        lambda *a, **k: {"ok": True})

    channel_chat.handle_inbound("telegram", "666", "hi")
    # The real thread runs _safe_turn, which clears the entry in its finally.
    import threading as _t
    for th in _t.enumerate():
        if th.name.startswith("chan-"):
            th.join(timeout=30)

    assert "telegram:666" not in channel_chat._INFLIGHT


# ── routing: the reminder owns its own replies ───────────────────────────

def _no_thread(*a, **k):
    """Stand in for ack_bridge when no reminder is open."""
    return {"routed": False, "reason": "no open thread for this sender",
            "schedule_id": "", "verdict": "", "reply_sent": False}


def _thread_reply(verdict="acknowledge"):
    """Stand in for ack_bridge when a reminder thread consumed the reply."""
    def _inner(*a, **k):
        return {"routed": verdict == "acknowledge", "reason": "handled",
                "schedule_id": "sch-abc123", "verdict": verdict,
                "reply_sent": verdict == "reply"}
    return _inner


def test_a_reply_to_a_reminder_never_becomes_a_chat_turn(monkeypatch):
    """"ok" is an answer to a reminder, not a new question. If a thread is open
    the ack path owns the message outright — even when its verdict is `silent`
    and no reply is needed."""
    import ack_bridge
    monkeypatch.setattr(ack_bridge, "handle_inbound", _thread_reply("silent"))
    chat_turn = []
    monkeypatch.setattr(channel_chat, "handle_inbound",
                        lambda *a, **k: chat_turn.append(a))

    out = channel_chat.route_inbound("telegram", "777", "k")

    assert out["path"] == "ack"
    assert out["schedule_id"] == "sch-abc123"
    assert out["verdict"] == "silent"
    assert chat_turn == [], (
        "a silent ack verdict was double-handled as a chat turn — the user "
        "would get a reply to a reaction")


def test_an_unrelated_message_falls_through_to_a_chat_turn(monkeypatch):
    import ack_bridge
    monkeypatch.setattr(ack_bridge, "handle_inbound", _no_thread)
    chat_turn = []
    monkeypatch.setattr(channel_chat, "handle_inbound",
                        lambda ch, s, t: chat_turn.append((ch, s, t))
                        or {"accepted": True, "conversation_id": "x"})

    out = channel_chat.route_inbound("telegram", "777", "what is 2+2?")

    assert out["path"] == "chat"
    assert chat_turn == [("telegram", "777", "what is 2+2?")]


def test_a_crash_in_the_ack_path_is_not_a_chat_turn(monkeypatch):
    """If ack routing blows up, the reply is lost — but it must not be
    answered as a general question, which would confuse the reminder flow."""
    import ack_bridge

    def _boom(*a, **k):
        raise RuntimeError("pairing store on fire")

    monkeypatch.setattr(ack_bridge, "handle_inbound", _boom)
    chat_turn = []
    monkeypatch.setattr(channel_chat, "handle_inbound",
                        lambda *a, **k: chat_turn.append(a))

    out = channel_chat.route_inbound("telegram", "777", "yes I took the pills")

    assert out["path"] == "ack"
    assert out["routed"] is False
    assert "ack routing failed" in out["reason"]
    assert chat_turn == []


# ── a live reminder must not swallow ordinary questions ──────────────────

def test_a_question_during_a_reminder_thread_is_answered_normally(monkeypatch):
    """The reported bug: while a reminder thread was open, EVERY message went
    to the reminder classifier, which usually answered `silent` — so a real
    question produced no reply at all and the agent looked like it was ignoring
    the user. `unrelated` is what lets it through."""
    import ack_bridge
    monkeypatch.setattr(ack_bridge, "handle_inbound",
                        lambda *a, **k: {"routed": False, "unrelated": True,
                                         "reason": "not about the reminder",
                                         "schedule_id": "", "verdict": "unrelated",
                                         "reply_sent": False})
    chat_turn = []
    monkeypatch.setattr(channel_chat, "handle_inbound",
                        lambda ch, s, t: chat_turn.append((ch, s, t))
                        or {"accepted": True, "conversation_id": "x"})

    out = channel_chat.route_inbound("telegram", "777", "what does the deploy script do?")

    assert out["path"] == "chat"
    assert out["ack_verdict"] == "unrelated"
    assert chat_turn == [("telegram", "777", "what does the deploy script do?")]


def test_the_ack_classifier_is_told_about_the_unrelated_verdict():
    """The closed set must name it. Left to itself the model answers `silent`
    for anything it cannot usefully answer, which is the swallowing behaviour."""
    import ack_bridge
    prompt = ack_bridge._DECIDE_PROMPT
    assert "unrelated" in prompt
    assert "swallowed" in prompt, (
        "the prompt does not tell the model that a wrong verdict loses the "
        "user's message entirely, which is why it must prefer `unrelated`")


def test_an_unrelated_verdict_leaves_the_thread_untouched(monkeypatch):
    """No reply recorded, no verdict applied: the reminder stays open and the
    follow-up budget is not spent on a message that was never a reply."""
    import ack
    import ack_bridge

    thread = {"schedule_id": "sch-1", "query": "check deploy",
              "prompt": "Reminder note", "channel": "telegram"}
    monkeypatch.setattr(ack, "find_thread_for", lambda ch, s: thread)
    monkeypatch.setattr(ack_bridge, "_ask_model",
                        lambda p: {"verdict": "unrelated", "text": ""})
    recorded, applied = [], []
    monkeypatch.setattr(ack, "record_user_reply",
                        lambda *a, **k: recorded.append(a))
    monkeypatch.setattr(ack, "apply_verdict",
                        lambda *a, **k: applied.append(a) or {"status": "open"})

    out = ack_bridge.handle_inbound("telegram", "7", "can you also check staging?")

    assert out["unrelated"] is True
    assert out["schedule_id"] == ""
    assert recorded == [] and applied == []


def test_a_silent_verdict_on_a_question_still_falls_through(monkeypatch):
    """Observed live: asked whether a message was about the reminder, the model
    answered `silent` to "In one sentence, what is a deployment pipeline?"
    because "deployment" appeared in the reminder text — and the question was
    consumed with no reply. The mechanical check overrides that."""
    import ack
    import ack_bridge

    thread = {"schedule_id": "sch-1", "query": "check deploy",
              "prompt": "Reminder note", "channel": "telegram"}
    monkeypatch.setattr(ack, "find_thread_for", lambda ch, s: thread)
    monkeypatch.setattr(ack_bridge, "_ask_model",
                        lambda p: {"verdict": "silent", "text": ""})
    applied = []
    monkeypatch.setattr(ack, "record_user_reply", lambda *a, **k: {})
    monkeypatch.setattr(ack, "apply_verdict",
                        lambda *a, **k: applied.append(a) or {"status": "open"})

    out = ack_bridge.handle_inbound(
        "telegram", "7", "In one sentence, what is a deployment pipeline?")

    assert out["unrelated"] is True
    assert applied == []


def test_real_acknowledgements_are_still_consumed_silently(monkeypatch):
    """The guard must not hand every "ok" to the chat turn — that would be
    noise, and it would keep the reminder thread open forever."""
    import ack
    import ack_bridge

    thread = {"schedule_id": "sch-1", "query": "check deploy",
              "prompt": "Reminder note", "channel": "telegram"}
    monkeypatch.setattr(ack, "find_thread_for", lambda ch, s: thread)
    monkeypatch.setattr(ack_bridge, "_ask_model",
                        lambda p: {"verdict": "silent", "text": ""})
    applied = []
    monkeypatch.setattr(ack, "record_user_reply", lambda *a, **k: {})
    monkeypatch.setattr(ack, "apply_verdict",
                        lambda *a, **k: applied.append(a) or {"status": "open"})

    for ack_text in ("ok", "thanks", "got it", "k", "will do", "👍"):
        out = ack_bridge.handle_inbound("telegram", "7", ack_text)
        assert out["unrelated"] is False, f"{ack_text!r} was treated as a request"
        assert out["schedule_id"] == "sch-1"
    assert len(applied) == 6


def test_looks_like_a_request_is_deliberate_about_the_short_side():
    from ack_bridge import looks_like_a_request

    assert not looks_like_a_request("ok")
    assert not looks_like_a_request("thanks!")
    assert not looks_like_a_request("")
    assert looks_like_a_request("what time is it?")
    assert looks_like_a_request("please summarise yesterday's deploy log")
    assert looks_like_a_request("can you check staging")


def test_a_second_message_is_answered_not_silently_dropped(monkeypatch):
    """Two quick messages used to mean the second vanished with no trace."""
    import threading as _threading

    monkeypatch.setattr(channel_chat, "trust_level", lambda ch, s: "paired")
    told = []
    monkeypatch.setattr(channel_chat, "_tell",
                        lambda ch, s, text, **k: told.append(text))

    class _FakeThread:
        """Never runs the body: the assertion is that the second message was
        answered, not that a turn started twice."""
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    monkeypatch.setattr(_threading, "Thread", _FakeThread)

    first = channel_chat.handle_inbound("telegram", "31", "one")
    second = channel_chat.handle_inbound("telegram", "31", "two")

    assert first["accepted"] is True
    assert second["accepted"] is False
    assert told and "previous message" in told[0]


def test_a_non_text_message_gets_an_explanation(monkeypatch):
    """A sticker or an unsupported attachment arrives with nothing readable.
    Silence there reads as the agent ignoring you.

    Files the adaptor CAN read (document/photo/voice) no longer land here -
    they carry an attachment and start a real turn. What remains is genuinely
    empty, so the reply asks them to resend rather than claiming it can only
    read text, which is no longer true."""
    monkeypatch.setattr(channel_chat, "trust_level", lambda ch, s: "paired")
    told = []
    monkeypatch.setattr(channel_chat, "_tell",
                        lambda ch, s, text, **k: told.append(text))
    turn = []
    monkeypatch.setattr(channel_chat, "_run_turn",
                        lambda *a, **k: turn.append(a))

    out = channel_chat.handle_inbound("telegram", "41", "   ")

    assert out["accepted"] is False
    assert turn == []
    assert told, "the user was told nothing about an empty message"
    assert "read" in told[0].lower()


def test_a_file_only_message_starts_a_turn(monkeypatch):
    """The other half: a document with no caption is a real request, not an
    empty one. It used to be rejected as malformed, which is what made
    'send me this file' impossible."""
    monkeypatch.setattr(channel_chat, "trust_level", lambda ch, s: "paired")
    told = []
    monkeypatch.setattr(channel_chat, "_tell",
                        lambda ch, s, text, **k: told.append(text))
    turn = []
    monkeypatch.setattr(channel_chat, "_run_turn",
                        lambda *a, **k: turn.append(a))
    monkeypatch.setattr(channel_chat, "_safe_turn",
                        lambda *a, **k: turn.append(a))

    out = channel_chat.handle_inbound(
        "telegram", "41", "",
        attachments=[{"kind": "file", "name": "notes.md", "size": 12}])

    assert out["accepted"] is True, "a file with no caption was refused"
    assert turn, "no agent turn was started for an inbound file"
    assert not told, "the user was told the file was unreadable"


# ── the drift that caused this whole bug ─────────────────────────────────

def test_the_catalog_schema_matches_the_registered_mcp_tool():
    """`skills._TOOL_CATALOG` is what the model is handed; `mcp_server` is what
    actually runs. They are two hand-maintained copies of one contract, and
    when they disagreed the model was offered a 3-argument `schedule_task`
    while the registered tool accepted nine — so it could not be told that
    `channel` existed, and told the user it could not notify them anywhere.

    Every argument the tool accepts must appear in the advertised schema.
    """
    import asyncio
    import mcp_server
    import skills

    registered = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    for name in ("schedule_task", "list_channels", "check_channel"):
        assert name in registered, f"{name} is not a registered MCP tool"
        assert name in skills._TOOL_CATALOG, (
            f"{name} is missing from skills._TOOL_CATALOG, so tool_payload "
            f"drops it and no model ever sees it")

        props = set((registered[name].inputSchema or {}).get("properties") or {})
        advertised = set(skills._TOOL_CATALOG[name]
                         ["input_schema"].get("properties") or {})
        missing = props - advertised
        assert not missing, (
            f"{name}: the model is not offered {sorted(missing)} — the "
            f"registered tool accepts them, so it will invent a shape and "
            f"be refused")


def test_the_three_notification_tools_reach_the_chat_model():
    """Being in the catalogue is not enough; they must also be in `_CHAT_TOOLS`,
    which is what the chat endpoint actually advertises."""
    import agent_server
    from skills import tool_payload

    names = {t.get("name") for t in (tool_payload(agent_server._CHAT_TOOLS) or [])}
    for n in ("schedule_task", "list_channels", "check_channel"):
        assert n in names, f"{n} never reaches the chat model"


def test_the_system_prompt_mentions_the_channel_arguments():
    """The tool schema alone is not enough: `_CHAT_TOOLS_SYSTEM`'s REMINDERS
    paragraph used to name only query/when, so a model following it never
    looked at the rest of the schema."""
    import agent_server
    prompt = agent_server._CHAT_SYSTEM
    for kw in ("channel", "lead_s", "list_channels"):
        assert kw in prompt, (
            f"_CHAT_SYSTEM never mentions {kw!r}; a model told only about "
            f"query/when will not discover the notification arguments")
    assert "TELEGRAM AS A CHAT" in prompt, (
        "the prompt does not tell the model a channel message is a chat turn")


def test_the_self_token_falls_back_to_the_file(monkeypatch, tmp_path):
    """A channel turn calls the agent's own chat route, which requires the
    per-launch token. Inside the server `auth.token()` has it. In any other
    process — a CLI, a worker — it does not, and without the file fallback the
    self-call 403s and every Telegram reply fails with an auth error."""
    import agent_server
    import auth

    monkeypatch.setattr(auth, "_token", None)
    monkeypatch.setattr(auth, "state_dir", lambda: tmp_path)
    tmp_path.joinpath("agent.token").write_text("token-from-file",
                                                encoding="utf-8")

    assert agent_server._self_token() == "token-from-file"


def test_the_self_token_is_empty_when_neither_exists(monkeypatch, tmp_path):
    """No token and no file (the `ARIA_AUTH=off` case): the header is absent
    and the endpoint does not check it."""
    import agent_server
    import auth

    monkeypatch.setattr(auth, "_token", None)
    monkeypatch.setattr(auth, "state_dir", lambda: tmp_path)

    assert agent_server._self_token() == ""


def test_the_chat_turn_targets_the_lightweight_stream_not_the_dag():
    """`POST /api/chat` runs the full research DAG. "what time is it" must not
    cost a planner/researcher/formatter run."""
    src = Path(channel_chat.__file__).read_text(encoding="utf-8")
    assert "/api/chat/simple/stream" in src, (
        "the channel turn must use the lightweight chat stream")
    assert '"/api/chat"' not in src and "'/api/chat'" not in src, (
        "the channel turn targets the research DAG endpoint; a Telegram "
        "message would launch a multi-node paid run")


# ── a long answer is split, not cut at 3500 ───────────────────────────────

def test_chunks_keep_every_character_and_respect_the_limit():
    text = "\n\n".join("Sentence number %d is here. " % i * 40
                       for i in range(12))
    pieces = channel_chat._chunks(text)
    assert len(pieces) > 1
    assert all(len(p) <= channel_chat._MAX_REPLY for p in pieces)
    assert " ".join(" ".join(pieces).split()) == " ".join(text.split())


def test_a_long_channel_answer_arrives_as_several_messages(monkeypatch):
    """Live: asking Telegram for a report sent ONE message containing the
    first 3500 characters of a 17k-character report - the rest never arrived,
    although the console path chunks."""
    long_answer = "\n\n".join(f"Paragraph {i}. " * 60 for i in range(8))
    sent = []

    monkeypatch.setattr(channel_chat, "_ask_agent",
                        lambda text, cid, atts=None:
                        (long_answer, "", []))
    import notify
    monkeypatch.setattr(
        notify, "send_channel_message",
        lambda ch, text, **kw: (sent.append((text, kw)),
                                {"delivered": True})[1])

    out = channel_chat._run_turn("telegram", "555", "the report", "ct-long")

    assert out["ok"] is True
    assert out["chunks"] == len(sent) > 1
    assert all(len(text) <= channel_chat._MAX_REPLY for text, _ in sent)
    assert " ".join(" ".join(t for t, _ in sent).split()) == \
        " ".join(long_answer.split()), "chunking must not drop characters"


def test_media_rides_the_first_chunk_only(monkeypatch):
    long_answer = "\n\n".join(f"Paragraph {i}. " * 60 for i in range(6))
    sent = []

    monkeypatch.setattr(channel_chat, "_ask_agent",
                        lambda text, cid, atts=None:
                        (long_answer, "", [{"artifact": "art:x",
                                            "filename": "evs.pdf"}]))
    monkeypatch.setattr(channel_chat, "_channel_media",
                        lambda ch, produced: [{"filename": "evs.pdf",
                                               "data_b64": "JVBERi0=",
                                               "caption": "evs.pdf"}])
    import notify
    monkeypatch.setattr(
        notify, "send_channel_message",
        lambda ch, text, **kw: (sent.append(kw),
                                {"delivered": True})[1])

    out = channel_chat._run_turn("telegram", "555", "make a pdf", "ct-media")

    assert out["ok"] is True
    assert sent[0].get("media"), "the attachment must be on the first message"
    assert all(not kw.get("media") for kw in sent[1:])


def test_a_failed_chunk_says_how_much_arrived(monkeypatch):
    long_answer = "\n\n".join(f"Paragraph {i}. " * 60 for i in range(6))
    calls = []

    monkeypatch.setattr(channel_chat, "_ask_agent",
                        lambda text, cid, atts=None: (long_answer, "", []))
    import notify

    def _send(ch, text, **kw):
        calls.append(text)
        if len(calls) == 2:
            return {"delivered": False, "error": "telegram 429"}
        return {"delivered": True}

    monkeypatch.setattr(notify, "send_channel_message", _send)

    out = channel_chat._run_turn("telegram", "555", "q", "ct-fail")

    assert out["ok"] is True
    assert "telegram 429" in out["error"]
    assert "after 1 of" in out["error"]
