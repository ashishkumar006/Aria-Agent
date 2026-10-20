"""A recovery planner must not re-plan work that already completed.

Measured: one author failure queued a recovery planner node, and the whole
research fleet ran a second time. Nodes 1-6 came back verbatim as 7-12, with
identical word counts - ~50% of every authoring run's research spend bought
nothing.

`flow.py` already collects `prior_complete` (the ids that finished) and passes
them to the recovery planner so it can wire to them by id instead of
re-emitting them. That only works if the planner prompt actually says so, so
this guards both halves: the prompt must teach the reuse, and the graph must
not already contain a duplicate fleet before the recovery planner runs.
"""
import pytest

flow = pytest.importorskip("flow")


def test_the_planner_prompt_teaches_recovery_to_reuse_completed_nodes():
    import skills
    prompt = (skills.ROOT / "prompts" / "planner.md").read_text(
        encoding="utf-8")
    low = prompt.lower()
    assert "recovery" in low, (
        "planner.md has no recovery section; a recovery planner with no "
        "guidance re-emits the whole graph and the entire research fleet runs "
        "a second time")
    assert "already-finished" in low or "already finished" in low or \
        "already-complete" in low or "already complete" in low, (
        "the recovery section must say the prior node ids are finished work")
    assert "do not re-emit" in low, (
        "the recovery section must forbid re-emitting a node for work an "
        "existing id already represents")


def test_the_stored_plan_is_not_truncated_to_eight_facets():
    """The plan the console shows must match the plan that ran."""
    import inspect
    src = inspect.getsource(flow)
    # the write_plan call that stores what the console displays
    assert '("facets") or [])][:8]' not in src, (
        "facets are still sliced to 8 when stored, so the console plan and the "
        "graph disagree")
    assert '("source_hints") or [])][:8]' not in src, (
        "source_hints are still sliced to 8 when stored")


def test_authoring_research_is_spliced_once_per_session():
    """A recovery planner completing must not add a second fan-out."""
    s = "test-recovery-fanout"
    flow._AUTHORING_RESEARCH_SESSIONS.discard(s)
    try:
        g = flow.Graph()
        p = g.add_node("planner", ["USER_QUERY"], {"goal": "write"})
        a = g.add_node("author", [p], {"goal": "write it"})
        g.add_node("formatter", [a], {"goal": "format"})
        first = flow.ensure_authoring_research(g, "roman aqueducts",
                                               session_id=s)
        assert first, "no research spliced on the first pass"

        # the recovery planner completes over the same graph
        rec = g.add_node("planner", [p], {"goal": "recover"})
        again = flow.ensure_authoring_research(g, "roman aqueducts",
                                               session_id=s)
        assert again == [], f"recovery planner added {again}"
        assert rec in g.g.nodes
    finally:
        flow._AUTHORING_RESEARCH_SESSIONS.discard(s)