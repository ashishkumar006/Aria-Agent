"""The earlier turns of a session are replayed whole, not summarised.

A coding agent's next turn continues the last one. Live symptom of the digest:
after a 75-call investigation, the follow-up produced "I don't see their results
in the conversation" and explored the same repository again.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_server as A  # noqa: E402
import studio_runs as runs  # noqa: E402


@pytest.fixture
def session(tmp_path, monkeypatch):
    def _paths(sid):
        (tmp_path / sid).mkdir(parents=True, exist_ok=True)
        return tmp_path / sid

    monkeypatch.setattr(runs, "runs_dir", lambda sid: _paths(sid))
    monkeypatch.setattr(runs, "_index_file", lambda sid: _paths(sid) / "index.json")
    monkeypatch.setattr(runs, "_transcript",
                        lambda sid, rid: _paths(sid) / f"{rid}.jsonl")
    sid = "st-replay"
    r = runs.start(sid, "explain the auth flow")
    runs.append(sid, r["id"], "thinking", {"text": "let me look"})
    runs.append(sid, r["id"], "tool_call",
                {"name": "read_file", "arguments": {"path": "auth.py"},
                 "id": "call_1"})
    runs.append(sid, r["id"], "tool_result",
                {"name": "read_file", "ok": True, "result": "TOKEN = 'x'",
                 "id": "call_1"})
    runs.append(sid, r["id"], "text", {"text": "It stores a token in auth.py."})
    runs.finish(r["id"], "finished")
    return sid, r["id"]


def test_the_tool_call_and_its_output_are_both_replayed(session):
    sid, first = session
    msgs = A._replay_prior_messages(sid, "r-new", 600_000)

    # The canonical shape is flat — {"id","name","arguments"} — because that is
    # what the gateway's ToolCall schema declares and what _translate_messages
    # reads. The OpenAI wire shape nests under `function`; sending that back in
    # raised KeyError('name') → a 502, and made every turn after the first in a
    # session fail.
    call = next(m for m in msgs if m.get("tool_calls"))
    assert set(call["tool_calls"][0]) >= {"id", "name", "arguments"}, call
    assert "function" not in call["tool_calls"][0]
    tool = [m for m in msgs if m.get("role") == "tool"]
    assert tool and "TOKEN = 'x'" in tool[0]["content"]
    # The model's own prose survives too.
    assert any("auth.py" in str(m.get("content") or "") for m in msgs)


def test_an_empty_assistant_turn_is_not_replayed(tmp_path, monkeypatch):
    """A turn whose only output was a malformed call must not replay as blank.

    Providers reject `{"role":"assistant","content":""}` with "Invalid request",
    which took out every later turn in the session.
    """
    def _paths(sid):
        (tmp_path / sid).mkdir(parents=True, exist_ok=True)
        return tmp_path / sid

    monkeypatch.setattr(runs, "runs_dir", lambda sid: _paths(sid))
    monkeypatch.setattr(runs, "_index_file", lambda sid: _paths(sid) / "index.json")
    monkeypatch.setattr(runs, "_transcript",
                        lambda sid, rid: _paths(sid) / f"{rid}.jsonl")

    sid = "st-blank"
    r = runs.start(sid, "go")
    runs.append(sid, r["id"], "tool_call",
                {"name": "", "arguments": {}, "id": "x"})
    runs.finish(r["id"], "finished")

    msgs = A._replay_prior_messages(sid, "r-new", 600_000)
    # The malformed call is dropped; what is left is nothing, and a blank
    # assistant message must not be replayed either.
    assert msgs == [], msgs


def test_thinking_is_not_replayed(session):
    sid, first = session
    msgs = A._replay_prior_messages(sid, "r-new", 600_000)
    assert "let me look" not in json.dumps(msgs)


def test_the_run_being_started_is_not_its_own_history(session):
    sid, first = session
    assert A._replay_prior_messages(sid, first, 600_000) == []


def test_older_runs_come_first_and_the_newest_last(session):
    sid, first = session
    second = runs.start(sid, "and the refresh?")
    runs.append(sid, second["id"], "text", {"text": "second turn"})
    runs.finish(second["id"], "finished")

    msgs = A._replay_prior_messages(sid, "r-new", 600_000)
    texts = [str(m.get("content") or "") for m in msgs]
    assert texts.index("It stores a token in auth.py.") < \
        texts.index("second turn")


def test_a_tight_budget_drops_oldest_messages_not_newest(session):
    sid, first = session
    second = runs.start(sid, "follow up")
    runs.append(sid, second["id"], "tool_call",
                {"name": "read_file", "arguments": {"path": "z.py"}, "id": "c9"})
    runs.append(sid, second["id"], "tool_result",
                {"name": "read_file", "ok": True, "result": "Z" * 5000, "id": "c9"})
    runs.finish(second["id"], "finished")

    msgs = A._replay_prior_messages(sid, "r-new", 2000)
    joined = json.dumps(msgs)
    assert "Z" * 100 in joined, "the newest tool output must survive"
    assert "It stores a token in auth.py." not in joined, \
        "the oldest prose is what the budget drops"


def test_a_tool_call_carries_the_prose_that_led_to_it(tmp_path, monkeypatch):
    """assistant→assistant pairs are refused upstream.

    The live loop sends one message with content + tool_calls; emitting the prose
    as its own message first produced two assistant messages in a row, which the
    provider rejects (surfaced here as `kilo failed: 'name'`).
    """
    def _paths(sid):
        (tmp_path / sid).mkdir(parents=True, exist_ok=True)
        return tmp_path / sid

    monkeypatch.setattr(runs, "runs_dir", lambda sid: _paths(sid))
    monkeypatch.setattr(runs, "_index_file", lambda sid: _paths(sid) / "index.json")
    monkeypatch.setattr(runs, "_transcript",
                        lambda sid, rid: _paths(sid) / f"{rid}.jsonl")

    sid = "st-prose"
    r = runs.start(sid, "look")
    runs.append(sid, r["id"], "text", {"text": "Let me read it."})
    runs.append(sid, r["id"], "tool_call",
                {"name": "read_file", "arguments": {"path": "a.py"}})
    runs.append(sid, r["id"], "tool_result",
                {"name": "read_file", "ok": True, "result": "x = 1"})
    runs.finish(r["id"], "finished")

    msgs = A._replay_prior_messages(sid, "r-new", 600_000)
    roles = [m["role"] for m in msgs]
    assert roles == ["assistant", "tool"], roles
    assert "Let me read it." in msgs[0]["content"]
    assert msgs[0]["tool_calls"][0]["name"] == "read_file"


def test_parallel_calls_stay_in_one_message(tmp_path, monkeypatch):
    """Parallel calls were one assistant message; the replay must keep them so.

    One message per call produced assistant→assistant pairs and orphaned
    results, which the provider refuses (masked as `kilo failed: 'name'`).
    """
    def _paths(sid):
        (tmp_path / sid).mkdir(parents=True, exist_ok=True)
        return tmp_path / sid

    monkeypatch.setattr(runs, "runs_dir", lambda sid: _paths(sid))
    monkeypatch.setattr(runs, "_index_file", lambda sid: _paths(sid) / "index.json")
    monkeypatch.setattr(runs, "_transcript",
                        lambda sid, rid: _paths(sid) / f"{rid}.jsonl")

    sid = "st-parallel"
    r = runs.start(sid, "read two")
    runs.append(sid, r["id"], "tool_call",
                {"name": "read_file", "arguments": {"path": "a.py"}, "id": "c1"})
    runs.append(sid, r["id"], "tool_call",
                {"name": "read_file", "arguments": {"path": "b.py"}, "id": "c2"})
    runs.append(sid, r["id"], "tool_result",
                {"name": "read_file", "ok": True, "result": "A", "id": "c1"})
    runs.append(sid, r["id"], "tool_result",
                {"name": "read_file", "ok": True, "result": "B", "id": "c2"})
    runs.finish(r["id"], "finished")

    msgs = A._replay_prior_messages(sid, "r-new", 600_000)
    roles = [m["role"] for m in msgs]
    assert roles == ["assistant", "tool", "tool"], roles
    calls = msgs[0]["tool_calls"]
    assert [c["id"] for c in calls] == ["c1", "c2"]
    assert [c["name"] for c in calls] == ["read_file", "read_file"]
    assert [m["tool_call_id"] for m in msgs[1:]] == ["c1", "c2"]


def test_no_previous_run_means_no_replay(tmp_path, monkeypatch):
    (tmp_path / "st-fresh").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runs, "runs_dir", lambda sid: tmp_path / sid)
    monkeypatch.setattr(runs, "_index_file", lambda sid: tmp_path / sid / "index.json")
    assert A._replay_prior_messages("st-fresh", "r-1", 600_000) == []


def test_every_tool_call_id_is_matched_by_its_result(tmp_path, monkeypatch):
    """A mismatched pair is a hard 400 upstream.

    Live: the replayed tool result fell back to the tool's NAME when the frame
    had no id, so the provider saw a result answering a call that never
        existed - and the client masked it as `kilo failed: 'name'`, which took
    down every later turn in that session.
    """
    def _paths(sid):
        (tmp_path / sid).mkdir(parents=True, exist_ok=True)
        return tmp_path / sid

    monkeypatch.setattr(runs, "runs_dir", lambda sid: _paths(sid))
    monkeypatch.setattr(runs, "_index_file", lambda sid: _paths(sid) / "index.json")
    monkeypatch.setattr(runs, "_transcript",
                        lambda sid, rid: _paths(sid) / f"{rid}.jsonl")

    sid = "st-noids"
    r = runs.start(sid, "do a thing")
    runs.append(sid, r["id"], "tool_call",
                {"name": "read_file", "arguments": {"path": "a.py"}})
    runs.append(sid, r["id"], "tool_result",
                {"name": "read_file", "ok": True, "result": "body"})
    runs.finish(r["id"], "finished")

    msgs = A._replay_prior_messages(sid, "r-new", 600_000)
    call = next(m for m in msgs if m.get("tool_calls"))
    tool = next(m for m in msgs if m.get("role") == "tool")
    assert call["tool_calls"][0]["id"] == tool["tool_call_id"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))