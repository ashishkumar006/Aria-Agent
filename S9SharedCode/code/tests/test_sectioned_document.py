"""A long document cannot be produced by one tool call, so it is split.

The author emits its whole `blocks` array in a single render_document call. A
60-page document is a ~36,000-token JSON object, and the model compresses hard
on outputs that shape: asked for 27,000 words it returned 2,464, then 1,261,
with max_tokens already at the provider ceiling. The budget was never the
constraint - the SHAPE was.
"""
import inspect

import pytest

import skills


def test_the_sectioned_block_emitter_exists():
    assert hasattr(skills, "_sectioned_document_blocks")
    src = inspect.getsource(skills._sectioned_document_blocks)
    assert "WRITE ONE SECTION" in src


def test_each_section_call_asks_for_json_blocks_not_prose():
    src = inspect.getsource(skills._sectioned_document_blocks)
    assert '"blocks"' in src, "the per-section call must ask for a blocks array"


def test_a_section_sees_all_of_the_research_and_spans_when_it_cannot_hold_it():
    """No per-section slice any more.

    The pack used to be `entries[index % len(entries)]` clipped to 24,000
    characters with 350-character previews of the rest, so a section writer saw
    one researcher chosen by position. Every researcher's material now travels
    to every section, and a section that cannot be written from it in one call
    is written in several and merged.
    """
    src = inspect.getsource(skills._sectioned_document_blocks)
    assert "_section_evidence(" in src, "the section writer has no evidence"
    evidence = inspect.getsource(skills._section_evidence)
    # A positive assertion, not a grep for the old expression: the docstring
    # quotes what it replaced.
    assert "_fleet_json(entries, title)" in evidence, (
        "the section pack is no longer the whole fleet")
    assert "author_section.md" in src, (
        "the section writer is re-sending the full author prompt again")
    prompt = (skills.ROOT / "prompts" / "author_section.md").read_text(
        encoding="utf-8")
    assert "ONE section" in prompt
    assert '"blocks"' in prompt
    assert "render_document" not in prompt, (
        "a section call cannot call render_document; that instruction made "
        "each section write a whole document")


def test_the_author_expands_only_when_materially_short():
    """There is no length gate any more.

    Four runs on an identical request gave 6,509 / 904 / 267 / 935+1,660
    words. The good one was not a threshold firing — it was nine section calls
    happening by luck of which render path the model took. Every gated version
    failed to fire, so a well-behaved author shipped one thin call.
    """
    import ast
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(skills.run_skill)))
    for n in ast.walk(tree):
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant):
            n.value.value = ""
    src = ast.unparse(tree)
    assert "_floor * 0.6" not in src, "a length threshold is gating it again"
    assert "_expand_document_sections(" in src


def test_the_author_expands_on_every_path():
    """One shared implementation, called from both render paths."""
    for fn in (skills.run_skill, skills._expand_document_sections):
        src = inspect.getsource(fn)
        assert "_expand_document_sections" in src or (
            "_sectioned_document_blocks" in src), (
            f"{fn.__name__} never reaches the sectioned writer")


def test_the_document_is_rendered_once_not_once_per_section():
    """N section calls, ONE document - the user asked for one file."""
    src = inspect.getsource(skills._expand_document_sections)
    assert "merged" in src, "sections are not merged into a single spec"
    assert "_sectioned_document_blocks" in src
    assert src.count("render_document(") == 1, (
        "one render call total, not one per section")


def test_headings_are_not_duplicated_by_the_merge():
    """Sections carry their own headings; appending the original ones again
    would produce a document with two of every section title.

    This previously asserted that headings were dropped BY TYPE. That was the
    wrong invariant: it removed every heading in the document, leaving nine
    sections and no visible structure at all. Duplicates are now removed by
    heading text instead, so the assertion is on the dedup, not the deletion.
    """
    src = inspect.getsource(skills._expand_document_sections)
    assert '!= "heading"' not in src, (
        "headings are filtered out of the merge by type again; that leaves "
        "the document with no contents page and no bookmarks")
    assert "seen_h" in src, "merged headings are not deduplicated by text"
    assert "_hkey" in src, "the heading dedup key is missing"


def test_a_short_document_is_never_split():
    """A 600-word report must not be rewritten as 8 sections."""
    assert skills.length_target("A briefing note on NHS handover delays",
                                "author")[0] == 0
    assert skills.length_target("a detailed analysis of X", "author")[0] == 3000


def test_section_outline_falls_back_to_the_research_questions():
    """A spec with no level-1 headings still needs an outline to expand along."""
    src = inspect.getsource(skills._derive_document_outline)
    assert "question" in src, (
        "no fallback outline when the model emitted no headings")


def test_a_section_cannot_invent_its_own_document_title():
    """Live output had three sections with three different invented H1s -
    one of them the raw brief. The requested title is enforced here."""
    out = skills._section_blocks("Production Benchmarks", [
        {"type": "heading", "level": 1,
         "text": "The State of Vector Databases in 2026"},
        {"type": "paragraph", "text": "Body."},
    ])
    assert out[0] == {"type": "heading", "level": 1,
                      "text": "Production Benchmarks"}


def test_a_section_gets_its_heading_even_if_the_model_omits_one():
    out = skills._section_blocks("Cost Models", [
        {"type": "paragraph", "text": "Body."}])
    assert out[0]["type"] == "heading"
    assert out[0]["text"] == "Cost Models"


def test_a_stray_second_level_one_becomes_a_subsection():
    out = skills._section_blocks("Cost Models", [
        {"type": "heading", "level": 1, "text": "Cost Models"},
        {"type": "heading", "level": 1, "text": "Managed vs self-hosted"},
        {"type": "paragraph", "text": "Body."},
    ])
    levels = [b["level"] for b in out if b["type"] == "heading"]
    assert levels == [1, 2], levels


def test_a_section_cannot_write_its_own_reference_list():
    out = skills._section_blocks("Decision Guide", [
        {"type": "heading", "level": 1, "text": "Decision Guide"},
        {"type": "paragraph", "text": "Body."},
        {"type": "heading", "level": 1, "text": "References"},
        {"type": "paragraph", "text": "[1] invented."},
    ])
    assert all("References" not in str(b.get("text") or "") for b in out)
    assert not any("invented" in str(b.get("text") or "") for b in out)

def test_a_request_heading_is_not_document_structure():
    """Live: an eight-page deep report had ONE H1 - the brief itself. The
    request is not a section, and it must not make a flat draft look
    structured enough to skip the rewrite."""
    query = ("Create a document.\n\nWrite a comprehensive, highly detailed "
             "report on vector databases in 2026.")
    blocks = [{"type": "heading", "level": 1,
               "text": "Write a comprehensive, highly detailed report on "
                       "vector databases in 2026."},
              {"type": "paragraph", "text": "Body."}]
    out = skills._strip_request_headings(blocks, query, "Vector Databases")
    assert not [b for b in out if b.get("type") == "heading"]


def test_a_real_section_heading_survives():
    blocks = [{"type": "heading", "level": 1, "text": "Cost Models"},
              {"type": "paragraph", "text": "Body."}]
    out = skills._strip_request_headings(
        blocks, "Write a comprehensive report on vector databases", "Vector DBs")
    assert out[0]["text"] == "Cost Models"


def test_the_outline_ignores_a_brief_used_as_a_heading():
    blocks = [{"type": "heading", "level": 1,
               "text": "Write a comprehensive report on vector databases"}]
    graph = [{"skill": "planner", "result": {"output": {
        "research_plan": {"facets": ["Architecture", "Benchmarks"]}}}}]
    query = ("Create a document.\n\nWrite a comprehensive report on vector "
             "databases")
    assert skills._derive_document_outline(blocks, [], graph, query) == [
        "Architecture", "Benchmarks"]


def test_a_planner_facet_becomes_a_heading_sized_title():
    """Facets are research briefs, not headings. Setting the whole colon
    sentence as an H1 wrapped three lines on every section."""
    assert skills._short_section_heading(
        "Major players and architectural differences: Pinecone, Weaviate, "
        "Qdrant, Milvus, pgvector - indexing, storage, distribution"
    ) == "Major players and architectural differences"
    assert skills._short_section_heading("Cost Models") == "Cost Models"
    assert skills._short_section_heading(
        "one two three four five six seven eight nine ten eleven"
    ) == "one two three four five six seven eight nine ten"


def test_planner_facets_are_read_from_the_live_node_view_shape():
    """`graph_nodes` is a dict keyed by node id and `result` is an
    AgentResult, not a dict. The facet branch never matched, so deep reports
    were written along researcher questions - which echoed the brief."""
    from schemas import AgentResult
    planner = AgentResult(
        success=True, agent_name="planner",
        output={"research_plan": {"facets": ["Architecture", "Costs"]}})
    graph = {"n:1": {"skill": "planner", "result": planner}}
    out = skills._derive_document_outline(
        [], [], graph,
        "Create a document.\n\nWrite a comprehensive report on vector DBs")
    assert out == ["Architecture", "Costs"]


def test_an_echoed_brief_is_not_a_section_question():
    from schemas import AgentResult
    query = "Create a document.\n\nWrite a report on vector databases"
    resolved = [{"output": {"question": query}},
                {"output": {"question": query}}]
    out = skills._derive_document_outline([], resolved, {}, query)
    assert query not in out
    assert len(out) >= 2, out


def test_planner_facets_are_read_from_a_mapping_not_only_a_dict():
    """networkx's NodeView is a Mapping but NOT a dict subclass.
    `isinstance(graph_nodes, dict)` was false in the live run, so the facets
    were invisible and the document got a generic frame."""
    from collections import UserDict
    from schemas import AgentResult
    planner = AgentResult(
        success=True, agent_name="planner",
        output={"research_plan": {"facets": ["Architecture", "Costs"]}})
    graph = UserDict({"n:1": {"skill": "planner", "result": planner}})
    assert not isinstance(graph, dict)
    out = skills._derive_document_outline(
        [], [], graph,
        "Create a document.\n\nWrite a comprehensive report on vector DBs")
    assert out == ["Architecture", "Costs"]
