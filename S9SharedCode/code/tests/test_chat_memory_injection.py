"""Chat must actually receive the user's stored memory.

The bug this pins down
----------------------
`flow.py` reads FAISS hits once per run and `skills.render_prompt` splices
them in as MEMORY HITS, so every skill saw the user's standing facts. Chat did
not: `chat_simple` and `chat_simple_stream` only called `_with_doc_context`.
The Memory page showed saved preferences, the `recall_preferences` tool could
fetch them, and neither had any effect on the one surface the user asks
"what do you remember?" on.

Both endpoints are covered, because they are separate code paths and the whole
class of bug here has been one endpoint fixed while its twin drifts.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import agent_server

HDR = {"X-Aria-Token": "test-token"}


@pytest.fixture()
def client():
    return TestClient(agent_server.app)


class _Hit:
    """Minimal stand-in for memory.MemoryItem as the formatter reads it."""

    def __init__(self, descriptor, kind="preference"):
        self.kind = kind
        self.descriptor = descriptor
        self.source = "test"
        self.keywords = ["test"]
        self.value = {}


def test_memory_block_includes_hit_descriptors(monkeypatch):
    import memory as mem

    monkeypatch.setattr(mem, "read",
                        lambda *a, **k: [_Hit("user prefers metric units")])
    monkeypatch.setattr(agent_server, "_gw_enabled_doc_ids", lambda: set())

    block = agent_server._chat_memory_block("what units do I use?", "s1")
    assert "user prefers metric units" in block
    assert "MEMORY HITS" in block


def test_memory_block_warns_against_conflation(monkeypatch):
    """The block must mark hits as cross-session, or the model will present
    a stored preference back as something said earlier in this thread."""
    import memory as mem

    monkeypatch.setattr(mem, "read", lambda *a, **k: [_Hit("user prefers dark mode")])
    monkeypatch.setattr(agent_server, "_gw_enabled_doc_ids", lambda: set())

    block = agent_server._chat_memory_block("hi", "s1")
    assert "THIS conversation" in block


def test_memory_block_empty_when_no_hits(monkeypatch):
    import memory as mem

    monkeypatch.setattr(mem, "read", lambda *a, **k: [])
    monkeypatch.setattr(agent_server, "_gw_enabled_doc_ids", lambda: set())

    assert agent_server._chat_memory_block("hi", "s1") == ""


def test_memory_recall_failure_never_breaks_chat(monkeypatch):
    """Fail-soft is the whole contract: a gateway outage must not 502 a turn."""
    import memory as mem

    def _boom(*a, **k):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(mem, "read", _boom)
    monkeypatch.setattr(agent_server, "_gw_enabled_doc_ids", lambda: set())

    assert agent_server._chat_memory_block("hi", "s1") == ""


def test_document_registry_failure_still_yields_hits(monkeypatch):
    """If the document registry is unreachable, memory should still be
    recalled rather than silently filtered to nothing."""
    import memory as mem

    def _bad_docs():
        raise RuntimeError("no documents endpoint")

    monkeypatch.setattr(agent_server, "_gw_enabled_doc_ids", _bad_docs)
    monkeypatch.setattr(mem, "read", lambda *a, **k: [_Hit("user is in IST")])

    assert "user is in IST" in agent_server._chat_memory_block("tz?", "s1")


async def test_with_chat_memory_inserts_before_final_user_turn(monkeypatch):
    """Ordering matters: the block must sit adjacent to the question, the
    same contract as document context.

    The block is stubbed. This test is about WHERE the block is inserted, and
    left reading the real memory store it silently depended on the store
    happening to be non-empty - so it passed or failed with whatever the user
    had remembered, and went red the moment memory was cleared.
    """
    monkeypatch.setattr(agent_server, "_chat_memory_block",
                        lambda q, s: "REMEMBERED: user is in IST")
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "the question"},
    ]
    out = await agent_server._with_chat_memory(msgs, "the question", "s1")

    assert out[-1]["content"] == "the question"
    assert out[-2]["role"] == "system"
    assert "REMEMBERED" in out[-2]["content"]
    # The original transcript must be intact around the insertion.
    assert [m["content"] for m in out[:3]] == ["sys", "old", "reply"]


async def test_with_chat_memory_is_a_noop_without_hits(monkeypatch):
    monkeypatch.setattr(agent_server, "_chat_memory_block", lambda q, s: "")
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "q"}]
    assert await agent_server._with_chat_memory(msgs, "q", "s1") == msgs


def test_chat_simple_endpoints_both_call_memory(monkeypatch):
    """Structural pin on the twin-drift bug."""
    import inspect

    for handler in (agent_server.chat_simple, agent_server.chat_simple_stream):
        assert "_with_chat_memory" in inspect.getsource(handler), (
            f"{handler.__name__} does not inject chat memory")
