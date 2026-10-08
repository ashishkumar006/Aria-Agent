"""Structured preview of a rendered document, for the console's preview pane.

Why this exists: the console can only show a real preview for PDFs (the
browser renders them). For PPTX/DOCX/XLSX there is nothing native to render,
and npm is unreachable so no JSZip/SheetJS. Rather than show a blank panel —
or worse, an empty one that reads as "working" — this module reads the
artifact that was ACTUALLY produced and describes it.

That distinction matters. The run's own `sections` / `slides` metadata is
model-generated and was repeatedly wrong (a deck that reported 10 slide
titles shipped a file containing 6 completely empty slides and 10
speaker-note-only slides). Reading the artifact is the only honest source.

Office formats are zip+XML, so they are parsed with the standard library
only - no new dependency. PDF text comes from pypdf, which is already
installed for the document parser.
"""
from __future__ import annotations

import re
import zipfile
from xml.etree import ElementTree as ET

# Namespace-agnostic tag matching: strip the namespace off each element so
# `{http://...drawingml/2006/main}t` matches `t`.
def _tag(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1] if "}" in el.tag else el.tag


def _texts(el: ET.Element) -> list[str]:
    """Every text run under `el`, in document order."""
    out = []
    for node in el.iter():
        if _tag(node) == "t" and node.text:
            out.append(node.text)
    return out


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


# ── PPTX ─────────────────────────────────────────────────────────────────

def _pptx(blob: bytes) -> dict:
    out: dict = {"kind": "pptx", "slides": [], "warnings": []}
    with zipfile.ZipFile(_buf(blob)) as z:
        names = set(z.namelist())
        # Slide order must come from presentation.xml; rglob order is
        # alphabetical, which puts slide10 before slide2.
        order: list[str] = []
        try:
            pres = ET.fromstring(z.read("ppt/presentation.xml"))
            rels = ET.fromstring(z.read("ppt/_rels/presentation.xml.rels"))
            target = {r.get("Id"): r.get("Target") for r in rels}
            for sld in pres.iter():
                if _tag(sld) == "sldId":
                    rid = sld.get(
                        "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                    t = (target.get(rid) or "").split("/")[-1]
                    if t:
                        order.append(f"ppt/slides/{t}")
        except Exception:
            pass
        if not order:
            order = sorted(n for n in names
                           if re.match(r"ppt/slides/slide\d+\.xml$", n))
        for i, path in enumerate(order, 1):
            if path not in names:
                continue
            root = ET.fromstring(z.read(path))
            shapes: list[list[str]] = []
            for sp in root.iter():
                if _tag(sp) == "txBody":
                    lines = [_clean("".join(
                        t for t in _texts(p) if t)) for p in sp.iter()
                        if _tag(p) == "p"]
                    lines = [l for l in lines if l]
                    if lines:
                        shapes.append(lines)
            if not shapes:
                for sp in root.iter():
                    if _tag(sp) == "txBody":
                        lines = [_clean(t) for t in _texts(sp) if _clean(t)]
                        if lines:
                            shapes.append(lines)
            title = shapes[0][0] if shapes else ""
            bullets = [l for s in shapes for l in s[1:]] if shapes else []
            if len(shapes) > 1:
                bullets += [shapes[0][1]] if len(shapes[0]) > 1 else []
            notes = ""
            npath = re.sub(r"slides/(slide\d+)\.xml",
                           r"notesSlides/\1-notesSlide.xml", path)
            if npath in names:
                try:
                    nroot = ET.fromstring(z.read(npath))
                    notes = " ".join(_clean(t) for t in _texts(nroot) if _clean(t))
                except Exception:
                    notes = ""
            slide = {"n": i, "title": title, "bullets": bullets,
                     "notes": notes, "empty": not (title or bullets)}
            out["slides"].append(slide)
    empties = [s["n"] for s in out["slides"] if s["empty"]]
    if empties:
        out["warnings"].append(
            f"{len(empties)} of {len(out['slides'])} slides have no text: "
            + ", ".join(str(n) for n in empties[:12]))
    if out["slides"]:
        out["title"] = out["slides"][0].get("title") or ""
        out["subtitle"] = f"{len(out['slides'])} slides"
    return out


# ── XLSX ─────────────────────────────────────────────────────────────────

def _xlsx(blob: bytes) -> dict:
    out: dict = {"kind": "xlsx", "sheets": [], "warnings": []}
    with zipfile.ZipFile(_buf(blob)) as z:
        names = set(z.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            sroot = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in sroot:
                if _tag(si) == "si":
                    shared.append(_clean("".join(_texts(si))))
        sheet_files: dict[str, str] = {}
        try:
            wb = ET.fromstring(z.read("xl/workbook.xml"))
            rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
            target = {r.get("Id"): r.get("Target") for r in rels}
            rid_attr = ("{http://schemas.openxmlformats.org/officeDocument/"
                        "2006/relationships}id")
            for sh in wb.iter():
                if _tag(sh) == "sheet":
                    t = (target.get(sh.get(rid_attr)) or "")
                    # The Target is relative to `xl/`, and normally reads
                    # "worksheets/sheet1.xml". Taking only the basename and
                    # re-prefixing "xl/" produced "xl/sheet1.xml", which is
                    # never in the archive - so EVERY sheet resolved to
                    # nothing and every workbook reported zero rows.
                    if t.startswith("/"):
                        sheet_files[sh.get("name") or f"Sheet{len(sheet_files)+1}"] = t.lstrip("/")
                    elif t:
                        sheet_files[sh.get("name") or f"Sheet{len(sheet_files)+1}"] = f"xl/{t}"
                    else:
                        sheet_files[sh.get("name") or f"Sheet{len(sheet_files)+1}"] = ""
        except Exception:
            pass
        for sheet_name, path in sheet_files.items():
            rows: list[list[str]] = []
            if path and path in names:
                try:
                    sroot = ET.fromstring(z.read(path))
                    for row in sroot.iter():
                        if _tag(row) != "row":
                            continue
                        cells = []
                        for c in row:
                            if _tag(c) != "c":
                                continue
                            v = None
                            inline = False
                            for child in c:
                                if _tag(child) == "v":
                                    v = child.text
                                    break
                                if _tag(child) == "is":
                                    # inlineStr: the text lives in <is><t>, and
                                    # there is no <v> to fall back to.
                                    v = "".join(_texts(child))
                                    inline = True
                                    break
                            if v is None:
                                cells.append("")
                            elif c.get("t") == "s" and not inline:
                                try:
                                    cells.append(shared[int(v)])
                                except Exception:
                                    cells.append("")
                            else:
                                cells.append(_clean(v))
                        if any(x for x in cells):
                            rows.append(cells)
                except Exception:
                    pass
            out["sheets"].append({"name": sheet_name, "rows": rows,
                                  "empty": not rows})
        blanks = [s["name"] for s in out["sheets"] if s["empty"]]
        if blanks:
            out["warnings"].append(
                f"empty sheet(s): {', '.join(blanks[:8])}")
    filled = sum(len(s["rows"]) for s in out["sheets"])
    out["title"] = out["sheets"][0]["name"] if out["sheets"] else ""
    out["subtitle"] = (f"{len(out['sheets'])} sheet(s), {filled} row(s)")
    return out


# ── DOCX ─────────────────────────────────────────────────────────────────

def _docx(blob: bytes) -> dict:
    out: dict = {"kind": "docx", "paragraphs": [], "tables": [], "warnings": []}
    with zipfile.ZipFile(_buf(blob)) as z:
        if "word/document.xml" not in z.namelist():
            out["warnings"].append("no word/document.xml in the file")
            return out
        root = ET.fromstring(z.read("word/document.xml"))
        body = next((c for c in root if _tag(c) == "body"), root)
        for el in body:
            t = _tag(el)
            if t == "p":
                txt = _clean("".join(_texts(el)))
                if txt:
                    out["paragraphs"].append(txt)
            elif t == "tbl":
                rows = []
                for tr in el.iter():
                    if _tag(tr) != "tr":
                        continue
                    cells = [_clean("".join(_texts(tc)))
                             for tc in tr if _tag(tc) == "tc"]
                    if any(cells):
                        rows.append(cells)
                if rows:
                    out["tables"].append(rows)
    words = sum(len(p.split()) for p in out["paragraphs"])
    out["title"] = out["paragraphs"][0][:80] if out["paragraphs"] else ""
    out["subtitle"] = (f"{len(out['paragraphs'])} paragraphs, {words} words")
    if words < 40:
        out["warnings"].append(
            f"only {words} words of text in the document - shorter than a "
            f"typical deliverable")
    return out


# ── PDF ──────────────────────────────────────────────────────────────────

def _pdf(blob: bytes) -> dict:
    out: dict = {"kind": "pdf", "pages": [], "warnings": []}
    try:
        from pypdf import PdfReader
        import io as _io
        r = PdfReader(_io.BytesIO(blob))
        for i, page in enumerate(r.pages, 1):
            try:
                txt = page.extract_text() or ""
            except Exception:
                txt = ""
            txt = _clean(txt)
            out["pages"].append({"n": i, "text": txt, "chars": len(txt)})
    except Exception as e:
        out["warnings"].append(f"could not read the PDF text: {e}")
    total = sum(p["chars"] for p in out["pages"])
    words = sum(len(p["text"].split()) for p in out["pages"])
    out["title"] = out["pages"][0]["text"][:80] if out["pages"] else ""
    out["subtitle"] = f"{len(out['pages'])} page(s), {words} words"
    if not out["pages"]:
        out["warnings"].append("no readable pages")
    # The old floor was 60 words. The measured median across 76 produced PDFs
    # was 209 words and nearly all were one page, so a 60-word floor never
    # fired and every thin document shipped quietly. 350 is below the honest
    # median of a real report and above a genuine one-pager.
    elif words < 350:
        out["warnings"].append(
            f"only {words} words across {len(out['pages'])} page(s) - "
            f"this is thin for a report (a one-page brief is ~400 words)")
    return out


def _buf(blob: bytes):
    import io
    return io.BytesIO(blob)


_DISPATCH = {"pptx": _pptx, "xlsx": _xlsx, "docx": _docx, "pdf": _pdf}


def preview(blob: bytes, content_type: str = "") -> dict:
    """Describe a rendered document. Never raises.

    Returns a dict with `kind`, a short `title`/`subtitle`, the structure
    for that kind, and `warnings` that name anything the user would
    otherwise only discover after downloading.
    """
    ct = (content_type or "").lower()
    kind = ""
    if "presentationml" in ct:
        kind = "pptx"
    elif "spreadsheetml" in ct:
        kind = "xlsx"
    elif "wordprocessingml" in ct:
        kind = "docx"
    elif "pdf" in ct:
        kind = "pdf"
    if not kind:
        if blob[:4] == b"%PDF":
            kind = "pdf"
        elif blob[:2] == b"PK":
            try:
                with zipfile.ZipFile(_buf(blob)) as z:
                    names = z.namelist()
                if any(n.startswith("ppt/") for n in names):
                    kind = "pptx"
                elif any(n.startswith("xl/") for n in names):
                    kind = "xlsx"
                elif any(n.startswith("word/") for n in names):
                    kind = "docx"
            except Exception:
                pass
    if not kind:
        return {"kind": "unknown", "title": "", "subtitle": "",
                "warnings": ["unrecognised file format"], "size_bytes": len(blob)}
    try:
        out = _DISPATCH[kind](blob)
    except Exception as e:
        return {"kind": kind, "title": "", "subtitle": "",
                "warnings": [f"preview failed: {e}"], "size_bytes": len(blob)}
    out["size_bytes"] = len(blob)
    return out