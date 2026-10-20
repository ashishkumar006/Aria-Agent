"""A Research run must answer in the transcript, never render a file.

Research and Authoring share one planner and one skill registry, so the planner
can reach for `author` on any run. Observed live on a delegated research
request ("...give me a proper report"): the plan was
`planner -> researchers -> author -> formatter`, the author rendered a PDF, and
the answer pointed at a download instead of giving the report - while the
Research page's own contract is the report inline (session s8-c7162a1f).
"""
import pytest

flow = pytest.importorskip("flow")


def _plan_with_author():
    g = flow.Graph()
    r1 = g.add_node("researcher", inputs=["USER_QUERY"])
    r2 = g.add_node("researcher", inputs=["USER_QUERY"])
    a = g.add_node("author", inputs=[r1, r2])
    f = g.add_node("formatter", inputs=[a])
    return g, r1, r2, a, f


def test_an_author_between_research_and_formatter_is_removed():
    g, r1, r2, a, f = _plan_with_author()
    changed = flow.ensure_research_is_text_only(g, session_id="s8-test-1")
    assert changed == [a], changed
    assert a not in g.g.nodes
    # The formatter now reads the evidence the author was given.
    assert g.g.nodes[f]["inputs"] == [r1, r2]
    assert set(g.g.predecessors(f)) == {r1, r2}


def test_every_consumer_of_the_author_is_rewired():
    g = flow.Graph()
    r = g.add_node("researcher", inputs=["USER_QUERY"])
    a = g.add_node("deck", inputs=[r])
    f1 = g.add_node("formatter", inputs=[a])
    f2 = g.add_node("formatter", inputs=[a, r])
    flow.ensure_research_is_text_only(g, session_id="s8-test-2")
    assert g.g.nodes[f1]["inputs"] == [r]
    assert g.g.nodes[f2]["inputs"] == [r]  # r not duplicated


def test_a_terminal_author_becomes_the_formatter():
    """No formatter in the plan: the author was going to be the last word, so
    its slot must write prose instead of rendering."""
    g = flow.Graph()
    r = g.add_node("researcher", inputs=["USER_QUERY"])
    a = g.add_node("author", inputs=[r])
    changed = flow.ensure_research_is_text_only(g, session_id="s8-test-3")
    assert changed == [a]
    assert a in g.g.nodes
    assert g.g.nodes[a]["skill"] == "formatter"
    assert g.g.nodes[a]["inputs"] == [r]


def test_the_rewrite_is_idempotent_per_session():
    g, *_ = _plan_with_author()
    assert flow.ensure_research_is_text_only(g, session_id="s8-test-4")
    assert flow.ensure_research_is_text_only(g, session_id="s8-test-4") == []


def test_a_clean_research_plan_is_untouched():
    g = flow.Graph()
    r1 = g.add_node("researcher", inputs=["USER_QUERY"])
    r2 = g.add_node("researcher", inputs=["USER_QUERY"])
    f = g.add_node("formatter", inputs=[r1, r2])
    assert flow.ensure_research_is_text_only(g, session_id="s8-test-5") == []
    assert g.g.nodes[f]["inputs"] == [r1, r2]
