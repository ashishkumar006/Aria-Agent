"""A Planner that numbers its inputs by sibling POSITION must be understood.

Live: a planner emitted

    {"skill": "researcher", ...}
    {"skill": "formatter", "inputs": ["0"]}

The researcher ran (112s, real findings) but the formatter was wired to the
PLANNER, because "0" matched no id and `extend_from` falls back to the parent.
The formatter's INPUTS were the planner's node list, so it answered "upstream
research findings were not provided" and the user got an apology instead of the
report. A bare position that is in range names a sibling unambiguously.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flow import Graph
from schemas import AgentResult, NodeSpec
from skills import SkillRegistry


def _registry():
    return SkillRegistry()


def test_bare_sibling_position_resolves_to_the_sibling():
    g = Graph()
    planner = g.add_node("planner", inputs=["USER_QUERY"])
    g.mark(planner, "complete")
    result = AgentResult(
        success=True, agent_name="planner",
        successors=[
            NodeSpec(skill="researcher", inputs=["USER_QUERY"]),
            NodeSpec(skill="formatter", inputs=["0"]),
        ])
    ids = g.extend_from(planner, result, registry=_registry())
    researcher, formatter = ids[0], ids[1]
    assert g.g.nodes[formatter]["inputs"] == [researcher]
    assert g.g.has_edge(researcher, formatter)
    assert not g.g.has_edge(planner, formatter)


def test_n_prefixed_position_is_not_treated_as_a_sibling_index():
    """`n:<int>` is a real graph id and must keep its meaning (recovery plans
    rewrite an existing node's inputs this way)."""
    g = Graph()
    planner = g.add_node("planner", inputs=["USER_QUERY"])
    g.mark(planner, "complete")
    result = AgentResult(
        success=True, agent_name="planner",
        successors=[NodeSpec(skill="formatter", inputs=[f"n:{planner[2:]}"])])
    ids = g.extend_from(planner, result, registry=_registry())
    # `n:<planner index>` is not a valid existing id here, so it falls back to
    # the planner - the point is only that it is NOT read as position 0 of a
    # node list that has no position 0 reference.
    assert g.g.nodes[ids[0]]["inputs"] == [planner]
