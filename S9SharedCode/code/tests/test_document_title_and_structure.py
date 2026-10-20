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
