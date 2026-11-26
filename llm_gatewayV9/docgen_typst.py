"""Typst PDF / slide writer over the docgen block model.

Typst is a modern markup-based typesetter (like LaTeX, but with a single
`typst compile` and no package install). It is wired here as an alternative
to the reportlab PDF path and as a slide engine (`slide: true` / `slide_size`),
so a deck can be a real PDF deck instead of a PPTX.

It needs the `typst` Python package (bundles the compiler); if the import
fails, `build_typst` raises DocGenError with the actionable reason rather than
emitting a broken file.

The `typst` package compiles a Typst source string to PDF bytes:
    import typst; typst.compile(source) -> bytes
"""
from __future__ import annotations

import re

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(title: str, fallback: str) -> str:
    s = _SAFE.sub("-", (title or "").strip()).strip("-")
    return (s or fallback)[:80]


def _esc(s: str) -> str:
    """Escape Typst special characters in body text (not markup we emit)."""
    s = str(s)
    for ch, rep in (("\\", "\\\\"), ("#", "\\#"), ("$", "\\$"),
                    ("@", "\\@"), ("<", "\\<"), (">", "\\>"),
                    ("[", "\\["), ("]", "\\]")):
        s = s.replace(ch, rep)
    return s


def _inline(s: str) -> str:
    # Reuse the same **bold** / `code` convention the other writers accept.
    s = _esc(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"*\1*", s)
    s = re.sub(r"`(.+?)`", r"``\1``", s)
    return s


def _blocks(spec: dict) -> list[dict]:
    return [b for b in (spec.get("blocks") or []) if isinstance(b, dict)]


def build_typst(spec: dict) -> tuple[bytes, str]:
    try:
        import typst as _typst
    except Exception as e:  # pragma: no cover
        raise ValueError(
            "the `typst` package is not installed (pip/uv install typst); "
            "it bundles the Typst compiler") from e

    # Slide mode: a 16:9 landscape deck. Triggered by `slide: true` or a
    # `slide_size` (the same signal the pptx builder uses).
    slide = bool(spec.get("slide") or spec.get("slide_size"))

    title = str(spec.get("title") or "Document")
    author = str(spec.get("author") or "")
    src: list[str] = []

    if slide:
        # A 16:9 landscape deck. Each level-1 heading starts a new slide.
        src.append("#set page(width: 16in, height: 9in, margin: 0.6in)")
        src.append("#set text(size: 26pt)")
        src.append("#set heading(numbering: none)")
    else:
        src.append("#set document(title: %s, author: %s)"
                   % (_typst_str(title), _typst_str(author) if author else '()'))
        src.append("#set page(numbering: \"1\")")
        src.append("#set heading(numbering: \"1.\")")
        src.append("#set text(size: 11pt)")
    src.append("")

    if not slide:
        src.append(f"= {_esc(title)}")
        if spec.get("subtitle"):
            src.append(f"_{_esc(spec['subtitle'])}_")
        src.append("")

    def add_blocks(blocks: list[dict]):
        for b in blocks:
            t = str(b.get("type") or "").lower()
            if t == "cover":
                src.append(f"= {_esc(b.get('title') or title)}")
                if b.get("subtitle"):
                    src.append(f"_{_esc(b['subtitle'])}_")
                src.append("")
            elif t == "heading":
                if slide and int(b.get("level") or 1) <= 1:
                    src.append("#pagebreak()")
                lvl = max(1, min(6, int(b.get("level") or 1)))
                src.append("=" * lvl + " " + _inline(b.get("text", "")))
                src.append("")
            elif t == "paragraph":
                src.append(_inline(b.get("text", "")))
                src.append("")
            elif t in ("bullets", "numbers"):
                for it in (b.get("items") or []):
                    src.append(("- " if t == "bullets" else "+ ") + _inline(it))
                src.append("")
            elif t == "quote":
                src.append(f"#quote[{_inline(b.get('text', ''))}]")
                src.append("")
            elif t == "callout":
                head = f"*{_esc(b['title'])}* " if b.get("title") else ""
                src.append(f"#block(fill: rgb(\"#eef2ff\"), inset: 8pt)"
                           f"[{head}{_inline(b.get('text', ''))}]")
                src.append("")
            elif t == "table":
                header = b.get("header") or []
                rows = b.get("rows") or []
                cols = len(header) or max((len(r) for r in rows), default=1)
                cells = []
                if header:
                    cells.append(", ".join(f"[{_inline(c)}]" for c in header))
                for r in rows:
                    cells.append(", ".join(f"[{_inline(c)}]" for c in r))
                src.append(f"#table(columns: {cols}, ..({', '.join(cells)}))")
                src.append("")
            elif t == "pagebreak":
                src.append("#pagebreak()")
                src.append("")
            # chart / image / stats / columns degrade to their text form here;
            # a full deck engine (Touying) is a follow-on.

    add_blocks(_blocks(spec))

    if spec.get("references"):
        src.append("= References")
        src.append("")
        for i, r in enumerate(spec["references"], 1):
            if isinstance(r, dict):
                src.append(f"{i}. {_esc(r.get('authors',''))} ({_esc(r.get('year',''))}). "
                           f"{_esc(r.get('title',''))}.")
            else:
                src.append(f"{i}. {_esc(r)}")
        src.append("")

    source = "\n".join(src)
    # typst.compile() takes a PATH to a .typ file, not the source string
    # (passing the source raised "filename ... syntax is incorrect"). Write a
    # temp file, compile it, and remove it.
    import os
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".typ", prefix="aria-docgen-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(source)
        try:
            pdf = _typst.compile(path)
        except Exception as e:
            raise ValueError(f"typst compile failed: {e}") from e
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    name = _slug(title, "document") + (".typst.pdf" if slide else ".pdf")
    return pdf, name


def _typst_str(s: str) -> str:
    s = str(s).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


BUILDERS = {
    "typst": (build_typst, "application/pdf"),
    "typst-pdf": (build_typst, "application/pdf"),
}