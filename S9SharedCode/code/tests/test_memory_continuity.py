"""P1 — Multi-turn memory continuity (deterministic, no live LLM/gateway).

Proves the defining "agent" behaviour: turn B can recall a fact stored
during turn A *through the real orchestrator path* (memory.read at session
start, hits injected into every skill prompt).

We drive the REAL `flow.Executor` with a stubbed LLM so the test is fast,
deterministic, and needs no credentials. The mock returns canned JSON per
skill; the orchestrator's memory plumbing is exercised end-to-end.

The durable store lives on the gateway now (covered by
llm_gatewayV9/tests/test_memory.py), so `memory.read` / `memory.remember`
are backed here by a small deterministic in-memory fake. What this suite
owns is the PLUMBING contract: writes go out, reads come back at the next
session start, hits land in skill prompts.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import flow
from schemas import AgentResult, MemoryItem, new_id


# ── mock LLM ─────────────────────────────────────────────────────────────────

class _FakeLLM:
    """Scripted LLM keyed by skill name. Tracks every call."""
    def __init__(self, script: dict[str, dict]):
        self._script = script
        self.calls: list[tuple[str, str]] = []

    def chat(self, prompt=None, *, messages=None, system=None, agent=None,
             session=None, **kw):
        skill = agent or "formatter"
        # Store the FULL prompt so assertions can search for memory-hit text
        # anywhere in it, not just the first 80 chars.
        self.calls.append((skill, prompt or ""))
        payload = self._script.get(skill, {"final_answer": f"answer from {skill}"})
        return {"text": json.dumps(payload), "provider": "fake",
                "input_tokens": 10, "output_tokens": 10}

    def vision(self, *a, **kw):
        return {"text": json.dumps({"content": "vision ok"}), "provider": "fake"}


async def _fake_run_with_tools(**kw):
    """Drop-in replacement for mcp_runner.run_with_tools that avoids real
    network calls and records the rendered prompt so tests can assert memory
    hits reached tool-using skills (researcher, action, etc.).

    Must be async: skills.py awaits run_with_tools (see
    test_orchestrator_integration.py's fake). A sync fake records the call
    but then blows up on `await dict`, failing the node and sending the
    run down the nondeterministic recovery path.
    """
    agent = kw.get("agent", "unknown")
    prompt = kw.get("prompt", "")
    _fake_run_with_tools.calls.append((agent, prompt))
    payload = _fake_run_with_tools._script.get(agent, {"final_answer": f"answer from {agent}"})
    return {"text": json.dumps(payload), "provider": "fake"}


_fake_run_with_tools.calls: list[tuple[str, str]] = []
_fake_run_with_tools._script: dict = {}


# ── in-memory fake for the gateway memory client ─────────────────────────────

_STOP = {"the", "is", "a", "an", "of", "to", "and", "or", "in", "on", "for",
        "at", "with", "by", "from", "what", "how", "when", "where", "why",
        "this", "that", "it", "be", "as", "are", "was", "were"}


def _toks(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.lower())
            if w not in _STOP and len(w) > 2}


class _FakeMemory:
    """Deterministic stand-in for the gateway store: remember appends,
    read does token-overlap ranking (the same fallback rule the gateway
    uses when vectors are unavailable)."""

    def __init__(self):
        self.items: list[MemoryItem] = []
        self.last_kwargs: dict = {}
        self.policy_notes: list[MemoryItem] = []

    def policies(self, limit: int = 50) -> list[MemoryItem]:
        return list(self.policy_notes[:limit])

    def remember(self, raw_text, *, source, run_id, goal_id=None,
                 session_id=None):
        item = MemoryItem(id=new_id("mem"), kind="fact",
                          keywords=sorted(_toks(raw_text))[:10],
                          descriptor=raw_text[:200],
                          value={"raw": raw_text},
                          embedding=None, source=source, run_id=run_id,
                          goal_id=goal_id)
        self.items.append(item)
        return item

    def read(self, query, history=None, *, kinds=None, top_k=8,
             session_id=None, drawers=None, doc_ids=None):
        # doc_ids restricts which uploaded documents may answer. The fake
        # carries no document records, so it is recorded and ignored.
        self.last_kwargs = {"kinds": kinds, "top_k": top_k,
                            "session_id": session_id, "drawers": drawers,
                            "doc_ids": doc_ids}
        qtoks = _toks(query)
        scored = []
        for item in self.items:
            if kinds and item.kind not in kinds:
                continue
            score = len(qtoks & (set(item.keywords) | _toks(item.descriptor)))
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda x: -x[0])
        return [i for _, i in scored[:top_k]]


@pytest.fixture()
def fake_memory(monkeypatch):
    """Route the real `memory` module's read/remember/clear/policies
    through the fake (or a stub) — no network, ever."""
    import memory as mem

    fake = _FakeMemory()
    monkeypatch.setattr(mem, "read", fake.read)
    monkeypatch.setattr(mem, "remember", fake.remember)
    monkeypatch.setattr(mem, "policies", fake.policies)
    monkeypatch.setattr(mem, "clear",
                        lambda session_id=None, drawers=None,
                        older_than=None: None)
    return fake


def _run(query: str, script: dict, *, session_id: str | None = None,
         llm_cls=None) -> tuple[str, _FakeLLM]:
    fake = (llm_cls or _FakeLLM)(script)
    # Reset the run_with_tools call log and script for this run.
    _fake_run_with_tools.calls.clear()
    _fake_run_with_tools._script = script
    with mock.patch.object(flow, "ensure_gateway", lambda: None), \
         mock.patch("skills.LLM", lambda: fake), \
         mock.patch("mcp_runner.run_with_tools", _fake_run_with_tools):
        answer = asyncio.run(
            flow.Executor().run(query, session_id=session_id or "t-mem-1")
        )
    return answer, fake


# ── tests ─────────────────────────────────────────────────────────────────────

class TestMemoryContinuity:
    """Multi-turn continuity through the real orchestrator."""

    def test_turn_b_recalls_fact_from_turn_a(self, fake_memory):
        """Turn A stores a preference; turn B asks for it and gets it back.

        This is the core 'agent memory' contract: the orchestrator reads
        memory at session start and injects hits into every skill prompt.
        """
        # Seed a fact that turn B will need.
        fake_memory.remember(
            "User's favourite city is Seville.",
            source="user_query", run_id="t-mem-1",
        )

        # Turn A: the agent stores the fact (already done above). Now turn B
        # asks for it. We mock the LLM so the planner/formatter just echo
        # back what they see in the prompt.
        script = {
            "planner": {
                "nodes": [
                    {"skill": "formatter", "inputs": ["USER_QUERY"], "metadata": {}},
                ],
            },
            "formatter": {
                "final_answer": (
                    "Based on what I know, your favourite city is Seville."
                ),
            },
        }
        answer, fake = _run(
            "What is my favourite city?",
            script,
            session_id="t-mem-1",
        )
        # The formatter's answer must mention Seville.
        assert "seville" in answer.lower(), f"memory fact not recalled: {answer!r}"

        # CONTRACT UPDATE (F9): the formatter deliberately does NOT see
        # memory hits (_MEMORY_EXCLUDED_SKILLS) — weaving raw memory into
        # the terminal answer caused contamination bugs. Memory reaches the
        # answer via the PLANNER, which reads hits and wires them into its
        # plan / upstream data. Assert the planner saw the fact instead.
        planner_prompts = [c[1] for c in fake.calls if c[0] == "planner"]
        assert planner_prompts, "planner was never called"
        combined = "\n".join(planner_prompts).lower()
        assert "seville" in combined, \
            "memory hit not injected into planner prompt (S7 contract broken)"
        # And confirm the formatter prompt is memory-free by design.
        formatter_prompts = [c[1] for c in fake.calls if c[0] == "formatter"]
        assert formatter_prompts, "formatter was never called"
        fmt_combined = "\n".join(formatter_prompts).lower()
        assert "memory hits" not in fmt_combined, \
            "formatter must not receive MEMORY HITS (F9 exclusion)"

    def test_memory_hits_visible_to_researcher(self, fake_memory):
        """Memory hits must appear in a researcher's prompt, not just the
        formatter's."""
        fake_memory.remember(
            "Python 3.12 was released in October 2023.",
            source="user_query", run_id="t-mem-2",
        )

        script = {
            "planner": {
                "nodes": [
                    {"skill": "researcher", "inputs": ["USER_QUERY"], "metadata": {}},
                    {"skill": "formatter", "inputs": ["n:1"], "metadata": {}},
                ],
            },
            "researcher": {"final_answer": "research result"},
            "formatter": {"final_answer": "formatted answer"},
        }
        answer, fake = _run(
            "When was Python 3.12 released?",
            script,
            session_id="t-mem-2",
        )
        researcher_prompts = [c[1] for c in _fake_run_with_tools.calls if c[0] == "researcher"]
        assert researcher_prompts, "researcher was never called"
        combined = "\n".join(researcher_prompts).lower()
        assert "python" in combined and "3.12" in combined, \
            f"memory hit missing from researcher prompt: {combined[:200]}"

    def test_empty_memory_does_not_break_run(self, fake_memory):
        """When memory is empty, the orchestrator must still complete."""
        # Don't write anything — empty memory.

        script = {
            "planner": {
                "nodes": [
                    {"skill": "formatter", "inputs": ["USER_QUERY"], "metadata": {}},
                ],
            },
            "formatter": {"final_answer": "No memory, but answer."},
        }
        answer, _ = _run("hi", script, session_id="t-mem-3")
        assert answer.strip() == "No memory, but answer."

    def test_session_start_uses_drawer_set(self, fake_memory):
        """Session-start recall passes the Phase 2 drawer set (everything
        but run-scoped working notes)."""
        import memory as mem

        script = {
            "planner": {
                "nodes": [
                    {"skill": "formatter", "inputs": ["USER_QUERY"], "metadata": {}},
                ],
            },
            "formatter": {"final_answer": "ok"},
        }
        _run("hi", script, session_id="t-mem-4")
        assert fake_memory.last_kwargs["drawers"] == mem.SESSION_DRAWERS
        assert "working" not in fake_memory.last_kwargs["drawers"]

    def test_run_end_purges_working(self, monkeypatch):
        """The run-end hook purges only this run's old working notes."""
        import flow

        seen = {}

        def fake_clear(session_id=None, drawers=None, older_than=None):
            seen.update(session_id=session_id, drawers=drawers,
                        older_than=older_than)

        monkeypatch.setattr(flow.memory_svc, "clear", fake_clear)
        flow._safe_purge_working("sess-1", "2026-09-07T00:00:00+00:00")
        assert seen["session_id"] == "sess-1"
        assert seen["drawers"] == ["working"]
        assert seen["older_than"] == "2026-09-07T00:00:00+00:00"

    def test_policies_reach_excluded_skills(self, fake_memory):
        """Phase 3: active policies inject into EVERY skill prompt — even
        the formatter, which never sees memory hits (F9 exclusion)."""
        fake_memory.policy_notes = [MemoryItem(
            id="mem:pol1", kind="fact", descriptor="never email passwords",
            source="dashboard", run_id="op-1")]
        script = {
            "planner": {
                "nodes": [
                    {"skill": "formatter", "inputs": ["USER_QUERY"], "metadata": {}},
                ],
            },
            "formatter": {"final_answer": "ok"},
        }
        _, fake = _run("hi", script, session_id="t-mem-5")
        formatter_prompts = [c[1] for c in fake.calls if c[0] == "formatter"]
        assert formatter_prompts, "formatter was never called"
        combined = "\n".join(formatter_prompts)
        assert "ACTIVE POLICIES" in combined
        assert "never email passwords" in combined
        # ...while memory hits stay excluded from the formatter.
        assert "MEMORY HITS" not in combined
