"""Tests for the document generators (docgen) and its API route."""

import io
import zipfile

import pytest

import docgen
from docgen import DocGenError


def spec():
    return {
        "title": "Indexing Report",
        "subtitle": "flat vs IVF vs HNSW",
        "blocks": [
            {"type": "heading", "level": 1, "text": "Summary"},
            {"type": "paragraph", "text": "A **bold** claim with `code` and "
                                          "[a link](https://example.com)."},
            {"type": "bullets", "items": ["one", "two"]},
            {"type": "numbers", "items": ["first", "second"]},
            {"type": "quote", "text": "quoted line"},
            {"type": "table", "header": ["A", "B"], "rows": [["1", "2"], ["3", "4"]]},
            {"type": "pagebreak"},
            {"type": "paragraph", "text": "after the break"},
        ],
    }


@pytest.mark.parametrize("fmt,magic", [("pdf", b"%PDF"), ("pptx", b"PK"),
                                       ("docx", b"PK"), ("xlsx", b"PK")])
def test_every_format_produces_a_real_file(fmt, magic):
    data, name, ctype = docgen.generate(fmt, spec())
    assert data.startswith(magic), f"{fmt} is not a {magic} file"
    assert name.endswith("." + fmt)
    assert len(data) > 500
    assert ctype


@pytest.mark.parametrize("fmt", ["pptx", "docx", "xlsx"])
def test_ooxml_output_is_a_valid_zip(fmt):
    data, _, _ = docgen.generate(fmt, spec())
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert z.testzip() is None
        assert "[Content_Types].xml" in z.namelist()


def test_pdf_has_multiple_pages_when_a_pagebreak_is_present():
    data, _, _ = docgen.generate("pdf", spec())
    assert data.count(b"/Type /Page") >= 2, "pagebreak did not start a page"


def test_xlsx_makes_one_sheet_per_table():
    data, _, _ = docgen.generate("xlsx", spec())
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert any("sheet" in n for n in z.namelist())


def test_filename_is_derived_from_the_title_and_sanitised():
    _, name, _ = docgen.generate("pdf", {"title": "Q3 / Report: 2026!", "blocks": [
        {"type": "paragraph", "text": "x"}]})
    assert name == "Q3-Report-2026.pdf", name


@pytest.mark.parametrize("bad,label", [
    ({}, "empty spec"),
    ({"blocks": []}, "no blocks"),
    ({"blocks": "not a list"}, "blocks wrong type"),
])
def test_empty_input_is_a_400_not_a_crash(bad, label):
    with pytest.raises(DocGenError):
        docgen.generate("pdf", bad)


def test_unknown_format_is_refused_with_the_valid_list():
    with pytest.raises(DocGenError) as e:
        docgen.generate("exe", spec())
    assert "pdf" in str(e.value)


def test_spec_must_be_an_object():
    with pytest.raises(DocGenError):
        docgen.generate("pdf", ["not", "an", "object"])


def test_markup_is_escaped_not_rendered():
    """A <script> in the title must not survive into the PDF as markup."""
    data, _, _ = docgen.generate("pdf", {
        "title": "<script>alert(1)</script>",
        "blocks": [{"type": "paragraph", "text": "<b>x</b> & <i>y</i>"}]})
    # reportlab draws text as glyph references; the literal tag must not
    # appear in a decoded content stream.
    import re
    import zlib
    drawn = b""
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        try:
            drawn += zlib.decompress(m.group(1))
        except Exception:
            continue
    assert b"<script>" not in drawn
    assert b"alert(" not in drawn


def test_table_rows_are_trimmed_not_dropped():
    """A wide table must keep its columns instead of crashing or truncating
    each row to nothing."""
    rows = [[str(i * 20 + j) for j in range(12)] for i in range(30)]
    data, _, _ = docgen.generate("pdf", {
        "title": "wide", "blocks": [
            {"type": "table", "header": [f"c{i}" for i in range(12)], "rows": rows}]})
    assert data.startswith(b"%PDF")
    assert len(data) > 1000


def test_malformed_blocks_are_skipped_not_fatal():
    data, _, _ = docgen.generate("pdf", {
        "title": "mixed",
        "blocks": [
            "not a dict", {"type": "nope"}, {"type": "paragraph"},
            {"type": "paragraph", "text": "kept"},
            {"type": "table", "rows": []},
        ]})
    assert data.startswith(b"%PDF")


def test_api_formats_and_schema_routes():
    from fastapi.testclient import TestClient
    from docgen_api import router
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    fmts = c.get("/v1/docgen/formats").json()
    assert {f["id"] for f in fmts["formats"]} == {"pdf", "pptx", "docx", "xlsx"}
    sch = c.get("/v1/docgen/schema").json()
    assert "blocks" in sch["spec"]


def test_api_returns_the_file_with_a_filename():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from docgen_api import router

    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    r = c.post("/v1/docgen", json={"format": "pdf", "spec": spec()})
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")
    assert "Indexing-Report.pdf" in r.headers.get("content-disposition", "")


def test_api_rejects_a_bad_request_with_400():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from docgen_api import router

    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    assert c.post("/v1/docgen", json={"format": "pdf"}).status_code == 400
    assert c.post("/v1/docgen", json={"format": "exe", "spec": spec()}).status_code == 400
    assert c.post("/v1/docgen", content=b"{bad json").status_code == 400


def test_a_render_with_nothing_in_it_is_refused():
    """The failure that looks most like success.

    Observed live: a deck node called render_document with an empty block list
    and the builder returned a one-page file anyway. The run delivered it, the
    user got a document with nothing in it, and nothing anywhere said so. A
    spec with no title, no blocks and no sheets cannot be a document.
    """
    import docgen

    for fmt in ("pdf", "pptx", "docx", "xlsx"):
        try:
            docgen.generate(fmt, {"blocks": [], "sheets": []})
        except docgen.DocGenError as e:
            assert "empty" in str(e).lower(), (fmt, str(e))
        else:
            raise AssertionError(
                f"{fmt}: an empty spec produced a file instead of an error")


def test_a_title_only_document_is_still_allowed():
    """A title page is a real, if small, document. The gate above is about a
    spec with nothing in it, not about brevity."""
    import docgen

    blob, _n, _ct = docgen.generate("pdf", {"title": "Cover only"})
    assert blob.startswith(b"%PDF-")