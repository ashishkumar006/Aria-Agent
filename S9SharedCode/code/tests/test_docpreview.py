"""docpreview: read what a document ACTUALLY contains, not what the run claimed.

The audit found decks whose reported slide titles did not exist in the file,
workbooks that opened on a blank sheet, and a run whose `sections` list bore
no relation to the artifact. The preview exists so the console can show the
real structure and warn about the gaps.

Fixtures are hand-built zips: the agent's venv has no python-pptx/openpyxl
and must not grow one just for tests.
"""
import io
import zipfile

import pytest

import docpreview


A = "http://schemas.openxmlformats.org/drawingml/2006/main"
PPT = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
SS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
PKG_R = "http://schemas.openxmlformats.org/package/2006/relationships"


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return buf.getvalue()


def _slide(title: str, bullets: list[str]) -> str:
    paras = "".join(
        f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in [title] + bullets)
    return (f'<p:sld xmlns:p="{PPT}" xmlns:a="{A}"><p:cSld><p:spTree><p:sp>'
            f"<p:txBody>{paras}</p:txBody></p:sp></p:spTree></p:cSld></p:sld>")


def _pptx(n_slides: int = 12) -> bytes:
    files: dict[str, str] = {}
    for i in range(1, n_slides + 1):
        files[f"ppt/slides/slide{i}.xml"] = _slide(
            f"Slide {i}", [f"point {i}a", f"point {i}b"])
    rels = "".join(
        f'<Relationship Id="rId{i}" '
        f'Type="{R}/slide" Target="slides/slide{i}.xml"/>'
        for i in range(1, n_slides + 1))
    files["ppt/_rels/presentation.xml.rels"] = (
        f'<Relationships xmlns="{PKG_R}">{rels}</Relationships>')
    # presentation.xml lists them in true order, 1..12
    ids = "".join(f'<p:sldId id="{255 + i}" r:id="rId{i}"/>'
                  for i in range(1, n_slides + 1))
    files["ppt/presentation.xml"] = (
        f'<p:presentation xmlns:p="{PPT}" xmlns:r="{R}">'
        f"<p:sldIdLst>{ids}</p:sldIdLst></p:presentation>")
    return _zip(files)


def test_slide_order_follows_the_presentation_not_the_filenames():
    """Sorting the zip's names puts slide10/slide11/slide12 before slide2.
    The order in presentation.xml is the real one."""
    p = docpreview.preview(_pptx(12), "application/vnd.openxmlformats-"
                          "officedocument.presentationml.presentation")
    nums = [s["n"] for s in p["slides"]]
    assert nums == list(range(1, 13)), nums
    assert p["slides"][0]["title"] == "Slide 1"
    assert p["slides"][9]["title"] == "Slide 10"


def test_slide_titles_and_bullets_are_extracted():
    p = docpreview.preview(_pptx(3), "application/vnd.openxmlformats-"
                          "officedocument.presentationml.presentation")
    s = p["slides"][1]
    assert s["title"] == "Slide 2"
    assert "point 2a" in s["bullets"]
    assert s["empty"] is False
    assert not p["warnings"]


def test_a_deck_of_blank_slides_is_reported_as_a_warning():
    """This is the defect the preview exists to surface: 6 empty slides
    shipped as a clean success."""
    files = {f"ppt/slides/slide{i}.xml":
             (f'<p:sld xmlns:p="{PPT}" xmlns:a="{A}"><p:cSld><p:spTree/>'
              f"</p:cSld></p:sld>") for i in range(1, 7)}
    files["ppt/presentation.xml"] = (
        f'<p:presentation xmlns:p="{PPT}" xmlns:r="{R}"><p:sldIdLst>'
        + "".join(f'<p:sldId id="{255 + i}" r:id="rId{i}"/>'
                  for i in range(1, 7))
        + "</p:sldIdLst></p:presentation>")
    files["ppt/_rels/presentation.xml.rels"] = (
        f'<Relationships xmlns="{PKG_R}">'
        + "".join(f'<Relationship Id="rId{i}" Type="{R}/slide" '
                  f'Target="slides/slide{i}.xml"/>' for i in range(1, 7))
        + "</Relationships>")
    p = docpreview.preview(_zip(files), "application/vnd.openxmlformats-"
                          "officedocument.presentationml.presentation")
    assert len(p["slides"]) == 6
    assert all(s["empty"] for s in p["slides"])
    assert p["warnings"] and "no text" in p["warnings"][0]


def _xlsx(sheets: dict[str, list[list[str]]], active: int = 0) -> bytes:
    rels, sheet_tags, sheet_files = [], [], {}
    for i, (name, _rows) in enumerate(sheets.items(), 1):
        rels.append(f'<Relationship Id="rId{i}" Type="{R}/worksheet" '
                    f'Target="worksheets/sheet{i}.xml"/>')
        sheet_tags.append(f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>')
        sheet_files[f"xl/worksheets/sheet{i}.xml"] = _rows_xml(_rows)
    rels_body = "".join(rels)
    files = {
        "xl/workbook.xml":
            f'<workbook xmlns="{SS}" xmlns:r="{R}">'
            f'<bookViews><workbookView activeTab="{active}"/></bookViews>'
            f"<sheets>{''.join(sheet_tags)}</sheets></workbook>",
        "xl/_rels/workbook.xml.rels":
            f'<Relationships xmlns="{PKG_R}">{rels_body}</Relationships>',
        **sheet_files,
    }
    return _zip(files)


def _rows_xml(rows: list[list[str]]) -> str:
    body = []
    for r, row in enumerate(rows, 1):
        cells = "".join(
            f'<c r="{chr(65 + c)}{r}" t="inlineStr"><is><t>{v}</t></is></c>'
            for c, v in enumerate(row))
        body.append(f'<row r="{r}">{cells}</row>')
    data = "".join(body)
    return (f'<worksheet xmlns="{SS}"><sheetData>{data}'
            f"</sheetData></worksheet>")


def test_xlsx_rows_are_read_per_sheet():
    p = docpreview.preview(_xlsx({"Index Type": [["Feature", "IVF", "HNSW"],
                                                 ["Recall", "mid", "high"]]}),
                           "application/vnd.openxmlformats-officedocument."
                           "spreadsheetml.sheet")
    assert p["kind"] == "xlsx"
    assert len(p["sheets"]) == 1
    assert p["sheets"][0]["rows"][0] == ["Feature", "IVF", "HNSW"]
    assert p["sheets"][0]["empty"] is False


def test_an_empty_workbook_is_reported():
    """Verified against a real artifact: 0 cells, every sheet blank, and the
    run reported success."""
    p = docpreview.preview(_xlsx({"Sheet1": [], "Index Type": []}),
                           "application/vnd.openxmlformats-officedocument."
                           "spreadsheetml.sheet")
    assert all(s["empty"] for s in p["sheets"])
    assert p["warnings"] and "empty sheet" in p["warnings"][0]


def test_docx_paragraphs_and_tables():
    doc = (
        '<?xml version="1.0"?><w:document '
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        "<w:p><w:r><w:t>Opening paragraph with enough words to count.</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>B</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "<w:p><w:r><w:t>Closing paragraph.</w:t></w:r></w:p>"
        "</w:body></w:document>")
    p = docpreview.preview(_zip({"word/document.xml": doc}),
                           "application/vnd.openxmlformats-officedocument."
                           "wordprocessingml.document")
    assert p["kind"] == "docx"
    assert len(p["paragraphs"]) == 2
    assert p["tables"] and p["tables"][0][0] == ["A", "B"]


def test_a_thin_document_is_flagged():
    doc = ('<?xml version="1.0"?><w:document '
           'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           "<w:body><w:p><w:r><w:t>Too short.</w:t></w:r></w:p></w:body>"
           "</w:document>")
    p = docpreview.preview(_zip({"word/document.xml": doc}),
                           "application/vnd.openxmlformats-officedocument."
                           "wordprocessingml.document")
    assert p["warnings"], "a 2-word document should be flagged"


def test_unknown_bytes_are_reported_not_guessed():
    p = docpreview.preview(b"\x00\x01not a document", "application/octet-stream")
    assert p["kind"] == "unknown"
    assert p["warnings"]


def test_preview_never_raises_on_a_truncated_zip():
    blob = _pptx(3)
    p = docpreview.preview(blob[:len(blob) // 2],
                           "application/vnd.openxmlformats-officedocument."
                           "presentationml.presentation")
    assert p["kind"] in ("pptx", "unknown")
    assert isinstance(p.get("warnings", []), list)


@pytest.mark.parametrize("ct,expect", [
    ("application/pdf", "pdf"),
    ("application/vnd.openxmlformats-officedocument."
     "presentationml.presentation", "pptx"),
    ("application/vnd.openxmlformats-officedocument."
     "wordprocessingml.document", "docx"),
    ("application/vnd.openxmlformats-officedocument."
     "spreadsheetml.sheet", "xlsx"),
])
def test_kind_is_derived_from_the_content_type(ct, expect):
    p = docpreview.preview(b"%PDF-1.4 fake", ct)
    assert p["kind"] == expect