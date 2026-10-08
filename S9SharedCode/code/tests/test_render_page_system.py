"""The page and type system has to be reachable from the tool.

The generator supported none of this until this wave, and the tool advertised
none of it either - so even a model that wanted a Letter page in a serif
academic style had no way to ask. These are the parameters that carry it.
"""
import json

import pytest


def _tool():
    import skills
    return skills._TOOL_CATALOG["render_document"]


def test_the_tool_offers_the_page_system():
    props = _tool()["input_schema"]["properties"]
    for key in ("page_size", "orientation", "margins", "columns", "style",
                "citation_style", "references", "slide_size", "toc",
                "running_header"):
        assert key in props, f"render_document does not accept {key}"


def test_the_style_enum_is_the_real_one():
    """A list the tool advertises but the generator rejects is worse than no
    list: the model picks it and the render fails."""
    import sys
    sys.path.insert(0, r"C:\Users\AISHWARYA\Downloads\project3\llm_gatewayV9")
    import doclayout as L
    props = _tool()["input_schema"]["properties"]
    for enum_key, table in (("style", L.DOC_STYLES),
                            ("citation_style", L.CITATION_STYLES),
                            ("margins", L.MARGIN_PRESETS),
                            ("page_size", L.PAGE_SIZES)):
        advertised = set(props[enum_key]["enum"])
        assert advertised, enum_key
        # Every advertised name must resolve in the generator.
        for name in advertised:
            assert name in table, f"{enum_key}: {name} is not in doclayout"


def test_the_prompt_tells_the_model_to_choose():
    md = open("prompts/author.md", encoding="utf-8").read()
    assert "citation_style" in md
    assert "references" in md
    assert "page_size" in md
    # And that choosing is a decision, not an afterthought.
    assert "deliberately" in md.lower() or "choose" in md.lower()


def _render(**kw):
    """Call the real tool with a stubbed gateway, and return the spec sent."""
    import mcp_server

    sent = {}

    class _Resp:
        status_code = 200
        content = b"%PDF-1.4 fake"
        headers = {"content-disposition": 'attachment; filename="t.pdf"',
                   "content-type": "application/pdf"}

        def json(self):
            return {}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, **kw):
            sent.update(json or {})
            return _Resp()

    import httpx
    orig = httpx.Client
    httpx.Client = lambda *a, **k: _Client()
    try:
        out = mcp_server.render_document(**kw)
    finally:
        httpx.Client = orig
    return out, sent


def test_page_and_type_fields_reach_the_gateway():
    out, sent = _render(
        format="pdf", title="Report",
        blocks=[{"type": "paragraph", "text": "x"}],
        page_size="letter", orientation="landscape", margins="wide",
        columns=2, style="academic", citation_style="ieee",
        references=[{"title": "A source", "year": "2020"}],
        toc=True, running_header="My Report")
    assert out.get("ok") is True, out
    assert sent["format"] == "pdf"
    assert sent["spec"]["page_size"] == "letter"
    assert sent["spec"]["orientation"] == "landscape"
    assert sent["spec"]["margins"] == "wide"
    assert sent["spec"]["columns"] == 2
    assert sent["spec"]["style"] == "academic"
    assert sent["spec"]["citation_style"] == "ieee"
    assert sent["spec"]["toc"] is True
    assert sent["spec"]["running_header"] == "My Report"
    assert sent["spec"]["references"][0]["title"] == "A source"


def test_optional_page_fields_are_omitted_rather_than_sent_empty():
    _out, sent = _render(format="pdf", title="T",
                         blocks=[{"type": "paragraph", "text": "x"}])
    spec = sent["spec"]
    for key in ("page_size", "orientation", "margins", "style",
                "citation_style", "slide_size", "references"):
        assert key not in spec, f"{key} was sent as an empty value"


def test_columns_are_clamped_not_trusted():
    _out, sent = _render(format="pdf", title="T",
                         blocks=[{"type": "paragraph", "text": "x"}],
                         columns=99)
    assert 1 <= sent["spec"]["columns"] <= 3


def test_the_harness_render_fallback_forwards_the_page_system():
    """When the model writes a spec instead of calling the tool, the harness
    renders it - and it must carry the same fields or a Letter report silently
    comes back as A4."""
    src = open("skills.py", encoding="utf-8").read()
    for field in ("page_size=", "orientation=", "margins=", "style=",
                  "citation_style=", "slide_size=", "references=",
                  "columns="):
        assert field in src, f"the spec fallback drops {field}"