# Code section — bridging the gap to VS Code

Written and executed 2026-10-03. Status per phase is at the bottom, including
the parts that are only partly done and the two that were deliberately left
alone. Nothing below is claimed as working unless a test asserts it.

## Honest framing

This will not become VS Code. VS Code is a mature product with a language
server protocol, a real extension host, GPU rendering and a decade of work.
What follows is the largest honest improvement available to a browser editor
that reads the repository read-only and cannot execute anything.

The gap that matters is not feature count, it is *feel*. A user opening VS Code
sees coloured code, a highlighted current line, fold arrows, a Problems list
with real errors, and Ctrl+Z that always works. Those are the things to build,
in that order.

## The foundation everything else needs

All of the first three phases need one thing: a **tokenizer**. Highlighting,
code folding and the symbol outline are three views of the same token stream.
So the tokenizer comes first and is shared, rather than three ad-hoc regex
passes over the text.

Constraint: **no new runtime dependency.** A compact hand-written tokenizer for
the language families in this repo (Python, JS/TS, JSON, YAML, Markdown, CSS,
HTML, shell, SQL, TOML, INI) is a few hundred lines and avoids shipping a
parser plus a grammar bundle to the browser.

Also load-bearing for the whole section, and already true — keep it true:

- The editor is a `<textarea>`. Syntax highlighting therefore cannot be done
  *in* the textarea. It is done with a synchronised `<pre>` overlay behind a
  transparent-text textarea. That constrains everything below: no per-character
  widgets, no native spellcheck, and the overlay must stay pixel-identical to
  the textarea's metrics or the caret drifts.
- Read-only source. Edits are browser drafts only.

---

## Phase 1 — Visual fidelity

The largest visible gap. Plain monospace text is the reason this does not look
like VS Code.

1. **Syntax highlighting** via the overlay technique: a `<pre>` under a
   transparent textarea, same font/metrics/padding, synced scroll.
2. **Current-line highlight** spanning the full editor width.
3. **Active-line number** emphasis in the gutter (partly present).
4. **Bracket matching** — highlight the pair adjacent to the caret.
5. **Indent guides** — vertical rules at each indent level, so nesting is
   readable in deeply indented Python.
6. **Selection fidelity** — the native selection must be visible against the
   overlay, which means the textarea's own text has to be transparent while the
   overlay's is not.

## Phase 2 — Structure

7. **Code folding** from indentation (Python/YAML) and brace depth
   (JS/TS/JSON/CSS), with chevrons in the gutter, fold-all/unfold-all, and
   `Ctrl+Shift+[` / `Ctrl+Shift+]`.
8. **Symbol outline** — functions/classes extracted from the token stream into
   a panel; `Ctrl+Shift+O` jumps between them.
9. **Sticky breadcrumbs** — the enclosing symbol stays pinned while scrolling,
   which is how VS Code keeps you oriented in a long function.

## Phase 3 — Editing correctness

10. **A real undo/redo stack.** This is a genuine bug class today: the
    structural edits (comment toggle, indent, move line, bracket close) are
    applied by writing to the textarea's value behind React's back, so native
    `Ctrl+Z` does not reliably step through them. An explicit snapshot stack
    fixes it. `Ctrl+Z` / `Ctrl+Y`.
11. **Multi-cursor** — Alt+Click to add a caret, Alt+Shift+Up/Down to add one
    per line, Escape to collapse to a single caret, and a live caret count.
12. **Copy line up/down** (`Shift+Alt+Up/Down`).
13. **Trim trailing whitespace**, exposed honestly as "draft only".

## Phase 4 — Real diagnostics

14. **`POST /api/code/check`** — send the *draft text* (never a path) and get
    real syntax errors back. For Python this is `ast.parse`, which gives genuine
    `line:col` errors. This is the single biggest credibility win in the whole
    plan: the Problems panel stops being an apology and starts being useful.
    Guarded: no filesystem write, no execution, bounded size, parse-only.
15. For non-Python languages, a brace/bracket balance check — reported as a
    heuristic, labelled as one.
16. Problems become clickable: click an error to jump to its line.

## Phase 5 — Workspace search

17. **`GET /api/code/search`** — bounded grep across the servable tree, with the
    same path confinement and extension allow-list as the file endpoint.
18. **`Ctrl+Shift+F`** — results pane, click to open at the matching line.

## Phase 6 — Polish

19. **Split editor** — two panes over the same tab set.
20. **Stale-file detection** — the source is read-only from the agent, so the
    file can change underneath a draft. Detect a version mismatch and offer to
    reload instead of silently showing text that no longer matches the server.
21. **Keyboard shortcuts cheat sheet** on `Ctrl+,`.
22. **Tab drag-reorder** and tab overflow scrolling.
23. Full regression coverage: every phase adds Playwright tests, and the
    existing phone-overflow invariant is extended to cover the overlay and
    minimap.

---

## Status

| # | Item | State |
|---|------|-------|
| 1 | Syntax highlighting overlay | done — `src/lib/lang.ts` tokenizer, no new dependency |
| 2 | Current-line highlight | done |
| 3 | Active-line number | done |
| 4 | Bracket matching | done |
| 5 | Indent guides | done |
| 6 | Selection fidelity | done — `selection:bg-accent/25` over a transparent textarea |
| 7 | Code folding | done — indent- and brace-scoped, chevrons in the gutter, fold all/unfold all |
| 8 | Symbol outline | done — classes/functions/sections from the same token pass |
| 9 | Sticky breadcrumbs | **not done** — breadcrumbs show the path, not the enclosing symbol |
| 10 | Undo/redo stack | done — `src/lib/useHistory.ts`, coalesced typing, capped depth |
| 11 | Multi-cursor | **partial** — see below |
| 12 | Copy line up/down | done — `Alt+Shift+Up/Down` |
| 13 | Trim trailing whitespace | done — draft only, says so |
| 14 | `POST /api/code/check` | done — `ast.parse` for Python |
| 15 | Delimiter balance for brace languages | done — labelled a heuristic in the response and the UI |
| 16 | Clickable problems | done — click jumps to `line:col` |
| 17 | `GET /api/code/search` | done — bounded, case-insensitive, no regex |
| 18 | `Ctrl+Shift+F` results pane | done — click opens the file at the line |
| 19 | Split editor | **not done** |
| 20 | Stale-file detection | done — 20s poll on `version`, offers reload |
| 21 | Shortcut cheat sheet | done — `Ctrl+,` |
| 22 | Tab drag-reorder | **not done** — tabs scroll horizontally but cannot be dragged |
| 23 | Regression coverage | done — 20 Playwright tests over the Code section |

### Why multi-cursor is only partial

A single `<textarea>` hosts exactly one caret. There is no multi-caret editing
in a textarea, and faking it with a custom contentEditable surface would mean
reimplementing text editing, IME support, undo and native accessibility — far
beyond this section's scope and worse at all of it than the browser's own.

What is real: `Alt+Up` / `Alt+Down` place additional carets, the status bar shows
the count, `Escape` collapses to one, and `Ctrl+D` grows the selection to the
next occurrence. Typing does *not* apply to all of them. It is navigation, not
multi-edit, and the UI does not claim otherwise.

### The overlay constraint, restated after it bit

Syntax highlighting cannot live inside a textarea. It is a `<pre>` behind a
transparent one. That makes metric parity load-bearing, and the first attempt
got it wrong: the gutter was floated *and* the textarea carried a matching
margin, so the textarea was offset twice and every line looked clipped by a
character. A screenshot looked "almost right"; only measuring both boxes
caught it. There is now a test that asserts the two layers agree on x, y,
width, padding, line-height and font-size.

## What is deliberately not built

- **IntelliSense / completion / go-to-definition.** These need a real language
  server. A regex-based imitation would be worse than none: it would suggest
  wrong symbols with confidence. The UI says "no language server" and stops
  there.
- **Running code.** Needs a sandbox and an approval gate; both are still open
  security work (see the auth/CSRF/SSRF items).
- **Writing files.** The API has no write verb by design, and a test asserts
  the route set stays GET-only.
- **Extensions / themes marketplace.** Not a console feature.
- **Git integration.** The repo has no commit path wired into the console, and
  the working tree is large and dirty; surfacing that in an editor invites
  accidents.

## Verification per phase

- `npx tsc --noEmit` then `npm run build`
- Playwright: the phase's own tests, then the whole spec
- `uv run python -m pytest tests/ -q` for any backend phase
- The phone-overflow invariant must stay green: the overlay is a second
  full-width layer and is exactly the kind of change that reintroduces sideways
  scrolling on a 390px viewport.
