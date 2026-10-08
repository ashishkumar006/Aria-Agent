"""Authoring must run the Research pipeline, then add the document.

The real run this fixes planned `planner -> author -> formatter`: the author
node saw only USER_QUERY, gathered nothing, and wrote the document from the
model's own memory. The Research section gets retriever + researcher fan-out
because the planner asks it to; authoring got the bare path because that is
the path of least resistance. Now it is enforced in code.
"""
import flow
import pytest


def _graph_with_author(inputs):
    g = flow.Graph()
    p = g.add_node("planner", inputs=["USER_QUERY"])
    a = g.add_node("author", inputs=list(inputs))
    f = g.add_node("formatter", inputs=[a])
    g.g.add_edge(p, a)
    return g, a, f


def test_an_ungrounded_author_node_gets_the_research_chain():
    g, a, f = _graph_with_author(["USER_QUERY"])
    added = flow.ensure_authoring_research(g, "write a report on X")

    skills = [g.g.nodes[n]["skill"] for n in added]
    assert skills.count("retriever") == 1, skills
    assert skills.count("researcher") >= 2, \
        "one researcher is the single-pass failure mode research avoids"

    # The author node now READS the evidence instead of just the query.
    inputs = g.g.nodes[a]["inputs"]
    assert "USER_QUERY" in inputs
    for n in added:
        if g.g.nodes[n]["skill"] == "researcher":
            assert n in inputs, "a researcher was added but not wired in"
            assert g.g.has_edge(n, a), "the author does not wait for it"
    assert any(g.g.nodes[n]["skill"] == "retriever" for n in added)


def test_each_researcher_gets_its_own_question():
    """One researcher handed the whole question answers one question. The
    fan-out exists so each worker has a scoped QUESTION block."""
    g, a, _f = _graph_with_author(["USER_QUERY"])
    flow.ensure_authoring_research(g, "write a report on X")
    workers = [n for n in g.g.nodes if g.g.nodes[n]["skill"] == "researcher"]
    questions = [g.g.nodes[n].get("metadata", {}).get("question")
                 for n in workers]
    assert all(q for q in questions), questions
    assert len(set(questions)) == len(questions), "duplicate facet questions"


def test_planner_facets_are_reused_when_present():
    """The rewrite reinforces the plan; it does not invent a new topic."""
    g = flow.Graph()
    p = g.add_node("planner", inputs=["USER_QUERY"])
    g.g.nodes[p]["result"] = {"output": {"research_plan": {
        "topic": "Bluebook placement rules",
        "facets": ["citation formats", "rule hierarchy", "court documents"],
    }}}
    a = g.add_node("author", inputs=["USER_QUERY"])
    g.g.add_edge(p, a)

    flow.ensure_authoring_research(g, "write a report")
    workers = [n for n in g.g.nodes if g.g.nodes[n]["skill"] == "researcher"]
    questions = [g.g.nodes[n]["metadata"]["question"] for n in workers]
    assert set(questions) == {"citation formats", "rule hierarchy",
                              "court documents"}, questions


def test_an_already_grounded_author_node_is_left_alone():
    """Do not stack a second research chain on a plan that has one."""
    g = flow.Graph()
    p = g.add_node("planner", inputs=["USER_QUERY"])
    r = g.add_node("retriever", inputs=["USER_QUERY"])
    w1 = g.add_node("researcher", inputs=[r], metadata={"question": "a"})
    a = g.add_node("author", inputs=[w1, "USER_QUERY"])
    g.g.add_edge(p, r)
    g.g.add_edge(r, w1)
    g.g.add_edge(w1, a)

    added = flow.ensure_authoring_research(g, "write a report")
    assert added == [], "a grounded author node was given more research"
    assert g.g.nodes[a]["inputs"] == [w1, "USER_QUERY"]


def test_deck_is_covered_too():
    g = flow.Graph()
    p = g.add_node("planner", inputs=["USER_QUERY"])
    d = g.add_node("deck", inputs=["USER_QUERY"])
    g.g.add_edge(p, d)
    added = flow.ensure_authoring_research(g, "make a deck on X")
    assert [g.g.nodes[n]["skill"] for n in added].count("researcher") >= 2


def test_a_research_only_run_is_untouched():
    g = flow.Graph()
    p = g.add_node("planner", inputs=["USER_QUERY"])
    r = g.add_node("retriever", inputs=["USER_QUERY"])
    w = g.add_node("researcher", inputs=[r], metadata={"question": "x"})
    f = g.add_node("formatter", inputs=[w])
    g.g.add_edge(p, r)
    g.g.add_edge(r, w)
    g.g.add_edge(w, f)
    before = len(g.g.nodes)
    assert flow.ensure_authoring_research(g, "what is X") == []
    assert len(g.g.nodes) == before


def test_a_fallback_facet_is_a_research_question_not_a_task():
    """The worker is asked to RESEARCH a subject, not to obey an order. The
    instruction prefix has to come off or every facet reads like a to-do."""
    facets = flow._fallback_facets(
        "write a 6 page report on the Bluebook placement rules")
    assert len(facets) >= 2, "a single fallback facet is the single-pass bug"
    for f in facets:
        assert not f.lower().startswith(("write", "make", "create", "build",
                                         "generate", "draft", "produce"))
        assert "Bluebook placement rules" in f
    assert len(set(facets)) == len(facets)


def test_a_bare_topic_still_produces_usable_facets():
    facets = flow._fallback_facets("Bluebook placement rules")
    assert len(facets) >= 2
    assert all(f.strip() for f in facets)


def test_the_tool_call_is_taught_before_the_receipt_json():
    """The output schema at the end of the prompt is a lure: the model writes
    that JSON instead of calling the tool, which produces no file at all
    while looking like success. The order has to be stated up front."""
    for name in ("author.md", "deck.md"):
        md = open(f"prompts/{name}", encoding="utf-8").read()
        first_json = md.index('"filename"')
        first_tool = md.index("render_document")
        assert first_tool < first_json, \
            f"{name}: the receipt schema appears before the tool is introduced"
        assert "until `render_document` has returned" in md, \
            f"{name}: nothing states that the tool call comes first"
        assert "once `render_document` has already returned" in md, \
            f"{name}: the receipt is not marked as coming after the render"


def test_the_planner_prompt_says_the_same_thing():
    """The prompt and the enforcement must not disagree, or the model spends
    its budget planning a shape the code then rewrites."""
    md = open("prompts/planner.md", encoding="utf-8").read()
    assert "DOCUMENT REQUESTS ARE RESEARCH REQUESTS" in md
    assert "NEVER emit `author`/`deck` straight from `USER_QUERY`" in md


def test_the_author_prompt_demands_grounding_in_its_inputs():
    md = open("prompts/author.md", encoding="utf-8").read().lower()
    assert "upstream" in md or "inputs" in md, \
        "the author prompt never mentions the evidence it is given"
    assert "research" in md