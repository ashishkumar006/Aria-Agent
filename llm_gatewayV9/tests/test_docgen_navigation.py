"""A rendered report must be navigable and numbered once.

Found on the 18-page Roman aqueduct document:
  * the contents page read "1. 1. Introduction and Historical Context" -
    the renderer numbered a heading the model had already numbered
  * `outline entries: 0` - no PDF bookmarks, so the reader's sidebar is empty
    even though a printed contents page exists
"""
import io

import pytest

docgen = pytest.importorskip("docgen")

BLOCKS = [
    {"type": "heading", "level": 1, "text": "Introduction and Context"},
    {"type": "paragraph", "text": "Body one."},
    {"type": "heading", "level": 1, "text": "Surveying and Alignment"},
    {"type": "paragraph", "text": "Body two."},
    {"type": "heading", "level": 1, "text": "Construction Materials"},
    {"type": "paragraph", "text": "Body three."},
    {"type": "heading", "level": 1, "text": "Hydraulics and Gradients"},
    {"type": "paragraph", "text": "Body four."},
]

# The shape that produced "1. 1. Introduction": the model carried its own
# outline numbering into the heading text.
ALREADY_NUMBERED = [
    {"type": "heading", "level": 1, "text": "1. Introduction and Context"},
    {"type": "paragraph", "text": "Body one."},
    {"type": "heading", "level": 1, "text": "2. Surveying and Alignment"},
    {"type": "paragraph", "text": "Body two."},
    {"type": "heading", "level": 1, "text": "3. Construction Materials"},
    {"type": "paragraph", "text": "Body three."},
    {"type": "heading", "level": 1, "text": "4. Hydraulics and Gradients"},
    {"type": "paragraph", "text": "Body four."},
]


def _text(spec):
    data, _name = docgen.build_pdf(spec)
    try:
        import pypdf
    except ImportError:
        pytest.skip("pypdf missing")
    rd = pypdf.PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in rd.pages), rd


def test_a_contents_entry_is_not_numbered_twice():
    txt, _rd = _text({"title": "T", "blocks": ALREADY_NUMBERED, "toc": True})
    import re
    # A heading that already began "1." must render as "1. Introduction",
    # never "1. 1. Introduction".
    assert not re.search(r"\b(\d+)\.\s+\1\.\s", txt), (
        "a heading was numbered twice:\n"
        + "\n".join(l for l in txt.splitlines() if re.search(r"\d+\.\s+\d+\.", l))[:600])


def test_unnumbered_headings_still_get_numbers():
    txt, _rd = _text({"title": "T", "blocks": BLOCKS, "toc": True})
    for want in ("1. Introduction and Context", "2. Surveying and Alignment"):
        assert want in txt, (want, txt[:600])


def test_the_pdf_carries_an_outline():
    """An 18-page report with a printed contents page but no bookmark tree is
    half-navigable: the reader's sidebar shows nothing to click."""
    _txt, rd = _text({"title": "T", "blocks": BLOCKS, "toc": True})
    entries = rd.outline
    assert entries, "no PDF outline/bookmarks were written"
    flat = []

    def _walk(xs):
        for x in xs:
            if isinstance(x, list):
                _walk(x)
            else:
                flat.append(getattr(x, "title", ""))
    _walk(entries)
    assert any("Introduction" in t for t in flat), flat


def test_a_bookmark_failure_never_costs_the_document():
    """Bookmarks are wrapped; the file must still build."""
    data, _n = docgen.build_pdf({"title": "T", "blocks": BLOCKS, "toc": True})
    assert data[:5] == b"%PDF-"