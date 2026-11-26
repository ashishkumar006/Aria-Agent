"""The console's PPTX preview must show the FILE, not a second guess at it.

Found live: a delivered deck whose cover is a split panel (gradient block, edge
bar, title rule) and whose content slides carry 19 shapes each previewed as a
**white page with a 6pt bar at the top**. The previewer walked the block spec
and re-drew it with its own layout function, so it could not know about any of
the deck's chrome - and a preview that disagrees with the file is worse than
none, because it is the thing the user checks the file against.
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import docgen  # noqa: E402
import docpreviewimg as P  # noqa: E402

BLOCKS = [
    {"type": "heading", "level": 1, "text": "Performance Metrics"},
    {"type": "stats", "items": [
        {"value": "250-300 Wh/kg", "label": "current Li-ion"},
        {"value": "400-500 Wh/kg", "label": "solid-state target"},
    ]},
    {"type": "columns", "items": [
        {"title": "Sulfide", "body": "High conductivity, moisture sensitive."},
        {"title": "Oxide", "body": "Stable, brittle, high interface resistance."},
    ]},
]


def _deck_bytes() -> bytes:
    data, _ = docgen.build_pptx({"title": "Solid-State Batteries",
                                 "subtitle": "Engineering outlook",
                                 "slide_size": "16:9", "blocks": BLOCKS})
    return data


def _svg(blob, idx=0):
    out = P.render_preview("pptx", blob, None, 12)
    assert out.get("mode") == "pages", out
    return out, out["pages"][idx]["data"]


# ── the preview reads the file ──────────────────────────────────────────────

def test_preview_reports_that_it_rendered_the_shapes():
    out, _ = _svg(_deck_bytes())
    assert "own shapes" in str(out.get("note") or ""), \
        f"preview did not use the shape renderer: {out.get('note')!r}"


def test_the_cover_panel_is_drawn():
    """The deck's gradient side panel used to vanish - it is the signature."""
    _, svg = _svg(_deck_bytes())
    # The panel is a filled rect occupying the right third, not a 6pt bar.
    widths = [float(w) for w in re.findall(r'<rect[^>]*width="([0-9.]+)"', svg)]
    assert any(w > 200 for w in widths), \
        f"no full-height panel rect in the cover: {sorted(widths)[:6]}"
    assert 'height="6"' not in svg, "the old synthetic 6pt bar is still drawn"


def test_text_keeps_the_files_own_font_sizes():
    """Sizes come from the run, so a 34pt stat figure stays 34pt."""
    _, svg = _svg(_deck_bytes(), idx=1)
    sizes = {float(x) for x in re.findall(r'font-size="([0-9.]+)"', svg)}
    assert sizes, "no text rendered at all (the 0pt regression)"
    assert 0.0 not in sizes, f"a run rendered at 0pt: {sorted(sizes)}"
    assert any(s >= 20 for s in sizes), \
        f"stat values lost their display size: {sorted(sizes)}"


def test_every_slide_becomes_a_page():
    """The preview's page count must equal the FILE's slide count.

    Asserting a hardcoded number pins my guess at how the deck paginates; the
    contract that matters is that the two agree.
    """
    blob = _deck_bytes()
    out, _ = _svg(blob)
    from pptx import Presentation
    real = len(Presentation(io.BytesIO(blob)).slides)
    assert out["page_count"] == real, \
        f"preview has {out['page_count']} pages, file has {real} slides"


def test_the_geometry_scale_is_not_applied_to_font_sizes():
    """EMU->SVG scaling on a pt size multiplied 40pt by ~8e-5 and drew 0.0.

    A 16:9 slide is 960pt wide, so the type scale is 1.0 and a 40pt title must
    render at 40, not 0.003.
    """
    _, svg = _svg(_deck_bytes())
    sizes = [float(x) for x in re.findall(r'font-size="([0-9.]+)"', svg)]
    assert any(abs(s - 40.0) < 0.6 for s in sizes), \
        f"cover title not at its 40pt size: {sorted(set(sizes))[:8]}"


def test_a_4_3_deck_keeps_its_type_sizes():
    """The canvas ratio is 1.0 on 16:9, so only a narrower slide exposes a
    double conversion: on 4:3 the geometry scale is 1.33 and a 40pt title
    would render at 53pt if it were applied to the type too.
    """
    blob, _ = docgen.build_pptx({"title": "Four Three", "slide_size": "4:3",
                                 "blocks": [{"type": "paragraph",
                                             "text": "Body copy."}]})
    out = P.render_preview("pptx", blob, None, 12)
    drawn = {round(float(x), 1) for x in
             re.findall(r'font-size="([0-9.]+)"', out["pages"][0]["data"])}
    from pptx import Presentation
    real = set()
    for sh in Presentation(io.BytesIO(blob)).slides[0].shapes:
        if sh.has_text_frame:
            for para in sh.text_frame.paragraphs:
                for r in para.runs:
                    if r.font.size is not None:
                        real.add(round(float(r.font.size.pt), 1))
    assert drawn <= real,         f"4:3 preview scaled the type: drew {sorted(drawn)} vs file {sorted(real)}"


# ── the live layout path still works ────────────────────────────────────────

def test_a_spec_preview_still_renders_without_bytes():
    """The authoring panel previews a spec before any file exists."""
    out = P.render_preview("pptx", None, {"title": "Draft", "blocks": BLOCKS})
    assert out.get("mode") == "pages" and out["page_count"] >= 1
    assert "<svg" in out["pages"][0]["data"]


# ── nothing is dropped ──────────────────────────────────────────────────────

def test_every_block_type_appears_in_the_preview_text():
    """A preview that silently skips a layout is the bug being prevented."""
    blob, _ = docgen.build_pptx({
        "title": "T", "slide_size": "16:9",
        "blocks": [
            {"type": "heading", "level": 1, "text": "Heading marker"},
            {"type": "paragraph", "text": "Paragraph marker"},
            {"type": "bullets", "items": ["Bullet marker"]},
            {"type": "stats", "items": [{"value": "42", "label": "Stat marker"}]},
            {"type": "columns", "items": [{"title": "Column marker",
                                            "body": "col body"}]},
            {"type": "callout", "title": "Callout marker", "text": "call body"},
            {"type": "table", "header": ["H"], "rows": [["Table marker"]]},
        ],
    })
    out = P.render_preview("pptx", blob, None, 24)
    text = " ".join(p["data"] for p in out["pages"])
    # A deck with seven blocks is paginated by the builder, so a block may sit
    # on a later slide - what must hold is that no block type vanishes. The
    # callout's title is UPPERCASED on the slide by design, so the comparison
    # is case-insensitive.
    from pptx import Presentation

    def _all_text(slide) -> list[str]:
        """Text frames AND table cells.

        A table's content lives in cells, not a text frame, so reading only
        `has_text_frame` made a perfectly good table look absent from the file.
        """
        out = []
        for sh in slide.shapes:
            if getattr(sh, "has_table", False):
                out.extend(c.text for r in sh.table.rows for c in r.cells)
            elif sh.has_text_frame:
                out.append(sh.text_frame.text)
        return out

    real_text = " ".join(
        t for sl in Presentation(io.BytesIO(blob)).slides
        for t in _all_text(sl)).lower()
    low = text.lower()
    for marker in ("Heading marker", "Paragraph marker", "Bullet marker",
                   "Stat marker", "Column marker", "Callout marker",
                   "Table marker"):
        assert marker.lower() in real_text, \
            f"{marker!r} missing from the FILE itself"
        assert marker.lower() in low, f"{marker!r} missing from the preview"