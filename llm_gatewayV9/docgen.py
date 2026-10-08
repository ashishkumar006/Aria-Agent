"""Document generation: turn structured content into real office files.

Backs the console's Documents/Authoring section. Produces genuine
PDF (reportlab), PowerPoint (python-pptx), Word (python-docx) and Excel
(openpyxl) — no pandoc/LibreOffice/wkhtmltopdf dependency, because none
of those exist on this host and npm has no network access.

Content arrives as a small declarative document model (JSON) so the
caller — the agent, or the console's authoring form — describes WHAT to
make, not how to lay it out:

    {
      "title": "Q3 Report",
      "subtitle": "optional",
      "author": "optional",
      "blocks": [
        {"type": "heading", "level": 2, "text": "Summary"},
        {"type": "paragraph", "text": "…markdown-lite…"},
        {"type": "bullets", "items": ["…", "…"]},
        {"type": "numbers", "items": ["…"]},
        {"type": "table", "header": ["A", "B"], "rows": [["1", "2"]]},
        {"type": "pagebreak"}
      ]
    }

Every builder returns bytes plus the filename, and each is defensive:
a malformed block is skipped rather than raising, because a document
that is 90% right and downloadable beats a 500.
"""

from __future__ import annotations

import io
import os
import pathlib
import re
from typing import Any

# Project root: the directory holding this file. Used to reach the stored
# document uploads when an image block reuses one of the user's own figures.
ROOT = pathlib.Path(__file__).resolve().parent

# ── limits ───────────────────────────────────────────────────────────────────
MAX_BLOCKS = 400
MAX_TITLE = 200
MAX_TEXT = 20_000
MAX_TABLE_ROWS = 500
MAX_TABLE_COLS = 40
MAX_ROWS_SHEET = 2000


class DocGenError(ValueError):
    """Bad input the caller can fix (→ HTTP 400)."""


# ── model helpers ────────────────────────────────────────────────────────────

def _as_text(v: Any, limit: int = MAX_TEXT) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        v = str(v)
    return v.strip()[:limit]


# Block types the model asked for that this renderer could not use, and
# why. A renderer that silently discards input is indistinguishable from one
# that rendered everything, so a deck of blank slides looked like success.
_DROPPED: dict[str, str] = {}


def _note_drop(kind: str, why: str) -> None:
    _DROPPED[kind] = why


def dropped_blocks() -> list[str]:
    """Human-readable summary of ignored block types, for this generate() call."""
    return [f"{k} ({v})" for k, v in sorted(_DROPPED.items())]


def _num(v, default=None):
    """Coerce to a finite float. Models emit "92%", "1,200" and "" freely, and
    a chart with a non-numeric cell raises deep inside reportlab with an error
    that does not name the block."""
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return float(v) if _finite(v) else default
    s = str(v or "").strip().replace(",", "").rstrip("%$€£ ")
    if not s:
        return default
    try:
        f = float(s)
    except ValueError:
        return default
    return f if _finite(f) else default


def _finite(f: float) -> bool:
    return f == f and f not in (float("inf"), float("-inf"))


def _line_chart(b: dict, cats, series, width, height, x, y, shades):
    """A line chart drawn directly.

    reportlab's HorizontalLineChart hangs indefinitely on this input (three
    categories, one series - reproduced with a 30s watchdog), which takes the
    whole document render with it. A handful of Line shapes is both
    predictable and gives exact control over the axes.
    """
    from reportlab.graphics.shapes import (Circle, Drawing, Line, PolyLine,
                                          String)
    from reportlab.lib import colors

    d = Drawing(width + x + 30, height + y + 20)
    plot_w, plot_h = width, height
    lo = min(0.0, min(min(s["data"]) for s in series))
    hi = max(max(s["data"]) for s in series)
    span = (hi - lo) or 1.0
    top = lo + span * 1.12
    bot = lo

    def y_of(v):
        return y + plot_h * (1.0 - (v - bot) / (top - bot or 1.0))

    def x_of(i):
        if len(cats) <= 1:
            return x
        return x + plot_w * i / (len(cats) - 1)

    # Gridlines + y labels.
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        yy = y + plot_h * frac
        val = top - (top - bot) * frac
        d.add(Line(x, yy, x + plot_w, yy,
                   strokeColor=colors.HexColor("#ececf2"), strokeWidth=0.5))
        d.add(String(x - 4, yy - 2.5, _short_num(val), fontName="Helvetica",
                     fontSize=6.6, fillColor=colors.HexColor("#7a7a85")))
    # Axis lines.
    d.add(Line(x, y, x + plot_w, y, strokeColor=colors.HexColor("#b9b9c4"),
               strokeWidth=0.8))
    d.add(Line(x, y, x, y + plot_h, strokeColor=colors.HexColor("#b9b9c4"),
               strokeWidth=0.8))
    # The series.
    for si, s in enumerate(series):
        pts = [(x_of(i), y_of(v)) for i, v in enumerate(s["data"])]
        if len(pts) < 2:
            continue
        d.add(PolyLine(pts, strokeColor=shades[si % len(shades)],
                       strokeWidth=1.8))
        for px, py in pts:
            # Markers go straight onto the drawing: nesting a Drawing per
            # point positions unpredictably.
            d.add(Circle(px - 1.8, py - 1.8, 1.8,
                         fillColor=shades[si % len(shades)], strokeColor=None))
    # x labels.
    step = max(1, len(cats) // 12)
    for i, c in enumerate(cats):
        if i % step:
            continue
        d.add(String(x_of(i) - 12, y - 10, str(c)[:12], fontName="Helvetica",
                     fontSize=6.6, fillColor=colors.HexColor("#5c5c66")))
    return d


def _short_num(v: float) -> str:
    """Axis labels, not float noise: 28.615 becomes 28.6, 1200 becomes 1.2k."""
    a = abs(v)
    if a >= 10000:
        return f"{v / 1000:.0f}k"
    if a >= 100:
        return f"{v:.0f}"
    if a >= 1:
        return f"{v:.1f}".rstrip("0").rstrip(".")
    return f"{v:.3g}"


def _nice_step(raw: float) -> float:
    """Round an axis step to 1/2/5 x a power of ten, so ticks read as
    20/25/50/100 rather than 28.615."""
    try:
        r = float(raw)
    except (TypeError, ValueError):
        return 1.0
    if r <= 0 or r != r:
        return 1.0
    import math
    mag = 10 ** math.floor(math.log10(r))
    for mult in (1, 2, 2.5, 5, 10):
        cand = mult * mag
        if cand >= r:
            return cand
    return 10 * mag


MAX_CHART_CATS = 40
MAX_CHART_SERIES = 8


def _norm_chart(b: dict) -> dict | None:
    """A chart needs at least one numeric series aligned to the categories.
    Anything else renders as an empty axis, which looks like a bug in the
    document rather than in the request."""
    kind = str(b.get("chart") or b.get("kind") or "bar").strip().lower()
    kind = {"column": "bar", "vertical_bar": "bar", "vbar": "bar",
            "hbar": "bar", "horizontal_bar": "bar",
            "donut": "pie", "doughnut": "pie",
            "trend": "line"}.get(kind, kind)
    if kind not in ("bar", "line", "pie"):
        kind = "bar"

    cats_raw = b.get("categories") or b.get("labels") or b.get("x") or []
    if isinstance(cats_raw, str):
        cats_raw = [cats_raw]
    cats = [_as_text(c, 80) for c in cats_raw if _as_text(c, 80)][:MAX_CHART_CATS]
    if not cats:
        return None

    series_in = b.get("series") or b.get("data") or []
    if isinstance(series_in, dict):
        series_in = [{"name": k, "data": v} for k, v in series_in.items()]
    if isinstance(series_in, list) and series_in and not isinstance(
            series_in[0], (list, tuple, dict)):
        series_in = [{"name": b.get("name") or "value", "data": series_in}]

    series = []
    for s in series_in[:MAX_CHART_SERIES]:
        if isinstance(s, (list, tuple)):
            data, name = list(s), ""
        elif isinstance(s, dict):
            data = s.get("data") or s.get("values") or []
            name = _as_text(s.get("name") or s.get("label"), 60)
        else:
            continue
        if isinstance(data, (str, int, float)):
            data = [data]
        if not isinstance(data, list):
            continue
        vals = [_num(v) for v in data[:MAX_CHART_CATS]]
        # Pad a short series so every bar exists; a gap reads as a zero.
        vals += [0.0] * (len(cats) - len(vals))
        vals = vals[:len(cats)]
        if all(v is None for v in vals):
            continue
        series.append({"name": name, "data": [0.0 if v is None else v for v in vals]})

    if not series:
        return None
    return {"type": "chart", "chart": kind,
            "title": _as_text(b.get("title"), 200),
            "caption": _as_text(b.get("caption"), 300),
            "categories": cats, "series": series}


_DOCS_ROOT_NAME = "documents"


def _resolve_document(doc_id: str) -> pathlib.Path | None:
    """Locate an uploaded document's stored bytes.

    `doc_id` arrives from a model, so it is untrusted: it is validated to a
    plain id (no separators, no traversal) and then resolved under the
    documents root only. Never join an arbitrary string onto a path here."""
    did = str(doc_id or "").strip()
    if not re.match(r"^[A-Za-z0-9_.-]{1,64}$", did) or did in (".", ".."):
        return None
    for root in (ROOT / "state" / _DOCS_ROOT_NAME, ROOT / _DOCS_ROOT_NAME):
        cand = (root / f"{did}.source")
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


MAX_IMAGE_BYTES = 12 * 1024 * 1024


def _extract_figure(path: pathlib.Path, page: int, index: int = 0):
    """Pull one embedded raster out of a stored PDF, as (bytes, ext).

    Only EMBEDDED images are reusable. Rasterising a whole page needs a
    rendering engine this project does not depend on, so a page with no
    embedded figure fails loudly rather than pretending."""
    raw = path.read_bytes()
    if raw[:4] != b"%PDF":
        # A plain image upload can be embedded directly.
        if raw[:8] == b"\x89PNG\r\n\x1a\n" or raw[:2] == b"\xff\xd8":
            ext = "png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "jpeg"
            return raw, ext
        return None
    try:
        from pypdf import PdfReader
    except Exception:
        return None
    try:
        reader = PdfReader(str(path))
        n = max(1, min(int(page or 1), len(reader.pages)))
        imgs = list(reader.pages[n - 1].images)
        if not imgs:
            return None
        im = imgs[max(0, min(int(index or 0), len(imgs) - 1))]
        blob = im.data
        if not blob or len(blob) > MAX_IMAGE_BYTES:
            return None
        ext = {"png": "png", "jpeg": "jpeg", "jpg": "jpeg",
               "jpeg2000": "jpeg"}.get(getattr(im, "name", "").rsplit(".", 1)[-1].lower(), "")
        if not ext:
            ext = "png" if blob[:8] == b"\x89PNG\r\n\x1a\n" else "jpeg"
        return blob, ext
    except Exception:
        return None


def _norm_image(b: dict) -> dict | None:
    """An image block reuses a figure the USER uploaded. There is no web
    fetching here on purpose: hotlinking, SSRF and licensing make it a
    different product decision, and the content the user supplied is already
    on disk."""
    # Already resolved (the block went through _norm_blocks once and is being
    # normalised again). Without this the second pass saw no "document" key,
    # resolved nothing, and DROPPED the figure - so normalisation was not
    # idempotent and any re-entry silently lost every image.
    if isinstance(b.get("data"), (bytes, bytearray)) and b.get("data"):
        return {"type": "image", "data": bytes(b["data"]),
                "ext": b.get("ext") or "png",
                "page": int(_num(b.get("page"), 1) or 1),
                "caption": b.get("caption") or "",
                "width_mm": min(170.0, max(40.0, _num(b.get("width_mm"), 150.0)
                                           or 150.0)),
                "alt": b.get("alt") or b.get("caption") or ""}
    doc = b.get("document") or b.get("doc") or b.get("doc_id") or ""
    path = _resolve_document(doc)
    if path is None:
        return None
    page = _num(b.get("page"), 1) or 1
    idx = _num(b.get("index"), 0) or 0
    got = _extract_figure(path, int(page), int(idx))
    if not got:
        return None
    blob, ext = got
    return {"type": "image", "data": blob, "ext": ext,
            "page": int(page),
            "caption": _as_text(b.get("caption"), 300),
            "width_mm": min(170.0, max(40.0, _num(b.get("width_mm"), 150.0) or 150.0)),
            "alt": _as_text(b.get("alt") or b.get("caption"), 300)}


def _norm_blocks(raw: Any) -> list[dict]:
    """Validate/clip the block list. Raises DocGenError only when there is
    nothing at all to render."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise DocGenError("blocks must be a list")
    out: list[dict] = []
    for b in raw[:MAX_BLOCKS]:
        if not isinstance(b, dict):
            continue
        kind = str(b.get("type") or "").strip().lower()
        # One alias table, shared with the published schema, so the vocabulary
        # a caller is told about and the vocabulary the normaliser accepts can
        # never drift apart.
        kind = _BLOCK_ALIASES.get(kind, kind)
        if kind == "heading":
            txt = _as_text(b.get("text"), 400)
            if txt:
                try:
                    lvl = int(b.get("level") or 2)
                except (TypeError, ValueError):
                    lvl = 2
                out.append({"type": "heading", "level": min(3, max(1, lvl)),
                            "text": txt})
        elif kind == "paragraph":
            txt = _as_text(b.get("text"))
            if txt:
                out.append({"type": "paragraph", "text": txt})
        elif kind in ("bullets", "numbers"):
            # A model asked for "3-6 bullets per slide" and never shown the
            # JSON shape will send `{"type": "bullet", "text": "..."}` - and
            # that was SILENTLY DROPPED, because only `items` was read. The
            # decks it produced had a title per slide and no body at all.
            # Accept every reasonable spelling.
            items = b.get("items")
            if items is None:
                items = b.get("points") or b.get("values")
            if items is None:
                one = b.get("text")
                if one:
                    items = [one]
            if isinstance(items, str):
                items = [items]
            if not isinstance(items, list):
                _note_drop(kind, "no items")
                continue
            clean = [_as_text(i, 1000) for i in items[:200]]
            clean = [c for c in clean if c]
            if clean:
                out.append({"type": "bullets" if kind == "bullets" else "numbers",
                            "items": clean})
            else:
                _note_drop(kind, "empty")
        elif kind == "table":
            # "columns" and "data" are spellings a model reaches for; without
            # them the table was dropped without trace.
            header = b.get("header") or b.get("columns")
            rows = b.get("rows") or b.get("data")
            if not isinstance(rows, list) or not rows:
                continue
            cols = (len(header) if isinstance(header, list)
                    else max((len(r) for r in rows if isinstance(r, list)),
                             default=0))
            cols = min(max(1, cols), MAX_TABLE_COLS)
            head = ([_as_text(h, 200) for h in header][:cols]
                    if isinstance(header, list) and header else None)
            body = []
            for r in rows[:MAX_TABLE_ROWS]:
                if isinstance(r, list):
                    body.append([_as_text(c, 500) for c in r[:cols]])
                elif isinstance(r, dict):
                    body.append([_as_text(r.get(c), 500) for c in
                                 (head or list(r.keys()))[:cols]])
            if body:
                out.append({"type": "table", "header": head, "rows": body})
        elif kind == "pagebreak":
            out.append({"type": "pagebreak"})
        elif kind == "chart":
            c = _norm_chart(b)
            if c is None:
                _note_drop(kind, "no plottable series")
            else:
                out.append(c)
        elif kind == "image":
            pic = _norm_image(b)
            if pic is None:
                _note_drop(kind, "no resolvable source")
            else:
                out.append(pic)
        elif kind == "cover":
            out.append({"type": "cover",
                        "title": _as_text(b.get("title"), MAX_TITLE) or title,
                        "subtitle": _as_text(b.get("subtitle"), 300),
                        "meta": [_as_text(m, 160) for m in (b.get("meta") or [])
                                 if _as_text(m, 160)][:8]})
        elif kind == "quote":
            txt = _as_text(b.get("text"))
            if txt:
                out.append({"type": "quote", "text": txt})
        elif kind == "note":
            txt = _as_text(b.get("text") or b.get("items"))
            if txt:
                out.append({"type": "note", "text": txt})
        else:
            # Previously this branch did not exist: an unrecognised type was
            # dropped with no trace, so a deck whose blocks were all
            # `speaker_notes` rendered as 11 near-blank slides and the run
            # reported success. Record it so `generate` can warn.
            _note_drop(kind, "unknown type")
    return out


# ── the block vocabulary, in ONE place ────────────────────────────────────────
# The schema endpoint is the only thing an LLM reads to learn what a block
# looks like, and it had quietly fallen behind the normaliser: it documented
# 7 block types while the code accepted 10, and it advertised `bullets` and
# `header` only - so charts, images and covers were unreachable no matter how
# good the prompting was, and the model guessed `bullet`/`text` (which the
# normaliser accepts, but which the schema never said so).
#
# `tests/test_docgen_visual.py::test_schema_documents_every_block_type` fails if
# these two ever disagree again. Add a block type in ONE place.
_BLOCK_ALIASES: dict[str, str] = {
    "h": "heading",
    "p": "paragraph",
    "text": "paragraph",
    "bullet": "bullets",
    "ul": "bullets",
    "numbers": "numbers",
    "numbered": "numbers",
    "ol": "numbers",
    "table": "table",
    "pagebreak": "pagebreak",
    "page_break": "pagebreak",
    "page-break": "pagebreak",
    "chart": "chart",
    "graph": "chart",
    "plot": "chart",
    "image": "image",
    "figure": "image",
    "picture": "image",
    "cover": "cover",
    "quote": "quote",
    "callout": "quote",
    "note": "note",
    "notes": "note",
    "speaker_note": "note",
    "speaker_notes": "note",
}

# What a caller - usually an LLM - should send. `format` notes where a block
# degrades rather than renders natively.
BLOCK_SCHEMA: list[dict] = [
    {"type": "cover", "title": "string", "subtitle": "string",
     "meta": ["string"], "format": "pdf, pptx, docx",
     "note": "own title page. Emitted once, as the first block."},
    {"type": "heading", "level": 1, "text": "string", "format": "all",
     "note": "level 1-3. Level 1 is numbered in pdf and listed in the "
             "contents."},
    {"type": "paragraph", "text": "string", "format": "all",
     "note": "**bold**, `code` and [links](https://x) are rendered."},
    {"type": "bullets", "items": ["string"], "format": "all"},
    {"type": "numbers", "items": ["string"], "format": "all"},
    {"type": "quote", "text": "string", "format": "all"},
    {"type": "chart", "kind": "bar | line | pie",
     "categories": ["string"],
     "series": [{"name": "string", "data": [1, 2, 3]}],
     "title": "string", "format": "vector in pdf, NATIVE editable chart "
                                  "in pptx, data table in docx"},
    {"type": "image", "document": "<uploaded doc id>", "page": 1,
     "caption": "string", "width_mm": 140, "format": "all",
     "note": "A figure lifted from a document the USER uploaded. There is no "
             "URL fetching - use only `document` ids that already exist."},
    {"type": "table", "header": ["string"], "rows": [["string"]],
     "format": "all"},
    {"type": "note", "text": "string", "format": "pptx speaker notes"},
    {"type": "pagebreak", "format": "pdf, pptx"},
]


def block_types() -> list[str]:
    """Canonical block type names, for callers that need to enumerate them."""
    return [b["type"] for b in BLOCK_SCHEMA]


def spec_schema() -> dict:
    """The spec an LLM should build. Served by /v1/docgen/schema."""
    import doclayout as _lay
    cat = _lay.catalogue()
    return {
        "spec": {
            "title": "string, required unless blocks are given",
            "subtitle": "string",
            "author": "string",
            "toc": "bool - build a contents list from the level 1 headings",
            "running_header": "string - short text in the page header",
            "page_size": {
                "name": "ISO A/B/C, ANSI (letter, legal, tabloid), or book "
                        "sizes. Defaults to a4.",
                "choices": cat["page_sizes"],
            },
            "orientation": {"choices": cat["orientations"]},
            "margins": {
                "name": cat["default_margins"],
                "choices": sorted(cat["margins"]),
                "or_mm": {"top": 22, "right": 25, "bottom": 24, "left": 25},
            },
            "columns": "1-3 text columns. Defaults to the style's own count.",
            "style": {
                "name": "a typographic system: page margins, measure, body "
                        "size and leading, heading scale, alignment, accent "
                        "colour. Defaults to report.",
                "choices": sorted(cat["styles"]),
                "detail": cat["styles"],
            },
            "citation_style": {
                "name": "format for the reference list. Decides the heading, "
                        "whether entries are numbered, and whether the list "
                        "sorts alphabetically (apa, mla, chicago, harvard) "
                        "or by order of citation (ieee, vancouver, ama).",
                "choices": sorted(cat["citation_styles"]),
                "detail": cat["citation_styles"],
            },
            "slide_size": {
                "note": "pptx only. An aspect ratio (16:9, 4:3, 1:1) or a "
                        "named paper slide size.",
                "choices": cat["slide_sizes"],
            },
            "references": {
                "note": "Source list for a research document. Dicts are "
                        "formatted by `citation_style`; plain strings are "
                        "printed as given.",
                "fields": ["type", "authors", "year", "title", "container",
                           "volume", "issue", "pages", "url"],
            },
            "blocks": BLOCK_SCHEMA,
        },
        "sheets": "xlsx only: [{name, header, rows}]",
        "block_aliases": _BLOCK_ALIASES,
    }


_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_CODE = re.compile(r"`([^`]+)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _strip_md(s: str) -> str:
    s = _MD_LINK.sub(r"\1 (\2)", s)
    s = _MD_BOLD.sub(r"\1", s)
    s = _MD_CODE.sub(r"\1", s)
    return re.sub(r"[*_`]+", "", s)


def _safe_filename(title: str, ext: str) -> str:
    """An ASCII, header-safe slug. The title is model-written free text, and
    the artifact descriptor (agent-written) round-trips into a
    Content-Disposition header further down the chain — parentheses, colons,
    quotes and non-ASCII there produced a header the browser refuses, which
    silently cancels the download. So: strip to characters legal inside a
    quoted-string, collapse whitespace to dashes, and cap the length."""
    base = re.sub(r"[^A-Za-z0-9 _.-]+", "", title or "document").strip()
    base = re.sub(r"\s+", "-", base)[:80].strip("-._") or "document"
    return f"{base}.{ext}"


# ── PDF ──────────────────────────────────────────────────────────────────────

def _norm_references(raw: Any) -> list[dict]:
    """Accept references as dicts, or as plain strings (formatted as given).

    A string reference is passed through untouched rather than being guessed
    into fields: an invented author or year in a reference list is worse than
    an unstyled one.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise DocGenError("references must be a list")
    out: list[dict] = []
    for r in raw[:200]:
        if isinstance(r, str):
            if r.strip():
                out.append({"title": r.strip(), "type": "misc"})
        elif isinstance(r, dict):
            out.append({k: v for k, v in r.items() if v not in (None, "")})
    return out


def build_pdf(spec: dict) -> tuple[bytes, str]:
    """Render a report.

    The first version of this function produced technically-valid output that
    read like a text dump: one heading style for every level, bullets with no
    hanging indent, a "quote" that was just smaller grey text, table rows with
    no vertical padding, and no page numbers anywhere. Rendering it and
    LOOKING at it is what caught all of that; the shapes below are the result.
    """
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (KeepTogether, PageBreak, Paragraph,
                                    SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    title = _as_text(spec.get("title"), MAX_TITLE)
    blocks = _norm_blocks(spec.get("blocks"))
    if not title and not blocks:
        raise DocGenError("nothing to render: give a title or some blocks")

    INK = colors.HexColor("#111114")
    MUTED = colors.HexColor("#5c5c66")
    RULE = colors.HexColor("#d8d8e0")
    HEAD_BG = colors.HexColor("#eeeff5")
    ZEBRA = colors.HexColor("#f8f8fb")

    # ── page geometry and typographic style ──────────────────────────────
    # These used to be hardcoded: A4, 24mm margins, 10.5/15.2 Helvetica. One
    # size and one type system means a request for a Letter report, a book
    # page or a newsletter renders as the same page with different numbers -
    # the layout options existed on paper but not in the generator.
    import doclayout as _lay
    _style = _lay.resolve_style(spec.get("style"))
    _page_mm = _lay.resolve_page_size(spec.get("page_size"))
    _orient = _lay.resolve_orientation(spec.get("orientation"))
    if _orient == "landscape":
        _page_mm = (max(_page_mm), min(_page_mm))
    _page = (_page_mm[0] * mm, _page_mm[1] * mm)
    _margins_mm = _lay.resolve_margins(
        spec.get("margins") if isinstance(spec.get("margins"), str) else None,
        _page_mm,
        spec.get("margins") if isinstance(spec.get("margins"), dict) else None)
    _margins_mm = _lay.measure_to_margins(_page_mm[0], _style, _margins_mm)
    MT, MR, MB, ML = (_m * mm for _m in _margins_mm)
    MARGIN = ML

    _cols = _num(spec.get("columns"), 0) or _style.get("columns") or 1
    _cols = max(1, min(3, int(_cols)))
    _family = _style.get("family", "sans")
    BODY = float(_style.get("body") or 10.5)
    LEAD = float(_style.get("leading") or 15.2)
    SCALE = float(_style.get("scale") or 1.32)
    ACCENT_HEX = _style.get("accent") or "#5b4bd6"
    ACCENT = colors.HexColor(ACCENT_HEX)
    HEAD_BG = colors.HexColor(ACCENT_HEX + "18")
    SERIF = _family == "serif"
    F_BODY = "Times-Roman" if SERIF else "Helvetica"
    F_BOLD = "Times-Bold" if SERIF else "Helvetica-Bold"
    F_BOLDIT = "Times-BoldItalic" if SERIF else "Helvetica-BoldOblique"
    F_IT = "Times-Italic" if SERIF else "Helvetica-Oblique"
    F_CAPT = F_BODY
    ALIGN = (TA_JUSTIFY if _style.get("align") == "justify" else TA_LEFT)
    INDENT_FIRST = bool(_style.get("indent_first"))

    buf = io.BytesIO()
    if _cols > 1:
        from reportlab.platypus import (BaseDocTemplate, Frame,
                                        PageTemplate)
        _gap = 7 * mm
        _col_w = (_page[0] - ML - MR - _gap * (_cols - 1)) / _cols
        # BaseDocTemplate starts with NO page templates, so the frames have to
        # be built and attached explicitly. reportlab lays a template's frames
        # out side by side across the page, which is exactly a column layout.
        _frames = []
        for _i in range(_cols):
            _f = Frame(id=f"c{_i}",
                       x1=ML + _i * (_col_w + _gap), y1=MB,
                       width=_col_w, height=_page[1] - MT - MB,
                       leftPadding=0, rightPadding=0,
                       topPadding=0, bottomPadding=0, showBoundary=0)
            # Frame.__init__ does not take these; they are attributes, and the
            # default origin is the frame box rather than the page, so without
            # this every column lands in the same place.
            _f.xOrigin = "absolute"
            _f.yOrigin = "absolute"
            _frames.append(_f)
        doc = BaseDocTemplate(
            buf, pagesize=_page, leftMargin=ML, rightMargin=MR,
            topMargin=MT, bottomMargin=MB,
            title=title or "document",
            author=_as_text(spec.get("author"), 120) or "",
            pageTemplates=[PageTemplate(id="cols", frames=_frames)])
    else:
        doc = SimpleDocTemplate(
            buf, pagesize=_page,
            leftMargin=ML, rightMargin=MR,
            topMargin=MT, bottomMargin=MB,
            title=title or "document",
            author=_as_text(spec.get("author"), 120) or "")

    ss = getSampleStyleSheet()
    st_title = ParagraphStyle("t", parent=ss["Title"], fontName="Helvetica-Bold",
                              fontSize=19, leading=23, spaceAfter=4,
                              textColor=INK, alignment=TA_LEFT)
    st_sub = ParagraphStyle("s", parent=ss["Normal"], fontSize=10.5, leading=14,
                            textColor=MUTED, spaceAfter=2)
    st_byline = ParagraphStyle("by", parent=st_sub, spaceAfter=10)

    def _furniture(canv, document):
        """Footer: a hairline rule and `page N`. A briefing with no page
        numbers cannot be referenced, printed, or checked against a claim.
        A running header carries the document title on pages after the
        first, which is what makes a stacked printout identifiable."""
        canv.saveState()
        pg = getattr(document, "page", 1)
        total = getattr(document, "_total_pages", 0)
        if pg > 1 and title:
            canv.setFont("Helvetica", 7.6)
            canv.setFillColor(colors.HexColor("#8b8b96"))
            canv.drawString(ML, _page[1] - 13 * mm, title[:78])
            canv.setStrokeColor(colors.HexColor("#ececf2"))
            canv.setLineWidth(0.4)
            canv.line(ML, _page[1] - 14.6 * mm, _page[0] - MR, _page[1] - 14.6 * mm)
        canv.setStrokeColor(RULE)
        canv.setLineWidth(0.5)
        y = 16 * mm
        canv.line(ML, y + 4 * mm, _page[0] - MR, y + 4 * mm)
        canv.setFont(F_BODY, 8)
        canv.setFillColor(MUTED)
        label = title[:70] if title else "document"
        canv.drawString(ML, y - 2 * mm, label)
        page_label = f"page {pg}" if not total else f"page {pg} of {total}"
        canv.drawRightString(_page[0] - MR, y - 2 * mm, page_label)
        canv.restoreState()
    st_h1 = ParagraphStyle("h1", parent=ss["Heading1"], fontName=F_BOLD,
                               fontSize=round(BODY * SCALE, 1),
                               leading=round(BODY * SCALE * 1.22, 1),
                               spaceBefore=round(LEAD, 1), spaceAfter=round(LEAD * 0.34, 1),
                               textColor=INK, keepWithNext=1)
    st_h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName=F_BOLD,
                               fontSize=round(BODY * (SCALE - 0.12), 1),
                               leading=round(BODY * (SCALE - 0.12) * 1.22, 1),
                               spaceBefore=round(LEAD * 0.75, 1),
                               spaceAfter=round(LEAD * 0.2, 1),
                               textColor=INK, keepWithNext=1)
    st_h3 = ParagraphStyle("h3", parent=ss["Heading3"], fontName=F_BOLDIT,
                               fontSize=round(BODY * 1.03, 1),
                               leading=round(LEAD * 0.92, 1),
                               spaceBefore=round(LEAD * 0.6, 1),
                               spaceAfter=round(LEAD * 0.14, 1),
                               textColor=colors.HexColor("#2c2c33"), keepWithNext=1)

    st_p = ParagraphStyle("p", parent=ss["BodyText"], fontName=F_BODY,
                              fontSize=BODY, leading=LEAD,
                              spaceAfter=round(LEAD * 0.46, 1),
                              textColor=colors.HexColor("#1d1d22"),
                              alignment=ALIGN,
                              # Widow/orphan control: reportlab defaults to
                              # allowing a single stranded line at a page break,
                              # which is the classic "one line alone at the top
                              # of a page" artefact.
                              allowWidows=0, allowOrphans=0,
                              firstLineIndent=(BODY if INDENT_FIRST else 0))
    st_li = ParagraphStyle("li", parent=st_p, leftIndent=15, firstLineIndent=-10,
                               spaceAfter=round(LEAD * 0.2, 1), bulletIndent=2)
    st_li_num = ParagraphStyle("lin", parent=st_li, leftIndent=18,
                                   firstLineIndent=-13)
    st_q = ParagraphStyle("q", parent=st_p, fontSize=round(BODY * 0.99, 1),
                              leading=round(LEAD * 0.95, 1),
                              textColor=colors.HexColor("#33333c"), firstLineIndent=0)
    st_qmark = ParagraphStyle("qm", parent=st_p, fontName=F_BOLD,
                                  fontSize=round(BODY * 1.24, 1),
                                  leading=round(LEAD, 1), textColor=ACCENT)
    st_cell = ParagraphStyle("c", parent=ss["BodyText"], fontName=F_BODY,
                                 fontSize=round(BODY * 0.86, 1),
                                 leading=round(BODY * 1.12, 1), textColor=INK,
                                 allowWidows=0, allowOrphans=0)
    st_cellh = ParagraphStyle("ch", parent=st_cell, fontName=F_BOLD,
                                  textColor=colors.white)
    st_cellk = ParagraphStyle("ck", parent=st_cell, fontName=F_BOLD)
    st_foot = ParagraphStyle("f", parent=ss["Normal"], fontName=F_BODY,
                                 fontSize=8, leading=9.6, textColor=MUTED)
    st_cap = ParagraphStyle("cap", parent=ss["Normal"], fontName=F_CAPT,
                                fontSize=round(BODY * 0.82, 1),
                                leading=round(BODY * 1.05, 1), textColor=MUTED)
    st_covert = ParagraphStyle("ct", parent=st_title, fontName=F_BOLD,
                                   fontSize=round(BODY * 2.5, 1),
                                   leading=round(BODY * 2.9, 1), spaceAfter=8)
    st_covers = ParagraphStyle("cs", parent=st_sub, fontName=F_BODY,
                                   fontSize=round(BODY * 1.14, 1),
                                   leading=round(BODY * 1.5, 1))
    st_coverm = ParagraphStyle("cm", parent=st_cap, fontName=F_CAPT,
                                   fontSize=round(BODY * 0.9, 1),
                                   leading=round(BODY * 1.4, 1), spaceBefore=3)
    st_toch = ParagraphStyle("tch", parent=ss["Heading1"], fontName=F_BOLD,
                                 fontSize=round(BODY * SCALE, 1),
                                 leading=round(BODY * SCALE * 1.22, 1),
                                 spaceAfter=6, textColor=INK, keepWithNext=1)
    st_toc1 = ParagraphStyle("t1", parent=ss["Normal"], fontName=F_BODY,
                                 fontSize=round(BODY * 0.95, 1), leading=LEAD,
                                 spaceAfter=1)
    st_toc2 = ParagraphStyle("t2", parent=st_toc1, leftIndent=14,
                                 textColor=colors.HexColor("#3a3a44"))
    # Reference entries. A hanging indent is not decoration: it is what makes the
    # second line of a citation readable as a continuation of the first.
    st_ref = ParagraphStyle("ref", parent=st_p,
                                fontSize=round(BODY * 0.9, 1),
                                leading=round(BODY * 1.18, 1),
                                leftIndent=14, firstLineIndent=-14,
                                spaceAfter=round(LEAD * 0.3, 1),
                                alignment=TA_LEFT)

    avail_w = _page[0] - ML - MR
    if _cols > 1:
        # In a column layout the usable width is the COLUMN, not the page.
        # Leaving it as the page width made charts and tables draw straight
        # across the gutter and into the next column - caught by rendering a
        # newsletter and LOOKING at it, not by any assertion.
        avail_w = _col_w

    # Section numbering. A multi-page report without numbers is hard to
    # navigate and impossible to cross-reference from a chat answer.
    counters = [0, 0]

    def _numbered(text: str, level: int) -> str:
        if level == 1:
            counters[0] += 1
            counters[1] = 0
            return f"{counters[0]}. {_strip_md(text)}"
        counters[1] += 1
        return f"{counters[0]}.{counters[1]} {_strip_md(text)}"

    def _heading(txt, level):
        if number_sections:
            txt = _numbered(txt, level)
        style = {1: st_h1, 2: st_h2}.get(level, st_h3)
        return Paragraph(_rich(txt), style)

    def _quote(txt):
        """A quote needs a rule and an indent to read as a quote. The old
        style only shrank and greyed it, so it was indistinguishable from a
        caption. A two-column table is the reliable way to draw the rule."""
        body = Paragraph(_rich(txt), st_q)
        mark = Paragraph("\u201c", st_qmark)
        t = Table([["", mark], ["", body]], colWidths=[2.2 * mm, None])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (0, -1), ACCENT),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (0, 0), 2),
            ("BOTTOMPADDING", (0, -1), (0, -1), 2),
            ("TOPPADDING", (1, 0), (1, 0), 0),
            ("BOTTOMPADDING", (0, -1), (1, -1), 8),
            ("LEFTPADDING", (1, 0), (1, -1), 7),
        ]))
        # KeepTogether: this is a 2-row table (rule + quote mark, then rule +
        # body). Left splittable, reportlab broke it across pages and left the
        # accent bar and the quote mark stranded at the foot of one page with
        # the sentence itself on the next.
        return [KeepTogether([t]), Spacer(1, 8)]

    def _table(b):
        head = b.get("header")
        data = []
        if head:
            data.append([Paragraph(_rich(h), st_cellh) for h in head])
        for row in b["rows"]:
            cells = []
            for ci, c in enumerate(row):
                # The first column is a label column; bolding it is what makes
                # a comparison table scannable.
                style = st_cellk if (ci == 0 and head) else st_cell
                cells.append(Paragraph(_rich(c), style))
            data.append(cells)
        ncols = max(len(r) for r in data)
        for r in data:
            r.extend([""] * (ncols - len(r)))
        cmds = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            # Rows were cramped: 4pt of side padding and none vertically, so
            # the text touched the rules.
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
            ("BOX", (0, 0), (-1, -1), 0.6, RULE),
        ]
        if head:
            cmds += [
                ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
                ("TOPPADDING", (0, 0), (-1, 0), 6),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            ]
            for i in range(1, len(data)):
                if i % 2 == 0:
                    cmds.append(("BACKGROUND", (0, i), (-1, i), ZEBRA))
        else:
            cmds.append(("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE))
        tbl = Table(data, repeatRows=1 if head else 0)
        tbl.setStyle(TableStyle(cmds))
        return [tbl, Spacer(1, 11)]

    def _chart(b, avail_w):
        """Vector chart. reportlab draws these as real paths, so they stay
        crisp at any zoom and add kilobytes rather than the megabyte an
        embedded raster would."""
        from reportlab.graphics.charts.barcharts import (HorizontalBarChart,
                                                         VerticalBarChart)
        from reportlab.graphics.charts.linecharts import HorizontalLineChart
        from reportlab.graphics.charts.piecharts import Pie
        from reportlab.graphics.shapes import Drawing

        cats = b["categories"]
        series = b["series"]
        kind = b["chart"]
        w = avail_w
        h = 150.0 if kind == "pie" else (170.0 if kind == "line" else 190.0)
        pad = 34.0
        shades = [ACCENT, colors.HexColor("#9c8ef0"),
                  colors.HexColor("#c4b5fd"), colors.HexColor("#7c6ee6"),
                  colors.HexColor("#b9b3d8"), colors.HexColor("#8f88b8"),
                  colors.HexColor("#d7d3ee"), colors.HexColor("#6f6a90")]

        # A legend goes UNDER the plot, not in a right-hand gutter: reportlab's
        # Legend lays out from its own anchor and kept spilling past the
        # drawing edge, so the series labels were clipped off the page.
        want_legend = len(series) > 1 or bool(series[0].get("name"))
        legend_h = 0.0
        if want_legend and kind != "pie":
            legend_h = 9.0 * len(series) + 6.0

        if kind == "pie":
            pie = Pie()
            pie.x, pie.y = 4, 4
            pie.width, pie.height = h - 8, h - 8
            first = series[0]["data"]
            pie.data = [max(0.0, v) for v in first]
            pie.labels = [str(c)[:14] for c in cats]
            pie.sideLabels = False
            for i in range(len(pie.slices)):
                pie.slices[i].fillColor = shades[i % len(shades)]
            chart_obj = pie
            # A pie has room for its legend beside it.
            w = min(avail_w, h + 150.0)
        elif kind == "line":
            lc = _line_chart(b, cats, series, w - pad - 34, h - pad - 14,
                              pad + 14, pad, shades)
            chart_obj = lc
        else:
            bc = (HorizontalBarChart() if kind == "bar" and
                  len(series) == 1 and len(cats) > 6 else VerticalBarChart())
            bc.x, bc.y, bc.height, bc.width = pad + 14, pad, h - pad - 14, w - pad - 34
            bc.data = [s["data"] for s in series]
            bc.categoryAxis.categoryNames = cats
            bc.barWidth = (max(3.0, (w - pad - 60) / max(1, len(cats) * 1.6))
                           if isinstance(bc, VerticalBarChart) else 8)
            bc.groupSpacing = 10
            for i in range(len(bc.bars)):
                bc.bars[i].fillColor = shades[i % len(shades)]
                bc.bars[i].strokeColor = None
            chart_obj = bc

        hi = max(max(s["data"]) for s in series) if series else 1.0
        lo = min(min(s["data"]) for s in series) if series else 0.0
        if hasattr(chart_obj, "valueAxis"):
            chart_obj.valueAxis.valueMin = min(0.0, lo)
            top = hi if hi > 0 else 1.0
            # Round the top and the step to something a reader recognises.
            # Left raw, a chart of 94/91/82 printed an axis of
            # 28.615 / 57.23 / 85.845 / 114.46, which reads as noise.
            step = _nice_step(top / 4.0)
            top_nice = step * 4.0
            chart_obj.valueAxis.valueMax = top_nice
            chart_obj.valueAxis.valueStep = step
            try:
                chart_obj.valueAxis.labelTextFormat = "%0.4g"
            except Exception:
                pass
        d = Drawing(w, h + legend_h)
        d.add(chart_obj)
        # A legend is not decoration: two unnamed colour series is an
        # unreadable chart, and this is exactly the case the model produces
        # when it plots a comparison.
        if legend_h > 0:
            from reportlab.graphics.charts.legends import Legend
            leg = Legend()
            leg.colorNamePairs = []
            # Only attributes Legend actually declares: it rejects anything
            # else with an AttributeError from attrmap.
            leg.fontName = "Helvetica"
            leg.fontSize = 7.4
            leg.alignment = "left"
            leg.columnMaximum = 1
            leg.dx = 7
            leg.dy = 5
            leg.deltax = 0
            leg.deltay = 9
            leg.boxAnchor = "nw"
            leg.dxTextSpace = 4
            targets = (getattr(chart_obj, "bars", None)
                       or getattr(chart_obj, "lines", None))
            if targets:
                for i, s in enumerate(series):
                    leg.colorNamePairs.append(
                        (targets[i], s.get("name") or f"series {i + 1}"))
                leg.x = pad + 6
                leg.y = 3
                d.add(leg)
        out = []
        if b.get("title"):
            out.append(Paragraph(_rich(b["title"]), st_h3))
        out.append(d)
        out.append(Spacer(1, 5))
        if b.get("caption"):
            out.append(Paragraph(_rich(b["caption"]), st_cap))
        out.append(Spacer(1, 9))
        return out

    def _image(b, avail_w):
        """A figure lifted from one of the user's own uploaded documents."""
        from reportlab.platypus import Image as RLImage
        from PIL import Image as PILImage

        try:
            with PILImage.open(io.BytesIO(b["data"])) as im:
                iw, ih = im.size
            if iw <= 0 or ih <= 0:
                return []
            avail = avail_w * 0.62
            if b.get("width_mm"):
                avail = b["width_mm"] * 3.0
            w = avail
            h = w * ih / iw
            if h > 330:          # never let a figure eat a whole page
                h = 330
                w = h * iw / ih
            # Keep the bytes in memory and hand reportlab a stream. A temp
            # FILE was deleted in a finally block, but reportlab reads an
            # Image lazily at draw time - long after that block ran - so the
            # figure silently never appeared in the PDF.
            flow = RLImage(io.BytesIO(b["data"]), width=w, height=h)
            flow.hAlign = "CENTER"
        except Exception:
            return []
        out = [flow, Spacer(1, 4)]
        if b.get("caption"):
            out.append(Paragraph(_rich(b["caption"]), st_cap))
        out.append(Spacer(1, 9))
        return out

    def flowable_for(b):
        t = b["type"]
        if t == "heading":
            return _heading(b["text"], b.get("level", 1))
        if t == "paragraph":
            return Paragraph(_rich(b["text"]), st_p)
        if t == "quote":
            return _quote(b["text"])
        if t == "chart":
            return _chart(b, avail_w)
        if t == "image":
            return _image(b, avail_w)
        if t == "cover":
            # No leading PageBreak: the cover is emitted by the story
            # assembly, which follows it with one. The block carried its own
            # break, which produced a stray blank page before the cover.
            return [Spacer(1, 78 * mm),
                    Paragraph(_rich(b.get("title") or ""), st_covert),
                    Spacer(1, 6),
                    *([Paragraph(_rich(b["subtitle"]), st_covers)]
                      if b.get("subtitle") else []),
                    Spacer(1, 18)]
        if t in ("bullets", "numbers"):
            out = []
            ordered = t == "numbers"
            for i, item in enumerate(b["items"], 1):
                style = st_li_num if ordered else st_li
                out.append(Paragraph(_rich(item), style,
                                     bulletText=(f"{i}." if ordered else "•")))
            out.append(Spacer(1, 4))
            return out
        if t == "table":
            return _table(b)
        return None

    story = []
    # A document long enough to navigate gets a contents list; a two-section
    # note does not need one.
    h1s = [b for b in blocks if b.get("type") == "heading"
           and (b.get("level", 1) == 1)]
    want_toc = bool(spec.get("toc")) if spec.get("toc") is not None \
        else (len(h1s) >= 4)
    number_sections = bool(spec.get("number_sections", want_toc))

    has_cover = any(b.get("type") == "cover" for b in blocks)
    if has_cover:
        # The cover owns page one, so the running title block is redundant.
        cover = next(b for b in blocks if b.get("type") == "cover")
        story.extend(flowable_for(cover) or [])
        for line in (cover.get("meta") or []):
            story.append(Paragraph(_rich(line), st_coverm))
        story.append(PageBreak())
    elif title:
        story.append(Paragraph(_rich(title), st_title))
        rule = Table([[""]], colWidths=[34 * mm], rowHeights=[1.6])
        rule.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), ACCENT),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                  ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                  ("TOPPADDING", (0, 0), (-1, -1), 0),
                                  ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        # reportlab centres a narrow table by default, which floated the rule
        # into the middle of the page beside the title.
        rule.hAlign = "LEFT"
        story.append(rule)
        story.append(Spacer(1, 7))
    sub = _as_text(spec.get("subtitle"), 300)
    if sub and not has_cover:
        story.append(Paragraph(_rich(sub), st_sub))
    au = _as_text(spec.get("author"), 120)
    if au and not has_cover:
        story.append(Paragraph(_rich(au), st_byline))
    elif (sub or au) and not has_cover:
        story.append(Spacer(1, 10))

    toc = None
    if want_toc:
        from reportlab.platypus.tableofcontents import TableOfContents
        toc = TableOfContents()
        toc.levelStyles = [st_toc1, st_toc2]
        # The cover already broke the page; a second break here left an empty
        # sheet between the cover and the contents.
        if not has_cover:
            story.append(PageBreak())
        story.append(Paragraph("Contents", st_toch))
        story.append(toc)
        story.append(PageBreak())
    elif has_cover and len(story) and not isinstance(story[-1], PageBreak):
        pass  # the cover's own break already separates it from the body

    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b["type"] == "pagebreak":
            story.append(PageBreak())
            i += 1
            continue
        if b["type"] == "cover":
            i += 1                      # already emitted as page one
            continue
        made = flowable_for(b)
        if made is None:
            i += 1
            continue
        # Never let a heading be the last thing on a page: bind it to the
        # block that follows it.
        if b["type"] == "heading":
            nxt = blocks[i + 1] if i + 1 < len(blocks) else None
            if nxt is not None and nxt["type"] not in ("pagebreak", "cover"):
                nxt_f = flowable_for(nxt)
                if nxt_f is not None:
                    if isinstance(nxt_f, list):
                        story.append(KeepTogether([made] + nxt_f[:1]))
                        i += 2
                        for extra in nxt_f[1:]:
                            story.append(extra)
                    else:
                        story.append(KeepTogether([made, nxt_f]))
                        i += 2
                    continue
        if isinstance(made, list):
            story.extend(made)
        else:
            story.append(made)
        i += 1

    # Reference list. A research pipeline that gathers sources and then prints
    # no reference list is the single most visible quality gap: the reader
    # cannot check a claim. `citation_style` picks the heading, the ordering
    # and the numbering, because APA alphabetical and IEEE order-of-citation
    # are visibly different documents.
    _refs = _norm_references(spec.get("references"))
    if _refs:
        _cite = _lay.resolve_citation(spec.get("citation_style"))
        _lines = _lay.build_reference_list(_refs, _cite)
        if _lines:
            story.append(PageBreak())
            story.append(Paragraph(_esc(_cite["heading"]), st_toch))
            story.append(Spacer(1, 4))
            for _line in _lines:
                story.append(Paragraph(_esc(_line), st_ref))
            story.append(Spacer(1, 6))

    if len(story) == 0:
        raise DocGenError("nothing to render: give a title or some blocks")

    # A table of contents needs a second pass to learn its page numbers.
    # `onPageEnd` is not a keyword SimpleDocTemplate.build accepts, so the
    # TOC hook is the documented mechanism: a subclass reporting each
    # heading through afterFlowable, driven by multiBuild.
    toc_hook = toc

    if toc is not None:
        # Subclass whatever template we actually built. Two mistakes were
        # possible here and both cost a document: hardcoding SimpleDocTemplate
        # breaks the multi-column BaseDocTemplate path, and reassigning
        # `doc.__class__` to SimpleDocTemplate in the fallback silently throws
        # away the page frames, so a two-column document rendered as an empty
        # file.
        _base = type(doc)

        class _TocDoc(_base):        # type: ignore[misc,valid-type]
            def afterFlowable(self, flowable):
                if toc_hook is None or not isinstance(flowable, Paragraph):
                    return
                style = getattr(flowable, "style", None)
                if style is st_h1:
                    level = 1
                elif style is st_h2:
                    level = 2
                else:
                    return
                try:
                    toc_hook.notify("TOCEntry",
                                    (level, flowable.getPlainText(), self.page))
                except Exception:
                    pass

        doc.__class__ = _TocDoc

    if _cols > 1:
        # A BaseDocTemplate takes its page furniture from the page template,
        # not from build() kwargs - passing onFirstPage here is a TypeError,
        # and the broad except below then hid it and reported an empty file.
        for _tpl in getattr(doc, "pageTemplates", []):
            _tpl.onPage = _furniture
        _build_kw = {}
    else:
        _build_kw = {"onFirstPage": _furniture, "onLaterPages": _furniture}
    try:
        doc.multiBuild(story, **_build_kw)
    except Exception:
        # A TOC that cannot resolve its own page numbers must not cost the
        # user their document.
        buf = io.BytesIO()
        if _cols > 1:
            for _tpl in getattr(doc, "pageTemplates", []):
                _tpl.onPage = _furniture
            doc.__class__ = BaseDocTemplate
            try:
                doc.build(story)
            except Exception:
                pass
        else:
            doc.__class__ = SimpleDocTemplate
            doc.canv = None
            try:
                doc.build(story, onFirstPage=_furniture, onLaterPages=_furniture)
            except Exception:
                pass
    data = buf.getvalue()
    if not data:
        raise DocGenError("the PDF came out empty")
    return data, _safe_filename(title, "pdf")


def _esc(s: str) -> str:
    """Escape XML-ish chars for reportlab Paragraph (which is XML)."""
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


# reportlab Paragraph parses a small inline markup dialect, so a PDF can
# render emphasis properly. Previously build_pdf only ever called _esc, so a
# block written as "**vector index**" shipped to the reader with literal
# asterisks - and author.md explicitly tells the model to mark terms that
# way. Stripping the markers would lose the emphasis; translating it keeps it.
_MD_BOLD_R = re.compile(r"\*\*(.+?)\*\*", re.S)
_MD_ITAL_R = re.compile(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])")
_MD_CODE_R = re.compile(r"`([^`\n]+)`")
_MD_LINK_R = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")


def _rich(s: str) -> str:
    """Escape for reportlab, then translate inline markdown to its markup."""
    out = _esc(s)
    out = _MD_LINK_R.sub(r"\1 (\2)", out)
    out = _MD_CODE_R.sub(r'<font name="Courier">\1</font>', out)
    out = _MD_BOLD_R.sub(r"<b>\1</b>", out)
    out = _MD_ITAL_R.sub(r"<i>\1</i>", out)
    # Any surviving emphasis markers were unbalanced; drop them rather than
    # print them.
    return re.sub(r"\*\*?", "", out)


# ── PowerPoint ───────────────────────────────────────────────────────────────

def _pptx_chart(slide, b: dict, left, top, width, advance) -> None:
    """A native, editable PowerPoint chart. Rendering the data as a picture
    would have been far less code and produced a deck nobody could update."""
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
    from pptx.util import Emu

    kind = b.get("chart") or "bar"
    cd = CategoryChartData()
    cd.categories = list(b["categories"])
    for s in b["series"]:
        cd.add_series(s.get("name") or "series", tuple(s["data"]))
    types = {"bar": (XL_CHART_TYPE.COLUMN_CLUSTERED
                     if len(b["categories"]) > 6
                     else XL_CHART_TYPE.BAR_CLUSTERED),
             "line": XL_CHART_TYPE.LINE_MARKERS,
             "pie": XL_CHART_TYPE.PIE}
    height = int(Emu(3400000))
    advance(slide, Emu(3600000))
    try:
        frame = slide.shapes.add_chart(types.get(kind, types["bar"]),
                                       left, top, width, height, cd)
    except Exception:
        return
    ch = frame.chart
    try:
        if b.get("title"):
            ch.has_title = True
            ch.chart_title.text_frame.text = _strip_md(b["title"])[:120]
        else:
            ch.has_title = False
        ch.has_legend = len(b["series"]) > 1 or bool(
            (b["series"][0] or {}).get("name"))
        if ch.has_legend:
            ch.legend.position = XL_LEGEND_POSITION.BOTTOM
            ch.legend.include_in_layout = False
    except Exception:
        pass


def _pptx_image(slide, b: dict, left, top, width, advance) -> None:
    """Reuse a figure from the user's own uploaded document."""
    from pptx.util import Emu
    from PIL import Image as PILImage
    try:
        with PILImage.open(io.BytesIO(b["data"])) as im:
            iw, ih = im.size
        if iw <= 0 or ih <= 0:
            return
        w = int(width)
        h = int(w * ih / iw)
        if h > Emu(4200000):
            h = int(Emu(4200000))
            w = int(h * iw / ih)
        advance(slide, Emu(4400000))
        # A stream, not a temp file: python-pptx reads the picture lazily too.
        slide.shapes.add_picture(io.BytesIO(b["data"]), left, top,
                                  width=w, height=h)
    except Exception:
        return


def build_pptx(spec: dict) -> tuple[bytes, str]:
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Emu, Pt

    title = _as_text(spec.get("title"), MAX_TITLE)
    blocks = _norm_blocks(spec.get("blocks"))
    if not title and not blocks:
        raise DocGenError("nothing to render: give a title or some blocks")

    prs = Presentation()
    import doclayout as _lay
    # The canvas was hardcoded to 16:9. PowerPoint ships thirteen preset sizes
    # and a deck sized for a printed handout or a portrait phone needs a
    # different one, so the slide geometry is now a spec field.
    _sw_in, _sh_in = _lay.resolve_slide_size(spec.get("slide_size"))
    prs.slide_width = Emu(int(_sw_in * 914400))
    prs.slide_height = Emu(int(_sh_in * 914400))
    blank = prs.slide_layouts[6]

    def new_slide():
        return prs.slides.add_slide(blank)

    def textbox(slide, left, top, width, height, text, size, bold=False,
                color=None):
        tb = slide.shapes.add_textbox(left, top, width, height)
        tf = tb.text_frame
        tf.word_wrap = True
        tf.text = text
        for p in tf.paragraphs:
            for r in p.runs:
                r.font.size = Pt(size)
                r.font.bold = bold
                if color is not None:
                    r.font.color.rgb = color
        return tb

    W = prs.slide_width
    H = prs.slide_height
    sub = _as_text(spec.get("subtitle"), 300)

    # Title slide when a title is present.
    if title:
        s = new_slide()
        textbox(s, Emu(685800), Emu(2400000), W - Emu(1371600), Emu(1200000),
                title, 40, bold=True)
        if sub:
            textbox(s, Emu(685800), Emu(3600000), W - Emu(1371600),
                    Emu(700000), sub, 18, color=RGBColor(0x55, 0x55, 0x55))

    cur = new_slide()
    top = Emu(500000)
    left = Emu(685800)
    width = W - Emu(1371600)
    bottom = H - Emu(500000)

    def advance(slide_ref, used):
        nonlocal cur, top
        if top + used > bottom:
            cur = new_slide()
            top = Emu(500000)
            return cur
        return slide_ref

    for b in blocks:
        t = b["type"]
        if t == "pagebreak":
            cur = new_slide()
            top = Emu(500000)
            continue
        if t == "heading":
            advance(cur, Emu(900000))
            textbox(cur, left, top, width, Emu(800000),
                    _strip_md(b["text"]), 26, bold=True)
            top += Emu(800000)
        elif t == "paragraph":
            txt = _strip_md(b["text"])[:1200]
            advance(cur, Emu(700000))
            textbox(cur, left, top, width, Emu(600000), txt, 15)
            top += Emu(620000)
        elif t == "quote":
            advance(cur, Emu(600000))
            textbox(cur, left, top, width, Emu(500000), _strip_md(b["text"]),
                    14, color=RGBColor(0x44, 0x44, 0x44))
            top += Emu(560000)
        elif t in ("bullets", "numbers"):
            lines = []
            for i, item in enumerate(b["items"], 1):
                mark = "• " if t == "bullets" else f"{i}. "
                lines.append(mark + _strip_md(item)[:300])
            body = "\n".join(lines)
            advance(cur, Emu(1200000))
            textbox(cur, left, top, width, Emu(1000000), body, 15)
            top += Emu(1050000)
        elif t == "chart":
            _pptx_chart(cur, b, left, top, width, advance)
        elif t == "image":
            _pptx_image(cur, b, left, top, width, advance)
        elif t == "table":
            rows, cols = len(b["rows"]) + (1 if b.get("header") else 0), \
                len(b.get("header") or b["rows"][0] or [1])
            rows, cols = max(1, min(rows, 60)), max(1, min(cols, 12))
            tbl_h = min(Emu(4200000), Emu(300000) * rows)
            advance(cur, tbl_h + Emu(200000))
            shape = cur.shapes.add_table(rows, cols, left, top, width, tbl_h)
            tblobj = shape.table
            ri = 0
            if b.get("header"):
                for ci, h in enumerate(b["header"][:cols]):
                    cellobj = tblobj.cell(0, ci)
                    cellobj.text = _strip_md(h)[:120]
                    for p in cellobj.text_frame.paragraphs:
                        for r in p.runs:
                            r.font.size = Pt(11)
                            r.font.bold = True
                ri = 1
            for row in b["rows"][:rows - ri]:
                for ci, c in enumerate(row[:cols]):
                    cellobj = tblobj.cell(ri, ci)
                    cellobj.text = _strip_md(c)[:200]
                    for p in cellobj.text_frame.paragraphs:
                        for r in p.runs:
                            r.font.size = Pt(10)
                ri += 1
            top += tbl_h + Emu(300000)
        elif t == "note":
            # Speaker notes belong on the notes slide, not on the slide face.
            # They used to be dropped entirely, so a deck whose blocks were
            # all notes rendered as a run of blank slides.
            try:
                cur.notes_slide.notes_text_frame.text = _strip_md(b["text"])[:2000]
            except Exception:
                # Some layouts have no notes placeholder; fall back to small
                # type at the foot of the slide rather than losing the text.
                advance(cur, Emu(400000))
                textbox(cur, left, H - Emu(900000), width, Emu(400000),
                        _strip_md(b["text"])[:400], 10,
                        color=RGBColor(0x77, 0x77, 0x77))

    out = io.BytesIO()
    prs.save(out)
    return out.getvalue(), _safe_filename(title, "pptx")


# ── Word ─────────────────────────────────────────────────────────────────────

def _docx_caption(d, text: str) -> None:
    """A caption under a figure, in the same grey small style the PDF uses."""
    try:
        p = d.add_paragraph(_strip_md(text)[:300])
        for r in p.runs:
            r.font.size = Pt(8)
            r.font.italic = True
            r.font.color.rgb = RGBColor(0x5C, 0x5C, 0x66)
        return p
    except Exception:
        return None


def _docx_chart_table(d, b: dict) -> None:
    if b.get("title"):
        try:
            h = d.add_heading(_strip_md(b["title"])[:200], level=4)
            for r in h.runs:
                r.font.size = Pt(11)
        except Exception:
            pass
    cats = b["categories"]
    series = b["series"]
    cols = min(1 + len(series), 12)
    rows = len(cats) + 1
    try:
        tbl = d.add_table(rows=0, cols=cols)
        tbl.style = "Table Grid"
    except Exception:
        return
    hdr = tbl.add_row().cells
    hdr[0].text = ""
    for i, s in enumerate(series[:cols - 1]):
        hdr[i + 1].text = _strip_md(s.get("name") or f"series {i + 1}")[:60]
    for ri, c in enumerate(cats):
        cells = tbl.add_row().cells
        cells[0].text = _strip_md(c)[:60]
        for ci, s in enumerate(series[:cols - 1]):
            v = s["data"][ri] if ri < len(s["data"]) else ""
            try:
                cells[ci + 1].text = (f"{v:g}" if isinstance(v, float)
                                      else str(v))
            except Exception:
                cells[ci + 1].text = ""
    if b.get("caption"):
        _docx_caption(d, b["caption"])


def build_docx(spec: dict) -> tuple[bytes, str]:
    from docx import Document
    from docx.shared import Mm, Pt, RGBColor

    title = _as_text(spec.get("title"), MAX_TITLE)
    blocks = _norm_blocks(spec.get("blocks"))
    if not title and not blocks:
        raise DocGenError("nothing to render: give a title or some blocks")

    import doclayout as _lay
    # The same page geometry the PDF honours, so the two formats of the same
    # request are the same document rather than a PDF and a Letter sheet.
    _style = _lay.resolve_style(spec.get("style"))
    _page_mm = _lay.resolve_page_size(spec.get("page_size"))
    if _lay.resolve_orientation(spec.get("orientation")) == "landscape":
        _page_mm = (max(_page_mm), min(_page_mm))
    _margins = _lay.measure_to_margins(
        _page_mm[0], _style,
        _lay.resolve_margins(
            spec.get("margins") if isinstance(spec.get("margins"), str)
            else None, _page_mm,
            spec.get("margins") if isinstance(spec.get("margins"), dict)
            else None))
    _cols = max(1, min(3, int(_num(spec.get("columns"), 0)
                              or _style.get("columns") or 1)))

    d = Document()
    _sec = d.sections[0]
    _sec.page_width = Mm(_page_mm[0])
    _sec.page_height = Mm(_page_mm[1])
    _sec.top_margin, _sec.right_margin = Mm(_margins[0]), Mm(_margins[1])
    _sec.bottom_margin, _sec.left_margin = Mm(_margins[2]), Mm(_margins[3])
    if _cols > 1:
        # Word models multi-column text as a section property, not as frames.
        from docx.oxml.ns import qn
        _sectPr = _sec._sectPr
        _cols_el = _sectPr.find(qn("w:cols"))
        if _cols_el is None:
            _cols_el = _sectPr.makeelement(qn("w:cols"), {})
            _sectPr.append(_cols_el)
        _cols_el.set(qn("w:num"), str(_cols))
        _cols_el.set(qn("w:space"), "425")   # 7.5mm gutter in twips

    # Typography follows the style, so a serif academic style is not rendered
    # in Calibri with serif intent.
    _serif = _style.get("family") == "serif"
    _base_font = "Times New Roman" if _serif else "Calibri"
    _body_pt = float(_style.get("body") or 11)
    for _name in ("Normal", "Body Text"):
        try:
            _s = d.styles[_name]
        except KeyError:
            continue
        _s.font.name = _base_font
        _s.font.size = Pt(_body_pt)
        # python-docx does not set the East Asian font, so Word can fall back
        # to a different face for non-Latin runs.
        try:
            _rpr = _s.element.get_or_add_rPr()
            _rfonts = _rpr.get_or_add_rFonts()
            _rfonts.set(qn_font_attr(), _base_font)
        except Exception:
            pass

    def qn_font_attr():
        from docx.oxml.ns import qn
        return qn("w:ascii")

    if title:
        d.add_heading(title, level=0)
    sub = _as_text(spec.get("subtitle"), 300)
    if sub:
        p = d.add_paragraph(sub)
        for r in p.runs:
            r.font.size = Pt(12)
            r.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
    au = _as_text(spec.get("author"), 120)
    if au:
        d.add_paragraph(au)

    for b in blocks:
        t = b["type"]
        if t == "pagebreak":
            d.add_page_break()
        elif t == "heading":
            d.add_heading(_strip_md(b["text"]), level=min(3, b["level"]))
        elif t == "paragraph":
            d.add_paragraph(_strip_md(b["text"]))
        elif t == "quote":
            p = d.add_paragraph(_strip_md(b["text"]), style="Intense Quote")
            for r in p.runs:
                r.font.color.rgb = RGBColor(0x44, 0x44, 0x44)
        elif t in ("bullets", "numbers"):
            style = "List Bullet" if t == "bullets" else "List Number"
            for item in b["items"]:
                d.add_paragraph(_strip_md(item), style=style)
        elif t == "chart":
            # python-docx has no chart API. The honest degradation is the
            # underlying numbers in a table with the chart's title: a reader
            # gets the data, and nothing pretends to be a picture.
            _docx_chart_table(d, b)
        elif t == "image":
            # No bare `except: pass` here: it hid a missing `Mm` import and
            # the figure simply never appeared, in every format, silently.
            try:
                from PIL import Image as PILImage
                with PILImage.open(io.BytesIO(b["data"])) as im:
                    iw, ih = im.size
                if iw and ih:
                    d.add_picture(io.BytesIO(b["data"]),
                                  width=Mm(min(165.0, b.get("width_mm") or 150.0)))
                    if b.get("caption"):
                        _docx_caption(d, b["caption"])
                else:
                    _note_drop("image", "zero-sized figure")
            except Exception as _e:
                _note_drop("image", f"{type(_e).__name__}: {_e}"[:80])
        elif t == "table":
            rows = b["rows"]
            cols = max((len(r) for r in rows), default=1)
            cols = min(max(1, cols), 30)
            tbl = d.add_table(rows=0, cols=cols)
            try:
                tbl.style = "Table Grid"
            except KeyError:
                pass
            if b.get("header"):
                cells = tbl.add_row().cells
                for ci, h in enumerate(b["header"][:cols]):
                    cells[ci].text = _strip_md(h)[:200]
                    for p in cells[ci].paragraphs:
                        for r in p.runs:
                            r.font.bold = True
            for row in rows:
                cells = tbl.add_row().cells
                for ci, c in enumerate(row[:cols]):
                    cells[ci].text = _strip_md(c)[:500]
            d.add_paragraph()

    # Same reference list as the PDF, so the two formats of one request
    # carry the same sources.
    _refs = _norm_references(spec.get("references"))
    if _refs:
        _cite = _lay.resolve_citation(spec.get("citation_style"))
        _lines = _lay.build_reference_list(_refs, _cite)
        if _lines:
            d.add_page_break()
            d.add_heading(_cite["heading"], level=1)
            for _line in _lines:
                p = d.add_paragraph(_line)
                p.paragraph_format.space_after = Pt(4)
                if _cite["hanging_indent"]:
                    pf = p.paragraph_format
                    pf.left_indent = Mm(8)
                    pf.first_line_indent = Mm(-8)

    out = io.BytesIO()
    d.save(out)
    return out.getvalue(), _safe_filename(title, "docx")


# ── Excel ────────────────────────────────────────────────────────────────────

def build_xlsx(spec: dict) -> tuple[bytes, str]:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    title = _as_text(spec.get("title"), MAX_TITLE)
    blocks = _norm_blocks(spec.get("blocks"))
    sheets_in = spec.get("sheets")
    if not sheets_in and not blocks:
        raise DocGenError("nothing to render: give sheets or some blocks")

    wb = Workbook()
    wb.remove(wb.active)

    def fill(ws, rows, header=None):
        r0 = 1
        if header:
            for ci, h in enumerate(header[:MAX_TABLE_COLS], 1):
                c = ws.cell(row=1, column=ci, value=_strip_md(h)[:200])
                c.font = Font(bold=True)
                c.alignment = Alignment(wrap_text=True, vertical="top")
            r0 = 2
        n = 0
        for row in rows[:MAX_ROWS_SHEET]:
            vals = row if isinstance(row, list) else [row]
            for ci, v in enumerate(list(vals)[:MAX_TABLE_COLS], 1):
                c = ws.cell(row=r0 + n, column=ci, value=_strip_md(str(v))[:2000])
                c.alignment = Alignment(wrap_text=True, vertical="top")
            n += 1
        for ci in range(1, min(MAX_TABLE_COLS, 12) + 1):
            ws.column_dimensions[get_column_letter(ci)].width = 28

    # Explicit sheets win — but ONLY if they actually produced a sheet. The
    # old guard was `isinstance(sheets_in, list) and wb.sheetnames`, and
    # because `wb.remove(wb.active)` had already emptied the workbook, a
    # request carrying an EMPTY `sheets: []` fell into the `pass` branch and
    # the whole table-block loop was skipped: the model sent `sheets: []`
    # plus real `table` blocks, and the workbook shipped with nothing but a
    # blank "Sheet1" (verified - 0 cells, 2 sheets, both empty).
    if isinstance(sheets_in, list):
        for s in sheets_in[:20]:
            if not isinstance(s, dict):
                continue
            nm = _as_text(s.get("name"), 60) or "Sheet"
            ws = wb.create_sheet(nm[:31])
            rows = s.get("rows")
            if isinstance(rows, list):
                fill(ws, [r for r in rows if isinstance(r, (list, dict, str, int, float))],
                     s.get("header") if isinstance(s.get("header"), list) else None)

    # Each table block becomes its own sheet, but ONLY when no explicit sheet
    # already supplied one. The bug this replaces had two mistakes: the guard
    # was `isinstance(sheets_in, list) and wb.sheetnames`, so an EMPTY
    # `sheets: []` took a `pass` branch and dropped every table; and the
    # fallback `create_sheet("Sheet1")` ran BEFORE this loop, so
    # `wb.sheetnames` was already non-empty here and the `elif` never fired.
    # A model sending `sheets: []` plus real tables got a workbook whose only
    # sheet was blank (verified: 0 cells).
    if not wb.sheetnames:
        idx = 0
        for b in blocks:
            if b["type"] != "table":
                continue
            nm = (b.get("header") or [None])[0] if b.get("header") else None
            nm = _as_text(nm, 60) or f"Table {idx + 1}"
            ws = wb.create_sheet(nm[:31])
            fill(ws, b["rows"], b.get("header"))
            idx += 1
    if not wb.sheetnames:
        # Nothing tabular at all: keep a single sheet so the file is valid.
        ws = wb.create_sheet("Sheet1")
        if title:
            ws.cell(row=1, column=1, value=title).font = Font(bold=True)
        for i, para in enumerate(p for p in blocks
                                 if p["type"] in ("heading", "paragraph", "quote")):
            if i > 200:
                break
            txt = _strip_md(para.get("text", ""))[:2000]
            if txt:
                ws.cell(row=2 + i, column=1, value=txt)

    # Point Excel at real content. openpyxl opens on the first sheet; when
    # that was the blank fallback the data was one tab to the right.
    populated = [s for s in wb.worksheets if s.max_row and s.max_column]
    if populated and populated[0].title != wb.worksheets[0].title:
        wb.active = wb.worksheets.index(populated[0])

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue(), _safe_filename(title or "export", "xlsx")


# ── dispatcher ───────────────────────────────────────────────────────────────

BUILDERS = {
    "pdf": (build_pdf, "application/pdf"),
    "pptx": (build_pptx,
             "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    "docx": (build_docx,
             "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "xlsx": (build_xlsx,
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
}
MAX_BYTES = 25 * 1024 * 1024


def _has_content(spec: dict, fmt: str) -> bool:
    """Was anything at all supplied?

    Deliberately a presence test, not a richness test. An earlier version of
    this gate tried to decide whether each block carried text by listing the
    content keys it knew about, and immediately rejected real documents whose
    blocks use `bullets`, `numbers`, `quote` or a table shape it had not
    guessed. This module already owns block normalisation (`_norm_blocks`) and
    this must not become a second, drifting opinion about what a block is.
    Thin content is reported as a warning; only a spec with nothing in it is
    refused, because that is the case that renders a one-page cover and calls
    it a document. A title alone IS a valid one-page document, which is why
    this checks the title rather than the blocks.
    """
    if str(spec.get("title") or "").strip():
        return True
    if any(isinstance(s, dict) and (s.get("rows") or s.get("header"))
           for s in (spec.get("sheets") or [])):
        return True
    return any(isinstance(b, dict) for b in (spec.get("blocks") or []))


def generate(fmt: str, spec: dict) -> tuple[bytes, str, str]:
    """Return (bytes, filename, content_type) for a format."""
    key = str(fmt or "").strip().lower()
    if key not in BUILDERS:
        raise DocGenError(f"unsupported format {fmt!r}; "
                          f"want one of {', '.join(sorted(BUILDERS))}")
    if not isinstance(spec, dict):
        raise DocGenError("spec must be a JSON object")
    _DROPPED.clear()
    fn, ctype = BUILDERS[key]
    # A spec with no content is the failure mode that looks most like success:
    # the model called render_document with an empty or malformed block list,
    # a builder dutifully produced a cover page, and the user received a
    # one-page file that is technically a document and is not what they asked
    # for. Refuse it, and name what was wrong so the model can fix and retry.
    if not _has_content(spec, key):
        raise DocGenError(
            "the spec was empty: no `title`, no `blocks` and no `sheets`. "
            "Send the document's actual content - a title, and blocks "
            "(heading, paragraph, bullets, table, chart) - and call again.")
    data, name = fn(spec)
    if not data:
        raise DocGenError("generated an empty file")
    if len(data) > MAX_BYTES:
        raise DocGenError(f"generated file is {len(data) // 1024}KB; "
                          f"too large � reduce the content")
    return data, name, ctype
