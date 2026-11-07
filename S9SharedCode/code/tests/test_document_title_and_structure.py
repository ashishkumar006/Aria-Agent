"""Title selection and section-heading retention in the agent package.

The 12-page aqueduct document opened with the literal prompt as its title -
"Create a document. Write a comprehensive treatise of about 60 pages on ..." -
because the document's own title was never extracted.

The companion layout tests (running header, `Subject`, tables, bookmarks,
double numbering) live in the gateway package: each package tests its own half
of the chain, since importing `skills` from the gateway suite shadows the
gateway's own `state`/`browser`/`config` modules.
"""
import pytest

skills = pytest.importorskip("skills")

PROMPT = ("Create a document. Write a comprehensive treatise of about 60 pages "
          "on the engineering of Roman aqueduct systems: construction "
          "materials, gradients and hydraulics, surveying and measurement, "
          "maintenance and failure modes, water quality and lead poisoning, "
          "and their decline and reuse.")


def test_the_ui_prefix_is_stripped_from_the_title():
    t = skills._document_title({}, PROMPT)
    assert not t.lower().startswith("create a document"), t


def test_the_length_instruction_is_stripped():
    t = skills._document_title({}, PROMPT)
    assert "60 pages" not in t, t
    assert "comprehensive treatise" not in t, t


def test_the_title_is_the_subject_not_the_instruction():
    t = skills._document_title({}, PROMPT)
    assert "aqueduct" in t.lower(), t
    assert len(t) < 120, t


def test_a_model_supplied_title_wins():
    t = skills._document_title({"title": "Frontinus On Aqueducts"}, PROMPT)
    assert t == "Frontinus On Aqueducts", t


def test_a_cover_block_title_wins():
    t = skills._document_title(
        {"blocks": [{"type": "cover", "title": "Roman Aqueducts"}]}, PROMPT)
    assert t == "Roman Aqueducts", t


def test_a_first_heading_is_the_last_resort_title():
    """The stripped query names the whole document; the first heading is one
    section of it, so it is only used when the query yields nothing."""
    t = skills._document_title(
        {"blocks": [{"type": "heading", "level": 1, "text": "1. Materials"}]},
        "Create a document.\n\nmake a thing")
    assert t and not t.lower().startswith("create a document"), t


def test_a_delegated_brief_names_the_document_not_the_instruction():
    """A chat-delegated document must not be filed under the agent's own
    instruction. Observed live: the PDF came back named
    `You-are-the-authoring-agent.-Produce-a-finished-document-and-render-it-as-a-real.pdf`.
    The brief now leads with the user's words, and the instruction is a second
    paragraph that the title fallback ignores."""
    from agent_server import _authoring_brief
    q = _authoring_brief("One-page brief comparing REST and GraphQL. As a PDF.",
                         "pdf")
    t = skills._document_title({}, q)
    assert "authoring agent" not in t.lower(), t
    assert "render_document" not in t, t
    assert "REST" in t and "GraphQL" in t, t
    assert len(t) < 120, t


def test_a_bare_prompt_still_yields_something_short():
    for q in ("", "   ", "?", "!!!"):
        t = skills._document_title({}, q)
        assert t and len(t) <= 200, (q, t)


def test_a_title_is_always_one_line():
    t = skills._document_title({}, PROMPT)
    assert "\n" not in t and "\r" not in t, repr(t)


def test_a_model_title_that_is_the_request_is_rejected():
    """A model that cannot think of a title echoes the brief; that echo was
    printed at 19pt on the cover and in every page header."""
    brief = ("Write a comprehensive, highly detailed report on the state of "
             "vector databases in 2026. Cover the major players.")
    t = skills._document_title({"title": brief}, brief)
    assert not t.lower().startswith("write a"), t
    assert "Cover the major players" not in t, t


def test_the_title_is_the_first_sentence_of_the_brief():
    brief = ("Create a document.\n\nWrite a comprehensive, highly detailed "
             "report on the state of vector databases in 2026. Cover the "
             "major players (Pinecone, Weaviate, Qdrant), benchmarks, cost "
             "models, and a decision guide.")
    t = skills._document_title({}, brief)
    assert t == "The state of vector databases in 2026", t


LLM_PROMPT = ("Create me an detailed pdf about working of llm's, make it big "
              "and nearky 25 pages long")


def test_a_request_verb_with_a_different_object_is_still_a_request():
    """The detector used to match only "create a document", so a model that
    echoed "Create me an detailed pdf about working of llm's, make it big and
    nearky 25 pages long" as its title was believed - and that sentence was
    then printed in the header and footer of all 23 pages."""
    assert skills._title_is_request(LLM_PROMPT), LLM_PROMPT
    t = skills._document_title({"title": LLM_PROMPT}, LLM_PROMPT)
    assert not t.lower().startswith("create"), t
    assert "25 pages" not in t, t


def test_an_instruction_clause_does_not_survive_into_the_title():
    """Even after the verb and the noun are stripped, the request keeps
    talking: "..., make it big and nearky 25 pages long"."""
    t = skills._document_title({}, LLM_PROMPT)
    for junk in ("make it", "nearky", "25 pages", "pdf"):
        assert junk not in t.lower(), (junk, t)


def test_a_title_that_names_a_length_is_the_request():
    assert skills._title_is_request("Everything about attention, 40 pages long")
    assert not skills._title_is_request("Notes from a 1953 seminar")


def test_pdf_and_deck_count_as_document_nouns():
    """The strip list knew "report" and "treatise" but not the formats people
    actually ask for, so the noun survived into the title."""
    t = skills._document_title({}, "Create me a detailed pdf about attention heads")
    assert "pdf" not in t.lower(), t
    assert "attention heads" in t.lower(), t


def test_section_headings_are_not_stripped_from_the_merge():
    """Merging used to drop heading blocks by type to dodge double-numbering.

    The result was a document with NINE sections and ZERO visible headings:
    no contents page, no bookmarks, and an unbroken wall of prose. The
    numbering is now stripped at the SOURCE (`docgen._unnumber_heading`), so
    the filter is solving a problem that no longer exists - and the test
    below asserts the filter is gone, so it cannot creep back in.
    """
    import inspect
    src = inspect.getsource(skills._expand_document_sections)
    assert '!= "heading"' not in src, (
        "headings are being filtered out of the section merge again; "
        "docgen strips a leading number before numbering, so this costs the "
        "document its entire structure")


# The merge itself is covered behaviourally by test_sectioned_document.py and
# test_sectioned_rewrite_paths.py. The check above guards the specific
# regression (headings filtered out of the merge) without pinning the 13
# positional arguments of a private helper, which would rot on any signature
# change.
