r"""LaTeX must not reach the page.

A live 23-page report on how LLMs work printed 112 literal `$` characters
across 9 pages: "Queries ($Q$), Keys ($K$)", "the head dimension ($d_k$)",
"Scaling by $\sqrt{d_k}$". `_rich()` translated `**bold**`, `` `code` `` and
links and nothing else, and the author prompt never said the renderer has no
maths — so a technical document did what a technical document does.

Two constraints shape the answer:

* the conversion is scoped to `$...$`, `\(...\)` and `\[...\]`, because pricing
  prose has dollars in it and must survive intact; and
* the renderer registers no TrueType fonts, so every glyph must exist in
  cp1252. A base-14 font does not draw a box for a character outside its
  repertoire, it draws the WRONG letter — which is how a first attempt at
  `d_k` came out of a test build as "dI".
"""
import pytest

import docgen


def _t(s):
    return docgen._rich(s)


def test_inline_math_loses_its_delimiters():
    assert _t("Queries ($Q$), Keys ($K$), and Values ($V$).") == \
        "Queries (Q), Keys (K), and Values (V)."


def test_sqrt_survives_and_the_subscript_is_typeset():
    """`d_k` used to land on the page as the literal characters `d_k`.

    Base-14 fonts have no subscript glyph, so the converter flattened it to
    text; reportlab draws `<sub>` as a real lowered run, so the notation now
    reads the way the author meant it."""
    assert _t("Scaling by $\\sqrt{d_k}$ maintains variance.") == \
        "Scaling by sqrt(d<sub>k</sub>) maintains variance."


def test_mathcal_and_superscript():
    assert _t("a catastrophic $\\mathcal{O}(N^2)$ complexity") == \
        "a catastrophic O(N²) complexity"


def test_greek_and_relations():
    # `_rich` returns reportlab markup, so a converted `\le` reaches the page as
    # `<=` but must be XML-escaped on the way.
    assert _t("$\\alpha$ and $\\beta$ weight it, where $k \\le m$.") == \
        "alpha and beta weight it, where k &lt;= m."


def test_a_display_equation_in_brackets_is_converted():
    out = _t("\\[ P(w) = \\frac{1}{Z} \\exp(-E(w)) \\]")
    assert "\\" not in out and "$" not in out, out
    assert "(1)/(Z)" in out, out


def test_currency_is_never_treated_as_math():
    """The whole risk of matching `$`: a cost table would lose its signs."""
    for s in ("The plan costs $5 per seat and $6 per user, billed monthly.",
              "Revenue grew from $1.2M to $3.4M in 2026.",
              "A 100% margin on a $0 licence is not a bug.",
              "Range: $5-$10 per request."):
        assert _t(s) == s, s


def test_markdown_still_translates():
    assert _t("a **term** with `code`") == \
        'a <b>term</b> with <font name="Courier">code</font>'


def test_an_unmatched_dollar_survives():
    assert _t("about 50$ of compute") == "about 50$ of compute"


# Verbatim from the pages that leaked in the live 23-page report. These are the
# forms with NO `$` around them at all — a display equation the model simply
# wrote as LaTeX — which a delimiter-only pass cannot see.
BARE_DISPLAY = [
    r"L(N, D) = \left( \frac{N_c}{N} \right)^{\alpha_N}"
    r" + \left( \frac{D_c}{D} \right)^{\alpha_D}",
    r"h = W_0 x + \Delta W x = W_0 x + \frac{\alpha}{r} B A x",
    r"\max_\theta \mathbb{E}_{(x, y) \sim D} \left[ R_\phi(x, y)"
    r" - \eta D_{KL}(\pi_\theta(y|x) \parallel \pi_{SFT}(y|x)) \right]",
    r"q = \text{round}\left(\frac{x}{S}\right) + Z",
    r"Sequence of integers ($1 \dots V$) is stored",
]


@pytest.mark.parametrize("line", BARE_DISPLAY)
def test_a_display_equation_written_without_delimiters_is_still_readable(line):
    out = _t(line)
    assert "\\" not in out, out
    assert "$" not in out, out
    assert "{" not in out and "}" not in out, out


def test_nested_braces_survive_the_conversion():
    """`\frac` whose arguments contain braces of their own — the form that made
    the flat regex give up and leave `\frac` on the page."""
    out = _t(r"\frac{\pi_\theta(y_w|x)}{\pi_{ref}(y_w|x)}")
    assert out.startswith("(pi_theta(y<sub>w</sub>|x))/(pi_(ref)(y<sub>w</sub>|x))"), out


def test_a_windows_path_is_not_math():
    """The bare-command pass runs on all text, so it must not invent damage."""
    s = r"Write it to C:\Users\name\docs and press Enter"
    assert _t(s) == s, _t(s)
    assert _t(r"the \nabla operator") == "the nabla operator"


def test_math_output_is_limited_to_what_the_fonts_can_draw():
    """The conversion may not introduce a glyph base-14 Helvetica lacks.

    Not a style preference: an out-of-repertoire character renders as a
    different LETTER, so the reader sees plausible text that is simply wrong.
    """
    samples = ["$\\alpha + \\beta \\le \\gamma$", "$x \\to \\infty$",
               "$\\sqrt{d_k}$", "$\\mathbb{R}^d$", "$\\sum_i y_i$",
               "$A \\cup B$, $v \\in V$", "$\\Omega(n)$ and $\\pi$"]
    for s in samples:
        out = _t(s)
        for ch in out:
            try:
                ch.encode("cp1252")
            except UnicodeEncodeError:
                pytest.fail(f"{s!r} -> {out!r} cannot be drawn: {ch!r}")


def test_a_greek_letter_written_directly_is_replaced_too():
    """The model may skip LaTeX and type `α` itself; same font problem."""
    assert _t("the $\\alpha$ term") == "the alpha term"
    assert "alpha" in _t("weighted by α and β")
    assert "sqrt" in _t("take √ of it")
    # ...and a character cp1252 does have is left completely alone.
    assert _t("an em dash — and 100% of ±3°") == "an em dash — and 100% of ±3°"


# ── end to end, through the actual renderer ─────────────────────────────────
MATHY = {
    "title": "Attention Mathematics",
    "toc": True,
    "blocks": [
        {"type": "heading", "level": 1, "text": "Scaled dot-product attention"},
        {"type": "paragraph",
         "text": "Embeddings project into Queries ($Q$), Keys ($K$), and "
                 "Values ($V$). Dividing by $\\sqrt{d_k}$ keeps the variance "
                 "near one, and the cost is $\\mathcal{O}(n^2 d)$ per layer."},
        {"type": "paragraph",
         "text": "The licence runs $5 per seat while the cluster costs $6 per "
                 "hour, so the arithmetic matters."},
    ],
}


def _pages():
    import pymupdf
    blob, _n, _ct = docgen.generate("pdf", MATHY)
    doc = pymupdf.open(stream=blob, filetype="pdf")
    return [doc[i].get_text() for i in range(len(doc))]


def test_the_rendered_page_contains_no_latex():
    text = "\n".join(_pages())
    mathy = [ln for ln in text.splitlines() if "Embeddings project" in ln]
    assert mathy, text[:400]
    assert "$" not in mathy[0], mathy[0]
    assert "\\sqrt" not in text and "\\mathcal" not in text
    assert "Queries (Q), Keys (K), and Values (V)" in mathy[0], mathy[0]
    assert "O(n²d)" in text.replace(" ", ""), text[:400]


def test_the_rendered_page_keeps_its_prices():
    text = "\n".join(_pages())
    assert "$5 per seat" in text, text
    assert "$6 per hour" in text, text


def test_the_formats_without_a_markup_engine_are_cleaned_too():
    """docx and pptx have no inline-markup path at all — they call
    `_strip_md`, which produced the same `($Q$)` on the page."""
    assert docgen._strip_md("($Q$) and $\\sqrt{d_k}$") == "(Q) and sqrt(d_k)"
    assert docgen._strip_md("a **b** and _i_ term, batch_size") == \
        "a b and i term, batch_size"
    assert docgen._strip_md("weighted by α") == "weighted by alpha"
    # An underscore between word characters is an identifier, not emphasis.
    assert "d_k" in docgen._strip_md("$d_k$")


def test_a_document_without_a_cover_block_does_not_waste_page_one():
    """The title-only branch emitted title + rule and then broke the page, so
    page 1 of a live report held 36 characters and nothing else."""
    pages = _pages()
    assert "Attention Mathematics" in pages[0]
    assert "Contents" in pages[0], pages[0][:300]
    assert "Scaled dot-product" in "".join(pages), pages


# ── markup that reached the page (live: 41-page LLM monograph) ───────────────

def test_a_markdown_heading_written_into_the_body_is_not_printed():
    """The author labels its own continuation chunk `## continuation`, and
    `_strip_md` stripped `**` but never the ATX marker — so the hashes landed
    on page 11 verbatim, in every block type."""
    for kind in ({"type": "paragraph", "text": "## continuation"},
                 {"type": "quote", "text": "## continuation"},
                 {"type": "heading", "level": 2, "text": "## continuation"},
                 {"type": "bullets", "items": ["## continuation"]}):
        blob, _n, _ct = docgen.generate("pdf", {"title": "T", "blocks": [kind]})
        assert b"##" not in blob, kind
        assert docgen._strip_md("## continuation") == "continuation"
        assert docgen._strip_md("  ### notes here") == "notes here"


def test_a_hash_that_is_not_a_heading_survives():
    """Markdown needs the space; `C#` and `#include` have none."""
    for s in ("written in C# and F#", "#include <stdio.h>", "issue #42"):
        assert docgen._strip_md(s) == s, s


def test_single_letter_subscripts_are_typeset():
    r = docgen._rich("head dimension d_k and value dimension d_v")
    assert r == "head dimension d<sub>k</sub> and value dimension d<sub>v</sub>"
    assert "<super>T</super>" in docgen._rich("softmax((Q K^T) / sqrt(d_k))")
    assert docgen._rich("x_hat = W_d f(x)").startswith("x<sub>hat</sub>")


def test_a_variable_name_is_not_a_subscript():
    """The bound is the whole rule: `W_merged` and `batch_size` are names."""
    assert docgen._rich("W_merged = W_0 + (alpha / r) (B A)") \
        == "W_merged = W<sub>0</sub> + (alpha / r) (B A)"
    assert docgen._rich("batch_size and hidden_dim stay plain") \
        == "batch_size and hidden_dim stay plain"


def test_code_spans_are_never_typeset_as_math():
    """A Courier span is code; the author wrote those characters deliberately."""
    assert docgen._rich("the `d_k` operator") == \
        'the <font name="Courier">d_k</font> operator'
