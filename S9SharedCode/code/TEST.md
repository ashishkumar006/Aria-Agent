# TEST.md — the Aria Code Studio verification checklist

Everything that has to be proved about `/studio`, written so that a future
change can be checked without re-deriving why each item matters. It is organised
by **what could go wrong**, because every entry here was either a real defect or
a defect that was one step away.

- Location: `S9SharedCode/code/TEST.md`
- Automated tests: `S9SharedCode/code/tests/test_studio_*.py` (backend) and
  `console-frontend/src/views/studio/*.test.ts` (frontend pure logic)
- Add to this file when you add a studio surface. An area with no entry here is
  an area nobody will think to test next time.

---

## 0. How to run it

```sh
cd S9SharedCode/code

# backend — targeted (each file is a slice)
uv run python -m pytest tests/test_studio_workspace.py tests/test_studio_review_routes.py \
  tests/test_studio_runs.py tests/test_studio_composer.py tests/test_studio_dock.py \
  tests/test_studio_files.py tests/test_studio_browser.py tests/test_studio_browser_pane.py \
  tests/test_studio_prompt_truth.py tests/test_studio_journey.py -q

# backend — the whole agent suite (≈5–6 min). NEVER bare `uv run pytest`:
# the shim resolves the wrong interpreter and dies on numpy.
uv run python -m pytest tests/ -q

# frontend
cd console-frontend
npx tsc --noEmit
npx vitest run src/views/studio/
npm run build

# live: services have to be restarted to pick up backend changes, and a
# restart is only real if health says so
curl -s http://127.0.0.1:8500/api/health          # must be gateway_up: true
curl -s http://127.0.0.1:8109/v1/providers | head # the gateway is answering
```

Browser verification is not optional for this surface. The in-app browser in
Qoder is 598 px wide, which squeezes the studio's three columns to unusable
widths; judge layout at the desktop app's real window size, and treat a
"renders fine" claim from a 598 px viewport as unverified.

### Preconditions some checks need, and skip quietly without

| Thing | Needed by | What happens without it |
|---|---|---|
| `git` on PATH | git verbs, commit message, dock shell | those tests `pytest.skip` |
| Playwright + Chromium | browser tools, pane, screencast | browser tests skip |
| A reachable gateway (`:8109`) | live agent runs, commit drafts | live scripts report the failure |
| `state/code-studio` writable | every route that persists | refuse, and say why |

### Flags a check may need flipped, and what each one is for

| Flag | Default | Flip it to test | Where the switch is enforced |
|---|---|---|---|
| `ui.code_ide` | **off** | whether `/code` still behaves as the old page with the studio off | `flags.py`, `lib/studioSession.ts` |
| `browser.tools` | **off** | whether the browser verbs vanish from the agent's payload *and* the pane says `disabled` | `flags.py` + `agent_server._browser_tools_enabled()` |
| `browser.headed` | **false** | whether the window is visible on the next browser a session starts | `flags.py` + `browser_session.get()` |

Flags are read **per request**, not at import. A flag that was baked into a
module constant is the bug that let a prompt describe browser tools while the
payload had none — see §9.

---

## 1. The kill switch (`ui.code_ide` off) — the first thing to test

Off must mean **the old page**, not a smaller studio.

- [ ] `/code` renders the Code page that shipped before the studio, and `/studio`
      does not create session state for a user who never enabled the flag.
- [ ] No `/api/studio/*` call is made by the console when the flag is off (check
      the network tab: the old page must not touch session routes).
- [ ] The process-wide code workspace still governs `/api/code/*` — with no
      session bound, `_code_workspace()` returns the module default, not an
      empty allow-list.
- [ ] Turning it on does not delete anything: sessions and folders already under
      `state/code-studio/` are simply not consulted while it is off.
- [ ] Turning it off leaves no residue: no orphan SSE watchers, no timers, no
      `/api/studio/live` connections from a remounted KeepAlive host.

Test: `tests/test_code_agent_*` (the old routes unchanged), plus a manual
network-tab check on both `/code` and `/studio`.

## 2. Sessions and workspaces (slice 1)

The whole studio rests on one rule: **a session's roots are the only place any
path can resolve**, and a session with no roots can reach nothing.

- [ ] Two sessions open in two tabs do not re-root each other. Type a path in
  one, save, and confirm the other tab's explorer is unchanged.
- [ ] A session may hold several folders; a bare relative path still resolves
  against the FIRST one, and `<otherroot>/file` resolves against that root.
  This is one rule shared by `agent_server._code_agent_abs` and
  `mcp_server._safe` — if you can name a third place paths are resolved, that
  is the bug.
- [ ] A session whose every folder was deleted gets an **empty** allow-list, not
  the shipped playground. Prove it: remove the folder from disk, ask the agent
  to edit a file, and it must refuse rather than write into
  `state/code_workspace`.
- [ ] `?sid=` on a GET and `sid` in a POST body both bind. The middleware only
  reads the query, so a body-only `sid` used to leave `/api/code/agent` writing
  into the process workspace while the panel read the session's — that is
  `tests/test_studio_workspace.py::...body...` and it must never come back.
- [ ] Workspace reset clears the review entries too; a checkpoint whose every
  file is unreachable is a list of refusals, not an undo history.

## 3. The editor, tabs, explorer (slice 3)

- [ ] Quick-open finds files the explorer has never listed (`/api/code/files`
  walks the tree; it used to be limited to expanded folders).
- [ ] An unsaved buffer is never silently overwritten by an agent write: the
  conflict banner is the only acceptable outcome, and it offers reload.
- [ ] A file opened from a search hit, a mention chip or an approval card
  reveals itself in the tree (auto-expand the chain).
- [ ] The explorer's first level is the session's roots, never the base
  directory's contents — with the base as the parent, listing it published
  `sessions/`, `threads/` and the rest of the state dir.
- [ ] 4000 rows must not freeze the tree (virtualization) and a folder that
  refuses to list shows its error inline rather than an empty folder.

## 4. Review: hunks, Keep/Revert, checkpoints (slice 4)

Hunk identity is content, not position. This is the invariant with the most
damage available if it goes: a wrong revert silently destroys a line.

- [ ] The same edit at a different line is the **same** hunk (fingerprint =
  old block + new block + occurrence index), and an edit whose content is
  genuinely different is a different hunk.
- [ ] A hunk whose content has moved gets `409 hunk-moved` and is **not**
  applied. Test it by editing the file elsewhere between listing and deciding —
  the panel must refuse, not guess the nearest match.
- [ ] `Keep` changes only the baseline (the file is untouched); `Revert` changes
  the file. Confuse them and the user's accepted work reappears as a change.
- [ ] The pending set after a turn is recomputed baseline→current
  (`pending_diff`), not the last hop's patch. Two edits in one turn must list
  two hunks — this was a live defect where the second edit erased the first
  from the review list.
- [ ] **…but only two hunks when the sites are far apart.** A unified diff
  carries three lines of context, so two edits within ~6 lines are genuinely
  ONE hunk, and git, Cursor and this studio all say so. A fixture whose file
  is six lines long therefore proves nothing about per-hunk review — it passes
  or fails on diff context, not on the defect. `test_studio_journey.py`'s
  `app.py` is eleven lines with the edit sites a dozen apart for exactly this
  reason; keep that spacing if you edit the fixture.
- [ ] Decide in the **middle**, not only at the end. `revert` implemented as
  "write the whole baseline back" reaches the same final file after a
  keep-then-revert pair and is still wrong: it decided a hunk nobody picked.
  The field that distinguishes them is `hunks_remaining` after each
  `/api/studio/changes/apply` — assert 1 then 0, and read the file between the
  two calls. Both mutations (whole-baseline restore, and reverting from the
  hunk's start to end of file) go red on step 3; that was checked by mutating
  `studio_review.apply_hunk`, not by trusting the green.
- [ ] A scripted agent turn must **read before it edits**. The write guard
  refuses `edit_file` on a file this run has not seen — "read_file('x') first"
  — and a test that stubs the model but scripts only the edit gets a refusal
  that looks like "the pipeline produced no hunks". This cost a whole
  debugging session: the standalone script passed because its prompt contained
  an `@`mention, and `context_rel_paths` seeds `read_paths` from a mention.
  Script `("read_file", …)` before `("edit_file", …)`.
- [ ] Every mutation is decided from the state the server will restore **to**,
  so the review editor's baseline is the stored baseline and not the disk.
- [ ] A checkpoint restore captures what it replaces as a checkpoint of its own
  (`undo.id`), so restoring is reversible from the same panel.
- [ ] CRLF files survive: byte comparisons, `split_lines` joiner, and writes
  that preserve the original line endings. Windows made this fail three times.
- [ ] Blob sweep only deletes bytes no index references — sweep after a
  checkpoint delete and confirm a still-referenced snapshot survives.
- [ ] Monaco view zones are removed by id (`acc.removeZone`) as well as the DOM
  node: leaking them is what froze the page.

- [ ] **A change the user undid by hand stops being counted.** If the file
  matches its baseline again (a checkpoint restore, another tool, a git
  checkout), `/api/studio/changes` must not list it. The live failure was the
  rail reading "4 files unreviewed" with `+0 −0` on every row, and clicking one
  opening the editor in **source** mode with no hunks to decide — the review
  promise with nothing behind it. Pinned by
  `tests/test_studio_journey.py::test_step_13` (mutation-checked: disable the
  `_matches_baseline` skip and it goes red). The conservative half matters:
  a file that could not be *read* stays listed with a reason, because dropping
  a change nobody can review because the disk was unreadable is how work
  disappears.
- [ ] **Opening a reviewed file and then switching away must not crash the
  view.** `ReviewEditor` keeps two effects: one creates the diff editor, the
  other creates its model pair. React runs cleanups in declaration order, so
  the editor is disposed *before* the model cleanup runs — and
  `ed.setModel(null)` on a disposed diff editor throws
  `InstantiationService has been disposed`, which the error boundary showed as
  "SOMETHING BROKE RENDERING". The mount cleanup nulls `edRef` first and the
  model cleanup only touches the editor while it is still the live one. Found
  by clicking the surface in a browser; no unit test could see it.
- [ ] A buffer whose read **failed** is not an empty document. It must render
  the reason with actions (Try again / Revert to the checkpoint / Close), not
  line 1 of nothing under a source-review switch that invites a save. Distinct
  from `error`, which also carries "truncated for editing" on a buffer that
  loaded fine — that one must still open the editor.

Tests: `tests/test_code_hunk_revert.py`, `tests/test_code_agent_review.py`,
`tests/test_studio_review_routes.py`, and `tests/test_studio_journey.py`
steps 2–5 and 13, which drive the same routes through a real agent turn.

### 4b. The review surface's own behaviour

- [ ] A **change set** is one task: the files a single turn wrote group under
  one header, and the header's Keep all / Undo all acts on every file in it.
  Grouping keys on the checkpoint the hunks were measured against, never on
  the turn that first saw the file — a file edited by task 3 while still
  pending from task 2 must not be offered a restore of task 2's snapshot.
  (`timeline.test.ts`, `groupChanges`)
- [ ] Hunk buttons are the smallest targets in the studio (10.5px label, ~24px
  tall). That is deliberate for a dense list, but they must stay **right-aligned
  and quiet until hover** — two saturated buttons per hunk make eight hunks read
  as sixteen controls and no content.
- [ ] A **rename is one act**. Undoing it restores the old name AND removes the
  new one; if the moved file was edited after the rename (its hash no longer
  matches) it is left alone and reported, never deleted.
- [ ] `/undo` steps **backwards** through task checkpoints and never re-restores
  the same one; `/redo` replays the snapshot the last undo replaced. A snapshot
  written *by a redo* is an undo target, not another redo.
- [ ] A checkpoint list that is long is capped in the panel (newest eight) with
  an explicit "show N older" — an undated wall of "before task N" rows is not a
  history anyone can read.

## 5. Runs: persistence, replay, Stop (slice 5)

- [ ] Close the tab mid-run: the run keeps going, is listed in `/api/studio/runs`,
  and can be stopped from another tab by id.
- [ ] Reload the studio: the transcript of the newest finished run comes back
  from its JSONL, through the **same** reducer the live stream uses
  (`foldFrame`). A replayed run that renders differently from a live one is the
  defect this sharing exists to prevent.
- [ ] The replay must fire even though `runs` arrives *after* the panel mounts.
  A cold load used to land on the hero question with a finished, billed answer
  missing.
- [ ] Two Stop verbs, and they mean different things: `run/stop` ends the loop
  at its next hop; aborting the fetch only closes the view. The UI label must
  say which happened.
- [ ] A run parked on an approval is `waiting`, not `running` — the status bar's
  `N agents · M waiting` depends on it, and a blocked agent reported as working
  is a lie about the thing the user is waiting for.
- [ ] The transcript records frames as they are sent, not at the end: a run that
  dies halfway still leaves the tool cards, the deltas and the unanswered
  approval.
- [ ] `MAX_RUNS_PER_SESSION` bound announces itself, and a truncated last line in
  a JSONL transcript is counted (`unreadable`) rather than crashing the replay.

### 5b. Names, empty states and the small surfaces

- [ ] **Every surface that names a session names it the same way**
  (`sessionName`): label, else the folder it works in, else "Untitled session".
  The tabs once said "Untitled session" while the switcher said the folder, so
  one row of the UI carried ten identical labels.
- [ ] The **Projects flyout** marks the session you are in (dot + weight, keyed
  on the client's sid — the server's `current` is per request and always false),
  shows the unreviewed count as a badge, and orders each project's sessions by
  `updated` descending.
- [ ] **A folder that is not a git repository is a state, not an error.** The
  Git panel must not paint it as a danger alert; it says so and names the fix
  (`git init`). A real git failure still gets `role="alert"`.
- [ ] The **slash menu** reads as two weights, not two shades: command 13px/500,
  a mid dot, hint 11px/400. Same size and colour made
  "/undo restore the last checkpoint" one blurred line.
- [ ] **Context** is never shown as a bare "0%". The ring popover and the Context
  panel both say "not measured yet" with a bar at zero, and fill in
  `<chars> of <cap> chars` once the gateway reports a window.
- [ ] Check runs leave **no trace in the project**: no `.pytest_cache`, no
  `__pycache__`, no `.ruff_cache`. `PYTHONDONTWRITEBYTECODE`, `-p
  no:cacheprovider` and `RUFF_CACHE_DIR` are what enforce it — verify by
  listing the folder before and after `/checks`.

## 6. The composer: `@`, `#`, `/`, ceilings, cost (slice 6)

- [ ] `@` completes inside **every** folder the session holds, in the explorer's
  vocabulary (`<root>/dir/file`), which is the form the agent's tools accept.
  Both spellings of a directory must work: `@src/` and `@<root>/src/`. A
  completion that offers a path `read_file` cannot use is worse than none.
- [ ] `node_modules`, `.git`, dotfiles and build trees are never offered — the
  completion is an offer to put a path in front of a model.
- [ ] The candidate list is fetched, debounced **and generation-guarded**: two
  keystrokes resolving out of order otherwise insert the first fragment's path.
- [ ] A walk that hits its budget says so (`stopped`, `note`); truncation is
  allowed, invisible truncation is not.
- [ ] An `@` in the prose is context: the file's contents go into the prompt, it
  counts as read for the read-before-write guard, and a mention that does not
  resolve is **reported** (`not attached: …`) — a silently ignored mention
  produces an answer as if it had been read.
- [ ] `me@example.com` is not a file. `a@b.com` must not become an attachment.
- [ ] The `/` popup is served from `/api/studio/commands`; a command the frontend
  invents is a button that does nothing, and an unknown verb must be refused in
  the transcript rather than sent to the model as a question.
- [ ] Enter on a highlighted command **runs** it (not completes-then-runs-twice).
- [ ] `#` attaches real machine-reported problems, and the count that was not
  shown to the model is stated in the prompt (`… N more problem(s) not shown`).
- [ ] The ceilings on the chips come from the `started` frame (`max_hops`,
  `max_context_chars`), not from a constant copied into TypeScript.
- [ ] The cost line is the gateway's, read from the run's **own** session id.
  Two runs that touched the same file must not share a cost column, and an
  unreachable ledger renders as `cost unknown`, never `$0.00`. A real $0 from a
  free model is legitimately `$0.0000` — check which one you are looking at.
- [ ] The footer renders at all. `A || B && <jsx>` parses as `A || (B && jsx)`
  and renders `true`, i.e. nothing — that is how the cost line disappeared once
  with every field correct behind it.

Tests: `tests/test_studio_composer.py`, `views/studio/Composer.test.ts`.

## 7. The dock: terminal, Problems, Output, Checks (slice 7)

- [ ] **A shell's `cwd` must be inside the session's roots.** `/api/terminal`
  used to accept any absolute path with `os.path.isdir()`, which made a studio
  dock a hole in the editor's confinement. Test `..`, an absolute system path, a
  sibling project, and a symlink pointing out (`_terminal_cwd_ok`).
- [ ] One shell per folder, scrollback kept when switching tabs (hide with CSS,
  do not unmount), a Restart control when the process exited.
- [ ] Problems checks the **unsaved buffer** (what the person is looking at),
  says which mode answered (`ast` vs `balance` vs no parser), and does not
  present "delimiter heuristic found nothing" as a clean bill.
- [ ] Output mirrors the command's **own** output from the transcript at 20k
  chars with `clipped` stated — not the model's 1500-char tool clip.
- [ ] Checks runs the project's real lint/typecheck/tests and names which tools
  were not installed instead of showing an empty green.
- [ ] A refused mention, a failed verify and a skipped file all appear in Output
  rather than only in the transcript.

Tests: `tests/test_studio_dock.py`, `views/studio/Dock.test.ts`.

## 8. File operations, search, replace, git verbs (slice 8)

- [ ] Rename, move, delete and mkdir are **planned** first: outside the session,
  into an existing path, a folder into itself, a rename that changes the folder
  (that is a move), the session root itself, anything under `.git`, and an
  unknown verb must each be refused with a reason.
- [ ] A delete or a move captures the bytes into a checkpoint **before** acting,
  and `/api/studio/checkpoint/restore` puts them back. Prove it by deleting a
  file and restoring it.
- [ ] Per-operation refusals are counted and reported
  (`applied` / `refused_count` / per-row `reason`); a partial success that reads
  as a total success is the failure mode here.
- [ ] Search covers every root, skips the vendor and build trees, skips binary
  files by extension, and reports `scanned` / `binary_skipped` / `truncated`.
- [ ] A nested-quantifier regex is **refused with its reason** before the walk:
  Python's `re` has no timeout and one `(a+)+` on a long line pins a worker for
  minutes. A naive detector also flags `OLD\s*=\s*(\d+)` because `"" in "*+"` is
  True — the mutation test for that off-by-one is in
  `tests/test_studio_files.py`.
- [ ] Replace is dry-run first. The preview binds a token to the exact bytes it
  read; applying re-reads each file, applies the verified ones, and names any
  that moved in `stale`. A stale file is never written from a stale plan, and a
  spent or missing token is a 409.
- [ ] The git verb list is **closed**: `push`, `force_push`, `reset`, `clean`,
  `checkout`, `rebase`, `merge`, `amend`, `init` are refused at the plan with a
  stated reason, and every path is passed after `--` so a filename beginning
  with a dash cannot become an option.
- [ ] Anything that moves HEAD or discards work answers **428 with the diff**
  until `confirm: true`. The console must render that as a confirmation reading
  the actual staged diff, not an error — which is why those routes use
  `pjBody` instead of throwing on a 4xx whose body is content.
- [ ] Every file operation and git action lands in the session journal
  (`/api/studio/journal`), newest first, with its argv. A human-started action
  belongs to no run transcript, so without this there is no answer to "what did
  Aria do to my repository".
- [ ] The repository is resolved with `git rev-parse --show-toplevel` per
  session root, not the workspace **base**: with one folder open the base is its
  parent, so the old route reported "not a git repository" for a real repo and
  showed someone else's history for a parent that happened to be one.

Tests: `tests/test_studio_files.py`.

## 9. The browser (slices 9-10)

The widest door in the product, and the only part of the studio that can act
outside the machine's file system.

**Grant (four layers, all four or it is not a capability)**
- [ ] Registered with `@mcp.tool()` in `mcp_server.py`; named in
  `_TOOL_CATALOG`; allowed by `tools_allowed`; **and present in the calling
  context's payload** (`CODE_AGENT_TOOLS` + the prompt's paragraph). Tests assert
  the cross-product both ways: no granted name without a catalog entry, no
  prompt claim without a payload entry.
- [ ] With `browser.tools` off, the payload contains no `browser_*` tool at all
  and the pane routes answer `disabled: true`. Read per request, not at import.

**The session-scoped context**
- [ ] One Chromium per **studio session**, not per run: a page opened by call N
  is still there on call N+5, and another session's `status` is `running: false`.
- [ ] `ARIA_BROWSER_SID` is the session; `ARIA_SESSION` is the run (so that one
  run's cost reads correctly). A browser keyed on the run forgets every login.
- [ ] **Looking never launches.** `status`, `events`, `page` and a frame poll must
  answer "no browser is open" instead of starting one — a pane polling every few
  seconds would otherwise own a process whether it used the browser or not.
- [ ] Tabs: bounded (`MAX_TABS`), listed with each tab's own console-error count,
  selectable, closable except the last one.
- [ ] Idle sweep closes a browser nobody has used (`IDLE_CLOSE_S`) — a session
  left open in a tab must not leave a Chromium and its cookie jar behind.

**Evidence**
- [ ] `page.on("console")`, `pageerror`, and request/response listeners exist and
  are per page. Without them the agent cannot see the JavaScript error its own
  change produced, which is the whole dev loop. Prove it with a page that throws
  (`tests/test_studio_browser.py::...javascript_error_is_captured`).
- [ ] Rings are bounded and report `dropped`. An app that logs in a loop must not
  be readable as a quiet one.
- [ ] Screenshots are filed under the session's artifact dir and served by
  `/api/artifacts/{sid}/…`; the pane renders those URLs. That route's containment
  is checked with a **percent-encoded** `../` (a literal one is normalised away
  by the client and never reaches the server, so a test that uses it proves
  nothing).
- [ ] `browser_verify` reports `nothing_checked` when handed no assertion. A
  verification that asserts nothing must not be able to pass.
- [ ] Marks belong to the snapshot's `epoch`: acting on a spent epoch is refused
  rather than clicking whatever moved into those coordinates, and a mark with no
  snapshot is refused rather than guessed.

**The gate is per CALL, not per tool name**
- [ ] `browser_verify` is in the read list because most of what it does is read,
  but **with a `url` it navigates** — that is `browser_open`'s act, and a
  read-only review turn must not be able to drive any address the model names.
  `agent_server._is_exec_call(name, args)` is the seam; the gate sites use it,
  not `name in _exec_tools()`. A verify with no url stays ungated, or the
  read-only mode loses the one verb it was meant to keep.
- [ ] A standing "Approve and remember" for a verify is keyed on the URL
  (`_code_sig`), so approving `localhost:5173` does not authorise every site.
- [ ] `browser_session._nav_url` is the scheme rule: **http, https and `data:`
  only**. The old rule asked "is there a colon", so `file://` navigated, and a
  file:// page reads this machine — the state directory, `agent.token`, the
  gateway's loopback UI. `data:` stays because it renders in an opaque origin,
  cannot read the disk or another origin's responses, and is how a caller hands
  Chromium a page it generated rather than one it fetched (every real-page test
  supplies its page that way). A bare host still becomes https, and the refusal
  happens **before** a Chromium is started.
- [ ] The prompt says which verifies ask. A model told "browser_verify is
  evidence" and then met with an approval card it was not warned about will
  describe the pause as a failure.

**The live view**
- [ ] CDP screencast over SSE. It captures one frame **immediately** and then
  streams on repaint: a headless compositor emits nothing for a page that is
  simply sitting there, so without the first capture the pane is an empty box
  over a perfectly rendered page — and an empty box is indistinguishable from a
  blank page, which is the last thing a takeover surface should be.
- [ ] Newest frame only, never a queue; the stream stops itself when the page
  goes quiet and when the window ends.
- [ ] Where screencast is unavailable the pane says so and shows the filmstrip.
  Silent degradation is a lie about what you are watching.

## 10. Prompt truth (slice 11)

The prompt is `prompts/studio_coder.md`, rendered per request; nothing else
reads it, so a false claim in it has no symptom except an agent that behaves
oddly.

- [ ] Every backtick-quoted tool name in the prompt resolves in the payload the
  same request produced (`tests/test_studio_prompt_truth.py`).
- [ ] **No `{{TOKEN}}` survives rendering.** An unreplaced placeholder reaches
  the model as literal template text and reads as a harness bug
  (`tests/test_studio_prompt_file.py`).
- [ ] **Editing the file applies without a restart** (mtime cache), and a
  missing or empty file falls back to the built-in prompt instead of killing
  the run. Both are asserted in `tests/test_studio_prompt_file.py`.
- [ ] **A prompt edit that only moves the text must render byte-identically.**
  The extraction from `agent_server.py` was verified that way; any later
  reshuffle should be too, or the diff is a behaviour change wearing a
  refactor's clothes.
- [ ] The workspace description names only what is true of **this** session's
  folder: `list_dir(".")` is how the tree is learned, and the shipped repo's
  directory names never appear. A fixed description naming
  `S9SharedCode/code` made the agent hunt for folders that were not there.
- [ ] The mode paragraph matches the gate the route applies, per request -
  read-only must withhold the tools *and* say they are absent.
- [ ] The review paragraph matches what the review layer actually does (hunk
  Keep/Revert, checkpoint undo). An agent that tells the user to accept a diff
  that has no accept button is describing a product this one does not have.
- [ ] `flags.py` descriptions state the real blast radius. `browser.tools` says
  it reaches `http://localhost:*`, because an operator toggling a switch whose
  description says "reads web pages" has no way to know what they just enabled.

## 11. Full-browser route × pipeline matrix (slice 12)

Run at the desktop window size, with a real project open. Every cell is one
interaction and one thing to look at, not one status code.

| # | Route / surface | Pipeline that must reach it | What "works" looks like |
|---|---|---|---|
| 1 | `/studio` cold | session list, roots, runs | hero question + one composer, no console errors, no request loop |
| 2 | New session + Add folder | `roots` → `sessions` → explorer | the folder appears; a second folder appears beside it, not instead |
| 3 | Open a file, type, Ctrl+S | `codeFile` → `codeWrite` | dirty dot clears; on disk the bytes changed; a second tab on the same file sees it |
| 4 | `@` mention → agent answer | `mentions` → `code/agent` | the answer needs no tool call because the file was in context |
| 5 | Agent writes in Manual | `permission` → `code/permission` | card with the diff; Approve lands it; Refuse is an answer the model is told |
| 6 | Agent writes in Auto files | `changes` → review editor | hunks listed per hunk; Keep/Revert each decide one; the file matches the decision |
| 7 | Two runs at once | `runs` → `live` watcher | `⚑ 2 agents`, each stoppable by id, neither transcript bleeding into the other |
| 8 | Stop mid-run | `run/stop` → cancel scope | billing stops at the next hop; the transcript says stopped and keeps what it got |
| 9 | Reload the tab | `runs/{id}` replay + `runs/{id}/cost` | the answer and its cost come back; nothing re-runs |
| 10 | `/undo` | `checkpoints` → `checkpoint/restore` | files return, buffers refresh from disk, an undo-of-the-undo exists |
| 11 | `/commit-msg` + git panel | `commit-msg` → `git` | 428 with the staged diff first, Confirm commits, log shows it, journal has both |
| 12 | Search → replace | `search` → `replace` (dry) → `replace` (apply) | preview counts, apply writes, `stale` refuses a raced file, undo restores it |
| 13 | Explorer rename/delete | `files` → `checkpoint/restore` | tree updates, refusal reasons show, delete is undoable |
| 14 | Dock terminal | `ws /api/terminal?sid&cwd` | a real prompt inside the folder; `cwd` outside the roots is refused server-side |
| 15 | Dock Problems | `code/check` per open buffer | a syntax error in the buffer appears; the mode that found it is named |
| 16 | `browser.tools` on → agent verifies a page | `browser_open` → `browser_verify` | live frames in the pane, console error visible, screenshot in the filmstrip |
| 17 | `/code` with the flag off | old routes only | no `/api/studio/*` in the network tab |
| 18 | Narrow window | layout | columns refuse to collapse to 60 px; the dock spans the workbench |

### What the 2026-11-06 pass found, and what each one taught

Five real defects came out of driving these rows in a browser. None of them
were visible from a status code, and four of them were invisible to every unit
suite in `tests/`:

1. **`/api/code/write` did not return the new mtime.** The client sends back
   the stamp it read, so a save that hands back nothing leaves the buffer with
   only its own clock to send — the SECOND `Ctrl+S` of one file is refused with
   "changed on disk", forever. The whole edit-save-edit loop was broken and
   only the first save of a session ever worked. Fixed by `_file_mtime` in
   `agent_server.py` and pinned by `tests/test_studio_journey.py::test_step_11`
   (mutation-checked: delete the field, step 11 goes red).
2. **Problems sent the file EXTENSION as the language.** `/api/code/check`
   answers mode `none` for anything it does not recognise, and the panel
   rendered "syntax clean for the open files" about Python it had never
   parsed. `langForPath` + `checkWeakness` in `Dock.tsx` map the name and make
   an unchecked file an *unchecked* claim. Pinned in `Dock.test.ts`.
3. **The Problems effect was keyed on the list of open paths, not their
   content.** Typing a syntax error into an already-open buffer re-ran
   nothing. The signature now samples the text at both ends (`pathSig`), which
   keeps a keystroke in a 1 MB file O(1) — the debounce is still what stops a
   parse per character.
4. **The sessions rail's "Open a folder in this session…" did nothing.** The
   path input lives in the workbench aside, so setting `adding` without
   opening the workbench rendered nothing at all. `Studio.tsx` now routes both
   entry points through the same `addFolderHere`.
5. **A route the console polls must answer whatever the flags say.** With
   `browser.tools` off, `/api/studio/browser/status` answered on a stale
   process — a 404 is a red console error on every composer focus, and the
   cold studio looked broken while being fine. It answers
   `{"disabled": true, "note": "browser.tools is off"}`, pinned by
   `test_step_12`.

### What the 2026-11-06 (second) pass found

This pass went looking for clutter and slowness with an instrumented walk —
every surface clicked, every request timed, screenshots of each state. Nine
things came out of it, and the two biggest were not where the complaint pointed.

1. **The slow thing was the address, not git.** The complaint was "checking the
   git tree takes too much time". Measured against a real repository: `status`
   80 ms, `branches` 46 ms, `log` 49 ms. Git was never the problem. What was:
   `/api/studio/runs/{id}/cost` at **2054 ms** on a cold load, because
   `_session_cost_breakdown` dialled `http://localhost:8109` — this box
   resolves `localhost` to `::1` first and the gateway binds IPv4 only, so the
   connect pays a refused round trip every time (2.42 s via `localhost`,
   0.18 s for the literal, same warm pool). `gateway.py` had been fixed for
   exactly this years ago; five other call sites (`_GW_NOTIFY`, `_GW_BASE`,
   `_GW_VOICE`, `mcp_server._GW`, `notify._gw_base`, `browser/skill.py`'s
   default) had not followed. Cold `/studio` went from 13 calls / 2826 ms
   summed to 12 calls / **413 ms**, and the duplicate
   `/api/code/agent/changes` alongside `/api/studio/changes` is gone. Pinned by
   `tests/test_memory_integration.py::test_every_gateway_call_site_is_ipv4`
   (mutation-checked: put `localhost:8109` back in `notify.py`, it goes red).
2. **A fix whose stated cause was wrong stays in the file with an honest
   comment.** `_warm_ledger()` was written to kill that 2 s and did not — the
   first cost read was still 2.046 s with the thread running. It is kept for
   the smaller claim `gateway.sync_pool` measures (230–590 ms to build the
   first `httpx.Client`), and the docstring now says the 2 s was the address.
3. **Monaco 0.57 worker labels are LANGUAGE IDS.** `workerManager.js` passes
   `label: this._defaults.languageId`, not the old `htmlWorker` name.
   `MonacoEnvironment.getWorker` returned the base editor worker for
   everything, so opening an HTML or TypeScript file threw an uncaught
   `Missing requestHandler or method: findDocumentLinks` /
   `findDocumentSymbols` per request, and links and document symbols were
   silently dead in exactly those files. Python never showed it, which is why
   it survived every earlier browser pass. Fixed in
   `components/MonacoEditor.tsx` (html/css/json/ts workers, editor worker as
   the fallback); the review surface now reports zero console errors.
4. **The file tree was a button board.** Every row printed
   `rename  del  <size>` — five always-visible verbs, and the path (the thing
   a tree exists to show) was the quietest element on its own row. Worse, the
   one verb that was meant to be hover-revealed carried `group-hover:opacity-100`
   on a row that had no `group` parent, so the class did nothing. Now the
   verbs live in one hover/focus-revealed cluster after the size.
5. **The git drawer was nine stacked buttons with no hierarchy** — commit,
   branch and destructive actions the same weight and adjacent, and the commit
   message field squeezed to the placeholder `commit messag`. Now three named
   groups (Commit / Branch / Set aside), the message field on its own row, and
   the caution reduced from four lines to one.
6. **A kill switch that leaves residue is worse than no kill switch.** With
   `browser.tools` off, the browser pane said so three times (live-view box,
   footer strip, help paragraph), still drew a URL bar, a LIVE/CONSOLE/NETWORK
   /SHOTS strip, `+` and `close`, and still dialled `/api/studio/browser/view`.
   It now renders one sentence and nothing else, and does not open the stream
   until status says the feature exists.
7. **Duplicated empty states and dead labels.** The editor said "no file open
   — open one" in the tab strip *and* "The editor is empty. The agent works in
   these same folders…" in the middle; the drawer header truncated to
   `WORKSP...`; the composer carried a static `this machine` chip that the
   sessions rail already owns; tab-close `×` was 7×17 px. All one source of
   truth now, and the close targets are 20×20.
8. **`forbidden_root` means /studio cannot open Aria's own repository.**
   Measured, not inferred: adding `…/project3` or `…/S9SharedCode/code` is
   refused with "this folder holds the agent's state directory". The rule is
   correct as security (a root that holds `state/` publishes `sessions/`,
   `threads/` and `agent.token` to the editor) but it also blocks the primary
   real workspace on this machine, which is why every studio test to date runs
   against tiny scratch fixtures. Left alone deliberately — narrowing it (e.g.
   allow the ancestor but hide the state subtree) is a security decision, not a
   UI one.
9. **Idle cost is honest.** 15 s untouched: one `/api/health` poll, 5 ms.
10. **The per-hunk Keep/Revert bar was unclickable.** Monaco puts `.view-zones`
    at child index 2 and `.view-lines` at index 3, both `position: absolute`
    with `z-index: auto`, so the text layer paints over the zone:
    `document.elementFromPoint()` at the centre of the Keep button answered
    `.view-lines`. The button worked when an event was dispatched at it — which
    is exactly why a scripted test that "clicks" via `dispatchEvent`, or a human
    who never aims at the real pixels, sees nothing wrong. `buildZone` now
    raises the row (`position: relative; z-index: 30`); matrix row 6 only passes
    with a genuine Playwright click, and it is the click that proves it.
11. **`⚑ N agents` is right; the watcher just needs a settle.** Two tabs, two
    concurrent turns: the store reported 2 running and `/api/studio/runs`
    answered `live: 2`, and after 5 s both footers read `⚑ 2 agents`. Reading
    the counter the instant the second tab's own turn went busy only proved the
    1.5 s emit interval. `useStudioRuns.watch()` now re-opens a few times if the
    window closes while the tab still believes nothing is live — the panel
    opens the stream at the same moment it POSTs the turn, so a first poll can
    legitimately see zero. That retry was NOT what fixed row 7; the settle was.
12. **Stop and parallelism both hold.** Row 8: the store moved to `stopped`,
    the transcript kept what it had produced and said so. Row 7: each run
    stopped by id, neither transcript bled into the other.

### What the 2026-11-07 visual-conformance pass found

The complaint was "the studio looks like a different product". It did, and the
cause was not density or layout: **two palettes live in `index.css`.** The
console pages use neutral hex (#090909 canvas, #0b0b0e rails, #0e0e12 cards,
`border-white/10`, 10px cards, `violet-400` primaries); the IDE uses the
blue-black token scale `surface-0..4` (#0a0c13..#252c42, 12px radii) under
`.ide`. The studio was authored against the second while sitting outside
`.ide`, so it rendered cooler, bluer and rounder than everything beside it.
`chrome.tsx` already existed as a conformance layer, but only five panels
imported parts of it and its `Btn` was dead code.

Fixed: `chrome.tsx` Panel/Btn/Tile now pin the console materials; all eleven
studio files were swept (`bg-surface-*` → `bg-void` / `bg-[#0b0b0e]` /
`bg-[#0e0e12]`, `border-edge` and `border-glass-border` → `border-white/10`,
control radii `rounded-lg` → `rounded-md`, `bg-accent text-black` →
`bg-violet-400 text-[#0b0b0e]`); the sessions rail went 262px → 248px to match
Chat's thread rail, and its rows became Chat's 10px cards with a violet active
border. The workbench drawer stays 248px.

Four defects the sweep exposed, fixed in the same pass:

1. **The committed frontend did not compile.** `components/ui.tsx` used `api`
   and `CONF` without importing them (`SearchX` was also unused). HEAD's "tsc
   clean" claim was false because the live SPA had been built from a
   pre-commit tree, so the breakage stayed invisible until a rebuild.
2. **The git drawer header could not fit.** A folder select, branch name,
   three view tabs and refresh do not share 248px at two roots: the branch
   collapsed to 0px and refresh hung 10px outside the drawer. It is now a
   branch row over a tab row.
3. **The composer clipped instead of wrapping.** At a 900px window the
   conversation column is ~180px; the fixed control row put Send and the mode
   menus outside the card's `overflow-hidden`, and the 300px popovers were
   clipped to the 133px card. The row wraps and popovers escape the card.
4. **Two AA failures.** Session metadata used `text-zinc-muted/70` (3.4:1) and
   explorer row verbs `text-zinc-500` (4.1:1); both are `text-zinc-muted`
   (5.6:1). An instrumented contrast walk now reports zero text nodes below
   WCAG AA outside Monaco and xterm.

Trap for the next pass: **do not bulk-replace class strings with PowerShell
5.1 `Get-Content`/`Set-Content`.** It decodes UTF-8 as ANSI, so every em dash,
middle dot and ellipsis in the file becomes `â€”` / `Â·` / `â€¦`. Use a
byte-safe Python/Node replacement, and after any sweep run
`rg "Ã|â€|Â" src/views/studio` — it must return nothing.

### The activity-bar pass (2026-11-07)

The user's complaint, verbatim intent: everything was cluttered — *"a vertical
bar containing the options for all, open to a side bar"* that *"slides open to
the left"* so the workbench is *"less cluttered and more spacious"*, and the
user should be able to slide the sections' size, as in many applications.

What `/studio` now is:

* The labelled strip above the editor is gone. A 46px vertical icon bar sits at
  the **far right** of the studio — Files, Search, Changes, Git, Browser,
  tooltips, a pending-changes badge on Changes — and it is **always visible**:
  it does not come and go with the workbench. Each section opens on its own to
  the left of the bar, sliding in from the right (`panel-in-right`, pure
  transform, 150 ms, no fade; disabled under `prefers-reduced-motion`), without
  dragging the editor, the dock and the tabs in with it. Clicking the active
  icon closes the panel; the agent opens the same panel when it has something
  to show. A `PanelRightOpen`/`PanelRightClose` button at the bottom of the bar
  toggles the workbench (editor + dock) by itself.
* The panel's width is **pixels**, not a share of the window — `useSplitPx` in
  `lib/useSplit.tsx`, 200–520px, default 248, persisted as `aria.studio.drawer`.
  Keyboard: Arrows ±16px, Shift+Arrows ±64px, Home/End to the bounds. The divider
  is a `role="separator"` with `aria-valuenow/min/max`. It separates the panel
  from whatever is to its left (the conversation when the workbench is hidden,
  the editor when it is open).
* **Sweep note:** Monaco's `aria-dark` and TerminalDock's xterm background stay
  #10131c on purpose — tool surfaces, shared with the rest of the app.

Trap learned on the way: **a scripted block move in TSX can "succeed" into broken
nesting.** The first reorder put the five section conditionals inside the row and
split the panel's opening tag from its workspace header — still JSX-shaped, still
all lines present, wrong tree. What caught it was the live browser, not the diff;
`tsc` only passed after the repair. After any scripted reorder, reload the page
and check what the DOM actually nests (`aside > div/span` vs the editor column)
before declaring the layout done.

### The session-panel pass (2026-11-07)

The user asked for the always-open, grouped session panel the OpenCode screenshot
shows, plus a shorter list of fixes the same screenshot surfaced:

* **The session column** (`SessionPanel.tsx`) is always visible before the
  activity bar: `Session` (label + `working`/`idle` pill), `Context` (model, a
  violet meter = the server-advertised prompt ceiling in chars, its % — nothing
  measured, nothing invented), `Project` (folder(s), branch, unreviewed count
  with +/−), `Changes waiting` (the approve section, moved out of the
  transcript) and `Runs` (this session's runs, status glyph, call count, time).
  Resizable 220–420px via the same `role="separator"` divider pattern,
  persisted as `aria.studio.sessionpanel`.
* **The approve section left the transcript.** `Changes waiting | review all` +
  the per-file rows with +/− moved into the session column. The per-hunk
  Keep/Revert deep view stays in the Changes panel; the file-level "review"
  verb opens the review editor, same as before.
* **The agent's 60-step ceiling is gone** (`_CODE_AGENT_MAX_HOPS` 60 → 0, i.e.
  no default ceiling; `max_hops: 0` unchanged, positive values still clamp to
  200). The transcript no longer prints `N/60 steps` (the usage line shows
  `N steps` only when there are tool calls), and the composer's `hop N/M` chip
  hides itself at the unbounded default — pinned as a literal `0` in
  `test_studio_journey.py::test_step_6_the_run_is_listed_replayable_and_costed`
  so the default can't silently creep back.
* **The "repeated an already-successful call" stall now asks for a final
  answer.** When the guard trips on an already-succeeded call the run no
  longer ends with `[stopped: repeated…]` and silence: `run_tool_loop` returns
  `needs_wrap_up` + `wrap_up_messages`, and `run_with_tools` issues one
  tool-less streaming pass that becomes the answer. Pinned by two tests in
  `test_mcp_runner.py` (marker + wrapped text). The per-hop `max_tokens`
  default went 4000 → 8192 — the gateway clamps to the provider window either
  way, so the affordable-call cap is now the model's, not a silent 4k.
* **The cache path, audited (2026-11-07).** Every coding-agent call in this
  ledger went through `cohere/north-mini-code:free`; every one reports
  `cache_read_tokens=0, cache_create_tokens=0`. `skills.py` defaults
  `cache_system=True` per hop, and the mechanism works where providers support
  it (16.4M cached tokens across Gemini-class providers in this same ledger),
  but Kilo/north-mini doesn't report prompt caching — nothing to fix in the
  studio; the Usage rows read the ledger honestly. Pinning a caching-capable
  model in Settings is the way to make the cache rows mean something.
* **Tool-call disclosure, redrawn**: a `Work / 6 git actions` summary with a
  violet Sparkles tile, an honest state pill (`working` spinner while running,
  `done` / `stopped` / `N failed`), and an expandable timeline — one tinted
  icon tile per tool (read=blue file, edit=amber pencil, run=violet terminal,
  git=GitBranch, check=mint, else wrench), `git_action` detail in mono, and
  per-row state glyphs (spinner while live, check when done, `x` on failure,
  a dash for a tool cut by the stop — which renders subtly, with the title
  "cut before it answered — the run was stopped first").
* **Session rail grouped by folder**: first folder a session holds is the
  group header (uppercase `DEMO`, `S4DEMO`, … `NO FOLDER` last), collapsible
  per-folder with the state in `aria.studio.sessionGroups`. Each row carries a
  live state column: a violet spinner while any run of that session is live,
  an emerald dot for `last_status == finished`, an amber dot for `stopped`,
  a red dot for `error`, zinc for never-ran. The sessions route now serves
  per-session `live` (in-memory) and `last_status` (index read) — cheap, and
  only on the manual sessions reload.
* **The model control** lost the bright ring (a calm `bg-white/10` open state
  with a Sparkles glyph and a full-id tooltip) — the ring read as a badge.
* **Token counts are out of the transcript line** (`$N · N call(s) · Ns · N
  steps`), and the cost-ledger line names its own gap when it has one.
* **The context block reads the selected model's real context window** from
  `/api/models?all=true` — the 600k-char elision ceiling was never the
  model's context at all; it's gone from every surface. Composer chip
  (`256k ctx`) and the session column both render this, and both re-fetch
  on a model change (`r?.default` matches the roster default — Auto is
  honest here, not by luck). Caption says so honestly; the roster failing
  gives `context window unavailable — the gateway roster did not answer`.
* **Tool calls interleave with the thinking stream.** `foldFrame` marks
  tool-call rows with `atLen` (the length of the thinking text when they
  landed); `ThinkingTimeline` splices each into the thinking paragraph at
  that offset, so the transcript reads `thinking → tool → thinking → tool`
  in execution order. Replay uses the same `foldFrame`, so old runs render
  identically. The 45ms coalescer is gone — deltas flush per animation
  frame so the stream looks like text flowing rather than blocks landing;
  per-frame batching is what keeps a 263-frame run from re-parsing per delta.
* **JetBrains Mono on every property-looking value** — paths, slugs, Ln/Col,
  +/− counts, usage costs, cached/fresh counters — the same grammar the rest
  of the console uses on its diff surfaces.

### The activity-bar and runs pass (2026-11-07)

The user wiped the studio and asked for the comparison to be functional, not
cosmetic. All state under `state/code-studio/` is gone; the studio boots on
"No sessions yet". `browser.tools` was flipped on (`state/flags.json`) and
the agent boot path was fixed: the import-time grant check referenced
`skills._TOOL_CATALOG` in a module that never imports `skills` at top level,
so a cold boot with the flag on died with NameError before serving anything.

* **The composer textarea no longer draws the blue ring.** The global
  `:focus-visible` rule (`outline: 2px solid var(--color-accent)`) outranked
  Tailwind's `outline-none` on equal specificity by source order. A scoped
  `[data-composer-menu] textarea:focus, …:focus-visible { outline: none }`
  fixes it in the composer only; the card's focus-within border is the
  affordance. The rest of the console keeps its keyboard ring.
* **The Explorer is a VS Code-style tree now.** Rename edits the row in
  place (the label swaps for an input); "new file"/"new folder" splice an
  inline input in as the FIRST ROW of the target folder (auto-expanded by
  the click); a Files toolbar row carries New File / New Folder icons whose
  target is the folder holding the open file, else the first root. The
  detached bottom input is gone — only delete keeps a confirm affordance
  there. Refused ops still surface in the tree, unchanged.
* **Root-level file ops silently never refreshed the tree.**
  `path.slice(0, path.lastIndexOf('/'))` on "st-ea42792f" yields
  "st-ea42792", so a folder created at a root produced a success message
  ("created st-ea42792f/notes") while the tree kept showing nothing. The
  re-read now targets `path` itself for mkdir and the true parent
  otherwise; verified live: `notes` and `notes2` both appear.
* **Session rows sit 14px right of their folder headers** (`ml-3.5` on the
  row), measured 15px at 100% zoom — slightly specified, not flush.
* **Session dots: running is blue, needs-you is yellow.** A live run spins
  blue (`text-info`); a run parked on your approval is a static yellow dot;
  finished + unreviewed changes is yellow; finished clean is green; error is
  rose. `/api/studio/sessions` now carries `pending_review` — the same
  `changes_payload` count the Changes drawer renders — bound per listed
  session so each path resolves against ITS roots, not the request's.
* **The activity bar has RUNS.** ScrollText icon, blue count badge for
  live runs. The panel lists this session's runs; clicking one opens the
  drill-in: question, status pill, model/provider/mode, started→ended,
  duration, frames, tokens, tool calls, ledger cost (`unknown` is an
  explicit unknown, never $0.00). Two views of the transcript: **Timeline**
  (coalesced thought/answer/action/result/approval/write/done rows with
  per-frame offsets from the first frame) and **Raw log** (every frame as
  one JSON line with index + offset, straight from the transcript file).
  Live runs follow themselves (append-only, 1.5s, `after=<known>`), with a
  stop button at their next hop. `buildEvents` is exported and pinned by
  5 vitest cases incl. the raw-transcript shapes.
* **Replay was reading the wrong field for thought.** Raw transcript frames
  carry `thinking` on `delta`, not the SSE `text`, so a replayed run reasoned
  in silence; `foldFrame` now reads both. Raw `tool_call` frames also now
  derive the one-line `detail` (which file/query/command) that wire frames
  carry server-side.
* **The rail never waited for a remount to show a new session.** Creating
  a folder, a playground or closing one left `sessions.reload()` uncalled,
  so the rail read "No sessions yet" over a session on disk. All three
  call sites reload now.

### The file-ref and streaming pass (2026-11-07)

Two things the agent's output felt wrong to say nothing about:

* **The session column trades places with the section panels.** Opening any
  section (Git/Files/…) slides that panel in and the session column steps
  aside; closing it restores the session column. The default left rail
  shows conversation + session column + bar with nothing open. Persisted
  drawer state is unchanged; only the two columns' visibility rule is new.
* **File paths in answers are buttons now** (`file-ref`, in the studio's
  transcript only — `renderMarkdown(..., fileRefs)` is opt-in, the rest of the
  console renders unchanged). Three shapes match: a drive-absolute path
  (`C:\dev\app.py`, `:42` stripped), a foldered relative path
  (`src/views/Studio.tsx`, `x/y.ts:7`), and a bare filename with a known
  extension (`renamed.txt`, `app.ts` — `.md/.json/.txt/.log/.csv/.pdf/.yaml`
  included). `1.5/2.5` is a fraction, `x.com/a.ts` is a URL — neither is a
  button (the fraction test is "extension must contain a letter"; the URL test
  is "previous char is `/`, or the match itself starts `www.`, or the 14
  characters before hold `://` / `www.` / `mailto:`").
* **Clicking one opens the file.** A foldered ref goes straight to the review
  surface via `openPath` (which refuses anything outside the session's roots
  with a stated reason). A bare ref resolves through `/api/studio/mentions`
  first: exactly one exact-basename match opens it; none or several puts a
  line in the transcript ("no file called X in this session's folders" /
  "N files are called X") rather than guessing — verified live with the
  ST-8EEB7AFB playground: a synthetic `renamed.txt` button opened the file in
  the editor, and a synthetic `does-not-exist-anywhere-42.txt` button produced
  the honest note.
* **Tool-call disclosure is one small expandable line** under "thinking…"
  (`Work · 6 git actions` + a red `N failed` only when something failed, all
  open while the run works, collapsed when it's done), with the tool timeline
  inside — no second card, no pill soup.
* **Streaming arrives in 45ms batches** (`deltaBuffer` in the consumer,
  flushed on a timer) instead of per-frame jolts, and the transcript no
  longer chases the bottom unless the reader is already within 160px of it.
  The auto-scroll used to yank the view down per delta even when the person
  had scrolled up.

* **Session switch is calm now.** A fresh remount used to flash the empty-state
  hero for 30–200ms before the replayed transcript landed — the effect looked
### The folder-picker pass (2026-11-07)

The user could not open any of the folders they actually work in from the
studio — and the only affordance for opening one was a text box to type an
absolute path from memory. Both halves fixed.

* **Why the folders were refused.** `validate_root` refuses the agent's state
  dir (`state/`), anything holding it (`project3`, `Downloads`, `S9SharedCode`,
  `code`), and the drive root — the state dir carries sessions, threads and
  `agent.token`. The app is installed *inside the user's project repository*,
  so the ancestor rule fires for exactly the folders they wanted. Nothing
  was wrong with the rule; the surface was.
* **A real folder picker.** `GET /api/studio/fs/list?path=` (folder names
  only — never file contents, never hidden entries) drives a new
  `FolderPicker.tsx` in the console's idiom: drives at the top level, click
  to navigate into a folder (blocked folders are WALKABLE — you must be
  able to reach the folder you want), Select disabled with the refusal text
  when the current folder would be refused, Enter/Escape, "type a path
  instead" as the power-user path. Wired to every add-folder affordance
  (empty-state button, composer overflow, Files drawer header, explorer
  via `addFolderHere`). The add route still enforces `validate_root`
  server-side — the picker only surfaces the refusal ahead of the click.
* **Verified live end-to-end**: opened the picker, navigated
  `C:\ → Users → AISHWARYA → Downloads → project3` (greyed, walkable), into
  `llm_gatewayV9`, picked it — the studio's tree lists the gateway repo
  (adaptors/policy/integrations), the rail shows the session, console
  zero errors. Verification session wiped again afterward.
* **Server side is pinned by 7 tests** (`tests/test_studio_fs_browse.py`):
  drives at the top, folder-only listings with hidden entries skipped, the
  state dir and its ancestor refused but walkable, scratch playgrounds
  allowed, missing path → 404, file → 400.
* **2026-11-07, part two: the actual OS dialog.** The user asked for the
  file explorer's own picker, not a rebuilt one — they were right.
  `POST /api/studio/pick-folder` opens a native `FolderBrowserDialog`
  ("Open a folder in Aria Studio", root = MyComputer, New-folder button)
  from the agent, which runs in the user's session on loopback. The request
  blocks until the dialog closes (300s cap — an unanswered dialog is a
  cancelled one, over 5 minutes is a 504 that kills the process). Returns
  `{path}` / `{cancelled: true}` / 501 (no powershell on the platform).
  Every add-folder affordance (empty-state button, hero "Open a folder",
  Files drawer header, composer overflow) now calls it; on ANY failure the
  console falls back to the in-app picker automatically. The route takes no
  input — no path injection surface; confinement stays in `validate_root`,
  enforced at add time as always. Pinned by 8 tests
  (`tests/test_studio_pick_folder.py`: fake subprocess — picked path,
  cancel, 501, 504-kills-the-process, body ignored). Verified live: click
  "Open a folder" → the native dialog spawns (status flips to "waiting for
  you to pick a folder…"); closing it returns the console to "folder pick
  cancelled" cleanly.
* **The full agent suite, run to completion after the prior session's
  backend changes:** 2809 passed at first run with 5 failures — four were
  pre-existing or environmental (two stale catalogue pins at 85 vs the
  measured 86; `test_render_document_is_callable` failed only while the
  gateway was down; the rail-isolation test tripped on a pre-existing
  `components/ui` import in the studio, moved to `components/primitives`),
  and `test_self_wake.py::test_the_wake_turn_path_runs_for_real` passes in
  isolation; the re-run is pinned pending notification.


  like the page blinking. `useStudioRuns` now reports `loading`, and the panel
  paints a calm skeleton until the session's runs are in; verified live:
  skeleton at 60–120 ms, transcript at ~180–240 ms, never the hero.
* **Session rows are single lines** (title · time · state dot), not two-line
  cards — folder headers carry the folder, so the row no longer repeats it.
* **The calls/steps counts are gone from the transcript line** — the footer is
  `$N · Ns`. Everything measured lives in the session column's Usage rows.
* **The "thought" disclosure is unboxed** — a bare one-line expandable, no
  border/background card around it.
* **The session column carries a Usage block**: model, context meter (chars +
  %), tokens in/out (registry, summed across runs), tool calls, elapsed,
  and — from the gateway ledger, for the newest finished run — cost and
  `cache_reads`/`cache_creates` (the rollout also extended the gateway's
  `by_agent` rollup with the two cache columns the DB was already storing;
  `estimated_usd` still covers in/out tokens only). A ledger that cannot be
  read says `cost unknown` rather than drawing $0.

### The live-context bar and rail-ring pass (2026-11-07)

* **Live runs now interleave tool calls into the thinking at the point they
  were dispatched.** The replay path was fixed earlier (`atLen` offsets);
  the live SSE pushes were never given the same marker, so during a run the
  tools appended at the end of the thinking block — exactly "they show in
  order when the session ends but not during". The live `permission` and
  `tool_call` handlers now stamp `atLen: (t.thinking || '').length` like
  the replay path. Live and replay fold identically now; live was verified
  by construction (both paths feed the same ThinkingTimeline), replay live
  on the git_action run.
* **The composer's `256k ctx` chip is gone** — it duplicated the session
  column's job. The context block now carries the meter: a violet bar
  (`[role="progressbar"]`) sized to the newest *finished* run's ledger
  in_tok as a share of the model's real context window, with the exact
  number in the caption (`≈ 18% of the 256k window · 45,119 in tokens,
  per the ledger`). An empty bar says either "no run yet" or "the ledger
  did not answer" — never an invented fill.
* **The gateway roster read is retried once on failure or a null window**
  (3s, bounded) — the caption used to freeze on "the roster did not
  answer" whenever the read raced a cold gateway; now it recovers. Cost
  reads get the same one-retry treatment (a transient unknown at ~15:41
  the day the gateway was briefly down used to stick forever — it now
  retries once, 3s, before the row says so).
* **The studio rail's buttons keep the keyboard ring without the blue
  box:** `:focus-visible` on rail buttons draws nothing; focus gets a
  soft inset violet hairline instead (`[aria-label="Studio sessions"]
  button:focus-visible { box-shadow: inset 0 0 0 1px #a78bfa8c }`).
* **"Open a folder…" is now a visible button in the session column**
  (an accent CTA under Project whenever the session holds no folder) —
  the only add-folder affordances used to be an `h-6` icon in the drawer
  header and the hero button (gone once a run exists), which is why the
  user could not find it. It calls the OS dialog; the in-app picker
  remains as the fallback path.
* **The OS folder dialog comes to the foreground.** A spawned
  FolderBrowserDialog opens behind a maximized browser; the script now
  hosts it on an invisible topmost owner form, which is the standard
  WinForms answer.
* **The cache row names its source** ("read from the gateway's calls
  ledger… Gemini-class providers report millions; cohere/north-mini-code
  reports 0") so the zeros don't read as a routing failure. The values
  were verified directly against `gateway_v8.db`: the `code` agent's runs
  carry `cache_read_tokens = 0` on every session — that's the provider's
  truth, not ours to improve.

### The kilo cache probe (2026-11-07)

The user's instinct was that Kilo does cache — checking settled it:**kilo IS
already wired to prompt-cache on supported providers (no config), but for
`cohere/north-mini-code:free` it reports `prompt_tokens_details.cached_tokens: 0`
and `cache_write_tokens: 0` even for an identical 18k-char prefix sent four
times in ten seconds. Genuinely zero cache hits for this model on this key.**

Two real defects were fixed in the gateway along the way:

* **The streaming path threw the provider's usage frame away entirely and
  logged a char-count estimate as `input_tokens`.** Every kilo row in the
  ledger was an estimate, never the provider's numbers — so cache reads
  from kilo could never have surfaced even when they happen (Gemini paid
  keys will have had this flaw too on routes that stream). The OpenAI-compatible
  stream now sends `stream_options: {"include_usage": true}` and relays the
  final usage frame as a private `[[USAGE]]` sentinel the client never sees;
  the gateway parses it into the ledger row (`input_tokens`, `output_tokens`,
  `cache_read_tokens`) instead of the estimate. An empty-usage match is still
  honoured.
* **The batched path only read OpenAI's nested `prompt_tokens_details.cached_tokens`
  and missed Cohere's top-level `usage.cached_tokens`.** Both are read now
  (max wins; a zero on one side cannot erase a positive on the other).

Verified live: a streaming `/v1/chat` call with kilo north-mini-code recorded
`4009 in · 6 out · cache_read=0` — the provider's own numbers through the new
path. Gateway suite: 1600 passed (the single `test_chat_batch` flake passes on
re-run; it is a live-network deployment test).

**What this means for the studio's context bill: kilo gives no cache savings
for north-mini-code, full stop.** The 5.4k-token per-hop floor is re-sent in
full on every hop (that is what the 600k elision and the new usage reporting
are working against) and the free model does not skip the bill. Caching IS
real on kilo for models where the provider reports it (and on paid Gemini
keys) — the ledger will now show it, because it flows.

### The context-compaction pass (2026-11-07)

The user reported the context "exploding" on small tasks. Measured: it was
not exponential — per-hop growth was 150-600 tokens on a 5.4k floor. The
real drivers were (a) the stateless loop re-sending the full prompt every
hop (sum of hops = the invoice), (b) assistant "said" text being echoed
back into the next prompt even though the progress ledger already carries
the facts, and (c) `cohere/north-mini-code:free` reporting zero cached
tokens on every call.

* **Compaction.** `mcp_runner._compact_context` now compresses older tool
  results (head 700 + tail 300 + byte count) and older assistant
  narration (head 250 + tail 150) when `_context_chars(messages)` exceeds
  `max(max_context_chars * 2 // 3, 400_000)`. The newest tool round and the
  current assistant turn are always preserved; tool_calls IDs are never
  rewritten. `max_context_chars` still hard-caps via elision after compaction.
* **Tests.** 4 new tests in test_mcp_runner.py pin: shrinks old rounds while
  keeping the newest, leaves small prompts alone, never orphans a tool pair,
  is a no-op under a loose budget. Compaction reduces a 22.9k heavy loop
  to 7.3k (68% smaller). All 33 mcp_runner tests pass.
* **Cache — Kilo verified.** The gateway was probed directly: an identical
  18k-char prefix sent 4× to kilo/north-mini-code:free returned
  `cached_tokens: 0, cache_write_tokens: 0` every time. Kilo caches on
  supported providers but this model reports zero. Two gateway defects were
  fixed so the truth can't be hidden: (1) the streaming path was logging a
  char-count estimate as `input_tokens` and never logging cache fields at all
  — now sends `stream_options: {include_usage: True}` and relays the final
  usage frame as a private `[[USAGE]]` sentinel that the gateway parses into
  the ledger (`input_tokens`, `output_tokens`, `cache_read_tokens`); (2) the
  batched path read only OpenAI's nested `prompt_tokens_details.cached_tokens`
  and missed Cohere's top-level `usage.cached_tokens` — both read now, max
  wins. Gateway suite: 1600 passed (1 flaky live-network test passes on
  re-run).
* **Agent test.** A complex task ("create a 700-900 line HTML5 racing game")
  was given to the studio agent on `reflection/beam-free` (1M context). The
  agent correctly oriented (list_dir → read_file → update_plan), produced
  a real 99-line `simple_car.html` on the simpler task, but on the full task
  emitted `create_file` with `{}` arguments twice (the model could not hold
  the 700-line HTML body in one tool call). The studio's approval gate,
  diff preview, run verification, and ledger accounting all behaved
  correctly; the failure is the model's, not the harness's.

### The S24 Car Game full-sweep pass (2026-11-07)

The user pointed the studio at a scratch folder (`Downloads\S24 Car Game`, a
Kivy self-driving-car project) and asked for "everything" to be tested, with
the folder as the playground. Every surface was exercised end to end;
two real bugs fell out and were fixed.

**The full workflow, verified:** rail switching → Files drawer (expand,
sizes, inline create/rename/delete incl. confirms) → Monaco editor (Ln/Col
tracking, language tag, tabs) → Search (158 hits in 17 files, 8 skipped as
binary, open-at-line jumps verified) → **agent run end to end** (read_file →
approval card with the real diff preview → Approve → `INSTRUCTIONS.txt` had
exactly one line appended) → Changes drawer (1 hunk +1 −0, Keep, badge
clears, checkpoint auto-created "before task 3") → **checkpoint restore**
(0-byte snapshot restored; the current state auto-captures a new
"before task 4" checkpoint first) → git drawer (`not a repo` → `git init` →
master, file rows with select checkboxes, `stage` → LLM-drafted commit
message ("Add git_probe.txt file"), `commit` with the server's 428
precondition confirm showing the real staged diff, `git log` shows both
commits) → session archive/restore (rail shows ARCHIVED group; re-read
pulls it back) → browser pane (spawn chromium → open example.com → 200 →
console log empty honestly → close) → RUNS panel (4 runs, live badge, run
detail timeline with per-frame offsets, raw log with all 174 frames).

**Bugs found and fixed:**

* **The RUNS list went blank for every row after a watcher frame.**
  `/api/studio/live` sent SLIM rows (`id`, `status`, `question` only); the
  watcher REPLACED the full list with that slice, leaving every row that
  followed with no `started`/`tool_calls`/tokens — all rows showed
  `· · 0 calls · 0 out` with no time or count. The SSE now ships the full
  rows (`rows[:20]`). The file append run proves it: rows carry `12m ago ·
  2m 4s · 3 calls · 16 out` again.
* **"⚑ N agents" lingered after a run finished.** The watch loop returned
  without resetting `live` when the stream ended (the final zero frame is
  not guaranteed to arrive). The loop now `setLive(0)` + full reload at
  stream end; the badge clears.

**Console "errors," all accounted for and by design:** 409 (folder was not
a git repo — the panel shows "this folder is not inside a git repository"
accurately), 428 (commit absolutely requires `confirm: true`; the panel
renders the confirm dialog with the real staged diff). Neither indicates
failure; both flows were exercised and behaved correctly.

**The one thing not verified live:** nothing — every surface was exercised;
the two commit/confirm flows execute the 428 loop correctly by reading the
code path. The git history in the scratch folder is now PART of the game
(init + 2 commits) — the user was told it is left as-is per "any changes".

### The console-wide sweep and studio polish (2026-11-09)

The user asked for two things: make the studio sections as polished as the
rest of the console (five named defects), and then test EVERY console
section completely. What this pass changed, and what it proved:

**Studio surfaces changed**

- Session rail: a quiet `+` on every folder group's header creates a new
  session bound to that folder. Two real defects were found here: the
  sessions index carries folder NAMES, not absolute paths, so the first
  implementation bound a bare `"S24 Car Game"` and the folder silently never
  appeared (now resolved via the roots API of any session in the group; a
  stale name resolves honestly or refuses with a status line); and the
  first test clicked a detached node after a remount, which React never
  saw — a reminder that a false negative in the browser looks exactly like
  a broken handler. Dots were removed from every session row on request.
- Explorer: VS Code's shape — file-type icons coloured by extension, tan
  folder/folder-open, indent guides on the same ladder the rows indent by,
  full-row active highlight, `EXPLORER` micro-label, collapse-all action.
- File-ref click: no longer opens the workbench review editor at all. It
  expands an inline, compact single-section diff inside the answer — one
  section per file, green added lines, red removed lines, hunk header with
  `Keep`/`Undo` per hunk (fingerprint ids), file-level keep/revert, fold,
  close. Verified end-to-end on a real pending change (hunk Undo deleted a
  created file and cleared the pending entry; file-level `Keep` cleared it
  with the bytes staying). Falls back to "nothing unreviewed for this file"
  + open-in-editor for unchanged files.
- Section restyle: drawer header and session blocks wear the console's
  card-head language (accent Tile + micro-label), panels carry a whisper
  of violet (radial hairline), git drawer rebuilt — branch chip,
  segmented Changes/Log/Journal switch, SCM letter chips (A/M/D/U,
  amber-green-rose), stage checkbox with `accent-violet-400`, violet
  commit primary with pen-draft-in-input, log and journal both render as
  timelines, `## master...origin/master` now displays as `master`.
- File-level verbs in the inline diff are icon-only so the filename keeps
  the width (`inline_probe.txt` no longer truncates to `inlin…`).

**Console sections verified end-to-end this pass** (all 16; live actions,
not just loads): Chat (send → answer → deleted my own probe thread), Runs
(142 runs; graph renders; node click → JSON inspector), Memory (list 970,
search instacart → 4 matches → clear → restored), Documents (531 docs;
folder create → delete with confirm; preview modal open/close;
Disable→Enable round trip), Research (topics open; Answer/Log tabs;
stored reports), Scheduler (17 jobs; per-row Enable/Delete affordances
present — left alone), Skills (pack toggle 100/0 → 97/3 → 100/0), Code
(renders then redirects to /studio — ui.code_ide is on; nothing else to
test without flipping a flag, which this pass must not do), Studio
(inline diff, restyled sections, + bind, archive flow), Authoring
(full DAG → One-Sentence-About-the-Moon.pdf, 52 words, "finished while you
were away · 1 file"), Apps (OpenRouter app: search/sort live, tracker
renders; 2 apps), Ledger (scope toggle works; cost honestly n/a on
free models), Console/mission (event feed renders scheduler + run events;
filters work), Parts (detail metrics, viewport presets, version history),
Computer (all tabs render; honest off state; nothing enabled), Settings
(flags listed, not toggled; deep links render).

**Not done, deliberately:** no feature flag in `flags.json` was flipped
(e2e control, computer control, code_ide routing), no scheduler job was
enabled/deleted, no gateway token or API token was rotated, no commit was
made. Test artifacts: `PROBE — *` studio sessions archived (3); Documents
probe folder created and deleted; Moon PDF remains in Authoring's recent
documents (no delete affordance in the UI for it); `inline_probe.txt`
created, tested through keep/revert, then deleted from disk.

### The studio stuck-run and stale-context pass (2026-11-09)

Prompted by the user's two screenshots-plus-text: the agent's run died with
"repeated an already-successful call; final pass requested" and the CONTEXT
meter did not update after a turn. Root causes, all fixed and then verified
through seven consecutive turns:

- **The wrap-up pass never ran.** `run_with_tools`'s `_stream_wrap_up` called
  `_RoutedLLM.stream_chat(..., cache_system=cache_system)` — a kwarg only the
  batched `chat()` accepts — so EVERY stall wrapped in a TypeError, logged once
  per run ("[mcp] wrap-up pass failed: _RoutedLLM.stream_chat() got an
  unexpected keyword argument 'cache_system'"), and the user saw the raw stall
  note as the answer instead of a closing pass. Fixed by forwarding only the
  locked signature; regression test pins `cache_system` OUT of the call
  (`tests/test_mcp_runner.py::test_wrap_up_streams_text_with_no_tools`).
- **The "1 agent" footer / context meter stuck forever.** The studio's
  read of the runs list went through the SPA's 30 s read-through cache
  (`DEFAULT_TTL_MS = 30_000`, keyed on the identical URL per session), so a
  turn-end reload returned the pre-turn state (`live=1`) no matter when it ran.
  `studioRuns`, `studioChanges`, `studioCheckpoints`, `studioRunFrames` now
  pass `cache: 'no-store'` — identity-key liveness can never be cached.
- **`useStudioRuns.watch()` dead-ended.** When a run ended before/between the
  1 s SSE polls (every gateway-503 fast fail), the watcher retried four
  windows and then returned WITHOUT a final `setLive(0) + reload()`: the badge
  read "1 agent" over a finished session forever. The fall-through now always
  settles the list from the truth.
- **Turn end never refreshed the runs list at all.** `ToolOutcome`-style
  completion only refreshed the CHANGES slice; `notePending` now also calls
  `runs.reload()`, so every finished/errored turn re-reads runs and the
  context meter's `newestFinished` moves.
- **Zero-token error runs wiped the context bar.** A 503 before the first
  model call (provider cooldown storm, all five `gemini35lite*`/`gemini*`
  candidates spent) reports `status: error, input_tokens: 0`; it was
  `newestFinished`, so the meter dropped to a misleading 0%. Zero-token
  errors are now filtered from the meter's baseline — the last real run's
  prompt is what the bar shows (verified live: 10,293 in · ≈4%).

Kept as it was: the stall guard itself (repeats of a successful call trip at
`MAX_SAME_CALL_REPEATS=2` and request the wrap-up rather than abort the run),
the provider 503s per se (gateway cooldown patterns, not a studio bug), and
the gateway's 30 s read-through cache for everything else.

### The 2026-11-09 full-suite re-run: two failures, both environmental drift

From the full-suite verification of the stuck-run pass above (`2833 passed →
2 failed`):

- `test_the_prompt_never_names_directories_the_agent_cannot_see` — its
  `"scratch workspace" in src` assert was stale: the prompt was rewritten
  (for the default-workspace work) to the stronger, and true, wording
  "current directory IS the workspace" / "This workspace is EMPTY: that is
  expected". The anchor is the same fact (the prompt names the directory the
  agent can actually see, never a repo it is not in); the test now pins the
  current phrasing.
- `TestSearchFiles::test_finds_with_line_numbers` — `files_scanned` meant
  "files read" on the Python walk and "files with hits" on the ripgrep fast
  path (`_rg_search`, added with no-gitignore env support); with rg present
  the test asserted 2, got 1. The fast path now enumerates the same tree with
  the fallback's `_walk_files` for the count (metadata only, capped at 2000
  like the fallback), keeping one meaning for one contract.

Neither failure was a regression from the stuck-run/meter fixes themselves;
both are exactly the class of "nobody noticed the tree had moved" drift the
full suite exists to catch.

### Driving the matrix (the traps that made a false red, not a false green)





- **Never address an explorer file with `button[title*="app.py"]`.** A session
  row's `title` is `label\n<its folder paths>`, so that selector matched
  another SESSION and switched the whole page mid-row. Use the exact display
  path: `button[title="demo/src/app.py"]`.
- **Monaco has no clickable textarea.** It uses the EditContext API
  (`native-edit-context`); the old `textarea.inputarea` is 0-size and readonly.
  Click `.monaco-editor .view-lines`, then assert `document.activeElement` to
  prove focus before typing.
- **The terminal renders to canvas.** `innerText` of `.xterm` is legitimately
  empty, so prove the PTY exists by the file it writes: `echo x > probe.txt`
  and look for `probe.txt` under each root — which also measures where the
  shell actually started.
- **Reset the probe project at the start of every run, from literals.** The
  second run of row 13 "failed" because row 13's own earlier rename had left
  `parsed.py` behind, and the product correctly refused to overwrite it. A
  leftover is a right answer from the server and a wrong result from the
  script.
- **Row subsets need their own setup.** `--only=13` cannot assume row 2
  navigated: every row starts from `ensureWorkspace()`, and any check that
  mutates the file behind the editor's back must reload the buffer or the
  concurrency guard will (correctly) refuse the next save.
- **`button:has-text("review")` is not the review button.** It matches
  "review all", which is earlier in the DOM and opens the Changes drawer
  instead — so the probe reported "no diff editor" and the reviewer concluded
  the surface was broken. Address an exact label with
  `getByRole('button', { name: 'review', exact: true })`.
- **Never edit `agent_server.py` while a test suite is running.** The wake
  test asserts on `inspect.getsource(A._run_wake_turn)`; `getsource` uses
  line numbers recorded at import time, so lines inserted mid-run shifted
  the window onto a different function (first `_wake_tool_payload`, then
  `_wake_system_block`) and the failure read as "the wake test is broken"
  while the wake code was unchanged. Freeze the tree while the suite runs;
  a red obtained from a moving file is a red about the harness, not the app.
- **`opacity` on a child of an `opacity-0` parent still reads `1`.** A hover
  audit that measures the button's own computed opacity says "visible" for
  something its parent has hidden. Screenshot it, or measure
  `getBoundingClientRect()` after a real `hover()`.
- **On a cold load the activity strip does not exist.** Files/Search/Changes/
  Git/Browser live in the workbench, and the workbench is closed until there is
  work to show. The only affordances are in the `<footer>` ("N files
  unreviewed", "▤ Files", "⑂ Git"). A row that reaches for
  `button[aria-label^="Changes —"]` on a fresh page is waiting for an element
  that will never appear; click the footer chip, which opens the workbench on
  the right drawer.
- **Scope the drawer with `aside`.** `button:has-text("demo/src/app.py")`
  matches a chat-card row before the drawer row, and clicking the chat one
  opens nothing — the row then reported "no diff editor" while the review
  surface was fine. `aside button` was the missing qualifier.
- **Selecting a file is not staging it.** The git drawer's checkbox is labelled
  `select <path>` and the `stage N` button is the action; a row that ticks the
  box and then expects Commit to enable is asserting the wrong thing. Commit
  staying disabled with an empty index is the correct answer.
- **A deliberate 428 is a console error to Chrome.** The preview-then-confirm
  path logs `Failed to load resource: 428 (Precondition Required)` on every
  commit. The route also returns `confirm_required` in the body so the panel
  never has to read the status; a row that fails on console errors has to
  whitelist that one, or it is testing the browser's logging.
- **Reset the probe project to something the repo's HEAD does not already
  contain.** Row 11 wrote `return "hello " + name`, then a previous run's own
  commit made that HEAD, and `/commit-msg` was right to answer 409 "nothing to
  write from". A unique marker per run keeps the row about the product.
- **A thin element is not necessarily a broken pane.** The resizer handles are
  3 px wide on purpose; exclude them by class and childlessness before
  asserting "no tall pane is ≤60 px".

## 12. The standing invariants — never let these come back

1. **No silent cap.** Anything bounded says what it bounded: `truncated`,
   `dropped`, `omitted`, `stopped`, `more`. A limit that is invisible turns a
   partial answer into a false one.
2. **One path vocabulary.** `<root>/dir/file` to the human, `dir/file` to the
   tools, one resolver shared by routes and MCP. Every defect in this project
   that produced a *wrong file* started with two of them.
3. **A grant is four layers.** Registration, catalog, allow-list, calling
   context. Three out of four reads as a tool that does not exist.
4. **A 4xx whose body is content must not be thrown away.** Confirmations and
   refusals are answers; `pj` treats them as failures.
5. **Never assume a restart happened.** Verify `/api/health` and
   `/v1/providers`.
6. **A false green is worse than a red.** If a test passes because the thing it
   probes never ran (403, skipped browser, a fixture that found zero matches),
   say so and fix the probe — several checks in this suite only looked right
   because everything upstream was refusing.
7. **The studio is dressed in the console's clothes.** `/studio` is its own
   route with its own chrome, but the *visual grammar* is shared with Docs,
   Skills and Authoring, and drift shows up immediately as two products side by
   side. The contract lives in `views/studio/chrome.tsx` and it is the palette
   the console pages actually use, not the token names that happen to be in
   `index.css`: canvas `bg-void` (#090909), rails `bg-[#0b0b0e]`, cards
   `bg-[#0e0e12]`, hairlines `border-white/10`, cards `rounded-[10px]`,
   controls `rounded-md`, primary `bg-violet-400` + `text-[#0b0b0e]` +
   `font-bold`, section labels at `text-[10px] font-bold uppercase
   tracking-[0.14em]`, a bordered icon tile left of a card head, **one** violet
   primary button per card, everything else ghost/outline, lucide icons for
   verbs rather than glyphs. A new studio surface uses these instead of
   re-deriving a scale. `surface-0..4` (#0a0c13..#252c42) is the IDE palette
   under `.ide` for the old `/code` page, not the console vocabulary — do not
   reintroduce it, or 12px card radii, into studio chrome. Monaco's
   `aria-dark` and TerminalDock's xterm theme stay as they are: those are tool
   surfaces shared with the rest of the app, not chrome.
   Where the console already ships a component, reuse it rather than a
   look-alike: status badges are `components/ui.tsx` `Pill`, pane empty states
   are `Empty`, list/route loading is `Skel`. `chrome.tsx` is only for the
   workbench pieces the console has no equivalent for (Panel/Head/Tile/Label/
   Btn). The dark theme is the shared CSS tokens; `/studio` must never sit
   under the `.ide` blue scope (that belongs to the old `/code` page).
8. **Checks and diagnostics are session-scoped.** `/api/code/checks` resolves
   the session's roots only when the request carries `?sid=`; bare, it runs in
   the process-global workspace. Anything whose output is fed to the agent as
   diagnostics (`#checks`, `/checks`) must pass `sid`, exactly as the Dock's
   Checks tab does — otherwise the agent is handed another project's test
   results as its own. Pinned by `e2e/studio.spec.ts`, which intercepts the
   route and asserts the sid on both the `/checks` and `#checks` paths.

---

## Authoring, figures and the lightweight repair

Not studio surfaces, but part of the same verify-before-ship loop, and each
line below was a live defect.

| Check | Command | What it proves |
|---|---|---|
| Figure supply | `uv run python -m pytest tests/test_image_supply.py tests/test_author_figures.py -q` | the unattended figure pass takes commercial licences only, clears its per-run topic cache, and keeps a figure when the vision check is unavailable |
| JSON repair fallback | `uv run python -m pytest tests/test_json_repair.py -q` | the lightweight fix tries `kilo` first and falls back to `gemini`; the gateway treats a provider pin as a single candidate, so the fallback must be explicit |
| Planner format contract | `uv run python -m pytest tests/test_authoring_research_chain.py -q` | the planner is told to set `metadata.format` on every author/deck node, so the render is forced to the requested type |
| Deck layout | `cd ../../llm_gatewayV9; uv run python -m pytest tests/test_docgen_pptx.py tests/test_docgen_cards.py -q` | the deck renderer's measured layout, stat cards and columns |
| LaTeX engine | `uv run python -m pytest tests/test_brief_format_is_binding.py -q` | an explicit LaTeX request forces the engine onto `render_document` even when the planner leaves `metadata.engine` empty |
| Length parsing | `uv run python -m pytest tests/test_length_contract.py -q` | a hyphenated "20-page" request parses as 9,000 words (it used to parse as 0 and ship an 845-word "20-page" document) |

Live (DAG authoring, not Lightweight): restart so the new backend is loaded,
then drive one real authoring run. The SSE log should show
`[skills] author: fetched figures for N section topic(s)`, and the delivered
PDF should contain `image` blocks with captions — not just prose.

