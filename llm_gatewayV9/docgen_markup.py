"""Markdown / HTML / CSV / JSON / EPUB writers over the docgen block model.

Why a separate module: the PDF/PPTX/DOCX/XLSX builders in `docgen.py` are
layout engines with hundreds of lines each. These formats are serialisations of
the SAME block model (`spec["blocks"]`), so they are small, dependency-free
writers that share one block vocabulary. Keeping them here means adding a
format is a function and a BUILDERS entry, not a change to a layout engine.

Every writer consumes the raw spec dict (heading / paragraph / bullets /
numbers / quote / table / chart / stats / columns / callout / cover /
pagebreak / image) and ignores what it cannot render in that format, rather
than raising — a Markdown file cannot embed a chart, so it prints the chart's
data as a table. Nothing here executes or fetches: `image` blocks carry a
document id, which is only meaningful to the PDF/PPTX builders.

All writers return (bytes, filename) to match docgen.BUILDERS.
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from xml.sax.saxutils import escape as _xml_escape

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(title: str, fallback: str) -> str:
    s = _SAFE.sub("-", (title or "").strip()).strip("-")
    return (s or fallback)[:80]


def _blocks(spec: dict) -> list[dict]:
    out = []
    for b in (spec.get("blocks") or []):
        if isinstance(b, dict):
            out.append(b)
    return out


def _table_rows(b: dict) -> tuple[list[str], list[list[str]]]:
    header = [str(c) for c in (b.get("header") or [])]
    rows = [[str(c) for c in (r or [])] for r in (b.get("rows") or [])]
    return header, rows


def _chart_table(b: dict) -> tuple[list[str], list[list[str]]]:
    cats = [str(c) for c in (b.get("categories") or [])]
    series = b.get("series") or []
    header = ["category"] + [str(s.get("name") or f"series {i+1}")
                             for i, s in enumerate(series)]
    rows = []
    for i, c in enumerate(cats):
        row = [c]
        for s in series:
            data = s.get("data") or []
            row.append(str(data[i]) if i < len(data) else "")
        rows.append(row)
    return header, rows


# ── Markdown ─────────────────────────────────────────────────────────────────

def build_markdown(spec: dict) -> tuple[bytes, str]:
    title = str(spec.get("title") or "Document")
    out: list[str] = []
    if str(spec.get("subtitle") or "").strip():
        out.append(f"_{spec['subtitle']}_")
        out.append("")
    for b in _blocks(spec):
        t = str(b.get("type") or "").strip().lower()
        if t == "cover":
            out.append(f"# {b.get('title') or title}")
            if b.get("subtitle"):
                out.append(f"_{b['subtitle']}_")
            out.append("")
        elif t == "heading":
            lvl = max(1, min(6, int(b.get("level") or 1)))
            out.append("#" * lvl + " " + str(b.get("text") or ""))
            out.append("")
        elif t == "paragraph":
            out.append(str(b.get("text") or ""))
            out.append("")
        elif t in ("bullets", "numbers"):
            for i, it in enumerate(b.get("items") or []):
                out.append(f"- {it}" if t == "bullets" else f"{i+1}. {it}")
            out.append("")
        elif t == "quote":
            out.append("> " + str(b.get("text") or "").replace("\n", "\n> "))
            out.append("")
        elif t == "callout":
            head = f"**{b['title']}**: " if b.get("title") else ""
            out.append(f"> {head}{b.get('text') or ''}")
            out.append("")
        elif t == "table":
            header, rows = _table_rows(b)
            if header:
                out.append("| " + " | ".join(header) + " |")
                out.append("| " + " | ".join("---" for _ in header) + " |")
                for r in rows:
                    out.append("| " + " | ".join(r) + " |")
                out.append("")
        elif t == "chart":
            header, rows = _chart_table(b)
            if b.get("title"):
                out.append(f"**{b['title']}**")
                out.append("")
            if header and rows:
                out.append("| " + " | ".join(header) + " |")
                out.append("| " + " | ".join("---" for _ in header) + " |")
                for r in rows:
                    out.append("| " + " | ".join(r) + " |")
                out.append("")
        elif t == "stats":
            for it in (b.get("items") or []):
                out.append(f"- **{it.get('value', '')}** — {it.get('label', '')}")
            out.append("")
        elif t == "columns":
            for it in (b.get("items") or []):
                out.append(f"### {it.get('title', '')}")
                out.append(str(it.get("body") or ""))
                out.append("")
        elif t == "image":
            cap = b.get("caption")
            if cap:
                out.append(f"*[image: {cap}]*")
                out.append("")
        elif t == "pagebreak":
            out.append("---")
            out.append("")
    # References, if the spec carries them.
    refs = spec.get("references") or []
    if refs:
        out.append("## References")
        out.append("")
        for i, r in enumerate(refs):
            if isinstance(r, dict):
                out.append(f"{i+1}. {r.get('authors', '')} ({r.get('year', '')}). "
                           f"{r.get('title', '')}. {r.get('container', '')}.")
            else:
                out.append(f"{i+1}. {r}")
        out.append("")
    body = "\n".join(out).strip() + "\n"
    return body.encode("utf-8"), _slug(title, "document") + ".md"


# ── HTML ─────────────────────────────────────────────────────────────────────

def _md_inline(s: str) -> str:
    s = _xml_escape(str(s))
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    return s


def build_html(spec: dict) -> tuple[bytes, str]:
    title = _xml_escape(str(spec.get("title") or "Document"))
    style = str(spec.get("style") or "report")
    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{title}</title>",
        "<style>",
        "body{max-width:52rem;margin:2rem auto;padding:0 1rem;font-family:"
        "system-ui,-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.6;"
        "color:#18181b;background:#fff}",
        "h1,h2,h3{line-height:1.25;margin:1.4em 0 .5em}",
        "table{border-collapse:collapse;width:100%;margin:1em 0;font-size:.95rem}",
        "th,td{border:1px solid #d4d4d8;padding:.4rem .6rem;text-align:left}",
        "th{background:#f4f4f5}",
        "blockquote{border-left:3px solid #a1a1aa;margin:1em 0;padding:.2em 1em;"
        "color:#52525b}",
        "code{background:#f4f4f5;padding:.1em .3em;border-radius:3px}",
        ".callout{border-left:4px solid #6366f1;background:#eef2ff;padding:.6em 1em;"
        "margin:1em 0;border-radius:0 4px 4px 0}",
        ".callout.warning{border-color:#f59e0b;background:#fffbeb}",
        ".stats{display:flex;gap:1.5rem;flex-wrap:wrap;margin:1em 0}",
        ".stat{text-align:center}.stat b{display:block;font-size:1.6rem}",
        ".cols{display:flex;gap:1.5rem;flex-wrap:wrap}.cols>div{flex:1;min-width:12rem}",
        "footer{margin-top:2.5rem;font-size:.85rem;color:#71717a}",
        "</style></head><body>",
    ]
    sub = spec.get("subtitle")
    if sub:
        parts.append(f"<p><em>{_xml_escape(str(sub))}</em></p>")
    for b in _blocks(spec):
        t = str(b.get("type") or "").strip().lower()
        if t == "cover":
            parts.append(f"<h1>{_xml_escape(str(b.get('title') or title))}</h1>")
            if b.get("subtitle"):
                parts.append(f"<p><em>{_xml_escape(str(b['subtitle']))}</em></p>")
        elif t == "heading":
            lvl = max(1, min(6, int(b.get("level") or 1)))
            parts.append(f"<h{lvl}>{_md_inline(b.get('text', ''))}</h{lvl}>")
        elif t == "paragraph":
            parts.append(f"<p>{_md_inline(b.get('text', ''))}</p>")
        elif t in ("bullets", "numbers"):
            tag = "ul" if t == "bullets" else "ol"
            lis = "".join(f"<li>{_md_inline(i)}</li>" for i in (b.get("items") or []))
            parts.append(f"<{tag}>{lis}</{tag}>")
        elif t == "quote":
            parts.append(f"<blockquote>{_md_inline(b.get('text', ''))}</blockquote>")
        elif t == "callout":
            cls = "callout warning" if b.get("tone") == "warning" else "callout"
            head = f"<strong>{_xml_escape(str(b['title']))}</strong> " if b.get("title") else ""
            parts.append(f'<div class="{cls}">{head}{_md_inline(b.get("text", ""))}</div>')
        elif t == "table":
            header, rows = _table_rows(b)
            if header:
                th = "".join(f"<th>{_md_inline(c)}</th>" for c in header)
                trs = "".join("<tr>" + "".join(f"<td>{_md_inline(c)}</td>" for c in r) + "</tr>"
                              for r in rows)
                parts.append(f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>")
        elif t == "chart":
            header, rows = _chart_table(b)
            if b.get("title"):
                parts.append(f"<h3>{_xml_escape(str(b['title']))}</h3>")
            if header and rows:
                th = "".join(f"<th>{_md_inline(c)}</th>" for c in header)
                trs = "".join("<tr>" + "".join(f"<td>{_md_inline(c)}</td>" for c in r) + "</tr>"
                              for r in rows)
                parts.append(f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>")
        elif t == "stats":
            cells = "".join(f'<div class="stat"><b>{_xml_escape(str(i.get("value", "")))}</b>'
                            f'{_xml_escape(str(i.get("label", "")))}</div>'
                            for i in (b.get("items") or []))
            parts.append(f'<div class="stats">{cells}</div>')
        elif t == "columns":
            cells = "".join(f'<div><h3>{_xml_escape(str(i.get("title", "")))}</h3>'
                            f'<p>{_md_inline(i.get("body", ""))}</p></div>'
                            for i in (b.get("items") or []))
            parts.append(f'<div class="cols">{cells}</div>')
        elif t == "pagebreak":
            parts.append('<hr style="page-break-after:always;border:0">')
    refs = spec.get("references") or []
    if refs:
        parts.append("<h2>References</h2><ol>")
        for r in refs:
            if isinstance(r, dict):
                parts.append(f"<li>{_xml_escape(str(r.get('authors','')))} "
                             f"({_xml_escape(str(r.get('year','')))}). "
                             f"{_xml_escape(str(r.get('title','')))}. "
                             f"{_xml_escape(str(r.get('container','')))}.</li>")
            else:
                parts.append(f"<li>{_xml_escape(str(r))}</li>")
        parts.append("</ol>")
    if spec.get("author"):
        parts.append(f"<footer>{_xml_escape(str(spec['author']))}</footer>")
    parts.append("</body></html>")
    html = "\n".join(parts)
    return html.encode("utf-8"), _slug(title, "document") + ".html"


# ── CSV ──────────────────────────────────────────────────────────────────────

def build_csv(spec: dict) -> tuple[bytes, str]:
    """One row per table cell block; a table block becomes its own CSV section.

    A CSV is tabular, so a document that is mostly prose has no faithful CSV
    form. Rather than silently dropping the prose, emit a two-column
    (section, text) CSV when there are no tables, so nothing is lost.
    """
    title = str(spec.get("title") or "export")
    buf = io.StringIO()
    w = csv.writer(buf)
    tables = [b for b in _blocks(spec) if str(b.get("type") or "").lower() == "table"]
    if tables:
        for b in tables:
            header, rows = _table_rows(b)
            if header:
                w.writerow(header)
            for r in rows:
                w.writerow(r)
            w.writerow([])
    else:
        w.writerow(["section", "text"])
        for b in _blocks(spec):
            t = str(b.get("type") or "").lower()
            if t == "heading":
                w.writerow(["heading", str(b.get("text") or "")])
            elif t in ("paragraph", "quote"):
                w.writerow([t, str(b.get("text") or "")])
            elif t in ("bullets", "numbers"):
                for it in (b.get("items") or []):
                    w.writerow([t, str(it)])
    return buf.getvalue().encode("utf-8"), _slug(title, "export") + ".csv"


# ── JSON ─────────────────────────────────────────────────────────────────────

def build_json(spec: dict) -> tuple[bytes, str]:
    title = str(spec.get("title") or "document")
    payload = json.dumps(spec, ensure_ascii=False, indent=2).encode("utf-8")
    return payload, _slug(title, "document") + ".json"


# ── EPUB ─────────────────────────────────────────────────────────────────────

_XHTML_NS = "http://www.w3.org/1999/xhtml"


def _epub_chapter(title: str, blocks: list[dict]) -> str:
    body = []
    for b in blocks:
        t = str(b.get("type") or "").lower()
        if t == "heading":
            lvl = max(1, min(6, int(b.get("level") or 1)))
            body.append(f"<h{lvl}>{_xml_escape(str(b.get('text','')))}</h{lvl}>")
        elif t == "paragraph":
            body.append(f"<p>{_xml_escape(str(b.get('text','')))}</p>")
        elif t in ("bullets", "numbers"):
            tag = "ul" if t == "bullets" else "ol"
            lis = "".join(f"<li>{_xml_escape(str(i))}</li>" for i in (b.get("items") or []))
            body.append(f"<{tag}>{lis}</{tag}>")
        elif t == "quote":
            body.append(f"<blockquote><p>{_xml_escape(str(b.get('text','')))}</p></blockquote>")
        elif t == "table":
            header, rows = _table_rows(b)
            if header:
                th = "".join(f"<th>{_xml_escape(c)}</th>" for c in header)
                trs = "".join("<tr>" + "".join(f"<td>{_xml_escape(c)}</td>" for c in r) + "</tr>"
                              for r in rows)
                body.append(f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>")
    return (f'<?xml version="1.0" encoding="utf-8"?>\n'
            f'<!DOCTYPE html>\n<html xmlns="{_XHTML_NS}" lang="en"><head>'
            f'<meta charset="utf-8"/><title>{_xml_escape(title)}</title></head>'
            f'<body><h1>{_xml_escape(title)}</h1>' + "".join(body) + "</body></html>")


def build_epub(spec: dict) -> tuple[bytes, str]:
    """A minimal, valid EPUB 3 (a zip of XHTML) with no external dependency.

    Chapters are split on level-1 headings; a document with no headings
    becomes a single chapter. Not a layout engine — it is the same block model
    serialised to XHTML, which is what an e-reader needs.
    """
    title = str(spec.get("title") or "Document")
    author = str(spec.get("author") or "")
    blocks = _blocks(spec)
    # Split into chapters at level-1 headings.
    chapters: list[tuple[str, list[dict]]] = []
    cur_title, cur = title, []
    for b in blocks:
        if str(b.get("type") or "").lower() == "heading" and int(b.get("level") or 1) <= 1:
            if cur:
                chapters.append((cur_title, cur))
            cur_title = str(b.get("text") or title)
            cur = [b]
        else:
            cur.append(b)
    if cur:
        chapters.append((cur_title, cur))
    if not chapters:
        chapters = [(title, [])]

    files: dict[str, str] = {}
    manifest, spine = [], []
    for i, (ch_title, ch_blocks) in enumerate(chapters, 1):
        href = f"chap{i}.xhtml"
        files[href] = _epub_chapter(ch_title, ch_blocks)
        manifest.append(f'<item id="chap{i}" href="{href}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="chap{i}"/>')

    opf = (f'<?xml version="1.0" encoding="utf-8"?>\n'
           f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
           f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:identifier id="id">urn:uuid:aria-docgen</dc:identifier>'
           f'<dc:title>{_xml_escape(title)}</dc:title>'
           f'<dc:language>en</dc:language>'
           + (f'<dc:creator>{_xml_escape(author)}</dc:creator>' if author else "")
           + '<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>'
           '</metadata><manifest>'
           '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
           + "".join(manifest) +
           '</manifest><spine>' + "".join(spine) + '</spine></package>')

    nav_items = "".join(f'<li><a href="chap{i}.xhtml">{_xml_escape(t)}</a></li>'
                        for i, (t, _) in enumerate(chapters, 1))
    nav = (f'<?xml version="1.0" encoding="utf-8"?>\n'
           f'<!DOCTYPE html>\n<html xmlns="{_XHTML_NS}" xmlns:epub='
           f'"http://www.idpf.org/2007/ops" lang="en"><head><meta charset="utf-8"/>'
           f'<title>Contents</title></head><body><nav epub:type="toc" id="toc">'
           f'<h1>Contents</h1><ol>{nav_items}</ol></nav></body></html>')

    container = ('<?xml version="1.0" encoding="utf-8"?>\n'
                 '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                 'media-type="application/oebps-package+xml"/></rootfiles></container>')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # mtime must be first and stored (not deflated) per the EPUB spec.
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/nav.xhtml", nav)
        for href, content in files.items():
            z.writestr(f"OEBPS/{href}", content)
    return buf.getvalue(), _slug(title, "book") + ".epub"


# ── ODT / ODS ────────────────────────────────────────────────────────────────
# ODF via odfpy. ODT is the text document (headings, paragraphs, lists,
# tables); ODS is the spreadsheet (one sheet per table block, or the spec's
# `sheets`). These are the open, non-proprietary counterparts to docx/xlsx.

def build_odt(spec: dict) -> tuple[bytes, str]:
    from odf.opendocument import OpenDocumentText
    from odf.text import H, P, List as OList, ListItem
    from odf.table import Table, TableRow, TableCell

    doc = OpenDocumentText()
    title = str(spec.get("title") or "Document")
    doc.text.addElement(H(outlinelevel=1, text=title))
    if spec.get("subtitle"):
        doc.text.addElement(P(text=str(spec["subtitle"])))
    for b in _blocks(spec):
        t = str(b.get("type") or "").lower()
        if t == "cover":
            doc.text.addElement(H(outlinelevel=1, text=str(b.get("title") or title)))
        elif t == "heading":
            doc.text.addElement(H(outlinelevel=int(b.get("level") or 1),
                                  text=str(b.get("text") or "")))
        elif t == "paragraph":
            doc.text.addElement(P(text=str(b.get("text") or "")))
        elif t in ("bullets", "numbers"):
            lst = OList()
            for it in (b.get("items") or []):
                li = ListItem()
                li.addElement(P(text=str(it)))
                lst.addElement(li)
            doc.text.addElement(lst)
        elif t == "quote":
            doc.text.addElement(P(text=str(b.get("text") or "")))
        elif t == "table":
            header, rows = _table_rows(b)
            table = Table(name=str(b.get("title") or "Table"))
            if header:
                tr = TableRow()
                for c in header:
                    cell = TableCell(valuetype="string")
                    cell.addElement(P(text=str(c)))
                    tr.addElement(cell)
                table.addElement(tr)
            for r in rows:
                tr = TableRow()
                for c in r:
                    cell = TableCell(valuetype="string")
                    cell.addElement(P(text=str(c)))
                    tr.addElement(cell)
                table.addElement(tr)
            doc.text.addElement(table)
    import io as _io
    buf = _io.BytesIO()
    doc.save(buf)
    return buf.getvalue(), _slug(title, "document") + ".odt"


def build_ods(spec: dict) -> tuple[bytes, str]:
    from odf.opendocument import OpenDocumentSpreadsheet
    from odf.table import Table, TableRow, TableCell
    from odf.text import P

    doc = OpenDocumentSpreadsheet()
    title = str(spec.get("title") or "export")
    sheets = spec.get("sheets") or []
    if not sheets:
        tables = [b for b in _blocks(spec) if str(b.get("type") or "").lower() == "table"]
        sheets = []
        for i, b in enumerate(tables, 1):
            header, rows = _table_rows(b)
            sheets.append({"name": str(b.get("title") or f"Sheet{i}"),
                           "header": header, "rows": rows})
    if not sheets:
        # No tables: one sheet of section/text rows, so nothing is lost.
        rows = [[str(b.get("type") or ""), str(b.get("text") or "")]
                for b in _blocks(spec) if b.get("text")]
        sheets = [{"name": "Sheet1", "header": ["type", "text"], "rows": rows}]
    for sh in sheets:
        table = Table(name=str(sh.get("name") or "Sheet"))
        header = sh.get("header") or []
        rows = sh.get("rows") or []
        if header:
            tr = TableRow()
            for c in header:
                cell = TableCell(valuetype="string")
                cell.addElement(P(text=str(c)))
                tr.addElement(cell)
            table.addElement(tr)
        for r in rows:
            tr = TableRow()
            for c in r:
                cell = TableCell(valuetype="string")
                cell.addElement(P(text=str(c)))
                tr.addElement(cell)
            table.addElement(tr)
        doc.spreadsheet.addElement(table)
    import io as _io
    buf = _io.BytesIO()
    doc.save(buf)
    return buf.getvalue(), _slug(title, "export") + ".ods"


BUILDERS = {
    "md": (build_markdown, "text/markdown; charset=utf-8"),
    "markdown": (build_markdown, "text/markdown; charset=utf-8"),
    "html": (build_html, "text/html; charset=utf-8"),
    "csv": (build_csv, "text/csv; charset=utf-8"),
    "json": (build_json, "application/json; charset=utf-8"),
    "epub": (build_epub, "application/epub+zip"),
    "odt": (build_odt, "application/vnd.oasis.opendocument.text"),
    "ods": (build_ods, "application/vnd.oasis.opendocument.spreadsheet"),
}