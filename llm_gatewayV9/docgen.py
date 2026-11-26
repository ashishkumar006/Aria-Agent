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
import struct
from typing import Any

# Project root: the directory holding this file. Used to reach the stored
# document uploads when an image block reuses one of the user's own figures.
ROOT = pathlib.Path(__file__).resolve().parent

class DocGenError(ValueError):
    """Bad input the caller can fix (→ HTTP 400)."""


# ── model helpers ────────────────────────────────────────────────────────────

def _as_text(v: Any, limit: int = 0) -> str:
    """Coerce to text and trim surrounding whitespace. Never shorten content.

    `limit` is accepted and IGNORED. It used to cut a block's text at 20,000
    characters and a title at 200, which is how the end of a section simply
    stopped existing in the finished PDF. The parameter stays so the call sites
    that still pass one read the same.
    """
    if v is None:
        return ""
    if not isinstance(v, str):
        v = str(v)
    return v.strip()


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




def value_axis(values, *, want_zero: bool = True):
    """Pick (min, step, max) for a value axis. Returns None when there is nothing to plot.

    Replaces two lines that were separately wrong, and which together are the
    reason the bar charts look broken.

    The old code sized the STEP from the maximum only (`_nice_step(hi/4)`) while
    setting the MINIMUM from the data (`min(0.0, lo)`). As soon as a series has a
    negative value the two disagree about how big the axis is: measured, `[-500,
    10, 20]` asked for a step of 5 across a span of 520 — **104 gridlines** — and
    `[-900, -10]` asked for 3,604. Either the renderer chokes or the chart prints
    as a solid block of lines. For an all-negative series `top` fell back to
    `1.0`, so the axis topped out above zero while every bar pointed down from
    nowhere near it.

    The second defect is quieter and affects ordinary positive data: rounding the
    step UP and then multiplying by a fixed 4 overshoots the tallest bar. `[1, 5,
    500]` got an axis to 800, so the tallest bar filled 62.5 % of the plot and the
    top third was blank; `[10, 20, 30]` got 40 for a max of 30. The chart reads as
    squashed and the eye has no reason to trust the height.

    So: choose the tick count as well as the step, from the SPAN that is actually
    drawn, and take the tightest axis that still reads as 3-6 ticks. Bars keep a
    baseline at zero (a bar chart that does not start at zero is a lie about
    ratios), but the axis no longer pretends the data is four times smaller
    than it is.
    """
    import math
    vals = [float(v) for v in values if isinstance(v, (int, float))
            and v == v and v not in (None,)]
    if not vals:
        return None
    hi, lo = max(vals), min(vals)
    if want_zero:
        hi, lo = max(hi, 0.0), min(lo, 0.0)
    span = hi - lo
    if span <= 0:
        # A flat series. One unit of room either side of the value, or zero
        # itself, so the axis is never degenerate.
        anchor = hi if hi else 0.0
        span = step = abs(anchor) or 1.0
        return (anchor - step, step, anchor + step)
    best = None
    for ticks in (3, 4, 5, 6):
        step = _nice_step(span / ticks)
        if step <= 0:
            continue
        top = step * math.ceil(hi / step)
        bot = step * math.floor(lo / step)
        if top < hi or bot > lo:
            continue
        n = round((top - bot) / step)
        if n < 2 or n > 8:
            continue
        waste = (top - bot) / span
        # Ties on waste go to the finer axis. `[94, 91, 82]` scores exactly the
        # same for step 50 (0/50/100) and step 25 (0/25/50/75/100); the coarser
        # one leaves the reader two reference lines for three close bars, and
        # "least wasted space" alone would have chosen it.
        key = (round(waste, 9), -n)
        if best is None or key < best[0]:
            best = (key, bot, step, top)
    if best is None:
        step = _nice_step(span / 4) or span
        return (step * math.floor(lo / step), step,
                step * math.ceil(hi / step))
    return best[1], best[2], best[3]


def _norm_chart(b: dict) -> dict | None:
    """A chart needs at least one numeric series aligned to the categories.
    Anything else renders as an empty axis, which looks like a bug in the
    document rather than in the request."""
    kind = str(b.get("chart") or b.get("kind") or "bar").strip().lower()
    kind = {"column": "bar", "vertical_bar": "bar", "vbar": "bar",
            "hbar": "bar", "horizontal_bar": "bar",
            "donut": "pie", "doughnut": "pie",
            "trend": "line",
            "point": "scatter", "points": "scatter", "xy": "scatter",
            "stacked_area": "area", "stackedarea": "area"}.get(kind, kind)
    # matplotlib draws bar/line/pie/scatter/area. The vector reportlab path
    # handles bar/line/pie; scatter and area fall back to the matplotlib PNG
    # (see the PDF chart branch), which is why they are allowed here.
    if kind not in ("bar", "line", "pie", "scatter", "area"):
        kind = "bar"

    cats_raw = b.get("categories") or b.get("labels") or b.get("x") or []
    if isinstance(cats_raw, str):
        cats_raw = [cats_raw]
    cats = [_as_text(c) for c in cats_raw if _as_text(c)]
    if not cats:
        return None

    series_in = b.get("series") or b.get("data") or []
    if isinstance(series_in, dict):
        series_in = [{"name": k, "data": v} for k, v in series_in.items()]
    if isinstance(series_in, list) and series_in and not isinstance(
            series_in[0], (list, tuple, dict)):
        series_in = [{"name": b.get("name") or "value", "data": series_in}]

    series = []
    for s in series_in:
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
        vals = [_num(v) for v in data]
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


def _image_dims(data: bytes) -> tuple[int, int]:
    """Pixel size straight out of the header — no image library, no decode.

    The author chooses a figure by shape as much as by subject, and a handle
    that says "0x0" tells it nothing."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    if data[:2] == b"\xff\xd8":
        i, n = 2, len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
                i += 2
                continue
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return int(w), int(h)
            i += 2 + seg
        return 0, 0
    return 0, 0


def _embeddable_raster(blob: bytes, hint: str) -> tuple[bytes, str] | None:
    """Bytes the renderer can embed, as (blob, ext). PNG/JPEG pass through
    untouched (no decode); other rasters (GIF/BMP/TIFF/WebP) are converted to
    PNG with Pillow. Vector metafiles (EMF/WMF) have no raster renderer here,
    so they are skipped rather than listed as a figure that cannot be drawn."""
    if not blob or len(blob) > MAX_IMAGE_BYTES:
        return None
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return blob, "png"
    if blob[:2] == b"\xff\xd8":
        return blob, "jpeg"
    if (hint or "").lower() in ("emf", "wmf"):
        return None
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(blob))
        im = im.convert("RGBA" if im.mode in ("P", "LA", "RGBA") else "RGB")
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        out = buf.getvalue()
        return (out, "png") if len(out) <= MAX_IMAGE_BYTES else None
    except Exception:
        return None


def _office_figures(raw: bytes) -> list[tuple[int, int, bytes, str]]:
    """Embedded rasters in a DOCX/PPTX as (page, index, bytes, ext).

    One enumeration shared by `figures_in` and `_extract_figure`, so a listed
    handle always lifts and an out-of-range one is dropped, never clamped.
    PPTX pages are slides (matching the text parser); DOCX has no fixed
    pagination, so its images are one page in package order."""
    if raw[:4] != b"PK\x03\x04":
        return []
    import zipfile
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        names = set(zf.namelist())
    except Exception:
        return []
    is_pptx = "ppt/presentation.xml" in names
    is_docx = "word/document.xml" in names
    if not (is_pptx or is_docx):
        return []
    out: list[tuple[int, int, bytes, str]] = []
    if is_pptx:
        try:
            from pptx import Presentation
            from pptx.enum.shapes import MSO_SHAPE_TYPE
            prs = Presentation(io.BytesIO(raw))

            def walk(shapes, acc):
                for sh in shapes:
                    try:
                        st = sh.shape_type
                    except Exception:
                        st = None
                    if st == MSO_SHAPE_TYPE.GROUP:
                        walk(sh.shapes, acc)
                        continue
                    if st != MSO_SHAPE_TYPE.PICTURE:
                        continue
                    try:
                        img = sh.image
                        got = _embeddable_raster(img.blob, img.ext)
                    except Exception:
                        continue
                    if got:
                        acc.append(got)

            for n, slide in enumerate(prs.slides, 1):
                acc: list[tuple[bytes, str]] = []
                walk(slide.shapes, acc)
                for i, (blob, ext) in enumerate(acc):
                    out.append((n, i, blob, ext))
            return out
        except Exception:
            out = []                      # fall back to the media folder
    media = sorted(n for n in names
                   if n.startswith(("word/media/", "ppt/media/")))
    idx = 0
    for n in media:
        try:
            blob = zf.read(n)
        except Exception:
            continue
        got = _embeddable_raster(blob, n.rsplit(".", 1)[-1])
        if not got:
            continue
        b, ext = got
        out.append((1, idx, b, ext))
        idx += 1
    return out


def figures_in(path: pathlib.Path) -> list[dict]:
    """Every reusable figure in a stored document, as (page, index) handles.

    `index` is the position in `page.images` — the same enumeration
    `_extract_figure` resolves against, so a handle read out of this list can
    never point at a figure the renderer cannot lift.

    Metadata comes from the image object itself rather than from its pixels:
    a scanned 300-page book is read as a directory here, not decoded as 300
    pictures. That matters because this runs once per authoring call."""
    raw = path.read_bytes()
    if raw[:4] != b"%PDF":
        # A plain image upload is its own single figure.
        if raw[:8] == b"\x89PNG\r\n\x1a\n" or raw[:2] == b"\xff\xd8":
            ext = "png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "jpeg"
            w, h = _image_dims(raw)
            return [{"page": 1, "index": 0, "ext": ext, "bytes": len(raw),
                     "width": w, "height": h}]
        # DOCX/PPTX: the same enumeration _extract_figure resolves against.
        out: list[dict] = []
        for page, index, blob, ext in _office_figures(raw):
            w, h = _image_dims(blob)
            out.append({"page": page, "index": index, "ext": ext,
                        "bytes": len(blob), "width": w, "height": h})
        return out
    try:
        from pypdf import PdfReader
    except Exception:
        return []
    try:
        reader = PdfReader(str(path))
    except Exception:
        return []
    out: list[dict] = []
    try:
        pages = list(reader.pages)
    except Exception:
        # `PdfReader(str(path))` is lazy: a file with a valid header but no
        # Root object OPENS and only throws when the page tree is walked. That
        # raised out of this function and 500'd /v1/documents/{id}/figures -
        # one broken upload took the whole figure listing down instead of
        # reporting "no figures", which the caller already handles.
        return []
    for n, page in enumerate(pages, 1):
        try:
            imgs = list(page.images)
        except Exception:
            continue                      # one unreadable page is not a failed doc
        for i, im in enumerate(imgs):
            ref = getattr(im, "indirect_reference", None)
            obj = None
            try:
                obj = ref.get_object() if ref is not None else None
            except Exception:
                obj = None
            width = height = 0
            if obj is not None:
                try:
                    width = int(obj.get("/Width") or 0)
                    height = int(obj.get("/Height") or 0)
                except Exception:
                    pass
            name = str(getattr(im, "name", "") or "")
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else "png"
            if ext not in ("png", "jpeg", "jpg"):
                ext = "jpeg" if ext in ("jpx", "jp2", "ccitt", "jbig2") else "png"
            out.append({"page": n, "index": i, "ext": ext,
                        "width": width, "height": height})
    return out


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
        want_page = int(page or 1)
        want_idx = int(index or 0)
        for p, i, blob, ext in _office_figures(raw):
            if p == want_page and i == want_idx:
                return blob, ext
        return None
    try:
        from pypdf import PdfReader
    except Exception:
        return None
    try:
        reader = PdfReader(str(path))
        want_page = int(page or 1)
        want_idx = int(index or 0)
        # An out-of-range handle is a failed reference, not a near miss: this
        # used to clamp, so "page 9" of a 3-page source silently printed page
        # 3's figure and the document looked like it had the right picture.
        if want_page < 1 or want_page > len(reader.pages):
            return None
        imgs = list(reader.pages[want_page - 1].images)
        if not imgs or want_idx < 0 or want_idx >= len(imgs):
            return None
        im = imgs[want_idx]
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
    _data = b.get("data")
    if isinstance(_data, str) and _data.strip():
        # A base64 image, inlined by the agent for an `art:` handle or a
        # sandbox path (the gateway is a separate process and cannot read
        # either). Decode it here; a bad payload is ignored, not fatal.
        import base64 as _b64
        try:
            _blob = _b64.b64decode(_data, validate=False)
        except Exception:
            _blob = b""
        if _blob:
            return {"type": "image", "data": _blob,
                    "ext": _as_text(b.get("ext"), 8) or "png",
                    "page": int(_num(b.get("page"), 1) or 1),
                    "caption": _as_text(b.get("caption"), 300),
                    "width_mm": min(170.0, max(40.0, _num(b.get("width_mm"), 150.0)
                                               or 150.0)),
                    "alt": _as_text(b.get("alt") or b.get("caption"), 300)}
    if isinstance(_data, (bytes, bytearray)) and _data:
        return {"type": "image", "data": bytes(_data),
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


def _norm_blocks(raw: Any, doc_title: str = "") -> list[dict]:
    """Validate/clip the block list. Raises DocGenError only when there is
    nothing at all to render."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise DocGenError("blocks must be a list")
    out: list[dict] = []
    for b in raw:
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
                # A heading with no level is level 1, not 2. It used to default
                # to 2, which made `{"type": "heading", "text": "Trade-offs"}` —
                # the shape a model writes when it omits the field — an
                # unnumbered sub-heading: in a deck it stopped being a slide
                # boundary, and then fell out of the output entirely.
                try:
                    lvl = int(b.get("level") or 1)
                except (TypeError, ValueError):
                    lvl = 1
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
            clean = [_as_text(i) for i in items]
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
            cols = max(1, cols)
            head = ([_as_text(h, 200) for h in header][:cols]
                    if isinstance(header, list) and header else None)
            body = []
            for r in rows:
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
                        "title": _as_text(b.get("title")) or doc_title,
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
        elif kind == "callout":
            # A titled, shaded box - the "KEY INSIGHT" / "EXAM ALERT" panel a
            # study document uses to break the wall of prose.
            txt = _as_text(b.get("text") or b.get("body"))
            title = _as_text(b.get("title") or b.get("label"), 60)
            tone = str(b.get("tone") or "insight").strip().lower()
            if txt or title:
                out.append({"type": "callout", "title": title or "Note",
                            "text": txt, "tone": tone})
        elif kind == "stats":
            raw = b.get("items") or b.get("stats") or b.get("values")
            items = []
            for it in (raw if isinstance(raw, list) else []):
                if isinstance(it, dict):
                    v = _as_text(it.get("value") or it.get("number"), 40)
                    lab = _as_text(it.get("label") or it.get("caption"), 120)
                    if v or lab:
                        items.append({"value": v, "label": lab})
                elif it:
                    items.append({"value": _as_text(it, 40), "label": ""})
            if items:
                out.append({"type": "stats", "items": items[:4]})
            else:
                _note_drop(kind, "no stat items")
        elif kind == "columns":
            raw = b.get("items") or b.get("columns") or b.get("cards")
            cols = []
            for it in (raw if isinstance(raw, list) else []):
                if not isinstance(it, dict):
                    if it:
                        cols.append({"title": "", "body": _as_text(it)})
                    continue
                cols.append({
                    "title": _as_text(it.get("title") or it.get("heading"), 80),
                    "body": _as_text(it.get("text") or it.get("body")
                                     or it.get("description"), 400),
                })
            cols = [c for c in cols if c["title"] or c["body"]]
            if cols:
                out.append({"type": "columns", "items": cols[:3]})
            else:
                _note_drop(kind, "no columns")
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
    "stats": "stats",
    "stat": "stats",
    "kpi": "stats",
    "kpis": "stats",
    "numbers_row": "stats",
    "callout": "callout",
    "insight": "callout",
    "alert": "callout",
    "box": "callout",
    "aside": "callout",
    "columns": "columns",
    "cards": "columns",
    "cards_row": "columns",
    "cols": "columns",
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
     "note": "**bold**, `code` and [links](https://x) are rendered. Nothing "
             "else is typeset: no maths, so write O(N²) and d_k, not "
             "$O(N^2)$ or $d_k$ - LaTeX prints as written."},
    {"type": "bullets", "items": ["string"], "format": "all"},
    {"type": "numbers", "items": ["string"], "format": "all"},
    {"type": "quote", "text": "string", "format": "all"},
    {"type": "chart", "kind": "bar | line | pie | scatter | area",
     "categories": ["string"],
     "series": [{"name": "string", "data": [1, 2, 3]}],
     "title": "string", "format": "vector in pdf, NATIVE editable chart "
                                  "in pptx, data table in docx; scatter/area are "
                                  "rendered by matplotlib in every format"},
    {"type": "image", "document": "<uploaded doc id>", "page": 1,
     "caption": "string", "width_mm": 140, "format": "all",
     "note": "A figure lifted from a document the USER uploaded, OR a plot you "
     "produced: pass `document` (an uploaded doc id), `artifact` (an `art:` "
     "handle from render_document/save_as_document), or `path` (a sandbox file, "
     "e.g. a matplotlib/matlab PNG). There is no URL fetching."},
    {"type": "table", "header": ["string"], "rows": [["string"]],
     "format": "all"},
    {"type": "stats",
     "items": [{"value": "42 GW", "label": "installed capacity"}],
     "format": "pptx (big-number cards); pdf prints them as a short list",
     "note": "One to four large figures with a short label each. Use for the "
             "numbers a slide is actually about, instead of burying them in "
             "bullets."},
    {"type": "columns",
     "items": [{"title": "Option A", "body": "one or two sentences"}],
     "format": "pptx (two or three side-by-side cards); pdf as sections",
     "note": "Two or three parallel points read across at once - the right "
             "layout for a comparison that is not purely numeric."},
    {"type": "callout", "title": "Key insight",
     "text": "the one sentence the reader must remember",
     "tone": "insight | warning",
     "format": "all",
     "note": "A titled, tinted box that breaks a wall of prose. Use once or "
             "twice per document for the conclusion or the trap, never as a "
             "replacement for a paragraph."},
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


# A line that starts with 1-6 hashes and a space is a markdown heading written
# into the body — the author skill does it when it labels its own continuation
# chunk ("## continuation"), and it reached the page verbatim in every block
# type. Markdown's own rule needs the space, so `C#` and `#include` survive.
_ATX_R = re.compile(r"^ {0,3}#{1,6}[ \t]+", re.M)
# Single-letter base with a short subscript/superscript: d_k, d_ff, x_hat, W_0,
# K^T. Bounded on purpose — `W_merged` is a variable name, not a subscript.
_SUBSCRIPT_R = re.compile(r"(?<!\w)([A-Za-z])_([a-z0-9]{1,3})(?!\w)")
_SUPERSCRIPT_R = re.compile(r"(?<!\w)([A-Za-z])\^([A-Za-z0-9]{1,3})(?!\w)")


def _scripts(s: str) -> str:
    """Typeset those tokens as real sub/superscripts in the markup path.

    The plain-text formats keep the underscore (docx and pptx have no
    equivalent here), and anything already inside a Courier span is code and is
    left exactly as the author wrote it."""
    parts = re.split(r"(<font[^>]*>.*?</font>)", s, flags=re.S)
    for i, part in enumerate(parts):
        if not part or part.startswith("<font"):
            continue
        part = _SUBSCRIPT_R.sub(r"\1<sub>\2</sub>", part)
        parts[i] = _SUPERSCRIPT_R.sub(r"\1<super>\2</super>", part)
    return "".join(parts)


def _strip_md(s: str) -> str:
    """Plain text for the formats that cannot typeset inline markup.

    Also normalises LaTeX and replaces characters the fonts cannot draw: the
    same `($Q$)` that reached a PDF reader reached the docx too, and an
    out-of-repertoire glyph renders as the WRONG letter rather than a box.
    """
    s = _ATX_R.sub("", s)
    s = _MD_LINK.sub(r"\1 (\2)", s)
    s = _MD_BOLD.sub(r"\1", s)
    s = _MD_CODE.sub(r"\1", s)
    s = re.sub(r"[*`]+", "", s)
    # An underscore between two word characters is part of an identifier
    # (`d_k`, `batch_size`), not emphasis. The blanket `[*_`]+` strip used to
    # turn `d_k` into `dk`, which reads as a different variable.
    s = re.sub(r"(?<!\w)_|_(?!\w)", "", s)
    return _font_safe(_math_to_text(s))


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
    for r in raw:
        if isinstance(r, str):
            if r.strip():
                out.append({"title": r.strip(), "type": "misc"})
        elif isinstance(r, dict):
            out.append({k: v for k, v in r.items() if v not in (None, "")})
    return out


def _wants_latex(spec: dict) -> bool:
    """Did the caller ask for real typesetting?

    Both spellings are accepted because two different callers exist: the agent
    (which sets `typeset`) and a direct API client (which sets `engine`).
    """
    for key in ("engine", "typeset"):
        if str(spec.get(key) or "").strip().lower() in ("latex", "tex",
                                                        "tectonic", "xelatex"):
            return True
    return False


def _chart_png(block: dict, *, dpi: int = 200, width_in: float = 7.2,
               height_in: float = 3.5) -> bytes | None:
    """Draw a chart block with matplotlib and return PNG bytes.

    Why matplotlib at all, when reportlab already draws vector charts: the
    LaTeX path needs a file it can `\\includegraphics`, and a document with
    charts should look like one document rather than two renderers' work. On
    this path the chart is raster at 200 dpi, which is print quality at the
    column width and costs ~60 KB.

    Returns None when matplotlib is unavailable or the block has nothing
    plottable - the caller keeps the vector path in that case.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None
    cats = [str(c) for c in (block.get("categories") or [])]
    series = [s for s in (block.get("series") or [])
              if isinstance(s, dict) and s.get("data")]
    if not cats or not series:
        return None
    kind = str(block.get("chart") or "bar")
    is_pie = kind == "pie"
    accent = "#5b4bd6"
    palette = ["#5b4bd6", "#9c8ef0", "#2f9e7c", "#c97b2b",
               "#7a1f4b", "#2b4a7d", "#8a6a1f", "#5c5c66"]
    fig, ax = plt.subplots(figsize=(width_in, height_in), dpi=dpi)
    try:
        if is_pie:
            vals = [max(0.0, float(v)) for v in series[0]["data"]]
            n = min(len(vals), len(cats))
            if sum(vals[:n]) <= 0:
                return None
            ax.pie(vals[:n], labels=cats[:n], autopct="%1.0f%%",
                   colors=palette[:n], startangle=90,
                   wedgeprops={"linewidth": 0.6, "edgecolor": "white"},
                   textprops={"fontsize": 9})
            ax.axis("equal")
        elif kind == "line":
            xs = list(range(len(cats)))
            for i, s in enumerate(series[:6]):
                ys = [float(v) for v in s["data"][:len(cats)]]
                ax.plot(xs, ys, marker="o", markersize=3.4, linewidth=1.9,
                        color=palette[i % len(palette)],
                        label=str(s.get("name") or f"series {i + 1}"))
            ax.set_xticks(xs)
            ax.set_xticklabels(cats, rotation=0, fontsize=8.5)
        elif kind == "scatter":
            # Points, not a connected line: the right matplotlib plot for a
            # relationship with no order (x vs y), which bar/line/pie cannot
            # express. Categories give the x labels; each series is one cloud.
            xs = list(range(len(cats)))
            for i, s in enumerate(series[:6]):
                ys = [float(v) for v in s["data"][:len(cats)]]
                pts = min(len(xs), len(ys))
                ax.scatter(xs[:pts], ys[:pts], s=26,
                           color=palette[i % len(palette)],
                           label=str(s.get("name") or f"series {i + 1}"))
            ax.set_xticks(xs)
            ax.set_xticklabels(cats, rotation=0, fontsize=8.5)
        elif kind == "area":
            # A filled line: volume/magnitude over an ordered axis.
            xs = list(range(len(cats)))
            for i, s in enumerate(series[:6]):
                ys = [float(v) for v in s["data"][:len(cats)]]
                ax.fill_between(xs, ys, color=palette[i % len(palette)],
                                alpha=0.45, linewidth=0)
                ax.plot(xs, ys, color=palette[i % len(palette)], linewidth=1.6,
                        label=str(s.get("name") or f"series {i + 1}"))
            ax.set_xticks(xs)
            ax.set_xticklabels(cats, rotation=0, fontsize=8.5)
        else:
            xs = list(range(len(cats)))
            n = len(series[:6])
            bw = 0.78 / max(1, n)
            for i, s in enumerate(series[:6]):
                ys = [float(v) for v in s["data"][:len(cats)]]
                offs = [x - 0.39 + bw * (i + 0.5) for x in xs]
                ax.bar(offs, ys, width=bw,
                       color=palette[i % len(palette)],
                       label=str(s.get("name") or f"series {i + 1}"))
            ax.set_xticks(xs)
            ax.set_xticklabels(cats, fontsize=8.5)
        if kind != "pie":
            ax.set_title(str(block.get("title") or ""), fontsize=10.5,
                         color="#111114", pad=9)
            ax.grid(axis="y", color="#e2e2ea", linewidth=0.7)
            ax.set_axisbelow(True)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
            for side in ("left", "bottom"):
                ax.spines[side].set_color("#c9c9d4")
            ax.tick_params(colors="#3a3a44", labelsize=8.5)
            if len(series) > 1 or any(s.get("name") for s in series):
                ax.legend(frameon=False, fontsize=8.5, loc="upper center",
                          bbox_to_anchor=(0.5, -0.13), ncol=min(4, len(series)))
                fig.tight_layout(rect=(0, 0.04, 1, 1))
            fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=dpi, facecolor="white",
                    bbox_inches="tight", pad_inches=0.12)
        return buf.getvalue()
    finally:
        plt.close(fig)


def _build_pdf_latex(spec: dict) -> tuple[bytes, str] | None:
    """The LaTeX path. Returns None when it cannot be used.

    None is not an error: a caller that asked for LaTeX on a machine with no
    engine must still get a document, so the caller falls back to the
    reportlab renderer. A TeX COMPILE failure is different - that is a bug in
    the generated source, and it falls back too, but it says so loudly.
    """
    try:
        import docgen_latex as _tex
    except Exception:
        return None
    ok, why = _tex.available()
    if not ok:
        print(f"[docgen] latex requested but unavailable ({why}); "
              f"using the reportlab renderer")
        return None
    title = _as_text(spec.get("title"))
    blocks = _norm_blocks(spec.get("blocks"), title)
    if not title and not blocks:
        raise DocGenError("nothing to render: give a title or some blocks")
    # Figures: charts are drawn by matplotlib, images come from the user's
    # uploads (already resolved to bytes by _norm_image).
    figures: dict[int, tuple[bytes, str]] = {}
    for i, b in enumerate(blocks):
        if b["type"] == "chart":
            png = _chart_png(b)
            if png:
                figures[i] = (png, "png")
        elif b["type"] == "image" and b.get("data"):
            figures[i] = (bytes(b["data"]), b.get("ext") or "png")
    refs: list[str] = []
    _refs = _norm_references(spec.get("references"))
    if _refs:
        import doclayout as _lay
        refs = _lay.build_reference_list(
            _refs, _lay.resolve_citation(spec.get("citation_style")))
    try:
        data = _tex.render(dict(spec), blocks, refs, figures)
    except Exception as e:
        print(f"[docgen] latex render failed ({e}); falling back to reportlab")
        return None
    if not data:
        return None
    return data, _safe_filename(title, "pdf")


def build_pdf(spec: dict) -> tuple[bytes, str]:
    """Render a report.

    The first version of this function produced technically-valid output that
    read like a text dump: one heading style for every level, bullets with no
    hanging indent, a "quote" that was just smaller grey text, table rows with
    no vertical padding, and no page numbers anywhere. Rendering it and
    LOOKING at it is what caught all of that; the shapes below are the result.

    A spec that asks for `engine: latex` is typeset by `docgen_latex` instead:
    real maths, numbered equations and TeX hyphenation. Everything else keeps
    the reportlab path below, unchanged.
    """
    if _wants_latex(spec):
        got = _build_pdf_latex(spec)
        if got is not None:
            return got
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (KeepTogether, PageBreak, Paragraph,
                                    SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    title = _as_text(spec.get("title"))
    blocks = _norm_blocks(spec.get("blocks"), title)
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
        # Subject/keywords were left unset, so every rendered file reported
        # `Subject: (unspecified)` in a PDF reader's properties panel.
        subject=_as_text(spec.get("subject"), 200)
        or _as_text(spec.get("subtitle"), 200) or "",
        author=_as_text(spec.get("author"), 120) or "")

    ss = getSampleStyleSheet()
    # Visual chrome for the style, if it declares one. Styles without it (the
    # original ten) keep the plain left-aligned black-on-white look exactly as
    # before - the keys default to the old behaviour.
    _chrome = dict(_style.get("chrome") or {})

    def _chrome_val(key, default):
        return _chrome.get(key, default)

    _masthead = _chrome_val("masthead", "left")
    _title_align = (TA_CENTER if _masthead in ("centred", "band",
                                              "cover_band") else TA_LEFT)
    _title_color = (colors.HexColor(str(_chrome_val("cover_ink", "#ffffff")))
                    if _masthead == "cover_band" else INK)
    st_title = ParagraphStyle(
        "t", parent=ss["Title"], fontName=F_BOLD,
        fontSize=float(_chrome_val("title_size", 19)),
        leading=float(_chrome_val("title_size", 19)) * 1.22,
        spaceAfter=4, textColor=_title_color, alignment=_title_align)
    st_sub = ParagraphStyle("s", parent=ss["Normal"], fontSize=10.5, leading=14,
                            textColor=MUTED, spaceAfter=2,
                            alignment=_title_align)
    st_byline = ParagraphStyle("by", parent=st_sub, spaceAfter=10)

    def _furniture(canv, document):
        """Footer: a hairline rule and `page N`. A briefing with no page
        numbers cannot be referenced, printed, or checked against a claim.
        A running header carries the document title on pages after the
        first, which is what makes a stacked printout identifiable."""
        canv.saveState()
        pg = getattr(document, "page", 1)
        total = getattr(document, "_total_pages", 0)

        def _fit(text: str, font: str, size: float, avail: float) -> str:
            """Shorten to the WIDTH available, not to a character count.

            `title[:78]` produced a two-line header on every page of a
            document whose title was the whole request: a long string sliced
            at 78 characters still overflows the measure, and ReportLab's
            drawString does not wrap - it runs off the page and the extract
            shows it doubled on every page. Trim to the actual width, with an
            ellipsis so it is visibly elided rather than looking truncated."""
            s = str(text or "")
            if not s:
                return ""
            if canv.stringWidth(s, font, size) <= avail:
                return s
            ell = "\u2026"
            lo, hi = 0, len(s)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if canv.stringWidth(s[:mid] + ell, font, size) <= avail:
                    lo = mid
                else:
                    hi = mid - 1
            return (s[:lo] + ell) if lo else ell

        # `running_header` was documented and accepted but NEVER READ - the
        # header always drew `title`, so a caller asking for a short running
        # head got the full title on every page instead. The title belongs on
        # the cover and in the footer; this is the header's own text.
        # The 400 here is a sanity bound only: clipping to a round number like
        # 120 silently chopped a long header with no ellipsis, so `_fit` below
        # never saw anything to elide and the header just stopped mid-word.
        # `_fit` is the single authority on how much actually fits.
        hdr = _as_text(spec.get("running_header"), 400) or title
        # The coursework cover: the band now carries its own title block (a
        # full-bleed table), so only the eyebrow rule is drawn here.
        if pg == 1 and _masthead == "cover_band":
            canv.setFillColor(ACCENT)
            canv.setStrokeColor(ACCENT)
            canv.setLineWidth(1.6)
            canv.line(ML, _page[1] - 22 * mm, ML + 26 * mm,
                      _page[1] - 22 * mm)
        elif pg > 1 and hdr and _chrome_val("running", "plain") == "split":
            # A two-part running head: the document on the left in muted ink,
            # the part name in the accent on the right, a hairline under both.
            canv.setFont(F_BODY, 7.8)
            canv.setFillColor(MUTED)
            left_txt = _fit(hdr, F_BODY, 7.8,
                            (_page[0] - ML - MR) * 0.58)
            canv.drawString(ML, _page[1] - 13 * mm, left_txt)
            canv.setFillColor(ACCENT)
            right_txt = _fit(str(_chrome_val("running_right", "Study Reference")),
                             F_BOLD, 7.8, (_page[0] - ML - MR) * 0.38)
            canv.drawRightString(_page[0] - MR, _page[1] - 13 * mm, right_txt)
            canv.setStrokeColor(RULE)
            canv.setLineWidth(0.5)
            canv.line(ML, _page[1] - 14.6 * mm, _page[0] - MR,
                      _page[1] - 14.6 * mm)
        elif pg > 1 and hdr:
            canv.setFont("Helvetica", 7.6)
            canv.setFillColor(colors.HexColor("#8b8b96"))
            canv.drawString(ML, _page[1] - 13 * mm,
                            _fit(hdr, "Helvetica", 7.6, _page[0] - MR - ML))
            canv.setStrokeColor(colors.HexColor("#ececf2"))
            canv.setLineWidth(0.4)
            canv.line(ML, _page[1] - 14.6 * mm, _page[0] - MR, _page[1] - 14.6 * mm)
        canv.setStrokeColor(RULE)
        canv.setLineWidth(0.5)
        y = 16 * mm
        canv.line(ML, y + 4 * mm, _page[0] - MR, y + 4 * mm)
        canv.setFont(F_BODY, 8)
        canv.setFillColor(MUTED)
        label = _fit(title or "document", F_BODY, 8, _page[0] - MR - ML - 70)
        if label:
            canv.drawString(ML, y - 2 * mm, label)
        page_label = f"page {pg}" if not total else f"page {pg} of {total}"
        canv.drawRightString(_page[0] - MR, y - 2 * mm, page_label)
        canv.restoreState()
    st_h1 = ParagraphStyle("h1", parent=ss["Heading1"], fontName=F_BOLD,
                               fontSize=round(BODY * float(_chrome_val("h1_size", SCALE)), 1),
                               leading=round(BODY * float(_chrome_val("h1_size", SCALE)) * 1.22, 1),
                               spaceBefore=round(LEAD, 1), spaceAfter=round(LEAD * 0.34, 1),
                               textColor=(ACCENT if _chrome_val("heading_color", "ink") == "accent"
                                          else INK), keepWithNext=1)
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
    # Stat cards on paper: a large accent figure over a small muted label.
    st_statv = ParagraphStyle("statv", parent=st_p, fontName=F_BOLD,
                              fontSize=round(BODY * 1.9, 1),
                              leading=round(BODY * 2.1, 1),
                              spaceAfter=2, textColor=ACCENT)
    st_statc = ParagraphStyle("statc", parent=st_p, fontName=F_BODY,
                              fontSize=round(BODY * 0.84, 1),
                              leading=round(BODY * 1.05, 1),
                              spaceAfter=0, textColor=MUTED)
    # Hairline + tint used by the stat row; derived from the accent so one
    # palette governs the whole document.
    _ar, _ag, _ab = ACCENT.red, ACCENT.green, ACCENT.blue
    _stat_line = colors.Color(_ar + (1 - _ar) * 0.72,
                              _ag + (1 - _ag) * 0.72,
                              _ab + (1 - _ab) * 0.72)
    _stat_tint = colors.Color(_ar + (1 - _ar) * 0.93,
                              _ag + (1 - _ag) * 0.93,
                              _ab + (1 - _ab) * 0.93)
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

    def _unnumbered(text: str) -> str:
        """Drop a leading "3." / "3.1" the author already wrote.

        The contents page read "1. 1. Introduction and Historical Context":
        the model kept writing its own outline numbering into the heading
        text (it was carrying forward a numbered outline from the first
        render), and the renderer then numbered it again. Numbering an
        already-numbered heading is the defect, not the model's prose."""
        import re as _re2
        return _re2.sub(r"^\s*\d+(?:\.\d+)*\.?\s+", "", str(text or ""), count=1)

    def _numbered(text: str, level: int) -> str:
        if level == 1:
            counters[0] += 1
            counters[1] = 0
            return f"{counters[0]}. {_strip_md(_unnumbered(text))}"
        counters[1] += 1
        return f"{counters[0]}.{counters[1]} {_strip_md(_unnumbered(text))}"

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

    def _callout(b):
        """A titled, tinted box: the study-document 'KEY INSIGHT' panel.

        Built as ONE two-row table (title row, body row) so the tint, the
        border and the spacing belong to the same object. Nesting one table
        inside another left the tint short of the border and the body text
        overhanging the frame."""
        title = _as_text(b.get("title")) or "Note"
        body = _as_text(b.get("text"))
        tone = str(b.get("tone") or "insight").lower()
        bar = colors.HexColor("#b3261e") if tone in ("warning", "danger",
                                                    "alert") else ACCENT
        o, g_, bl = bar.red, bar.green, bar.blue
        tint = colors.Color(o + (1 - o) * 0.94, g_ + (1 - g_) * 0.94,
                            bl + (1 - bl) * 0.94)
        pad = 8
        head = Paragraph(_rich(title.upper()), ParagraphStyle(
            "cot", parent=st_h3, fontName=F_BOLD,
            fontSize=round(BODY * 0.80, 1), textColor=colors.white,
            spaceBefore=0, spaceAfter=0))
        rows = [[head]]
        if body:
            rows.append([Paragraph(_rich(body), ParagraphStyle(
                "cob", parent=st_p, fontSize=round(BODY * 0.97, 1),
                spaceAfter=0, textColor=colors.HexColor("#23232c")))])
        t = Table(rows, colWidths=[avail_w])
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), bar),
            ("BOX", (0, 0), (-1, -1), 0.8, bar),
            ("LEFTPADDING", (0, 0), (-1, -1), pad),
            ("RIGHTPADDING", (0, 0), (-1, -1), pad),
            ("TOPPADDING", (0, 0), (-1, 0), 4),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
        ]
        if body:
            style += [
                ("BACKGROUND", (0, 1), (-1, -1), tint),
                ("TOPPADDING", (0, 1), (-1, -1), pad),
                ("BOTTOMPADDING", (0, 1), (-1, -1), pad),
            ]
        t.setStyle(TableStyle(style))
        return [KeepTogether([t]), Spacer(1, 9)]

    def _stats(b):
        """Big-number callouts as a bordered, tinted row on paper."""
        items = b.get("items") or []
        if not items:
            return None
        n = max(1, len(items))
        widths = [avail_w / n] * n
        vals = [Paragraph(_rich(str(it.get("value") or "")), st_statv)
                for it in items]
        labs = [Paragraph(_rich(str(it.get("label") or "")), st_statc)
                for it in items]
        t = Table([vals, labs], colWidths=widths)
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOX", (0, 0), (-1, -1), 0.6, _stat_line),
            ("INNERGRID", (0, 0), (-1, -1), 0.6, _stat_line),
            ("BACKGROUND", (0, 0), (-1, -1), _stat_tint),
            ("LEFTPADDING", (0, 0), (-1, -1), 9),
            ("RIGHTPADDING", (0, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, 0), 9),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 0),
            ("TOPPADDING", (0, 1), (-1, 1), 0),
            ("BOTTOMPADDING", (0, 1), (-1, 1), 9),
        ]))
        return [t, Spacer(1, 9)]

    def _columns(b):
        """Two or three parallel points as side-by-side, ruled columns."""
        items = b.get("items") or []
        if not items:
            return None
        n = max(1, len(items))
        widths = [avail_w / n] * n
        head = [Paragraph(_rich(str(c.get("title") or "")), st_h3) for c in items]
        bodies = []
        for c in items:
            bodies.append(Paragraph(_rich(str(c.get("body") or "")), st_p))
        t = Table([head, bodies], colWidths=widths)
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEABOVE", (0, 0), (-1, 0), 1.4, ACCENT),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, 0), 6),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 3),
            ("TOPPADDING", (0, 1), (-1, 1), 0),
        ]))
        return [t, Spacer(1, 9)]

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
            # Light column rules. Without them a comparison table reads as one
            # undifferentiated run of text - both to a person scanning it and
            # to text extraction, which flattened every cell into a single
            # line ("Perspective Argument Regarding Sinter ...") with no way to
            # see where one column ended and the next began.
            ("LINEBEFORE", (1, 0), (-1, -1), 0.3, RULE),
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
        if kind in ("scatter", "area"):
            # reportlab has no scatter/area chart; render with matplotlib and
            # embed the PNG (the same engine the LaTeX and OOXML paths use), so
            # a scatter/area is a real plot rather than an empty axis.
            png = _chart_png(b)
            if png:
                from reportlab.platypus import Image as RLImage
                from PIL import Image as PILImage
                try:
                    with PILImage.open(io.BytesIO(png)) as im:
                        iw, ih = im.size
                    out = []
                    if b.get("title"):
                        out.append(Paragraph(_rich(b["title"]), st_h3))
                    w = avail_w
                    h = w * ih / iw
                    if h > 300:
                        h = 300
                        w = h * iw / ih
                    flow = RLImage(io.BytesIO(png), width=w, height=h)
                    flow.hAlign = "CENTER"
                    out.append(flow)
                    if b.get("caption"):
                        out.append(Paragraph(_rich(b["caption"]), st_cap))
                    out.append(Spacer(1, 9))
                    return out
                except Exception:
                    pass
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
            # A pie has room for its legend beside it.
            w = min(avail_w, h + 150.0)
            # reportlab draws each slice's label OUTSIDE the circle, so a pie
            # that fills its drawing puts those labels above the drawing's own
            # top edge — live, the chart title printed straight through the
            # first category. The inset is the label's margin.
            inset = 20.0
            diameter = max(60.0, h - 2 * inset)
            pie = Pie()
            pie.x = max(inset, (w - diameter) / 2.0)
            pie.y = inset
            pie.width = pie.height = diameter
            first = series[0]["data"]
            pie.data = [max(0.0, v) for v in first]
            pie.labels = [str(c)[:14] for c in cats]
            pie.sideLabels = False
            for i in range(len(pie.slices)):
                pie.slices[i].fillColor = shades[i % len(shades)]
            chart_obj = pie
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

        all_vals = [v for s in series for v in s["data"]]
        ax = value_axis(all_vals, want_zero=(kind != "line"))
        if ax and hasattr(chart_obj, "valueAxis"):
            bot, step, top_nice = ax
            # One helper for both ends of the axis. Splitting it — minimum from
            # the data, step from the maximum alone — is what produced a
            # 104-gridline chart for `[-500, 10, 20]` and a bar that filled
            # 62.5 % of the plot for `[1, 5, 500]`. See `value_axis`.
            chart_obj.valueAxis.valueMin = bot
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
        if t == "callout":
            return _callout(b)
        if t == "stats":
            return _stats(b)
        if t == "columns":
            return _columns(b)
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
        # Masthead. `cover_band` paints a full-bleed block behind white display
        # type; `band` sits the title under a full-width rule; `centred` sets a
        # centred title with a short rule underneath. The page furniture draws
        # the band (it needs the page canvas), so here we only reserve space.
        _mt = _chrome_val("masthead", "left")
        if _mt == "cover_band":
            # The band owns the whole first page: a full-bleed navy block with
            # the eyebrow, the display title and the subtitle set into it. Those
            # three lines are drawn as a table whose background IS the band, so
            # the type sits inside the colour instead of on white below it.
            _ink = colors.HexColor(str(_chrome_val("cover_ink", "#ffffff")))
            _band_h = float(_chrome_val("band_h", 78)) * mm
            _eyebrow = Paragraph(
                _rich(_as_text(spec.get("running_header"), 200)
                      or title.upper()),
                ParagraphStyle("cbe", parent=st_sub, fontName=F_BOLD,
                               fontSize=8.5, leading=11, textColor=ACCENT,
                               alignment=TA_LEFT))
            _ttl = Paragraph(_rich(title), ParagraphStyle(
                "cbt", parent=st_title, fontSize=float(
                    _chrome_val("title_size", 24)),
                leading=float(_chrome_val("title_size", 24)) * 1.16,
                textColor=_ink, alignment=TA_LEFT, spaceAfter=0))
            _sub = _as_text(spec.get("subtitle"), 300)
            _cells = [[_eyebrow], [_ttl]]
            if _sub:
                _cells.append([Paragraph(_rich(_sub), ParagraphStyle(
                    "cbs", parent=st_sub, fontSize=11, leading=15,
                    textColor=colors.Color(0.78, 0.80, 0.85),
                    alignment=TA_LEFT))])
            _block = Table(_cells, colWidths=[_page[0] - ML - MR])
            _block.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(
                    str(_chrome_val("cover_band", "#16233c")))),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, 0), 22),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                ("TOPPADDING", (0, 1), (-1, 1), 0),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 6),
                ("TOPPADDING", (0, -1), (-1, -1), 0),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 18),
            ]))
            story.append(_block)
            story.append(Spacer(1, 10))
        elif _mt == "band":
            story.append(Spacer(1, 6))
            story.append(Paragraph(_rich(title), st_title))
            story.append(Spacer(1, 4))
            _band = Table([[""]], colWidths=[_page[0] - ML - MR],
                          rowHeights=[1.4])
            _band.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), ACCENT),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
            _band.hAlign = "LEFT"
            story.append(_band)
            story.append(Spacer(1, 6))
        else:
            story.append(Paragraph(_rich(title), st_title))
            _rule_w = 34 * mm
            rule = Table([[""]], colWidths=[_rule_w], rowHeights=[1.6])
            rule.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), ACCENT),
                                      ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                      ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                      ("TOPPADDING", (0, 0), (-1, -1), 0),
                                      ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
            # reportlab centres a narrow table by default, which floated the
            # rule into the middle of the page beside the title.
            rule.hAlign = ("CENTER" if _title_align == TA_CENTER else "LEFT")
            story.append(rule)
            story.append(Spacer(1, 7))
    sub = _as_text(spec.get("subtitle"), 300)
    if sub and not has_cover and _masthead != "cover_band":
        story.append(Paragraph(_rich(sub), st_sub))
    au = _as_text(spec.get("author"), 120)
    if au and not has_cover and _masthead != "cover_band":
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
            # And without a cover the title is only a heading, so the contents
            # belong under it. Breaking there spent the rest of page 1: a live
            # 23-page report opened with 36 characters of content on it.
            story.append(Spacer(1, 9))
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
            # A one-page memo with one section was rendered as three pages:
            # body, a tail of the body, then a references page - because the
            # list always started with a PageBreak. Reserve a fresh page only
            # for a genuinely multi-section report; otherwise the list follows
            # the text it supports.
            _h1 = sum(1 for b in blocks
                      if isinstance(b, dict) and b.get("type") == "heading"
                      and int(b.get("level") or 1) == 1)
            if _h1 >= 3:
                story.append(PageBreak())
            else:
                story.append(Spacer(1, LEAD * 1.1))
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
                # PDF bookmarks, so the document is navigable in the reader's
                # sidebar. An 18-page report with a printed contents page but
                # no outline tree is half-navigable: the reader shows nothing
                # to click. Wrapped because a bookmark failure must never
                # cost the user their document.
                try:
                    _key = f"h{self.page}-{level}-{len(getattr(self, '_outline_n', []))}"
                    self._outline_n = getattr(self, "_outline_n", [])
                    self._outline_n.append((_key, flowable.getPlainText(), level))
                    self.canv.bookmarkPage(_key)
                    self.canv.addOutlineEntry(
                        flowable.getPlainText(), _key, level=0,
                        closed=(level == 1))
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


# ── inline math ──────────────────────────────────────────────────────────────
# reportlab has no maths typesetter, and `_rich` translated only bold, code and
# links - so a technical document reached the reader with the markup still on
# it. Measured in a live 23-page LLM report: 112 literal `$` characters over 9
# pages, including "($Q$)", "$d_k$" and "$\sqrt{d_k}$".
#
# The conversion is deliberately narrow: only text inside `$...$`, `\(...\)` or
# `\[...\]` is touched, so ordinary prose (and pricing: "$5 per seat and $6 per
# user") is left exactly as written.
_MATH_DOLLARDOLLAR_R = re.compile(r"\$\$(.+?)\$\$", re.S)
_MATH_DOLLAR_R = re.compile(r"\$([^$\n]{1,160})\$")
_MATH_PAREN_R = re.compile(r"\\\((.{1,200}?)\\\)")
_MATH_BRACKET_R = re.compile(r"\\[\[](.{1,200}?)\\[\]]", re.S)
_MATH_CMD_AT_R = re.compile(r"\\([a-zA-Z]+)")
_MATH_SUP_R = re.compile(r"\^\{([^{}]*)\}|\^([A-Za-z0-9+-])")
_MATH_SUB_R = re.compile(r"_\{([^{}]*)\}|_([A-Za-z0-9+-])")
# Sizing/bracing words that carry no meaning once the maths is plain text.
_MATH_SIZE_NAMES = {"left", "right", "big", "Big", "bigg", "Bigg",
                    "bigl", "bigr", "Bigl", "Bigr", "middle",
                    "biggl", "biggr", "Biggm"}
# Commands whose braces ARE the content.
_MATH_WRAP_NAMES = {"mathcal", "mathbf", "boldsymbol", "mathit", "mathrm",
                    "mathsf", "mathtt", "text", "textrm", "textbf", "textit",
                    "textsf", "operatorname", "mathbb", "underline", "hat",
                    "widehat", "tilde", "overline", "bar", "vec", "dot",
                    "ddot", "stackrel", "overset", "underset", "mbox"}

# The renderer registers no TrueType fonts: every page is drawn with base-14
# Helvetica/Times, whose repertoire is cp1252. Anything outside it does not
# render as a box or a question mark - it renders as the WRONG LETTER, which is
# how `d_k` came out of a test build as "dI". So this table maps to cp1252 where
# cp1252 has the character (`×`, `±`, `²`, `·`) and to plain ASCII where it does
# not (`sqrt(`, `<=`, `->`, `alpha`).
_MATH_SYMBOLS = {
    "alpha": "alpha", "beta": "beta", "gamma": "gamma", "delta": "delta",
    "epsilon": "epsilon", "varepsilon": "epsilon", "zeta": "zeta",
    "eta": "eta", "theta": "theta", "iota": "iota", "kappa": "kappa",
    "lambda": "lambda", "mu": "µ", "nu": "nu", "xi": "xi", "pi": "pi",
    "rho": "rho", "sigma": "sigma", "tau": "tau", "upsilon": "upsilon",
    "phi": "phi", "varphi": "phi", "chi": "chi", "psi": "psi", "omega": "omega",
    "Gamma": "Gamma", "Delta": "Delta", "Theta": "Theta", "Lambda": "Lambda",
    "Xi": "Xi", "Pi": "Pi", "Sigma": "Sigma", "Upsilon": "Upsilon",
    "Phi": "Phi", "Psi": "Psi", "Omega": "Omega",
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "-/+",
    "le": "<=", "leq": "<=", "ge": ">=", "geq": ">=", "ne": "!=", "neq": "!=",
    "approx": "~=", "equiv": "==", "propto": "prop", "ll": "<<", "gg": ">>",
    "in": "in", "notin": "not in", "subset": "subset", "subseteq": "<=",
    "supset": "supset", "cup": "union", "cap": "intersect", "setminus": "minus",
    "to": "->", "rightarrow": "->", "longrightarrow": "->",
    "Rightarrow": "=>", "leftarrow": "<-", "leftrightarrow": "<->",
    "mapsto": "|->", "implies": "=>", "iff": "<=>",
    "uparrow": "^", "downarrow": "v",
    "infty": "infinity", "partial": "d", "nabla": "nabla", "sum": "sum",
    "prod": "prod", "int": "int", "oint": "oint", "sqrt": "sqrt",
    "degree": "°", "hbar": "h", "ell": "l", "aleph": "aleph",
    "quad": " ", "qquad": "  ", "ldots": "...", "cdots": "...", "dots": "...",
    "langle": "<", "rangle": ">", "lVert": "||", "rVert": "||",
    "lvert": "|", "rvert": "|", "mid": "|", "forall": "for all",
    "exists": "exists", "wedge": "and", "vee": "or", "star": "*",
    "prime": "'", "circ": "o", "bullet": "·", "pmod": "mod",
    # Operators and function names: `\log` is not a typo for "log", it is the
    # reader's only clue, and leaving the backslash is what leaked on a live
    # document's page 15.
    "max": "max", "min": "min", "sup": "sup", "inf": "inf", "lim": "lim",
    "log": "log", "ln": "ln", "exp": "exp", "sin": "sin", "cos": "cos",
    "tan": "tan", "sinh": "sinh", "cosh": "cosh", "tanh": "tanh",
    "arcsin": "arcsin", "arccos": "arccos", "softmax": "softmax",
    "argmax": "argmax", "argmin": "argmin", "det": "det", "tr": "tr",
    "diag": "diag", "gcd": "gcd", "mod": "mod",
    "sim": "~", "simeq": "~", "parallel": "||",
    "oplus": "(+)", "ominus": "(-)", "otimes": "(x)", "oslash": "(/)",
    "bigcup": "union", "bigcap": "intersect", "bigoplus": "(+)",
    "bigotimes": "(x)", "bigsqcup": "sqcup",
    "complement": "complement",
}
# Only these raised forms exist in cp1252; everything else keeps the caret.
_SUP = {"1": "¹", "2": "²", "3": "³"}
# cp1252 has no subscripts at all, so `_attach` always falls back to `_x`.
_SUB: dict = {}

# The same hazard applies to characters the model writes directly rather than
# as LaTeX. A base-14 font does not draw a box for an out-of-repertoire
# character, it draws whatever glyph sits at that code point - so `α` can
# arrive as `a` with a mark, or as an unrelated letter. Anything listed here is
# rewritten before drawing; anything not listed is left as written, because
# deleting reader-visible text to fix a font problem is the worse trade.
_FONT_SAFE = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon",
    "θ": "theta", "λ": "lambda", "π": "pi", "ρ": "rho", "σ": "sigma",
    "τ": "tau", "φ": "phi", "ω": "omega", "μ": "u", "η": "eta", "ξ": "xi",
    "κ": "kappa", "ν": "nu", "χ": "chi", "ψ": "psi", "ζ": "zeta",
    "Γ": "Gamma", "Δ": "Delta", "Θ": "Theta", "Λ": "Lambda", "Σ": "Sigma",
    "Π": "Pi", "Φ": "Phi", "Ψ": "Psi", "Ω": "Omega",
    "≤": "<=", "≥": ">=", "≠": "!=", "≈": "~=", "≡": "==", "∝": "prop",
    "→": "->", "←": "<-", "↔": "<->", "⇒": "=>", "↦": "|->",
    "∞": "infinity", "√": "sqrt", "∑": "sum", "∏": "prod", "∫": "int",
    "∂": "d", "∇": "nabla", "∈": "in", "∉": "not in", "⊂": "subset",
    "⊆": "<=", "∪": "union", "∩": "intersect", "∀": "for all", "∃": "exists",
    "∅": "empty", "⋯": "...", "⟹": "=>", "⟺": "<=>",
    "✓": "yes", "✔": "yes", "✗": "no", "✘": "no",
    "─": "-", "━": "-", "│": "|", "┃": "|",
    # cp1252 carries only ¹ ² ³. The remaining superscript digits and the
    # superscript sign are NOT in it, and an unmapped glyph drew as a solid
    # block - `W/m²·K⁴` reached the page as "W/m²·K■". Fall back to caret
    # notation, which is unambiguous and always drawable.
    "⁰": "^0", "¹": "^1", "²": "^2", "³": "^3", "⁴": "^4", "⁵": "^5",
    "⁶": "^6", "⁷": "^7", "⁸": "^8", "⁹": "^9", "⁻": "^-", "⁺": "^+",
    "₀": "_0", "₁": "_1", "₂": "_2", "₃": "_3", "₄": "_4", "₅": "_5",
    "₆": "_6", "₇": "_7", "₈": "_8", "₉": "_9",
}


def _font_safe(s: str) -> str:
    """Rewrite characters the built-in fonts cannot draw, before escaping."""
    out = []
    for ch in str(s or ""):
        if ord(ch) < 128:
            out.append(ch)
            continue
        try:
            ch.encode("cp1252")
            out.append(ch)
            continue
        except UnicodeEncodeError:
            out.append(_FONT_SAFE.get(ch, ch))
    return "".join(out)


def _attach(body: str, table: dict, marker: str) -> str:
    """Raise or lower a run, or fall back to caret/underscore notation.

    cp1252 provides only `¹ ² ³` and no subscripts at all, so most runs cannot
    be set. Printing a partial conversion would silently change the meaning, so
    those keep an explicit `^(...)` / `_(...)`.
    """
    if not body:
        return marker
    mapped = "".join(table.get(c, "") for c in body)
    if len(mapped) == len(body):
        return mapped
    return f"{marker}({body})" if len(body) > 1 else f"{marker}{body}"


def _take_braces(s: str, i: int) -> tuple[list[str], int]:
    """Read consecutive balanced `{...}` groups starting at index `i`."""
    args: list[str] = []
    while i < len(s) and s[i] == "{":
        depth, j = 1, i + 1
        while j < len(s) and depth:
            if s[j] == "{":
                depth += 1
            elif s[j] == "}":
                depth -= 1
            j += 1
        if depth:
            break
        args.append(s[i + 1:j - 1])
        i = j
    return args, i


def _bare_math(s: str) -> str:
    r"""Rewrite `\command{...}` wherever it appears, braces nested or not.

    The delimited pass above cannot see a display equation the model wrote
    with no `$` around it, which is what a live document did:
    `L(N, D) = \left( \frac{N_c}{N} \right)^{\alpha_N} + ...` reached the page
    unchanged. This works on any text, so it only ever rewrites a backslash
    followed by a real command name - `C:\Users` and an apostrophe-`n` are
    untouched, because neither is a known command.
    """
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        m = _MATH_CMD_AT_R.match(s, i)
        if not m:
            out.append(s[i])
            i += 1
            continue
        name = m.group(1)
        args, j = _take_braces(s, m.end())
        if name in _MATH_SIZE_NAMES:
            # `\left(` / `\right]`: the sizing word goes, and the delimiter
            # that follows it is ordinary text.
            i = m.end()
            continue
        if name in ("frac", "dfrac", "tfrac", "binom") and len(args) >= 2:
            a, b = _bare_math(args[0]), _bare_math(args[1])
            out.append(f"({a} choose {b})" if name == "binom" else f"({a})/({b})")
            i = j
            continue
        if name == "sqrt" and args:
            out.append(f"sqrt({_bare_math(args[0])})")
            i = j
            continue
        if name in _MATH_WRAP_NAMES and args:
            out.append(_bare_math(args[0]))
            i = j
            continue
        if name in _MATH_SYMBOLS:
            out.append(_MATH_SYMBOLS[name])
            i = m.end()
            continue
        if not args:
            # An unknown `\word` with no arguments is not ours to guess; leave
            # it exactly as written.
            out.append(s[i:m.end()])
            i = m.end()
            continue
        out.append(_bare_math(" ".join(args)))
        i = j
    return "".join(out)


def _sup_sub(s: str) -> str:
    """`x^2` → x², `d_k` stays `d_k`. Applied to plain text as well as maths."""
    s = _MATH_SUP_R.sub(
        lambda m: _attach(m.group(1) if m.group(1) is not None else m.group(2),
                          _SUP, "^"), s)
    s = _MATH_SUB_R.sub(
        lambda m: _attach(m.group(1) if m.group(1) is not None else m.group(2),
                          _SUB, "_"), s)
    return s


def _math_body(s: str) -> str:
    out = _sup_sub(_bare_math(s))
    out = re.sub(r"\\[,;!>: ]", " ", out)
    out = out.replace("\\", "").replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", out).strip()


def _dollar_span(m) -> str:
    r"""Decide whether a `$...$` span is maths or money.

    TeX's own rule is that no space may sit next to the delimiter, which
    rejects "costs $5 per seat and $6 per hour" (the span between the two
    dollar signs is a phrase, not an expression). A span that begins with a
    digit and contains a space is treated as currency unless it also holds a
    `\command`, because `$1, \dots, V$` really is maths.
    """
    body = m.group(1)
    whole = m.group(0)
    if not body or body[0].isspace() or body[-1].isspace():
        return whole
    if body[0].isdigit() and " " in body and not _MATH_CMD_AT_R.search(body):
        return whole
    if re.fullmatch(r"[\d.,%+\-–—$€£¥\s]+", body):
        return whole
    return _math_body(body)


def _math_to_text(s: str) -> str:
    """Render LaTeX math as readable text.

    Delimited maths (`$$…$$`, `$…$`, `\\(...\\)`, `\\[…\\]`) is converted in
    full, including the braces and backslashes left inside it. Text outside a
    delimiter is only touched where a real `\\command` appears, so prose, file
    paths and prices are left exactly as written.
    """
    out = _MATH_DOLLARDOLLAR_R.sub(lambda m: _math_body(m.group(1)), s)
    out = _MATH_DOLLAR_R.sub(_dollar_span, out)
    out = _MATH_PAREN_R.sub(lambda m: _math_body(m.group(1)), out)
    out = _MATH_BRACKET_R.sub(lambda m: _math_body(m.group(1)), out)
    return _sup_sub(_bare_math(out))


def _rich(s: str) -> str:
    r"""Escape for reportlab, then translate inline markdown to its markup.

    Font substitution and math conversion run BEFORE escaping on purpose: they
    emit `<`, `>` and `&` (`\le` becomes `<=`), and text produced after `_esc`
    would reach reportlab's XML parser unescaped.
    """
    out = _font_safe(s)
    out = _math_to_text(out)
    out = _ATX_R.sub("", out)
    out = _esc(out)
    out = _MD_LINK_R.sub(r"\1 (\2)", out)
    out = _MD_CODE_R.sub(r'<font name="Courier">\1</font>', out)
    out = _MD_BOLD_R.sub(r"<b>\1</b>", out)
    out = _MD_ITAL_R.sub(r"<i>\1</i>", out)
    out = _scripts(out)
    # Any surviving emphasis markers were unbalanced; drop them rather than
    # print them.
    return re.sub(r"\*\*?", "", out)


# ── PowerPoint ───────────────────────────────────────────────────────────────

def _pptx_axis_max(series):
    """A value axis that stops where the data does.

    PowerPoint's automatic maximum gave a 0-1 recall chart an axis to 1.2, so
    a fifth of the plot was empty white and the bars looked short."""
    vals = [v for s in series for v in s["data"]]
    if not vals:
        return None
    hi, lo = max(vals), min(vals)
    if hi <= 0:
        return None
    if all(0 <= v <= 1 for v in vals):
        return 1.0
    for mag in (1, 10, 100, 1000, 10000, 100000, 1000000):
        for step in (1, 2, 2.5, 5):
            cap = mag * step
            if hi <= cap and hi / cap >= 0.62:
                return float(cap)
    return float(hi * 1.08)


def _pptx_chart(slide, b: dict, left, top, width, advance, deck=None) -> None:
    """A native, editable PowerPoint chart. Rendering the data as a picture
    would have been far less code and produced a deck nobody could update."""
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION, XL_LABEL_POSITION
    from pptx.util import Emu, Pt

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
    pal = (deck.series if deck is not None else
           [_pptx_rgb("5b4bd6"), _pptx_rgb("2E6FDB"), _pptx_rgb("C0392B")])
    ink = deck.ink if deck is not None else _pptx_rgb("14141A")
    muted = deck.muted if deck is not None else _pptx_rgb("5C5C66")
    grid = deck.soft if deck is not None else _pptx_rgb("DDDDDD")
    try:
        if b.get("title"):
            ch.has_title = True
            ch.chart_title.text_frame.text = _strip_md(b["title"])
            for para in ch.chart_title.text_frame.paragraphs:
                for r in para.runs:
                    r.font.size = Pt(16)
                    r.font.bold = True
                    r.font.color.rgb = ink
        else:
            ch.has_title = False
        # A single-series chart names itself with its title and its data
        # labels; a legend repeating that one name is noise (it was the loudest
        # amateur tell on every bar chart). A legend earns its space only when
        # there are two or more series to tell apart.
        ch.has_legend = len(b["series"]) > 1
        if ch.has_legend:
            ch.legend.position = XL_LEGEND_POSITION.BOTTOM
            ch.legend.include_in_layout = False
            try:
                ch.legend.font.size = Pt(12)
                ch.legend.font.color.rgb = muted
            except Exception:
                pass
    except Exception:
        pass

    try:
        # A chart dropped on tinted paper must not carry its own white card:
        # the stock white plot area reads as a pasted screenshot next to the
        # rest of the deck. Make the chart and plot canvases transparent so
        # the slide's paper shows through, and drop Office's frame around it.
        ch.chart_area.format.fill.background()
        ch.chart_area.format.line.fill.background()
        ch.plots[0].format.fill.background()
    except Exception:
        pass

    # Series colours: the deck's own palette. Leaving them at Office's default
    # red-and-blue was the loudest possible sign that nothing here was designed.
    try:
        plot = ch.plots[0]
        for i, ser in enumerate(plot.series):
            col = pal[i % len(pal)]
            try:
                ser.format.fill.solid()
                ser.format.fill.fore_color.rgb = col
            except Exception:
                pass
            if kind == "line":
                try:
                    ser.format.line.color.rgb = col
                    ser.format.line.width = Pt(2.5)
                except Exception:
                    pass
        if kind == "pie":
            try:
                for i, pt in enumerate(plot.series[0].points):
                    pt.format.fill.solid()
                    pt.format.fill.fore_color.rgb = pal[i % len(pal)]
            except Exception:
                pass
        if kind == "bar":
            try:
                plot.gap_width = 55
                plot.overlap = -12
            except Exception:
                pass
        if len(b["series"]) == 1 and kind in ("bar", "pie"):
            try:
                plot.has_data_labels = True
                dl = plot.data_labels
                dl.font.size = Pt(12)
                dl.font.color.rgb = ink
                if kind == "pie":
                    dl.position = XL_LABEL_POSITION.OUTSIDE_END
                    dl.show_percentage = True
            except Exception:
                pass
        elif kind == "bar":
            # A clustered bar with no numbers on it makes the reader estimate
            # every value from a gridline. With few enough points, print them.
            points = sum(len(s["data"]) for s in b["series"])
            if points <= 12:
                try:
                    plot.has_data_labels = True
                    dl = plot.data_labels
                    dl.font.size = Pt(10)
                    dl.font.color.rgb = ink
                    dl.show_value = True
                except Exception:
                    pass
    except Exception:
        pass

    if kind != "pie":
        try:
            va = ch.value_axis
            va.has_major_gridlines = True
            va.major_gridlines.format.line.color.rgb = grid
            va.major_gridlines.format.line.width = Pt(0.75)
            va.tick_labels.font.size = Pt(12)
            va.tick_labels.font.color.rgb = muted
            try:
                va.format.line.fill.background()
            except Exception:
                pass
            cap = _pptx_axis_max(b["series"])
            if cap is not None:
                va.maximum_scale = cap
                if min(v for s in b["series"] for v in s["data"]) >= 0:
                    va.minimum_scale = 0.0
        except Exception:
            pass
        try:
            ca = ch.category_axis
            ca.tick_labels.font.size = Pt(12)
            ca.tick_labels.font.color.rgb = ink
            ca.format.line.color.rgb = grid
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
        # A tall screenshot pinned to the left margin leaves the right half of
        # the slide empty; a figure sits in the middle of its column.
        left = int(left) + int((int(width) - w) / 2)
        # A stream, not a temp file: python-pptx reads the picture lazily too.
        slide.shapes.add_picture(io.BytesIO(b["data"]), left, top,
                                  width=w, height=h)
    except Exception:
        return


# ── PowerPoint: a layout engine, not a text placer ───────────────────────────
#
# `build_pptx` used to drop every block into a fixed-height text box on the
# Blank layout: a heading always cost 0.8in whatever it said, body type was
# 15pt on a 13.3x7.5in canvas, `**bold**` was stripped, bullets were literal
# "• " characters inside one text frame, and nothing carried a colour. Measured
# on a live deck: 4 slides, 19 text boxes, 0 images/tables/charts, 0 font
# colours, sizes 40/26/15 only.
#
# It also broke the contract in prompts/deck.md, which tells the model that a
# level-1 heading starts a new slide. It did not — six sections packed into
# three slides, which is most of what "the slides are basic" was.
#
# Now: a level-1 heading always opens a slide, every block is advanced by its
# MEASURED height, type is scaled for a projected canvas, titles are real
# placeholders (so the theme and PowerPoint's Outline view see them), emphasis
# survives as runs, bullets are real list paragraphs, and the accent system the
# PDF already had is shared.

_PPTX_IN = 914400


def _pptx_rgb(hex_str):
    from pptx.dml.color import RGBColor
    s = re.sub(r"[^0-9a-f]", "", str(hex_str or "").lower())
    if len(s) != 6:
        s = "5b4bd6"
    return RGBColor(int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def _pptx_mix(rgb, toward, factor):
    """Blend two colours — how a deck gets tints of ITS accent rather than a
    second, unrelated palette."""
    from pptx.dml.color import RGBColor
    f = max(0.0, min(1.0, float(factor)))
    a, b = list(rgb), list(toward)
    return RGBColor(*[int(round(a[i] + (b[i] - a[i]) * f)) for i in range(3)])


def _pptx_hue_shift(rgb, deg, dl=0.0):
    """Rotate a colour's hue — chart series need to be tellable apart, and
    six tints of one accent are not."""
    import colorsys
    from pptx.dml.color import RGBColor
    h, l, s = colorsys.rgb_to_hls(*[c / 255 for c in rgb])
    h = (h + float(deg) / 360.0) % 1.0
    l = max(0.18, min(0.78, l + float(dl)))
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return RGBColor(int(r * 255), int(g * 255), int(b * 255))


def _pptx_series(accent):
    """A deterministic, harmonious series palette built from the accent."""
    return [accent,
            _pptx_hue_shift(accent, 34, 0.06),
            _pptx_hue_shift(accent, -34, -0.06),
            _pptx_hue_shift(accent, 150, 0.02),
            _pptx_hue_shift(accent, 68, 0.14),
            _pptx_hue_shift(accent, -150, -0.04)]


def _pptx_in(v) -> int:
    return int(float(v) * _PPTX_IN)


_PPTX_NUMBER_R = re.compile(
    r"[-+~<>=]?\s*\$?\d[\d,.]*\s*(%|x|×|k|K|M|G|B|ms|s|min|h|GB|MB|to|nm)?\b")


_MD_TOKEN_R = re.compile(
    r"(\*\*.+?\*\*|`[^`\n]+`|(?<![\w*])\*[^*\n]+?\*(?![\w*]))", re.S)


def _md_tokens(s: str):
    """Split markdown-lite text into (kind, text) pieces for run formatting."""
    out = []
    for part in _MD_TOKEN_R.split(str(s or "")):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            out.append(("b", part[2:-2]))
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            out.append(("code", part[1:-1]))
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            out.append(("i", part[1:-1]))
        else:
            out.append(("", part))
    return out


def _pptx_text_height(text, width_emu, size_pt, leading_mult: float = 1.22,
                      pad_pt: float = 4.0, em: float = 0.50,
                      line_em: float = 1.22) -> int:
    """Height of `text` laid out in a `width_emu` column, in EMU.

    Words are wrapped the way a renderer does it - greedily, at spaces - not by
    dividing characters by a fixed count. The old char-count model was wrong
    exactly where it mattered: "Grid-Scale Battery Storage: Engineering Outlook
    2026" measured two lines because 52 chars fit 2 x 26, but Georgia Bold
    wraps it to THREE, and the accent rule under the title was then drawn
    straight through the last one. Word widths come from `em`, the average
    advance per character for the face being measured.

    `line_em` is the FONT's own line height (ascent + descent), which is not
    the same number as the paragraph's spacing multiplier. Georgia measures
    1.14em and Segoe UI 1.34em, so a two-line value in a stat card is ~2.3x
    its point size tall, not 2.0x - treating them as equal is what let
    "$210-430/kWh" print over its own label.
    """
    w_in = max(1.0, width_emu / _PPTX_IN)
    limit = w_in * 72.0                      # usable points on one line
    cw = max(0.05, float(em)) * float(size_pt)   # one character, in points
    lines = 0
    for raw in str(text or "").split("\n"):
        words = raw.split()
        if not words:
            lines += 1
            continue
        n = 1
        cur = 0.0
        for w in words:
            ww = len(w) * cw
            if ww > limit:
                # A single token wider than the column breaks mid-word, which
                # is what a renderer does with "$210-430/kWh" in a quarter-
                # width card. Counting it as one line put its second line
                # straight through the label below.
                if cur:
                    n += 1
                n += int(ww // limit)
                cur = ww % limit
                continue
            if cur and cur + cw * 1.0 + ww > limit:   # space + word overflows
                n += 1
                cur = ww
            else:
                cur += (cw if cur else 0.0) + ww
        lines += n
    # Each line occupies the font's line height scaled by the spacing
    # multiplier; the last line still needs its own glyph box, which is why the
    # line height is multiplied rather than added once.
    per_line = size_pt * max(float(leading_mult), float(line_em))
    return _pptx_in((lines * per_line + pad_pt) / 72.0)


def _pptx_theme(prs, deck) -> None:
    """Rewrite the stock Office theme to the deck's own palette and type.

    Every run is already coloured and fonted explicitly, but placeholders,
    speaker notes, and PowerPoint's own colour/font pickers INHERIT the theme.
    A deck that looks right on the slide yet carries Calibri/4F81BD underneath
    still edits like a stock template — the accent you pick is not the accent
    the chart editor or the text highlighter offers. This is what makes the
    whole file cohere around one palette instead of the accent plus whatever
    Office happened to ship.
    """
    from lxml import etree
    ns = "http://schemas.openxmlformats.org/drawingml/2006/main"

    def q(t):
        return f"{{{ns}}}{t}"

    def hexs(rgb):
        return "%02X%02X%02X" % tuple(rgb)

    try:
        master = prs.slide_masters[0]
    except Exception:
        return
    part = None
    for rel in master.part.rels.values():
        if rel.reltype.endswith("/theme"):
            part = rel.target_part
            break
    if part is None:
        return
    try:
        root = etree.fromstring(part.blob)
    except Exception:
        return
    cs = root.find(".//" + q("clrScheme"))
    if cs is not None:
        cs.set("name", "Aria")
        series = deck.series
        white = _pptx_rgb("FFFFFF")
        mapping = {
            "dk1": deck.ink, "lt1": white,
            "dk2": deck.dark, "lt2": deck.tint,
            "accent1": deck.accent,
            "accent2": series[1], "accent3": series[2],
            "accent4": series[3], "accent5": series[4],
            "accent6": series[5],
            "hlink": deck.accent, "folHlink": deck.dark,
        }
        for tag, rgb in mapping.items():
            el = cs.find(q(tag))
            if el is None:
                continue
            for ch in list(el):
                el.remove(ch)
            etree.SubElement(el, q("srgbClr"), {"val": hexs(rgb)})
    fs = root.find(".//" + q("fontScheme"))
    if fs is not None:
        fs.set("name", "Aria")
        for tag, face in (("majorFont", deck.head_font),
                          ("minorFont", deck.body_font)):
            f = fs.find(q(tag))
            if f is None:
                continue
            latin = f.find(q("latin"))
            if latin is not None:
                latin.set("typeface", face)
    try:
        part._blob = etree.tostring(root, xml_declaration=True,
                                    encoding="UTF-8", standalone=True)
    except Exception:
        pass


class _Deck:
    """Slide-by-slide placement with the cursor in measured units."""

    BODY = 18
    MIN_BODY = 14
    MAX_BODY = 22
    HEAD = 30
    TITLE = 40

    def __init__(self, prs, style: dict, running_title: str = ""):
        from pptx.dml.color import RGBColor
        self.prs = prs
        self.W = int(prs.slide_width)
        self.H = int(prs.slide_height)
        self.mx = _pptx_in(0.8)
        self.cw = self.W - 2 * self.mx
        self.accent = _pptx_rgb(style.get("accent"))
        self.ink = RGBColor(0x14, 0x14, 0x1A)
        self.muted = RGBColor(0x5C, 0x5C, 0x66)
        white, black = RGBColor(255, 255, 255), RGBColor(0, 0, 0)
        # Everything else is derived, so a deck carries one palette instead of
        # the accent plus whatever theme colours PowerPoint happens to own.
        self.tint = _pptx_mix(self.accent, white, 0.90)
        self.soft = _pptx_mix(self.accent, white, 0.72)
        self.dark = _pptx_mix(self.accent, black, 0.30)
        self.series = _pptx_series(self.accent)
        # Paper is a whisper of the accent so content slides are not stark
        # white; pale is text/lines that sit ON the accent (section dividers).
        self.paper = _pptx_mix(self.accent, white, 0.93)
        self.pale = _pptx_mix(self.accent, white, 0.80)
        self.band = _pptx_in(0.09)
        self.rule_h = _pptx_in(0.045)
        # A deck in Calibri Light is a deck in the default font: the type pairing
        # is the first thing a room notices. Segoe UI ships with Windows and
        # Office, and its Semibold cut carries a slide title the way Calibri
        # Light never does.
        fam = str(style.get("family") or "sans").lower()
        if fam == "serif":
            self.head_font, self.body_font = "Georgia", "Georgia"
            # Georgia is a WIDE face: measured against the installed font, its
            # bold averages 0.536em per character and its regular 0.50em. The
            # serif branch used to inherit the sans defaults, so every serif
            # title was measured ~8% too narrow and wrapped one line more than
            # the box allowed - the accent rule then struck through it.
            self.em = 0.52
            self.head_em = 0.56
            # Georgia: ascent 0.92 + descent 0.22 = 1.14em
            self.line_em = 1.14
        else:
            self.head_font = "Segoe UI Semibold"
            self.body_font = "Segoe UI"
            self.em = 0.53
            # A semibold head is ~9% wider per character than the regular body
            # this estimate was calibrated on. Measuring titles with the body
            # factor under-counted their lines: a cover title estimated at two
            # lines wrapped to three and the accent rule was drawn straight
            # through the last one.
            self.head_em = 0.58
            # Segoe UI: ascent 1.08 + descent 0.26 = 1.34em
            self.line_em = 1.34
        self.card = False
        self.footer_h = _pptx_in(0.40)
        self.bottom = self.H - self.footer_h - _pptx_in(0.26)
        self.running = running_title or ""
        self.count = 0
        self.cur = None
        self.top = 0
        self.wrote = False
        self.spread = 0
        # slack a single card block may absorb (see balance/card_only)
        self.grow = 0

    # ── layouts ───────────────────────────────────────────────────────────
    def _layout(self, name: str, fallback: int):
        """A layout by NAME, so the placeholders exist at all.

        The old code used `slide_layouts[6]` — Blank — for every slide, which is
        why nothing themed, nothing autofit, and the Outline view was empty.
        """
        try:
            for L in self.prs.slide_layouts:
                if str(L.name).strip().lower() == name.lower():
                    return L
        except Exception:
            pass
        try:
            return self.prs.slide_layouts[fallback]
        except Exception:
            return self.prs.slide_layouts[6]

    def add(self, layout_name: str, fallback: int, band=None, bg=None):
        s = self.prs.slides.add_slide(self._layout(layout_name, fallback))
        self.count += 1
        self.cur = s
        self.top = _pptx_in(0.5)
        self.wrote = False
        self.spread = 0
        self.grow = 0
        # A deck on stark white reads as a converted document. Every slide
        # carries a whisper of the accent as paper; the cover and the section
        # dividers pass their own background to override it.
        try:
            s.background.fill.solid()
            s.background.fill.fore_color.rgb = self.paper if bg is None else bg
        except Exception:
            pass
        self.edge = self.band if band is None else int(band)
        if self.edge:
            # One accent edge down every slide: the deck then reads as one
            # object instead of six white pages that happen to share a font.
            from pptx.enum.shapes import MSO_SHAPE
            strip = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, self.edge,
                                       self.H)
            try:
                strip.fill.solid()
                strip.fill.fore_color.rgb = self.accent
                strip.line.fill.background()
                strip.shadow.inherit = False
            except Exception:
                pass
        return s

    # ── primitives ────────────────────────────────────────────────────────
    def _box(self, left, top, width, height):
        tb = self.cur.shapes.add_textbox(left, top, width, height)
        tb.text_frame.word_wrap = True
        # PowerPoint insets a text frame 0.1in per side by default, so text
        # wraps ~0.2in narrower than the box. The measurement used the box
        # width, which is how a title estimated at two lines rendered three.
        tb.text_frame.margin_left = 0
        tb.text_frame.margin_right = 0
        return tb

    def _write(self, para, text, size, color=None, bold=False, italic=False,
               align=None, line_spacing=None, space_after=None, font=None):
        """One paragraph, with markdown emphasis kept as real runs."""
        from pptx.util import Pt
        first = True
        for kind, chunk in _md_tokens(_math_to_text(text)):
            # Reuse the paragraph's existing run when there is one (a fresh
            # placeholder already has an empty run; a textbox does not).
            r = para.runs[0] if (first and len(para.runs)) else para.add_run()
            first = False
            r.text = chunk
            f = r.font
            f.size = Pt(size)
            f.bold = bool(bold or kind == "b")
            f.italic = bool(italic or kind == "i")
            f.name = font or self.body_font
            if kind == "code":
                f.name = "Consolas"
            if color is not None:
                f.color.rgb = color
        if first:                      # empty text: still make a run-less para
            r = para.add_run()
            r.text = ""
            r.font.size = Pt(size)
        # The Office layouts centre their title placeholder and lead their body
        # by a theme default. Inheriting that put a centred title over a
        # left-aligned rule and a left-aligned list, which is what made every
        # slide look like an accident. Say what we mean.
        if align is not None:
            para.alignment = align
        if line_spacing:
            para.line_spacing = line_spacing
        if space_after is not None:
            para.space_after = Pt(space_after)

    def rule(self, left, top, width, color=None, thick=None):
        from pptx.enum.shapes import MSO_SHAPE
        sh = self.cur.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width,
                                       thick or self.rule_h)
        try:
            sh.fill.solid()
            sh.fill.fore_color.rgb = color or self.accent
            sh.line.fill.background()
            sh.shadow.inherit = False
        except Exception:
            pass
        return sh

    def footer(self, light=False):
        if self.count <= 1 or not self.running:
            return
        from pptx.util import Pt
        txt = self.pale if light else self.muted
        self.rule(self.mx, self.H - self.footer_h - _pptx_in(0.20),
                  self.cw - _pptx_in(0.8), thick=_pptx_in(0.018),
                  color=self.soft)
        tb = self._box(self.mx, self.H - self.footer_h - _pptx_in(0.06),
                       self.cw - _pptx_in(0.8), self.footer_h)
        self._write(tb.text_frame.paragraphs[0], self.running, 9,
                    color=txt)
        n = self._box(self.W - self.mx - _pptx_in(0.8),
                      self.H - self.footer_h - _pptx_in(0.06), _pptx_in(0.8),
                      self.footer_h)
        p = n.text_frame.paragraphs[0]
        try:
            from pptx.enum.text import PP_ALIGN
            p.alignment = PP_ALIGN.RIGHT
        except Exception:
            pass
        self._write(p, str(self.count), 9, color=txt)
        del Pt

    # ── slides ────────────────────────────────────────────────────────────
    def title_slide(self, title, subtitle=""):
        from pptx.enum.text import PP_ALIGN
        from pptx.enum.shapes import MSO_SHAPE
        s = self.add("Title Slide", 0, band=0)
        # A cover is a composition, not a page with a title on it. A solid
        # accent panel holds the right third and the text sits in what is left
        # of it, so the deck reads as designed before a word is legible.
        panel = int(self.W * 0.30)
        face = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, self.W - panel, 0,
                                  panel, self.H)
        try:
            # A gradient panel reads as designed; a flat rectangle reads as a
            # colour swatch. The accent at the top settles into its own dark
            # at the foot, so the cover has depth before a word is legible.
            face.fill.gradient()
            stops = face.fill.gradient_stops
            stops[0].color.rgb = self.accent
            stops[1].color.rgb = self.dark
            face.fill.gradient_angle = 90.0
            face.line.fill.background()
            face.shadow.inherit = False
        except Exception:
            try:
                face.fill.solid()
                face.fill.fore_color.rgb = self.accent
                face.line.fill.background()
                face.shadow.inherit = False
            except Exception:
                pass
        edge = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, self.W - panel, 0,
                                  _pptx_in(0.14), self.H)
        try:
            edge.fill.solid()
            edge.fill.fore_color.rgb = self.dark
            edge.line.fill.background()
            edge.shadow.inherit = False
        except Exception:
            pass
        left = self.mx
        width = self.W - panel - left - _pptx_in(0.7)
        top = int(self.H * 0.32)
        # A cover that wraps to four lines is a layout failure, so the type
        # steps down until the title fits two.
        size = self.TITLE
        while (size > 26 and self._m(title, width, size, 1.05,
                                               pad_pt=2.0,
                                               em=self.head_em) > _pptx_in(1.45)):
            size -= 2
        h = self._m(title, width, size, 1.05, pad_pt=2.0, em=self.head_em)
        ph = self._title_placeholder()
        if ph is not None:
            ph.left, ph.top = left, top
            ph.width, ph.height = width, h
            self._write(ph.text_frame.paragraphs[0], title, size,
                        color=self.ink, bold=True, align=PP_ALIGN.LEFT,
                        line_spacing=1.05, font=self.head_font)
        else:
            self._write(self._box(left, top, width, h).text_frame.paragraphs[0],
                        title, size, color=self.ink, bold=True,
                        align=PP_ALIGN.LEFT, line_spacing=1.05,
                        font=self.head_font)
        self.wrote = True
        # The rule belongs to the title it sits under: same left edge, so the
        # two read as one block instead of a stray bar in the margin.
        self.rule(left, top + h + _pptx_in(0.16), _pptx_in(2.2),
                  thick=_pptx_in(0.075))
        if subtitle:
            sp = None
            for p in self._placeholders():
                try:
                    if int(p.placeholder_format.idx) == 1:
                        sp = p
                        break
                except Exception:
                    continue
            y = top + h + _pptx_in(0.42)
            sh = self._m(subtitle, width, 17, 1.15)
            if sp is not None:
                sp.left, sp.top = left, y
                sp.width, sp.height = width, max(sh, _pptx_in(0.6))
                self._write(sp.text_frame.paragraphs[0], subtitle, 17,
                            color=self.muted, align=PP_ALIGN.LEFT,
                            line_spacing=1.15)
            else:
                self._write(self._box(left, y, width, sh).text_frame.paragraphs[0],
                            subtitle, 17, color=self.muted,
                            align=PP_ALIGN.LEFT, line_spacing=1.15)
        self._drop_empty_placeholders()
        # A hairline near the foot closes the cover's lower half. It must stop
        # at the accent panel: drawn across the full text width it used to run
        # INTO the panel and hang under the white right-hand edge.
        self.rule(left, self.H - _pptx_in(1.1),
                  min(width, self.W - panel - left - _pptx_in(0.5)),
                  thick=_pptx_in(0.02), color=self.soft)
        return s

    def section_slide(self, heading):
        """A section divider: a heading with no body of its own.

        These used to render as a near-empty content slide — a title and a rule
        at the top of a white canvas with the bottom two-thirds blank. A divider
        is a composition: full accent, white title, a large faint numeral. It is
        the rhythm a polished deck uses to say "new part" without a word the
        author did not write, and it is the third layout the renderer reaches
        for instead of Title Slide / Title Only on every page.
        """
        from pptx.enum.text import PP_ALIGN
        s = self.add("Section Header", 2, band=0, bg=self.accent)
        text = _as_text(heading)
        left, width = self.mx, self.cw
        white = _pptx_rgb("FFFFFF")
        # A large faint numeral in the top corner: the section's own number,
        # lighter than the accent so it reads as a watermark, not a headline.
        num = self._box(self.W - self.mx - _pptx_in(2.2), _pptx_in(0.45),
                        _pptx_in(2.2), _pptx_in(1.5))
        self._write(num.text_frame.paragraphs[0], f"{self.count - 1:02d}", 66,
                    color=_pptx_mix(self.accent, white, 0.30), bold=True,
                    align=PP_ALIGN.RIGHT, font=self.head_font)
        size = 40
        while (size > 28
               and self._m(text, width, size, 1.1, pad_pt=2.0,
                           em=self.head_em) > _pptx_in(2.4)):
            size -= 2
        h = self._m(text, width, size, 1.1, pad_pt=2.0, em=self.head_em)
        top = int(self.H * 0.42) - h // 2
        ph = self._title_placeholder()
        if ph is not None:
            ph.left, ph.top, ph.width, ph.height = left, top, width, h
            self._write(ph.text_frame.paragraphs[0], text, size, color=white,
                        bold=True, align=PP_ALIGN.LEFT, line_spacing=1.1,
                        font=self.head_font)
        else:
            self._write(self._box(left, top, width, h).text_frame.paragraphs[0],
                        text, size, color=white, bold=True,
                        align=PP_ALIGN.LEFT, line_spacing=1.1,
                        font=self.head_font)
        self.wrote = True
        self.rule(left, top + h + _pptx_in(0.20), _pptx_in(2.2),
                  color=self.pale, thick=_pptx_in(0.06))
        self._drop_empty_placeholders()
        self.footer(light=True)
        return s

    def _placeholders(self):
        try:
            return list(self.cur.placeholders)
        except Exception:
            return []

    def _title_placeholder(self):
        for p in self._placeholders():
            try:
                if int(p.placeholder_format.idx) == 0:
                    # Placeholders carry the layout's own insets (0.1in per
                    # side, plus extra leading). The measured height assumes
                    # the full width, so a title sized to fit wrapped one line
                    # more than planned and its rule cut through the text.
                    p.text_frame.margin_left = 0
                    p.text_frame.margin_right = 0
                    p.text_frame.margin_top = 0
                    p.text_frame.margin_bottom = 0
                    return p
            except Exception:
                continue
        return None

    def _drop_empty_placeholders(self):
        """A placeholder nobody filled shows "Click to add text" on the slide.

        The layout brings its own subtitle box; using it when there is a
        subtitle and removing it when there is not is what keeps an edited deck
        clean.
        """
        for p in self._placeholders():
            try:
                if p.text.strip():
                    continue
                el = p._element
                el.getparent().remove(el)
            except Exception:
                pass

    def row_h(self, n_rows) -> int:
        """Row height a table can actually take.

        A table cannot absorb spare height the way a list's spacing can, so on a
        slide with four rows and a blank bottom half it just looked small. Rows
        grow into the column instead, up to a height that still reads as a table.
        """
        fit = int(self.room() / max(1, n_rows) * 0.62)
        return max(_pptx_in(0.36), min(fit, _pptx_in(0.62)))

    def list_size(self, items) -> int:
        """Type size for a list, in both directions.

        A three-bullet slide is not a five-bullet slide wearing the same font:
        when the column has room to spare the type grows into it, which is what
        makes a short slide look chosen instead of shrunken."""
        size = self.BODY
        while size > self.MIN_BODY and self._list_height(items, size) > self.room():
            size -= 1
        while (size < self.MAX_BODY and len(items) <= 5
               and self._list_height(items, size + 1) <= self.room() * 0.80):
            size += 1
        return size

    def group_height(self, pend) -> int:
        """Measure a section's blocks before writing them.

        The PDF layout has measured a page for years; the deck only ever placed
        as it went, which is why four bullets landed in the top third of a
        13.3x7.5in canvas and the rest stayed white."""
        total = 0
        for b in pend:
            t = b["type"]
            if t in ("bullets", "numbers"):
                total += (self._list_height(b["items"],
                                            self.list_size(b["items"]))
                          + _pptx_in(0.16)
                          + (2 * self.CARD_PAD if self.card else 0))
            elif t == "paragraph":
                total += (self._m(b["text"], self.cw, self.BODY)
                          + _pptx_in(0.10))
            elif t == "quote":
                q = "“" + _as_text(b["text"]) + "”"
                total += (self._m(q, self.cw - _pptx_in(0.62),
                                            self.BODY + 2)
                          + _pptx_in(0.18))
            elif t == "heading":
                lvl = int(b.get("level") or 1)
                total += (self._m(b["text"], self.cw,
                                            22 if lvl == 2 else 19, 1.15,
                                            pad_pt=2.0) + _pptx_in(0.06))
            elif t == "table":
                cols = max(1, len(b.get("header")
                               or (b["rows"][0] if b.get("rows") else [1])))
                n = len(b.get("rows") or []) + (1 if b.get("header") else 0)
                total += self.row_h(n) * max(1, n) + _pptx_in(0.14)
            elif t == "chart":
                total += _pptx_in(3.62) + (_pptx_in(0.34)
                                           if b.get("caption") else 0)
            elif t == "image":
                total += _pptx_in(4.4) + (_pptx_in(0.34)
                                          if b.get("caption") else 0)
            elif t == "stats":
                # Measured the same way stat_cards sizes itself, so `balance`
                # does not reserve 1.78in for a row that is actually taller.
                gap = _pptx_in(0.22)
                n = max(1, len(b["items"]))
                w = (self.cw - gap * (n - 1)) // n
                inner = w - _pptx_in(0.32)
                vh = max(self._m(it["value"], inner, 30, 0.95, pad_pt=2.0,
                                 em=self.head_em) for it in b["items"])
                lh = max((self._m(it.get("label") or "", inner, 12, 1.1)
                          for it in b["items"]), default=0)
                total += vh + lh + _pptx_in(0.32) + _pptx_in(0.16)
            elif t == "columns":
                pad = _pptx_in(0.22)
                n = max(1, len(b["items"]))
                gap = _pptx_in(0.26)
                w = (self.cw - gap * (n - 1)) // n
                body = max((self._m(c.get("body") or "", w - 2 * pad,
                                    self.BODY - 3, 1.12)
                            for c in b["items"]), default=0)
                total += max(body + _pptx_in(0.84) + 2 * pad, _pptx_in(1.5)) \
                    + _pptx_in(0.16)
        return total

    def balance(self, pend):
        """Use the slide's height instead of leaving the bottom half white.

        Centring the block outright left a void between the rule and the first
        bullet. Real decks spend spare height on the rhythm between lines, so
        most of the slack goes into the gaps and only a little into the drop."""
        need = self.group_height(pend)
        slack = self.room() - need
        if slack <= _pptx_in(0.6):
            self.spread = 0
            return
        if self.card_only(pend):
            # One self-contained block: there are no inter-block gaps to grow,
            # so give the block a share of the slack to fill vertically. The
            # renderers read `self.grow` as extra card height / column height.
            # Three stat cards or three columns pinned to the top third of a
            # 7.5in canvas is the "unfinished slide" look the user reports:
            # the block is allowed to take most of the body height, and the
            # slack above it is spent on a drop so the group sits optically
            # centred rather than stuck to the rule.
            self.grow = min(int(slack * 0.80), _pptx_in(2.6))
            self.top += min(int(slack * 0.26), _pptx_in(0.90))
            self.spread = 0
            return
        self.grow = 0
        on_face = [b for b in pend if b["type"] != "note"]
        if (len(on_face) == 1
                and on_face[0]["type"] in ("quote", "paragraph")):
            # A pull quote alone on a slide is a composition, not a line of
            # text near the ceiling: drop it toward the optical centre and
            # leave the air below.
            self.spread = 0
            self.top += min(int(slack * 0.38), _pptx_in(2.0))
            return
        gaps = self.gap_count(pend)
        self.top += min(int(slack * 0.18), _pptx_in(0.55))
        self.spread = min(int(slack * 0.62) // max(1, gaps), _pptx_in(0.42))

    def gap_count(self, pend) -> int:
        n = 0
        for b in pend:
            t = b["type"]
            if t in ("bullets", "numbers"):
                n += max(1, len(b["items"]) - 1)
            elif t in ("paragraph", "quote", "heading", "table", "chart",
                       "image", "stats", "columns", "callout"):
                n += 1
        return max(1, n)

    def card_only(self, pend) -> bool:
        """True when the slide is one block that draws its own fixed box.

        `balance` grows the *gaps* between blocks; a single stats row or a
        single set of columns has no gaps to grow, so the whole slack went
        unused and the cards sat in the top third of an otherwise empty slide.
        For these, the block itself is allowed to grow into the space.
        """
        on_face = [b for b in pend if b["type"] != "note"]
        return len(on_face) == 1 and on_face[0]["type"] in (
            "stats", "columns", "callout")

    def slide_for(self, heading, continued=False, pend=None):
        """A content slide: real title placeholder, measured, with its rule."""
        from pptx.enum.text import PP_ALIGN
        s = self.add("Title Only", 5)
        text = _as_text(f"{heading} (continued)"
                        if (continued and heading) else heading)
        if text:
            tw = int(self.cw * 0.86)
            h = self._m(text, tw, self.HEAD, 1.12, pad_pt=2.0,
                        em=self.head_em)
            ph = self._title_placeholder()
            if ph is not None:
                ph.left, ph.top = self.mx, self.top
                ph.width, ph.height = tw, h
                self._write(ph.text_frame.paragraphs[0], text, self.HEAD,
                            color=self.ink, bold=True, align=PP_ALIGN.LEFT,
                            line_spacing=1.12, font=self.head_font)
                self._index(self.top, h)
            else:
                self._write(self._box(self.mx, self.top, tw,
                                      h).text_frame.paragraphs[0], text,
                            self.HEAD, color=self.ink, bold=True,
                            align=PP_ALIGN.LEFT, line_spacing=1.12)
            # A little air between the last baseline and the rule: the box
            # bottom is where the descender sits, and a rule flush against it
            # reads as a strikethrough on a two-line title.
            self.top += h + _pptx_in(0.10)
            self.rule(self.mx, self.top, _pptx_in(1.5))
            self.top += self.rule_h + _pptx_in(0.28)
        else:
            # An untitled group is not given the deck's title (that is slide 1's
            # job) and is not left with an empty title band either: the
            # placeholder is dropped and the body takes the top margin.
            self.top = _pptx_in(0.9)
        # `wrote` tracks BODY content, not the title: the first block on a slide
        # must be allowed to overflow onto the page rather than be handed back
        # for a continuation that cannot hold it either.
        self.wrote = False
        if pend:
            self.balance(pend)
        self._drop_empty_placeholders()
        self.footer()
        return s

    # ── content, each measured ────────────────────────────────────────────
    def _m(self, text, width_emu, size_pt, leading_mult=1.22, pad_pt=4.0,
           em=None):
        return _pptx_text_height(text, width_emu, size_pt, leading_mult, pad_pt,
                                 em=self.em if em is None else em,
                                 line_em=getattr(self, "line_em", 1.22))

    def _index(self, title_top, title_h):
        """The slide's own number, large and faint in the title band.

        A numeral is how a deck says 'fourth section' without adding a word the
        author did not write, and it gives the top of a sparse slide a shape."""
        from pptx.enum.text import PP_ALIGN
        if self.count < 2:
            return
        w = _pptx_in(1.25)
        box = self._box(self.W - self.mx - w, title_top - _pptx_in(0.10), w,
                        title_h + _pptx_in(0.2))
        self._write(box.text_frame.paragraphs[0], f"{self.count - 1:02d}", 34,
                    color=self.soft, bold=True, align=PP_ALIGN.RIGHT,
                    font=self.head_font)

    def room(self) -> int:
        return self.bottom - self.top

    def paragraph(self, text, force=False) -> bool:
        h = self._m(text, self.cw, self.BODY)
        if not force and h > self.room():
            return False
        tb = self._box(self.mx, self.top, self.cw, h)
        self._write(tb.text_frame.paragraphs[0], text, self.BODY,
                    color=self.ink, line_spacing=1.15)
        self.top += h + _pptx_in(0.10) + self.spread
        return True

    def quote(self, text, force=False) -> bool:
        """A pulled quote: accent bar, indented, one size above the body.

        It used to arrive as plain text wrapped in quotation marks, which is
        how a slide says "I had no idea this was a quotation"."""
        q = "“" + _as_text(text) + "”"
        bar_w = _pptx_in(0.055)
        indent = _pptx_in(0.30)
        w = self.cw - indent - bar_w
        h = self._m(q, w, self.BODY + 2, 1.18)
        if not force and h > self.room():
            return False
        self.rule(self.mx, self.top + _pptx_in(0.04), bar_w, thick=h,
                  color=self.accent)
        tb = self._box(self.mx + bar_w + indent, self.top, w, h)
        self._write(tb.text_frame.paragraphs[0], q, self.BODY + 2,
                    color=self.dark, italic=True, line_spacing=1.18)
        self.top += h + _pptx_in(0.18) + self.spread
        return True

    CARD_PAD = _pptx_in(0.26)

    def stat_cards(self, items, force=False) -> bool:
        """Big-number callouts across the slide width.

        A deck of bullets reads as a converted report; a row of large figures
        with small labels is the single most recognisable "designed deck"
        move. Each card is a tinted panel with the number in the accent and
        the label beneath it in muted ink."""
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
        from pptx.util import Pt
        n = max(1, len(items))
        gap = _pptx_in(0.22)
        w = (self.cw - gap * (n - 1)) // n
        pad = _pptx_in(0.16)
        inner_w = w - 2 * pad
        # Measure the value at its real size: "$210-430/kWh" wraps to two lines
        # in a quarter-width card, and a fixed 0.72in slot let the second line
        # print straight over its own label ("2025 ... x86"). Both the card
        # height and the label's offset now come from the same measurement.
        # ONE size for the row: a stats band whose figures are set at
        # different sizes reads as an accident. Shrink until the longest value
        # fits, then set every card at that size.
        size = 34
        while size > 20 and max(
                self._m(it["value"], inner_w, size, 0.95, pad_pt=2.0,
                        em=self.head_em) for it in items) > _pptx_in(1.10):
            size -= 2
        val_sizes = [size] * len(items)
        val_hs = [self._m(it["value"], inner_w, size, 0.95, pad_pt=2.0,
                          em=self.head_em) for it in items]
        label_hs = [self._m(it.get("label") or "", inner_w, 12, 1.1)
                    if it.get("label") else 0 for it in items]
        # All labels start at the same Y, after the TALLEST value in the row.
        # Placing each label straight under its own value left a two-line
        # figure's label sitting far below its neighbours' - a row of cards is
        # read across, so their labels have to line up.
        val_slot = max(val_hs)
        content_h = max(v + l for v, l in zip(val_hs, label_hs)) + 2 * pad
        # A stats row is a band of numbers, not a panel: a share of spare
        # height is fine, but growing it to fill a whole slide leaves three
        # inches of empty tint under every label. Cap the growth and centre
        # the content inside whatever height the card ends up with.
        h = content_h + min(int(getattr(self, "grow", 0)), _pptx_in(1.30))
        if h > self.room() and not force:
            return False
        top = self.top
        for i, it in enumerate(items):
            x = self.mx + i * (w + gap)
            card = self.cur.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                             x, top, w, h)
            try:
                card.adjustments[0] = 0.08
                card.fill.solid()
                card.fill.fore_color.rgb = self.tint
                card.line.color.rgb = self.soft
                card.line.width = Pt(0.75)
                card.shadow.inherit = False
            except Exception:
                pass
            # Value: large, accent, semibold. Label: small, muted, wrapped.
            # Everything sits centred in the card rather than pinned to its top.
            inner_h = val_slot + (label_hs[i] if it.get("label") else 0)
            y0 = top + max(pad, (h - inner_h) // 2)
            vb = self._box(x + pad, y0, inner_w, val_hs[i])
            vb.text_frame.word_wrap = True
            try:
                vb.text_frame.vertical_anchor = MSO_ANCHOR.TOP
            except Exception:
                pass
            self._write(vb.text_frame.paragraphs[0], it["value"],
                        val_sizes[i],
                        color=self.accent, bold=True, align=PP_ALIGN.LEFT,
                        line_spacing=0.95, font=self.head_font)
            if it.get("label"):
                lb = self._box(x + pad, y0 + val_slot, inner_w,
                               label_hs[i])
                lb.text_frame.word_wrap = True
                self._write(lb.text_frame.paragraphs[0], it["label"], 12,
                            color=self.muted, align=PP_ALIGN.LEFT,
                            line_spacing=1.1)
        self.top += h + _pptx_in(0.16) + self.spread
        return True

    def callout(self, title, text, tone=None, force=False) -> bool:
        """A titled, tinted panel - the deck's 'KEY INSIGHT' band."""
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.enum.text import PP_ALIGN
        from pptx.util import Pt
        title = str(title or "Note")
        body = str(text or "")
        warn = str(tone or "").lower() in ("warning", "danger", "alert")
        bar = _pptx_rgb("B3261E") if warn else self.accent
        inner_w = self.cw - _pptx_in(0.5)
        body_h = self._m(body, inner_w, self.BODY - 3, 1.14) if body else 0
        h = _pptx_in(0.50) + body_h + _pptx_in(0.30)
        if h > self.room() and not force:
            return False
        card = self.cur.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                         self.mx, self.top, self.cw, h)
        try:
            card.adjustments[0] = 0.06
            card.fill.solid()
            card.fill.fore_color.rgb = _pptx_mix(bar, _pptx_rgb("FFFFFF"), 0.94)
            card.line.color.rgb = bar
            card.line.width = Pt(0.9)
            card.shadow.inherit = False
        except Exception:
            pass
        band = self.cur.shapes.add_shape(MSO_SHAPE.RECTANGLE, self.mx,
                                         self.top, self.cw, _pptx_in(0.42))
        try:
            band.fill.solid()
            band.fill.fore_color.rgb = bar
            band.line.fill.background()
            band.shadow.inherit = False
        except Exception:
            pass
        tb = self._box(self.mx + _pptx_in(0.16), self.top + _pptx_in(0.05),
                       self.cw - _pptx_in(0.32), _pptx_in(0.32))
        self._write(tb.text_frame.paragraphs[0], title.upper(), 11,
                    color=_pptx_rgb("FFFFFF"), bold=True,
                    align=PP_ALIGN.LEFT, font=self.head_font)
        if body:
            bb = self._box(self.mx + _pptx_in(0.18),
                           self.top + _pptx_in(0.50), inner_w, body_h)
            bb.text_frame.word_wrap = True
            self._write(bb.text_frame.paragraphs[0], body, self.BODY - 3,
                        color=self.ink, align=PP_ALIGN.LEFT,
                        line_spacing=1.14)
        self.top += h + _pptx_in(0.16) + self.spread
        return True

    def column_cards(self, items, force=False) -> bool:
        """Two or three side-by-side columns, each a titled card.

        This is the layout for "compare these three things" that does not force
        a table: the eye reads across in parallel, which is the point. A single
        card takes the full width; two split evenly with a gutter; three use the
        deck's own three-up rhythm."""
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.enum.text import PP_ALIGN
        from pptx.util import Pt
        n = max(1, len(items))
        gap = _pptx_in(0.26)
        w = (self.cw - gap * (n - 1)) // n
        pad = _pptx_in(0.22)
        inner_w = w - 2 * pad
        # The title is measured, not assumed to be one line: "Long-Duration:
        # Flow" wraps in a third-width card, and a fixed 0.40in slot for it put
        # the body on top of the title's second line and spilled text past the
        # card's bottom edge.
        title_h = [self._m(c.get("title") or "", inner_w, self.BODY - 1, 1.05,
                           pad_pt=2.0, em=self.head_em)
                   for c in items]
        body_h = [self._m(c.get("body") or "", inner_w, self.BODY - 3, 1.12)
                  for c in items]
        h = max(max(t + b for t, b in zip(title_h, body_h)) + 2 * pad,
                _pptx_in(1.5))
        h += int(getattr(self, "grow", 0))
        if h > self.room() and not force:
            return False
        top = self.top
        for i, c in enumerate(items):
            x = self.mx + i * (w + gap)
            card = self.cur.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                             x, top, w, h)
            try:
                card.adjustments[0] = 0.06
                card.fill.solid()
                card.fill.fore_color.rgb = _pptx_rgb("FFFFFF")
                card.line.color.rgb = self.soft
                card.line.width = Pt(0.75)
                card.shadow.inherit = False
            except Exception:
                pass
            # An accent bar down the card's inner edge ties the three columns to
            # the deck without a heavy fill.
            bar = self.cur.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, top,
                                            _pptx_in(0.055), h)
            try:
                bar.fill.solid()
                bar.fill.fore_color.rgb = self.accent
                bar.line.fill.background()
                bar.shadow.inherit = False
            except Exception:
                pass
            y = top + pad
            if c.get("title"):
                th = title_h[i]
                tb = self._box(x + pad, y, inner_w, th)
                self._write(tb.text_frame.paragraphs[0], c["title"],
                            self.BODY - 1, color=self.dark, bold=True,
                            align=PP_ALIGN.LEFT, line_spacing=1.05,
                            font=self.head_font)
                y += th + _pptx_in(0.04)
            if c.get("body"):
                bb = self._box(x + pad, y, inner_w,
                               max(_pptx_in(0.3), h - (y - top) - pad + _pptx_in(0.1)))
                bb.text_frame.word_wrap = True
                self._write(bb.text_frame.paragraphs[0], c["body"],
                            self.BODY - 3, color=self.ink,
                            align=PP_ALIGN.LEFT, line_spacing=1.12)
        self.top += h + _pptx_in(0.16) + self.spread
        return True

    def list_inner(self) -> int:
        return self.cw - _pptx_in(0.38) - (2 * self.CARD_PAD if self.card else 0)

    def _list_height(self, items, size) -> int:
        """Rendered height of a list, INCLUDING the gap between its lines.

        `_bullet_frame` sets 9pt of space after every item, and this measure did
        not know about it, so the next block landed a quarter inch too high —
        which is how a sub-heading ended up on top of the last bullet."""
        inner = self.list_inner()
        hs = [self._m(i, inner, size) for i in items]
        return sum(hs) + _pptx_in(0.125) * max(0, len(hs) - 1)

    def subhead(self, text, size=22, force=False) -> bool:
        """A level-2/3 heading inside a slide.

        These used to be dropped by the deck renderer altogether: the loop only
        recognised a level-1 heading and had no branch for the rest, so a
        sub-heading's text reached the slide as nothing at all.
        """
        h = self._m(text, self.cw, size, 1.15, pad_pt=2.0)
        if h > self.room() and not force:
            return False
        if self.wrote and self.spread:
            # The air goes on BOTH sides of a sub-heading. Only ever adding it
            # after left "What breaks" sitting on the last bullet above it.
            self.top += self.spread
        tb = self._box(self.mx, self.top, self.cw, h)
        # In the dark end of the deck's own accent: a sub-heading in the same
        # black as the body is a wall, not a structure.
        self._write(tb.text_frame.paragraphs[0], text, size, color=self.dark,
                    bold=True, line_spacing=1.15, font=self.head_font)
        self.top += h + _pptx_in(0.06) + self.spread
        return True

    def bullets(self, items, numbered=False, force=False):
        """Write the items that fit; return the ones that did not.

        Shrinks the type before giving up, and only then asks the caller for a
        continuation slide — so a nine-bullet section becomes two slides rather
        than one unreadable one, and never becomes fewer words.
        """
        items = [str(i) for i in (items or []) if str(i).strip()]
        if not items:
            return []
        size = self.list_size(items)
        need = self._list_height(items, size)
        inner = self.list_inner()
        if need > self.room():
            # Split regardless of `force`: a list has no minimum indivisible
            # size, so "the first block must go on this slide" cannot be
            # satisfied by writing thirty bullets at 14pt under the footer.
            # At least one item always lands here, so the queue makes progress.
            keep, used = [], 0.0
            for one in items:
                h = self._m(one, inner, size)
                if keep and used + h > self.room():
                    break
                keep.append(one)
                used += h
            self._bullet_frame(keep, numbered, size)
            return items[len(keep):]
        keep = items
        if not force:
            keep, used = [], 0.0
            for i in items:
                one = self._m(i, inner, size)
                if used + one > self.room() and keep:
                    break
                keep.append(i)
                used += one
        self._bullet_frame(keep, numbered, size)
        return items[len(keep):]

    def _bullet_frame(self, items, numbered, size):
        from pptx.oxml.ns import qn
        from pptx.util import Pt
        h = self._list_height(items, size)
        # The list's real extent is its measured lines PLUS the air handed out
        # between them; sizing the card on `h` alone left the last bullet
        # standing outside it.
        span = h + self.spread * max(0, len(items) - 1)
        pad = self.CARD_PAD if self.card else 0
        if pad:
            # A list that owns its slide sits in a soft card of the accent
            # tint. Free-floating text on a 13x7.5in white canvas is the shape
            # of a deck that "looks basic" no matter how the type is set.
            from pptx.enum.shapes import MSO_SHAPE
            card = self.cur.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                             self.mx, self.top, self.cw,
                                             span + 2 * pad)
            try:
                card.adjustments[0] = 0.045
                card.fill.solid()
                card.fill.fore_color.rgb = self.tint
                card.line.fill.background()
                card.shadow.inherit = False
            except Exception:
                pass
        tb = self._box(self.mx + pad, self.top + pad, self.cw - 2 * pad,
                       span + _pptx_in(0.06))
        tf = tb.text_frame
        accent_hex = "%02X%02X%02X" % tuple(self.accent)
        for n, item in enumerate(items):
            p = tf.paragraphs[0] if n == 0 else tf.add_paragraph()
            try:
                p.space_after = Pt(9 + self.spread / 12700
                                   if n < len(items) - 1 else 0)
                p.line_spacing = 1.10
                pPr = p._p.get_or_add_pPr()
                pPr.set("marL", str(_pptx_in(0.38)))
                pPr.set("indent", str(-_pptx_in(0.38)))
                for tag in ("a:buClr", "a:buSzPct", "a:buFont", "a:buNone",
                            "a:buChar", "a:buAutoNum"):
                    for el in pPr.findall(qn(tag)):
                        pPr.remove(el)
                # Schema order: buClr, buSzPct, buFont, then the bullet itself.
                # The marker in the accent colour is what turns a wall of black
                # text into a list a room can follow.
                bu_clr = pPr.makeelement(qn("a:buClr"), {})
                bu_clr.append(pPr.makeelement(qn("a:srgbClr"),
                                              {"val": accent_hex}))
                pPr.append(bu_clr)
                pPr.append(pPr.makeelement(qn("a:buSzPct"), {"val": "95000"}))
                if numbered:
                    el = pPr.makeelement(qn("a:buAutoNum"),
                                         {"type": "arabicPeriod"})
                else:
                    pPr.append(pPr.makeelement(qn("a:buFont"),
                                               {"typeface": "Arial"}))
                    el = pPr.makeelement(qn("a:buChar"), {"char": "•"})
                pPr.append(el)
            except Exception:
                item = ("• " if not numbered else f"{n + 1}. ") + str(item)
            self._write(p, item, size, color=self.ink, line_spacing=1.10)
        # The gaps that were handed out above have to be paid for here or the
        # next block lands on top of this one.
        self.top += span + 2 * pad + _pptx_in(0.16)
        return True

    def table(self, header, rows, force=False) -> list:
        """Write the rows the slide holds; return the ones it does not.

        A table used to be all-or-nothing: fifteen rows that did not fit were
        written anyway and the last five fell under the footer. Rows split, and
        the continuation repeats the header so the columns keep their names."""
        from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
        rows = list(rows or [])
        cols = max(1, len(header or (rows[0] if rows else [1])))
        body_pt = 14 if cols <= 4 else (12 if cols <= 6 else 11)
        n_head = 1 if header else 0
        row_h = self.row_h(n_head + len(rows))
        fit = int(max(0, self.room() - self.spread) // row_h) - n_head
        if fit < 1:
            if not force and self.wrote:
                return rows            # none of it fits here: next slide
            fit = 1                    # an empty slide still gets a row
        take, rest = rows[:fit], rows[fit:]
        n_rows = len(take) + n_head
        h = row_h * max(1, n_rows)
        shape = self.cur.shapes.add_table(n_rows, cols, self.mx, self.top,
                                          self.cw, h)
        t = shape.table
        # The default table style paints its OWN first row and banding on top of
        # what we set, and its rows size to content, which is why every deck
        # table had one tall row and ragged edges. Take the style off and own it.
        try:
            t.first_row = False
            t.horz_banding = False
        except Exception:
            pass
        for r in t.rows:
            try:
                r.height = row_h
            except Exception:
                pass
        # Own the table's lines. The blank layout's default table style draws
        # a full grid, and a grid line lands INSIDE the cell's text band: a
        # live deck showed a vertical rule running through "Index | Memory"
        # in the header and through every figure in the right-hand column.
        # Fills (a tinted band per row, an accent header) carry the structure;
        # no rule is drawn anywhere.
        _no_line = {"a:lnL": {}, "a:lnR": {}, "a:lnT": {}, "a:lnB": {}}

        def _clear_borders(cell):
            try:
                tcPr = cell._tc.get_or_add_tcPr()
                for tag in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):
                    for old in tcPr.findall(
                            "{http://schemas.openxmlformats.org/drawingml/2006/main}"
                            + tag.split(":")[1]):
                        tcPr.remove(old)
                from lxml import etree
                ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
                for tag in ("lnL", "lnR", "lnT", "lnB"):
                    el = etree.SubElement(tcPr, f"{{{ns}}}{tag}", w="0")
                    etree.SubElement(el, f"{{{ns}}}noFill")
            except Exception:
                pass
        numeric = []
        for ci in range(cols):
            vals = [row[ci] for row in rows if ci < len(row) and str(row[ci]).strip()]
            numeric.append(bool(vals) and all(_PPTX_NUMBER_R.match(str(v).strip())
                                              for v in vals))
        from pptx.enum.text import MSO_ANCHOR

        def place(cell, text, size, color, bold=False, align=None):
            cell.text = ""
            _clear_borders(cell)
            try:
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                cell.margin_left = _pptx_in(0.12)
                cell.margin_right = _pptx_in(0.12)
                cell.margin_top = _pptx_in(0.04)
                cell.margin_bottom = _pptx_in(0.04)
            except Exception:
                pass
            self._write(cell.text_frame.paragraphs[0], text, size, color=color,
                        bold=bold, align=align)

        ri = 0
        if header:
            for ci in range(cols):
                place(t.cell(0, ci), header[ci] if ci < len(header) else "",
                      body_pt, _pptx_rgb("FFFFFF"), bold=True,
                      align=PP_ALIGN.RIGHT if numeric[ci] else PP_ALIGN.LEFT)
                try:
                    t.cell(0, ci).fill.solid()
                    t.cell(0, ci).fill.fore_color.rgb = self.accent
                except Exception:
                    pass
            ri = 1
        for n, row in enumerate(take):
            for ci in range(cols):
                place(t.cell(ri + n, ci), row[ci] if ci < len(row) else "",
                      body_pt, self.ink,
                      align=PP_ALIGN.RIGHT if numeric[ci] else PP_ALIGN.LEFT)
                try:
                    t.cell(ri + n, ci).fill.solid()
                    t.cell(ri + n, ci).fill.fore_color.rgb = (
                        self.tint if n % 2 else _pptx_rgb("FFFFFF"))
                except Exception:
                    pass
        self.top += h + _pptx_in(0.14) + self.spread
        return rest

    def note(self, text) -> bool:
        try:
            self.cur.notes_slide.notes_text_frame.text = _math_to_text(
                str(text or ""))
            return True
        except Exception:
            return False

    def media(self, kind: str, b: dict) -> bool:
        """Charts and pictures: the two things that made decks text-only."""
        from pptx.enum.text import PP_ALIGN
        from pptx.util import Emu
        height = Emu(3400000) if kind == "chart" else Emu(4200000)
        cap = _as_text(b.get("caption"))
        cap_h = self._m(cap, self.cw, 12, 1.1) if cap else 0
        if height + cap_h + Emu(260000) > self.room() and self.wrote:
            return False
        if kind == "chart":
            _pptx_chart(self.cur, b, self.mx, self.top, self.cw,
                        lambda s, u: self.cur, deck=self)
        else:
            _pptx_image(self.cur, b, self.mx, self.top, self.cw,
                        lambda s, u: self.cur)
        self.top += height + Emu(140000) + self.spread
        if cap:
            # The caption used to survive normalisation and then be dropped on
            # the floor here: the number on a chart with no units on it.
            tb = self._box(self.mx, self.top, self.cw, cap_h)
            self._write(tb.text_frame.paragraphs[0], cap, 12,
                        color=self.muted, italic=True, align=PP_ALIGN.LEFT,
                        line_spacing=1.1)
            self.top += cap_h + Emu(120000) + self.spread
        return True


def build_pptx(spec: dict) -> tuple[bytes, str]:
    from pptx import Presentation
    from pptx.util import Emu

    title = _as_text(spec.get("title"))
    blocks = _norm_blocks(spec.get("blocks"), title)
    if not title and not blocks:
        raise DocGenError("nothing to render: give a title or some blocks")

    prs = Presentation()
    import doclayout as _lay
    # Thirteen preset canvas sizes; the deck's geometry is a spec field, and
    # every measurement below is derived from it rather than hardcoded.
    _sw_in, _sh_in = _lay.resolve_slide_size(spec.get("slide_size"))
    prs.slide_width = Emu(int(_sw_in * _PPTX_IN))
    prs.slide_height = Emu(int(_sh_in * _PPTX_IN))
    deck = _Deck(prs, _lay.resolve_style(spec.get("style")),
                 running_title=title)
    # The theme is the deck's palette and type underneath every explicit run:
    # placeholders, notes and PowerPoint's own pickers inherit it.
    _pptx_theme(prs, deck)

    def _norm_key(s):
        return re.sub(r"[\W_]+", " ", _strip_md(s or "").lower()).strip()

    # A deck's cover arrives three ways: `title`+`subtitle` fields, a `cover`
    # block, or a level-1 heading that restates the title. Resolve the subtitle
    # from whichever the model used BEFORE the title slide is written, so the
    # sentence is said once, on the cover, and not again as slide 2.
    subtitle = _as_text(spec.get("subtitle"))
    if title and not subtitle:
        for b in blocks:
            if b["type"] == "cover":
                subtitle = _as_text(b.get("subtitle"))
                break
            if b["type"] == "heading" and int(b.get("level") or 1) == 1:
                break
            if b["type"] == "paragraph":
                subtitle = _as_text(b.get("text"))
                break
    cover_key, sub_key = _norm_key(title), _norm_key(subtitle)
    cover_shown = False
    if title:
        deck.title_slide(title, subtitle)
        cover_shown = True

    def write_onto_slide(heading, pend):
        """Lay out one section on the current slide; return where it broke."""
        wrote = False
        i = 0
        while i < len(pend):
            b = pend[i]
            t = b["type"]
            if t in ("bullets", "numbers"):
                rest = deck.bullets(b["items"], numbered=(t == "numbers"),
                                    force=not wrote)
                if rest:
                    # The list broke across the slide: hand the remainder back
                    # for a continuation slide, same block, next page.
                    return i, dict(b, items=rest)
                wrote = True
            elif t == "paragraph":
                if not deck.paragraph(b["text"], force=not wrote):
                    return i, None
                wrote = True
            elif t == "quote":
                if not deck.quote(b["text"], force=not wrote):
                    return i, None
                wrote = True
            elif t == "table":
                rest = deck.table(b.get("header"), b["rows"], force=not wrote)
                if rest:
                    return i, dict(b, rows=rest)
                wrote = True
            elif t == "heading":
                # Only level 1 opens a slide; a deeper heading is a sub-heading
                # line, and its text must never be dropped.
                if not deck.subhead(_strip_md(b["text"]),
                                    22 if int(b.get("level") or 1) == 2
                                    else 19, force=not wrote):
                    return i, None
                wrote = True
            elif t in ("chart", "image"):
                if not deck.media(t, b):
                    return i, None
                wrote = True
            elif t == "stats":
                if not deck.stat_cards(b["items"], force=not wrote):
                    return i, None
                wrote = True
            elif t == "callout":
                if not deck.callout(b.get("title"), b.get("text"),
                                    b.get("tone"), force=not wrote):
                    return i, None
                wrote = True
            elif t == "columns":
                if not deck.column_cards(b["items"], force=not wrote):
                    return i, None
                wrote = True
            elif t == "note":
                if not deck.note(b["text"]):
                    deck.paragraph(b["text"], force=True)
                wrote = True
            else:
                wrote = True
            i += 1
        return None, None

    sections_done = 0

    def flush(heading, section):
        """One slide per section, continuing onto more when it overflows."""
        nonlocal sections_done
        pend = list(section)
        if not heading:
            # A heading-less group that only restates the cover is not a slide:
            # the sentence is already on the title slide.
            def _restates(b):
                k = _norm_key(b.get("text") or "")
                return bool(k) and k in (cover_key, sub_key)
            pend = [b for b in pend if not _restates(b)]
            if not pend:
                return
        sections_done += 1
        # A heading with no body of its own is a section divider, not a
        # near-empty content slide: full accent, white title. Speaker notes
        # still land on the notes page.
        if heading and not [x for x in pend if x["type"] != "note"]:
            deck.section_slide(heading)
            for x in pend:
                if x["type"] == "note":
                    deck.note(x["text"])
            return
        first = True
        guard = 0
        while True:
            guard += 1
            if guard > 40:                      # never spin on a bad block
                return
            # A list that owns the slide gets a card. A speaker note does not
            # own it: it lands on the notes page and takes no room on the face.
            on_face = [x for x in pend if x["type"] != "note"]
            deck.card = (len(on_face) == 1
                         and on_face[0]["type"] in ("bullets", "numbers"))
            deck.slide_for(heading, continued=not first, pend=pend)
            first = False
            idx, leftover = write_onto_slide(heading, pend)
            if idx is None:
                return
            if leftover is not None:
                # Partially written: the remainder stands in for that block.
                pend = [leftover] + pend[idx + 1:]
            else:
                pend = pend[idx:]        # nothing written: retry it whole
            if not pend:
                return

    pending: list[dict] = []
    heading = ""
    sections_skipped = 0
    for b in blocks:
        t = b["type"]
        if t == "cover":
            if not cover_shown:
                # A cover written as a block with no `title` field used to be
                # skipped outright, which threw the deck's title slide away.
                ct = _strip_md(b.get("title")) or title
                deck.title_slide(ct, _as_text(b.get("subtitle")) or subtitle)
                cover_key, sub_key = _norm_key(ct), _norm_key(subtitle)
            cover_shown = True
            continue
        if t == "heading" and int(b.get("level") or 1) == 1:
            hd = _strip_md(b["text"])
            if (cover_key and _norm_key(hd) == cover_key and not sections_done
                    and cover_shown):
                # The model wrote the title slide as its first section. The
                # cover already says it, so this heading opens no slide; its
                # blocks stay a lead group (and go away if they only restate
                # the cover).
                if pending:
                    flush("", pending)
                sections_skipped += 1
                heading, pending = "", []
                continue
            if heading or pending:
                flush(heading, pending)
            heading, pending = hd, []
            continue
        if t == "pagebreak":
            # Close the current section; the block after it opens the next
            # slide. Adding a slide HERE used to leave a blank page between the
            # break and the heading that followed it, because a level-1 heading
            # now starts a slide of its own.
            if heading or pending:
                flush(heading, pending)
            heading, pending = "", []
            continue
        pending.append(b)

    if heading or pending:
        # A heading-less group must not borrow the deck's title: the title
        # slide has already said it, and repeating it produced a second slide
        # named exactly like the first. Only a deck with no title slide at all
        # needs the fallback.
        flush(heading or ("" if deck.count else title or "Presentation"),
              pending)
    elif not deck.count:
        deck.slide_for(title or "Presentation")

    sections = sum(1 for b in blocks if b["type"] == "heading"
                   and int(b.get("level") or 1) == 1) - sections_skipped
    if deck.count < sections + 1:
        print(f"[docgen.pptx] {sections} sections produced {deck.count} "
              f"slides — a section was packed instead of separated")

    out = io.BytesIO()
    prs.save(out)
    return out.getvalue(), _safe_filename(title, "pptx")


# ── Word ─────────────────────────────────────────────────────────────────────

def _docx_caption(d, text: str) -> None:
    """A caption under a figure, in the same grey small style the PDF uses."""
    try:
        p = d.add_paragraph(_strip_md(text))
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
            h = d.add_heading(_strip_md(b["title"]), level=4)
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
        hdr[i + 1].text = _strip_md(s.get("name") or f"series {i + 1}")
    for ri, c in enumerate(cats):
        cells = tbl.add_row().cells
        cells[0].text = _strip_md(c)
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

    title = _as_text(spec.get("title"))
    blocks = _norm_blocks(spec.get("blocks"), title)
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
                    cells[ci].text = _strip_md(h)
                    for p in cells[ci].paragraphs:
                        for r in p.runs:
                            r.font.bold = True
            for row in rows:
                cells = tbl.add_row().cells
                for ci, c in enumerate(row[:cols]):
                    cells[ci].text = _strip_md(c)
            d.add_paragraph()
        elif t == "callout":
            # Word has no shaded-band primitive in python-docx, so the titled
            # box becomes a bold title paragraph plus its body - the words
            # survive, which is what matters on paper.
            p = d.add_paragraph()
            r = p.add_run(_strip_md(b.get("title") or "Note").upper())
            r.font.bold = True
            r.font.size = Pt(9.5)
            if b.get("text"):
                d.add_paragraph(_strip_md(b["text"]))
            d.add_paragraph()
        elif t == "stats":
            # No cards on paper: the figures still need to be read, so they
            # become one emphasised line each rather than vanishing.
            for it in b["items"]:
                p = d.add_paragraph()
                if it.get("value"):
                    r = p.add_run(_strip_md(it["value"]) + "  ")
                    r.font.bold = True
                    r.font.size = Pt(16)
                if it.get("label"):
                    p.add_run(_strip_md(it["label"]))
            d.add_paragraph()
        elif t == "columns":
            for c in b["items"]:
                if c.get("title"):
                    d.add_heading(_strip_md(c["title"]), level=3)
                if c.get("body"):
                    d.add_paragraph(_strip_md(c["body"]))
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

    title = _as_text(spec.get("title"))
    blocks = _norm_blocks(spec.get("blocks"), title)
    sheets_in = spec.get("sheets")
    if not sheets_in and not blocks:
        raise DocGenError("nothing to render: give sheets or some blocks")

    wb = Workbook()
    wb.remove(wb.active)

    def fill(ws, rows, header=None):
        r0 = 1
        if header:
            for ci, h in enumerate(header, 1):
                c = ws.cell(row=1, column=ci, value=_strip_md(h))
                c.font = Font(bold=True)
                c.alignment = Alignment(wrap_text=True, vertical="top")
            r0 = 2
        n = 0
        widest = 0
        for row in rows:
            vals = list(row) if isinstance(row, list) else [row]
            widest = max(widest, len(vals))
            for ci, v in enumerate(vals, 1):
                c = ws.cell(row=r0 + n, column=ci, value=_strip_md(str(v)))
                c.alignment = Alignment(wrap_text=True, vertical="top")
            n += 1
        # Widths for every column the data actually has. This used to stop at
        # 12 because a table was capped at 40 columns and 12 was "enough".
        for ci in range(1, widest + 1):
            ws.column_dimensions[get_column_letter(ci)].width = 28

    # Explicit sheets win — but ONLY if they actually produced a sheet. The
    # old guard was `isinstance(sheets_in, list) and wb.sheetnames`, and
    # because `wb.remove(wb.active)` had already emptied the workbook, a
    # request carrying an EMPTY `sheets: []` fell into the `pass` branch and
    # the whole table-block loop was skipped: the model sent `sheets: []`
    # plus real `table` blocks, and the workbook shipped with nothing but a
    # blank "Sheet1" (verified - 0 cells, 2 sheets, both empty).
    if isinstance(sheets_in, list):
        for s in sheets_in:
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
            txt = _strip_md(para.get("text", ""))
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

# Markdown / HTML / CSV / JSON / EPUB are serialisations of the SAME block
# model, so they live in docgen_markup and are small, dependency-free writers.
# Merged in here so `generate(fmt, spec)` and the schema pick them up with no
# change to a layout engine. If the import fails (it should not - stdlib only),
# the four office formats still work.
try:
    import docgen_markup as _markup
    BUILDERS.update(_markup.BUILDERS)
except Exception as _e:  # pragma: no cover - defensive
    print(f"[docgen] markup writers unavailable: {_e!r}")

# Typst is an alternative typesetter (PDF) and a slide engine. It needs the
# `typst` package, which bundles the compiler; if it is missing the builders
# still register and raise an actionable error only when used, so a missing
# optional dependency never breaks the office formats.
try:
    import docgen_typst as _typst
    BUILDERS.update(_typst.BUILDERS)
except Exception as _e:  # pragma: no cover - defensive
    print(f"[docgen] typst writer unavailable: {_e!r}")
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
