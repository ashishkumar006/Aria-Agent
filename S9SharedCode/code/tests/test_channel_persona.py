"""A channel turn must read like a friend texting, and only channel turns.

The persona note is a system message injected at request time - never written
into the stored thread - so the transcript remains the person's own words.
"""

import inspect

import agent_server

TELEGRAM = "telegram-069246b4b2a2"
CONSOLE = "ct-7c6f06ea"


def _msgs():
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "yo, how's it going"},
    ]


def test_telegram_turn_gets_the_friend_persona_adjacent_to_the_question():
    out = agent_server._with_channel_persona(_msgs(), TELEGRAM)

    assert out[-1]["content"] == "yo, how's it going"
    assert out[-2]["role"] == "system"
    assert "close friend" in out[-2]["content"]
    # Short, plain-text, phone-friendly - the failure modes we actually saw.
    assert "short" in out[-2]["content"]
    assert "no headings" in out[-2]["content"].lower()
    # Request-time only: the stored transcript is untouched.
    assert [m["content"] for m in out[:3]] == ["sys", "old", "reply"]


def test_console_turns_get_no_persona():
    msgs = _msgs()
    assert agent_server._channel_persona_block(CONSOLE) == ""
    assert agent_server._with_channel_persona(msgs, CONSOLE) == msgs


def test_missing_or_unknown_conversation_ids_are_a_noop():
    msgs = _msgs()
    for cid in (None, "", "research-abc", "slack-0123456789ab", "telegram"):
        assert agent_server._with_channel_persona(msgs, cid) == msgs, cid


def test_both_chat_endpoints_inject_the_channel_persona():
    """Twin-drift pin: the non-streaming and streaming handlers are separate
    code paths, and a channel reply must sound the same on either."""
    for handler in (agent_server.chat_simple, agent_server.chat_simple_stream):
        assert "_with_channel_persona" in inspect.getsource(handler), (
            f"{handler.__name__} does not apply the channel persona")
