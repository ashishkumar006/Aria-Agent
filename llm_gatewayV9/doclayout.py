"""Layout, page geometry, document styles and citation styles.

Separated from `docgen.py` on purpose. The generator is where content becomes
bytes; this is where *how it looks* is decided. Keeping the tables here means
the model can be told about them from one place (docgen.spec_schema()), and
they can be tested without rendering a single page.

Every dimension comes from a published standard rather than taste:

  * ISO 216 (A and B series) and ISO 269 (C series) for paper. All are
    1:sqrt(2), each size halved from the one above.
  * ANSI/ASME Y14.1 for the North American sizes (Letter, Legal, Tabloid...).
  * PPTX canvas sizes as PowerPoint ships them.
  * Citation styles per their current manuals, not folk memory - the
    reference list is sorted differently by APA (alphabetical) and IEEE
    (order of first citation), and that difference is the whole point of
    naming a style.

Typographic defaults follow two rules that hold across every good document:
a measure of roughly 45-75 characters, and all vertical spacing in whole
multiples of the body leading (a baseline grid).
"""
from __future__ import annotations

import re

# ── paper sizes ────────────────────────────────────────────────────────────
# name -> (width_mm, height_mm), portrait.
ISO_A = {
    "a0": (841, 1189), "a1": (594, 841), "a2": (420, 594), "a3": (297, 420),
    "a4": (210, 297), "a5": (148, 210), "a6": (105, 148), "a7": (74, 105),
    "a8": (52, 74), "a9": (37, 52), "a10": (26, 37),
}
ISO_B = {
    "b0": (1000, 1414), "b1": (707, 1000), "b2": (500, 707), "b3": (353, 500),
    "b4": (250, 353), "b5": (176, 250), "b6": (125, 176), "b7": (88, 125),
    "b8": (62, 88), "b9": (44, 62), "b10": (31, 44),
}
ISO_C = {
    "c0": (917, 1297), "c1": (648, 917), "c2": (458, 648), "c3": (324, 458),
    "c4": (229, 324), "c5": (162, 229), "c6": (114, 162),
}

# ANSI / North American. Letter is 6mm wider and 18mm shorter than A4, which
# is why a document laid out on A4 reflows when it crosses the Atlantic.
ANSI = {
    "letter": (215.9, 279.4),        # 8.5 x 11 in
    "legal": (215.9, 355.6),         # 8.5 x 14 in
    "tabloid": (279.4, 431.8),       # 11 x 17 in
    "ledger": (431.8, 279.4),
    "executive": (184.15, 266.7),     # 7.25 x 10.5 in
    "statement": (139.7, 215.9),     # 5.5 x 8.5 in
    "government-letter": (203.2, 266.7),   # 8 x 10 in
    "folio": (215.9, 330.2),         # 8.5 x 13 in
    "quarto": (215.9, 269.875),
}

# Book and trade sizes. A5 is the default for a trade paperback; B6 and
# pocket sizes are common in self-publishing.
BOOK = {
    "pocket": (108, 178), "digest": (138, 204), "crown": (140, 216),
    "royal": (156, 234), "superroyal": (178, 257),
    "a-format": (148, 210),           # = A5
    "b-format": (129, 198),
}

PAGE_SIZES: dict[str, tuple[float, float]] = {
    **ISO_A, **ISO_B, **ISO_C, **ANSI, **BOOK,
    # Aliases people actually type.
    "a": (210, 297), "us-letter": (215.9, 279.4), "us-legal": (215.9, 355.6),
    "us-tabloid": (279.4, 431.8), "a4+": (210, 297),
}

DEFAULT_PAGE_SIZE = "a4"

ORIENTATIONS = ("portrait", "landscape")

# Margin presets in mm: (top, right, bottom, left)
MARGIN_PRESETS: dict[str, tuple[float, float, float, float]] = {
    "narrow": (12, 12, 12, 12),
    "normal": (20, 24, 22, 24),
    "moderate": (22, 25, 24, 25),
    "wide": (25, 32, 28, 32),
    "generous": (28, 38, 30, 38),
}
DEFAULT_MARGIN = "moderate"


def resolve_page_size(name: str | None) -> tuple[float, float]:
    """Portrait dimensions in mm for a page-size name.

    Unknown names fall back to A4 rather than raising: a model that invents
    "letter-legal" should still get a document, not an error page.
    """
    if not name:
        return ISO_A["a4"]
    key = str(name).strip().lower().replace("_", "-").replace(" ", "")
    key = re.sub(r"^(iso[-_]?|us[-_]?)?", lambda m: m.group(0)
                 if m.group(0) in ("iso-", "iso_", "us-", "us_") else "",
                 key)
    return PAGE_SIZES.get(key, ISO_A["a4"])


def resolve_orientation(name: str | None) -> str:
    if not name:
        return "portrait"
    return ("landscape" if str(name).strip().lower().startswith("land")
            else "portrait")


def resolve_margins(name: str | None, page: tuple[float, float],
                    override: dict | None = None) -> tuple[float, float,
                                                           float, float]:
    """Margins as (top, right, bottom, left) in mm, in page order.

    `override` accepts explicit millimetre values and wins over the preset, so
    a caller can say {"left": 30} without restating the other three.
    """
    preset = MARGIN_PRESETS.get(str(name or "").strip().lower(),
                                MARGIN_PRESETS[DEFAULT_MARGIN])
    t, r, b, l = preset
    if override:
        def _num(v, fallback):
            try:
                f = float(v)
            except (TypeError, ValueError):
                return fallback
            return max(0.0, min(f, min(page) / 2 - 5))
        t = _num(override.get("top"), t)
        r = _num(override.get("right"), r)
        b = _num(override.get("bottom"), b)
        l = _num(override.get("left"), l)
    # Never let the margins consume the page.
    w, h = page
    if l + r >= w - 20:
        r = l = max(6.0, (w - 60) / 2)
    if t + b >= h - 20:
        t = max(6.0, (h - 60) / 3)
        b = max(6.0, (h - 60) / 3)
    return (t, r, b, l)


# ── document styles ────────────────────────────────────────────────────────
# A style is a typographic system, not a colour. Each entry fixes the body
# size and leading (the baseline unit), a heading scale derived from it, and
# whether the document gets front matter. `measure` is the target line length
# in characters; the margin resolution below honours it.
#
# family: "sans" (Helvetica) or "serif" (Times) - reportlab's core fonts, so
# no font embedding is needed and the output stays portable.
DOC_STYLES: dict[str, dict] = {
    # The default: sans body, single column, numbered sections.
    "report": dict(
        family="sans", body=10.5, leading=15.2, scale=1.34,
        align="left", indent_first=False, numbered=True,
        accent="#5b4bd6", measure=78, columns=1,
    ),
    # Reads as a corporate briefing: bigger type, more air, no cover.
    "brief": dict(
        family="sans", body=11.5, leading=16.4, scale=1.30,
        align="left", indent_first=False, numbered=True,
        accent="#0b5cad", measure=72, columns=1,
    ),
    # Memo convention: no title page, no numbering, direct.
    "memo": dict(
        family="sans", body=11, leading=16, scale=1.26,
        align="left", indent_first=False, numbered=False,
        accent="#3a3a44", measure=74, columns=1,
    ),
    # Serif body and a bigger leading - the look of a working paper.
    "academic": dict(
        family="serif", body=11.5, leading=17.5, scale=1.28,
        align="justify", indent_first=True, numbered=True,
        accent="#1d1d22", measure=80, columns=1,
    ),
    # Long-form, generous margins, serif, strong numbering for navigation.
    "whitepaper": dict(
        family="serif", body=10.8, leading=16.0, scale=1.36,
        align="justify", indent_first=False, numbered=True,
        accent="#0f6e5c", measure=76, columns=1,
    ),
    # Procedural: tighter, sans, generous headings for scanning.
    "manual": dict(
        family="sans", body=10.2, leading=14.6, scale=1.36,
        align="left", indent_first=False, numbered=True,
        accent="#b3541e", measure=76, columns=1,
    ),
    # Two-column is the giveaway of a newsletter.
    "newsletter": dict(
        family="serif", body=9.8, leading=13.6, scale=1.30,
        align="justify", indent_first=False, numbered=False,
        accent="#7a1f4b", measure=100, columns=2,
    ),
    # Dense and tabular-friendly; monospace-ish tone via tighter leading.
    "technical": dict(
        family="sans", body=9.6, leading=13.2, scale=1.28,
        align="left", indent_first=False, numbered=True,
        accent="#2b4a7d", measure=88, columns=1,
    ),
    # Trade book page: serif, first-line indents, no headings numbered.
    "book": dict(
        family="serif", body=10.6, leading=15.6, scale=1.32,
        align="justify", indent_first=True, numbered=False,
        accent="#3a3a44", measure=72, columns=1,
    ),
    # Slide-notes style used for the .docx twin of a deck.
    "plain": dict(
        family="sans", body=11, leading=15.4, scale=1.24,
        align="left", indent_first=False, numbered=False,
        accent="#5c5c66", measure=76, columns=1,
    ),
}
DEFAULT_STYLE = "report"


def resolve_style(name: str | None) -> dict:
    if not name:
        return dict(DOC_STYLES[DEFAULT_STYLE])
    key = re.sub(r"[^a-z]", "", str(name).lower())
    if key in DOC_STYLES:
        return dict(DOC_STYLES[key])
    for k, v in DOC_STYLES.items():
        if k.startswith(key) or key.startswith(k):
            return dict(v)
    return dict(DOC_STYLES[DEFAULT_STYLE])


def baseline_unit(style: dict) -> float:
    """The vertical rhythm unit: the body leading, in points."""
    return float(style.get("leading") or 15.2)


def measure_to_margins(page_w_mm: float, style: dict,
                       margins: tuple[float, float, float, float]
                       ) -> tuple[float, float, float, float]:
    """Widen the side margins until the line is a readable length.

    The rule of thumb is 45-75 characters; Helvetica averages roughly 0.50em
    per character, so 68 characters is about 34em. At the style's body size
    that is a target width in points, and anything wider gets extra margin.
    This is why the same style looks right on A5 and on A3.
    """
    t, r, b, l = margins
    target_chars = float(style.get("measure") or 76)
    body_pt = float(style.get("body") or 10.5)
    # 1pt = 0.3528mm
    target_mm = (target_chars * body_pt * 0.50) / 0.3528
    page_w_pt = page_w_mm * 72 / 25.4
    avail_pt = page_w_pt - (l + r) * 72 / 25.4
    if avail_pt > target_mm and page_w_mm > 120:
        need_pt = avail_pt - target_mm
        add_mm = need_pt * 0.3528 / 2
        l += add_mm
        r += add_mm
        if l + r >= page_w_mm - 40:      # small page: back off
            excess = (l + r) - (page_w_mm - 40)
            l -= excess / 2
            r -= excess / 2
    return (t, r, b, l)


# ── citation styles ────────────────────────────────────────────────────────
# Each entry names the system, the heading it prints, and whether the list
# sorts alphabetically (APA, MLA, Chicago author-date, Harvard, OSCOLA) or by
# order of first citation (IEEE, Vancouver, AMA).
CITATION_STYLES: dict[str, dict] = {
    "apa": dict(label="APA", heading="References", order="alphabetical",
                numbered=False, hanging_indent=True),
    "mla": dict(label="MLA", heading="Works Cited", order="alphabetical",
                numbered=False, hanging_indent=True),
    "chicago": dict(label="Chicago", heading="Bibliography",
                    order="alphabetical", numbered=False, hanging_indent=True),
    "harvard": dict(label="Harvard", heading="References", order="alphabetical",
                    numbered=False, hanging_indent=True),
    "ieee": dict(label="IEEE", heading="References", order="citation",
                 numbered=True, hanging_indent=False),
    "vancouver": dict(label="Vancouver", heading="References", order="citation",
                      numbered=True, hanging_indent=False),
    "ama": dict(label="AMA", heading="References", order="citation",
                numbered=True, hanging_indent=False),
    "bluebook": dict(label="Bluebook", heading="Authorities", order="alphabetical",
                     numbered=False, hanging_indent=True),
    "oscola": dict(label="OSCOLA", heading="Table of Cases and Legislation",
                   order="alphabetical", numbered=False, hanging_indent=True),
    "plain": dict(label="References", heading="References", order="citation",
                  numbered=False, hanging_indent=False),
}
DEFAULT_CITATION = "plain"


def resolve_citation(name: str | None) -> dict:
    if not name:
        return dict(CITATION_STYLES[DEFAULT_CITATION])
    key = re.sub(r"[^a-z]", "", str(name).lower())
    if key in CITATION_STYLES:
        return dict(CITATION_STYLES[key])
    for k, v in CITATION_STYLES.items():
        if k.startswith(key) or key.startswith(k):
            return dict(v)
    return dict(CITATION_STYLES[DEFAULT_CITATION])


def _join(parts: list[str], sep: str = ", ") -> str:
    return sep.join(p for p in parts if p)


def _sentence(parts: list[str], sep: str = ". ") -> str:
    """Join with a full stop, without ever emitting '..'.

    Author initials already end in a period ('J. Zhang'), so the naive
    separator produced 'Chanson, H., & Jia, Y..' in every reference list.
    """
    out = ""
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        if not out:
            out = p
            continue
        glue = sep
        if out.endswith(".") and glue.startswith("."):
            glue = glue[1:]
        out = out + glue + p
    return out + "." if out and not out.endswith(".") else out


def _authors(ref: dict, style: dict) -> str:
    """Author rendering, which is the single most visible difference between
    the styles: `Zhang, J.` (APA), `Zhang, Jane` (MLA/Chicago), `J. Zhang`
    (IEEE/Vancouver/AMA)."""
    raw = ref.get("authors") or ref.get("author") or ""
    if isinstance(raw, list):
        names = [str(a).strip() for a in raw if str(a).strip()]
    else:
        names = [n.strip() for n in str(raw).split(" and ") if n.strip()]
    if not names:
        return ""
    label = style["label"]

    def _inverted(n: str, initials: bool) -> str:
        """Surname first, but ONLY when the name says which part is which.

        Guessing produced 'Chanson H' -> 'H, C.', i.e. a reference list
        confidently naming the wrong author. A wrong name is worse than an
        unstyled one, so an ambiguous string is passed through untouched.

        Trustworthy shapes: 'Zhang, Jane' (comma), 'J. Zhang' (leading
        initial), 'Zhang J' (single trailing initial, already inverted).
        """
        n = n.strip()
        if "," in n:
            surname, _, given = n.partition(",")
            given = given.strip()
            if not given:
                return surname.strip()
            if initials:
                ini = "".join(f"{w[0]}." for w in given.split() if w)
                return f"{surname.strip()}, {ini}" if ini else surname.strip()
            return f"{surname.strip()}, {given}"
        bits = n.split()
        if len(bits) < 2:
            return n
        # A leading initial means the name is already "Initial Surname".
        first = bits[0]
        if len(first.rstrip(".")) == 1 and len(bits) >= 2:
            surname = " ".join(bits[1:])
            if initials:
                return f"{first.rstrip('.')}. {surname}"
            return f"{first.rstrip('.')}. {surname}"
        # A single trailing initial also means it is already inverted.
        last = bits[-1]
        if len(last.rstrip(".")) == 1 and len(bits) >= 2:
            return n
        return n          # ambiguous - do not guess

    if label in ("IEEE", "Vancouver", "AMA"):
        # "Zhang, Jane" -> "J. Zhang". The comma declares which part is which,
        # so this reordering is safe; an ambiguous shape is left alone.
        out = []
        for n in names:
            if "," in n:
                surname, _, given = n.partition(",")
                ini = "".join(f"{w[0]}." for w in given.split() if w)
                out.append(f"{ini} {surname.strip()}" if ini
                           else surname.strip())
            else:
                out.append(n)
        return _join(out)

    if label in ("APA", "Harvard"):
        return _join([_inverted(n, initials=True) for n in names])
    # MLA, Chicago, Bluebook, OSCOLA: surname first, given name spelled out.
    return _join([_inverted(n, initials=False) for n in names])


def format_reference(ref: dict, style: dict, number: int | None = None) -> str:
    """One formatted reference line.

    Aimed at the structural differences between the styles rather than
    character-perfect conformance to all nine manuals: author order, the
    year/container position, whether the list is numbered, and the title
    casing rule. Those are the differences a reader notices.
    """
    label = style["label"]
    kind = str(ref.get("type") or "").lower()
    authors = _authors(ref, style)
    year = str(ref.get("year") or ref.get("date") or "").strip()
    title = str(ref.get("title") or "").strip().rstrip(".")
    container = str(ref.get("container") or ref.get("journal")
                    or ref.get("publisher") or "").strip()
    volume = str(ref.get("volume") or "").strip()
    issue = str(ref.get("issue") or "").strip()
    pages = str(ref.get("pages") or "").strip()
    url = str(ref.get("url") or ref.get("doi") or "").strip()
    if kind in ("web", "website", "online") or (url and not container):
        container = container or "Web"
    prefix = f"[{number}] " if (style["numbered"] and number) else ""
    # Numeric styles put the year last; author-date puts it right after the
    # author. That is the visible shape of the two families.
    if label in ("IEEE", "Vancouver", "AMA"):
        core = _join([a for a in (authors, title) if a], ". ")
        tail = _join([f"vol. {volume}" if volume else "",
                      f"no. {issue}" if issue else "",
                      pages, year, container])
        return _sentence([prefix + core, tail])
    if label == "MLA":
        core = _join([authors, title], ". ")
        tail = _join([container, f"vol. {volume}" if volume else "",
                      f"no. {issue}" if issue else "", year, pages], ", ")
        return _sentence([prefix + core, tail])
    if label in ("Bluebook", "OSCOLA"):
        return _sentence([prefix + authors, title, container, year, url])
    # APA / Chicago / Harvard: author, date, title, container.
    return _sentence([prefix + authors, f"({year})" if year else year,
                      title, container, pages, url])


def build_reference_list(refs: list[dict], style: dict) -> list[str]:
    """Sort and number the list the way the named style requires.

    APA sorts alphabetically by surname; IEEE and Vancouver sort by order of
    first citation. Using the wrong order is the error that makes a reference
    list look wrong to anyone in the field, so this is not cosmetic.
    """
    entries = [r for r in refs if isinstance(r, dict) and (
        r.get("title") or r.get("authors") or r.get("author"))]
    if style["order"] == "citation":
        ordered = entries
    else:
        def _key(r: dict) -> str:
            a = _authors(r, style) or str(r.get("title") or "")
            return a.lstrip("[").lower()
        ordered = sorted(entries, key=_key)
    return [format_reference(r, style, i + 1 if style["numbered"] else None)
            for i, r in enumerate(ordered)]


# ── presentation canvases and layouts ──────────────────────────────────────
# PowerPoint's own slide sizes, in inches.
SLIDE_SIZES: dict[str, tuple[float, float]] = {
    "16:9": (13.333, 7.5),
    "16:10": (10.0, 6.25),
    "4:3": (10.0, 7.5),
    "a4": (10.833, 7.5),
    "letter": (10.0, 7.5),
    "ledger": (13.319, 9.99),
    "b4": (11.84, 8.88),
    "b5": (7.84, 5.88),
    "square": (10.0, 10.0),
    "story": (7.5, 13.333),
}
DEFAULT_SLIDE_SIZE = "16:9"

# PowerPoint ships 9 in an English install (2 more are CJK vertical-text).
# Each is what the generator does with the blocks on that slide.
SLIDE_LAYOUTS: dict[str, str] = {
    "title": "Title slide: title and subtitle only, no body.",
    "section": "Section header: large title used to open a section.",
    "title-content": "Title and one body area (bullets, a table, a chart).",
    "two-content": "Title and two side-by-side body areas.",
    "comparison": "Title, two headings, then two body areas - for contrasts.",
    "title-only": "Title with no body: used for a statement or a quote.",
    "blank": "No placeholders; content placed freely.",
    "content-caption": "Body area with a caption bar beside it.",
    "picture-caption": "Image with a caption beside it.",
}
DEFAULT_SLIDE_LAYOUT = "title-content"


def resolve_slide_size(name: str | None) -> tuple[float, float]:
    """(width_in, height_in) for a slide-size name or an aspect ratio."""
    if not name:
        return SLIDE_SIZES[DEFAULT_SLIDE_SIZE]
    raw = str(name).strip().lower()
    if raw in SLIDE_SIZES:
        return SLIDE_SIZES[raw]
    if ":" in raw:
        try:
            a, b = raw.split(":", 1)
            ratio = float(a) / float(b)
        except (ValueError, ZeroDivisionError):
            return SLIDE_SIZES[DEFAULT_SLIDE_SIZE]
        # Keep 10in on the short axis for landscape, which is what every
        # 16:9 deck in the wild is sized against.
        if ratio >= 1:
            return (10.0 * ratio, 10.0)
        return (10.0, 10.0 * ratio)
    compact = re.sub(r"[^a-z0-9]", "", raw)
    for k, v in SLIDE_SIZES.items():
        if re.sub(r"[^a-z0-9]", "", k) == compact:
            return v
    if compact in ("widescreen", "onScreenShow169"):
        return SLIDE_SIZES["16:9"]
    if compact in ("standard", "onscreenshow43"):
        return SLIDE_SIZES["4:3"]
    return SLIDE_SIZES[DEFAULT_SLIDE_SIZE]


def resolve_slide_layout(name: str | None) -> str:
    if not name:
        return DEFAULT_SLIDE_LAYOUT
    key = re.sub(r"[^a-z]", "", str(name).lower())
    if key in SLIDE_LAYOUTS:
        return key
    for k in SLIDE_LAYOUTS:
        if k.startswith(key) or key.startswith(k):
            return k
    return DEFAULT_SLIDE_LAYOUT


# ── the catalogue the model is shown ───────────────────────────────────────
def catalogue() -> dict:
    """Everything a caller may choose, for /v1/docgen/schema and the prompts.

    One function so the schema can never advertise a name the resolvers do
    not accept - the drift that hid the chart/image blocks until this was
    centralised.
    """
    return {
        "page_sizes": sorted(PAGE_SIZES),
        "default_page_size": DEFAULT_PAGE_SIZE,
        "orientations": list(ORIENTATIONS),
        "margins": {k: dict(zip(("top", "right", "bottom", "left"), v))
                    for k, v in MARGIN_PRESETS.items()},
        "default_margins": DEFAULT_MARGIN,
        "styles": {k: {
            "body_pt": v["body"], "leading_pt": v["leading"],
            "family": v["family"], "columns": v["columns"],
            "numbered_sections": v["numbered"],
            "align": v["align"],
        } for k, v in DOC_STYLES.items()},
        "default_style": DEFAULT_STYLE,
        "citation_styles": {k: {
            "heading": v["heading"], "order": v["order"],
            "numbered": v["numbered"]} for k, v in CITATION_STYLES.items()},
        "default_citation_style": DEFAULT_CITATION,
        "slide_sizes": sorted(SLIDE_SIZES),
        "default_slide_size": DEFAULT_SLIDE_SIZE,
        "slide_layouts": SLIDE_LAYOUTS,
        "default_slide_layout": DEFAULT_SLIDE_LAYOUT,
    }