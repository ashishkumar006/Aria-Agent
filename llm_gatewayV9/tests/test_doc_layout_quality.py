"""Layout defects measured on the 12-page aqueduct document.

  * the running header used `title[:78]` - a CHARACTER slice, which still
    overflowed the measure, ran off the page and extracted as a doubled
    header on every page
  * `running_header` was documented and accepted but NEVER READ: the header
    always drew `title`
  * `Subject` was unset, so every file reported `Subject: (unspecified)`
  * a comparison table had no column rules, so its cells flattened into one
    undifferentiated run of text

The title-selection and heading-retention rules live in the AGENT package and
are tested in S9SharedCode/code/tests/test_document_title_and_structure.py.
Importing `skills` here would put the agent's `state`/`browser`/`config`
modules ahead of the gateway's on sys.path and break four gateway test modules
at collection time, so each package tests its own half of the chain.
"""
import io

import pytest

docgen = pytest.importorskip("docgen")

PROMPT = ("Create a document. Write a comprehensive treatise of about 60 pages "
          "on the engineering of Roman aqueduct systems: construction "
          "materials, gradients and hydraulics, surveying and measurement, "
          "maintenance and failure modes, water quality and lead poisoning, "
          "and their decline and reuse.")

# Enough text for several pages: the running header only appears on page 2+.
BODY = [{"type": "paragraph", "text": "Lorem ipsum dolor sit amet. " * 700}]


def _pdf_text(data):
    import pypdf
    rd = pypdf.PdfReader(io.BytesIO(data))
    return rd, "\n".join((p.extract_text() or "") for p in rd.pages)


def test_a_long_running_header_is_elided_to_fit():
    """`title[:78]` sliced by CHARACTERS; a long string still overflowed."""
    spec = {"title": "Short title", "running_header": PROMPT,
            "blocks": BODY}
    data, _n = docgen.build_pdf(spec)
    rd, _txt = _pdf_text(data)
    for i, pg in enumerate(rd.pages):
        if i == 0:
            continue
        t = pg.extract_text() or ""
        assert t.count("Create a document") <= 1, (i, t[:300])
        assert "\u2026" in t, f"p{i + 1} header was not elided: {t[:200]}"


def test_the_running_header_is_its_own_text_not_the_title():
    """`running_header` was accepted and then ignored.

    Only the HEADER line is checked. The footer legitimately carries the
    title, so asserting the title is absent from the whole page would fail
    for the right behaviour."""
    spec = {"title": "The Full Document Title Goes Here",
            "running_header": "Aqueducts", "blocks": BODY}
    data, _n = docgen.build_pdf(spec)
    rd, _txt = _pdf_text(data)
    for i, pg in enumerate(rd.pages):
        if i == 0:
            continue
        header = (pg.extract_text() or "").splitlines()[0]
        assert header.strip() == "Aqueducts", (i + 1, header)


def test_a_header_that_fits_is_left_alone():
    spec = {"title": "T", "running_header": "Aqueducts", "blocks": BODY}
    data, _n = docgen.build_pdf(spec)
    rd, _txt = _pdf_text(data)
    later = " ".join((p.extract_text() or "") for p in rd.pages[1:])
    assert "Aqueducts" in later, "a header that fits was elided"


def test_subject_metadata_is_populated():
    spec = {"title": "T", "subtitle": "A short subtitle",
            "blocks": [{"type": "paragraph", "text": "x"}]}
    data, _n = docgen.build_pdf(spec)
    rd, _txt = _pdf_text(data)
    md = rd.metadata or {}
    assert md.get("/Title") == "T", md
    assert md.get("/Subject") != "(unspecified)", md


def test_a_document_with_sections_gets_a_contents_page_and_bookmarks():
    spec = {"title": "T", "toc": True, "blocks": [
        {"type": "heading", "level": 1, "text": "Materials"},
        {"type": "paragraph", "text": "Body."},
        {"type": "heading", "level": 1, "text": "Hydraulics"},
        {"type": "paragraph", "text": "Body."},
        {"type": "heading", "level": 1, "text": "Decline"},
        {"type": "paragraph", "text": "Body."},
        {"type": "heading", "level": 1, "text": "Legacy"},
        {"type": "paragraph", "text": "Body."}]}
    data, _n = docgen.build_pdf(spec)
    rd, txt = _pdf_text(data)
    assert "Placeholder for table of contents" not in txt, (
        "the contents page has nothing behind it")
    assert rd.outline, "no PDF bookmarks for a document with sections"


def test_a_heading_is_not_numbered_twice():
    """The contents page read "1. 1. Introduction ..."."""
    spec = {"title": "T", "toc": True, "number_sections": True, "blocks": [
        {"type": "heading", "level": 1, "text": "1. Introduction"},
        {"type": "paragraph", "text": "Body."},
        {"type": "heading", "level": 1, "text": "2. Hydraulics"},
        {"type": "paragraph", "text": "Body."},
        {"type": "heading", "level": 1, "text": "3. Decline"},
        {"type": "paragraph", "text": "Body."},
        {"type": "heading", "level": 1, "text": "4. Legacy"},
        {"type": "paragraph", "text": "Body."}]}
    data, _n = docgen.build_pdf(spec)
    _rd, txt = _pdf_text(data)
    import re
    assert not re.search(r"\b(\d+)\.\s+\1\.\s", txt), (
        "a heading was numbered twice: "
        + "\n".join(l for l in txt.splitlines()
                    if re.search(r"\d+\.\s+\d+\.", l))[:400])


def test_a_table_has_column_rules():
    """Without them the cells extract as one undifferentiated run."""
    import inspect
    src = inspect.getsource(docgen.build_pdf)
    assert "LINEBEFORE" in src, (
        "a comparison table with no column rules reads as a single line of "
        "text to both a reader and to extraction")


def test_a_table_still_renders_its_cells():
    spec = {"title": "T", "blocks": [
        {"type": "table",
         "header": ["Hypothesis", "Implication"],
         "rows": [["Sinter protects", "Lead risk is low"],
                  ["Sinter fails", "Chronic exposure"]]}]}
    data, _n = docgen.build_pdf(spec)
    _rd, txt = _pdf_text(data)
    assert "Sinter protects" in txt and "Chronic exposure" in txt, txt[:300]