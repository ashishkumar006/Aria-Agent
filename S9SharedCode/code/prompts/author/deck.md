## Renderer: PPTX deck (editable PowerPoint)

You are writing an editable PowerPoint deck, not a prose document. These rules
are appended because the planner chose the pptx format.

- **One idea per slide.** Each `heading` block is a slide title; keep it short.
- **Bullets are short** — a few words each, not sentences. No dense paragraphs
  on a slide.
- **Use the right block for the shape:** `stats` for headline numbers, `chart`
  for data (renders as a NATIVE, editable chart in PowerPoint), `table` for
  comparisons, `columns` for side-by-side points.
- **`note` blocks become speaker notes** — put the detail you would say aloud
  there, not on the slide.
- The format is forced to `pptx` for you; `render_document` returns a real
  `.pptx`.