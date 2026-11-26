"""Real document previews: the document itself, not a description of it.

The console used to show a text outline - headings, a word count, a warning.
That is useful metadata and useless as a check on a document, because the
thing being judged is the page: the measure, the margins, whether the chart
fits the column, whether the cover looks like a cover.

What each format gets, and why:

  pdf   PyMuPDF rasterises the exact bytes that were delivered. This is the
        document, pixel for pixel.
  pptx  SVG built from the same block list and the same slide geometry the
        deck builder uses, so the preview and the .pptx agree by
        construction rather than by a second guess at the layout.
  docx  The saved .docx is re-opened and its real paragraphs, styles and
        tables are emitted as HTML.
  xlsx  The saved .xlsx is re-opened and its real cells, merged ranges and
        number formats are emitted as an HTML grid.

LibreOffice would give one raster path for all four, but it is not installed,
and a preview that silently disagreed with the file would be worse than an
honest per-format one. Each of these reads the artefact it previews.
"""
from __future__ import annotations

import base64
import html
import io
from typing import Any

MAX_PREVIEW_PAGES = 12
PREVIEW_DPI_SCALE = 1.4          # ~100 dpi, enough to judge layout
MAX_SVG_CHARS = 900_000          # a huge inline SVG stalls the browser


def _png(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _esc(s: Any) -> str:
    return html.escape(str(s or ""), quote=True)


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ── pdf ─────────────────────────────────────────────────────────────────────
def _preview_pdf(blob: bytes, max_pages: int) -> dict:
    import pymupdf
    doc = pymupdf.open(stream=blob, filetype="pdf")
    pages = []
    for i in range(min(doc.page_count, max_pages)):
        page = doc[i]
        pix = page.get_pixmap(dpi=int(72 * PREVIEW_DPI_SCALE))
        pages.append({
            "index": i,
            "kind": "png",
            "data": _png(pix.tobytes("png")),
            "width_pt": round(page.rect.width, 1),
            "height_pt": round(page.rect.height, 1),
        })
    total = doc.page_count
    doc.close()
    return {"mode": "pages", "pages": pages, "page_count": total,
            "truncated": total > len(pages)}


# ── pptx ────────────────────────────────────────────────────────────────────
def _spec_from_pptx(blob: bytes) -> dict:
    """Recover a renderable spec from a saved .pptx.

    The first slide becomes the cover and each later slide becomes one page of
    blocks, which is the shape `build_pptx` writes. This is not a second guess
    at the styling - the slide geometry and the layout function are still the
    real ones - but it is honest about one limit: bullet nesting and tables
    come through as text, because the preview needs the words and their order,
    not a PowerPoint clone.
    """
    from pptx import Presentation
    from pptx.util import Emu

    prs = Presentation(io.BytesIO(blob))
    slide_size = None
    try:
        w_in = Emu(prs.slide_width).inches
        h_in = Emu(prs.slide_height).inches
        for name, dims in doclayout.KNOWN_SLIDE_SIZES.items():
            if abs(dims[0] / dims[1] - w_in / h_in) < 0.02:
                slide_size = name
                break
    except Exception:
        pass

    spec: dict = {"slide_size": slide_size or "16:9", "blocks": []}
    for idx, slide in enumerate(prs.slides, start=1):
        lines: list[str] = []
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            for para in shape.text_frame.paragraphs:
                text = "".join(r.text or "" for r in para.runs).strip()
                if text:
                    lines.append(text)
        if not lines:
            continue
        if idx == 1:
            spec["title"] = lines[0][:200]
            if len(lines) > 1:
                spec["subtitle"] = " ".join(lines[1:])[:300]
            continue
        spec["blocks"].append({"type": "pagebreak"})
        for n, text in enumerate(lines):
            spec["blocks"].append(
                {"type": "heading" if n == 0 else "bullet",
                 "level": 1 if n == 0 else 0, "text": text[:400]})
    return spec


def _preview_pptx(spec: dict, max_pages: int) -> dict:
    """Slide previews as SVG, laid out exactly as build_pptx lays them out.

    Reusing one layout function for both the .pptx and the preview is the
    whole point: a preview that is a second, looser re-implementation is how
    a preview ends up disagreeing with the file.
    """
    import doclayout as lay

    from docgen import _as_text, _norm_blocks, _strip_md

    w_in, h_in = lay.resolve_slide_size(spec.get("slide_size"))
    W, H = w_in * 72, h_in * 72
    blocks = _norm_blocks(spec.get("blocks"))
    style = lay.resolve_style(spec.get("style"))
    body = _f(style.get("body"), 11)
    lead = body * 1.32
    accent = style.get("accent") or "#5b4bd6"
    margin = 0.6 * 72

    def slide_list() -> list[list[dict]]:
        out: list[list[dict]] = []
        if _as_text(spec.get("title"), 200):
            out.append([{"type": "coverish",
                         "title": _as_text(spec.get("title"), 200),
                         "subtitle": _as_text(spec.get("subtitle"), 300)}])
        cur: list[dict] = []
        for b in blocks:
            # A LEVEL-1 HEADING IS A SLIDE, exactly as in build_pptx.
            #
            # This splitter only broke on `pagebreak`, and a deck's blocks
            # almost never carry one - the author writes headings. So a live
            # layout preview put the whole deck on page 1 and reported 2 pages
            # for a file that downloads with 4, which is precisely the
            # "the PPTX does not render properly" complaint: the preview was
            # showing a draft the user never receives.
            if b["type"] == "heading" and int(b.get("level") or 1) == 1 \
                    and cur:
                out.append(cur)
                cur = []
                cur.append(b)
                continue
            if b["type"] == "pagebreak":
                if cur:
                    out.append(cur)
                    cur = []
                continue
            cur.append(b)
        if cur:
            out.append(cur)
        return out[:max_pages]

    def wrap(text: str, chars: int) -> list[str]:
        words, line, lines = str(text or "").split(), "", []
        for wd in words:
            if len(line) + len(wd) + 1 > chars and line:
                lines.append(line)
                line = wd
            else:
                line = (line + " " + wd).strip()
        if line:
            lines.append(line)
        return lines or [""]

    def svg_for(items: list[dict], is_cover: bool) -> str:
        p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} {H:.0f}" '
             f'width="100%" role="img">',
             f'<rect width="{W:.0f}" height="{H:.0f}" fill="#ffffff"/>']
        if is_cover:
            p.append(f'<rect x="0" y="0" width="{W:.0f}" height="6" '
                     f'fill="{accent}"/>')
            title = _strip_md(items[0].get("title", ""))
            sub = _strip_md(items[0].get("subtitle", ""))
            y = H * 0.42
            for ln in wrap(title, 30)[:3]:
                p.append(f'<text x="{margin}" y="{y:.0f}" font-family="Helvetica, '
                         f'Arial, sans-serif" font-size="40" font-weight="700" '
                         f'fill="#111114">{_esc(ln)}</text>')
                y += 46
            for ln in wrap(sub, 60)[:2]:
                p.append(f'<text x="{margin}" y="{y + 14:.0f}" '
                         f'font-family="Helvetica, Arial, sans-serif" '
                         f'font-size="18" fill="#5c5c66">{_esc(ln)}</text>')
                y += 24
            p.append("</svg>")
            return "".join(p)

        y = margin
        col_chars = max(20, int((W - 2 * margin) / (body * 0.5)))
        for b in items:
            t = b["type"]
            if t == "heading":
                size = body * 1.5 if b.get("level", 1) == 1 else body * 1.2
                y += lead
                for ln in wrap(_strip_md(b["text"]), col_chars)[:3]:
                    p.append(f'<text x="{margin}" y="{y:.0f}" '
                             f'font-family="Helvetica, Arial, sans-serif" '
                             f'font-size="{size:.0f}" font-weight="700" '
                             f'fill="#111114">{_esc(ln)}</text>')
                    y += size * 1.25
                y += lead * 0.4
            elif t in ("bullets", "numbers"):
                for i, item in enumerate(b["items"], 1):
                    mark = "• " if t == "bullets" else f"{i}. "
                    for j, ln in enumerate(wrap(_strip_md(item), col_chars - 3)):
                        p.append(
                            f'<text x="{margin + 12}" y="{y:.0f}" '
                            f'font-family="Helvetica, Arial, sans-serif" '
                            f'font-size="{body:.0f}" fill="#1d1d22">'
                            f'{_esc((mark if j == 0 else "  ") + ln)}</text>')
                        y += lead
            elif t == "paragraph":
                for ln in wrap(_strip_md(b["text"])[:900], col_chars):
                    if y > H - margin:
                        break
                    p.append(f'<text x="{margin}" y="{y:.0f}" '
                             f'font-family="Helvetica, Arial, sans-serif" '
                             f'font-size="{body:.0f}" fill="#1d1d22">'
                             f'{_esc(ln)}</text>')
                    y += lead
                y += lead * 0.4
            elif t == "quote":
                p.append(f'<rect x="{margin}" y="{y:.0f}" width="4" '
                         f'height="{lead * 2.4:.0f}" fill="{accent}"/>')
                y += lead
                for ln in wrap(_strip_md(b["text"]), col_chars - 4)[:3]:
                    p.append(f'<text x="{margin + 14}" y="{y:.0f}" '
                             f'font-family="Helvetica, Arial, sans-serif" '
                             f'font-size="{body - 0.5:.0f}" fill="#33333c" '
                             f'font-style="italic">{_esc(ln)}</text>')
                    y += lead
                y += lead * 0.6
            elif t == "table":
                rows = list(b.get("rows") or [])
                head = b.get("header") or []
                ncols = max(1, len(head) or max((len(r) for r in rows),
                                                default=1))
                cw = (W - 2 * margin) / ncols
                if head:
                    p.append(f'<rect x="{margin}" y="{y:.0f}" '
                             f'width="{W - 2 * margin:.0f}" height="{lead:.0f}" '
                             f'fill="{accent}"/>')
                    for ci, h in enumerate(head[:ncols]):
                        p.append(f'<text x="{margin + 6 + ci * cw:.0f}" '
                                 f'y="{y + lead * 0.72:.0f}" '
                                 f'font-family="Helvetica, Arial, sans-serif" '
                                 f'font-size="11" font-weight="700" '
                                 f'fill="#ffffff">{_esc(_strip_md(h))}</text>')
                    y += lead
                for ri, row in enumerate(rows[:10]):
                    if ri % 2 == 1:
                        p.append(f'<rect x="{margin}" y="{y:.0f}" '
                                 f'width="{W - 2 * margin:.0f}" '
                                 f'height="{lead:.0f}" fill="#f8f8fb"/>')
                    for ci, c in enumerate(row[:ncols]):
                        p.append(f'<text x="{margin + 6 + ci * cw:.0f}" '
                                 f'y="{y + lead * 0.72:.0f}" '
                                 f'font-family="Helvetica, Arial, sans-serif" '
                                 f'font-size="11" fill="#111114">'
                                 f'{_esc(_strip_md(c))}</text>')
                    y += lead
                y += lead
            elif t == "stats":
                # The cards are the slide's whole point on a "the numbers"
                # slide. The spec preview skipped this type entirely, so the
                # one slide that exists to show three big figures previewed as
                # a blank page under its title.
                items_s = [it for it in (b.get("items") or [])
                           if isinstance(it, dict)][:4]
                if items_s:
                    gap = 16.0
                    cw = (W - 2 * margin - gap * (len(items_s) - 1)) / len(items_s)
                    ch = min(H * 0.26, 118.0)
                    for i, it in enumerate(items_s):
                        cx = margin + i * (cw + gap)
                        p.append(f'<rect x="{cx:.1f}" y="{y:.1f}" '
                                 f'width="{cw:.1f}" height="{ch:.1f}" rx="10" '
                                 f'fill="#f4f2fc" stroke="#ddd9f6"/>')
                        p.append(f'<text x="{cx + 16:.1f}" y="{y + 46:.1f}" '
                                 f'font-family="Helvetica, Arial, sans-serif" '
                                 f'font-size="30" font-weight="700" '
                                 f'fill="{accent}">{_esc(_strip_md(str(it.get("value") or "")))}</text>')
                        p.append(f'<text x="{cx + 16:.1f}" y="{y + 78:.1f}" '
                                 f'font-family="Helvetica, Arial, sans-serif" '
                                 f'font-size="13" fill="#5c5c66">'
                                 f'{_esc(_strip_md(str(it.get("label") or "")))}</text>')
                    y += ch + lead
            elif t == "columns":
                items_c = [it for it in (b.get("items") or [])
                           if isinstance(it, dict)][:3]
                if items_c:
                    gap = 16.0
                    cw = (W - 2 * margin - gap * (len(items_c) - 1)) / len(items_c)
                    ch = min(H * 0.32, 168.0)
                    for i, it in enumerate(items_c):
                        cx = margin + i * (cw + gap)
                        p.append(f'<rect x="{cx:.1f}" y="{y:.1f}" '
                                 f'width="{cw:.1f}" height="{ch:.1f}" rx="10" '
                                 f'fill="#ffffff" stroke="#ddd9f6"/>')
                        p.append(f'<rect x="{cx:.1f}" y="{y:.1f}" width="4" '
                                 f'height="{ch:.1f}" fill="{accent}"/>')
                        p.append(f'<text x="{cx + 16:.1f}" y="{y + 30:.1f}" '
                                 f'font-family="Helvetica, Arial, sans-serif" '
                                 f'font-size="15" font-weight="700" '
                                 f'fill="{accent}">{_esc(_strip_md(str(it.get("title") or "")))}</text>')
                        ty = y + 54
                        for ln in wrap(_strip_md(str(it.get("body") or "")),
                                       max(14, int(cw / 7.0)))[:5]:
                            p.append(f'<text x="{cx + 16:.1f}" y="{ty:.1f}" '
                                     f'font-family="Helvetica, Arial, sans-serif" '
                                     f'font-size="12" fill="#1d1d22">'
                                     f'{_esc(ln)}</text>')
                            ty += 16
                    y += ch + lead
            elif t == "callout":
                txt = _strip_md(str(b.get("text") or ""))
                title = _strip_md(str(b.get("title") or "Note"))
                lines = wrap(txt, col_chars - 2)[:4]
                box_h = 40 + len(lines) * lead
                p.append(f'<rect x="{margin}" y="{y:.1f}" '
                         f'width="{W - 2 * margin:.1f}" height="{box_h:.1f}" '
                         f'rx="8" fill="#f4f2fc" stroke="#ddd9f6"/>')
                p.append(f'<text x="{margin + 18:.1f}" y="{y + 24:.1f}" '
                         f'font-family="Helvetica, Arial, sans-serif" '
                         f'font-size="10" font-weight="700" fill="{accent}">'
                         f'{_esc(title.upper())}</text>')
                ty = y + 44
                for ln in lines:
                    p.append(f'<text x="{margin + 18:.1f}" y="{ty:.1f}" '
                             f'font-family="Helvetica, Arial, sans-serif" '
                             f'font-size="{body:.0f}" fill="#1d1d22">'
                             f'{_esc(ln)}</text>')
                    ty += lead
                y += box_h + lead
            elif t == "chart":
                cats = b.get("categories") or []
                series = b.get("series") or []
                if cats and series:
                    vals = [_f(x) for x in (series[0].get("data") or [])]
                    top = max(vals) if vals else 1
                    ctitle = _strip_md(str(b.get("title") or ""))
                    if ctitle:
                        # The chart's own title was dropped here, so a slide
                        # whose meaning lives in "Queries per second" previewed
                        # as unlabelled bars.
                        p.append(f'<text x="{margin}" y="{y + 4:.0f}" '
                                 f'font-family="Helvetica, Arial, sans-serif" '
                                 f'font-size="14" font-weight="700" '
                                 f'fill="#111114">{_esc(ctitle)}</text>')
                        y += lead * 1.4
                    plot_h, plot_w = H * 0.38, W - 2 * margin - 60
                    bw = plot_w / max(1, len(vals))
                    base = y + plot_h
                    for i, v in enumerate(vals[:40]):
                        bh = plot_h * (v / top if top else 0)
                        p.append(
                            f'<rect x="{margin + 40 + i * bw + bw * 0.12:.0f}" '
                            f'y="{base - bh:.0f}" width="{bw * 0.76:.0f}" '
                            f'height="{bh:.0f}" fill="{accent}"/>')
                    p.append(f'<line x1="{margin + 34}" y1="{base:.0f}" '
                             f'x2="{margin + 40 + plot_w:.0f}" y2="{base:.0f}" '
                             f'stroke="#9a9aa6" stroke-width="1"/>')
                    y = base + lead * 2
        p.append(f'<line x1="{margin}" y1="{H - 40:.0f}" '
                 f'x2="{W - margin:.0f}" y2="{H - 40:.0f}" stroke="#e2e2ea" '
                 f'stroke-width="0.75"/>')
        p.append("</svg>")
        return "".join(p)

    pages = []
    slides = slide_list()
    for i, items in enumerate(slides):
        svg = svg_for(items, bool(items and items[0]["type"] == "coverish"))
        if len(svg) > MAX_SVG_CHARS:
            svg = svg[:MAX_SVG_CHARS] + "</svg>"
        pages.append({"index": i, "kind": "svg", "data": svg,
                      "width_pt": round(W, 1), "height_pt": round(H, 1)})
    return {"mode": "pages", "pages": pages, "page_count": len(slides),
            "truncated": False, "note": "layout preview from the deck geometry"}


# ── pptx: draw the FILE, not a re-guess ─────────────────────────────────────
def _svg_from_pptx(blob: bytes, max_pages: int) -> dict | None:
    """Rasterise the delivered .pptx's OWN shapes into SVG, one per slide.

    The old preview walked the block spec and re-drew it with a second layout
    function. That second renderer knew nothing about the deck's real chrome -
    the split cover panel, the accent edge band, the tinted stat cards, the
    section dividers - so a delivered deck with 19 shapes on a slide previewed
    as a white page with a 6pt rule at the top. A preview that disagrees with
    the file is worse than no preview: it is the thing the user checks the file
    against.

    Reading the shapes means the preview cannot drift: every rectangle, text
    frame, table and picture is drawn at the position the .pptx records. What
    it cannot do is evaluate theme inheritance or render a chart's plot, so
    those fall back to the shape's own fill/text.
    """
    import base64

    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    from pptx.util import Emu

    try:
        prs = Presentation(io.BytesIO(blob))
    except Exception:
        return None

    W = prs.slide_width or Emu(12192000)
    H = prs.slide_height or Emu(6858000)
    # TWO scales, and conflating them blanked every text run:
    #   SP   EMU -> SVG points (960pt canvas whatever the real slide size)
    #   ST   typographic scaling for a FONT SIZE, which is already in points,
    #        so it only needs the canvas ratio (960pt / slide_width_in_72pt).
    # A 16:9 slide is 960pt wide, so ST is 1.0; a 4:3 slide is 720pt, so ST is
    # 1.33. Using the EMU ratio for fonts multiplied 40pt by 7.8e-5 and every
    # size rendered as 0.0.
    SP = 960.0 / float(W)
    ST = 960.0 / (float(W) / 914400.0 * 72.0)

    def px(v) -> float:
        return float(v or 0) * SP

    def rgb_of(fill) -> str | None:
        """A solid fill's colour, or a gradient's FIRST stop.

        The cover's accent panel is a gradient, and a gradient has no
        `.fore_color.rgb` - so the panel was skipped entirely and the preview
        lost the deck's most distinctive element. One stop is enough for a
        preview whose job is composition.
        """
        try:
            if fill.type is None:
                return None
            try:
                c = fill.fore_color
                if c.type is not None and c.rgb is not None:
                    return "#%02x%02x%02x" % tuple(c.rgb)
            except Exception:
                pass
            # MSO_FILL.GRADIENT == 3
            stops = fill.gradient_stops
            if stops:
                c = stops[0].color
                if c.rgb is not None:
                    return "#%02x%02x%02x" % tuple(c.rgb)
        except Exception:
            return None
        return None

    def text_runs(tf) -> list[tuple[str, float, bool, str, str]]:
        out = []
        for para in tf.paragraphs:
            txt = "".join(r.text for r in para.runs) or para.text or ""
            if not txt.strip():
                continue
            r0 = para.runs[0] if para.runs else None
            size = 12.0
            bold = False
            colour = "#111114"
            font = "Helvetica, Arial, sans-serif"
            if r0 is not None:
                try:
                    if r0.font.size is not None:
                        size = float(r0.font.size.pt)
                    elif para.font.size is not None:
                        size = float(para.font.size.pt)
                except Exception:
                    pass
                try:
                    bold = bool(r0.font.bold)
                except Exception:
                    pass
                try:
                    if r0.font.color and r0.font.color.rgb is not None:
                        colour = "#%02x%02x%02x" % tuple(r0.font.color.rgb)
                except Exception:
                    pass
                try:
                    if r0.font.name:
                        font = f"{r0.font.name}, Helvetica, Arial, sans-serif"
                except Exception:
                    pass
            out.append((txt, size, bold, colour, font))
        return out

    _FONT_CACHE: dict = {}

    def _pillow_font(name: str, bold: bool, px_size: int):
        key = (name, bold, px_size)
        if key in _FONT_CACHE:
            return _FONT_CACHE[key]
        f = None
        try:
            from PIL import ImageFont
            candidates = []
            low = (name or "").lower()
            if "georgia" in low:
                candidates = ["georgiab.ttf", "georgia.ttf"] if bold                     else ["georgia.ttf", "georgiab.ttf"]
            elif "segoe" in low:
                candidates = ["segoeuib.ttf", "segoeui.ttf"] if bold                     else ["segoeui.ttf", "segoeuib.ttf"]
            else:
                candidates = ["arialbd.ttf", "arial.ttf"] if bold                     else ["arial.ttf", "arialbd.ttf"]
            for c in candidates:
                try:
                    f = ImageFont.truetype(c, max(6, int(px_size)))
                    break
                except Exception:
                    continue
        except Exception:
            f = None
        _FONT_CACHE[key] = f
        return f

    def _text_w(text: str, size: float, bold: bool, font: str) -> float:
        """Width of `text` in points, from the real font when it is installed.

        The 0.52em average is a decent guess for prose and a bad one for
        digits: "$210-430/kWh" and "250-300 Wh/kg" measure wider than they
        draw, so the preview wrapped them to two lines where the deck has one
        and then squeezed them through the label below. Measuring the actual
        face is the only way the preview agrees with the file.
        """
        f = _pillow_font(font, bold, int(size * 4))
        if f is not None:
            try:
                return f.getlength(text) / 4.0
            except Exception:
                pass
        return len(text) * size * 0.52

    def wrap_pt(text: str, width: float, size: float,
                bold: bool = False, font: str = "") -> list[str]:
        """Greedy wrap at the measured width, breaking a too-long token."""
        limit = max(4.0, width)
        words, line, lines = text.split(), "", []
        for wd in words:
            trial = (line + " " + wd).strip()
            if line and _text_w(trial, size, bold, font) > limit:
                lines.append(line)
                # A single word wider than the column breaks mid-word, which
                # is what a renderer does with a long URL or a compound figure.
                while _text_w(wd, size, bold, font) > limit and len(wd) > 1:
                    cut = len(wd)
                    while cut > 1 and _text_w(wd[:cut], size, bold,
                                              font) > limit:
                        cut -= 1
                    lines.append(wd[:cut])
                    wd = wd[cut:]
                line = wd
            else:
                line = trial
        if line:
            lines.append(line)
        return lines or [""]

    def esc(s: Any) -> str:
        return html.escape(str(s or ""), quote=True)

    def shape_svg(sh) -> list[str]:
        p: list[str] = []
        try:
            x, y, w, h = px(sh.left), px(sh.top), px(sh.width), px(sh.height)
        except Exception:
            return p
        if w <= 0 or h <= 0:
            return p
        st = None
        try:
            st = sh.shape_type
        except Exception:
            st = None
        # Pictures: emit the real bytes so a figure shows in the preview too.
        if st == MSO_SHAPE_TYPE.PICTURE or sh.__class__.__name__ == "Picture":
            try:
                img = sh.image
                b64 = base64.b64encode(img.blob).decode("ascii")
                p.append(f'<image x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" '
                         f'height="{h:.1f}" preserveAspectRatio="none" '
                         f'href="data:image/{img.ext};base64,{b64}"/>')
                return p
            except Exception:
                return p
        # Tables: draw each cell so the preview matches the grid.
        if getattr(sh, "has_table", False):
            try:
                tbl = sh.table
                n_r = len(tbl.rows)
                n_c = len(tbl.columns)
                if n_r and n_c:
                    rh = h / n_r
                    cws = [px(c.width) for c in tbl.columns]
                    tot = sum(cws) or w
                    cws = [c * (w / tot) for c in cws]
                    for ri, row in enumerate(tbl.rows):
                        cxp = x
                        for ci, cell in enumerate(row.cells):
                            cw = cws[ci] if ci < len(cws) else w / n_c
                            try:
                                col = rgb_of(cell.fill) or "#ffffff"
                            except Exception:
                                col = "#ffffff"
                            p.append(f'<rect x="{cxp:.1f}" y="{y + ri * rh:.1f}" '
                                     f'width="{cw:.1f}" height="{rh:.1f}" '
                                     f'fill="{col}" stroke="#e2e2ea" '
                                     f'stroke-width="0.5"/>')
                            for txt, size, bold, colour, font in \
                                    text_runs(cell.text_frame)[:1]:
                                wt = ' font-weight="700"' if bold else ''
                                p.append(
                                    f'<text x="{cxp + 4:.1f}" '
                                    f'y="{y + ri * rh + rh * 0.66:.1f}" '
                                    f'font-family="{esc(font)}" '
                                    f'font-size="{size:.1f}"'
                                    + wt +
                                    f' fill="{colour}">{esc(txt)}</text>')
                            cxp += cw
                    return p
            except Exception:
                return p
        # Charts: the shape records the plot type, not the points. Draw the
        # frame and title so the slide's composition is honest, and let the
        # caption carry the meaning.
        if getattr(sh, "has_chart", False):
            p.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" '
                     f'height="{h:.1f}" fill="#ffffff" stroke="#e2e2ea" '
                     f'stroke-width="0.75"/>')
            try:
                if sh.chart.has_title:
                    t = sh.chart.chart_title.text_frame.text
                    p.append(f'<text x="{x + w / 2:.1f}" y="{y + 16:.1f}" '
                             f'text-anchor="middle" font-size="12" '
                             f'font-weight="700" fill="#111114">'
                             f'{esc(t)}</text>')
            except Exception:
                pass
            p.append(f'<text x="{x + w / 2:.1f}" y="{y + h / 2:.1f}" '
                     f'text-anchor="middle" font-size="10" fill="#9a9aa6">'
                     f'(chart &mdash; open the file for the plotted data)</text>')
            return p
        # Autoshapes and text boxes: the fill first, then the text.
        fill = None
        if getattr(sh, "has_text_frame", False) is not None:
            try:
                fill = rgb_of(sh.fill)
            except Exception:
                fill = None
        if fill and fill.lower() not in ("#ffffff",):
            radius = ""
            try:
                if "ROUNDED" in str(getattr(sh, "auto_shape_type", "")):
                    radius = f' rx="{min(w, h) * 0.06:.1f}"'
            except Exception:
                radius = ""
            p.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" '
                     f'height="{h:.1f}" fill="{fill}"{radius}/>')
        if getattr(sh, "has_text_frame", False):
            runs = text_runs(sh.text_frame)
            # Lay the paragraph out FIRST so the block can be anchored inside
            # the box the way PowerPoint anchors it. Rendering line-by-line
            # from the box top ran a two-line cover title down through the
            # accent rule that sits below the box.
            laid: list[tuple[str, float, bool, str, str, float]] = []
            for txt, size, bold, colour, font in runs:
                # A PLACEHOLDER's size lives in the layout, not the run, so it
                # reads as 0pt and the title vanished from the preview. Derive
                # one from the box the layout allocated; `px()` already scaled
                # the geometry, so no second multiply here.
                # `font.size.pt` is ALREADY points; the only scaling type
                # needs is the canvas ratio (1.0 on a 960pt-wide slide). When a
                # run carries no size at all, derive one from the box.
                size_pt = (size if size > 0.5
                           else max(9.0, min(40.0, px(sh.height) * 0.72)))
                # The font's own line height, not a flat 1.25: Georgia is
                # 1.14em and Segoe UI 1.34em, and over-tightening the cover
                # title was the other half of the collision.
                lem = 1.14 if "Georgia" in font else (
                    1.34 if "Segoe" in font else 1.22)
                # The deck sized this box from its OWN line count, and the
                # box height is evidence of how many lines the file has. Re-
                # wrapping with a different character metric produced MORE
                # lines than the box holds (a one-line "250-300 Wh/kg" became
                # two and printed through the label below it), so cap the wrap
                # at what the height allows and shrink the type until it fits.
                max_lines = max(1, int((h - 2.0) / max(1.0, size_pt * lem)
                                       + 0.35))
                lines = wrap_pt(txt, w - 2, size_pt, bold, font)
                guard = 0
                while len(lines) > max_lines and size_pt > 8 and guard < 24:
                    size_pt = max(8.0, size_pt - 1.0) * 1.0
                    size_pt = round(size_pt - 0.0, 2)
                    lines = wrap_pt(txt, w - 2, size_pt, bold, font)
                    max_lines = max(1, int((h - 2.0) /
                                           max(1.0, size_pt * lem) + 0.35))
                    guard += 1
                if len(lines) > max_lines:
                    lines = lines[:max_lines]
                for ln in lines:
                    laid.append((ln, size_pt, bold, colour, font,
                                 size_pt * lem))
            if laid:
                total = sum(x[5] for x in laid)
                try:
                    from pptx.enum.text import MSO_ANCHOR
                    va = sh.text_frame.vertical_anchor
                except Exception:
                    va = None
                if va == MSO_ANCHOR.MIDDLE:
                    ty = y + max(0.0, (h - total) / 2.0)
                elif va == MSO_ANCHOR.BOTTOM:
                    ty = y + max(0.0, h - total)
                else:
                    ty = y
                for ln, size_pt, bold, colour, font, lh in laid:
                    ty += size_pt * 0.92      # baseline inside the line box
                    wt2 = ' font-weight="700"' if bold else ''
                    p.append(
                        f'<text x="{x + 1:.1f}" y="{ty:.1f}" '
                        f'font-family="{esc(font)}" '
                        f'font-size="{size_pt:.1f}"'
                        + wt2 +
                        f' fill="{colour}">{esc(ln)}</text>')
                    ty += lh - size_pt * 0.92
        return p

    pages = []
    for i, slide in enumerate(prs.slides):
        if i >= max_pages:
            break
        p = [f'<svg xmlns="http://www.w3.org/2000/svg" '
             f'viewBox="0 0 {px(W):.0f} {px(H):.0f}" width="100%" role="img">',
             f'<rect width="{px(W):.0f}" height="{px(H):.0f}" fill="#ffffff"/>']
        # The slide's own background (the deck paints paper + accent edges).
        try:
            bg = slide.background.fill
            col = rgb_of(bg)
            if col and col.lower() != "#ffffff":
                p.append(f'<rect width="{px(W):.0f}" height="{px(H):.0f}" '
                         f'fill="{col}"/>')
        except Exception:
            pass
        for sh in slide.shapes:
            try:
                p.extend(shape_svg(sh))
            except Exception:
                continue
        p.append("</svg>")
        svg = "".join(p)
        if len(svg) > MAX_SVG_CHARS:
            svg = svg[:MAX_SVG_CHARS] + "</svg>"
        pages.append({"index": i, "kind": "svg", "data": svg,
                      "width_pt": round(px(W), 1),
                      "height_pt": round(px(H), 1)})
    if not pages:
        return None
    return {"mode": "pages", "pages": pages, "page_count": len(pages),
            "truncated": len(prs.slides) > len(pages),
            "note": "rendered from the deck's own shapes"}


# ── docx ────────────────────────────────────────────────────────────────────
def _preview_docx(blob: bytes, max_pages: int) -> dict:
    """Re-open the saved .docx and emit its real content as HTML."""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = Document(io.BytesIO(blob))
    out: list[str] = ["<div class='pg'>"]
    count = 0

    def emit_block(el) -> None:
        nonlocal count
        if count >= max_pages * 40:
            return
        if isinstance(el, Paragraph):
            text = (el.text or "").strip()
            if not text:
                return
            style = (getattr(el.style, "name", "") or "").lower()
            count += 1
            if "heading 1" in style or style == "title":
                out.append(f"<h1>{_esc(text)}</h1>")
            elif "heading 2" in style:
                out.append(f"<h2>{_esc(text)}</h2>")
            elif "heading 3" in style:
                out.append(f"<h3>{_esc(text)}</h3>")
            elif "list" in style:
                out.append(f"<li>{_esc(text)}</li>")
            elif "quote" in style:
                out.append(f"<blockquote>{_esc(text)}</blockquote>")
            else:
                out.append(f"<p>{_esc(text)}</p>")
        elif isinstance(el, Table):
            rows = []
            for r in el.rows[:40]:
                cells = "".join(f"<td>{_esc(c.text)}</td>"
                                for c in r.cells[:12])
                rows.append(f"<tr>{cells}</tr>")
            out.append("<table>" + "".join(rows) + "</table>")

    body = d.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            emit_block(Paragraph(child, d))
        elif tag == "tbl":
            emit_block(Table(child, d))
    out.append("</div>")
    return {"mode": "html", "html": "".join(out),
            "page_count": 1, "note": "content read from the saved .docx"}


# ── xlsx ────────────────────────────────────────────────────────────────────
def _preview_xlsx(blob: bytes, max_pages: int) -> dict:
    """Re-open the saved .xlsx and emit its real cells as an HTML grid."""
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    wb = load_workbook(io.BytesIO(blob), data_only=True)
    out: list[str] = []
    total_rows = 0
    for ws in wb.worksheets[:max_pages]:
        rows = []
        for row in ws.iter_rows(min_row=1, max_row=60, max_col=18):
            cells = []
            for c in row:
                v = c.value
                txt = "" if v is None else str(v)
                cls = " class='num'" if isinstance(v, (int, float)) else ""
                cells.append(f"<td{cls}>{_esc(txt)}</td>")
            while cells and cells[-1] == "<td></td>":
                cells.pop()
            if cells:
                rows.append(f"<tr>{''.join(cells)}</tr>")
            total_rows += 1
        out.append(f"<div class='pg'><h1>{_esc(ws.title)}</h1>"
                   f"<table class='grid'>{''.join(rows)}</table></div>")
    wb.close()
    return {"mode": "html", "html": "".join(out),
            "page_count": len(out) or 1,
            "note": "cells read from the saved .xlsx"}


# ── entry point ─────────────────────────────────────────────────────────────
def render_preview(fmt: str, blob: bytes | None, spec: dict | None,
                   max_pages: int = MAX_PREVIEW_PAGES) -> dict:
    """Preview a rendered artefact (`blob`) or a not-yet-rendered spec."""
    fmt = (fmt or "").strip().lower()
    max_pages = max(1, min(int(max_pages or MAX_PREVIEW_PAGES), 24))
    if fmt == "pdf":
        if blob is None:
            import docgen
            blob, _n = docgen.build_pdf(spec or {})
        return _preview_pdf(blob, max_pages)
    if fmt == "pptx":
        # A DELIVERED deck is previewed from its own bytes. The old path
        # rebuilt a spec from the file and re-drew it with a second layout
        # function that knew nothing about the deck's chrome, so a real slide
        # with 19 shapes previewed as white space. Reading the shapes cannot
        # drift from the file.
        if blob is not None:
            from_file = _svg_from_pptx(blob, max_pages)
            if from_file is not None:
                return from_file
        # No bytes (a live layout preview while the user is still choosing):
        # fall back to drawing the geometry from the spec.
        has_content = bool(spec and (spec.get("title")
                                     or spec.get("blocks")))
        if blob is not None and not has_content:
            try:
                spec = _spec_from_pptx(blob)
            except Exception:
                # A corrupt or unexpected .pptx should say so in the preview
                # rather than render an empty box that reads as "no slides".
                spec = None
                return {"mode": "html", "format": "pptx",
                        "html": "<p class='warn'>This .pptx could not be "
                                "opened for preview. The download still "
                                "works.</p>"}
        return _preview_pptx(spec or {}, max_pages)
    if fmt == "docx":
        if blob is None:
            import docgen
            blob, _n = docgen.build_docx(spec or {})
        return _preview_docx(blob, max_pages)
    if fmt == "xlsx":
        if blob is None:
            import docgen
            blob, _n = docgen.build_xlsx(spec or {})
        return _preview_xlsx(blob, max_pages)
    raise ValueError(f"cannot preview {fmt!r}")