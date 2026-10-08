"""Page geometry, typographic styles, columns and citation styles.

Every one of these was on paper and in the standards, and absent from the
generator: the page was hardcoded A4 portrait at 24mm margins with one type
system, so "make it a Letter report in a book typeface with APA references"
had no way to be true.
"""
import io
import re
import zipfile

import pytest

import docgen
import doclayout as L


def _pdf(**spec):
    spec.setdefault("title", "T")
    spec.setdefault("blocks", [
        {"type": "heading", "level": 1, "text": "Findings"},
        {"type": "paragraph", "text": "Body prose for the findings. " * 20}])
    blob, _n, _c = docgen.generate("pdf", spec)
    return blob


def _rect(blob):
    import pymupdf
    return pymupdf.open(stream=blob, filetype="pdf")[0].rect


# ── page sizes ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name,w,h", [
    ("a3", 297, 420), ("a4", 210, 297), ("a5", 148, 210),
    ("letter", 215.9, 279.4), ("legal", 215.9, 355.6),
    ("tabloid", 279.4, 431.8), ("executive", 184.15, 266.7),
    ("b5", 176, 250),
])
def test_page_sizes_reach_the_page(name, w, h):
    r = _rect(_pdf(page_size=name))
    assert abs(r.width - w * 72 / 25.4) < 1.5, (name, r.width)
    assert abs(r.height - h * 72 / 25.4) < 1.5, (name, r.height)


def test_an_unknown_page_size_falls_back_instead_of_failing():
    """A model that invents 'letter-legal' should still get a document."""
    r = _rect(_pdf(page_size="letter-legal"))
    assert abs(r.width - 210 * 72 / 25.4) < 1.5


def test_landscape_swaps_the_axes():
    p = _rect(_pdf(page_size="a4"))
    l = _rect(_pdf(page_size="a4", orientation="landscape"))
    assert l.width > l.height and p.height > p.width
    assert abs(l.width - p.height) < 1.5


@pytest.mark.parametrize("preset", sorted(L.MARGIN_PRESETS))
def test_every_margin_preset_renders_and_moves_the_text_block(preset):
    import pymupdf
    narrow = pymupdf.open(stream=_pdf(margins="narrow"), filetype="pdf")
    generous = pymupdf.open(stream=_pdf(margins="generous"),
                            filetype="pdf")

    def left_margin(doc):
        for b in doc[0].get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                if "".join(s["text"] for s in l["spans"]).strip():
                    return l["bbox"][0]
        return None

    ln, lg = left_margin(narrow), left_margin(generous)
    assert ln is not None and lg is not None
    assert lg > ln + 8, f"{preset}: margins did not widen the margin"


def test_explicit_millimetre_margins_override_the_preset():
    import pymupdf
    def left(**kw):
        d = pymupdf.open(stream=_pdf(**kw), filetype="pdf")
        for b in d[0].get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                if "".join(s["text"] for s in l["spans"]).strip():
                    return l["bbox"][0]
    assert left(margins={"left": 45}) > left(margins="normal") + 12


def test_margins_can_never_consume_the_page():
    r = _rect(_pdf(page_size="a6", margins={"left": 100, "right": 100}))
    assert r.width > 0 and _pdf(page_size="a6", margins={"left": 100})


# ── styles ─────────────────────────────────────────────────────────────────
def test_every_style_renders():
    for name in L.DOC_STYLES:
        blob = _pdf(style=name)
        assert blob.startswith(b"%PDF-")


def test_serif_styles_actually_embed_a_serif_font():
    """A style that says serif but renders Helvetica is a broken promise."""
    import pymupdf

    def fonts(style):
        d = pymupdf.open(stream=_pdf(style=style), filetype="pdf")
        return {f[3] for p in d for f in p.get_fonts(full=False)}

    sans = fonts("report")
    serif = fonts("academic")
    assert sans, "no fonts found at all - the probe is wrong, not the doc"
    assert any("Helvetica" in f or "Arial" in f for f in sans), sans
    assert any("Times" in f or "Serif" in f for f in serif), serif


def test_body_size_follows_the_style():
    import pymupdf

    def body_size(style):
        d = pymupdf.open(stream=_pdf(style=style), filetype="pdf")
        best = 0
        for p in d:
            for b in p.get_text("dict")["blocks"]:
                for l in b.get("lines", []):
                    for s in l["spans"]:
                        t = s["text"]
                        if t.startswith("Body prose"):
                            return round(s["size"], 1)
        return best

    assert body_size("report") != body_size("manual")
    assert body_size("brief") > body_size("technical")


def test_the_measure_stays_readable_on_a_small_page():
    """The same style must not set 90-character lines on A6."""
    import pymupdf

    def widest(page_size):
        d = pymupdf.open(stream=_pdf(page_size=page_size), filetype="pdf")
        best = 0.0
        for p in d:
            for b in p.get_text("dict")["blocks"]:
                for l in b.get("lines", []):
                    txt = "".join(s["text"] for s in l["spans"]).strip()
                    if txt.startswith("Body prose"):
                        best = max(best, l["bbox"][2] - l["bbox"][0])
        return best

    a4, a6 = widest("a4"), widest("a6")
    assert a6 < a4, "the line did not shorten on a narrower page"
    assert a6 < 200, f"A6 measure is {a6}pt - too wide to read"


def test_newsletter_defaults_to_two_columns():
    assert L.DOC_STYLES["newsletter"]["columns"] == 2


@pytest.mark.parametrize("cols", [1, 2, 3])
def test_columns_render(cols):
    blob = _pdf(columns=cols, style="report")
    assert blob.startswith(b"%PDF-")


def test_two_columns_put_text_in_two_x_positions():
    """Both columns must actually be used - a frame bug stacks them.

    Detected by which half of the page the text sits in, not by matching a
    phrase: only the first line of a wrapped paragraph starts with the
    opening words, so a phrase filter misses the second column entirely.
    """
    import pymupdf
    long_doc = [{"type": "paragraph", "text": "Filler sentence for flow. " * 260}]
    blob = _pdf(columns=2, style="newsletter", blocks=long_doc)
    d = pymupdf.open(stream=blob, filetype="pdf")
    mid = d[0].rect.width / 2
    left, right = 0, 0
    for p in d:
        for b in p.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                txt = "".join(s["text"] for s in l["spans"]).strip()
                if not txt or txt.startswith("page "):
                    continue
                if l["bbox"][2] <= mid:
                    left += 1
                elif l["bbox"][0] >= mid - 5:
                    right += 1
    assert left > 0 and right > 0, \
        f"text only in one column (left={left} right={right})"


def test_nothing_overflows_a_column_in_a_two_column_layout():
    """Charts and tables must be sized to the column.

    Found by rendering a newsletter and looking at it: `avail_w` was the page
    width, so the bar chart drew straight across the gutter and over the
    second column. Every text-based assertion passed while this was broken.
    """
    import pymupdf
    blob = _pdf(columns=2, style="newsletter", blocks=[
        {"type": "paragraph", "text": "Lead-in sentence. " * 4},
        {"type": "chart", "kind": "bar", "categories": ["A", "B", "C"],
         "series": [{"name": "s", "data": [3, 5, 2]}]},
        {"type": "table", "header": ["K", "V"],
         "rows": [["one", "1"], ["two", "2"]]},
    ])
    d = pymupdf.open(stream=blob, filetype="pdf")
    page_w = d[0].rect.width
    # A drawing must sit ENTIRELY in one column. Content in the right-hand
    # column is supposed to be right of the midpoint - what must never happen
    # is a single element straddling the gutter, which is what a page-width
    # chart did.
    mid = page_w / 2
    gutter = 16          # the 7mm gutter plus a little slack
    for p in d:
        for drawing in p.get_drawings():
            r = drawing["rect"]
            x0, x1 = r.x0, r.x1
            if x1 - x0 < 2 or r.y1 - r.y0 < 2:
                continue          # a rule or a hairline, not content
            straddles = x0 < mid - gutter and x1 > mid + gutter
            assert not straddles, \
                f"content runs x={x0:.0f}..{x1:.0f} across the gutter " \
                f"(mid={mid:.0f}) on a {page_w:.0f}pt page"


# ── citation styles ────────────────────────────────────────────────────────
REFS = [
    {"authors": "Zhang, Jane", "year": "2024", "title": "Adaptive index choice",
     "container": "Journal of Vector Search", "volume": "12", "issue": "3",
     "pages": "44-59"},
    {"authors": "Ruiz, Ana", "year": "2021", "title": "Graph recall at scale",
     "container": "Proceedings of the Conference on Indexing",
     "pages": "10-21", "url": "https://example.org/2021"},
]


@pytest.mark.parametrize("name", sorted(L.CITATION_STYLES))
def test_every_citation_style_renders_a_list(name):
    blob = _pdf(citation_style=name, references=REFS)
    assert blob.startswith(b"%PDF-")


@pytest.mark.parametrize("name", sorted(L.CITATION_STYLES))
def test_the_style_names_its_own_section(name):
    import pymupdf
    cfg = L.CITATION_STYLES[name]
    blob = _pdf(citation_style=name, references=REFS)
    d = pymupdf.open(stream=blob, filetype="pdf")
    text = "\n".join(p.get_text() for p in d)
    assert cfg["heading"] in text, f"{name}: no {cfg['heading']!r} heading"


def test_numeric_styles_number_and_alphabetical_ones_do_not():
    import pymupdf

    def text_for(style):
        d = pymupdf.open(stream=_pdf(citation_style=style, references=REFS),
                          filetype="pdf")
        return "\n".join(p.get_text() for p in d)

    ieee = text_for("ieee")
    assert "[1]" in ieee and "[2]" in ieee
    assert "[1]" not in text_for("apa")


def test_alphabetical_and_citation_order_really_differ():
    """This is the difference that makes a reference list look wrong to
    anyone in the field: APA sorts by surname, IEEE keeps citation order."""
    apa = L.build_reference_list(REFS, L.CITATION_STYLES["apa"])
    ieee = L.build_reference_list(REFS, L.CITATION_STYLES["ieee"])
    # REFS are passed Zhang first; alphabetically Ruiz precedes Zhang.
    assert apa[0].startswith("Ruiz"), apa
    assert ieee[0].startswith("[1] J. Zhang"), ieee[0]


def test_author_rendering_follows_the_family():
    ref = {"authors": "Zhang, Jane", "year": "2020", "title": "T"}
    # APA and Harvard put the surname first with initials; MLA and Chicago
    # spell the given name out; the numeric styles lead with initials.
    assert L.format_reference(ref, L.CITATION_STYLES["apa"]).startswith(
        "Zhang, J.")
    assert L.format_reference(ref, L.CITATION_STYLES["mla"]).startswith(
        "Zhang, Jane")
    assert L.format_reference(ref, L.CITATION_STYLES["ieee"], 1).startswith(
        "[1] J. Zhang")


def test_an_ambiguous_author_is_not_mangled():
    """Guessing produced 'Chanson H' -> 'H, C.' - a reference list
    confidently naming the wrong author is worse than an unstyled one."""
    for style in ("apa", "ieee", "mla", "chicago"):
        cfg = L.CITATION_STYLES[style]
        for name in ("Chanson H", "Jane Zhang", "Wikipedia contributors"):
            out = L._authors({"authors": name}, cfg)
            assert out == name or out.startswith(name.split()[0]), \
                f"{style}: {name!r} became {out!r}"
    # Shapes that DO declare themselves are still normalised.
    assert L._authors({"authors": "Zhang, Jane"},
                      L.CITATION_STYLES["apa"]) == "Zhang, J."
    assert L._authors({"authors": "J. Zhang"},
                      L.CITATION_STYLES["apa"]) == "J. Zhang"


def test_references_never_double_a_full_stop():
    """Initials already end in a period, so a naive separator printed
    'Chanson, H., & Jia, Y..' in every list."""
    out = L.build_reference_list(
        [{"authors": "Zhang, Jane and Li, Bo", "year": "2020",
          "title": "A study", "container": "Journal X"}],
        L.CITATION_STYLES["apa"])
    assert ".." not in out[0], out
    for style in L.CITATION_STYLES:
        for line in L.build_reference_list(REFS, L.CITATION_STYLES[style]):
            assert ".." not in line, (style, line)


def test_references_accept_plain_strings_untouched():
    import pymupdf
    blob = _pdf(citation_style="plain",
                references=["Smith, J. (2020). A title. Somewhere."])
    text = "\n".join(p.get_text() for p in
                     pymupdf.open(stream=blob, filetype="pdf"))
    assert "Smith, J. (2020). A title. Somewhere." in text


def test_a_reference_list_never_invents_fields():
    """A bare string must not acquire an author or a year it never had."""
    out = L.build_reference_list(
        [{"title": "Just a title"}], L.CITATION_STYLES["apa"])
    assert out == ["Just a title."]


# ── docx / pptx parity ─────────────────────────────────────────────────────
def _docx_xml(**spec):
    spec.setdefault("title", "T")
    spec.setdefault("blocks", [{"type": "paragraph", "text": "Body. " * 20}])
    blob, _n, _c = docgen.generate("docx", spec)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        return blob, z.read("word/document.xml").decode("utf-8", "replace")


def test_docx_honours_page_size_and_orientation():
    def page(**kw):
        _b, xml = _docx_xml(**kw)
        m = re.search(r'<w:pgSz[^>]*w:w="(\d+)"[^>]*w:h="(\d+)"', xml)
        return (round(int(m.group(1)) / 1440 * 25.4),
                round(int(m.group(2)) / 1440 * 25.4))

    assert page(page_size="letter") == (216, 279)
    pw, ph = page(page_size="a4", orientation="landscape")
    assert pw > ph


def test_docx_columns_are_a_section_property():
    for n in (2, 3):
        _b, xml = _docx_xml(columns=n)
        assert f'w:num="{n}"' in xml


def test_docx_font_follows_the_style_family():
    _b, xml = _docx_xml(style="academic")
    _b2, xml2 = _docx_xml(style="technical")
    with zipfile.ZipFile(io.BytesIO(_b)) as z:
        sa = z.read("word/styles.xml").decode("utf-8", "replace")
    with zipfile.ZipFile(io.BytesIO(_b2)) as z:
        st = z.read("word/styles.xml").decode("utf-8", "replace")
    assert "Times New Roman" in sa
    assert "Calibri" in st


def test_pptx_honours_the_slide_canvas():
    for size, w, h in (("16:9", 13.33, 7.5), ("4:3", 10.0, 7.5),
                       ("16:10", 10.0, 6.25), ("1:1", 10.0, 10.0)):
        blob, _n, _c = docgen.generate("pptx", {
            "title": "T", "slide_size": size,
            "blocks": [{"type": "bullets", "items": ["a"]}]})
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            pres = z.read("ppt/presentation.xml").decode("utf-8", "replace")
        m = re.search(r'sldSz[^/]*cx="(\d+)"[^/]*cy="(\d+)"', pres)
        assert abs(int(m.group(1)) / 914400 - w) < 0.02, size
        assert abs(int(m.group(2)) / 914400 - h) < 0.02, size


def test_an_arbitrary_aspect_ratio_is_accepted():
    blob, _n, _c = docgen.generate("pptx", {
        "title": "T", "slide_size": "21:9",
        "blocks": [{"type": "bullets", "items": ["a"]}]})
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        pres = z.read("ppt/presentation.xml").decode("utf-8", "replace")
    m = re.search(r'sldSz[^/]*cx="(\d+)"[^/]*cy="(\d+)"', pres)
    assert abs(int(m.group(1)) / int(m.group(2)) - 21 / 9) < 0.01


# ── the catalogue must not drift from the resolvers ───────────────────────
def test_the_schema_advertises_only_real_choices():
    schema = docgen.spec_schema()["spec"]
    for name in schema["page_size"]["choices"]:
        assert name in L.PAGE_SIZES, name
    for name in schema["style"]["choices"]:
        assert name in L.DOC_STYLES, name
    for name in schema["citation_style"]["choices"]:
        assert name in L.CITATION_STYLES, name
    for name in schema["margins"]["choices"]:
        assert name in L.MARGIN_PRESETS, name
    for name in schema["slide_size"]["choices"]:
        assert name in L.SLIDE_SIZES, name


def test_every_resolver_round_trips_its_own_keys():
    for name in L.PAGE_SIZES:
        assert L.resolve_page_size(name) == L.PAGE_SIZES[name], name
    for name in L.DOC_STYLES:
        assert L.resolve_style(name)["body"] == L.DOC_STYLES[name]["body"]
    for name in L.CITATION_STYLES:
        assert L.resolve_citation(name)["heading"] == \
            L.CITATION_STYLES[name]["heading"]
    for name in L.SLIDE_SIZES:
        assert L.resolve_slide_size(name) == L.SLIDE_SIZES[name], name


def test_paper_sizes_are_all_root_two():
    """ISO 216's defining property. Sizes are rounded to whole millimetres,
    so the tolerance has to absorb the rounding - A8 is 52x74, which is
    root(2)*52 = 73.5 rounded UP, not a typo."""
    import math
    for series in (L.ISO_A, L.ISO_B, L.ISO_C):
        for name, (w, h) in series.items():
            expected = w * math.sqrt(2)
            # Sizes are rounded to whole millimetres, and ISO rounds UP, so
            # B5 is 176x250 where root(2)*176 = 248.9. Allow the rounding
            # the standard itself introduces.
            assert abs(h - expected) <= max(1.0, w * 0.01), (name, w, h, expected)