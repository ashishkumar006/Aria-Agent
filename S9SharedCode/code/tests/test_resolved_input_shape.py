"""The resolved-input wrapper, and the shape bugs it caused.

`resolve_inputs` returns `{"id": "n:3", "kind": "upstream", "output":
{...findings...}}`. Code that reads `e.get("findings")` at the top level
sees nothing, so:

  * the expansion's "is there research?" guard reported `no research` for an
    author handed 46,969 characters of findings, and silently skipped;
  * the outline fallback found no questions and fell through to a generic
    frame;
  * every section got titled "Part N of M" instead of its actual question.
"""
import pytest

import skills


# The real shape resolve_inputs produces for a completed researcher node.
UPSTREAM = {"id": "n:3", "kind": "upstream",
            "output": {"question": "Construction materials in Roman aqueducts",
                       "findings": "Opus caementicium..." * 200,
                       "sources": [{"url": "https://en.wikipedia.org/wiki/X",
                                    "title": "X"}]}}


def test_the_guard_sees_research_through_the_wrapper():
    assert skills._entry_has_research(UPSTREAM), (
        "an upstream researcher entry is not recognised as research")


def test_the_guard_still_sees_a_flat_entry():
    assert skills._entry_has_research({"findings": "x", "sources": [1]})


def test_the_guard_rejects_an_entry_with_no_research():
    assert not skills._entry_has_research({"id": "USER_QUERY",
                                           "kind": "query", "value": "hi"})
    assert not skills._entry_has_research({})
    assert not skills._entry_has_research("not a dict")
    assert not skills._entry_has_research(None)


def test_section_titles_come_from_the_wrapped_question():
    assert skills._section_title(UPSTREAM, 0, 5) == \
        "Construction materials in Roman aqueducts"


def test_section_titles_still_work_on_a_flat_entry():
    assert skills._section_title({"question": "Flat"}, 0, 3) == "Flat"


def test_a_useless_entry_still_falls_back_to_a_positional_title():
    assert skills._section_title({"nothing": 1}, 2, 5) == "Part 3 of 5"


def test_the_outline_uses_wrapped_questions():
    got = skills._derive_document_outline(
        [], [dict(UPSTREAM, id="n:3"), dict(UPSTREAM, id="n:4")], [],
        "make a report")
    assert got and "Construction materials" in got[0], got


def test_the_expansion_does_not_skip_when_research_is_wrapped():
    """The regression that produced a 913-word document with 40,000
    characters of findings sitting in the same prompt."""
    src = __import__("inspect").getsource(skills._expand_document_sections)
    assert "_entry_has_research(" in src, (
        "the guard must read through the resolved-input wrapper")
    # And must not have regressed to a top-level lookup.
    assert 'e.get("findings")' not in src