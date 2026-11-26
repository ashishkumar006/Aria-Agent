## Renderer: LaTeX (`typeset="latex"`)

You are writing for the LaTeX renderer. These rules are appended because the
planner chose the LaTeX engine for this document; they OVERRIDE any general
maths note above.

- **All maths goes inside maths delimiters, and nothing else is maths.**
  Inline: `$\sqrt{d_k}$`, `$O(N^2)$`, `$\alpha \le \beta$`.
  Display: its own `paragraph` whose text is `\[ ... \]`:
      {"type": "paragraph",
       "text": "\\[ \\mathrm{softmax}(QK^\\top/\\sqrt{d_k})V \\]"}
- **Write real TeX commands** — `\sqrt`, `\frac`, `\mathrm`, `\alpha`, `\sum`,
  `\int`, `\le`, `\times`, `\mathcal{L}`. In the JSON you send, a backslash is
  written `\\`, so `\\sqrt` in the JSON is `\sqrt` in the formula. Never write
  four backslashes (`\\\\sqrt`).
- **Do NOT write Unicode symbols for maths** (`α`, `≤`, `ℒ`, `×`, `→`) — use
  the TeX command. Outside maths those glyphs are not in the font.
- **Do NOT write plain-text maths** (`sqrt(d)`, `x^T`, `d_k`, `1/Z`) on this
  path — it will not be typeset.
- A single stray Unicode symbol, or an unescaped `_` in prose, can make the
  WHOLE document fail to compile. Keep all maths inside `$...$` / `\[...\]`.
- You get real radicals, subscripts, numbered sections, proper table rules and
  a bibliography. `render_document` is called with `typeset="latex"` for you.