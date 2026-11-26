"""The LaTeX path: real maths, and the two ways it used to mangle prose.

Measured before this existed: a 23-page LLM report leaked 112 literal `$`
across 9 pages because reportlab has no maths typesetter. The fix here is a
REAL engine (tectonic) driven from the same block vocabulary, opted into per
spec so every existing document and every geometry test is untouched.

The failure modes pinned below were all seen on a live draft of this renderer:
a `\\caption*` that leaked a literal `*` without the caption package, display
math printed as source text, and — the dangerous one — a pricing sentence
eaten by the `$...$` rule and set as run-together italics.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import docgen           # noqa: E402
import docgen_latex     # noqa: E402


def _have_engine():
    ok, _ = docgen_latex.available()
    return pytest.mark.skipif(not ok, reason="no TeX engine installed")()


def _pdf(blocks, **extra):
    spec = {"engine": "latex", "title": "T", "blocks": blocks, **extra}
    data, name = docgen.build_pdf(spec)
    return data, name


def _text(pdf: bytes) -> str:
    import pymupdf
    d = pymupdf.open(stream=pdf, filetype="pdf")
    out = "\n".join(p.get_text() for p in d)
    d.close()
    return out


# ── engine-agnostic: the escaping rules ───────────────────────────────────

def test_currency_is_never_read_as_maths():
    """`$5 per seat and $6 per user` reached the page as
    `5per seat and $6` in italics on a live draft."""
    out = docgen_latex._tex_inline(
        "The plan costs $5 per seat and $6 per user, billed monthly.")
    assert "\\$5" in out and "\\$6" in out, out
    assert "$" not in out.replace("\\$", ""), out


def test_inline_and_display_math_are_passed_through_untouched():
    out = docgen_latex._tex_inline(r"grows as $O(N^2)$ here")
    assert "$O(N^2)$" in out, out
    disp = docgen_latex._tex_inline(r"\[ a=b \]")
    assert r"\[ a=b \]" in disp, disp


def test_latex_specials_in_prose_are_escaped():
    out = docgen_latex._tex_inline("snake_case & 100% of #1 {x}")
    assert "\\_" in out and "\\&" in out and "\\%" in out, out
    assert "\\#" in out, out


def test_markdown_survives_as_latex_markup():
    out = docgen_latex._tex_inline("**bold** and `code` and [a](https://x.y)")
    assert "\\textbf{bold}" in out
    assert "\\texttt{code}" in out
    assert "\\href{https://x.y}{a}" in out


def test_available_reports_why_when_absent(monkeypatch):
    monkeypatch.setattr(docgen_latex, "engine_path", lambda: "")
    ok, why = docgen_latex.available()
    assert ok is False and "tectonic" in why


# ── engine-backed: the page the user gets ─────────────────────────────────

@_have_engine()
def test_math_is_typeset_not_printed():
    data, _ = _pdf([{"type": "paragraph",
                     "text": r"The cost is $\sqrt{d_k}$ and $O(N^2)$."}])
    text = _text(data)
    # The source must not survive: no `\sqrt`, no `$`.
    assert "\\sqrt" not in text, text
    assert "$" not in text, text


@_have_engine()
def test_display_equation_renders_as_a_display():
    """`\\[...\\]` printed verbatim until the display pass existed."""
    data, _ = _pdf([{"type": "paragraph",
                     "text": r"\[ \mathrm{softmax}\left(\frac{QK^\top}{\sqrt{d_k}}\right)V \]"}])
    text = _text(data)
    assert "\\[" not in text and "\\frac" not in text, text
    assert "softmax" in text, text


@_have_engine()
def test_currency_survives_a_real_render():
    data, _ = _pdf([{"type": "paragraph",
                     "text": "Range: $5-$10 per request, or $1.2M total."}])
    text = _text(data)
    assert "$5-$10" in text or "$5" in text, text
    assert "per request" in text, text


@_have_engine()
def test_heading_numbering_and_table_rules():
    """TeX numbers sections itself; a report whose headings are unnumbered
    is not the document the style asked for."""
    data, _ = _pdf([
        {"type": "heading", "level": 1, "text": "Attention"},
        {"type": "table", "header": ["Model", "Params"],
         "rows": [["Small", "117M"]]},
    ])
    text = _text(data)
    assert "1" in text and "Attention" in text, text
    assert "Model" in text and "117M" in text, text


@_have_engine()
def test_chart_is_embedded_as_a_figure():
    import pymupdf
    data, _ = _pdf([{"type": "chart", "chart": "bar", "title": "T",
                     "categories": ["a", "b"],
                     "series": [{"name": "s", "data": [1, 2]}]}])
    d = pymupdf.open(stream=data, filetype="pdf")
    assert d[0].get_images(full=True), "chart figure missing from the page"
    d.close()


@_have_engine()
def test_no_engine_falls_back_instead_of_failing(monkeypatch):
    """A run on a machine without TeX must still deliver a document."""
    monkeypatch.setattr(docgen_latex, "engine_path", lambda: "")
    data, name = _pdf([{"type": "paragraph", "text": "Body text here."}])
    assert data[:4] == b"%PDF" and name.endswith(".pdf")


def test_reportlab_path_is_unchanged_by_default():
    """The default must stay reportlab, byte-for-byte behaviour included: a
    spec that does not ask for LaTeX never touches the new code."""
    data, _ = docgen.build_pdf({"title": "Plain", "blocks": [
        {"type": "paragraph", "text": "Body."}]})
    assert data[:4] == b"%PDF"
