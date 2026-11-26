## Renderer: native (built-in PDF engine)

You are writing for the native renderer (the default when no LaTeX engine is
chosen). These rules are appended because the planner chose the native engine.

- **There is NO maths renderer on this path.** Write maths in plain text:
  `O(N²)`, `sqrt(d)`, `d_k`, `x^T`, `alpha`, `<=`, `->`, `1/Z`.
- **NEVER write `$...$`, `\sqrt{}`, `\alpha`, `\frac`** — they would print as
  literal markup.
- **Do not type Greek or maths symbols directly** (`α`, `√`, `≤`, `→`): the
  built-in PDF fonts lack them and an unsupported character shows as the WRONG
  LETTER rather than a box.
- Use `chart`, `table`, `stats`, `columns` and `callout` blocks for structure —
  this renderer draws them natively (charts as vectors, tables with rules).