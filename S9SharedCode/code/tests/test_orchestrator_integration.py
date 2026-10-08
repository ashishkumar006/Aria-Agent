"""P1.1 — Orchestrator integration tests (mocked LLM).

Drives the REAL growing-graph orchestrator (flow.Executor) end-to-end with a
*stubbed* gateway so the tests are deterministic, fast, and need no
credentials. We patch ``skills.LLM`` so every text skill's ``LLM().chat()``
returns a canned JSON object keyed by the skill name, and we let the real
``sandbox_executor`` run (it shells out to python — no network).

These guard the core "is this an agent" behaviors:
  - minimal planner→formatter path
  - fan-out (parallel workers)
  - recovery on a transient node failure (re-run once, then skip)
  - malformed planner JSON fails loudly, run survives
  - node cap (MAX_NODES=60) stops a planner loop cleanly
  - planner short-circuit (direct answer, no formatter)
  - critic auto-insertion on a critic:true skill

Run:  pytest tests/test_orchestrator_integration.py -q
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import flow
from schemas import AgentResult


# ── mock LLM ─────────────────────────────────────────────────────────────────
# A scripted LLM that returns a canned JSON reply per skill. The orchestrator
# reads `output.final_answer` (formatter/planner short-circuit) and
# `successors` (planner) out of the parsed JSON.

def _planner_reply(nodes: list[dict], answer: str | None = None) -> dict:
    """Build a planner AgentResult payload. `nodes` is a list of NodeSpec
    dicts; `answer` (when given) triggers the short-circuit path."""
    if answer is not None:
        return {"answer": answer}
    return {"nodes": nodes}


class _FakeLLM:
    """Returns scripted JSON based on the `agent=` kwarg the dispatcher passes
    (which equals the skill name). Falls back to a generic formatter answer."""

    def __init__(self, script: dict):
        self._script = script
        self.calls: list[tuple[str, str]] = []  # (skill, prompt_snippet)

    def chat(self, prompt=None, *, messages=None, system=None, agent=None,
             session=None, **kw):
        skill = agent or "formatter"
        self.calls.append((skill, (prompt or "")[:60]))
        payload = self._script.get(skill, {"final_answer": f"answer from {skill}"})
        return {"text": json.dumps(payload), "provider": "fake",
                "input_tokens": 10, "output_tokens": 10}

    def vision(self, *a, **kw):
        return {"text": json.dumps({"content": "vision ok"}), "provider": "fake"}


def _run(query: str, script: dict, *, session_id: str | None = None,
         llm_cls=None) -> tuple[str, _FakeLLM]:
    """Patch skills.LLM with `script` and run the orchestrator to completion.
    Returns (final_answer, fake_llm) so tests can inspect call counts.

    Tool-channel skills (researcher/browser/action/...) bypass skills.LLM
    entirely — they go through mcp_runner.run_with_tools. Patch that too,
    routing through the same scripted LLM so one script drives both paths."""
    import mcp_runner
    fake = (llm_cls or _FakeLLM)(script)

    async def _fake_run_with_tools(*, prompt=None, tools_payload=None,
                                   agent=None, session_id=None, **kw):
        reply = fake.chat(prompt=prompt, agent=agent, session=session_id)
        return {**reply, "tool_calls": []}

    with mock.patch.object(flow, "ensure_gateway", lambda: None), \
         mock.patch("skills.LLM", lambda: fake), \
         mock.patch.object(mcp_runner, "run_with_tools", _fake_run_with_tools):
        answer = asyncio.run(
            flow.Executor().run(query, session_id=session_id or "t-orch-1")
        )
    return answer, fake


# ── 1. minimal path ──────────────────────────────────────────────────────────

def test_minimal_planner_formatter():
    script = {
        "planner": _planner_reply([
            {"skill": "formatter", "inputs": ["USER_QUERY"], "metadata": {}},
        ]),
        "formatter": {"final_answer": "Hello! Nice to meet you."},
    }
    answer, _ = _run("hi", script)
    assert answer.strip() == "Hello! Nice to meet you."


# ── 2. fan-out (parallel workers) ─────────────────────────────────────────────

def test_fanout_three_researchers():
    # Planner emits 3 researcher siblings, each scoped by metadata.question,
    # then a single formatter that consumes all three.
    script = {
        "planner": _planner_reply([
            {"skill": "researcher", "inputs": [], "metadata": {"label": "r1", "question": "population of London"}},
            {"skill": "researcher", "inputs": [], "metadata": {"label": "r2", "question": "population of Paris"}},
            {"skill": "researcher", "inputs": [], "metadata": {"label": "r3", "question": "population of Berlin"}},
            {"skill": "formatter", "inputs": ["n:r1", "n:r2", "n:r3"], "metadata": {}},
        ]),
        "researcher": {"final_answer": "research result"},
        "formatter": {"final_answer": "London, Paris and Berlin compared."},
    }
    answer, fake = _run("Compare populations of London, Paris, Berlin", script)
    assert "compared" in answer
    # Exactly 3 researcher calls.
    researchers = [c for c in fake.calls if c[0] == "researcher"]
    assert len(researchers) == 3
    # The formatter is `sectioned`: with 3 upstream results it makes one
    # focused call per section plus a short lead (skills.
    # _sectioned_final_answer), so a multi-source run produces a
    # multi-section report instead of the ~300 words a single call
    # yields from these models. 3 sections + 1 lead = 4 calls, all the
    # formatter. (The fake records only the first 60 prompt chars, so
    # the count is what can be asserted here.)
    formatters = [c for c in fake.calls if c[0] == "formatter"]
    assert len(formatters) == 4, [c[0] for c in fake.calls]


# ── 3. transient failure is classified and skipped ──────────────────────────

def test_recovery_retries_then_skips_transient():
    """A researcher that fails with a transient 503 is classified by
    recovery.py as `transient` and SKIPPED immediately (the gateway owns
    retries; the orchestrator must not re-plan on top of a 503). The node
    is marked `skipped` so downstream children still run, and the run
    completes with a formatter answer rather than crashing."""
    state = {"attempts": 0}

    class _FlakyLLM(_FakeLLM):
        def chat(self, prompt=None, *, messages=None, system=None, agent=None,
                 session=None, **kw):
            skill = agent or "formatter"
            if skill == "researcher":
                state["attempts"] += 1
                raise RuntimeError(
                    "exception: HTTPStatusError: Server error "
                    "'503 Service Unavailable'")
            return super().chat(prompt, messages=messages, system=system,
                                agent=agent, session=session, **kw)

    script = {
        "planner": _planner_reply([
            {"skill": "researcher", "inputs": [], "metadata": {"label": "r1", "question": "x"}},
            {"skill": "formatter", "inputs": ["n:r1"], "metadata": {}},
        ]),
        # `researcher` is never asked again after the transient skip.
        "formatter": {"final_answer": "Final after skip."},
    }
    answer, _ = _run("do research", script, session_id="t-rec-1", llm_cls=_FlakyLLM)
    assert "Final after skip" in answer
    # Exactly one researcher attempt (transient → skip; no retry, no replan).
    assert state["attempts"] == 1


# ── 4. malformed planner JSON fails loudly, run survives ──────────────────────

def test_malformed_planner_json_does_not_crash():
    """When the planner emits a NodeSpec missing required fields, the node
    fails with a clear error and the orchestrator does not raise — it
    completes (here, with a fallback answer)."""
    script = {
        # `skill` is required; this dict omits it → ValidationError → node fails.
        "planner": {"nodes": [{"inputs": [], "metadata": {}}]},
        "formatter": {"final_answer": "fallback answer"},
    }
    # The planner itself fails (validation_error → skip). The run must still
    # terminate and return *something* (the executor falls back to the last
    # complete node's output, or empty string — never an exception).
    answer, _ = _run("broken plan", script)
    assert isinstance(answer, str)  # no exception escaped


# ── 5. node cap (MAX_NODES=60) ────────────────────────────────────────────────

def test_fanout_is_bounded_and_still_terminates():
    """A planner that emits more nodes than MAX_FANOUT must not open them all
    at once, and the run must still finish.

    Deferring a node is only safe while it stays PENDING. Marking the whole
    ready set as running and dispatching a capped batch left the deferred
    nodes stuck in `running` with nothing executing them, so has_running()
    never cleared and the run looped forever.
    """
    n_nodes = flow.MAX_FANOUT * 2 + 3

    def _wide_planner(count):
        return _planner_reply([
            {"skill": "researcher", "inputs": [],
             "metadata": {"label": f"wide{i}", "question": f"q{i}"}}
            for i in range(count)
        ] + [{"skill": "formatter",
              "inputs": [f"n:wide{i}" for i in range(count)],
              "metadata": {}}])

    script = {
        "planner": _wide_planner(n_nodes),
        "researcher": {"final_answer": "r"},
        "formatter": {"final_answer": "wide done"},
    }
    answer, fake = _run("wide fanout", session_id="t-fanout-cap",
                        script=script)
    assert "wide done" in answer
    # Every researcher eventually ran, across multiple batches.
    researchers = [c for c in fake.calls if c[0] == "researcher"]
    assert len(researchers) == n_nodes, f"got {len(researchers)}"
    # And nothing was left non-terminal on disk.
    from persistence import SessionStore
    g = SessionStore("t-fanout-cap").read_graph()
    stuck = [(n, d.get("status")) for n, d in g.nodes(data=True)
             if d.get("status") in ("pending", "running")]
    assert not stuck, f"nodes left non-terminal: {stuck}"


def test_max_fanout_constant_is_sane():
    assert 1 <= flow.MAX_FANOUT <= 8, \
        "a very wide fan-out re-creates the rate-limit problem it bounds"
    assert flow.MAX_FANOUT < flow.MAX_NODES


def test_node_cap_stops_planner_loop():
    """A planner that keeps adding nodes must stop at MAX_NODES=60 and the
    run must terminate (no infinite loop / no hang)."""
    state = {"n": 0}

    class _LoopingLLM(_FakeLLM):
        def chat(self, prompt=None, *, messages=None, system=None, agent=None,
                 session=None, **kw):
            skill = agent or "formatter"
            if skill == "planner":
                # keep adding 5 workers every time the planner runs
                nodes = [
                    {"skill": "researcher", "inputs": [],
                     "metadata": {"label": f"w{state['n']+i}", "question": "q"}}
                    for i in range(5)
                ]
                state["n"] += 5
                return {"text": json.dumps({"nodes": nodes}), "provider": "fake"}
            return super().chat(prompt, messages=messages, system=system,
                                agent=agent, session=session, **kw)

    script = {
        "researcher": {"final_answer": "w"},
        "formatter": {"final_answer": "done"},
    }
    answer, _ = _run("loop", session_id="t-cap-1", script=script, llm_cls=_LoopingLLM)
    assert answer is not None
    # Every node must reach a terminal state. A node left `running` or
    # `pending` on disk makes the run read as never-finished forever, and the
    # startup reconciler only rescues nodes that look `running`.
    from persistence import SessionStore
    g = SessionStore("t-cap-1").read_graph()
    stuck = [(n, d.get("status")) for n, d in g.nodes(data=True)
             if d.get("status") in ("pending", "running")]
    assert not stuck, f"nodes left non-terminal: {stuck}"
    # The orchestrator must have stopped adding nodes; total nodes bounded by
    # MAX_NODES + a small margin for the planner/formatter control nodes.
    assert state["n"] <= flow.MAX_NODES + 10


# ── 6. planner short-circuit ──────────────────────────────────────────────────

def test_planner_short_circuit_single_call():
    """When the planner emits a direct `answer` with no successors, the
    executor returns it immediately (one LLM call, no formatter)."""
    script = {
        "planner": _planner_reply(None, answer="The capital of France is Paris."),
    }
    answer, fake = _run("capital of France?", script)
    assert answer.strip() == "The capital of France is Paris."
    # Only the planner was called — no formatter.
    planner_calls = [c for c in fake.calls if c[0] == "planner"]
    formatter_calls = [c for c in fake.calls if c[0] == "formatter"]
    assert len(planner_calls) == 1
    assert len(formatter_calls) == 0


# ── 7. critic auto-insertion ──────────────────────────────────────────────────

def test_critic_auto_inserted_for_critic_skill():
    """A `critic:true` skill (distiller) gets a Critic node gating its outgoing
    edge to the formatter. We assert the run still completes and that a
    critic node was executed (its `verdict: pass` lets the child run)."""
    script = {
        "planner": _planner_reply([
            {"skill": "distiller", "inputs": ["USER_QUERY"], "metadata": {}},
            {"skill": "formatter", "inputs": ["n:distiller"], "metadata": {}},
        ]),
        "distiller": {"final_answer": "extracted fields"},
        # Critic must return verdict:pass so the formatter child runs.
        "critic": {"verdict": "pass", "rationale": "looks good"},
        "formatter": {"final_answer": "Final formatted answer."},
    }
    answer, fake = _run("extract something", script)
    assert "Final formatted answer" in answer
    critic_calls = [c for c in fake.calls if c[0] == "critic"]
    assert len(critic_calls) >= 1
