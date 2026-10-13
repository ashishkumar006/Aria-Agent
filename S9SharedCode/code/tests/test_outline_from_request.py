"""The outline must not depend on the model emitting headings.

A run rendered 72 blocks with `sections: 0` - no level-1 heading anywhere - so
there was nothing to expand along and the document stopped at 267 words against
a 27,000-word floor. The expansion could not fire because the thing it keyed on
was missing, and it was missing because the same model said so.

The request itself always names the sections.
"""
import pytest

import skills

Q = ("Create a document.\n\nWrite a comprehensive treatise of about 60 pages "
      "on the engineering of Roman aqueduct systems: construction materials, "
      "gradients and hydraulics, surveying and measurement, maintenance and "
      "failure modes, water quality and lead poisoning, the military and "
      "administrative supply chain, the towns and economies that grew around "
      "them, and their decline and reuse.")


def test_topics_come_from_the_request_when_there_are_no_headings():
    """72 blocks, zero headings: the request is still a full outline."""
    got = skills._topics_from_query(Q)
    assert len(got) >= 6, got
    for want in ("Construction materials", "Gradients and hydraulics",
                 "Surveying and measurement", "Maintenance and failure modes",
                 "Water quality and lead poisoning",
                 "Their decline and reuse"):
        assert want in got, (want, got)


def test_a_preamble_does_not_become_a_section():
    got = skills._topics_from_query(
        "Create a document. Write a report about the history of India.")
    assert got == [], got


def test_a_single_word_is_not_a_section():
    got = skills._topics_from_query(
        "Write a report on Roman aqueducts: materials, cost, politics")
    assert all(len(t.split()) >= 2 for t in got), got


def test_model_headings_are_preferred_when_they_exist():
    blocks = [{"type": "heading", "level": 1, "text": "1. First"},
              {"type": "heading", "level": 1, "text": "Second"},
              {"type": "paragraph", "text": "x"}]
    got = skills._derive_document_outline(blocks, [], [], Q)
    assert got == ["First", "Second"], got


def test_no_headings_falls_back_to_planner_facets():
    graph = [{"skill": "planner",
              "result": {"output": {"research_plan": {"facets": [
                  "materials and mortar", "hydraulics and flow",
                  "surveying instruments"]}}}}]
    got = skills._derive_document_outline([], [], graph, Q)
    assert got == ["materials and mortar", "hydraulics and flow",
                   "surveying instruments"], got


def test_no_headings_no_facets_falls_back_to_questions():
    res = [{"question": "What mortar did they use?", "findings": "..."},
           {"question": "How did water flow uphill?", "findings": "..."}]
    got = skills._derive_document_outline([], res, [], Q)
    assert len(got) == 2, got
    assert "mortar" in got[0]


def test_nothing_at_all_still_yields_a_spine():
    """A generic frame is a last resort, but it is never empty."""
    got = skills._derive_document_outline([], [], [], "make a report")
    assert len(got) >= 4, got
    assert "Introduction" in got


def test_the_query_route_is_preferred_over_the_generic_frame():
    """The user named eight topics; we must not substitute Introduction."""
    got = skills._derive_document_outline([], [], [], Q)
    assert "Construction materials" in got, got
    assert got[:1] != ["Introduction"], got


def test_both_rewrite_sites_use_it():
    import inspect
    src = inspect.getsource(skills.run_skill)
    # The outline is now derived inside `_expand_document_sections`, the one
    # shared implementation both render paths call.
    src = inspect.getsource(skills._expand_document_sections)
    assert "_derive_document_outline(" in src


def test_the_outline_survives_blocks_with_no_headings_at_all():
    """The exact failure: 72 blocks, `sections: 0`."""
    blocks = [{"type": "paragraph", "text": "some text"} for _ in range(72)]
    got = skills._derive_document_outline(blocks, [], [], Q)
    assert len(got) >= 6, got