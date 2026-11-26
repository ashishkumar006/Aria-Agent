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


def test_one_based_positions_do_not_self_loop():
    """A planner that numbers siblings from 1 ('1' = the first node) must not
    produce self-loops. Resolved 0-based, '1' would be the SECOND node, so the
    author would wait on itself and the whole tail (author/action/formatter)
    would never become ready — the run would silently finish having only done
    research (observed live: a competitive-analysis run skipped author/action/
    formatter and emailed nothing)."""
    g = Graph()
    planner = g.add_node("planner", inputs=["USER_QUERY"])
    g.mark(planner, "complete")
    result = AgentResult(
        success=True, agent_name="planner",
        successors=[
            NodeSpec(skill="researcher", inputs=["USER_QUERY"]),   # 1st
            NodeSpec(skill="author", inputs=["1"]),                # -> 1st
            NodeSpec(skill="action", inputs=["2"]),                # -> 2nd (author)
            NodeSpec(skill="formatter", inputs=["1", "2", "3"]),   # -> 1st,2nd,3rd
        ])
    ids = g.extend_from(planner, result, registry=_registry())
    researcher, author, action, formatter = ids
    assert g.g.nodes[author]["inputs"] == [researcher]
    assert g.g.nodes[action]["inputs"] == [author]
    assert g.g.nodes[formatter]["inputs"] == [researcher, author, action]
    # No node waits on itself — the invariant the bug violated.
    for nid in ids:
        assert nid not in g.g.nodes[nid]["inputs"], f"{nid} waits on itself"
