# Studio → OpenCode Desktop parity plan

Goal: make `/studio` look and behave like OpenCode Desktop. The harness will
change later; for now this is **visual + structural parity**.

## Reference (OpenCode Desktop)
- Electron client/server; multi-session **tabs**; **parallel agents**.
- **Workspaces** = Git worktrees on `opencode/<name>`, isolated dirs.
- Side panels (**files, review, browser**) draggable to a bottom pane.
- **Diff viewer with per-line comments**.
- **Command palette** + **Jump to message** (`⌘⌥G`).
- **Recent sessions** sidebar (cross-server, pins, reorder).
- Visual: 12.5–13px UI, 11px meta, 1px `white/10` borders, muted palette,
  minimal chrome, `font-mono` for code/paths.

## Checklist

### A. Visual alignment  *(done — the two biggest gaps)*
- [x] Type scale: 12.5–13px UI, 11px meta; keep mono for code/paths.
- [x] Borders: 1px `white/10`; remove gradients / large radii in chrome.
- [x] Diff palette: OpenCode-style green/red line washes + gutter bars, added to
  the `aria-dark` Monaco theme (`MonacoEditor.tsx`).
- [x] Reduce chrome; keep rail-free workbench + right activity bar.
- [x] **Top session tabs** (browser-tab style, live dot, × to archive) moved to
  the very top of the studio, not a rail (`Studio.tsx`).
- [x] **Flat near-black theme** — removed the violet gradient panel bg
  (`PANEL_BG` → `#0b0b0e`); the browser pane already has its URL bar/tab strip.
- [ ] Verify: screenshots at desktop width.

### B. Workspaces (Git worktrees)  *(DONE)*
- [x] Backend: `studio_files.py` — `worktrees` read action + `worktree_add`
  (`opencode/<name>` branch) + `worktree_remove`; route rejects a target inside
  the repo. Tests in `test_studio_files.py`.
- [x] Frontend: `GitPanel.tsx` — **Workspaces** tab: list, create (sibling
  folder), remove (confirm).
- [x] Verified: real `git worktree add/list/remove` in a temp repo; `tsc`,
  `vitest`, build; 63 studio-file tests.

### C. Parallel sessions as tabs  *(DONE)*
- [x] Session tab bar above the conversation (label + running dot + `+`),
  `Studio.tsx`.
- [x] Verify: `tsc`, `vitest`, build.

### D. Dock: side ↔ bottom  *(DONE)*
- [x] `Dock.tsx` `side` prop + `Studio.tsx` toggle (persisted per window); the
  dock becomes a 380px right column or the bottom bar.
- [x] Verify: `tsc`, `vitest`, build.

### D2. Diff line comments  *(DONE)*
- [x] `ReviewEditor.tsx`: a **Comment** button per hunk (prompt → note), shown
  under the hunk label; stored per hunk id; rebuilds the zone on change.
- [x] Verify: `tsc`, `vitest`, build.

### E. Command palette + Jump to message  *(todo)*
- [ ] Add “Jump to message” (`⌘⌥G`) — needs a transcript search over the
  session's messages; the studio runs list is the closest index.

### F. Docs
- [ ] Update `TEST.md` / `CODE_IDE_DESIGN.md` when done.

## Order
A → B → C → D → D2 → E → F. Verify after each; do not batch.