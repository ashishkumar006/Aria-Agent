"""Facet fallbacks must never carry the request back as the topic.

A logged run emitted a section titled verbatim: "Create a document. Write a
comprehensive tre..." - the Authoring console prefixes every brief with
"Create a document.\\n\\n" and the old regex matched only the task verb, so
with that prefix in front nothing matched and the whole raw prompt became
the research facet and then the section heading.
"""
import pytest

flow = pytest.importorskip("flow")

RAW = ("Create a document. Write a comprehensive treatise of about 60 pages "
       "on the engineering of Roman aqueduct systems: construction materials, "
       "gradients and hydraulics, surveying and measurement")


def test_the_console_prefix_never_becomes_a_facet():
    for f in flow._fallback_facets(RAW):
        assert "create a document" not in f.lower(), f
        assert "write a comprehensive" not in f.lower(), f


def test_the_topic_survives_verb_extraction():
    facets = flow._fallback_facets(RAW)
    assert any("aqueduct" in f.lower() for f in facets), facets


def test_a_bare_request_does_not_produce_an_empty_facet():
    for q in ("Create a document.", "", "   ", "write a report", "?"):
        for f in flow._fallback_facets(q):
            assert f.strip(), q
            assert len(f) > 12, (q, f)


def test_the_length_clause_is_not_left_in_the_facet():
    """'about 60 pages' was surviving into the facet text."""
    facets = flow._fallback_facets(RAW)
    blob = " ".join(facets).lower()
    assert "60 pages" not in blob, blob[:120]
    assert " pages " not in blob, blob[:120]


def test_a_normal_research_question_is_untouched():
    facets = flow._fallback_facets("what did Roman surveyors actually measure?")
    assert any("roman surveyors" in f.lower() for f in facets), facets