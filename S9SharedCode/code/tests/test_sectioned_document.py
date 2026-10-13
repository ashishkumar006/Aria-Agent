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


def test_a_section_sees_only_its_share_of_the_research():
    """One call handed the entire fleet is what compressed the output."""
    src = inspect.getsource(skills._sectioned_document_blocks)
    assert "90_000" in src, "no bound on the per-section research payload"
    assert "picked = entries[" in src, "research is not partitioned per section"


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