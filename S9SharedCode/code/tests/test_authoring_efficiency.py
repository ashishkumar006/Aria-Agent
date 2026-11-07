"""Authoring should spend tokens on the document the user asked for.

The old pipeline always rewrote the draft section-by-section. A two-paragraph
brief became 1,436 words; a one-page brief became a multi-section report; each
section call re-sent the full author prompt and a 90k-char research dump. These
tests pin the length-aware gate, compact evidence, style inference and source
fallback.
"""
from __future__ import annotations

import skills


def _rich_research() -> list[dict]:
    return [{"id": "n:2", "kind": "upstream", "output": {
        "question": "What is the topic?",
        "findings": "\n\n".join(f"Finding paragraph {i} with a concrete figure {i} and a source-backed detail. " * 8 for i in range(40)),
        "sources": [{"title": "Source A", "url": "https://example.com/a"}]}}]


def test_a_short_brief_does_not_expand():
    ok, why = skills.should_expand_document(
        "Create a document. A one-paragraph summary of the water cycle",
        "author", 200, ["Introduction", "Conclusion"], _rich_research())
    assert ok is False and "capped" in why


def test_two_paragraphs_do_not_expand_even_with_rich_research():
    ok, _ = skills.should_expand_document(
        "Create a document. two paragraphs on the water cycle",
        "author", 300, ["A", "B"], _rich_research())
    assert ok is False


def test_a_report_that_already_clears_its_floor_does_not_expand():
    ok, why = skills.should_expand_document(
        "Create a neat report on temples in India", "author", 1655,
        ["A", "B", "C"], _rich_research())
    assert ok is False and "floor" in why


def test_a_comprehensive_brief_expands_when_short():
    ok, why = skills.should_expand_document(
        "Write a comprehensive, highly detailed essay about quantum computing",
        "author", 500, ["A", "B", "C"], _rich_research())
    assert ok is True


def test_an_unspecified_spreadsheet_does_not_expand():
    ok, why = skills.should_expand_document(
        "A spreadsheet comparing IVF, HNSW and flat search on recall and memory",
        "author", 400, ["A", "B"], _rich_research())
    assert ok is False and "no explicit depth" in why


def test_style_is_inferred_from_the_brief():
    assert skills._auto_document_style("A memo on onboarding") == "memo"
    assert skills._auto_document_style("A one-page brief on pricing") == "brief"
    assert skills._auto_document_style("An academic paper on voting") == "academic"
    assert skills._auto_document_style("A newsletter for the team") == "newsletter"
    assert skills._auto_document_style("A technical specification") == "technical"
    assert skills._auto_document_style("A report on the market") == "report"


def test_sources_become_references_when_the_model_omits_them():
    refs = skills._references_from_resolved(_rich_research())
    assert refs and refs[0]["title"] == "Source A"
    assert refs[0]["url"] == "https://example.com/a"


def test_a_host_labelled_source_becomes_a_usable_citation():
    """Researchers label pages "posthog.com: feature flags vs configuration".
    Printed as-is, the hostname became the title and the numbered style then
    dropped the URL - a source the reader could not open."""
    refs = skills._references_from_resolved([{"output": {"sources": [{
        "title": "posthog.com: feature flags vs configuration",
        "url": "https://posthog.com/product-engineers/feature-flags-vs-configuration",
    }]}}])
    assert refs == [{
        "title": "feature flags vs configuration",
        "url": "https://posthog.com/product-engineers/feature-flags-vs-configuration",
        "type": "web",
        "container": "posthog.com",
    }]


def _fleet(n: int = 4) -> list[dict]:
    """n researchers, each with a distinguishable body of findings."""
    return [{"id": f"n:{i}", "kind": "upstream", "output": {
        "question": f"Facet number {i} of the document",
        "findings": "\n\n".join(
            f"Facet {i} finding {j}: a figure, a citation and an example. " * 6
            for j in range(30)),
        "sources": [{"title": f"Source {i}-{j}", "url": f"https://e.test/{i}/{j}"}
                    for j in range(9)]}} for i in range(n)]


def test_section_evidence_carries_the_entire_fleet():
    """Every researcher's material, whole, into every section.

    This used to hand section `i` the findings of researcher `i`
    (`entries[index % len(entries)]`), clipped to 24,000 characters, plus a
    350-character preview of each of the others. With six researchers a section
    writer therefore saw one sixth of the research it was paid to use, chosen by
    position rather than by subject.
    """
    entries = _fleet()
    out = skills._section_evidence(entries, 1, ["A", "B", "C", "D"])
    assert "FULL RESEARCH FLEET" in out
    for e in entries:
        body = e["output"]["findings"]
        assert body[:300] in out, f"{e['id']} findings missing"
        assert body[-300:] in out, f"{e['id']} findings truncated"
        for s in e["output"]["sources"]:
            assert s["url"] in out, f"{e['id']} lost a source"


def test_the_fleet_is_ordered_by_relevance_not_position():
    """Ordering is the only thing that may differ between sections."""
    entries = _fleet()
    titles = ["Facet number 3 of the document", "A", "B", "C"]
    out = skills._section_evidence(entries, 0, titles)
    first = out.index('"order": 1')
    assert out.index("Facet number 3") < out.index("Facet number 0"), (
        "the section's own facet should lead the pack")
    assert first > 0


def test_a_section_spans_calls_instead_of_losing_evidence():
    """When one call cannot hold the fleet, the section gets more calls.

    The old behaviour was to cut the text. `_section_evidence_windows` splits at
    the natural seam — one researcher per pack — and `_one` merges the results,
    so the count of calls absorbs the size instead of the content shrinking.
    """
    entries = _fleet()
    windows = skills._section_evidence_windows(entries, 0, ["A", "B"])
    assert len(windows) == len(entries)
    for i, w in enumerate(windows):
        body = entries[i]["output"]["findings"]
        assert body[:300] in w and body[-300:] in w
        assert entries[(i + 1) % len(entries)]["output"]["findings"][:300] not in w
    src = __import__("inspect").getsource(skills._sectioned_document_blocks)
    assert "_section_evidence_windows(" in src, (
        "the spanning fallback is gone; a section too large for one call would "
        "be trimmed again")


def test_a_short_brief_gets_a_research_effort_cap():
    note = skills._research_effort_note(
        "Create a document. A one-page brief comparing REST and GraphQL")
    assert "at most 3 authoritative sources" in note
    assert skills._research_effort_note(
        "Write a comprehensive report on vector databases") == ""


def test_a_deep_document_prefers_the_planner_facets_over_two_headings():
    blocks = [{"type": "heading", "level": 1, "text": "Introduction"},
              {"type": "heading", "level": 1, "text": "Analysis"}]
    graph_nodes = [{"skill": "planner", "result": {"output": {"research_plan": {
        "facets": ["Architecture", "HNSW indexing", "IVF and flat search",
                   "Production selection"]}}}}]
    outline = skills._derive_document_outline(
        blocks, [], graph_nodes,
        "Write a comprehensive, highly detailed technical report on vector databases")
    assert len(outline) >= 4
    assert "Production selection" in outline


def test_nothing_bounds_how_much_a_researcher_may_read():
    """The fetch bounds are gone: (2, 4000) … (6, 35000) capped hops and the
    characters of each page a researcher could see, which is a cap on the
    evidence itself rather than on the prompt."""
    import inspect
    assert not hasattr(skills, "_research_runtime_limits")
    assert not hasattr(skills, "_short_research_limits")
    src = inspect.getsource(skills.run_skill)
    assert "max_tool_result_chars=0" in src, (
        "the harness is clipping tool results again")
    assert "_research_runtime_limits" not in src


def test_a_long_report_gets_a_bounded_research_budget():
    note = skills._research_effort_note(
        "Create a document. Write a comprehensive, highly detailed report on vector databases")
    assert "at most 4 authoritative sources" in note


def test_a_full_treatise_keeps_the_full_research_budget():
    note = skills._research_effort_note(
        "Create a document. Write a 60 page comprehensive treatise on Roman aqueducts")
    assert note == ""


def test_a_degenerate_file_is_not_a_document():
    assert skills.document_quality_error(
        9, 0, "Create a document. A one-page brief", "author")
    assert skills.document_quality_error(
        400, 3, "Create a document. A one-page brief", "author") == ""
    assert skills.document_quality_error(
        150, 2, "Create a document. Write a comprehensive report", "author")


def test_the_author_is_never_hand_a_trimmed_evidence_block():
    """The first call used to be cut to 16,000 characters of INPUTS, and the
    empty-reply retry to 24,000 — both silently dropping most of the research.
    A document too large for one call is now written in more calls."""
    import inspect
    assert not hasattr(skills, "_trim_prompt_inputs")
    assert not hasattr(skills, "_initial_author_prompt")
    src = inspect.getsource(skills.run_skill)
    assert "reply = await _run_loop(rendered)" in src, (
        "the author is being sent something other than the complete prompt")


def test_a_newsletter_stays_two_column_even_if_the_model_says_one():
    assert skills._document_columns("newsletter", 0, 1) == 2
    assert skills._document_columns("Newsletter", None, None) == 2
    # An explicit caller choice still wins.
    assert skills._document_columns("newsletter", 3, 1) == 3
    assert skills._document_columns("report", 0, 2) == 2
    assert skills._document_columns("report", 0, 0) == 0
