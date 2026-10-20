"""A second planner node must not splice a second researcher fan-out.

Measured on a real 60-page run: 12 researcher nodes for 6 facets, nodes 1-6
repeated verbatim as 7-12. `ensure_authoring_research` ran once per COMPLETED
PLANNER NODE, so a second planner node - a recovery replan, or the sectioned
expansion - ran it again and added the whole fan-out a second time. Roughly
half of every authoring run's research spend produced nothing.
"""
import pytest

flow = pytest.importorskip("flow")


def _graph():
    """planner -> author -> formatter, with inputs carrying the wiring."""
    g = flow.Graph()
    p = g.add_node("planner", ["USER_QUERY"], {"goal": "write a treatise"})
    a = g.add_node("author", [p], {"goal": "write it"})
    g.add_node("formatter", [a], {"goal": "format it"})
    return g


def _researchers(g):
    return [n for n in g.g.nodes if g.g.nodes[n].get("skill") == "researcher"]


def test_a_second_planner_does_not_double_the_fan_out():
    session = "test-no-double-research"
    flow._AUTHORING_RESEARCH_SESSIONS.discard(session)
    try:
        g1 = _graph()
        first = flow.ensure_authoring_research(g1, "roman aqueducts",
                                               session_id=session)
        assert first, "no research was spliced at all"
        n1 = len(_researchers(g1))
        assert n1 > 0

        # A second planner node appears - a recovery replan, or the sectioned
        # expansion - and completes over a fresh author node.
        g2 = _graph()
        second = flow.ensure_authoring_research(g2, "roman aqueducts",
                                                session_id=session)
        assert second == [], f"spliced a SECOND fan-out: {second}"
        assert not _researchers(g2), (
            "a second planner node added researchers for the same session; "
            "that is the duplicate run")
    finally:
        flow._AUTHORING_RESEARCH_SESSIONS.discard(session)


def test_a_different_session_still_gets_its_own_fan_out():
    for s in ("test-s1", "test-s2"):
        flow._AUTHORING_RESEARCH_SESSIONS.discard(s)
    try:
        a = flow.ensure_authoring_research(_graph(), "x", session_id="test-s1")
        b = flow.ensure_authoring_research(_graph(), "x", session_id="test-s2")
        assert a and b, (
            "the guard is global rather than per-session; a second request "
            "would never get any research at all")
    finally:
        for s in ("test-s1", "test-s2"):
            flow._AUTHORING_RESEARCH_SESSIONS.discard(s)


def test_an_author_that_is_already_grounded_is_left_alone():
    session = "test-grounded"
    flow._AUTHORING_RESEARCH_SESSIONS.discard(session)
    try:
        # The researcher must be an ANCESTOR of the author - the author reads
        # from it. A researcher that depends on the author does not ground it.
        g = flow.Graph()
        p = g.add_node("planner", ["USER_QUERY"], {"goal": "write"})
        r = g.add_node("researcher", [p], {"goal": "already researched"})
        a = g.add_node("author", [r], {"goal": "write it"})
        g.add_node("formatter", [a], {"goal": "format it"})
        out = flow.ensure_authoring_research(g, "x", session_id=session)
        assert out == [], "added research above an author that already has some"
    finally:
        flow._AUTHORING_RESEARCH_SESSIONS.discard(session)