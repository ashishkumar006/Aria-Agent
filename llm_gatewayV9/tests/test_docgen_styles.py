"""Named document styles carry a visual identity, not just type metrics.

The original ten styles varied body size, leading, family and column count -
every one still opened with a left-aligned black title and black headings on
plain white. The three `chrome` styles below add a masthead, coloured section
heads and a cover band. These tests pin BOTH: the chrome actually changes the
render, and a style with no chrome is byte-for-byte the old behaviour.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import docgen  # noqa: E402
import doclayout  # noqa: E402


def _pdf(style, blocks=None):
    spec = {
        "format": "pdf", "style": style,
        "title": "Sequential Experimentation",
        "subtitle": "Exam revision notes",
        "toc": False,
        "blocks": blocks if blocks is not None else [
            {"type": "paragraph", "text": "Body text that must appear."},
            {"type": "heading", "level": 1, "text": "A Section"},
        ],
    }
    data, _ = docgen.build_pdf(spec)
    return data


def _text(pdf) -> str:
    import fitz
    d = fitz.open(stream=pdf, filetype="pdf")
    return "".join(p.get_text() for p in d)


def _colors(pdf) -> set:
    """Distinct fill colours actually painted on page 1."""
    import fitz
    d = fitz.open(stream=pdf, filetype="pdf")
    out = set()
    for dr in d[0].get_drawings():
        for key in ("fill", "color"):
            c = dr.get(key)
            if c:
                out.add(tuple(round(v, 2) for v in c))
    return out


# ── the catalogue ───────────────────────────────────────────────────────────

def test_the_three_identity_styles_exist():
    styles = doclayout.DOC_STYLES
    for name in ("lecture", "exambook", "coursework"):
        assert name in styles, f"{name} missing from the style catalogue"


def test_identity_styles_declare_chrome():
    for name in ("lecture", "exambook", "coursework"):
        chrome = doclayout.DOC_STYLES[name].get("chrome")
        assert isinstance(chrome, dict) and chrome.get("masthead"), \
            f"{name} has no chrome.masthead"


def test_original_styles_declare_no_chrome():
    """The ten original styles must keep rendering exactly as before."""
    for name in ("report", "academic", "brief", "whitepaper", "newsletter",
                 "memo", "manual", "technical", "book", "plain"):
        assert not doclayout.DOC_STYLES[name].get("chrome"), \
            f"{name} gained chrome; that changes every existing document"


def test_every_cli_style_resolves():
    for name in doclayout.DOC_STYLES:
        got = doclayout.resolve_style(name)
        assert got and got.get("body"), name


# ── the render actually changes ─────────────────────────────────────────────

def test_coursework_paints_a_cover_band():
    """The navy band is a real filled rectangle on page one."""
    pdf = _pdf("coursework")
    fills = _colors(pdf)
    # #16233c -> (0.09, 0.14, 0.24)
    assert any(abs(c[0] - 0.086) < 0.06 and abs(c[2] - 0.235) < 0.08
               for c in fills if len(c) >= 3), \
        f"no navy cover band found among {sorted(fills)[:8]}"


def test_coursework_prints_the_title_in_white_on_the_band():
    """White display type on the band, not black type below it."""
    import fitz
    pdf = _pdf("coursework")
    d = fitz.open(stream=pdf, filetype="pdf")
    spans = []
    for blk in d[0].get_text("dict").get("blocks", []):
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                spans.append(sp)
    title = [s for s in spans if "Sequential" in s.get("text", "")]
    assert title, "the title is missing from the cover"
    # White ink is 1,1,1 in the span colour int (0xFFFFFF).
    assert any(s.get("color") == 0xFFFFFF for s in title), \
        "the cover title is not set in white on the band"


def test_lecture_centres_its_masthead():
    import fitz
    pdf = _pdf("lecture")
    d = fitz.open(stream=pdf, filetype="pdf")
    page = d[0]
    blocks = [b for b in page.get_text("dict").get("blocks", [])
              if b.get("lines")]
    title_blk = next((b for b in blocks
                      if "Sequential" in " ".join(
                          s.get("text", "") for ln in b["lines"]
                          for s in ln.get("spans", []))), None)
    assert title_blk, "title block not found"
    x0, x1 = title_blk["bbox"][0], title_blk["bbox"][2]
    mid = (x0 + x1) / 2
    assert abs(mid - page.rect.width / 2) < page.rect.width * 0.12, \
        f"title is not centred (mid {mid:.0f} vs page {page.rect.width / 2:.0f})"


def test_exambook_headings_use_the_accent_colour():
    import fitz
    pdf = _pdf("exambook")
    d = fitz.open(stream=pdf, filetype="pdf")
    dk = doclayout.DOC_STYLES["exambook"]
    accent = dk["accent"].lstrip("#")
    want = tuple(int(accent[i:i + 2], 16) for i in (0, 2, 4))
    found = set()
    for pno in range(d.page_count):
        for blk in d[pno].get_text("dict").get("blocks", []):
            for ln in blk.get("lines", []):
                for sp in ln.get("spans", []):
                    if "A Section" in sp.get("text", "").strip():
                        c = sp["color"]
                        found.add(((c >> 16) & 255, (c >> 8) & 255, c & 255))
    assert want in found, f"heading colour {want} not among {found}"


# ── the callout block ───────────────────────────────────────────────────────

def test_callout_survives_normalisation():
    got = docgen._norm_blocks([
        {"type": "callout", "title": "Key insight", "tone": "warning",
         "text": "the trap"}])
    assert got and got[0]["type"] == "callout"
    assert got[0]["title"] == "Key insight"
    assert got[0]["tone"] == "warning"


def test_insight_alias_reaches_callout():
    got = docgen._norm_blocks([
        {"type": "insight", "title": "T", "text": "b"}])
    assert got and got[0]["type"] == "callout"


def test_empty_callout_is_dropped():
    assert docgen._norm_blocks([{"type": "callout"}]) == []


def test_callout_prints_its_title_and_text():
    pdf = _pdf("report", blocks=[
        {"type": "callout", "title": "Key insight",
         "text": "A saturated design buys information per run."}])
    text = _text(pdf)
    assert "KEY INSIGHT" in text
    assert "saturated design buys information" in text


def test_callout_box_is_drawn_with_its_tone_colour():
    """A warning callout paints the rust bar, not the deck accent."""
    pdf = _pdf("report", blocks=[
        {"type": "callout", "title": "Exam alert", "tone": "warning",
         "text": "State the alias structure."}])
    fills = _colors(pdf)
    # #b3261e -> (0.70, 0.15, 0.12)
    assert any(abs(c[0] - 0.70) < 0.08 and abs(c[1] - 0.15) < 0.08
               for c in fills if len(c) >= 3), \
        f"warning tone colour not painted: {sorted(fills)[:8]}"


# ── other formats still carry the words ─────────────────────────────────────

def test_docx_keeps_callout_text():
    data, _ = docgen.build_docx({
        "format": "docx", "title": "T",
        "blocks": [{"type": "callout", "title": "Key insight",
                     "text": "keep this sentence"}]})
    import io
    import docx
    d = docx.Document(io.BytesIO(data))
    body = "\n".join(p.text for p in d.paragraphs)
    assert "KEY INSIGHT" in body and "keep this sentence" in body


def test_pptx_renders_callout():
    data, _ = docgen.build_pptx({
        "title": "T", "blocks": [
            {"type": "callout", "title": "Key insight",
             "text": "keep this sentence"}]})
    import io
    from pptx import Presentation
    prs = Presentation(io.BytesIO(data))
    txt = "\n".join(sh.text_frame.text for s in prs.slides
                    for sh in s.shapes if sh.has_text_frame)
    assert "KEY INSIGHT" in txt and "keep this sentence" in txt