"""Real LaTeX typesetting for PDF documents.

Why a second renderer
---------------------
`docgen.build_pdf` draws with reportlab and base-14 fonts. That is the right
tool for a briefing, and the wrong one for anything with maths: reportlab has
no maths typesetter, cp1252 has no subscripts, and the workaround (`_math_to_text`)
prints `sqrt(d_k)` where LaTeX prints a real radical with a real subscript. A
live 23-page report on LLMs leaked 112 literal `$` before that workaround
existed, and after it the notation was merely *readable* rather than *set*.

This module produces a `.tex` document from the SAME block vocabulary and
compiles it with tectonic, which is a single self-contained binary that
downloads its own packages on first use. Nothing else in the project needs a
TeX distribution.

Contract
--------
- Opt-in only. `build_pdf` keeps its reportlab path unless the spec asks for
  LaTeX (`"engine": "latex"` / `"typeset": "latex"`) AND an engine is present,
  so existing documents and every test that pins their geometry are untouched.
- Honest fallback. When the spec asks for LaTeX and no engine is found,
  `available()` reports why and the caller falls back with a warning rather
  than failing a run that would otherwise have produced a document.
- The block vocabulary is the schema's, not a second one: a document that
  renders in reportlab renders here, and an unknown block is skipped loudly.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

ENGINE_ENV = "ARIA_TECTONIC"          # explicit binary path, for packaging
_CACHE_DIR = Path(__file__).resolve().parent / "tools" / "cache"


# ── engine discovery ────────────────────────────────────────────────────────

def engine_path() -> str:
    """Path to a TeX engine, or "".

    Order: an explicit `ARIA_TECTONIC`, the copy shipped under `tools/bin`,
    then `tectonic`/`xelatex`/`pdflatex` on PATH. `latexmk` alone is not
    enough — it is a driver, not a typesetter.
    """
    explicit = (os.environ.get(ENGINE_ENV) or "").strip()
    if explicit and Path(explicit).is_file():
        return explicit
    here = Path(__file__).resolve().parent
    for name in ("tectonic.exe", "tectonic"):
        local = here / "tools" / "bin" / name
        if local.is_file():
            return str(local)
    for name in ("tectonic", "xelatex", "pdflatex"):
        got = shutil.which(name)
        if got:
            return got
    return ""


def available() -> tuple[bool, str]:
    """(usable, human reason). Never raises."""
    exe = engine_path()
    if not exe:
        return False, ("no TeX engine found — install tectonic or set "
                       f"{ENGINE_ENV}")
    return True, os.path.basename(exe)


# ── LaTeX escaping ──────────────────────────────────────────────────────────

_ESC = {
    "\\": r"\textbackslash{}",
    "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
    "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\^{}",
}

# Inline markdown we translate to LaTeX equivalents. `$...$` is deliberately
# left ALONE: on this path it is maths, not literal text, so it must reach TeX
# untouched.
_MD_CODE = re.compile(r"`([^`]+)`")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_ITAL = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_URL_BARE = re.compile(r"(?<![\w/)])https?://[^\s)]+")


def _tex_escape(s: str) -> str:
    """Escape text for LaTeX, preserving maths spans and markdown emphasis."""
    # Maths first: split on `$...$` so nothing inside is escaped into soup.
    parts = re.split(r"(\$[^$\n]+?\$)", str(s or ""))
    out: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 1:                  # inside $...$
            out.append(part)
            continue
        # Markdown emphasis inside prose only.
        part = _MD_LINK.sub(r"\\href{\2}{\1}", part)
        part = _MD_CODE.sub(r"\\texttt{\1}", part)
        part = _MD_BOLD.sub(r"\\textbf{\1}", part)
        part = _MD_ITAL.sub(r"\\emph{\1}", part)
        # A bare URL inside \texttt must still survive as text.
        buf: list[str] = []
        for ch in part:
            buf.append(_ESC.get(ch, ch))
        out.append("".join(buf))
    return "".join(out)


# Unicode maths symbols the ec/lmodern fonts do not carry. Left as-is they
# produce "Missing character" warnings and, when they sit in prose, a fatal
# "Missing $ inserted". Map the common ones to LaTeX or text equivalents.
_UNICODE_MAP = {
    "\u2264": "$\\leq$", "\u2265": "$\\geq$", "\u2260": "$\\neq$",
    "\u2248": "$\\approx$", "\u00b1": "$\\pm$", "\u00d7": "$\\times$",
    "\u2192": "$\\to$", "\u2190": "$\\leftarrow$", "\u21d2": "$\\Rightarrow$",
    "\u221e": "$\\infty$", "\u2211": "$\\sum$", "\u220f": "$\\prod$",
    "\u2212": "$-$", "\u2013": "--", "\u2014": "---",
    "\u2018": "`", "\u2019": "'", "\u201c": "``", "\u201d": "''",
    "\u2026": "\\ldots{}",
    "\u2112": "$\\mathcal{L}$", "\u2113": "$\\ell$",
    "\u00b2": "\\textsuperscript{2}", "\u00b3": "\\textsuperscript{3}",
    "\u03b1": "$\\alpha$", "\u03b2": "$\\beta$", "\u03b3": "$\\gamma$",
    "\u03b4": "$\\delta$", "\u03b5": "$\\epsilon$", "\u03b8": "$\\theta$",
    "\u03bb": "$\\lambda$", "\u03bc": "$\\mu$", "\u03c0": "$\\pi$",
    "\u03c3": "$\\sigma$", "\u03c6": "$\\phi$", "\u03c9": "$\\omega$",
    "\u0394": "$\\Delta$", "\u03a9": "$\\Omega$", "\u2202": "$\\partial$",
    "\u2207": "$\\nabla$", "\u222b": "$\\int$", "\u221a": "$\\sqrt{}$",
}


def _tex_inline(s: str) -> str:
    """Escape, then re-open the markup we just escaped.

    `_tex_escape` escapes the whole run, which would turn the `\\href{...}`
    it just inserted into literal text. So markup is applied AFTER escaping,
    in one pass over the escaped string.

    CURRENCY: `$5 per seat and $6 per user` must not be read as maths. Two
    dollars on one line is a price, not an equation, so a `$` run only counts
    as maths when it CLOSES on the same line and does not look like money -
    the same rule `docgen._math_to_text` uses for the reportlab path. Getting
    this wrong on this path is worse than on that one: TeX turns
    `5 per seat and $6` into italic run-together words, which is what a live
    draft did.
    """
    raw = str(s or "")
    # The author (and JSON) frequently OVER-escapes display maths: `\\[ ... \\]`
    # arrives as two backslashes. Normalise to `\[ ... \]` so the maths
    # protection below matches it as a span; otherwise one backslash is left
    # outside and escaped into \textbackslash{}, which breaks the whole render
    # ("Missing { inserted") and silently falls back to ReportLab.
    raw = (raw.replace("\\\\[", "\\[").replace("\\\\]", "\\]")
              .replace("\\\\(", "\\(").replace("\\\\)", "\\)"))
    # Replace Unicode maths symbols the fonts lack with LaTeX/text equivalents
    # BEFORE the maths protection, so the inserted `$...$` is held as maths.
    for _u, _r in _UNICODE_MAP.items():
        if _u in raw:
            raw = raw.replace(_u, _r)
    # 1. protect maths
    maths: list[str] = []

    def _hold(m):
        span = m.group(0)
        # Collapse OVER-ESCAPED commands inside maths: the author/JSON often
        # emits `\\mathrm`, `\\sqrt`, `\\top` (two backslashes). TeX reads `\\`
        # as a line break, so the command breaks ("Missing { inserted"). Reduce
        # `\\` + letter to `\` + letter; a real `\\` line break (followed by
        # `[`, `]` or space) is left alone.
        span = re.sub(r"\\\\([a-zA-Z])", r"\\\1", span)
        maths.append(span)
        return f"\x00M{len(maths) - 1}\x00"

    # Braced groups are TeX syntax in ORDINARY prose too, not only in maths:
    # `1 in 5{,}000` and `10{,}000` are how a LaTeX writer writes a thousands
    # separator, and escaping the braces printed "5{,}000" verbatim. Hold any
    # complete `{...}` run so its braces survive as TeX.
    held_groups: list[str] = []

    def _hold_group(m):
        # Hold the braces but ESCAPE the content: a held group used to be
        # reinserted verbatim, so a `_` inside `{N, D_tr | ...}` reached TeX
        # unescaped and broke the run ("Missing $ inserted"). Escape the inner
        # text the same way ordinary prose is escaped, keep the braces.
        inner = m.group(0)[1:-1]
        esc = "".join(_ESC.get(c, c) for c in inner)
        held_groups.append("{" + esc + "}")
        return f"\x00G{len(held_groups) - 1}\x00"

    held = re.sub(r"\\\[(.+?)\\\]", _hold, raw, flags=re.S)          # \[...\]
    held = re.sub(r"\\\((.+?)\\\)", _hold, held, flags=re.S)        # \(...\)
    held = re.sub(r"\$\$(.+?)\$\$", _hold, held, flags=re.S)        # $$...$$
    held = re.sub(r"(?<![\d$\w])\$(?!\s)([^$\n]{1,160}?)(?<![\s$])\$(?!\d)",
                  _hold, held)                                      # $...$
    # THEN brace groups, so a `{...}` INSIDE maths is already held (as part of
    # the maths span) and never escaped. The old order ran braces FIRST, which
    # escaped math content inside braces and mangled `QK^\top` into
    # `\textbackslash` — one of the reasons LaTeX renders silently fell back.
    held = re.sub(r"\{[^{}\n]{0,40}\}", _hold_group, held)
    # 2. protect markdown structure, remember the pieces
    tokens: list[tuple[str, str]] = []

    def _tok(fmt: str, body: str) -> str:
        tokens.append((fmt, body))
        return f"\x00T{len(tokens) - 1}\x00"

    held = _MD_LINK.sub(lambda m: _tok("link", f"{m.group(1)}|{m.group(2)}"), held)
    held = _MD_CODE.sub(lambda m: _tok("code", m.group(1)), held)
    held = _MD_BOLD.sub(lambda m: _tok("bold", m.group(1)), held)
    held = _MD_ITAL.sub(lambda m: _tok("ital", m.group(1)), held)
    # 3. escape what remains (prose)
    out = "".join(_ESC.get(ch, ch) for ch in held)
    # 4. re-insert structure, escaping each body
    for idx in range(len(tokens) - 1, -1, -1):
        fmt, body = tokens[idx]
        esc = lambda t: "".join(_ESC.get(c, c) for c in t)  # noqa: E731
        if fmt == "link":
            label, href = body.split("|", 1)
            rep = f"\\href{{{href}}}{{{esc(label)}}}"
        elif fmt == "code":
            rep = f"\\texttt{{{esc(body)}}}"
        elif fmt == "bold":
            rep = f"\\textbf{{{esc(body)}}}"
        else:
            rep = f"\\emph{{{esc(body)}}}"
        out = out.replace(f"\x00T{idx}\x00", rep)
    for idx in range(len(maths) - 1, -1, -1):
        out = out.replace(f"\x00M{idx}\x00", maths[idx])
    for idx in range(len(held_groups) - 1, -1, -1):
        out = out.replace(f"\x00G{idx}\x00", held_groups[idx])
    return out


# ── document preamble ───────────────────────────────────────────────────────

_PAPER = {"a4": "a4paper", "a5": "a5paper", "b5": "b5paper",
          "letter": "letterpaper", "legal": "legalpaper",
          "tabloid": "tabloidpaper"}

_STYLE = {
    "report":     dict(font="lmodern", size="10.5pt", par="\\setlength{\\parindent}{0pt}\\setlength{\\parskip}{6pt}"),
    "brief":      dict(font="lmodern", size="11.5pt", par="\\setlength{\\parindent}{0pt}\\setlength{\\parskip}{8pt}"),
    "memo":       dict(font="lmodern", size="11pt",   par="\\setlength{\\parindent}{0pt}\\setlength{\\parskip}{6pt}"),
    "academic":   dict(font="lmodern", size="11.5pt", par="\\setlength{\\parindent}{1.5em}\\setlength{\\parskip}{0pt}"),
    "whitepaper": dict(font="lmodern", size="10.8pt", par="\\setlength{\\parindent}{0pt}\\setlength{\\parskip}{7pt}"),
    "manual":     dict(font="lmodern", size="10.2pt", par="\\setlength{\\parindent}{0pt}\\setlength{\\parskip}{6pt}"),
    "newsletter": dict(font="lmodern", size="9.8pt",  par="\\setlength{\\parindent}{0pt}\\setlength{\\parskip}{5pt}"),
    "technical":  dict(font="lmodern", size="9.6pt",  par="\\setlength{\\parindent}{0pt}\\setlength{\\parskip}{5pt}"),
    "book":       dict(font="lmodern", size="10.6pt", par="\\setlength{\\parindent}{1.5em}\\setlength{\\parskip}{0pt}"),
    "plain":      dict(font="lmodern", size="11pt",   par="\\setlength{\\parindent}{0pt}\\setlength{\\parskip}{6pt}"),
    "lecture":    dict(font="lmodern", size="10.9pt", par="\\setlength{\\parindent}{0pt}\\setlength{\\parskip}{7pt}"),
    "exambook":   dict(font="lmodern", size="10.6pt", par="\\setlength{\\parindent}{0pt}\\setlength{\\parskip}{6pt}"),
    "coursework": dict(font="lmodern", size="10.7pt", par="\\setlength{\\parindent}{0pt}\\setlength{\\parskip}{6pt}"),
}


def _preamble(spec: dict) -> tuple[str, str]:
    """(geometry options, style block) for article/none base classes."""
    style_name = str(spec.get("style") or "report").strip().lower()
    st = _STYLE.get(style_name, _STYLE["report"])
    page = str(spec.get("page_size") or "a4").strip().lower()
    paper = _PAPER.get(page, "a4paper")
    geo = [paper, "margin=22mm"]
    if str(spec.get("orientation") or "").lower() == "landscape":
        geo.append("landscape")
    cols = 2 if (style_name == "newsletter"
                 or int(spec.get("columns") or 0) == 2) else 1
    if cols == 2:
        geo.append("twocolumn")
    return ",".join(geo), st["size"], st["par"]


def _tex_doc(spec: dict, blocks: list[dict], refs: list[str],
             figure_files: dict[int, str]) -> str:
    title = str(spec.get("title") or "").strip()
    subtitle = str(spec.get("subtitle") or "").strip()
    author = str(spec.get("author") or "").strip()
    geo, size, par = _preamble(spec)
    page = str(spec.get("page_size") or "a4").strip().lower()
    paper = _PAPER.get(page, "a4paper")
    landscape = str(spec.get("orientation") or "").lower() == "landscape"
    cols = 2 if (str(spec.get("style") or "").lower() == "newsletter"
                 or int(spec.get("columns") or 0) == 2) else 1

    L = [
        "\\documentclass[%s]{%s}" % (size, "article"),
        "\\usepackage[%s]{%s}" % (
            ",".join([x for x in [paper, "margin=22mm",
                                  "landscape" if landscape else ""] if x]),
            "geometry"),
        "\\usepackage[T1]{fontenc}",
        "\\usepackage[utf8]{inputenc}",
        "\\usepackage{lmodern}",
        "\\usepackage{amsmath,amssymb,amsfonts,mathtools}",
        "\\usepackage{booktabs,longtable,array,tabularx}",
        "\\usepackage{graphicx}",
        "\\usepackage{caption}",
        "\\captionsetup{font=small,labelfont=bf,labelsep=period}",
        "\\usepackage{xcolor}",
        "\\usepackage{enumitem}",
        "\\usepackage[hidelinks]{hyperref}",
        "\\usepackage{fancyhdr}",
        "\\usepackage[normalem]{ulem}",
    ]
    if cols == 2:
        L += ["\\twocolumn"]
    L += [par,
          "\\definecolor{accent}{HTML}{%s}" % str(
              (spec.get("accent") or "5B4BD6")).lstrip("#"),
          "\\pagestyle{fancy}\\fancyhf{}",
          "\\fancyhead[L]{\\small\\color{gray}%s}" % _tex_inline(title)[:80] if title else "\\fancyhead[L]{}",
          "\\fancyfoot[C]{\\small\\thepage}",
          "\\renewcommand{\\headrulewidth}{0.4pt}",
          "\\setlength{\\emergencystretch}{3em}",
          "\\begin{document}"]
    if title:
        L += ["\\begin{center}",
              "{\\LARGE\\bfseries\\color{accent} %s}\\\\[6pt]" % _tex_inline(title),]
        if subtitle:
            L += ["{\\large %s}\\\\[4pt]" % _tex_inline(subtitle)]
        if author:
            L += ["{\\normalsize %s}" % _tex_inline(author)]
        L += ["\\end{center}", "\\vspace{6pt}"]

    # ── blocks ──────────────────────────────────────────────────────────
    list_open = False
    sect = [0, 0, 0]

    def _close_list():
        nonlocal list_open
        if list_open:
            L.append("\\end{itemize}")
            list_open = False

    def _head(text: str, level: int):
        _close_list()
        lvl = max(1, min(3, int(level or 1)))
        cmd = {1: "section", 2: "subsection", 3: "subsubsection"}[lvl]
        sect[0 if lvl == 1 else (1 if lvl == 2 else 2)] += 1
        if lvl == 1:
            sect[1] = sect[2] = 0
        elif lvl == 2:
            sect[2] = 0
        L.append("\\%s{%s}" % (cmd, _tex_inline(text)))

    pending_figs: list[int] = []
    for bi, b in enumerate(blocks):
        t = b.get("type")
        if t != "bullets" and t != "numbers":
            _close_list()
        if t == "heading":
            _head(b.get("text") or "", b.get("level") or 1)
        elif t == "paragraph":
            L.append(_tex_inline(b.get("text") or ""))
            L.append("")
        elif t in ("bullets", "numbers"):
            _close_list()
            env = "enumerate" if t == "numbers" else "itemize"
            L.append("\\begin{%s}[leftmargin=1.4em,itemsep=2pt]" % env)
            for it in (b.get("items") or []):
                L.append("  \\item %s" % _tex_inline(it))
            L.append("\\end{%s}" % env)
        elif t == "quote":
            L += ["\\begin{quote}\\itshape %s\\end{quote}"
                  % _tex_inline(b.get("text") or "")]
        elif t == "callout":
            tone = str(b.get("tone") or "insight").lower()
            colour = "accent" if tone == "insight" else "red!70!black"
            title = b.get("title") or "Note"
            L += ["\\begin{quote}",
                  "\\noindent\\colorbox{%s!10}{\\parbox{\\dimexpr\\linewidth-2\\fboxsep}{"
                  "\\textbf{\\textcolor{%s}{%s}}\\\\[2pt] %s}}"
                  % (colour, colour, _tex_inline(title),
                     _tex_inline(b.get("text") or "")),
                  "\\end{quote}"]
        elif t == "table":
            head = b.get("header") or []
            rows = b.get("rows") or []
            ncol = max(len(head), max((len(r) for r in rows), default=0))
            if not ncol:
                continue
            spec_cols = "l" * ncol
            L.append("\\begin{center}\\small")
            L.append("\\begin{tabular}{@{}%s@{}}" % spec_cols)
            L.append("\\toprule")
            if head:
                L.append(" & ".join("\\textbf{%s}" % _tex_inline(h)
                                    for h in head[:ncol]) + " \\\\")
                L.append("\\midrule")
            for r in rows:
                cells = list(r or [])[:ncol]
                cells += [""] * (ncol - len(cells))
                L.append(" & ".join(_tex_inline(c) for c in cells) + " \\\\")
            L += ["\\bottomrule", "\\end{tabular}", "\\end{center}"]
        elif t == "chart":
            # A chart is drawn by matplotlib (docgen._chart_png) and arrives
            # here as a file. TeX only places it, so line weights and fonts
            # match the rest of the page.
            f = figure_files.get(bi)
            if not f:
                continue
            # matplotlib already draws the block's title inside the image, so
            # a caption repeating it printed the same sentence twice under
            # every chart. Only a caption that says something ELSE is kept.
            title_txt = str(b.get("title") or "").strip()
            cap = str(b.get("caption") or "").strip()
            L += ["\\begin{figure}[htbp]", "\\centering",
                  "\\includegraphics[width=0.86\\linewidth]{%s}" % f]
            if cap and cap != title_txt:
                L.append("\\caption*{%s}" % _tex_inline(cap))
            L.append("\\end{figure}")
        elif t == "image":
            f = figure_files.get(bi)
            if not f:
                continue
            cap = str(b.get("caption") or "")
            width = float(b.get("width_mm") or 150.0) / 170.0
            L += ["\\begin{figure}[htbp]", "\\centering",
                  "\\includegraphics[width=%.2f\\linewidth]{%s}"
                  % (min(1.0, width), f)]
            if cap:
                L.append("\\caption{%s}" % _tex_inline(cap))
            L.append("\\end{figure}")
        elif t == "stats":
            items = b.get("items") or []
            if items:
                L.append("\\begin{center}")
                for it in items:
                    L.append("{\\large\\bfseries %s}\\quad %s\\\\"
                             % (_tex_inline(it.get("value") or ""),
                                _tex_inline(it.get("label") or "")))
                L.append("\\end{center}")
        elif t == "columns":
            items = b.get("items") or []
            if items:
                L.append("\\begin{center}")
                L.append("\\begin{tabular}{@{}%s@{}}"
                         % ("p{0.3\\linewidth}" * len(items)))
                L.append(" & ".join("\\textbf{\\textcolor{accent}{%s}}"
                                    % _tex_inline(it.get("title") or "")
                                    for it in items) + " \\\\[4pt]")
                L.append(" & ".join(_tex_inline(it.get("body") or "")
                                    for it in items) + " \\\\")
                L += ["\\end{tabular}", "\\end{center}"]
        elif t == "pagebreak":
            L.append("\\clearpage")
        elif t == "cover":
            cover_title = b.get("title") or title
            L += ["\\begin{titlepage}\\centering",
                  "\\vspace*{0.22\\textheight}",
                  "{\\Huge\\bfseries\\color{accent} %s}\\\\[14pt]"
                  % _tex_inline(cover_title),
                  ]
            if b.get("subtitle"):
                L.append("{\\Large %s}\\\\[10pt]"
                         % _tex_inline(b["subtitle"]))
            for m in (b.get("meta") or []):
                L.append("{\\large %s}\\\\" % _tex_inline(m))
            L += ["\\vfill", "\\end{titlepage}"]
        elif t == "note":
            continue                     # speaker notes: no page presence

    _close_list()

    if refs:
        L += ["", "\\begin{thebibliography}{%d}" % max(9, len(refs))]
        for r in refs:
            L.append("\\bibitem{ref%s} %s" % (len(L), _tex_inline(r)))
        L.append("\\end{thebibliography}")
    if "\\begin{document}" in L:
        L.append("\\end{document}")
    return "\n".join(L) + "\n"


# ── compile ─────────────────────────────────────────────────────────────────

_FONTCONFIG_FILE: Path | None = None


def _fontconfig_file() -> Path | None:
    """A minimal fontconfig file so tectonic's XeTeX can find system fonts.

    On Windows fontconfig has no default config, so XeTeX dies with
    "Fontconfig error: Cannot load default config file" and every
    `typeset="latex"` render failed and silently fell back to ReportLab.
    Point it at a config that includes the system font directory.
    """
    global _FONTCONFIG_FILE
    if _FONTCONFIG_FILE and _FONTCONFIG_FILE.is_file():
        return _FONTCONFIG_FILE
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fc = _CACHE_DIR / "fonts.conf"
        if not fc.is_file():
            font_dir = r"C:\Windows\Fonts" if os.name == "nt" else "/usr/share/fonts"
            fc.write_text(
                '<?xml version="1.0"?>\n'
                '<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">\n'
                '<fontconfig>\n'
                f'  <dir>{font_dir}</dir>\n'
                f'  <cachedir>{_CACHE_DIR}</cachedir>\n'
                '</fontconfig>\n', encoding="utf-8")
        _FONTCONFIG_FILE = fc
        return fc
    except Exception:
        return None


def compile_tex(tex: str, *, workdir: Path | None = None,
                timeout: float = 240.0) -> bytes:
    """Compile a full .tex source to PDF bytes. Raises RuntimeError."""
    exe = engine_path()
    if not exe:
        raise RuntimeError("no TeX engine available")
    own = workdir is None
    wd = Path(workdir or tempfile.mkdtemp(prefix="aria-tex-"))
    wd.mkdir(parents=True, exist_ok=True)
    src = wd / "document.tex"
    src.write_text(tex, encoding="utf-8")
    args = [exe]
    if "tectonic" in os.path.basename(exe).lower():
        args += ["--outdir", str(wd), "--keep-logs"]
        # The bundled tectonic is an older build that REJECTS --cache-dir
        # ("unexpected argument"); TEXMFVAR (set in the env below) already
        # points the cache at _CACHE_DIR, so the flag is unnecessary and was
        # failing every LaTeX render into a silent ReportLab fallback.
    else:
        args += ["-interaction=nonstopmode", "-halt-on-error",
                 "-output-directory", str(wd)]
    args.append(str(src))
    env = {**os.environ, "TEXMFVAR": str(_CACHE_DIR)}
    _fc = _fontconfig_file()
    if _fc:
        env["FONTCONFIG_FILE"] = str(_fc)
        env.setdefault("FONTCONFIG_PATH", str(_fc.parent))
    try:
        proc = subprocess.run(args, cwd=str(wd), capture_output=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"TeX timed out after {timeout:.0f}s")
    pdf = wd / "document.pdf"
    if proc.returncode != 0 or not pdf.is_file():
        err = (proc.stderr or b"").decode("utf-8", "replace")[-2500:]
        log = (wd / "document.log")
        if log.is_file():
            tail = log.read_text(encoding="utf-8", errors="replace")
            err += "\n" + tail[-2500:]
        # Keep the failing source so the offending line can be inspected
        # (the temp dir is otherwise removed).
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            (_CACHE_DIR / "last-failed.tex").write_text(tex, encoding="utf-8")
        except Exception:
            pass
        raise RuntimeError("TeX failed: " + (err.strip() or "no output"))
    blob = pdf.read_bytes()
    if own:
        shutil.rmtree(wd, ignore_errors=True)
    return blob


def render(spec: dict, blocks: list[dict], refs: list[str],
           figures: dict[int, tuple[bytes, str]] | None = None) -> bytes:
    """Spec + normalised blocks + figures -> PDF bytes.

    `figures` maps a block index to (bytes, extension). The bytes are written
    into the compile directory under a generated name, because TeX resolves
    `\\includegraphics` relative to the document and a path into the caller's
    temp tree would not survive packaging.
    """
    wd = Path(tempfile.mkdtemp(prefix="aria-tex-"))
    try:
        names: dict[int, str] = {}
        for idx, (blob, ext) in (figures or {}).items():
            name = f"fig{idx}.{'png' if ext in ('png', 'jpeg', 'jpg') else ext}"
            (wd / name).write_bytes(blob)
            names[idx] = name
        tex = _tex_doc(spec, blocks, refs, names)
        return compile_tex(tex, workdir=wd)
    finally:
        shutil.rmtree(wd, ignore_errors=True)
