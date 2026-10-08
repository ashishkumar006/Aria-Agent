"""`GET /v1/documents/{id}/source` serves the stored upload bytes.

The console's inline preview fetches these bytes and renders them with
the browser's own viewer, so the route must hand back the original
media type, honour `Range` (a viewer streams a large PDF page-by-page
instead of buffering it whole), and 404 cleanly when the document or
its source file is gone.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import main as M           # noqa: E402  (the app the router is mounted on)
import memory_api as MA    # noqa: E402  (where the route lives)
from documents.registry import Registry  # noqa: E402

# Not a real PDF - the route serves stored bytes verbatim, so the
# content only has to be byte-identical to what was written.
PDF = b"%PDF-1.4\n" + b"x" * 100


def _wire(monkeypatch, tmp_path):
    """Register one PDF document against a temp registry."""
    reg = Registry(tmp_path / "documents")
    doc = reg.add(filename="menu.pdf", doc_type="pdf",
                  size_bytes=len(PDF))
    reg.write_source(doc.id, PDF)
    monkeypatch.setattr(MA, "_docs", lambda: reg)
    return TestClient(M.app), doc


def test_source_serves_full_body_with_media_type(monkeypatch, tmp_path):
    c, doc = _wire(monkeypatch, tmp_path)
    with c:
        r = c.get(f"/v1/documents/{doc.id}/source")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.headers["accept-ranges"] == "bytes"
    assert r.content == PDF


def test_source_honours_range_requests(monkeypatch, tmp_path):
    c, doc = _wire(monkeypatch, tmp_path)
    with c:
        r = c.get(f"/v1/documents/{doc.id}/source",
                  headers={"Range": "bytes=0-9"})
        assert r.status_code == 206
        assert r.headers["content-range"] == f"bytes 0-9/{len(PDF)}"
        assert r.content == PDF[:10]

        # Suffix range: the last 5 bytes.
        r2 = c.get(f"/v1/documents/{doc.id}/source",
                   headers={"Range": "bytes=-5"})
        assert r2.status_code == 206
        assert r2.headers["content-range"] == (
            f"bytes {len(PDF) - 5}-{len(PDF) - 1}/{len(PDF)}")
        assert r2.content == PDF[-5:]

        # Open-ended: everything from byte 4 on.
        r3 = c.get(f"/v1/documents/{doc.id}/source",
                   headers={"Range": "bytes=4-"})
        assert r3.status_code == 206
        assert r3.headers["content-range"] == f"bytes 4-{len(PDF) - 1}/{len(PDF)}"
        assert r3.content == PDF[4:]

        # An unparseable or unsatisfiable range falls back to the
        # full body rather than erroring - a viewer that asked for
        # something odd still gets the document.
        r4 = c.get(f"/v1/documents/{doc.id}/source",
                   headers={"Range": "bytes=abc"})
        assert r4.status_code == 200
        assert r4.content == PDF
        r5 = c.get(f"/v1/documents/{doc.id}/source",
                   headers={"Range": "bytes=99999999-"})
        assert r5.status_code == 200
        assert r5.content == PDF


def test_source_404s_for_missing_document_and_file(monkeypatch, tmp_path):
    c, doc = _wire(monkeypatch, tmp_path)
    with c:
        r = c.get("/v1/documents/doc-nope/source")
        assert r.status_code == 404
        assert "error" in r.json()

    # Registered, but the source file on disk is gone.
    (tmp_path / "documents" / f"{doc.id}.source").unlink()
    with c:
        r2 = c.get(f"/v1/documents/{doc.id}/source")
        assert r2.status_code == 404
        assert "error" in r2.json()
