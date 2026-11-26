# Slides from markup: which engine, and for what

> **Status (implemented):** the Typst engine now exists as `llm_gatewayV9/docgen_typst.py`
> (`format: "typst"`, `slide: true` → 16:9 PDF deck) and is wired into the docgen
> dispatcher, schema and `render_document`. Touying (the packaged slide template)
> is the recommended next step for richer themes. PPTX remains the editable path.

A survey of the "write slides as text, compile to something" tools, and a
decision for Aria. Researched November 2026.

The question your note raises is the right one: PDF documents got a LaTeX
path, so why does a deck still go through an Office library? The answer is
that slides-as-code is a genuinely different problem — a slide is a *canvas
with a layout*, not a *column of text* — and the tools fork sharply on whether
they want to own that canvas.

---

## The contenders

| Tool | Source language | Output | Runtime needed | Math | Diagrams / animation | Verdict for Aria |
|---|---|---|---|---|---|---|
| **LaTeX Beamer** | LaTeX | PDF | TeX (we now ship tectonic) | **Best in class** | TikZ (very strong), some overlays | Best maths fidelity; worst for anything the user will edit |
| **Typst + Touying** | Typst markup | PDF (and HTML in 2026) | One small binary | Strong (`$...$` native, real math) | Good; programmable | **Closest overall fit** — a modern TeX replacement with sane syntax |
| **reveal.js** | HTML/Markdown | HTML (+ PDF via headless Chrome) | Browser | Via KaTeX/MathJax | Very strong (fragments, transitions) | Best *presenting* experience, weakest *file* |
| **Marp** | Markdown | PDF, PPTX, HTML | Node | Via KaTeX | Limited | Fastest Markdown → deck; weak layout control |
| **Slidev** | Markdown + Vue | HTML, PDF, **PPTX export** | Node + Vue | Via KaTeX | Strong (Vue components) | Best developer deck; Vue coupling |
| **Quarto** | Markdown | HTML/PDF/PPTX/Beamer | Pandoc + engine | Via engine | Strong | Good glue, adds a big dependency |
| **Pandoc → PPTX** | Markdown | Editable PPTX | Pandoc | Weak | Weak | Only if the *editable file* is the goal |
| **our current builder** | JSON blocks | **Editable PPTX** | python-pptx | None | Native charts, shapes | Best for a file someone will open in PowerPoint |

---

## What each is actually good for

### LaTeX Beamer
The reference for technical talks. Equations are perfect, TikZ draws anything,
and the PDF is the deliverable. The cost is everything else: a custom theme is
an afternoon, the source is hostile to a non-LaTeX user, and **the result
cannot be edited in PowerPoint** — which matters, because a user who asks for a
deck usually means "something I can present *and* tweak".

**Use when:** the deck is a mathematical talk with no editability requirement.

### Typst + Touying
Typst is a LaTeX replacement with a markdown-family syntax that compiles in
milliseconds from a single binary — the same distribution model as the
`tectonic` binary we just added. Touying is the presentation package on top.
Math is native (`$...$`, proper typesetting), layouts are programmable, and the
2026 releases added HTML export.

**Use when:** you want Beamer-grade maths and layout control from something an
LLM can actually write correctly, and a PDF is acceptable.

**This is the best second engine for Aria**, for one reason beyond quality:
it is *the same shape of dependency as tectonic* — one binary, no toolchain —
so a "typeset the deck" switch would install cleanly next to the one we already
have.

### reveal.js / Slidev / Marp
All three own the *web presentation* slot: speaker notes, fragments, live
embeds, and browser delivery. Marp is the least ambitious (Markdown → slides,
very fast) and the most LLM-friendly; reveal.js is the most capable; Slidev is
the nicest for a developer audience.

The shared limitation is decisive for us: **the primary artefact is HTML.** A
PDF is a print of a browser, and PPTX export either does not exist (reveal.js,
Marp) or is a lossy re-write (Slidev). None of them gives an editable deck.

**Use when:** the deck is delivered over the web and nobody will open it in
PowerPoint.

### Quarto / Pandoc
Both are *converters*, not layout engines: they faithfully produce whatever
the downstream engine can do. Pandoc → PPTX is the only route to an editable
deck from Markdown, and its layout vocabulary is thin — which is exactly the
"converted report" look we already built blocks to avoid.

---

## The decision for Aria

**Keep the current PPTX builder as the default, and do not replace it.**

The reason is the one thing none of the research tools provide: an **editable
PowerPoint file**. Our builder produces native editable charts, real shapes,
real speaker notes and a preview read back from the file's own geometry. Beamer
and Typst cannot produce that; reveal.js and Marp cannot either. For the
default "make me a deck" request, editability wins.

**Add Typst/Touying as a second deck engine behind the same switch as the
PDF typesetter**, for:
- mathematically dense decks (the same audience that gets `typeset="latex"`),
- very large decks where compile speed matters,
- users who ask for a printed/handout PDF rather than a file to edit.

**Do not add Beamer.** We can already typeset with a TeX engine, so Beamer
would be a second TeX path with worse syntax for the model to write. Touying
covers the same need with a language an LLM writes far more reliably.

**Do not adopt reveal.js/Slidev/Marp** unless the product grows a "present in
the browser" surface. They solve a delivery problem Aria does not have yet.

---

## Where the maths actually matters for decks

Worth stating plainly, because it changes the recommendation: the deck builder
currently has **no maths path at all**. A statistics or ML talk gets `d_k` and
`O(N²)` as plain text, or a callout apologising for the notation. That is the
gap the Typst engine would close — and if it is deferred, the deck prompt
should say so explicitly rather than leaving the model to guess.

---

### Sources

- [Alternative to Beamer LaTeX: Practical Options for 2026](https://www.anygen.io/showcase/alternative-to-beamer-latex/index.html)
- [latex beamer alternative (2026)](https://www.anygen.io/showcase/latex-beamer-alternative/index.html)
- [touying — Typst Universe](https://typst.app/universe/package/touying/)
- [Slidev vs Marp vs Reveal.js 2026](https://www.pkgpulse.com/guides/slidev-vs-marp-vs-revealjs-code-first-presentations-2026)
- [Reveal.js Alternatives](https://deckary.com/blog/reveal-js-alternatives)
- [Compare Slaide vs Marp vs Slidev vs reveal.js](https://getslaide.com/compare/slaide-vs-marp-vs-slidev-vs-revealjs/)
- [The Best Slides-as-Code Presentation Tools for Developers](https://xergioalex.com/blog/best-slides-as-code-presentation-tools/)
- [Check out marp, presenterm and reveal.js (HN)](https://news.ycombinator.com/item?id=43817866)
