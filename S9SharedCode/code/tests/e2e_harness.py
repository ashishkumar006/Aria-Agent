"""Shared harness for the E2E production-readiness test plan (S9 agent).

Two layers of helper live here:

1. **Deterministic L0 driver** - replaces ``skills.run_skill`` (and the copy
   ``flow.run_skill`` bound at import time) with a scenario-driven fake. This
   gives us a fully mocked orchestrator run: no LLM, no gateway, no Playwright,
   no desktop daemon. Each test sets ``SCENARIO[skill_name]`` to control what the
   fake returns; every node the orchestrator executes is recorded in
   ``CALL_LOG`` (skill name + node id + output) so assertions can check
   node counts, order, and per-skill outputs.

2. **Live HTTP helpers** - small clients for ``/api/chat`` (SSE), ``/api/tts``,
   ``/api/cost``, ``/api/schedule`` used by the ``@live`` tests that drive the
   real running servers (agent :8500, gateway :8109).

The existing ``conftest.py`` already skips ``@pytest.mark.live`` tests unless
the suite is run with ``-m live``; this module adds a ``stress`` marker and the
fixtures above. Import directly from test modules, e.g.::

    from e2e_harness import deterministic, CALL_LOG, SCENARIO
"""
from __future__ import annotations

import asyncio
import sys
import time as _time
import urllib.request
import json
from pathlib import Path

# Make the project root importable (skills, flow, schemas, gateway, memory ...).
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from schemas import AgentResult, NodeSpec  # noqa: E402
import skills  # noqa: E402
import flow  # noqa: E402
import gateway  # noqa: E402

# -- deterministic L0 state -------------------------------------------------
CALL_LOG: list = []   # [{"skill", "node", "output"}, ...]
SCENARIO: dict = {}   # skill_name -> plan spec (or a list of specs, consumed
                      # in call order so a failed-then-recovered node can differ)
CALL_COUNTS: dict = {}  # skill_name -> how many times it has been invoked


def _planner(nodes, answer=None, rejected=None) -> AgentResult:
    successors = [
        NodeSpec(skill=n["skill"], inputs=n.get("inputs", []),
                 metadata=n.get("metadata", {}))
        for n in nodes
    ]
    if answer and not successors:
        # Match the REAL planner contract (prompts/planner.md): a direct
        # answer is emitted as {"rationale", "answer"} with NO nodes, and
        # flow.Executor short-circuits on output["answer"] when successors
        # are empty. The old shape used "final_answer" + a
        # skills.PLANNER_SHORTCIRCUIT flag that no longer exists.
        return AgentResult(
            success=True, agent_name="planner",
            output={"rationale": "sc", "answer": answer},
            successors=[], elapsed_s=0.0,
        )
    out = {"rationale": "plan", "nodes": nodes}
    if rejected:
        out["_rejected"] = rejected
    return AgentResult(
        success=not rejected, agent_name="planner", output=out,
        successors=successors, elapsed_s=0.0,
    )


async def fake_run_skill(skill, node_id, graph_nodes, session_id, query, fr,
                         memory_hits=None, **kwargs):
    name = skill.name
    CALL_LOG.append({"skill": name, "node": node_id, "output": None})
    raw = SCENARIO.get(name, {})
    # A list of specs is consumed one-per-invocation (call order), so a node
    # can fail on its first call and succeed on the recovery re-plan.
    if isinstance(raw, list):
        idx = min(CALL_COUNTS.get(name, 0), len(raw) - 1)
        CALL_COUNTS[name] = CALL_COUNTS.get(name, 0) + 1
        spec = raw[idx]
    else:
        spec = raw

    if name == "planner":
        return _planner(spec.get("nodes", []), spec.get("answer"),
                        spec.get("rejected")), ""

    if name == "browser":
        out = {"path": spec.get("path", "extract"),
               "text": spec.get("text", "browser output")}
        CALL_LOG[-1]["output"] = out
        return AgentResult(
            success=spec.get("success", True), agent_name="browser",
            output=out, successors=[], elapsed_s=0.0, provider="fake",
            error=spec.get("error")), ""

    if name == "computer":
        out = {"layer": spec.get("layer", "a11y"),
               "result": spec.get("text", "computer output"),
               "error": spec.get("error")}
        CALL_LOG[-1]["output"] = out
        return AgentResult(
            success=spec.get("success", True), agent_name="computer",
            output=out, successors=[], elapsed_s=0.0, provider="fake"), ""

    # Generic text skills: researcher, retriever, formatter, distiller,
    # summariser, sandbox_executor, vision_file, critic, etc.
    text = spec.get("text", "output of " + name)
    out = {"text": text}
    if name == "formatter":
        out["final_answer"] = text
    CALL_LOG[-1]["output"] = out
    return AgentResult(
        success=spec.get("success", True), agent_name=name, output=out,
        successors=[], elapsed_s=0.0, provider="fake"), ""


@pytest.fixture
def deterministic():
    """Install the fake ``run_skill`` for a single L0 test.

    Yields ``SCENARIO`` so the test can declare per-skill behaviour, e.g.::

        deterministic["planner"] = {"nodes": [{"skill": "formatter",
                                               "inputs": ["USER_QUERY"]}]}
        deterministic["formatter"] = {"text": "the answer"}
    """
    import memory as _mem
    import persistence as _persist
    import os as _os
    CALL_LOG.clear()
    SCENARIO.clear()
    CALL_COUNTS.clear()
    orig_sk = skills.run_skill
    orig_fl = flow.run_skill
    orig_ensure = gateway.ensure_gateway
    orig_flow_ensure = flow.ensure_gateway
    orig_mem_ensure = _mem.ensure_gateway
    orig_mem_read = _mem.read
    orig_safe = flow._safe_remember
    orig_purge = flow._safe_purge_working
    orig_atomic = _persist._atomic_write

    def _atomic_write_retry(path, data):
        # Windows (Defender/Indexing) occasionally holds a brief lock on a
        # just-written session file; retry the rename so a transient
        # PermissionError doesn't fail an otherwise-correct orchestration run.
        for _attempt in range(8):
            try:
                return orig_atomic(path, data)
            except PermissionError:
                if _attempt == 7:
                    raise
                _time.sleep(0.25)

    _mem.read = lambda q, top_k=5, **kw: []          # no memory lookups
    flow._safe_remember = lambda q, s: None     # no background memory writes
    flow._safe_purge_working = lambda s, t: None  # no working purges
    gateway.ensure_gateway = lambda *a, **k: None
    flow.ensure_gateway = lambda *a, **k: None  # flow bound its own ref at import
    _mem.ensure_gateway = lambda *a, **k: None
    _persist._atomic_write = _atomic_write_retry
    skills.run_skill = fake_run_skill
    flow.run_skill = fake_run_skill

    yield SCENARIO

    skills.run_skill = orig_sk
    flow.run_skill = orig_fl
    gateway.ensure_gateway = orig_ensure
    flow.ensure_gateway = orig_flow_ensure
    _mem.ensure_gateway = orig_mem_ensure
    _mem.read = orig_mem_read
    flow._safe_remember = orig_safe
    flow._safe_purge_working = orig_purge
    _persist._atomic_write = orig_atomic


def run_executor(query, session_id):
    """Drive the real orchestrator in-process with the fake run_skill."""
    return asyncio.run(flow.Executor().run(query, session_id=session_id))


def skills_called():
    return [c["skill"] for c in CALL_LOG]


def last_output(skill_name):
    for rec in reversed(CALL_LOG):
        if rec["skill"] == skill_name:
            return rec["output"]
    return None


# -- live HTTP helpers ------------------------------------------------------
def chat_sse(base, query, conversation_id=None, timeout=240):
    """POST /api/chat and parse the SSE stream.

    Returns ``(answer, frames, meta)`` where ``frames`` is the list of decoded
    JSON objects (one per ``data:`` line) and ``meta`` is the final ``meta``
    frame (or None).
    """
    payload = {"query": query}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        base + "/api/chat", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    answer, frames, meta = "", [], None
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                d = json.loads(line[len("data:"):].strip())
            except json.JSONDecodeError:
                continue
            frames.append(d)
            if d.get("type") == "meta":
                meta = d
            elif d.get("type") == "done":
                answer = d.get("answer", "")
            elif d.get("type") == "error":
                answer = d.get("error", answer)
    return answer, frames, meta


def tts(base, text, voice="af_heart"):
    payload = {"text": text, "voice": voice}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        base + "/api/tts", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def health(base):
    with urllib.request.urlopen(base + "/api/health", timeout=10) as resp:
        return json.loads(resp.read().decode())
