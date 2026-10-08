"""A PDF must not show literal markdown."""
import io
import re
import zipfile

import pytest

import docgen


def _pdf_text(blob: bytes) -> str:
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(blob))
    return "\n".join((p.extract_text() or "") for p in r.pages)


def test_bold_markers_are_rendered_not_printed():
    """author.md tells the model to mark defined terms with **bold**. The PDF
    builder only escaped XML, so the reader got literal asterisks."""
    blob, _n, _ct = docgen.generate("pdf", {
        "title": "Vector Indexes",
        "blocks": [{"type": "paragraph",
                    "text": "A **vector index** organises *embeddings* for search."}],
    })
    text = _pdf_text(blob)
    assert "**" not in text, f"literal bold markers in the PDF: {text[:160]!r}"
    assert "vector index" in text


def test_inline_code_becomes_monospace_not_backticks():
    blob, _n, _ct = docgen.generate("pdf", {
        "title": "Syntax",
        "blocks": [{"type": "paragraph", "text": "Run `pip install aria` first."}],
    })
    text = _pdf_text(blob)
    assert "`" not in text
    assert "pip install aria" in text


def test_markdown_links_degrade_to_text_and_url():
    blob, _n, _ct = docgen.generate("pdf", {
        "title": "Ref",
        "blocks": [{"type": "paragraph",
                    "text": "See [the docs](https://example.com/x) for detail."}],
    })
    text = _pdf_text(blob)
    assert "](" not in text
    assert "the docs" in text and "example.com" in text


def test_xml_characters_are_still_escaped():
    """_rich runs regexes over escaped text; a stray < must not break the
    XML parse and silently produce a corrupt PDF."""
    blob, _n, _ct = docgen.generate("pdf", {
        "title": "A & B < C > D",
        "blocks": [{"type": "paragraph", "text": "if a<b and c>d then **x**"}],
    })
    text = _pdf_text(blob)
    assert "&" in text and "x" in text


def test_unbalanced_markers_do_not_leak():
    blob, _n, _ct = docgen.generate("pdf", {
        "title": "T",
        "blocks": [{"type": "paragraph", "text": "2 * 3 * 4 = 24 and **bold"}],
    })
    text = _pdf_text(blob)
    assert "**" not in text
    assert "24" in text


def test_docx_and_pptx_strip_markdown_rather_than_render_it():
    for fmt, spec in (
        ("docx", {"title": "D", "blocks": [{"type": "paragraph",
                                            "text": "A **bold** word."}]}),
        ("pptx", {"title": "P", "blocks": [{"type": "heading", "level": 1,
                                            "text": "A **bold** title"},
                                           {"type": "bullets",
                                            "items": ["**key** point"]}]}),
    ):
        blob, _n, _ct = docgen.generate(fmt, spec)
        if fmt == "docx":
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                body = z.read("word/document.xml").decode("utf-8", "replace")
            assert "**" not in body
        else:
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                body = " ".join(z.read(n).decode("utf-8", "replace")
                                for n in z.namelist()
                                if n.startswith("ppt/slides/slide"))
            assert "**" not in body
        assert "bold" in body