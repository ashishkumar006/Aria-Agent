# Document formats: what Aria has, what it lacks, and what to add

> **Status (implemented):** Aria now also exports **Markdown, HTML, CSV, JSON,
> EPUB, ODT, ODS** (serialisations of the block model in `docgen_markup.py`) and
> **Typst** PDF / slide decks (`docgen_typst.py`). Charts gained **scatter** and
> **area** kinds (matplotlib, embedded in every format). An `image` block now
> accepts a `document` id, an `art:` handle, or a sandbox `path` — so a plot a
> sandbox/coder node writes with matplotlib/matlab drops straight into a
> PDF/LaTeX/DOCX/PPTX. The **PPTX preview** rasterises real slide images with
> PowerPoint COM (`pptx_raster.py`, `/api/…/slides`) and falls back to a text
> outline when PowerPoint is absent. Still open: tracked changes / comments in
> DOCX, and richer Touying slide themes (base Typst slides work).

Researched November 2026 against the requirements of an agent that produces
deliverables rather than chat text.

Today Aria renders **PDF** (reportlab + LaTeX), **PPTX**, **DOCX**, **XLSX**,
plus **A2UI** interactive surfaces in the console. That is four file formats.
It reads **PDF, DOCX, XLSX, PPTX, images and text** for retrieval.

This note covers the gap, in priority order, with the library each format
needs and the reason it earns its place.

---

## Tier 1 — missing formats that the same code should already produce

### 1. ODT / ODS (OpenDocument)
**Why:** the LibreOffice-native equivalents of DOCX/XLSX. Common in
government, education and EU organisations, and the only acceptable office
format in some of them.
**Tooling:** `odfpy` (pure Python) for writing; `odfdo` for richer editing.
**Effort:** low — our block vocabulary maps the same way DOCX does.

### 2. Markdown + HTML bundles
**Why:** the cheapest possible deliverable, and the one that survives a diff,
a git review or a paste into a wiki. An agent that can only emit binary
artefacts makes review hard.
**Tooling:** already present in spirit (`_strip_md`, `_rich`); needs a writer
that emits markdown with the same block structure, and an HTML template for
self-contained output (inline CSS, base64 images).
**Effort:** low.

### 3. CSV / TSV and JSON export of tables
**Why:** every research run produces tables that end up trapped in a PDF. A
"give me the data" follow-up is common and currently means re-doing the work.
**Tooling:** stdlib `csv`, `json`.
**Effort:** trivial.

### 4. EPUB
**Why:** long-form documents — the ones a user actually reads on a phone or a
reader. A 100-page report as PDF is a print artefact; as EPUB it is readable.
**Tooling:** `ebooklib` (pure Python) or a hand-built EPUB container (it is
a zip of XHTML plus an OPF manifest — no dependency strictly needed).
**Effort:** moderate — needs the block vocabulary mapped to XHTML and a
navigable spine from the heading tree we already compute for the TOC.
**Constraint:** the LaTeX/typeset path is irrelevant here; the block model
is the input, not the TeX source.

### 5. PPTX → PDF (a print of an existing deck)
**Why:** the request "send me the slides as a PDF" is extremely common, and
often arrives *after* a deck exists.
**Tooling:** PowerPoint COM on Windows (already used for visual QA), or
LibreOffice headless when available. Must degrade honestly when neither is
installed.
**Effort:** moderate, with a platform dependency to declare.

---

## Tier 2 — formats with a real authoring story

### 6. Diagrams as a first-class block (Mermaid / Graphviz / SVG)
**Why:** a document about systems needs a diagram more often than a photo.
We have `chart` and `image`; there is no `diagram` block.
**Tooling:** `mermaid-cli` (Node), `graphviz` (binary), or plain SVG emitted
by us. Mermaid is the best target for an LLM: the source language is small and
well-known to models.
**Effort:** moderate — a new block type plus a render-to-PNG/SVG step before
the document assembler, exactly like the matplotlib chart step.

### 7. MathML / embedded equation objects in DOCX
**Why:** a Word document with `d_k` as text is a Word document nobody can edit
the maths in.
**Tooling:** `python-docx` cannot write OMML directly; the usual route is
LaTeX → OMML via `latex2mathml` + an XSLT (MML2OMML.XSL ships with Office).
**Effort:** high; worth doing only if DOCX with maths is a real request.

### 8. Scanned PDF / OCR reading
**Why:** reading, not writing. A scanned page currently yields no text.
**Tooling:** `tesseract` via `pytesseract`, or the OS OCR API. We already have
`vision_file` for images, so a PDF page can be rasterised (PyMuPDF) and sent
to the same vision path.
**Effort:** moderate, and arguably `vision_file` should own it.

---

## Tier 3 — deliberately not worth it now

| Format | Why not |
|---|---|
| **ODP/ODG** (Impress/Draw) | Almost nobody asks; LibreOffice reads our PPTX/DOCX anyway. |
| **RTF** | A legacy interchange format with no authoring upside. |
| **Pages / Keynote** | Proprietary, no Python writer, and Office export covers the need. |
| **LaTeX source (.tex) as a deliverable** | Interesting for a technical user, but the PDF is what they want; expose it only if someone asks. |
| **Jupyter notebooks** | A coding deliverable, and `coder` already writes files into the workspace. |
| **ePub Fixed Layout / comics** | Niche; the reflowable EPUB covers reading. |
| **iCal, ICS calendars** | Not a document. The calendar integration already creates events properly. |

---

## Recommended order

1. **Markdown + HTML** — cheapest, unlocks review and pasting.
2. **CSV/JSON table export** — turns dead tables into data.
3. **EPUB** — the long-form reader format, reusing the heading tree.
4. **PPTX → PDF** — the most-requested conversion.
5. **ODT/ODS** — the open-standards twin of the Office formats.
6. **Mermaid diagrams** — the biggest *quality* gain of the six.

The first four are mostly plumbing over the block model we already have; only
diagrams add new rendering.

---

## What this means for the renderer

The block vocabulary is the asset. Every format above is a projection of the
same `blocks` list, so the work is a writer per format rather than a new
pipeline — with two exceptions that need real rendering: **diagrams** (a new
block type) and **PPTX → PDF** (a print step that needs an office runtime).

One caveat learned the hard way on the PDF path, and worth applying to all of
them: **a format that cannot typeset something must say so, not silently
degrade.** The LaTeX fallback prints a line to the log; a DOCX that cannot do
maths should put the equation in an obvious, honest form rather than half-set
it.

---

### Sources

- [Universal Document Converter — Microsoft CAT Agent Skills](https://microsoft.github.io/cat-agent-skills/skills/doc-format-converter/)
- [edit2docs — agent-native DOCX/XLSX/PPTX engine](https://pypi.org/project/edit2docs/)
- [Docxtemplater](https://docxtemplater.com/)
- [Carbone — AI document generation](https://carbone.io/features/ai-document-generation.html)
- [Syncfusion Document SDK](https://www.syncfusion.com/document-sdk)
- [Best data extraction tools for AI teams](https://www.firecrawl.dev/blog/best-data-extraction-tools)
