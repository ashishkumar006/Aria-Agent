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

The prompt is a string literal inside the route; nothing else reads it, so a
false claim in it has no symptom except an agent that behaves oddly.

- [ ] Every backtick-quoted tool name in the prompt resolves in the payload the
  same request produced (`tests/test_studio_prompt_truth.py`).
- [ ] The workspace description names only what is true of **this** session's
  folder: `list_dir(".")` is how the tree is learned, and the shipped repo's
  directory names never appear. A fixed description naming
  `S9SharedCode/code` made the agent hunt for folders that were not there.
- [ ] The mode paragraph matches the gate the route applies, per request —
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
   side. The contract lives in `views/studio/chrome.tsx`: a `glass` sticky top
   bar carrying the crumb (`Studio / <session>`); 12px radii and `border-edge`
   on surfaces; `bg-surface-1` cards with the 2px accent hairline along the
   top; section labels at `text-[10px] font-bold uppercase tracking-[0.14em]`;
   a bordered icon tile left of a card head; **one** violet primary button per
   card, everything else ghost/outline; lucide icons for verbs rather than
   glyphs. A new studio surface uses these instead of re-deriving a scale.
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

