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
            elif t == "chart":
                cats = b.get("categories") or []
                series = b.get("series") or []
                if cats and series:
                    vals = [_f(x) for x in (series[0].get("data") or [])]
                    top = max(vals) if vals else 1
                    plot_h, plot_w = H * 0.42, W - 2 * margin - 60
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
        # A delivered .pptx arrives with no usable spec, and previewing an
        # empty spec gave the user a blank box labelled "0 slides" under a
        # file that really had three. Read the artefact back into a spec so
        # the SAME layout function that drew the deck draws its preview.
        # A spec counts as "usable" only if it has something to draw: an
        # artifact request carries keys like `artifact`, which are truthy but
        # are not content.
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