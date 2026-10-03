# Documents — implementation plan

Upload documents once; any chat or research run can then consult them.

## Decisions (confirmed)

| # | Decision |
|---|---|
| 1 | Disable = **hide**. Chunks are kept, excluded from recall. Re-enable is instant. |
| 2 | Chat toggle is **per conversation**. |
| 3 | File types: **PDF, DOCX, MD, TXT, HTML, CSV, XLSX**. More later. |
| 4 | **No size caps** (per-file, per-corpus, or page count). Pacing protects the model instead. |
| 5 | Search scope (restricting a run to specific documents) — **deferred**. |
| 6 | Chunk size chosen by **measurement**, not guesswork. |
| 7 | Ollama is the only embedder. If it is down the document goes to `blocked` and **never** `ready`; it resumes automatically when Ollama returns. |
| 8 | Every chunk records **which model** embedded it, enforced on read. |
| 9 | Embedding uses **batches of 16** (configurable up to 32) with a yield between batches. |

## Why batching, and what was measured

Ollama is the only embedder (`/v1/embedders` → `order: ["ollama"]`,
`nomic-embed-text`, dim 768; the Gemini fallback was removed). The gateway
today only calls `/api/embeddings`, which takes a **single** prompt, so
`/v1/embed` rejects a list.

Measured on this machine:

| Approach | Throughput |
|---|---|
| One chunk per request | 0.45 chunks/s |
| 4 concurrent requests | 1.47 chunks/s (flatlines — model saturated) |
| True batch of 8 | 2.3 chunks/s |
| True batch of 16 | 3.2 chunks/s |
| True batch of 32 | 4.0 chunks/s |

So the plan adds `POST /v1/embed/batch` backed by Ollama's `/api/embed`
array path. Batching is ~3x the ceiling of plain concurrency because it
amortises per-request overhead rather than merely overlapping it. A
300-chunk document is therefore ~90s, not ~4.5 minutes.

Contention was also measured: with 4 embeds in flight a chat call went from
~1.9s to ~2.9s and still returned 200. Bounded, not starvation. `/v1/embed`
is an async handler awaiting async httpx, so the gateway's event loop is
never blocked. Hence **one document at a time** (keeps a queue of uploads
ordered) and a **deliberate yield between batches**.

## Why the existing chunker is being replaced

`mcp_server._chunk_text` is `text.split()` with a 400-word sliding window.
That cuts mid-sentence, splits list items and table rows apart, orphans
headings from their sections, and treats a 4,000-word table as prose.

The new chunker is **hierarchy first, then sentence-aware packing**:

1. Headings are hard boundaries — a section never merges into a sibling.
2. Within a section, pack **whole sentences**. Split on `.!?` plus closing
   quotes, with guards so `Dr.`, `e.g.`, `3.14`, `U.S.` do not split.
3. **Overlap = whole trailing sentences** (~20% of target), never a fixed
   word slice.
4. Tables chunk by row groups with the **header repeated on every chunk** —
   numbers without column names are useless.
5. A table with **three or more named columns** is rendered with every cell
   carrying its column name (`- Thursday: …`) rather than pipe-delimited. A wide
   row otherwise makes the column a *positional* lookup: on the weekly-menu
   PDF, "Thursday breakfast" came back as the **Friday** column verbatim —
   correctly read, indexed one column off — because the model had to count
   seven pipes across a 700-character line. Naming the cells makes it a keyword
   lookup. Two-column tables keep the pipe form.
6. Each chunk keeps `heading_path`, `page`, `char_span` so a retrieved
   fragment stays understandable and citable. `page` must be read back out of
   the record's `value` — it is not an attribute of the record, so a
   `hasattr(hit, "page")` lookup is always false and silently reports `null`.
7. The heading path is prepended to each chunk's text.

### PDF tables

`pypdf`'s `extract_text()` flattens a table into one run of text: a weekly
menu arrives as the day names once in a header, followed by ~35 dishes with no
association between them. The document indexes and retrieves perfectly, and
the answer is still unanswerable — no retrieval or prompting strategy recovers
a mapping the parser threw away. (`extraction_mode="layout"` returns an empty
string for such files.)

PDF tables therefore go through **`pdfplumber`** with text-alignment settings,
plus a normalisation pass that:

- picks the header from the first three rows by count of short cells;
- infers logical columns from the header's x-positions, so a value wrapping
  across a column gap is rejoined rather than split into two columns;
- assigns body rows to the **nearest** row label. Labels are vertically
  centred, so they sit both above and below their own cells; grouping by "the
  next label below" put every DINNER cell into SNACKS and emitted a DINNER row
  with nothing in it;
- keeps a row label's own cells when the label shares a physical row with the
  first of its values.

`pdfplumber` is optional at runtime: without it the parser degrades to flat
text and says so in `warnings`.

**Known limitation:** text-alignment clustering splits some words at the PDF's
own intra-word spacing (`Chana` → `Ch ana`, `100gm` → `1 00gm`). Day-to-dish
association is unaffected and verified, and models read the fragments
correctly, but the cell text is uglier than the source. A word-level
(`extract_words`) rebuild was prototyped and rejected: it loses the column
structure the header inference depends on, so it would trade a cosmetic defect
for a correctness one.

## Reused, not rebuilt

| Existing | Role |
|---|---|
| `document` drawer + `DocSpan` | Chunk storage — the schema already exists |
| `document` already in `SESSION_DRAWERS` and `RECALL_ORDER` | Runs already recall it; no new plumbing |
| `flow.py:353` memory read | The delivery path into every skill |
| `POST /v1/embed` | Single-chunk embedding |

## Status machine

```
pending -> parsing -> chunking -> embedding -> ready
                                            \-> blocked  (Ollama down; auto-resume)
                                            \-> failed   (parse error; needs re-upload)
```

- **Enable is only permitted at `ready`.** A half-embedded document is worse
  than none: the user enables it and gets half-answers with no error.
- Resume is **idempotent** and its unit is **one batch**, so a crash re-does
  at most one batch, never the document.

## Filtering — the part that fails quietly

The enabled flag must reach the query. The registry passes an **allowlist of
enabled `doc_id`s** into the memory read; filtering is applied **after**
vector search (filtering before would gut recall), then results are
re-ranked. Model mismatch is enforced here too.

## Model provenance

`MemoryItem` currently stores `embedding: list[float]` with **no record of
which model produced it**. If the model ever changes, old and new vectors
would sit in the same FAISS index and be compared silently. This adds
`embed_model` + `embed_dim` to every record and excludes mismatched records
on read — the same idea as the existing dimension-drift guard, applied to
model identity.

## Stages

1. **Structure-aware chunker** + tests proving no mid-sentence cuts
2. **Parsers** for all 7 types, with fixture documents
3. **Benchmark** chunk sizes by retrieval quality; pick the best
4. **`/v1/embed/batch`**, 16 default / 32 optional, per-chunk fallback
5. **Model provenance** enforced on read
6. **Registry**: upload, status machine, Ollama-blocked + auto-resume
7. **Enable/disable filtering**, proven by a retrieval test
8. **Per-conversation chat toggle**, proven both ways
9. **Agent endpoints** (upload/list/detail/toggle/delete) + tests
10. **Console `/documents` page** + chat toggle UI
11. **End-to-end verification** + full regression suites

Each stage is tested before the next begins. Nothing touches the agent's
behaviour until stage 9.

## Console

- Upload with drag-drop, multi-file, per-file progress
- Table: name, type, size, status, chunks, enabled, actions
- **Click through to a detail view**: metadata, parse warnings, chunk list
  with previews and page refs, re-index, delete
- A **test-search box** to verify retrieval without spending LLM tokens
