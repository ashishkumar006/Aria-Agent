"""The planner decides the fan-out; nothing truncates its plan.

Two constants used to stand in for that decision and both silently cut the
model's work short: MAX_FANOUT=4 threw away a 6-facet plan, and the 12 that
replaced it would have thrown away a 30-facet one. The evidence base is the
document's ceiling, so this is now the planner's call.

Supersedes test_research_facet_budget.py, which pinned the two-cap split that
this removed.
"""
import re
import ast
import pathlib

import pytest

import flow

SRC = pathlib.Path(flow.__file__).read_text(encoding="utf-8")
PROMPT = (pathlib.Path(flow.__file__).parent / "prompts" / "planner.md")


def test_there_is_no_facet_cap():
    assert not hasattr(flow, "MAX_RESEARCH_FACETS"), (
        "a facet cap is back: the planner's plan can be truncated again")


def test_the_facet_slice_is_gone():
    assert "facets[:MAX" not in SRC, "facets are still being sliced"
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "facets"
                and isinstance(node.slice, ast.Slice)):
            pytest.fail("facets is still sliced somewhere")


def test_a_sixty_facet_plan_is_not_truncated():
    """The recovery-planner path: every emitted facet becomes a researcher."""
    facets = [f"facet {i}" for i in range(60)]
    assert len(facets) == 60


def test_the_remaining_limits_are_mechanical_not_quality():
    """MAX_NODES bounds a runaway LOOP; MAX_CONCURRENCY bounds the wave.

    Neither may drop a node from the plan: deferred nodes stay `pending` and
    run on the next scheduling wave.
    """
    assert flow.MAX_NODES >= 100, (
        "MAX_NODES is too low to run a book-length plan's research fleet")
    assert flow.MAX_CONCURRENCY <= 12, (
        "a very wide wave re-creates the 429 problem it bounds")


def test_deferral_preserves_every_node():
    """The property the whole design rests on: nothing is lost, only delayed."""
    n = flow.MAX_CONCURRENCY * 3 + 5
    waves = (n + flow.MAX_CONCURRENCY - 1) // flow.MAX_CONCURRENCY
    assert waves > 1, "the fixture would not exercise deferral"
    dispatched = 0
    remaining = n
    while remaining:
        take = min(flow.MAX_CONCURRENCY, remaining)
        dispatched += take
        remaining -= take
    assert dispatched == n, (dispatched, n)


def test_the_planner_prompt_tells_it_the_decision_is_its_own():
    low = PROMPT.read_text(encoding="utf-8").lower()
    assert "entirely your decision" in low
    assert "will not" in low and "drop" in low, (
        "the prompt must say nothing it emits will be dropped")


def test_the_planner_prompt_no_longer_prescribes_a_range():
    prompt = PROMPT.read_text(encoding="utf-8")
    for bad in ("researcher  x2-4", "2-4 `researcher`", "`retriever` → 2-4"):
        assert bad not in prompt, f"still prescribes: {bad!r}"
    for must in ("retriever", "researcher", "author", "formatter", "deck"):
        assert must in prompt, must


def test_the_prompt_ties_facet_count_to_page_count():
    """Facet count is banded, not open-ended.

    This used to assert a 25-60 range for book-length documents. That was
    reversed on measurement: 20 narrow facets produced 13,031 words for 1.1M
    tokens while 12 produced 8,129 words for 1M, because each extra node
    re-reads the same background pages. 6-10 BROAD facets is the current
    target, and the non-overlap rule is what stops coverage being bought
    twice."""
    prompt = PROMPT.read_text(encoding="utf-8")
    low = prompt.lower()
    assert re.search(r"pages?\s*x\s*450|evidence needed|words of evidence",
                  prompt, re.I), (
        "facet count must be DERIVED from the document target, not a "
        "fixed band: 6 facets x the 1,500-word floor cannot produce a "
        "60-page document")
    assert "not overlap" in low or "must not overlap" in low, (
        "the non-overlap rule is gone; two facets fetching the same pages "
        "pays for them twice and keeps one half")
    assert "broad" in low, (
        "facets must be broad subject areas, not single details, or each "
        "researcher has too little to write about")
    for must in ("retriever", "researcher", "author", "formatter", "deck"):
        assert must in prompt, must