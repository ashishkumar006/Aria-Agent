"""Growing-graph orchestrator (Session 8 origin, live in Session 9).

The agent's loop becomes a NetworkX DiGraph. Each node is a skill; edges
carry typed AgentResult payloads. The graph GROWS at runtime via five
actors: the Planner's seed plan, dynamic successors from any skill,
static `internal_successors` from the yaml, Critic auto-insertion on
edges out of `critic:true` skills, and Planner re-invocation on node
failure (gated by `recovery.plan_recovery`). Perception's tool-blindness
contract from S7 is preserved — Planner names skills, never tools.

Persistence lives in persistence.py; skill execution in skills.py;
failure-policy in recovery.py; sandbox in sandbox.py.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

import networkx as nx

import memory as memory_svc
import turnlog
from gateway import ensure_gateway
from persistence import SessionStore
from skills import ROOT as _SROOT
from recovery import RecoveryDecision, handle_critic_verdict, plan_recovery
from schemas import AgentResult, NodeState
from skills import SkillRegistry, run_skill

# There is no cap on how many researcher nodes a plan may contain, and no cap
# on how many facets the planner may list. The planner is told it decides, that
# nothing it emits will be dropped, and what each node type is for; the
# executor honours it. Two earlier constants were both local guesses about
# "enough" that silently truncated plans the model had already reasoned about:
# MAX_FANOUT=4 threw away a 6-facet plan, and the 12 that replaced it threw
# away a 30-facet one. Measured cost of the 4: 4 facets produced 17,782 chars
# of findings and a 2,367-word document, 3 produced 13,421 and a 310-word stub.
# The evidence base is the document's ceiling, so it is the planner's call.
#
# What remains are limits on MECHANICS, not on plan quality, and neither
# discards work:
#   MAX_NODES      - a runaway planner/recovery LOOP cannot run forever. It
#                    stops scheduling, not the plan's quality.
#   MAX_CONCURRENCY - how many calls run in the same wave. gemini is tpm
#                    250000 (router.py); unbounded waves 429 together and every
#                    sibling fails. Deferred nodes stay `pending` and run on the
#                    next wave, so a 60-facet plan still runs all 60.
MAX_NODES = 400
MAX_CONCURRENCY = 8

# ── soft time budget ───────────────────────────────────────────────────────
# Every node already stamps started_at/completed_at into the graph rollup, and
# the Timeline in the UI reads them — but nothing ever CONSUMED the timing, so a
# 36s researcher that was going to be cut off at 30s still ran to 60s and the
# only feedback was a post-mortem bar chart.
#
# A budget does not cancel anything (asyncio cannot interrupt an in-flight LLM
# call safely, and a half-killed tool could leave side effects). It tells the
# NEXT invocation for the same skill how much of its allowance it has already
# spent, so the model synthesises from what it has instead of starting a fourth
# fetch. That is the cheap, safe half of the problem; the expensive half
# (hard cancellation) is deliberately not attempted here.
NODE_BUDGET_S: dict[str, float] = {
    "researcher": 150.0,  # parallel fetches plus a multi-page write-up
    "browser": 120.0,     # a real page interaction is legitimately slow
    "action": 60.0,       # one round trip to a third-party API
    "planner": 30.0,      # it emits a small JSON graph, nothing more
    # A long-form answer is a long generation: ~12k tokens of prose takes
    # well over a minute and a half, and this note TELLS the model to
    # stop searching and synthesise. At 45s the Formatter crossed its
    # warn ratio on nearly every research run, so the note was actively
    # pressuring the one node whose whole job is a thorough report.
    "formatter": 240.0,
    "default": 90.0,
}
BUDGET_WARN_RATIO = 0.7


# BaseExceptionGroup is Python 3.11+. On older interpreters the tuple matches
# nothing, so isinstance(x, ()) is always False and _readable_exc degrades to
# the plain formatting below.
try:  # pragma: no cover - version guard
    _BaseEG: tuple = (BaseExceptionGroup,)  # type: ignore[name-defined]
except NameError:  # pragma: no cover
    _BaseEG = ()


def _readable_exc(exc: BaseException) -> str:
    """A one-line error that names the REAL cause, not a TaskGroup wrapper.

    A skill that runs tools inside an anyio/asyncio task group (the MCP stdio
    session) surfaces a failure as
    `ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)` — a
    string that names neither what broke nor where. Unwrap the group(s) to the
    first leaf that is not a bare cancellation and report that instead, so the
    node's error and the run's degraded-detail say something actionable.
    """
    cur: BaseException = exc
    for _ in range(8):
        if not isinstance(cur, _BaseEG) or not cur.exceptions:
            break
        nxt = next((x for x in cur.exceptions
                    if not isinstance(x, asyncio.CancelledError)),
                   cur.exceptions[0])
        cur = nxt
    return f"{type(cur).__name__}: {cur}"


def _budget_note(skill_name: str, spent_by_skill: dict) -> str:
    """A one-line time-budget reminder for a node about to run.

    Only fires once the skill is past its warn ratio, so a fast run never sees
    it and the prompt is not polluted with noise on the common path.
    """
    budget = NODE_BUDGET_S.get(skill_name, NODE_BUDGET_S["default"])
    spent = float(spent_by_skill.get(skill_name, 0.0))
    if spent < budget * BUDGET_WARN_RATIO:
        return ""
    return (f"TIME BUDGET: earlier {skill_name} invocations in this run have "
            f"already used {spent:.0f}s of this skill's {budget:.0f}s "
            f"allowance. Cover what you still need in ONE more pass, then "
            f"synthesise. Do not start a broad new search.")


# ── Graph ────────────────────────────────────────────────────────────────────

class Graph:
    """NetworkX DiGraph wrapper. Nodes are str ids `n:<i>`; each node carries
    `skill`, `inputs` (list of str), and `status`."""

    def __init__(self):
        self.g = nx.DiGraph()
        self._counter = 0

    def add_node(self, skill: str, inputs: list[str], metadata: dict | None = None) -> str:
        self._counter += 1
        nid = f"n:{self._counter}"
        self.g.add_node(nid, skill=skill, inputs=list(inputs),
                        metadata=dict(metadata or {}), status="pending")
        for inp in inputs:
            if inp.startswith("n:") and inp in self.g.nodes:
                self.g.add_edge(inp, nid)
        return nid

    def mark(self, nid: str, status: str, error: str | None = None) -> None:
        """Set a node's status, stamping wall-clock timing into the graph
        attrs as a side effect. The graph.json rollup is what the UI polls
        every 2s, so timing belongs here — not only in the per-node state
        files, which are megabytes and never polled. `started_at` is set
        once (a re-mark to running must not restart the clock);
        `completed_at` is set on every terminal mark so a retry overwrites
        a stale end time rather than leaving one."""
        import time as _time
        node = self.g.nodes[nid]
        node["status"] = status
        if error is not None:
            # Record WHY a node ended without an answer, so the UI can say
            # "hit the node cap" instead of leaving a bare `skipped` tick.
            node["error"] = error
        if status == "running" and not node.get("started_at"):
            node["started_at"] = _time.time()
        if status in ("complete", "failed", "skipped"):
            node["completed_at"] = _time.time()

    def ready_nodes(self) -> list[str]:
        # A predecessor counts as "satisfied" when it is either complete or
        # skipped (the latter is how a Critic-fail removes a child from the
        # critical path without blocking unrelated branches downstream).
        out = []
        for nid, d in self.g.nodes(data=True):
            if d["status"] != "pending":
                continue
            preds = list(self.g.predecessors(nid))
            if all(self.g.nodes[p]["status"] in ("complete", "skipped") for p in preds):
                out.append(nid)
        return out

    def has_running(self) -> bool:
        return any(d["status"] == "running" for _, d in self.g.nodes(data=True))

    def extend_from(self, src_nid: str, result: AgentResult,
                    *, registry: SkillRegistry) -> list[str]:
        """Splice in dynamic successors, static internal_successors, and
        critic auto-insertion. Returns the list of new node ids.

        Resolves label-based input references (`n:<label>`) against the
        `metadata.label` of nodes added in the same batch. The Planner is
        encouraged to name its nodes by label so it can reference them
        without knowing the integer ids the orchestrator will hand out."""
        added: list[str] = []
        src_def = registry.get(self.g.nodes[src_nid]["skill"])

        # Pass 1: add the new nodes; build a label → assigned-id map.
        label_to_id: dict[str, str] = {}
        # Sibling POSITION → assigned id. The Planner is asked for node ids,
        # but it routinely emits zero-based positions instead ("inputs": ["0"]
        # meaning "the first node I listed"). Live: a researcher was wired
        # correctly but the formatter, told `["0"]`, fell through to the
        # fallback below and was wired to the PLANNER — so it received the
        # planner's node list as its INPUTS, reported "upstream findings were
        # not provided", and the user got a 272-char apology after a
        # successful 112s research node. Positions are unambiguous when in
        # range, so resolve them.
        idx_to_id: dict[str, str] = {}
        pending: list[tuple[str, list[str]]] = []
        for _idx, spec in enumerate(result.successors):
            label = (spec.metadata or {}).get("label")
            new_id = self.add_node(spec.skill, inputs=[],
                                   metadata=spec.metadata)
            added.append(new_id)
            idx_to_id[str(_idx)] = new_id
            # Map BOTH the explicit metadata.label AND the skill name to the
            # assigned id, so cross-skill references like "n:distiller" or
            # bare "distiller" resolve even when the Planner forgot to set
            # an explicit label. The first sibling of a given skill wins the
            # skill-name slot — collisions between siblings still require
            # explicit labels.
            if isinstance(label, str) and label:
                label_to_id[label] = new_id
            if spec.skill not in label_to_id:
                label_to_id[spec.skill] = new_id
            pending.append((new_id, list(spec.inputs)))

        # Pass 2: resolve inputs now that every sibling has an id. Translate
        # `n:<label>` to `n:<assigned-id>` if the label matches; pass numeric
        # `n:<i>` references through; pass anything else through unchanged.
        # NOTE: an empty `raw_inputs` is now a legitimate Planner signal for
        # a fan-out worker scoped via `metadata.question` (see planner.md).
        # We do NOT substitute the parent in that case — doing so would dump
        # the parent's full output (which for the Planner contains every
        # sibling's question) back into the worker's INPUTS block and undo
        # the scoping. The structural parent edge is preserved separately
        # below so the graph topology is still correct.
        for new_id, raw_inputs in pending:
            resolved: list[str] = []
            for inp in raw_inputs:
                # `n:<label>` or `n:<int>` form (preferred).
                if inp.startswith("n:"):
                    suffix = inp[2:]
                    if suffix in label_to_id:
                        resolved.append(label_to_id[suffix])
                        continue
                    if suffix.isdigit() and inp in self.g.nodes:
                        resolved.append(inp)
                        continue
                # Bare label form — the Planner sometimes drops the n: prefix.
                if inp in label_to_id:
                    resolved.append(label_to_id[inp])
                    continue
                # Special literal — the user query is always available.
                if inp == "USER_QUERY":
                    resolved.append(inp)
                    continue
                # Artifact handle — pass through, the input renderer handles it.
                if inp.startswith("art:"):
                    resolved.append(inp)
                    continue
                # Bare position of a sibling in THIS plan ("0", "1", ...).
                # Checked after the id forms so a real id always wins.
                if inp.isdigit() and inp in idx_to_id:
                    resolved.append(idx_to_id[inp])
                    continue
                # Unresolvable input — fall back to the parent so the child
                # has at least one upstream dependency to wait on. This still
                # leaks the parent's output into INPUTS, but only when the
                # Planner emitted a bad input name; it is not the fan-out
                # path. A future round may want to fail loudly here instead.
                print(f"[flow] {new_id}: unresolvable input {inp!r}; "
                      f"falling back to {src_nid}")
                resolved.append(src_nid)
            self.g.nodes[new_id]["inputs"] = resolved
            for inp in resolved:
                if inp.startswith("n:") and inp in self.g.nodes:
                    self.g.add_edge(inp, new_id)
            # Fan-out worker case: planner emitted inputs=[] on purpose. No
            # data dependency, but we still record the structural parent
            # edge so the executor's `ready_nodes` ordering and replay
            # topology stay coherent.
            if not raw_inputs:
                self.g.add_edge(src_nid, new_id)

        for child_skill in src_def.internal_successors:
            nid = self.add_node(child_skill, inputs=[src_nid])
            added.append(nid)

        # Critic auto-insertion: when a `critic: true` skill completes,
        # gate every outgoing edge (to a non-critic child) with a Critic
        # node. Covers BOTH newly-added dynamic successors AND pre-existing
        # edges from the initial Planner plan — earlier versions only saw
        # `added`, so a pre-planned distiller → formatter chain bypassed
        # the auto-critic entirely (the `critic: true` flag became a no-op
        # in the common pre-planned case). Reading the graph's actual
        # outgoing edges makes the flag load-bearing in both shapes.
        if src_def.critic:
            child_targets: list[str] = []
            for child_nid in list(self.g.successors(src_nid)):
                if self.g.nodes[child_nid].get("skill") == "critic":
                    continue  # already gated
                child_targets.append(child_nid)
            for child_nid in child_targets:
                self.g.remove_edge(src_nid, child_nid)
                # Critics need USER_QUERY: without it the critic falls back
                # to MEMORY HITS for context (and stale hits from prior
                # sessions can fool the critic into believing the user
                # asked a completely different question). With USER_QUERY
                # the critic evaluates against the real ask and not against
                # whatever happens to be top-of-FAISS.
                critic_nid = self.add_node(
                    "critic", inputs=["USER_QUERY", src_nid],
                    metadata={"target": src_nid, "child": child_nid},
                )
                self.g.add_edge(critic_nid, child_nid)
                added.append(critic_nid)

        return added


# The skills that gather evidence. An authoring node that reaches the renderer
# without one of these upstream is writing from the model's memory.
_RESEARCH_SKILLS = ("retriever", "researcher", "browser", "distiller",
                    "summariser")
_AUTHORING_SKILLS = ("author", "deck")

# Sessions whose research plan has already been stripped of authoring nodes.
# A second planner node (a recovery replan) must not re-run the rewrite.
_RESEARCH_TEXT_ONLY_SESSIONS: set[str] = set()


def ensure_research_is_text_only(graph: Graph, session_id: str = "") -> list[str]:
    """A Research run answers in the transcript; it must never render a file.

    Research and Authoring share one planner and one skill registry, so the
    planner can always reach for `author`. Observed live: a delegated research
    request ("...give me a proper report") planned
    `planner -> researchers -> author -> formatter`, the author rendered a PDF,
    and the answer pointed at a download instead of giving the report. Asking
    the planner not to is not enough - it is a model, and a document is the
    flattering answer to the word "report" - so the shape is enforced here:
    every author/deck node is removed, and the nodes downstream of it read the
    evidence directly. A terminal author (no formatter in the plan) becomes the
    formatter, which is the node that writes prose.

    Idempotent per session, like `ensure_authoring_research`.

    Returns the ids changed, for logging and tests.
    """
    if session_id:
        if session_id in _RESEARCH_TEXT_ONLY_SESSIONS:
            return []
        _RESEARCH_TEXT_ONLY_SESSIONS.add(session_id)
    changed: list[str] = []
    for nid in list(graph.g.nodes):
        if graph.g.nodes[nid]["skill"] not in _AUTHORING_SKILLS:
            continue
        preds = list(graph.g.nodes[nid].get("inputs") or [])
        succ = list(graph.g.successors(nid))
        if not succ:
            # Terminal author: keep the node and its inputs, but let the
            # formatter write the report instead of the renderer.
            graph.g.nodes[nid]["skill"] = "formatter"
            changed.append(nid)
            continue
        for s in succ:
            ins = list(graph.g.nodes[s].get("inputs") or [])
            new: list[str] = []
            for i in ins:
                replacements = preds if i == nid else [i]
                for r in replacements:
                    if r not in new:
                        new.append(r)
            graph.g.nodes[s]["inputs"] = new
            graph.g.remove_edge(nid, s)
            for p in new:
                if p.startswith("n:") and p in graph.g.nodes:
                    graph.g.add_edge(p, s)
        graph.g.remove_node(nid)
        changed.append(nid)
    return changed


def _consumers_have_other_evidence(graph: Graph, nid: str) -> bool:
    """Can the failed node be skipped without starving its consumers?

    A fan-out worker shares its consumers with its siblings. When the
    surviving siblings already carry the majority of every consumer's data
    inputs, the run continues on what was produced instead of paying a
    recovery planner to redo the fan-out. Live evidence for the alternative:
    two failed researchers of three queued TWO recovery planners, each
    re-emitted the full chain, and the run delivered the document twice while
    the original author waited on a node that would never complete.

    A consumer with no surviving sibling (a sole dependency) keeps the node
    alive for replanning — a chain must not silently lose its only evidence.
    """
    succs = [s for s in graph.g.successors(nid)
             if graph.g.nodes[s]["skill"] != "critic"]
    if not succs:
        return False
    for s in succs:
        others = [
            p for p in graph.g.predecessors(s)
            if p != nid and p.startswith("n:") and p in graph.g.nodes
            and graph.g.nodes[p]["skill"] not in ("planner", "critic")
        ]
        total = len(others) + 1          # this failed node was one data input
        alive = sum(1 for p in others
                    if graph.g.nodes[p]["status"] == "complete")
        if alive * 2 < total:            # survivors are a minority: recover
            return False
    return True


def _recovery_targets(graph: Graph, nid: str) -> list[str]:
    """The failed nodes a completed recovery planner is replacing.

    Filtered to targets that are actually `failed`: a critic-fail recovery
    targets a COMPLETE node and must keep the old no-splice behaviour.
    """
    md = graph.g.nodes[nid].get("metadata") or {}
    ids = md.get("recovers_all") or md.get("recovers") or []
    if isinstance(ids, str):
        ids = [ids]
    return [r for r in ids if isinstance(r, str) and r in graph.g.nodes
            and graph.g.nodes[r]["status"] == "failed"]


def _annotate_failure_report(graph: Graph, nid: str, fr: str) -> str:
    """Extend a failure report with the failed node's brief and its waiting
    consumers, so the recovery planner scopes its plan to the missing piece
    instead of re-emitting the pipeline that already exists around it."""
    d = graph.g.nodes[nid]
    q = str((d.get("metadata") or {}).get("question") or "").strip()
    if q:
        fr += f"\nits brief was: {q[:300]}"
    waiting = [s for s in graph.g.successors(nid)
               if graph.g.nodes[s]["status"] in ("pending", "running")]
    if waiting:
        fr += ("\nexisting nodes already wait on this output: "
               + ", ".join(f"{s} ({graph.g.nodes[s]['skill']})"
                           for s in waiting))
    return fr


def _splice_recovery(graph: Graph, added: list[str],
                     rec_ids: list[str]) -> bool:
    """Rewire a recovery plan in place of the FAILED node(s) it replaces.

    A replan used to splice its nodes BESIDE the original chain, so the plan's
    own author/formatter rendered a second document while the original
    consumers waited forever on a node stuck in `failed` (ready_nodes never
    releases on a failed predecessor). One request, two deliverables. Here the
    plan REPLACES the failure instead:

      - nodes whose skill already exists downstream of the failure are pruned
        — a second author re-renders the deliverable rather than finishing it;
      - the remaining plan terminals are wired into every waiting consumer,
        in the failed node's place;
      - the failed node is marked skipped, which releases its subtree exactly
        once, on the original downstream chain.

    Returns True when the graph changed (caller persists the graph).
    """
    kept = [n for n in added if n in graph.g.nodes]
    if not kept:
        # The planner planned nothing. Release the consumers anyway; their
        # prompt carries the upstream-missing marker from resolve_inputs.
        for r in rec_ids:
            graph.mark(r, "skipped", error="recovery plan was empty")
        return True
    failed_skills = {graph.g.nodes[r]["skill"] for r in rec_ids}
    occupied: set[str] = set()
    for r in rec_ids:
        for d in nx.descendants(graph.g, r):
            occupied.add(graph.g.nodes[d]["skill"])
    occupied -= failed_skills
    occupied.discard("critic")
    pruned = {n for n in kept if graph.g.nodes[n]["skill"] in occupied}
    # Reconnect kept consumers of pruned nodes to the pruned node's own
    # inputs (a replacement chain with an interior duplicate role).
    for n in list(pruned):
        repl = [p for p in (graph.g.nodes[n].get("inputs") or [])
                if p in graph.g.nodes and p not in pruned]
        for c in list(graph.g.successors(n)):
            if c in pruned:
                continue
            ins = list(graph.g.nodes[c].get("inputs") or [])
            new: list[str] = []
            for i in ins:
                for rr in (repl if i == n else [i]):
                    if rr not in new:
                        new.append(rr)
            graph.g.nodes[c]["inputs"] = new
            graph.g.remove_edge(n, c)
            for p in new:
                if p.startswith("n:") and p in graph.g.nodes:
                    graph.g.add_edge(p, c)
    for n in pruned:
        graph.g.remove_node(n)
    kept = [n for n in kept if n not in pruned]
    terminals = [n for n in kept
                 if not any(s in kept for s in graph.g.successors(n))]
    if not terminals:
        # Everything was a duplicate role. Dropping the inputs would also
        # drop the honest upstream-missing marker, so the failures are simply
        # skipped and the consumers proceed with what they already have.
        for r in rec_ids:
            graph.mark(r, "skipped",
                       error="recovery plan duplicated the waiting pipeline")
        return True
    for r in rec_ids:
        for s in list(graph.g.successors(r)):
            if s not in graph.g.nodes:
                continue
            graph.g.remove_edge(r, s)
            ins = list(graph.g.nodes[s].get("inputs") or [])
            new = []
            for i in ins:
                for rr in (terminals if i == r else [i]):
                    if rr not in new:
                        new.append(rr)
            graph.g.nodes[s]["inputs"] = new
            for t in new:
                if t.startswith("n:") and t in graph.g.nodes:
                    graph.g.add_edge(t, s)
        graph.mark(r, "skipped",
                   error=f"replaced by recovery plan ({', '.join(terminals)})")
    return True


def _fallback_facets(query: str) -> list[str]:
    """Facets to research when the planner wrote no research_plan.

    Deliberately more than one. A single worker asked for "everything about X"
    returns one pass of the obvious material - which is the failure the
    Research section's fan-out exists to prevent, so the fallback must not
    reintroduce it.
    """
    import re as _re
    q = " ".join(str(query or "").split())
    # Strip the Authoring console's own prefix AND the task verb together.
    # The console prefixes every brief with "Create a document.\n\n" and then
    # the task ("Write a comprehensive treatise of about 60 pages on X").
    # The old pattern matched only the verb...about/on/of, so with the
    # "Create a document." prefix in front nothing matched and the whole raw
    # prompt became the facet - a logged run emitted a section titled
    # "Create a document. Write a comprehensive tre..." verbatim.
    q = _re.sub(r"(?i)^\s*create a (?:document|report|deck|presentation)"
                r"\s*[.:]?\s*", "", q)
    # Strip the authoring instruction so the worker is asked to RESEARCH a
    # subject, not to obey an order. "write a 6 page report on the Bluebook"
    # becomes "the Bluebook", and each facet then reads as a research
    # question rather than a task list.
    q = _re.sub(r"(?i)^\s*(please\s+)?(can you\s+)?"
                r"(write|create|make|build|generate|draft|produce|prepare|"
                r"give me|turn)\b[^.?]*?\b(about|on|regarding|covering|for|of)\s+",
                "", q, count=1)
    # A surviving length clause ("60 pages", "under 200 words") is not a topic.
    q = _re.sub(r"(?i)\b(?:about|around|roughly)?\s*\d+\s*(?:page|pages|word|"
                r"words)\b", "", q)
    q = q.strip(" ?.!;:,.") or "the subject of the requested document"
    # Stripping the length clause can leave a leading preposition or article
    # dangling ("on the engineering of Roman aqueducts"). Clean it or the
    # facet reads as a fragment.
    q = _re.sub(r"(?i)^\s*(?:on|of|about|regarding|covering|for|the|a|an)\b\s*",
                "", q)
    q = _re.sub(r"(?i)^\s*(?:on|of|about|regarding|covering|for|the|a|an)\b\s*",
                "", q)
    q = q.strip(" ?.!;:,.") or "the subject of the requested document"
    if not q:
        q = "the subject of the requested document"
    return [
        f"{q}: what it is, how it works, and the current state of it",
        f"{q}: specific evidence, figures, examples and primary sources",
        f"{q}: limitations, disagreements, edge cases and open questions",
    ]


# Sessions whose authoring research fan-out has already been spliced. A second
# planner node in the same session must NOT add a second identical fan-out.
_AUTHORING_RESEARCH_SESSIONS: set[str] = set()


def ensure_authoring_research(graph: Graph, query: str,
                              session_id: str = "") -> list[str]:
    """Make an authoring plan research the way a Research question does.

    The Authoring section is a Research request with a document at the end of
    it. A real run of "write me a report on X" planned as
    `planner -> author -> formatter`: the author node saw only USER_QUERY,
    called no retrieval, and wrote the document from the model's own memory.
    That is precisely the failure the Research pipeline exists to prevent.

    Prompting the planner is not enough on its own - it is a model, and this
    shape is the path of least resistance. So the structure is enforced here:
    any author/deck node with no evidence-gathering ancestor gets a
    `retriever` and a small fan-out of `researcher` nodes spliced above it,
    and its inputs are rewired to read them.

    IDEMPOTENT PER SESSION. This ran once per COMPLETED PLANNER NODE, so a
    second planner node - a recovery replan, or the sectioned expansion - ran
    it again and spliced a SECOND identical researcher fan-out. Measured:
    12 researcher nodes for 6 facets, nodes 1-6 repeated verbatim as 7-12,
    ~50% of every run's research spend spent producing nothing. The
    skill check at the call site prevented applying this twice for ONE
    planner node; it did nothing about a second planner node.

    Returns the ids added, for logging and tests.
    """
    if session_id:
        if session_id in _AUTHORING_RESEARCH_SESSIONS:
            return []
        _AUTHORING_RESEARCH_SESSIONS.add(session_id)
    added: list[str] = []
    for nid in list(graph.g.nodes):
        if graph.g.nodes[nid]["skill"] not in _AUTHORING_SKILLS:
            continue
        # Does anything upstream already gather evidence? Walk the ancestors.
        seen: set[str] = set()
        stack = list(graph.g.predecessors(nid))
        grounded = False
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            if graph.g.nodes[cur]["skill"] in _RESEARCH_SKILLS:
                grounded = True
                break
            stack.extend(graph.g.predecessors(cur))
        if grounded:
            continue

        old_inputs = list(graph.g.nodes[nid].get("inputs") or [])
        # A retriever first: the user's own uploads are the highest-quality
        # source available and cost one call.
        ret = graph.add_node("retriever", inputs=["USER_QUERY"])
        added.append(ret)
        edges: list[tuple[str, str]] = []

        # Facets come from the planner's own research_plan when it wrote one,
        # so the rewrite reinforces the plan instead of inventing a new topic.
        facets: list[str] = []
        try:
            for _pnid in graph.g.nodes:
                if graph.g.nodes[_pnid]["skill"] != "planner":
                    continue
                _res = (graph.g.nodes[_pnid].get("result") or {})
                _rp = ((_res.get("output") or {}) if isinstance(_res, dict)
                       else {}).get("research_plan")
                if isinstance(_rp, dict):
                    facets = [str(f) for f in (_rp.get("facets") or []) if f]
        except Exception:
            facets = []
# No facet cap: the planner decided, and every facet it emitted becomes
        # a researcher. MAX_NODES bounds a runaway loop and MAX_CONCURRENCY
        # bounds the wave size; neither drops a node from the plan.
        facets = facets or _fallback_facets(query)

        workers: list[str] = []
        for i, facet in enumerate(facets):
            w = graph.add_node("researcher", inputs=[ret],
                               metadata={"question": facet,
                                         "label": f"research{i + 1}"})
            workers.append(w)
            added.append(w)
            edges.append((ret, w))

        # The author node reads every worker. It keeps USER_QUERY so it still
        # knows the shape the user asked for, and the retriever so the user's
        # own uploads stay in view.
        for old in old_inputs:
            if old == "USER_QUERY" and "USER_QUERY" not in graph.g.nodes[nid]["inputs"]:
                graph.g.nodes[nid]["inputs"].append(old)
        for w in workers:
            if w not in graph.g.nodes[nid]["inputs"]:
                graph.g.nodes[nid]["inputs"].append(w)
        if ret not in graph.g.nodes[nid]["inputs"]:
            graph.g.nodes[nid]["inputs"].insert(0, ret)
        for a, b in edges:
            graph.g.add_edge(a, b)
        for w in workers:
            graph.g.add_edge(w, nid)
        graph.g.add_edge(ret, nid)
        print(f"[flow] authoring without research: added retriever + "
              f"{len(workers)} researcher node(s) above {nid}")
    return added


def _safe_remember(query: str, sid: str) -> None:
    """Best-effort memory write for the user query, safe to run on a daemon
    thread off the critical path. Any failure is logged and swallowed so a
    bad embed or classifier can never abort the agent run.

    Phase 1 drawers: the write carries the run's session id (additive
    metadata — global reads still see the item; drawer recall can scope
    to it later without any backfill)."""
    try:
        memory_svc.remember(query, source="user_query", run_id=sid,
                             session_id=sid)
    except Exception as e:  # pragma: no cover - deferred write must not crash
        print(f"[memory.remember] skipped: {e!r}")


# Files the run rendered (author/deck call render_document). Reported in the
# stream's `done` event so the console can build download cards WITHOUT
# parsing the answer text: the formatter legitimately rewrites the receipt
# as prose, and any handle in it was not reliably preserved.
_DOCUMENT_INTENT_RE = re.compile(
    r"create a document|produce a (pdf|pptx|docx|xlsx|word|excel|deck)"
    r"|make a (pdf|pptx|docx|xlsx)|render a (pdf|pptx|docx|xlsx)",
    re.I)


def take_produced_files(session_id: str) -> list[dict]:
    """Read this session's rendered-file sidecar. Never raises."""
    try:
        if (not session_id or session_id.startswith(".")
                or "/" in session_id or "\\" in session_id):
            return []
        p = _SROOT / "state" / "sessions" / session_id / "produced_files.json"
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        out = []
        # Resolve every handle against the store, not just its shape. The
        # writer refuses to persist an unresolvable id (see _finish_write,
        # which checks artifacts.get_bytes), but sidecars written before that
        # guard existed still name handles like "art:dummy" and
        # "art:mock-placeholder". Reading those back produced a row of
        # download buttons that all failed with "400 malformed artifact id",
        # indistinguishable from the genuine files beside them. Verifying at
        # READ time closes the boundary properly: the guard belongs on both
        # sides of persistence, not only the writer.
        try:
            import artifacts as _arts
        except Exception:
            _arts = None
        for e in data:
            if not isinstance(e, dict):
                continue
            art = e.get("artifact")
            if not (isinstance(art, str)
                    and re.fullmatch(r"art:[0-9a-fA-F]{16}", art)):
                continue
            if _arts is not None:
                try:
                    _arts.get_bytes(art)
                except Exception:
                    print(f"  -> produced_files names {art}, which the store "
                          f"cannot resolve; omitting it from the file list")
                    continue
            # Cost is attached per file but is the RUN's total, labelled `scope:
            # "run"`. Splitting it across files would be a fiction - the
            # researchers, planner and author share one budget and cannot be
            # attributed per document after the fact. The console shows it as
            # the cost of producing this run.
            cost = session_cost_totals(session_id)
            # bytes and stats are carried through. They are already in the
            # sidecar and in the author node's `produced`, but this reader
            # dropped them, so the `done` frame told the console a file had
            # no size and no word count - the exact signal the model uses to
            # notice it shipped a stub, and the one a person uses to decide
            # whether to open it.
            out.append({"artifact": art,
                        "filename": str(e.get("filename") or "document"),
                        "format": str(e.get("format") or "").lower(),
                        "skill": str(e.get("skill") or ""),
                        "bytes": e.get("bytes"),
                        "blocks": e.get("blocks"),
                        "stats": e.get("stats") or {},
                        "cost": cost})
        return out
    except Exception:
        return []


def _cost_from_gateway(session_id: str, zero: dict) -> dict:
    """Last resort: the gateway's own per-session total.

    `turn_costs.json` is only written when `_session_cost_delta` could reach the
    gateway's cost API. When that call fails the ledger is deliberately NOT
    written - a fabricated `$0` row would be worse than none - which left the
    Authoring UI reporting `0 tokens` for documents that had in fact spent
    hundreds of thousands. Falling back to the live session total shows the
    run's LIFETIME spend rather than one turn's, so it is labelled `scope:
    "session"` and the UI must not present it as per-document.

    Any failure here returns `zero` unchanged: a missing number is honest, a
    wrong one is not."""
    try:
        import agent_server as _as
        bd = _as._session_cost_breakdown(session_id)
        if not isinstance(bd, dict) or not bd:
            return zero
        # `_session_cost_breakdown` returns PER-AGENT buckets
        # ({agent: {usd, in_tok, out_tok, calls}}), not flat totals. Reading
        # `input_tokens` off it returned None and the UI showed 0 for a
        # session the gateway was holding 1.4M tokens for.
        it = ot = calls = 0
        usd = 0.0
        by_agent: dict[str, dict] = {}
        for agent, v in bd.items():
            if not isinstance(v, dict):
                continue
            a_in = int(v.get("in_tok") or 0)
            a_out = int(v.get("out_tok") or 0)
            a_usd = float(v.get("usd") or 0.0)
            a_calls = int(v.get("calls") or 0)
            it += a_in
            ot += a_out
            usd += a_usd
            calls += a_calls
            by_agent[agent] = {"input_tokens": a_in,
                               "output_tokens": a_out,
                               "cost": a_usd, "calls": a_calls}
        if not (it or ot):
            return zero
        return {"input_tokens": it, "output_tokens": ot,
                "total_tokens": it + ot, "turns": 0, "calls": calls,
                "cost": round(usd, 6), "currency": "USD",
                "by_agent": by_agent, "scope": "session"}
    except Exception:
        return zero


def session_cost_totals(session_id: str) -> dict:
    """Total tokens and spend for one session, for the done frame.

    Read from the per-turn cost sidecar, which is written as calls complete.
    Never raises: a missing or unreadable sidecar is zero, because the
    download list must still appear when cost accounting is unavailable.
    """
    zero = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "turns": 0, "calls": 0, "cost": 0.0, "currency": "USD",
            "by_agent": {},
            # The figure is the whole run's spend, attached to each produced
            # file for display. Splitting it per document would be a fiction:
            # the planner, researchers and author share one budget.
            "scope": "run"}
    try:
        if (not session_id or session_id.startswith(".")
                or "/" in session_id or "\\" in session_id):
            return zero
        p = _SROOT / "state" / "sessions" / session_id / "turn_costs.json"
        if not p.exists():
            return _cost_from_gateway(session_id, zero)
        rows = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(rows, dict):
            rows = rows.get("turns") or []
        if not isinstance(rows, list):
            return zero
        # The sidecar schema is {"ts", "query", "per_agent": {skill: {usd,
        # in_tok, out_tok, calls}}, "totals": {...}} - there are no top-level
        # `input_tokens`/`output_tokens`/`cost` keys, so reading those (as an
        # earlier version did) reported 0 tokens for every run.
        it = ot = 0
        cost = 0.0
        calls = 0
        by_agent: dict[str, dict] = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            per = r.get("per_agent")
            if isinstance(per, dict):
                for agent, v in per.items():
                    if not isinstance(v, dict):
                        continue
                    acc = by_agent.setdefault(
                        agent, {"input_tokens": 0, "output_tokens": 0,
                                "cost": 0.0, "calls": 0})
                    acc["input_tokens"] += int(v.get("in_tok") or 0)
                    acc["output_tokens"] += int(v.get("out_tok") or 0)
                    acc["cost"] += float(v.get("usd") or 0.0)
                    acc["calls"] += int(v.get("calls") or 0)
            t = r.get("totals")
            if isinstance(t, dict):
                it += int(t.get("in_tok") or 0)
                ot += int(t.get("out_tok") or 0)
                cost += float(t.get("usd") or 0.0)
                calls += int(t.get("calls") or 0)
        for acc in by_agent.values():
            if not it and not ot:
                it += acc["input_tokens"]
                ot += acc["output_tokens"]
                cost += acc["cost"]
                calls += acc["calls"]
        return {"input_tokens": it, "output_tokens": ot,
                "total_tokens": it + ot, "turns": len(rows),
                "calls": calls, "cost": round(cost, 6),
                "currency": "USD", "scope": "run",
                "by_agent": by_agent}
    except Exception:
        return zero


def _write_run_status(sid: str, *, degraded: bool = False,
                      detail: list | None = None) -> None:
    """Persist how this run ENDED, next to its artifacts.

    The graph records what each node did; it does not record the verdict. So
    the two facts the console most needs on reload - "this answer is
    incomplete, here is what is missing" and "this run was stopped by the
    operator" - existed only inside the SSE frame that carried them. Reopening
    the run showed a report with no warning at all.

    Best-effort: a run that cannot write its own status file must still
    deliver its answer.
    """
    try:
        import json as _json
        d = _SROOT / "state" / "sessions" / sid
        if not sid or sid.startswith(".") or "/" in sid or "\\" in sid:
            return
        d.mkdir(parents=True, exist_ok=True)
        _p = d / "run_status.json"
        _tmp = _p.with_suffix(".json.tmp")
        _tmp.write_text(_json.dumps({
            "degraded": bool(degraded),
            "detail": [str(x)[:300] for x in (detail or [])][:20],
            "updated": time.time(),
        }, indent=2), encoding="utf-8")
        _tmp.replace(_p)
    except Exception as e:
        print(f"  -> could not persist run status for {sid}: {e!r}")


def read_run_status(session_id: str) -> dict:
    """How a past run ended. Never raises; {} when nothing was recorded."""
    try:
        if (not session_id or session_id.startswith(".")
                or "/" in session_id or "\\" in session_id):
            return {}
        p = _SROOT / "state" / "sessions" / session_id / "run_status.json"
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# F10 FIX (cross-session memory contamination): best-effort append of the
# completed turn (query + final answer) to this session's turn log. Runs on
# a daemon thread so persistence cost never lands on the critical path.
def _safe_log_turn(query: str, sid: str, answer: str) -> None:
    try:
        turnlog.append(sid, query, answer)
    except Exception as e:  # pragma: no cover - must never crash the run
        print(f"[turnlog] skipped: {e!r}")


# Phase 2 drawers: working notes belong to one run. At run end, purge this
# run's working drawer items — but only those created BEFORE the run
# started, so a slow daemon remember from this very run is never caught in
# its own purge (the older_than cutoff is the race guard).
def _safe_purge_working(sid: str, run_start_iso: str) -> None:
    try:
        memory_svc.clear(session_id=sid, drawers=["working"],
                         older_than=run_start_iso)
    except Exception as e:  # pragma: no cover - must never crash the run
        print(f"[memory.purge] skipped: {e!r}")


# F5 FIX (identity-write race): queries that state durable facts about the
# user ("my name is X", "remember that ...") must be persisted BEFORE the
# next turn reads memory — a daemon-thread write (classifier + embed ≈ 3-7s)
# loses the race when the user immediately asks "what is my name?". Detect
# these identity-bearing queries and write them synchronously; everything
# else keeps the fast async path.
_IDENTITY_MARKERS = (
    "my name is", "i am called", "call me ", "remember that",
    "remember this", "my favorite", "my favourite", "i like", "i prefer",
    "i work at", "i live in",
)


def _is_identity_write(query: str) -> bool:
    q = query.lower().strip()
    return any(m in q for m in _IDENTITY_MARKERS)


# ── Executor ─────────────────────────────────────────────────────────────────

class Executor:
    def __init__(self, registry: SkillRegistry | None = None):
        ensure_gateway()
        self.registry = registry or SkillRegistry()
        # Planner-only run parameters, set by run() (see _research_depth_note
        # in agent_server). Empty means "no operator override".
        self._run_note = ""

    async def run(self, query: str, *, session_id: str | None = None,
                     resume: bool = False,
                     doc_setup: dict | None = None,
                     should_cancel: Callable[[], bool] | None = None,
                     run_note: str = "",
                     run_kind: str = "") -> str:
        sid = session_id or f"s8-{uuid.uuid4().hex[:8]}"
        store = SessionStore(sid)
        # Planner-only run parameters (the operator's research depth, etc.).
        # Deliberately NOT part of `query`: every node reads the query, and a
        # worker that read "deliver a final report as Markdown" wrote a report
        # instead of its JSON contract and failed (see agent_server's
        # _research_depth_note). Only the planner - the node that sets the
        # fleet size - is told.
        self._run_note = (run_note or "").strip()
        # The run's KIND decides which plan shapes are legal. Research answers
        # in the transcript; Authoring renders a file. They share one planner,
        # so the contract is enforced in `ensure_research_is_text_only` rather
        # than requested from the model.
        self._run_kind = (run_kind or "").strip().lower()
        # Persist the run's KIND up front so the sidebar can list a run from the
        # moment it starts, before it has produced a single node. `authoring`
        # (file-rendering) and `research` runs share the session store; without
        # this a just-started research run was invisible until its planner
        # emitted a second skill.
        if run_kind:
            try:
                store.write_run_meta({"kind": run_kind})
            except Exception as e:  # pragma: no cover - never block a run
                print(f"[run_meta] skipped: {e!r}")
        run_start_iso = datetime.now(timezone.utc).isoformat()
        if resume:
            existing = store.read_graph()
            if existing is None:
                raise RuntimeError(f"cannot resume {sid}: no graph.json on disk")
            graph_obj = existing
            graph = Graph.__new__(Graph)
            graph.g = graph_obj
            graph._counter = max(
                [int(n.split(":")[1]) for n in graph.g.nodes if n.startswith("n:")] or [0]
            )
            for _, d in graph.g.nodes(data=True):
                if d["status"] == "running":
                    d["status"] = "pending"
            if not query:
                query = store.read_query()
        else:
            store.write_query(query)
            graph = Graph()
            graph.add_node("planner", inputs=["USER_QUERY"])

        print(f"\n{'=' * 78}\nsession {sid}  -  query: {query}\n{'=' * 78}")
        # Read memory ONCE at session start; the same hits flow into every
        # skill's prompt. The S7 contract is that every cognitive role sees
        # memory; carrying that forward verbatim here is what makes S7's
        # indexing investment continue to pay off in S8.
        # Phase 2: explicit session-start drawer set (working notes are
        # run-scoped — a new run has none yet — so recall is everything
        # else, legacy included until backfilled).
        # Document allowlist: a research run may only draw on documents that are
        # currently enabled. A failure to read the registry fails CLOSED (no
        # documents) rather than silently exposing every document.
        doc_ids = None
        try:
            from agent_server import _gw_enabled_doc_ids as _enabled
            doc_ids = _enabled()
        except Exception as e:
            print(f"[documents] registry unavailable ({e!r}); "
                  f"excluding documents from this run")
            doc_ids = set()
        memory_hits = memory_svc.read(
            query, drawers=memory_svc.SESSION_DRAWERS, doc_ids=doc_ids) or []
        if memory_hits:
            print(f"[memory.read] {len(memory_hits)} hit(s) visible to every skill this run")
        # Phase 3: active policies constrain behavior and reach EVERY skill
        # prompt with no exclusions (unlike memory hits). Few, small, cached
        # per run — one extra local round-trip at session start.
        try:
            policy_notes = memory_svc.policies() or []
        except Exception as e:  # pragma: no cover - fail-soft, never block
            print(f"[memory.policies] skipped: {e!r}")
            policy_notes = []
        if policy_notes:
            print(f"[memory.policies] {len(policy_notes)} active polic(ies) injected")
        # F10 FIX: load THIS conversation's recent turns once at session
        # start. This is the authoritative in-thread history — global memory
        # hits are background facts about the user and must never be
        # presented as conversation history (see skills.render_prompt).
        prior_turns = turnlog.recent(sid)
        if prior_turns:
            print(f"[turnlog] {len(prior_turns)} prior turn(s) loaded for this conversation")
        # F5 FIX: identity-bearing writes go synchronously (bounded by the
        # classifier's 60s client timeout) so the fact is durable before the
        # user's next turn reads memory. All other queries keep the fast
        # daemon-thread path — the current answer never depends on those.
        if _is_identity_write(query):
            print("[memory.remember] identity-bearing query; writing synchronously")
            _safe_remember(query, sid)
        else:
            threading.Thread(
                target=_safe_remember, args=(query, sid), daemon=True
            ).start()

        formatter_answer: str | None = None
        executed_count = 0
        # Cap on recovery replans per failed node so a persistently broken
        # provider (e.g. empty/503 responses) can't spin the run forever.
        # Per-target cap for critic-fail recovery; see P1 #5 fix below.
        recovered_branches: dict[str, bool] = {}
        # NOTES_RUNS round-3 review #5: when the cap fires, the branch is
        # skipped silently and the final answer reflects missing data with
        # no flag. Track every second-or-later critic-fail here so the
        # final log can surface it.
        critic_fail_cap_hit: list[str] = []
        # Wall-clock already consumed per skill in this run, so the budget
        # note above can tell the model it is running late. Populated as
        # nodes complete.
        spent_by_skill: dict[str, float] = {}
        # Recovery is capped per NODE and per RUN. The per-node cap alone
        # never fired for a persistently failing skill: every recovery
        # re-plans a FRESH node (n:5, n:8, n:11, ...) with a new id, so each
        # attempt started its own count at zero. One authoring run reached 37
        # author attempts and ~778k input tokens before the node cap stopped
        # it. A skill that keeps failing will not succeed on the fifth try
        # with the same inputs, so it is bounded per SKILL as well.
        recovery_attempts: dict[str, int] = {}
        MAX_RECOVERY_ATTEMPTS = 2
        skill_failures: dict[str, int] = {}
        exhausted_skills: set[str] = set()
        MAX_SKILL_FAILURES = 3
        recovery_rounds = 0
        MAX_RECOVERY_ROUNDS = 8

        while True:
            # Operator cancel (POST /api/chat/cancel): checked BETWEEN
            # dispatch batches, so an in-flight node finishes its own LLM call
            # (asyncio can't kill it mid-await) but nothing new starts. Only
            # pending work is abandoned — nodes that already completed stay
            # complete so the partial graph is still meaningful on disk.
            if should_cancel is not None and should_cancel():
                for _nid in list(graph.g.nodes):
                    if graph.g.nodes[_nid]["status"] in ("pending", "running"):
                        graph.mark(_nid, "skipped")
                store.write_graph(graph.g)
                print(f"[flow] cancelled — {executed_count} node(s) ran, rest skipped")
                break
            ready = graph.ready_nodes()
            if not ready and not graph.has_running():
                break
            # Bound BOTH the batch and the projected total. The cap check must
            # use the batch actually dispatched, not the full ready set: a
            # planner that emits 9 nodes with MAX_CONCURRENCY=8 would
            # otherwise see
            # `executed + 5 > MAX_NODES` on the first round, skip every node,
            # and spin without ever advancing.
            if executed_count + len(ready) > MAX_NODES:
                print(f"[flow] node cap {MAX_NODES} hit at {executed_count}; "
                      f"stopping")
                # Mark the un-run nodes skipped. They were left `pending` on
                # disk, and the startup reconciler only rescues nodes that
                # look `running` -- so a capped run persisted with permanent
                # pending nodes and read as never-finished forever.
                for _nid in ready:
                    graph.mark(_nid, "skipped",
                               error=f"node cap {MAX_NODES} reached")
                store.write_graph(graph.g)
                break

            # Bounded fan-out. Every ready node was gathered at once, so a
            # planner that emitted 20 facets would open 20 concurrent LLM
            # calls and blow through the provider's rate limit. Deferring a
            # node requires leaving it PENDING, so this must be computed
            # before the running marks below.
            batch = ready[:MAX_CONCURRENCY]
            if len(ready) > MAX_CONCURRENCY:
                print(f"[flow] batch limited to {MAX_CONCURRENCY}; "
                      f"{len(ready) - MAX_CONCURRENCY} node(s) deferred")

            # Mark ONLY the dispatched batch as running. Marking the whole
            # ready set left deferred nodes stuck in `running` with nothing
            # executing them: has_running() stayed true, the scheduler kept
            # waking, ready_nodes() returned nothing, and the run spun forever.
            for nid in batch:
                graph.mark(nid, "running")
            store.write_graph(graph.g)

            # return_exceptions=True: one sibling raising CancelledError /
            # BaseException must not abort the whole fan-out batch. Results
            # that come back as exceptions are coerced to failed AgentResults
            # below (see _run_one's own Exception catch for the common case).
            # Per-node prompt note: the soft time-budget reminder, plus the
            # operator's planner-only run note. Both ride the same channel
            # (render_prompt appends it verbatim) rather than the query, which
            # every worker reads.
            def _node_note(_nid: str) -> str:
                _sk = graph.g.nodes[_nid]["skill"]
                _n = _budget_note(_sk, spent_by_skill)
                if _sk == "planner" and self._run_note:
                    _n = f"{self._run_note}\n\n{_n}" if _n else self._run_note
                return _n

            outcomes = await asyncio.gather(*[
                self._run_one(nid, graph, sid, query, store, memory_hits,
                              prior_turns, policy_notes,
                              _node_note(nid),
                              doc_setup)
                for nid in batch],
                                            return_exceptions=True)

            coerced: list[tuple[str, AgentResult, str]] = []
            for nid, outcome in zip(batch, outcomes):
                if isinstance(outcome, BaseException):
                    # Cancellation is not a node failure: coercing it let the
                    # loop dispatch the next batch after the run's task was
                    # already cancelled (C7).
                    if isinstance(outcome, asyncio.CancelledError):
                        raise outcome
                    skill_name = graph.g.nodes[nid]["skill"]
                    outcome = (nid, AgentResult(success=False, agent_name=skill_name,
                                                error=f"exception: {_readable_exc(outcome)}"),
                               "(exception escaped dispatcher)")
                coerced.append(outcome)
            outcomes = coerced

            for nid, result, prompt in outcomes:
                executed_count += 1
                graph.g.nodes[nid]["result"] = result
                graph.mark(nid, "complete" if result.success else "failed")
                # Feed the elapsed time back into the budget so the NEXT
                # invocation of this skill in this run knows it is late.
                _sk = graph.g.nodes[nid]["skill"]
                spent_by_skill[_sk] = spent_by_skill.get(_sk, 0.0) + float(
                    getattr(result, "elapsed_s", 0.0) or 0.0)
                store.write_node(NodeState(
                    node_id=nid, skill=graph.g.nodes[nid]["skill"],
                    status=graph.g.nodes[nid]["status"],
                    inputs=graph.g.nodes[nid]["inputs"],
                    result=result, prompt_sent=prompt,
                    started_at=time.time() - result.elapsed_s,
                    completed_at=time.time(),
                ))
                print(f"[{nid}] {graph.g.nodes[nid]['skill']:18s} "
                      f"{graph.g.nodes[nid]['status']:8s} "
                      f"({result.elapsed_s:.1f}s)"
                      + (f"  err={result.error[:80]}" if result.error else ""))

            # PASS 2 — act on the outcomes. Every mark happened in pass 1, so
            # each decision below (absorb vs replan, and the recovery
            # planner's `prior_complete`) sees the whole batch as it really
            # ended. Interleaving marks with decisions made a still-unmarked
            # sibling invisible: two recovery planners both ran with
            # prior_complete=[] and re-researched facets the batch had just
            # finished.
            replans: list[tuple[str, RecoveryDecision]] = []
            for nid, result, prompt in outcomes:

                if result.success:
                    if graph.g.nodes[nid]["skill"] == "critic":
                        if handle_critic_verdict(nid, result, graph,
                                                 recovered_branches,
                                                 critic_fail_cap_hit):
                            continue
                        # verdict == pass: the child is now ready to run.
                    # PLANNER SHORT-CIRCUIT: when the Planner answers directly
                    # ({"answer": "..."} with NO successor nodes) the run is
                    # done — trivial queries ("hi", "what is 2+2") must not
                    # pay for a formatter hop, and the raw dict must never
                    # leak through the last-complete-node JSON fallback.
                    if (graph.g.nodes[nid]["skill"] == "planner"
                            and not result.successors):
                        _direct = result.output.get("answer")
                        if isinstance(_direct, str) and _direct.strip():
                            formatter_answer = _direct.strip()
                            print(f"[{nid}] planner short-circuit: direct "
                                  f"answer, no downstream nodes")
                            continue
                    added = graph.extend_from(nid, result,
                                              registry=self.registry)
                    # Authoring gets the Research chain, enforced rather than
                    # requested: see ensure_authoring_research. Runs after
                    # extend_from so the planner's nodes exist, and only for
                    # the planner so it cannot be applied twice.
                    if graph.g.nodes[nid]["skill"] == "planner":
                        _rec_md = graph.g.nodes[nid].get("metadata") or {}
                        _is_rec = bool(_rec_md.get("recovers")
                                       or _rec_md.get("recovers_all"))
                        # Research answers in the transcript. Strip any
                        # author/deck node the planner reached for BEFORE the
                        # authoring guard, so a research run cannot be turned
                        # into a document run by a model that liked the word
                        # "report". The session guard would skip a recovery
                        # planner (the session is already in the set), so a
                        # recovery replan forces the rewrite — it must not
                        # re-introduce an authoring node the first planner
                        # was never allowed to keep.
                        if getattr(self, "_run_kind", "") == "research":
                            _changed = ensure_research_is_text_only(
                                graph,
                                session_id="" if _is_rec else session_id)
                            if _changed:
                                print(f"[{nid}] research is text-only: "
                                      f"removed {len(_changed)} authoring "
                                      f"node(s) {_changed}")
                                store.write_graph(graph.g)
                        # Splice a recovery plan in place of the failed node
                        # it replaces: prune duplicate downstream roles,
                        # rewire the waiting consumers to the plan's
                        # terminals, and release the original chain.
                        _rec_ids = _recovery_targets(graph, nid)
                        if _rec_ids:
                            _splice_recovery(graph, added, _rec_ids)
                            print(f"[{nid}] recovery plan spliced for "
                                  f"{', '.join(_rec_ids)}; waiting nodes "
                                  f"rewired to the plan")
                            store.write_graph(graph.g)
                        _added = ensure_authoring_research(
                            graph, query, session_id=session_id)
                        if _added:
                            store.write_graph(graph.g)
                    if graph.g.nodes[nid]["skill"] == "formatter":
                        fa = result.output.get("final_answer")
                        if isinstance(fa, str) and fa.strip():
                            formatter_answer = fa
                    # Structured research plan. The planner may emit a
                    # `research_plan` block; persisting it makes the topic,
                    # facets and source hints first-class instead of something
                    # the UI has to regex back out of the researcher's system
                    # prompt in query.txt.
                    if (graph.g.nodes[nid]["skill"] == "planner"
                            and not store.read_plan()):
                        _plan = result.output.get("research_plan")
                        if isinstance(_plan, dict) and _plan.get("topic"):
                            try:
                                store.write_plan({
                                    "topic": str(_plan.get("topic"))[:200],
                                    # No cap. This slice silently truncated the
                                    # stored plan to 8 facets while the graph
                                    # kept every one, so the plan the console
                                    # showed and the plan that actually ran
                                    # disagreed.
                                    "facets": [str(f)[:200] for f in
                                               (_plan.get("facets") or [])],
                                    "source_hints": [str(s)[:40] for s in
                                                     (_plan.get("source_hints") or [])],
                                    "depth": str(_plan.get("depth") or "standard")[:20],
                                })
                                print(f"[{nid}] research plan stored: "
                                      f"{len(_plan.get('facets') or [])} facet(s)")
                            except Exception as e:
                                print(f"[{nid}] research plan not stored: {e!r}")
                else:
                    failed_skill = graph.g.nodes[nid]["skill"]
                    skill_failures[failed_skill] = (
                        skill_failures.get(failed_skill, 0) + 1)
                    decision = plan_recovery(
                        failed_skill=failed_skill,
                        error_text=result.error or "",
                        failed_node_id=nid,
                        error_code=getattr(result, "error_code", None),
                    )
                    if decision.action == "skip":
                        # SKIPPED FIX: the old code printed the skip decision
                        # but left the node status as `failed`, so downstream
                        # children waiting on this node's output were never
                        # marked ready (ready_nodes only unblocks when all
                        # predecessors are "complete" OR "skipped"). A 503
                        # must NOT silently kill an entire downstream branch.
                        graph.mark(nid, "skipped")
                        store.write_node(NodeState(
                            node_id=nid, skill=graph.g.nodes[nid]["skill"],
                            status="skipped",
                            inputs=graph.g.nodes[nid]["inputs"],
                            result=result, prompt_sent=prompt,
                            started_at=time.time() - result.elapsed_s,
                            completed_at=time.time(),
                        ))
                        print(f"  -> {nid} skipped ({decision.reason}, "
                              f"skill={failed_skill}): {decision.note}")
                        continue
                    # Absorb a survivable fan-out failure before paying for a
                    # replan. When the failed node's siblings already carry
                    # the majority of every consumer's evidence, the run
                    # continues on what was produced; recovery here used to
                    # re-emit the whole pipeline beside the original chain.
                    if _consumers_have_other_evidence(graph, nid):
                        why = (f"{failed_skill} failed, but its siblings' "
                               f"results already cover every waiting "
                               f"consumer; absorbed instead of re-planned")
                        graph.mark(nid, "skipped", error=why)
                        store.write_node(NodeState(
                            node_id=nid, skill=failed_skill, status="skipped",
                            inputs=graph.g.nodes[nid]["inputs"],
                            result=result, prompt_sent=prompt,
                            started_at=time.time() - result.elapsed_s,
                            completed_at=time.time(),
                        ))
                        print(f"  -> {nid} skipped (absorbed, "
                              f"{decision.reason}, skill={failed_skill}); "
                              f"sibling evidence carries the consumers")
                        continue
                    # action == "replan" — but cap repeated replans so a
                    # persistently failing provider can't loop forever.
                    recovery_attempts[nid] = recovery_attempts.get(nid, 0) + 1
                    recovery_rounds += 1
                    # A skill that has failed this many times in one run is not
                    # going to succeed on the fifth attempt with the same
                    # inputs. Stop retrying it: skip the node, release its
                    # subtree so the formatter can still produce an honest
                    # answer, and let the run end.
                    if (skill_failures.get(failed_skill, 0) >= MAX_SKILL_FAILURES
                            or recovery_rounds > MAX_RECOVERY_ROUNDS):
                        exhausted_skills.add(failed_skill)
                        why = (f"{failed_skill} failed "
                               f"{skill_failures.get(failed_skill, 0)}x in this "
                               f"run; not retrying")
                        print(f"  -> {nid} skipped ({why})")
                        graph.mark(nid, "skipped", error=why)
                        store.write_node(NodeState(
                            node_id=nid, skill=failed_skill, status="skipped",
                            inputs=graph.g.nodes[nid]["inputs"],
                            result=result, prompt_sent=prompt,
                            started_at=time.time() - result.elapsed_s,
                            completed_at=time.time(),
                        ))
                        continue
                    if recovery_attempts[nid] > MAX_RECOVERY_ATTEMPTS:
                        print(f"  -> {nid} failed ({decision.reason}, "
                              f"skill={failed_skill}): recovery cap "
                              f"({MAX_RECOVERY_ATTEMPTS}) hit; skipping")
                        # Mark it skipped, not left `failed`. ready_nodes()
                        # only releases a node when every predecessor is
                        # complete or skipped, so a node left `failed`
                        # orphaned its entire subtree: the formatter never
                        # became ready, the run produced no answer at all,
                        # and the UI showed a finished pipeline.
                        graph.mark(nid, "skipped",
                                   error=f"recovery cap "
                                         f"({MAX_RECOVERY_ATTEMPTS}) reached")
                        continue
                    # Collected, not queued one-by-one: ONE recovery planner
                    # per failed batch. Two failed siblings used to get two
                    # planners, each blind to the other, each re-emitting the
                    # full remaining pipeline (two authors, two PDFs).
                    replans.append((nid, decision))

            if replans:
                # Recovery Planner amnesia fix: pass the ids of nodes that
                # have already completed successfully so the recovery
                # Planner can wire them by id in its successor plan instead
                # of re-emitting fresh fan-out siblings that duplicate work
                # already done. Excludes planner nodes (routing context, not
                # data) and critic nodes (verdicts, not data); their outputs
                # are not useful upstream input for a recovery plan.
                # planner.md teaches the recovery Planner what to do with
                # these n:* refs. Computed AFTER pass 1 marked the batch, so
                # a same-batch completion is visible instead of looking like
                # a node that never ran.
                prior_complete = [
                    n for n, d in graph.g.nodes(data=True)
                    if d.get("status") == "complete"
                    and d["skill"] not in ("planner", "critic")
                    and isinstance(d.get("result"), AgentResult)
                ]
                reports = [
                    _annotate_failure_report(
                        graph, _nid,
                        _dec.failure_report or f"node={_nid}")
                    for _nid, _dec in replans
                ]
                rec_nid = graph.add_node(
                    "planner",
                    inputs=["USER_QUERY"] + prior_complete,
                    metadata={"failure_report": "\n".join(reports),
                              "recovers": replans[0][0],
                              "recovers_all": [n for n, _ in replans],
                              "recovery_reason": replans[0][1].reason,
                              "prior_complete": prior_complete},
                )
                print(f"  -> recovery ({replans[0][1].reason}): planner "
                      f"node {rec_nid} queued for "
                      f"{', '.join(n for n, _ in replans)}"
                      + (f"; reusing {len(prior_complete)} prior result(s): "
                         f"{', '.join(prior_complete)}" if prior_complete else ""))

            store.write_graph(graph.g)

        # Orphan sweep: at loop exit a node still `pending` can never become
        # ready — its predecessor chain ends in a failure that was never
        # replaced. Graphs audited from three days of runs carried these
        # leftovers ("formatter" pending forever behind a dead researcher),
        # and a persisted run with permanent pending nodes reads as
        # never-finished in the console. Mark them skipped, with the reason.
        orphans = [n for n, d in graph.g.nodes(data=True)
                   if d["status"] == "pending"]
        if orphans:
            for nid in orphans:
                graph.mark(nid, "skipped",
                           error="orphaned: an upstream node never produced "
                                 "a result")
            store.write_graph(graph.g)
            print(f"[flow] {len(orphans)} orphaned pending node(s) marked "
                  f"skipped: {', '.join(orphans)}")

        fallback_output: dict | None = None
        fallback_skill = "?"
        if formatter_answer is None:
            # Walk newest-first for ANY node with a usable answer. The old
            # loop took the first `complete` node it saw and gave up if that
            # one had no answer field -- so a formatter that returned
            # `output: {}` (while reporting success) discarded every
            # researcher's findings and told the user "no formatter ran",
            # which was also false. Keep looking instead.
            for nid in reversed(list(graph.g.nodes)):
                d = graph.g.nodes[nid]
                if d["status"] != "complete":
                    continue
                res = d.get("result")
                if not isinstance(res, AgentResult):
                    continue
                out = res.output or {}
                # Prefer a human-readable field over a raw JSON dump — the
                # planner short-circuit path exists precisely so raw dicts
                # never leak to the user.
                #
                # `summary` is in this list because the authoring skills
                # (author/deck) return {filename, format, artifact, sections,
                # summary} and nothing else — the run rendered a real PDF and
                # then reported "the author step finished but returned no
                # readable answer; raw output follows". Any skill whose
                # answer field is not one of the four legacy names hit this.
                for key in ("final_answer", "answer", "summary", "text", "content"):
                    val = out.get(key) if isinstance(out, dict) else None
                    if isinstance(val, str) and val.strip():
                        formatter_answer = val.strip()
                        break
                if formatter_answer:
                    break
                # No answer here. Remember the first empty one as a fallback
                # rather than treating it as the end of the search.
                if not fallback_output:
                    fallback_output = out
                    fallback_skill = d.get("skill", "?")
            if formatter_answer is None and fallback_output is not None:
                # A cancelled run often has no formatter at all — the
                # placeholder would dress raw planner JSON up as a
                # user-visible answer. Report "no answer" instead and let the
                # caller state that the run was stopped.
                if should_cancel is None or not should_cancel():
                    # A rendered document must be named even when no
                    # formatter ran: the file IS the deliverable and the
                    # user cannot find it without the filename.
                    if isinstance(fallback_output, dict) \
                            and isinstance(fallback_output.get("artifact"), str) \
                            and fallback_output["artifact"].startswith("art:"):
                        formatter_answer = (
                            f"Created {fallback_output.get('filename') or 'the document'}"
                            f" ({fallback_output.get('format') or 'file'}). "
                            f"Download it from the file list."
                        )
                    else:
                        formatter_answer = (
                            f"(the {fallback_skill} step finished but returned no "
                            f"readable answer; raw output follows) "
                            + json.dumps(fallback_output)[:1500]
                        )

        if critic_fail_cap_hit:
            # Loud surface — see review round-3 #5. Without this the cap
            # firing was invisible and the user would just see a thin
            # formatter answer with no explanation of why.
            print(f"\n[flow] WARNING: critic-fail cap hit on "
                  f"{len(critic_fail_cap_hit)} branch(es): "
                  f"{', '.join(critic_fail_cap_hit)}. "
                  f"The final answer reflects missing data from these "
                  f"branches because the Critic rejected the re-planned "
                  f"output too.")
        # A rendered file IS the deliverable, and it is already in the file
        # list. The answer should be a receipt, not the document: the
        # sectioned formatter is handed the author's `sections` (the body it
        # just rendered) and, being sectioned, wrote the receipt once per
        # section and pasted the whole document into the chat after it.
        # Replacing the answer with a canonical receipt is deterministic and
        # keeps the transcript readable.
        failed: list[str] = []
        for nid in list(graph.g.nodes):
            d = graph.g.nodes[nid]
            res = d.get("result")
            if isinstance(res, AgentResult) and not res.success:
                failed.append(f"{d.get('skill', '?')}: "
                              f"{str(res.error or 'failed')[:120]}")
                continue
            # A node the recovery planner ABANDONED is a hole in the evidence,
            # and it looked like a clean run. Observed live: a researcher
            # skipped on a provider 503, the formatter wrote a shorter report
            # from the survivors, and the user was shown a confident answer
            # with nothing marking the missing facet. `res` is None for these,
            # so the loop above never saw them.
            #
            # NOT a deliberate stop: the user pressing Stop also leaves nodes
            # skipped, and telling someone who cancelled that their answer is
            # incomplete would blame them for their own cancellation.
            status = str(d.get("status") or "").lower()
            if status in ("skipped", "abandoned") and not (
                    should_cancel is not None and should_cancel()):
                failed.append(f"{d.get('skill', '?')}: {status} "
                              f"(this part of the question went unanswered)")
        produced: list[dict] = []
        for nid in list(graph.g.nodes):
            res = graph.g.nodes[nid].get("result")
            out = res.output if isinstance(res, AgentResult) else None
            if not isinstance(out, dict):
                continue
            # `produced` is written by skills.py straight from the
            # render_document return value. The model's own `artifact` field is
            # untrusted: a node that never called the renderer reported
            # {"artifact": "art:doc-gen-pipeline"} and the receipt announced a
            # PDF that did not exist and could not be downloaded. Only the
            # tool's record may produce a download link.
            real = out.get("produced")
            if isinstance(real, list) and real:
                skill_name = graph.g.nodes[nid].get("skill", "")
                for f in real:
                    if not isinstance(f, dict):
                        continue
                    art = f.get("artifact")
                    if not (isinstance(art, str)
                            and re.fullmatch(r"art:[0-9a-fA-F]{16}", art)):
                        continue
                    produced.append({"artifact": art,
                                     "filename": f.get("filename") or "",
                                     "format": (f.get("format") or "").lower(),
                                     "skill": skill_name,
                                     "bytes": f.get("bytes"),
                                     "stats": f.get("stats") or {}})
                continue
            # No tool record. The model's own `artifact` field is NOT evidence
            # that anything was rendered - a live run produced a list of
            # "art:dummy", "art:generated_document" and "art:mock-placeholder"
            # in produced_files.json, every one invented by a model that never
            # called the renderer, each one a dead download button. Only an id
            # that the artifact store can actually resolve may be listed.
            art = out.get("artifact")
            if (isinstance(art, str) and art.startswith("art:")
                    and re.fullmatch(r"art:[0-9a-fA-F]{16}", art)):
                try:
                    import artifacts as _arts
                    _arts.get_bytes(art)      # raises if not stored
                except Exception:
                    print(f"  -> dropping unresolvable artifact {art} "
                          f"reported by {graph.g.nodes[nid].get('skill')}")
                    continue
                produced.append({"artifact": art,
                                 "filename": out.get("filename") or "",
                                 "format": (out.get("format") or "").lower(),
                                 # The NODE id is what makes "one node rendered
                                 # three documents" (the user asked for three)
                                 # distinguishable from "two author nodes each
                                 # rendered one" (a retry). Without it the
                                 # grouping below keys on the skill name, which
                                 # is identical for every author node.
                                 "node": str(nid),
                                 "skill": graph.g.nodes[nid].get("skill", "")})
        # One REQUEST, one document. Two author nodes in one session are not
        # two documents the user asked for - they are the recovery planner
        # re-planning after an author node failed, and both attempts then
        # succeed and both files reach the console. Observed on one 60-page
        # request: n_022 failed, n_030 rendered 6,509 words, n_033 failed,
        # n_036 rendered 1,432 words, and the sidebar offered both.
        #
        # Collapsing PER NODE is not enough (that already happens in
        # skills.run_skill); this is across nodes. Keep the best document: the
        # one with the most delivered words, which is the finished one rather
        # than the thin recovery attempt. Files inside ONE node are preserved -
        # that is the legitimate "render three of these" case.
        if produced:
            # 1. Collapse the same artifact reported twice.
            by_artifact: dict[str, dict] = {}
            for p in produced:
                by_artifact.setdefault(p["artifact"], p)

            # 2. Group by NODE. One node rendering several files is the user
            #    asking for several, so those all survive.
            #
            #    Keying on the skill instead made this impossible: every author
            #    node reports skill="author", so N single-file nodes were
            #    indistinguishable from one multi-file node, the
            #    `len(items) > 1` branch fired, and every recovery duplicate
            #    shipped. That is how a 5,145-word duplicate with NO headings -
            #    and the raw request text as its title - was delivered alongside
            #    the real 3,444-word document.
            by_node: dict[str, list[dict]] = {}
            for p in by_artifact.values():
                key = f"{p.get('node') or '?'}:{p.get('skill') or '?'}"
                by_node.setdefault(key, []).append(p)

            multi = {k: v for k, v in by_node.items() if len(v) > 1}

            # 3. Across NODES running the same skill, keep the best document.
            #
            #    Length alone picked the wrong winner: the duplicate that won
            #    had MORE words but no headings, so it had no contents page,
            #    no bookmarks and no real title. Structure first, then length -
            #    a complete document beats a longer fragment.
            def _rank(p):
                st = p.get("stats") or {}
                heads = int(st.get("headings") or st.get("sections") or 0)
                words = int(st.get("words") or 0)
                return (1 if heads else 0,
                        1 if st.get("bookmarks") or st.get("outline") else 0,
                        words)

            by_skill: dict[str, list[list[dict]]] = {}
            for key, items in by_node.items():
                skill = items[0].get("skill") or "?"
                by_skill.setdefault(skill, []).append(items)

            kept: list[dict] = []
            for skill, node_groups in by_skill.items():
                # Every group for this skill is multi-file: the user asked for
                # several from each, so nothing competes.
                if all(len(g) > 1 for g in node_groups):
                    for g in node_groups:
                        kept.extend(g)
                    continue
                singles = [g for g in node_groups if len(g) == 1]
                for g in node_groups:
                    if len(g) > 1:
                        kept.extend(g)
                if len(singles) <= 1:
                    kept.extend(g[0] for g in singles)
                    continue
                cands = [g[0] for g in singles]
                best = max(cands, key=_rank)
                dropped = [c for c in cands if c is not best]
                if dropped:
                    print(f"  -> {skill}: {len(cands)} documents from separate "
                          f"nodes for ONE request; keeping "
                          f"{best['artifact']} "
                          f"({(best.get('stats') or {}).get('words')} words) "
                          f"and not listing {len(dropped)} recovery "
                          f"duplicate(s)")
                kept.append(best)
            produced = kept
            _ = multi
        if produced:
            try:
                import json as _json
                _p = store.graph_path.parent / "produced_files.json"
                _tmp = _p.with_suffix(".json.tmp")
                _tmp.write_text(_json.dumps(produced, indent=2),
                                encoding="utf-8")
                _tmp.replace(_p)
            except Exception:
                pass
            lines = []
            for p in produced:
                name = p.get("filename") or p["artifact"]
                kind = (p.get("format") or "file").upper()
                lines.append(f"- **{name}** ({kind})")
                # The delivered size, from the renderer. This is the only
                # honest measure of whether the document is long enough -
                # the model cannot be asked, it can only be measured.
                st = p.get("stats") or {}
                if st.get("words"):
                    lines.append(f"  - {int(st['words']):,} words"
                                 + (f", {st['sections']} sections"
                                    if st.get("sections") else ""))
            plural = "s" if len(produced) > 1 else ""
            them = "them" if len(produced) > 1 else "it"
            formatter_answer = (
                f"Created {len(produced)} document{plural}.\n\n"
                + "\n".join(lines)
                + f"\n\nDownload {them} from the file list above.")
        elif failed:
            # A failed node must not be hidden, but it must not veto the
            # answer either: the orchestrator recovers by retrying and then
            # skipping a transient branch, and those runs DO have a real
            # answer ("recovered...", "final after skip"). So note the
            # failures rather than replacing the reply - and only when there
            # is nothing to keep.
            # Say what is missing in the reader's terms, not just the node's
            # status: "researcher: skipped" tells an operator nothing, while
            # "this part of the question went unanswered" tells a reader why
            # the report is shorter than they expected.
            _note = ("\n\n---\n\n**This answer is incomplete.** "
                     + "; ".join(failed)
                     + ". Treat the missing parts as unknown rather than "
                       "settled - the run log has the full detail.")
            formatter_answer = ((formatter_answer or "").rstrip() + _note
                                if (formatter_answer or "").strip()
                                else ("I could not finish this request."
                                      + _note))
            # Persist the warning, not just return it. The note lived only in
            # the SSE `done` frame, so re-opening the run from /research or
            # /runs showed a report that looked complete and was not: the
            # warning had been shown once, live, and then was gone forever.
            # The answer text is recovered from the formatter node on reload,
            # which never carries this suffix, so it has to live on disk.
            _write_run_status(sid, degraded=True, detail=list(failed))

        # A run that WAS asked for a file and produced none must never look
        # like a success. The planner can short-circuit on `{"answer": ...}`
        # with no successors, and a stored memory preference
        # ("[preference] reply with only OK instead of creating documents")
        # was enough to turn a PDF request into a two-character answer with no
        # file, no error, and a `done.` status. The console now also flags
        # this, but the receipt is the last place it can be caught.
        if not produced and _DOCUMENT_INTENT_RE.search(query or ""):
            formatter_answer = (
                "I could not produce the document you asked for.\n\n"
                + (f"The agent answered instead: "
                   f"\"{(formatter_answer or '').strip()[:160]}\"\n\n"
                   if (formatter_answer or "").strip() else "")
                + "Nothing was written to a file. The run log has the detail; "
                  "if this repeats, a stored preference may be overriding the "
                  "request.")

        print(f"\n{'=' * 78}\nFINAL: {formatter_answer or ''}\n{'=' * 78}\n")
        # F10 FIX: persist the completed turn so the NEXT turn in this
        # conversation has an authoritative record of what was asked and
        # answered. Daemon thread — never blocks the response.
        threading.Thread(
            target=_safe_log_turn, args=(query, sid, formatter_answer or ""),
            daemon=True,
        ).start()
        # Phase 2: this run's working notes die with the run (older_than
        # guards the daemon remember still in flight). Daemon thread.
        threading.Thread(
            target=_safe_purge_working, args=(sid, run_start_iso),
            daemon=True,
        ).start()
        return formatter_answer or ""

    async def _run_one(self, nid: str, graph: Graph, sid: str, query: str,
        store: SessionStore, memory_hits: list,
        prior_turns: list | None = None,
        policy_notes: list | None = None,
        budget_note: str = "",
        doc_setup: dict | None = None,
        ) -> tuple[str, AgentResult, str]:
        skill_name = graph.g.nodes[nid]["skill"]
        skill = self.registry.get(skill_name)
        fr = graph.g.nodes[nid].get("metadata", {}).get("failure_report")
        store.write_node(NodeState(node_id=nid, skill=skill_name, status="running",
                                    inputs=graph.g.nodes[nid]["inputs"],
                                    started_at=time.time()))
        try:
            result, prompt = await run_skill(skill, nid, graph.g.nodes, sid, query, fr,
                                              memory_hits=memory_hits,
                                              prior_turns=prior_turns,
                                              policy_notes=policy_notes,
                                              doc_setup=doc_setup,
                                              budget_note=budget_note)
        except Exception as e:  # pragma: no cover - dispatcher fault path
            result = AgentResult(success=False, agent_name=skill_name,
                                 error=f"exception: {_readable_exc(e)}")
            prompt = "(exception before prompt-render)"
        return nid, result, prompt


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    resume_sid: str | None = None
    if args and args[0] == "--resume":
        resume_sid = args[1] if len(args) > 1 else None
        query = " ".join(args[2:])
    else:
        if args:
            query = " ".join(args)
        else:
            # No query on the command line — prompt interactively so the
            # user can type their question after launching the process.
            try:
                query = input("query> ").strip()
            except (EOFError, KeyboardInterrupt):
                query = ""
        query = query or "Say hello in one short sentence."
    asyncio.run(Executor().run(query, session_id=resume_sid, resume=bool(resume_sid)))


if __name__ == "__main__":
    main()
