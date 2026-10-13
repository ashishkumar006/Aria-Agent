"""Reproduce the blank-shell document, in-process, without a gateway.

`art:4da2e5f19c01a00d` was 4 pages / 9 words: cover, TOC, blank page,
references. The author passed ONE block - a cover - and the renderer reported
success. These pin the guard that now refuses it.
"""
import pytest

import mcp_server as mcp


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail loudly if a guard let a shell document reach the gateway."""
    def _boom(*a, **k):
        raise AssertionError("a content-free document reached /v1/docgen")
    monkeypatch.setattr(mcp.httpx, "Client", _boom)


CONTENT = [
    {"type": "heading", "level": 1, "text": "Section"},
    {"type": "paragraph", "text": "Real prose with actual content in it."},
]


class _FakeResp:
    status_code = 200
    content = b"%PDF-1.4 fake"
    headers = {"content-disposition": 'attachment; filename="a.pdf"'}


def _fake_gateway(monkeypatch, resp=None):
    """Capture what would be sent to /v1/docgen instead of sending it."""
    sent = {}
    resp = resp or _FakeResp()

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, **k):
            sent["url"] = url
            sent["spec"] = json["spec"]
            return resp

    monkeypatch.setattr(mcp.httpx, "Client", _Client)
    return sent


def test_the_reproduced_cover_only_document_is_refused():
    """The exact shape from the failing run: one cover block, 9 words."""
    out = mcp.render_document(
        "pdf", "Retrieval-Augmented Generation (RAG)",
        blocks=[{"type": "cover", "title": "Retrieval-Augmented Generation",
                 "meta": ["A report"]}],
        toc=True)
    assert out.get("ok") is False, out
    err = out.get("error", "")
    assert "no content blocks" in err, err
    # It has to be actionable, or the model retries the same empty call.
    assert "Call render_document again" in err, err
    assert "paragraph" in err, err


@pytest.mark.parametrize("chrome", [
    [{"type": "cover", "title": "X"}],
    [{"type": "pagebreak"}],
    [{"type": "heading", "level": 1, "text": "Only a heading"}],
    [{"type": "cover", "title": "X"}, {"type": "pagebreak"},
     {"type": "heading", "level": 1, "text": "H"}],
    [],
])
def test_documents_made_only_of_chrome_are_refused(chrome):
    out = mcp.render_document("pdf", "Title", blocks=chrome, toc=True)
    assert out.get("ok") is False, (chrome, out)


def test_the_error_names_what_was_actually_passed():
    out = mcp.render_document("pdf", "T",
                              blocks=[{"type": "cover"}, {"type": "pagebreak"}])
    assert "cover" in out.get("error", ""), out
    assert "pagebreak" in out.get("error", ""), out


@pytest.mark.parametrize("body", [
    [{"type": "paragraph", "text": "prose"}],
    [{"type": "bullets", "items": ["a", "b"]}],
    [{"type": "numbers", "items": ["a", "b"]}],
    [{"type": "table", "header": ["A", "B"], "rows": [["1", "2"]]}],
    [{"type": "quote", "text": "quoted"}],
    [{"type": "chart", "chart": "bar", "categories": ["x"],
     "series": [{"name": "s", "data": [1]}]}],
    [{"type": "note", "text": "a note"}],
])
def test_a_single_real_content_block_is_enough(body, monkeypatch):
    """The guard must not become a new cap - one paragraph is a valid call."""
    sent = _fake_gateway(monkeypatch)
    out = mcp.render_document("pdf", "T", blocks=body)
    assert out.get("ok") is True, out
    assert sent["spec"]["blocks"] == body


def test_a_cover_alongside_real_content_still_renders(monkeypatch):
    """The guard rejects SHELLS, not covers. A titled report is legitimate."""
    sent = _fake_gateway(monkeypatch)
    blocks = [{"type": "cover", "title": "X"}, {"type": "pagebreak"}] + CONTENT
    out = mcp.render_document("pdf", "T", blocks=blocks, toc=True)
    assert out.get("ok") is True, out
    # cover retained, toc retained - there IS something to index.
    assert any(b["type"] == "cover" for b in sent["spec"]["blocks"])
    assert sent["spec"].get("toc") is True


def test_sheets_still_work_without_blocks(monkeypatch):
    """xlsx has no blocks, so the body check must not apply to it."""
    sent = _fake_gateway(monkeypatch)
    out = mcp.render_document("xlsx", "T", blocks=[],
                              sheets=[{"name": "S", "header": ["A"],
                                       "rows": [["1"]]}])
    assert out.get("ok") is True, out


def test_doc_stats_agree_that_a_shell_is_empty():
    """The stats the model reads must match what the guard enforces."""
    shell = {"title": "Retrieval-Augmented Generation",
             "toc": True,
             "blocks": [{"type": "cover",
                         "title": "Retrieval-Augmented Generation"}]}
    st = mcp._doc_stats(shell, "pdf")
    assert st["words"] < 20, st
    assert st["sections"] == 0, st
    real = {"title": "T", "blocks": CONTENT}
    assert mcp._doc_stats(real, "pdf")["sections"] == 1