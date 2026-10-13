"""The fetch cap was the reason researchers returned so little.

`_extract_page` truncated at 20,000 chars MID-SENTENCE with a marker the
researcher never sees, so a long article silently arrived as its opening
paragraphs. The researcher prompt then claimed pages came back whole.
"""
import inspect

import mcp_server as mcp
import pytest


def test_the_default_is_above_any_real_article():
    sig = inspect.signature(mcp._extract_page)
    cap = sig.parameters["max_chars"].default
    assert cap >= 100_000, f"cap is {cap:,} - articles get cut again"


def test_the_docstring_no_longer_advertises_a_cap():
    doc = mcp._extract_page.__doc__ or ""
    assert "capped" not in doc.lower(), doc[:200]
    assert "truncat" in doc.lower()


def test_fetch_url_docstring_says_the_page_arrives_whole():
    doc = mcp.fetch_url.__doc__ or ""
    assert "capped at ~20k" not in doc, doc[:200]
    assert "complete" in doc.lower() or "whole" in doc.lower(), doc[:200]


def test_no_call_site_passes_the_old_cap():
    """A stale `max_chars=20000` at a call site silently restores the bug."""
    import pathlib
    src = pathlib.Path(mcp.__file__).read_text(encoding="utf-8")
    for bad in ("max_chars=20000", "max_chars = 20000"):
        assert bad not in src, f"{bad} still present"


@pytest.mark.asyncio
async def test_a_long_page_is_not_silently_shortened(monkeypatch):
    """A 120k-char page must arrive complete, and say so."""
    big = ("SECTION. " * 17000)          # ~136k chars, well past the old cap
    assert len(big) > 20000

    import trafilatura

    monkeypatch.setattr(trafilatura, "fetch_url", lambda u: "<html/>")
    monkeypatch.setattr(trafilatura, "extract",
                        lambda *a, **k: big)

    out = await mcp._extract_page("https://example.com/very-long", timeout_s=5)
    assert out["status"] == 200
    assert out["truncated"] is False, out["truncated"]
    assert len(out["text"]) > 100_000, len(out["text"])
    assert "TRUNCATED" not in out["text"]


@pytest.mark.asyncio
async def test_truncation_is_advertised_inside_the_text(monkeypatch):
    """A researcher reading only `text` must know it holds a partial page."""
    big = "x" * 400_000
    import trafilatura

    monkeypatch.setattr(trafilatura, "fetch_url", lambda u: "<html/>")
    monkeypatch.setattr(trafilatura, "extract", lambda *a, **k: big)

    out = await mcp._extract_page("https://example.com/huge", timeout_s=5)
    assert out["truncated"] is True
    assert "TRUNCATED" in out["text"], out["text"][-300:]
    # And it must tell the model what to do, not just that it happened.
    assert "caveats" in out["text"]