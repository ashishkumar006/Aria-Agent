"""Recovery continuity: absorb, coalesce, splice.

Three defects lived in the failure path, all seen live on one authoring run
(the EV PDF that came back twice):

  1. `prior_complete` was computed while the batch was still being marked, so
     a sibling that completed in the same wave was invisible to the recovery
     planner, which then re-planned work that was already done.
  2. Each failed node queued its OWN recovery planner; two dead researchers
     produced two planners, each re-emitting the full remaining pipeline.
  3. A recovery plan was spliced BESIDE the original chain. Nothing rewired
     the original consumers, so they waited forever on a `failed` node while
     the plan's own author/formatter ran — one request, two PDFs, and a
     component of pending nodes left on disk forever.

The fixes under test:
  - PASS 1 marks every outcome before PASS 2 acts on any of them;
  - a failed fan-out sibling whose consumers still hold majority sibling
    evidence is ABSORBED (skipped), not re-planned;
  - one recovery planner per failed batch (coalesced failure report);
  - a recovery plan is SPLICED: duplicate downstream roles are pruned, the
    waiting consumers are rewired to the plan's terminals, the failed node is
    marked skipped so the original chain resumes exactly once;
  - leftover pending nodes are swept to `skipped` at loop exit.

Driven through the REAL Executor with a scripted fake LLM (same harness shape
as tests/test_orchestrator_integration.py), so graph mechanics are exercised,
not just helpers.

Run:  pytest tests/test_recovery_splice.py -q
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import flow
from flow import Graph, _consumers_have_other_evidence
from persistence import SessionStore
from schemas import AgentResult


class _ScriptedLLM:
    """Canned JSON per skill, queue-capped: the last queued reply repeats.

    `fail_calls` names (skill, call-index) pairs to raise on — the index is
    per-skill, so "the first researcher dies, the recovery researcher
    succeeds" is expressible. `full_calls` keeps complete prompts for
    assertions on what the recovery planner was told."""

    def __init__(self, scripts: dict | None = None, fail_calls=()):
        def _queue(v):
            return list(v) if isinstance(v, list) else [v]
        self._queues = {k: _queue(v) for k, v in (scripts or {}).items()}
        self._fail_calls = set(fail_calls)
        self._n: dict[str, int] = {}
        self.full_calls: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str]] = []

    def chat(self, prompt=None, *, messages=None, system=None, agent=None,
             session=None, **kw):
        skill = agent or "formatter"
        i = self._n.get(skill, 0)
        self._n[skill] = i + 1
        self.full_calls.append((skill, prompt or ""))
        self.calls.append((skill, (prompt or "")[:60]))
        if (skill, i) in self._fail_calls:
            raise RuntimeError(
                "researcher returned no usable JSON: empty reply")
        q = self._queues.get(skill)
        if q:
            payload = q.pop(0) if len(q) > 1 else q[0]
        else:
            payload = {"final_answer": f"answer from {skill}"}
        return {"text": json.dumps(payload), "provider": "fake",
                "input_tokens": 10, "output_tokens": 10}

    def vision(self, *a, **kw):
        return {"text": json.dumps({"content": "vision ok"}), "provider": "fake"}


def _run(query: str, fake: _ScriptedLLM, *, session_id: str) -> str:
    import mcp_runner

    async def _fake_run_with_tools(*, prompt=None, tools_payload=None,
                                   agent=None, session_id=None, **kw):
        reply = fake.chat(prompt=prompt, agent=agent, session=session_id)
        return {**reply, "tool_calls": []}

    with mock.patch.object(flow, "ensure_gateway", lambda: None), \
         mock.patch("skills.LLM", lambda: fake), \
         mock.patch.object(mcp_runner, "run_with_tools", _fake_run_with_tools):
        return asyncio.run(
            flow.Executor().run(query, session_id=session_id)
        )


def _graph(session_id: str):
    g = SessionStore(session_id).read_graph()
    assert g is not None, f"no graph persisted for {session_id}"
    return g


def _by_skill(g, skill: str) -> list[str]:
    return [n for n, d in g.nodes(data=True) if d.get("skill") == skill]


def _calls(fake: _ScriptedLLM, skill: str) -> list[str]:
    return [p for s, p in fake.full_calls if s == skill]


# ── helper-level checks ──────────────────────────────────────────────────────

def test_absorb_requires_majority_surviving_evidence():
    g = Graph()
    r_fail = g.add_node("researcher", inputs=[])
    g.mark(r_fail, "failed")
    r_ok1 = g.add_node("researcher", inputs=[])
    g.mark(r_ok1, "complete")
    r_ok2 = g.add_node("researcher", inputs=[])
    g.mark(r_ok2, "complete")
    author = g.add_node("author", inputs=[r_fail, r_ok1, r_ok2])
    # 2 of 3 siblings survived: absorbed.
    assert _consumers_have_other_evidence(g, r_fail) is True


def test_absorb_refuses_when_survivors_are_a_minority():
    g = Graph()
    r_fail1 = g.add_node("researcher", inputs=[])
    g.mark(r_fail1, "failed")
    r_fail2 = g.add_node("researcher", inputs=[])
    g.mark(r_fail2, "failed")
    r_ok = g.add_node("researcher", inputs=[])
    g.mark(r_ok, "complete")
    g.add_node("author", inputs=[r_fail1, r_fail2, r_ok])
    # 1 of 3 survived: replan instead of delivering a thinned document.
    assert _consumers_have_other_evidence(g, r_fail1) is False


def test_absorb_refuses_for_a_sole_dependency():
    g = Graph()
    r_fail = g.add_node("researcher", inputs=[])
    g.mark(r_fail, "failed")
    g.add_node("formatter", inputs=[r_fail])
    assert _consumers_have_other_evidence(g, r_fail) is False


# ── end-to-end through the Executor ──────────────────────────────────────────

def test_same_batch_completions_are_visible_and_survivable_failures_absorb():
    """3 researchers, first fails. Its two same-batch siblings completed, so
    the failure is absorbed: no recovery planner, and the single terminal
    node still runs — the graph shows the dead node skipped, not failed."""
    script = {
        "planner": {"nodes": [
            {"skill": "researcher", "inputs": [],
             "metadata": {"label": "a", "question": "facet A"}},
            {"skill": "researcher", "inputs": [],
             "metadata": {"label": "b", "question": "facet B"}},
            {"skill": "researcher", "inputs": [],
             "metadata": {"label": "c", "question": "facet C"}},
            {"skill": "formatter", "inputs": ["n:a", "n:b", "n:c"],
             "metadata": {}},
        ]},
        "researcher": {"question": "q", "findings": "evidence"},
        "formatter": {"final_answer": "Report complete."},
    }
    fake = _ScriptedLLM(script, fail_calls={("researcher", 0)})
    answer = _run("make a report", fake, session_id="t-splice-absorb-1")

    assert "Report complete." in answer
    assert len(_calls(fake, "planner")) == 1, "no recovery planner expected"
    assert len(_calls(fake, "researcher")) == 3, \
        "the surviving siblings must not be re-run"
    g = _graph("t-splice-absorb-1")
    assert len(_by_skill(g, "formatter")) == 1
    dead = [n for n, d in g.nodes(data=True)
            if d.get("skill") == "researcher" and d["status"] == "skipped"]
    assert len(dead) == 1, "failed sibling must be absorbed (skipped)"
    assert "absorbed" in (g.nodes[dead[0]].get("error") or "")


def test_single_dependency_failure_replans_and_splices_with_prune():
    """researcher -> formatter, the researcher dies. One recovery planner
    replaces it; the plan ALSO re-emits a formatter, which must be pruned so
    the ORIGINAL formatter renders exactly one answer."""
    script = {
        "planner": [
            {"nodes": [
                {"skill": "researcher", "inputs": [],
                 "metadata": {"label": "r", "question": "Q"}},
                {"skill": "formatter", "inputs": ["n:r"], "metadata": {}},
            ]},
            {"nodes": [
                {"skill": "researcher", "inputs": ["USER_QUERY"],
                 "metadata": {"label": "r2", "question": "Q"}},
                {"skill": "formatter", "inputs": ["n:r2"], "metadata": {}},
            ]},
        ],
        "researcher": {"question": "Q", "findings": "evidence"},
        "formatter": {"final_answer": "Recovered answer."},
    }
    fake = _ScriptedLLM(script, fail_calls={("researcher", 0)})
    answer = _run("research this", fake, session_id="t-splice-single-1")

    assert "Recovered answer." in answer
    assert len(_calls(fake, "planner")) == 2, "one replan"
    assert len(_calls(fake, "researcher")) == 2
    # The recovery planner was told what waits downstream and what died.
    rec_prompt = _calls(fake, "planner")[1]
    assert "existing nodes already wait on this output" in rec_prompt
    assert "its brief was: Q" in rec_prompt
    g = _graph("t-splice-single-1")
    assert len(_by_skill(g, "formatter")) == 1, \
        "the plan's duplicate formatter must be pruned, not run"
    replaced = [n for n in _by_skill(g, "researcher")
                if g.nodes[n]["status"] == "skipped"]
    assert len(replaced) == 1
    assert "replaced by recovery plan" in (g.nodes[replaced[0]].get("error") or "")
    assert any(g.nodes[n]["status"] == "complete"
               for n in _by_skill(g, "researcher")), \
        "the replacement researcher must have run"


def test_two_failed_siblings_get_one_recovery_planner():
    """3 researchers, two die, survivors are a minority: ONE recovery planner
    covers both and the single terminal node runs once on re-wired
    evidence — no duplicate second half of the pipeline."""
    script = {
        "planner": [
            {"nodes": [
                {"skill": "researcher", "inputs": [],
                 "metadata": {"label": "a", "question": "facet A"}},
                {"skill": "researcher", "inputs": [],
                 "metadata": {"label": "b", "question": "facet B"}},
                {"skill": "researcher", "inputs": [],
                 "metadata": {"label": "c", "question": "facet C"}},
                {"skill": "formatter",
                 "inputs": ["n:a", "n:b", "n:c"], "metadata": {}},
            ]},
            {"nodes": [
                {"skill": "researcher", "inputs": ["USER_QUERY"],
                 "metadata": {"label": "fix", "question": "missing facets"}},
                {"skill": "formatter", "inputs": ["n:fix"],
                 "metadata": {}},
            ]},
        ],
        "researcher": {"question": "q", "findings": "evidence"},
        "formatter": {"final_answer": "One report."},
    }
    fake = _ScriptedLLM(script, fail_calls={("researcher", 0),
                                            ("researcher", 1)})
    answer = _run("make a report", fake, session_id="t-splice-coalesce-1")

    assert "One report." in answer
    planners = _calls(fake, "planner")
    assert len(planners) == 2, (
        "two failed siblings must coalesce into ONE recovery planner")
    assert "node=n:2" in planners[1] and "node=n:3" in planners[1]
    assert len(_calls(fake, "researcher")) == 4
    g = _graph("t-splice-coalesce-1")
    assert len(_by_skill(g, "formatter")) == 1, \
        "the plan's duplicate formatter must be pruned, not run"
    failed = [n for n, d in g.nodes(data=True)
              if d.get("skill") == "researcher" and d["status"] == "skipped"]
    assert len(failed) == 2
    # The formatter was rewired: no dead reference may remain in its inputs.
    fmt = _by_skill(g, "formatter")[0]
    ins = list(g.nodes[fmt]["inputs"])
    assert not any(g.nodes[i]["status"] in ("skipped", "failed")
                   for i in ins if i in g.nodes)


def test_empty_recovery_plan_releases_the_consumers():
    """The recovery planner emits nothing. The failed node is skipped so the
    formatter still runs on the honest upstream-missing marker."""
    script = {
        "planner": [
            {"nodes": [
                {"skill": "researcher", "inputs": [],
                 "metadata": {"label": "r", "question": "Q"}},
                {"skill": "formatter", "inputs": ["n:r"], "metadata": {}},
            ]},
            {"nodes": []},
        ],
        "researcher": {"question": "Q", "findings": "evidence"},
        "formatter": {"final_answer": "Answer despite gap."},
    }
    fake = _ScriptedLLM(script, fail_calls={("researcher", 0)})
    answer = _run("research this", fake, session_id="t-splice-empty-1")

    assert "Answer despite gap." in answer
    g = _graph("t-splice-empty-1")
    dead = _by_skill(g, "researcher")
    assert dead and g.nodes[dead[0]]["status"] == "skipped"


def test_orphaned_pending_nodes_are_swept_at_exit():
    """The researcher dies AND its recovery planner dies. The formatter can
    never become ready; at loop exit it must be marked skipped, not left
    pending forever on disk."""
    script = {
        "planner": [
            {"nodes": [
                {"skill": "researcher", "inputs": [],
                 "metadata": {"label": "r", "question": "Q"}},
                {"skill": "formatter", "inputs": ["n:r"], "metadata": {}},
            ]},
        ],
        "researcher": {"question": "Q", "findings": "evidence"},
        "formatter": {"final_answer": "never reached"},
    }
    fake = _ScriptedLLM(script, fail_calls={("researcher", 0),
                                            ("planner", 1)})
    answer = _run("research this", fake, session_id="t-splice-orphan-1")

    assert isinstance(answer, str) and answer
    assert len(_calls(fake, "formatter")) == 0
    g = _graph("t-splice-orphan-1")
    statuses = {d.get("skill"): d["status"] for _, d in g.nodes(data=True)}
    assert "pending" not in statuses.values(), \
        f"no node may persist as pending: {statuses}"
    fmt = _by_skill(g, "formatter")
    assert fmt and g.nodes[fmt[0]]["status"] == "skipped"
    assert "orphaned" in (g.nodes[fmt[0]].get("error") or "")
