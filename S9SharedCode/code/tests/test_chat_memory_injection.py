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
                        lambda q, s, d=None: "REMEMBERED: user is in IST")
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
    monkeypatch.setattr(agent_server, "_chat_memory_block",
                        lambda q, s, d=None: "")
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "q"}]
    assert await agent_server._with_chat_memory(msgs, "q", "s1") == msgs


def test_chat_simple_endpoints_both_call_memory(monkeypatch):
    """Structural pin on the twin-drift bug."""
    import inspect

    for handler in (agent_server.chat_simple, agent_server.chat_simple_stream):
        assert "_with_chat_memory" in inspect.getsource(handler), (
            f"{handler.__name__} does not inject chat memory")


# ── the recall gate: a greeting must not pay for a corpus search ──────────
"""A bare "hi" ran the full memory recall anyway.

`_with_chat_memory` called `_chat_memory_block` with no predicate, and that
block issues `POST /v1/memory/search` over the episode, fact, profile *and
document* drawers — one query embed fanned out across 177 enabled documents.
The gateway ledger has the row: `calls.id=107140`, prompt_chars 2,
`latency_ms=35550`. Warm, that embed is 2.1s; when Ollama has evicted
nomic-embed-text it is 25-51s, and the agent awaits it before the model is
even asked. The 35-second "search" the user timed was a two-letter greeting.

Skipping it is a behaviour change, so it has a switch (`chat.recall_gate`) and
a truth table. The gate is in the CALLER, never inside `_chat_memory_block`:
that function's contract is "render whatever recall found", and tests above
call it with "hi" on purpose.
"""

SKIP = ["hi", "Hi there!", "hey", "thanks", "gm", "good morning",
        "how are you?", "what's up", "hello", "nope", "ok"]
KEEP = ["who am I?", "what units do I use?", "continue",
        "hi what did we decide about the deck", "remember my timezone",
        "what do you know about me", "", "hey what is 2+2", "summarise it"]


def test_the_gate_skips_openers_only():
    for q in SKIP:
        assert agent_server._is_small_talk(q) is True, f"{q!r} should skip recall"
    for q in KEEP:
        assert agent_server._is_small_talk(q) is False, (
            f"{q!r} needs recall — a length rule would have caught this, which "
            f"is why the gate is a closed list of phrases")


async def test_greeting_does_not_search_but_keeps_the_profile(monkeypatch):
    """The profile is a standing read of the user and costs no embed, so a
    greeting still gets it. The corpus search is what a greeting cannot use."""
    calls: list[str] = []
    monkeypatch.setattr(agent_server, "_chat_memory_block",
                        lambda q, s, d=None: calls.append(q) or "SHOULD NOT APPEAR")
    monkeypatch.setattr(agent_server, "_chat_profile_block",
                        lambda: "NOTICED: they like terse answers")
    monkeypatch.setattr(agent_server, "_conversation_doc_ids", lambda *a: set())
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]

    out = await agent_server._with_chat_memory(msgs, "hi", "s-gate")

    assert calls == [], "a greeting triggered a corpus search"
    assert out is not msgs
    assert "NOTICED" in out[-2]["content"]


async def test_real_question_still_searches(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(agent_server, "_chat_memory_block",
                        lambda q, s, d=None: calls.append(q) or "REMEMBERED: metric")
    monkeypatch.setattr(agent_server, "_chat_profile_block", lambda: "")
    monkeypatch.setattr(agent_server, "_conversation_doc_ids", lambda *a: set())
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "what units do I use?"}]

    out = await agent_server._with_chat_memory(msgs, "what units do I use?", "s1")

    assert calls == ["what units do I use?"]
    assert "REMEMBERED" in out[-2]["content"]


async def test_the_kill_switch_restores_the_old_call(monkeypatch):
    """`chat.recall_gate` off must mean "recall on every turn", with no
    residue — the whole point of a switch you can turn a feature back off with."""
    import flags as _f
    calls: list[str] = []
    monkeypatch.setattr(_f, "is_enabled", lambda name, default=True:
                        False if name == "chat.recall_gate" else default)
    monkeypatch.setattr(agent_server, "_chat_memory_block",
                        lambda q, s, d=None: calls.append(q) or "")
    monkeypatch.setattr(agent_server, "_chat_profile_block", lambda: "")
    monkeypatch.setattr(agent_server, "_conversation_doc_ids", lambda *a: set())
    msgs = [{"role": "user", "content": "hi"}]

    await agent_server._with_chat_memory(msgs, "hi", "s-off")
    assert calls == ["hi"], "switching the gate off did not restore recall"


async def test_a_cold_embed_cannot_hang_the_turn(monkeypatch):
    """The gate does not help a real question, so the budget has to.

    A `to_thread` cannot be cancelled, so this bounds the WAIT, not the
    search — which is exactly the claim the test checks: the user gets an
    answer while a straggler thread finishes in the background.
    """
    import asyncio

    def _cold(q, s, d=None):
        import time
        time.sleep(3.0)
        return "REMEMBERED: too late to matter"

    monkeypatch.setattr(agent_server, "_chat_memory_block", _cold)
    monkeypatch.setattr(agent_server, "_chat_profile_block", lambda: "")
    monkeypatch.setattr(agent_server, "_conversation_doc_ids", lambda *a: set())
    monkeypatch.setattr(agent_server, "CHAT_RECALL_BUDGET_S", 0.2)
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "the question"}]

    started = asyncio.get_event_loop().time()
    out = await agent_server._with_chat_memory(msgs, "the question", "s-slow")
    waited = asyncio.get_event_loop().time() - started

    assert out == msgs, "a timed-out recall still modified the turn"
    assert waited < 1.5, f"the turn waited {waited:.1f}s for a dead search"
