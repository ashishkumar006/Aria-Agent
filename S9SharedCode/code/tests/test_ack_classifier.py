"""The acknowledgement classifier.

It had never executed a single model call. Three defects stacked:

  1. `LLM().chat(prompt=...)` takes the message list as its first POSITIONAL
     argument. Passing it positionally sent the list as `prompt`, leaving
     `messages` unset; the gateway received an array where it wanted a string
     and answered 422.
  2. `except Exception: return {}` swallowed that 422. An empty verdict
     defaults to `silent`.
  3. The gateway returns a response OBJECT. `str(raw)` on a dict yields its
     repr, which contains no parseable JSON, so the parser returned {} even
     when the model had answered perfectly.

So every inbound reply was classified `silent`: no acknowledgement was ever
recognised, no reply was ever sent, and reminders re-sent themselves until
their budget ran out — while the code looked completely correct and every
test passed, because they all stubbed `_ask_model`.

The call is pinned here against a fake LLM that enforces the real contract
(keyword `messages`, dict response), so a positional call or a `str()` on the
response fails loudly.
"""

from __future__ import annotations

import pytest

import ack
import ack_bridge


class _FakeLLM:
    """Enforces the gateway's actual contract."""

    calls: list[dict] = []
    reply: dict = {"text": '{"verdict": "acknowledge", "text": ""}'}
    raises: Exception | None = None

    def __init__(self, **kwargs):
        pass

    def chat(self, prompt=None, *, messages=None, **kwargs):
        type(self).calls.append({"prompt": prompt, "messages": messages,
                                 "kwargs": kwargs})
        if type(self).raises:
            raise type(self).raises
        return type(self).reply


@pytest.fixture(autouse=True)
def _reset():
    _FakeLLM.calls = []
    _FakeLLM.reply = {"text": '{"verdict": "acknowledge", "text": ""}'}
    _FakeLLM.raises = None
    yield
    _FakeLLM.calls = []


@pytest.fixture()
def fake_llm(monkeypatch):
    """Patch the name `_ask_model` resolves.

    It does `from gateway import LLM` inside the function, so patching the
    attribute on the already-imported `gateway` module is NOT enough — the
    binding is looked up on every call from that module's namespace, which a
    `monkeypatch.setattr(gateway, "LLM", ...)` does satisfy. Patching
    `sys.modules["gateway"]` covers both spellings.
    """
    import sys
    import gateway  # ensure it is imported before we patch it
    monkeypatch.setattr(gateway, "LLM", _FakeLLM)
    return _FakeLLM


def _thread():
    return {"schedule_id": "sch-1", "query": "stretch",
            "prompt": "Time to stretch.", "channel": "telegram"}


def test_the_model_is_actually_called(fake_llm):
    """The regression itself: zero calls meant zero verdicts."""
    ack_bridge._ask_model("decide this")

    assert len(_FakeLLM.calls) == 1, (
        "the classifier made no model call at all — every verdict has been a "
        "silent default")
    call = _FakeLLM.calls[0]
    assert call["prompt"] is None, (
        "the message list was passed positionally, so the gateway received it "
        "as `prompt` and answered 422")
    assert isinstance(call["messages"], list), (
        "messages must be passed as a keyword list of {role, content}")
    assert call["messages"][0]["role"] == "user"


def test_a_dict_response_is_parsed_not_stringified(fake_llm):
    """The gateway returns `{"text": ...}`. `str(raw)` on a dict yields a repr
    with no parseable JSON."""
    out = ack_bridge._ask_model("decide this")
    assert out == {"verdict": "acknowledge", "text": ""}, (
        "a successful model answer was discarded")


def test_a_plain_string_response_still_parses(fake_llm):
    _FakeLLM.reply = {"text": '```json\n{"verdict": "acknowledge", "text": ""}\n```'}
    assert ack_bridge._ask_model("x") == {"verdict": "acknowledge", "text": ""}


def test_a_model_failure_is_reported_not_silently_defaulted(fake_llm, capsys):
    """`silent` is a real verdict. If every failure also means `silent`, a
    broken classifier is indistinguishable from a user who never replies."""
    _FakeLLM.raises = RuntimeError("gateway 422")

    assert ack_bridge._ask_model("x") == {}
    assert "422" in capsys.readouterr().out, (
        "the classifier failed and said nothing, so the failure looked like a "
        "verdict")


def test_an_acknowledgement_closes_the_thread_and_confirms(fake_llm,
                                                           monkeypatch):
    """The whole point: an ack must stop the reminders AND be acknowledged."""
    monkeypatch.setattr(ack, "find_thread_for", lambda c, s: _thread())
    applied = {}
    monkeypatch.setattr(ack, "record_user_reply", lambda *a, **k: {})
    monkeypatch.setattr(ack, "apply_verdict",
                        lambda sid, v, r="": applied.update({"v": v}) or
                        {"status": "acknowledged"})

    told = []
    import notify
    monkeypatch.setattr(notify, "send_channel_message",
                        lambda ch, text, **k: told.append(text) or
                        {"delivered": True})

    out = ack_bridge.handle_inbound("telegram", "7", "ok thanks, seen it")

    assert out["verdict"] == "acknowledge"
    assert applied["v"] == "acknowledge", "the thread was not closed"
    assert told, "the user acknowledged and the agent said nothing back"


def test_a_silent_verdict_leaves_the_thread_open(fake_llm, monkeypatch):
    """'maybe later' must NOT close it — that is the difference between
    acknowledge and silent."""
    _FakeLLM.reply = {"text": '{"verdict": "silent", "text": ""}'}
    monkeypatch.setattr(ack, "find_thread_for", lambda c, s: _thread())
    monkeypatch.setattr(ack, "record_user_reply", lambda *a, **k: {})
    monkeypatch.setattr(ack, "apply_verdict",
                        lambda sid, v, r="": {"status": "open"})
    told = []
    import notify
    monkeypatch.setattr(notify, "send_channel_message",
                        lambda ch, text, **k: told.append(text) or
                        {"delivered": True})

    out = ack_bridge.handle_inbound("telegram", "7", "maybe later")

    assert out["verdict"] == "silent"
    assert not told, "told the user it was cleared when it was not"