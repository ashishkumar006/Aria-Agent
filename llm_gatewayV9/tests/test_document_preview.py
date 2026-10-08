import asyncio
import json
"""The console must be able to SEE the document, not describe it.

The authoring pane used to show extracted text with a comment explaining that
an inline PDF viewer was impossible in the browser. Both halves of that were
wrong: the pages are rasterised server-side, so no browser plugin is needed,
and a text outline cannot show whether the margins, the measure or the chart
are right - which is the thing being judged.
"""
import io
import zipfile

import pytest

import docgen
import docpreviewimg as P


SPEC = {
    "title": "Aqueduct Engineering", "subtitle": "What the evidence shows",
    "style": "academic", "page_size": "letter", "toc": True,
    "blocks": [
        {"type": "cover", "title": "Aqueduct Engineering",
         "subtitle": "What the evidence shows", "meta": ["Layout preview"]},
        {"type": "heading", "level": 1, "text": "Hydraulic design"},
        {"type": "paragraph", "text": "A gradient of 1 in 200. " * 30},
        {"type": "bullets", "items": ["Specus", "Pons"]},
        {"type": "chart", "kind": "bar", "categories": ["A", "B", "C"],
         "series": [{"name": "s", "data": [3, 5, 2]}]},
        {"type": "table", "header": ["K", "V"],
         "rows": [["a", "1"], ["b", "2"]]},
    ],
    "references": [{"authors": "Zhang, Jane", "year": "2015",
                    "title": "A study", "container": "J X"}],
    "citation_style": "apa",
}


def test_pdf_previews_as_real_page_images():
    out = P.render_preview("pdf", None, SPEC)
    assert out["mode"] == "pages"
    assert out["pages"], "no pages rendered"
    p = out["pages"][0]
    assert p["kind"] == "png"
    assert p["data"].startswith("data:image/png;base64,")
    # Letter, not A4: the preview must reflect the requested paper.
    assert abs(p["width_pt"] - 612) < 2, p["width_pt"]
    assert len(p["data"]) > 3000, "suspiciously small page image"


def test_pptx_previews_as_drawn_slides():
    out = P.render_preview("pptx", None, SPEC)
    assert out["mode"] == "pages"
    assert out["pages"][0]["kind"] == "svg"
    assert out["pages"][0]["data"].startswith("<svg")
    assert "Aqueduct Engineering" in out["pages"][0]["data"]
    # Honesty: it is a layout preview, not a raster of the .pptx.
    assert out.get("note")


def test_docx_preview_comes_from_the_saved_file():
    blob, _n = docgen.build_docx(SPEC)
    out = P.render_preview("docx", blob, None)
    assert out["mode"] == "html"
    assert "Hydraulic design" in out["html"]
    assert "<table" in out["html"], "the real table is missing"


def test_xlsx_preview_comes_from_the_saved_file():
    blob, _n = docgen.build_xlsx({
        "title": "Comparison",
        "sheets": [{"name": "Index", "header": ["K", "Recall", "Memory"],
                    "rows": [["IVF", "moderate", "low"],
                             ["HNSW", "high", "high"]]}]})
    out = P.render_preview("xlsx", blob, None)
    assert "HNSW" in out["html"]
    assert "<td" in out["html"]


def test_a_preview_of_a_delivered_artifact_uses_its_bytes():
    """Previewing the artefact must rasterise the delivered file, not
    re-render a spec - otherwise the preview can disagree with the download."""
    blob, _n = docgen.build_pdf({**SPEC, "page_size": "a5"})
    out = P.render_preview("pdf", blob, None)
    assert abs(out["pages"][0]["width_pt"] - 420) < 3, \
        "preview did not use the delivered page size"


def test_preview_page_count_is_bounded():
    long_doc = {"title": "Long", "blocks": [
        {"type": "paragraph", "text": "Filler. " * 400} for _ in range(6)]}
    out = P.render_preview("pdf", None, long_doc, max_pages=3)
    assert len(out["pages"]) <= 3
    assert out["truncated"] is True
    assert out["page_count"] > 3, "the real page count should still be shown"


def test_an_unpreviewable_format_is_refused_not_guessed():
    with pytest.raises(ValueError):
        P.render_preview("exe", None, SPEC)


# ── the API surface ────────────────────────────────────────────────────────
def test_the_route_is_advertised_in_the_openapi_schema():
    import docgen_api
    paths = {getattr(r, "path", None) for r in docgen_api.router.routes}
    assert "/v1/docgen/preview" in paths


def test_the_preview_route_rejects_a_traversal_handle():
    """The artifact handle reaches this route and becomes a file path, so its
    shape has to be checked before anything else.

    Mounted on the REAL router: a hand-rolled stand-in with an untyped
    Request answers 422 for the wrong reason and proves nothing.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import docgen_api

    app = FastAPI()
    # No token middleware here, so the route sees an empty header and treats
    # the request as unauthenticated only at the gateway edge - which is not
    # what is under test.
    app.include_router(docgen_api.router)
    c = TestClient(app)

    bad = c.post("/v1/docgen/preview",
                 json={"artifact": "art:../../etc/passwd", "format": "pdf"})
    assert bad.status_code == 400, bad.text[:200]
    assert "malformed" in bad.json().get("error", "").lower()

    for shape in ("art:", "art:zz", "art:/etc/passwd",
                  "art:0123456789abcdefEXTRA", "documents/art:0123456789abcdef"):
        r = c.post("/v1/docgen/preview",
                   json={"artifact": shape, "format": "pdf"})
        assert r.status_code == 400, f"{shape} -> {r.status_code}"

def test_a_delivered_deck_previews_from_its_own_bytes():
    """A delivered .pptx arrives with no spec. Previewing an empty spec gave
    a blank box labelled "0 slides" under a file that really had three."""
    import docgen
    import docpreviewimg

    blob, _n = docgen.build_pptx({
        "title": "Resilience Fallbacks",
        "subtitle": "A short deck",
        "blocks": [
            {"type": "pagebreak"},
            {"type": "heading", "level": 1, "text": "First finding"},
            {"type": "bullet", "text": "Retries must be bounded"},
            {"type": "pagebreak"},
            {"type": "heading", "level": 1, "text": "Second finding"},
            {"type": "bullet", "text": "Fail loudly, not silently"},
        ]})

    out = docpreviewimg.render_preview("pptx", blob, None)
    assert out["mode"] == "pages", out["mode"]
    assert len(out["pages"]) == 3, len(out["pages"])
    assert all(p.get("data") for p in out["pages"]), "a slide rendered blank"
    assert "Resilience Fallbacks" in out["pages"][0]["data"]
    assert "bounded" in out["pages"][1]["data"]
def test_the_artifact_request_shape_does_not_swallow_the_bytes():
    """A real artifact request is {artifact, format, blob_b64}.

    The endpoint derived `spec` from the whole body minus a few keys, so the
    "spec" contained blob_b64, so it was non-empty, so the pptx path never
    rebuilt a spec from the bytes - and every delivered deck previewed as
    "0 slides". This is the shape the console actually sends.
    """
    import base64
    import docgen
    import docgen_api
    from fastapi import Request

    blob, _n = docgen.build_pptx({
        "title": "Energy Retrofit",
        "blocks": [
            {"type": "pagebreak"},
            {"type": "heading", "level": 1, "text": "LED savings"},
            {"type": "bullet", "text": "Payback in 18 months"},
        ]})

    body = {"artifact": "art:0123456789abcdef", "format": "pptx",
            "blob_b64": base64.b64encode(blob).decode()}
    payload = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": payload,
                "more_body": False}

    req = Request({"type": "http", "method": "POST",
                   "path": "/v1/docgen/preview",
                   "headers": [(b"content-type", b"application/json")]},
                  receive)
    resp = asyncio.new_event_loop().run_until_complete(
        docgen_api.docgen_preview(req))
    raw = b''
    if hasattr(resp, 'body'):
        raw = resp.body
    out = json.loads(raw) if raw else {}

    assert out["page_count"] >= 2, out
    assert any("Payback" in (p.get("data") or "")
               for p in out["pages"]), "the delivered text did not render"