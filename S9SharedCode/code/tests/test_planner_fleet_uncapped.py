"""The fleet is the planner's decision, not a number in an operator note.

Two defects lived together here. `_research_depth_note` and
`_authoring_run_note` named a researcher COUNT and called it "a hard budget,
not a hint", while `planner.md` promised "no ceiling you are being trimmed to" —
both sentences arrived in the same prompt, and live runs topped out at exactly
the number in the prose. And `NodeSpec` declared only `skill`/`inputs`/
`metadata`, so the `goal` and `depth` the planner prompt tells the model to
write on each node were discarded by Pydantic in silence: `metadata` came back
empty, `researcher` found no `question`, and six facet-scoped researchers each
received the same run-level USER_QUERY.
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import flow  # noqa: E203,E402
import agent_server as srv  # noqa: E402
from schemas import NodeSpec  # noqa: E402

# "at most 3 researchers", "up to 6 researcher nodes", "exactly one researcher"
_COUNT_CEILING = re.compile(
    r"(at most|no more than|up to|maximum of|exactly)\s+"
    r"(one|two|three|four|five|six|seven|eight|\d+)\s+"
    r"(researcher|research nodes|facet)", re.I)


def test_nodespec_keeps_what_the_planner_prompt_asks_for():
    """planner.md tells the model to write `goal` and `depth` on the node."""
    spec = NodeSpec.model_validate({
        "skill": "researcher", "goal": "Grading bias in RAG evals",
        "depth": "deep", "inputs": ["USER_QUERY"]})
    assert spec.goal == "Grading bias in RAG evals"
    assert spec.depth == "deep"
    meta = spec.merged()
    # `question` is the only channel the researcher skill reads for scoping.
    assert meta["question"] == "Grading bias in RAG evals", meta
    assert meta["depth"] == "deep", meta


def test_nodespec_explicit_metadata_still_wins():
    spec = NodeSpec.model_validate({
        "skill": "researcher", "goal": "top level",
        "metadata": {"question": "explicit", "label": "research2"}})
    meta = spec.merged()
    assert meta["question"] == "explicit"
    assert meta["label"] == "research2"


@pytest.mark.parametrize("depth", ["quick", "standard", "deep", "nonsense", ""])
def test_research_depth_note_names_no_researcher_count(depth):
    note = srv._research_depth_note(depth)
    hit = _COUNT_CEILING.search(note)
    assert not hit, f"depth={depth!r} still caps the fleet: {hit.group(0)!r}"
    assert "NOT a node count" in note


@pytest.mark.parametrize("query", [
    "a one page memo about our Q3 hiring plan",
    "write a comprehensive 60 page treatise on retrieval augmented generation",
    "a 10 page overview of vector databases",
    "summarise this",
])
def test_authoring_note_names_no_researcher_count(query):
    note = srv._authoring_run_note(query)
    hit = _COUNT_CEILING.search(note)
    assert not hit, f"authoring note still caps the fleet: {hit.group(0)!r}"


def test_authoring_note_tells_the_planner_to_scope_each_researcher():
    note = srv._authoring_run_note(
        "write a comprehensive treatise on index selection for vector search")
    assert "distinct" in note.lower()
    assert "goal" in note, "the facet must be named on the node it belongs to"


def test_planner_promise_and_operator_note_agree():
    """The contradiction was the bug: one side said 'no ceiling', the other
    said 'hard budget', and both ride the same prompt (flow.py appends them
    together).

    The fix is not to pick a side. The prompt now NAMES the mechanics limits —
    the node stop, the wave width, the section ceiling — and still tells the
    planner nothing trims its plan, which is true of all three: they bound
    scheduling and rendering, never the size of the fleet it asked for. An
    absolute claim returning here means the two halves have drifted apart
    again, and this time the test says which claim is allowed.
    """
    text = (ROOT / "prompts" / "planner.md").read_text(encoding="utf-8")
    assert "no ceiling you are being trimmed to" not in text, (
        "the prompt is back to claiming there are no limits")
    assert str(flow.MAX_NODES) in text, "the node stop must be stated"
    assert str(flow.MAX_CONCURRENCY) in text, "the wave width must be stated"
    assert "160 section" in text, "the section ceiling must be stated"
    assert "drops nothing" in text or "loses nothing" in text, (
        "naming a limit must not read as a trim: say which of them lose work")
    for depth in ("quick", "standard", "deep"):
        assert not _COUNT_CEILING.search(srv._research_depth_note(depth))


def test_max_nodes_is_documented_as_a_runaway_guard():
    """MAX_NODES bounds a graph the recovery planner could grow; it is not a
    limit on the fleet the planner asked for. The cap check must also test the
    dispatched batch, or a 9-node plan with MAX_CONCURRENCY=8 could skip
    everything and spin."""
    src = (ROOT / "flow.py").read_text(encoding="utf-8")
    assert "if executed_count + len(batch) > MAX_NODES" in src, (
        "the node cap is being tested against the full ready set again")
    assert "if executed_count + len(ready) > MAX_NODES" not in src
