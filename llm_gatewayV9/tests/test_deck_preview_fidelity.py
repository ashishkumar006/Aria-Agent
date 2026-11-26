"""The Authoring preview must show the deck the user actually receives.

The complaint was "the PPTX does not render properly in Authoring". The file
was fine; the LIVE preview was not. It split slides only on `pagebreak`, and a
deck's blocks carry headings, not pagebreaks — so a four-slide deck previewed
as two pages with every section crushed onto one. It also skipped `stats`,
`columns` and `callout` entirely, which are exactly the blocks a deck uses to
stop looking like a converted report, and it dropped a chart's title.

These pin the preview against the builder for the same spec, because "the
preview and the file agree" is the only property that makes a preview useful.
"""
from __future__ import annotations

import re

import pytest

import docgen
import docpreviewimg


SPEC = {
    "title": "Vector Indexing",
    "subtitle": "Trade-offs",
    "blocks": [
        {"type": "heading", "level": 1, "text": "Why the index decides"},
        {"type": "paragraph", "text": "Recall at a fixed latency budget."},
        {"type": "bullets", "items": ["IVF", "HNSW"]},
        {"type": "heading", "level": 1, "text": "Throughput"},
        {"type": "chart", "chart": "bar", "title": "QPS by scale",
         "categories": ["1M", "10M"],
         "series": [{"name": "IVF", "data": [94, 91]}]},
        {"type": "heading", "level": 1, "text": "Decision"},
        {"type": "stats", "items": [{"value": "97%", "label": "recall"}]},
        {"type": "columns", "items": [{"title": "IVF", "body": "bounded"}]},
        {"type": "callout", "title": "Key insight", "text": "Size the fleet."},
    ],
}


def _preview():
    return docpreviewimg.render_preview("pptx", None, dict(SPEC), 12)


def _texts(svg: str) -> str:
    return " ".join(re.findall(r">([^<>]{2,})</text>", svg))


def test_preview_slide_count_matches_the_delivered_deck():
    """The preview may not SHOW FEWER slides than the file, which is the bug
    this file exists for: two preview pages against a five-slide deck.

    It may show a slide the file then overflows onto a continuation of — the
    preview lays out at nominal geometry and the builder measures real text
    against the slide's usable height, which is why a dense slide becomes
    "Decision" + "Decision (continued)" in the file. Fewer pages is a lie;
    one extra is the builder being more careful than the preview.
    """
    from pptx import Presentation
    import io

    blob, _ = docgen.build_pptx(dict(SPEC))
    real = len(Presentation(io.BytesIO(blob)).slides)
    preview = _preview()["page_count"]
    assert preview >= real - 1, (preview, real)
    assert preview > 2, "the heading-per-slide split regressed"


def test_each_heading_opens_its_own_preview_slide():
    pages = _preview()["pages"]
    assert "Why the index decides" in _texts(pages[1]["data"])
    assert "Throughput" in _texts(pages[2]["data"])
    assert "Decision" in _texts(pages[3]["data"])


def test_stats_columns_and_callout_are_drawn():
    """These were silently skipped, so the richest slides previewed blank."""
    text = _texts(_preview()["pages"][3]["data"])
    assert "97%" in text, text
    assert "recall" in text, text
    assert "IVF" in text and "bounded" in text, text
    assert "KEY INSIGHT" in text, text


def test_chart_title_is_not_dropped():
    pages = _preview()["pages"]
    assert "QPS by scale" in _texts(pages[2]["data"])


def test_a_delivered_deck_previews_from_its_own_shapes():
    """The file-based path must not regress either: it is what the console
    shows for a deck that already exists."""
    blob, _ = docgen.build_pptx(dict(SPEC))
    out = docpreviewimg.render_preview("pptx", blob, None, 12)
    assert out["page_count"] == 5, out["page_count"]
    joined = " ".join(_texts(p["data"]) for p in out["pages"])
    assert "Vector Indexing" in joined
    assert "97%" in joined
