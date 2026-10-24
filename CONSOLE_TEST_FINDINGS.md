# Console test-user findings

> Role: I am acting as an ordinary test user on the console (`http://127.0.0.1:8500`).
> Ordinary users do NOT follow happy paths: they double-click, paste junk, submit
> empty forms, cancel late, resize smaller, and give up too early. Everything
> below was probed the way such a user would behave.
> Scope note (user instruction): cybersecurity/hardening issues are handled LATER â€”
> this file focuses on UX, UI and functional inconsistencies.

**Working method, per section**
1. Load the section, screenshot it as the user first sees it.
2. Probe every visible control the way a careless user would: empty submits,
   whitespace/emoji/5000-char inputs, double-clicks, rapid repeated toggles,
   cancel/retry, invalid values, keyboard-only paths, small-viewport resize.
3. Watch for: no feedback, silent no-ops, double-triggered writes, wedged
   loading states, dead buttons, misleading labels/counters, layout breakage,
   unreachable controls, inconsistent copy between sections doing the same thing.
4. Append findings here with severity, then keep probing the next section.

**Severity legend**
- **H** = breaks a task or loses user work / data
- **M** = confusing or annoying; user needs workarounds
- **L** = cosmetic/polish

## What was fixed (2026-10-18)

Every fix below was verified in the browser, not just compiled.

| # | Fix | Verified by |
|---|---|---|
| H | **An interrupted chat turn no longer loses the question.** The user half is written to the thread *before* the answer streams (the old code wrote both halves in one block at the end, so disconnecting lost both). "Start new chat" mid-answer now also *says* what happened instead of blanking the transcript. | Live: sent, abandoned mid-stream, thread read back â†’ the question is there. Two regression tests (`test_the_question_is_stored_before_the_answer_is_known`, `test_a_completed_turn_stores_the_question_exactly_once`). |
| M | **Memory can create an observation.** The kind select offered only fact/preference/tool_outcome/scratchpad while the sidebar filtered on observations and the page advertised 20 of them. | Live: options now `fact,preference,observation,tool_outcome,scratchpad`. |
| M | **Reopening a research run now shows its report.** `openTopic` fetched the stored report into `answer`, but the pane rendered only `turns` â€” so every past run looked empty ("Ask a topic to begin") beneath a report that had been fetched. | Live: reopened "Capital of Portugal" â†’ the report renders, the contradictory empty state is gone. |
| M | **A finished run can be continued.** Reopening a topic now seeds the conversation id from the run, so the follow-up composer appears and a follow-up actually resumes the conversation instead of starting an unrelated run. | Live: follow-up box present on a reopened run. |
| M | **Code can create a file.** Added `/api/code/newfile` (confirm-gated, plain filename, same confinement as every read, `O_EXCL` so it can only ever add an empty file) and a "New file" button in the explorer. Previously a fresh playground was empty, "Open folder" needed a folder that existed, and nothing in the console could create anything. | Live: created and opened a file in Monaco. Six tests cover the write surface (confirm required, no overwrite, no escape, extension required, allow-list). |
| M | **Runs no longer offers "Stop run" for dead runs.** `looksLive` defaulted to true whenever a run had no status counts â€” which is exactly the leftover rows â€” so a run that finished long ago was treated as live. Now Stop appears only on positive evidence. | Live: 4/4 real sends unaffected; the button tracks liveness. |
| L | **"idle" â†’ "ready"** in the Research status pill (server vocabulary, not a user word). | Build + live check. |
| Critical | **Research DAG no longer fails every researcher.** The run query was the console's multi-role brief, which every node reads; each researcher wrote the final report instead of its JSON contract ("returned no usable JSON", ~300s each). The topic is now the query and depth is a planner-only note. | Live `s8-95a538b1`, `s8-1fb46950`: planner/researcher/formatter all complete, 4,594-5,142-char reports, zero JSON failures. `BUGS.md` BUG-004. |
| H | **A planner's `"inputs": ["0"]` no longer orphans the formatter.** It fell through to the parent (the planner), so a successful researcher was discarded and the answer was a 272-char apology. Sibling positions now resolve; `planner.md` spells out the input forms. | `tests/test_plan_input_indices.py`; live report above. `BUGS.md` BUG-005. |
| H | **An MCP teardown `ExceptionGroup` no longer fails a completed tool loop** (nodes failed at `elapsed_s 0.0` with their answer in hand). Result in hand wins; with no result the real cause is unwrapped and tripped. | `tests/test_mcp_teardown_guard.py`; live runs show no ExceptionGroup. `BUGS.md` BUG-006. |
| H | **A new research topic is stored the moment it starts.** The run declares its kind, the server writes `run_meta.json` + the query before the warmup joins, and the Research sidebar lists it at once â€” so navigating away and back no longer loses it. DAG-mode Chat runs are declared `chat` and stay out of Research. | Live: submit â†’ `/api/sessions` listed it immediately (`kind=research`); navigate to Runs and back â†’ the topic is still at the top. |
| M | **Research no longer shows "working" after the answer arrived.** Opening a topic or "New topic" left `running` true (Stop enabled, composer disabled, "Working on the last questionâ€¦"). Every path now clears it, a detached stream cannot write into the topic you switched to, and a `finally` safety net clears it. | Live: reopened a finished topic â†’ pill `ready`, Stop disabled, composer enabled with `Send`. |
| M | **Research has Chat's layout.** One full-height transcript with the composer pinned at the bottom (depth + Stop/Send + files above it), instead of a topic box on top, a follow-up box at the bottom and a canvas between them. | Screenshots `res-02`/`res-04`; one `textarea`, no `#research-followup`. |

**Agent suite: 1564 passed, 39 skipped, 0 failed** (+17 new tests this round).
Frontend: 51/51 unit tests, `tsc --noEmit` clean, build clean.

### Withdrawn after re-testing (were harness artifacts, not bugs)
- ~~"The Send button is dead"~~ â€” clicking while a turn is streaming *queues*;
  my test measured "no POST in 800 ms" and called that a dead button.
- ~~"Double-tap loses the message"~~ â€” a double-tap correctly queues 2
  ("2 queued") and both drain.
- ~~"Ledger's n/a contradicts its tables"~~ â€” the tile says pricing is
  unavailable and the USD cells print "â€”"; the token columns are real. Honest.
- ~~"Research's Stop/Log/Download/Copy look enabled"~~ â€” they are disabled
  (`disabled={...}`), just styled faintly. Now an L-level polish item.

---

1. **H â€” an interrupted chat turn is silently lost**, message + answer, with no
   "stopped" note and no trace in the store. (Chat)
2. **M â€” Memory**: the create form has no `observation` kind, yet observations
   are a filter, a whole section and 20 stored items.
3. **M â€” Documents**: a document can sit on "parsing Â· 0 chunks" forever with
   no failed/timeout/retry state.
4. **M â€” Research**: the follow-up composer vanishes after a reload, so a
   finished run can never be continued.
5. **M â€” Research** shows the idle empty state *underneath* a real transcript.
6. **M â€” Code has no "New file" action**, so a new user cannot author anything.
7. **M â€” stale controls when idle**: Runs "Stop run", Research "Stop" +
   "idle" pill, Research Log/Download/Copy all look active with nothing running.
8. **M â€” Ledger says "cost data unavailable" while the same page shows real
   cost tables.**
9. **M â€” topbar crumb drifts from the thread title** after a follow-up.
10. **M â€” leftover test data has no cleanup UI**: 8 zombie Runs, duplicate
    observations, a document stuck parsing.
11. **L â€” assorted polish**: Memory form wedged mid-list; "+324" unexplained;
    raw model id in Code's status bar; Console feed dominated by access-log
    noise; inconsistent left-pane usage; a "Free OpenRouter models" app lingers
    after OpenRouter was removed as a provider; Scheduler's deleted job lingers
    until restart.

*(Withdrawn after re-testing: "the Send button is dead" and "double-tap loses a
message". Both are harness artifacts â€” see the retraction note in Chat.)*

---

## Test state at start of session

- Agent `:8500` ready, gateway `:8109` up, `gateway_up: true`.
- Earlier "wipe everything" effort was believed complete. Verified counts found:
  **8 zombie sessions** (`t-orch-1`, `t-rec-1`, `L0-er01/02/04`, `L0-eb04`,
  `L0-ea05/06` â€” all `run_kind=research`, empty status AND empty query),
  **65 memory items** (20 observation / 21 tool_outcome / 24 fact, incl.
  triplicated "prefers publication dates on every claim"), and one schedule row.
- Authoring: RECENT DOCUMENTS already empty in the UI, but **5 orphaned
  `author:pdf` artifacts (identical "Registration Probe" PDFs)** sat in
  `state/artifacts` with no session pointing at them â€” unreachable by any UI.
  Deleted on request at session start.

## Orphaned artifacts â€” no UI path to delete them

**H (data hygiene) Â· state/artifacts**
Authoring documents (`.bin` + `.json`, `source: author:pdf`) can outlive their
run: deleting the authoring run (or wiping sessions) leaves the artifact files
behind with no UI that lists or deletes them. The section under test is
whatever UI should have handled cleanup; a user cannot find or remove these.
â†’ Fix orientation: artifact lifetime must be tied to its producing run, and
Authoring/Runs needs a "documents" list that can delete artifacts.

---

## Cross-section status-language findings (recorded while probing state)

**M Â· /runs and /authoring**
`/api/sessions` returns 8 zombie sessions with empty `status` and empty `query`.
The Runs section showed a **"Stop run" button while nothing is running** (stale
running-flag), and both sections frame clearly dead test rows as usable rows.
â†’ Consistent rows should carry an explicit status badge or be hidden.

**M Â· /scheduler**
`DELETE /api/schedule/{sid}` answered `{"status":"ok"}`, yet the job **still appears
in the list** (`_cancelled: true` still returned by the list endpoint), with
**Enable job / Delete job** buttons still active on it. A user who deletes a job
sees it stay, and can delete it again and again.
â†’ Two inconsistent rules: delete says "removed", the list says "here it is".

**M Â· /memory**
Earlier "wipe" completed but 65 items remain (incl. triplicated observations).
Also: **Wipe shows no confirm dialog text** before acting (native `confirm()` is
silently auto-dismissed by the test browser, but a real user would at least see
a dialog â€” needs check with dialogs enabled).
â†’ Record when confirmed with native dialogs handled.

**M Â· /apps**
A pre-existing app named **"Free OpenRouter models"** (`openrouter_free`) is
listed. OpenRouter was removed as a gateway *provider*, yet an OpenRouter-named
app with `kind=openrouter_free` remains user-visible. Confusing naming.

**M Â· API status codes (affects console error UX; noted now, cyber later)**
Endpoints disagree on how they report "not found / rejected":
- `/api/code/tree` outside-workspace â†’ **HTTP 200** carrying `{"error":â€¦}` (but
  `/api/code/file` correctly 403s).
- `/api/schedule/nope/enabled` â†’ **HTTP 200** `{"ok":false,â€¦}` (documents route
  404s in the same situation).
- `/api/templates/nope` â†’ **HTTP 200** `{"status":"not_found"}`.
The console is the consumer of these; mixed envelopes cause inconsistent
user-facing failure modes section to section.

---

---

## Section-by-section findings

### Chat (`/console`)

**[H] The Send BUTTON is dead â€” clicking it never sends; only pressing Enter does.**
Verified rigorously with an in-page `fetch` hook (counts POSTs) and real keyboard
typing (not synthetic fill):
- type message â†’ **click** Send â†’ 0 POSTs (composer keeps text, no bubble, no error)
- type â†’ click, even after a 2.5s settle, and with rapid/double clicks â†’ 0 POSTs
- type â†’ press **Enter** (same composer, same moment) â†’ 1 POST, message sends
`document.elementFromPoint` confirms the click lands on the real button
(`pointer-events:auto`, `disabled:false`, `topIsButton:true`), so it is not an
overlay â€” the button's `onClick` is effectively a no-op while the textarea's
`Enter` handler works. The Send button is the primary action of the whole
console; a mouse-only user cannot send a message. Suspect: the button is
re-rendered on each keystroke (its aria-label swaps Sendâ†”Queue), detaching the
click target. Fix: stabilize the button/handler, or put the form onSubmit path
so both click and Enter go through one stable submit.

**[H] A turn that is interrupted loses the user's message completely.**
Probe: send â†’ press "Start new chat" (or close the tab) while the answer is
still streaming. The transcript resets silently ("Stopped" is not said), and
the store keeps **nothing** (verified `/api/chat/threads/{id}` â†’ 0 messages). The
user paid for tokens and typed a question, and it evaporates with no trace.

**[RETRACTED â€” was reported as H "Send button is dead". It is not a bug.]**
Earlier I claimed clicking Send never sends while Enter does. That was my
harness measuring "no POST within 800 ms" *while the previous turn was still
streaming* â€” a click during a turn correctly queues instead of posting.
Re-tested properly:
- click Send while idle â†’ sends (4/4 trials, plus the in-page `fetch` counter);
- click Send while streaming â†’ the button relabels to "Queue", the click queues,
  the **"1 queued"** badge appears, and the queue drains afterwards;
- double-tap while streaming â†’ **"2 queued"**, both drain, transcript holds all
  three messages, 3 POSTs.
Both the button and the Enter path work. Two of my earlier "findings" were
harness artifacts, now removed so nobody "fixes" a non-bug.

**[M] Topbar crumb drifts from the thread title.** After a follow-up, the crumb
shows the last turn ("â€¦SECOND-queue-turn") while the sidebar title stays the
first ("â€¦FIRST-queue-turn"). Confusing when sharing/scanning the list.

**[M] "20 tools Â· no pipeline"** badge is shown constantly even in plain
lightweight chat where neither tool-count nor "pipeline" is meaningful to the
user. Cryptic / noisy.

**[L] Starter chips send immediately** rather than prefilling the composer;
clicking one with a half-typed message discards the draft (worth a confirm).

### Runs (`/runs`)

**[M] Eight indistinguishable zombie rows.** All rows are "(untitled)",
"no graph", "$0.0000", "14h ago" with a "no graph" pill â€” leftovers from
aborted tests. A user sees 8 useless rows and a "Select a run" pane; there's no
affordance to tell these are empty or to delete/clean them.

**[M] "Stop run" button is shown when nothing is running** (stale
running-flag) â€” a dead control.

### Memory (`/memory`)

**[M] The REMEMBER form is rendered in the middle of the results list**, glued
between the observations above and the tool-outcomes below. It reads like a
broken card and breaks the scan of the list; a user looking for their memories
sees a form first.

**[M] Three identical "prefers publication dates on every claim" observations**
stacked (duplicates not merged/superseded visually). "hide superseded" is on
yet duplicates remain.

**[L] Sidebar filter cards carry no counts** ("the gateway does not report how
manyâ€¦"). Honest copy, but six count-less filters make scanning hard.

### Documents (`/documents`)

**[L]** Clean, honest section: upload dropzone with limits + per-doc
enable/re-index/preview/download/delete. Only a real uploaded PDF
("Hybrid_AI_Manufacturing", status "parsing") is present; nothing to fix. (A
zero-byte upload attempt logs a benign 400 in console â€” acceptable.)

### Research (`/research`)

**[M] Stale "Stop" button + jargon "idle" pill** show even with no run
started (same stale-affordance pattern as Runs). Log / Download .md / Copy are
enabled-looking but do nothing with no run selected. Depth quick/standard/deep
toggle correctly.

### Scheduler (`/scheduler`)

**[H, previously seen, now cleared on restart]** A deleted job stayed listed
with Enable/Delete buttons (delete returned `{"status":"ok"}`). After a service
restart the list is clean (Jobs 0), so the ghost was in-memory scheduler state,
not persisted â€” but the delete/list inconsistency is real and will recur.

**[M] Double empty state.** "No jobs â€” Schedule a reminderâ€¦" in the left pane
AND a "No jobs" table below the form. Redundant, and the whole left sidebar is
empty while everything sits on the right â€” inconsistent with Runs/Memory which
use the left pane as the primary list.

### Skills (`/skills`)

**[L]** 39 "Withhold" buttons on the page â€” one per pack. Filter works. The
volume of repeated identical actions is heavy; a bulk/pattern affordance would
help. No functional break found.

### Code (`/code`)

**(strong; no functional break)** Real VS Code-style IDE: explorer, Monaco,
Problems + "Run checks", terminal, agent panel, model picker, honest empty
"Pick a file to start editing". Two polish items:
- **[L]** Status bar shows raw model id `cohere/north-mini-code:free` while the
  picker shows the friendly "FREE north mini code Â· cohereâ€¦" â€” inconsistent.
- **[L]** "+324" next to the picker is unexplained.

### Apps (`/apps`)

**(strong; no functional break)** Clear flag docs, override/default pills,
per-flag "revert", live systems row, spend-by-skill. Toggling a flag and
reverting works (verified restore). One data note below.

**[M] A "Free OpenRouter models" app (`kind=openrouter_free`) still exists**
even though OpenRouter was removed as a gateway *provider*. Name confuses users.

### Ledger (`/ledger`)

**[M] "SPEND n/a â€” cost data unavailable" contradicts the same page**, whose
SPEND-BY-SKILL and TURN-HISTORY tables render real tokens/calls. Either the
headline or the tables are wrong; a user can't trust the page.

**[L] "CALLS 5" and "TURNS 5" are two near-identical numbers** with no
clarifying copy; queued follow-ups appear as their own turn rows, which is
correct but unexplained.

### Console (`/mission`)

**[L]** Event feed + source filters + counts look solid and honest
("ERRORS 0 â€” need attention"). The feed is dominated by raw
`/kilo/models?all=true "HTTP/1.1` and `_client.py` access-log noise, which a
non-technical user reads as gibberish. Minor filter/format suggestion.

### Authoring (`/authoring`)

**[L]** Empty state ("Nothing yetâ€¦ stay downloadable after a reload") is clear;
right pane has good empty guidance. The left BRIEF card and the right help pane
are balanced enough. The "Try one of these" disclosure didn't auto-open in the
probe (needs a click) â€” fine. No functional break. (Artifacts cleaned at start.)

### Settings (`/settings`)

**[L]** Config viewer with filter works (9 matches for "provider"). YAML
highlighting present. No break found.

### Parts (`/parts`)

**[L]** Pass selector populated from gateway (4 passes, annotated "measure
only"), switching updates the panel. No break found.

---

# Pass 2 â€” actually USING each section as a user

The first pass only *loaded* most sections; they looked empty because all test
data had been deleted on request. This pass created real content in each
section through the UI and left it in place, so every section now has something
to look at.

**Content now present (created this pass, deliberately kept):**
- Memory: 3 new memories via the form (fact / preference / observation).
- Documents: `console-qa-notes.md` uploaded + enabled (ready, 2 chunks).
- Scheduler: 2 jobs ("remind me to stretch and drink water" every 2h;
  "summarise yesterday console errors" daily@09:00).
- Research: a real quick run, "Capital of Portugal" â€” 9 nodes, answered in 80s.
- Authoring: a real build â€” 14 nodes, 1 document in RECENT DOCUMENTS.
- Code: 2 playgrounds created, 1 agent write-approval prompt exercised.

**[M] Memory: you can filter for a memory type you cannot create.**
The REMEMBER form's kind select offers only `fact | preference | tool_outcome
| scratchpad`, but the left sidebar has an **observations** filter and the page
advertises "WHAT WE'VE NOTICED â€” 20 observations". There is no way to add an
observation from the UI. A user is shown a whole section of a type they cannot
contribute to.

**[M] Documents: a document can sit on "parsing Â· 0 chunks" forever.**
`Hybrid_AI_Manufacturing (1).pdf` (7.9 MB) has shown **parsing Â· 0 chunks**
across many probes while the header still reports BLOCKED 0 / READY 1. There is
no failed/timeout/retry state â€” it looks like it is working indefinitely.

**[M] Research: after a reload you cannot follow up on a past run.**
During the live run the composer reads "Ask a follow-up â€” the run keeps its
contextâ€¦" with an **Ask** button. After reloading and re-selecting the same
topic, that composer is **gone** (only the topic box remains), so a run that
finished in an earlier session can never be continued. A user who comes back
tomorrow cannot ask a second question of yesterday's research.

**[M] Research: contradictory empty state.**
After selecting the topic, the real transcript (the answer + the incomplete
notice) is displayed, and *underneath* it the idle empty state still renders:
"Ask a topic to begin â€” Aria breaks the question into partsâ€¦". Both at once.

**[M] Code: there is no way to create a file.**
The explorer offers **Open folder** and **New empty playground** only â€” there is
no "New file". A brand-new playground contains nothing, and nothing in the
console creates a file from the UI, so a user who has not already got a folder
to open cannot author anything in Code. (The agent can write, approval-gated.)

**Things that worked well (verified, not assumed):**
- **Code write-approval is excellent**: modal titled "The agent wants to change
  a file", the exact path, a real unified diff, a `create_file` tool badge, and
  honest copy â€” "Declining tells the agent you refused, and it continues" â€”
  with Decline / Accept.
- **Authoring works end to end**: brief â†’ build â†’ a real document in RECENT
  DOCUMENTS with Preview / Result / Run log.
- **Research is honest about failure**: instead of a confident wrong answer it
  shows "**This answer is incomplete.** researcher: returned no usable JSON â€¦
  ExceptionGroup: unhandled errors in a TaskGroup. Treat the missing parts as
  unknown rather than settled â€” the run log has the full detail."
- **Runs** renders the real DAG (PLANNER node, 9 nodes) with failed nodes
  outlined in red, and clicking a node opens the inspector.
- Scheduler create, Memory remember, Documents upload+enable, Ledger spend rows
  and the Knowledge/Console event feed all work through the real UI.

**Correction to pass 1 (harness, not app):** several controls were reported as
"missing/untested" in pass 1 only because my selectors were wrong â€” the
Scheduler button's accessible name is "Schedule job" (not "Schedule"), the
memory fields are labelled by aria ("Memory kind", "Memory descriptor"), and the
approval buttons read "Accept"/"Decline". All of them work. The genuine
unfixed bugs from pass 1 remain the Chat Send-button defect, the lost
interrupted turn, and the stale idle controls.

1. **Stale controls shown when idle** in three sections: Runs "Stop run",
   Research "Stop" + "idle" pill, Research Log/Download/Copy enabled-looking.
   Same class: an action affordance rendered from a stale running-flag.
2. **Inconsistent use of the left pane**: Runs/Memory use it as the primary
   list; Scheduler/Documents/Research/Ledger push all content to the right and
   leave the left empty or duplicating. The shell should feel consistent.
3. **Jargon in user chrome**: "no pipeline", "idle", "n/a", "Withhold" Ã—39,
   "+324", "thinkingmachines" (model vendor name) leaking into body text.
4. **Data hygiene**: 8 zombie sessions + 65 memory items + a leftover uploaded
   PDF all persist from earlier tests; the UI offers no "clean up test data"
   affordance, so a real user inherits clutter.
5. **Empty/abandoned turns silently vanish** (Chat) â€” the single most
   user-damaging behavior found; worth fixing before anything else.

## Probe-method corrections (so the numbers above are trustworthy)
- Two earlier "Send does nothing / duplicate send" readings came from broken
  network-filter regexes in my own harness, not the app; the 6/6 fresh-chat
  dead-Send above was re-confirmed with an in-page fetch hook. The interrupted-
  turn-lost and double-click findings were each re-verified against the server
  store (`/api/chat/threads`) rather than the screen.

*(Cybersecurity/hardening pass deferred per user instruction â€” see the API
probe notes at the top; those items are real but out of scope for now.)*

---

## Round 2 â€” reported from the user's own session (fixed here)

**H â€” the first message of a conversation could vanish.** Sending "hi" showed the
answer but not the question, and a reload lost the turn entirely. Cause: the
turn (question + answer) was written to the thread in one block *after* the
answer streamed, so an early disconnect â€” or any turn whose save did not land â€”
left the user's own words nowhere. Fixed earlier in this round by persisting the
question before the answer streams.
*Verified:* sent "hi 41061", reloaded, the message is still on screen.

**H â€” "(empty answer)".** When the tool loop stopped on a hop cap or a stall with
only tool calls pending, `text` was empty and the console printed the literal
phrase "(empty answer)" â€” a dead string with no cause, for a turn the user had
paid for. Now `_chat_empty_answer()` says what stopped it (the cap, the stall)
and what to do next ("try one thing at a time, or run it from Research").

**H â€” the agent claimed it could not call tools in parallel.** It answered "I
can only call one tool at a time â€” the functions run sequentially", which is
false: the runner has always dispatched a batch with `asyncio.gather` behind a
semaphore. It believed it because the prompt never said otherwise and silence
reads as "no".
*Fixed and verified:* asked it again â€” "Yes, I can call multiple tools in
parallel. When a task requires several independent lookups, I can invoke them
together in a single turn so they run simultaneously."

**M â€” Research: picking a topic while a run was in flight did nothing.** The
pane kept showing the *previous* report under the newly-selected topic's name â€”
which is exactly "it shows capital to portugal even when I selected the
greeting one". Switching topics is now allowed; the run in progress keeps
running server-side and keeps its place in the list (switching no longer kills
a paid run either â€” it used to be blocked for that reason).
*Verified:* Lisbon â†’ greeting â†’ reminders each render their own content.

**M â€” Research had no way to start a new one.** Added a "+ New topic" button
that clears the pane to an empty brief. It also clears the composer â€” opening a
topic seeds the composer with its title, so without that the "new" topic came
pre-filled with the previous question, ready to be run by accident.

**M â€” the research report leaked the pipeline's internals.** Asked "what can you
do?", the final answer narrated the plan and its nodes under a
"===== FINAL: =====" banner. The brief now says, explicitly, that the report is
written for the person who asked: never mention nodes, skills, the plan or the
system instructions, and never wrap the answer in a banner.

**H â€” chat paraphrased the delegated research report.** "Research about EV cars
and tell me" showed the full report under Research, but the same delegation from
chat answered "Based on my researchâ€¦" â€” a summary of the report the user had
just asked to see. The report now *is* the chat answer, verbatim, shown as a
preview card that opens in a dialog over a blurred backdrop (the Ctrl+K
surface); no summarising, no word-by-word repainting. Every later turn and every
reload gets the same card, and the model only receives the report's name and
opening lines (BUG-014).
*Verified:* `ct-live-report` â€” `done kind='report'`, 26,550 chars, zero delta
frames, ~136 s.

**M â€” "send it to my telegram" was scheduled a minute in the future.** Chat had
no immediate-send tool â€” only `schedule_task` â€” so "send" became "scheduled for
in 1 minute". A new `send_channel_message` tool sends NOW through the same
paired destination, chunking long reports instead of truncating them; the
prompt reserves `schedule_task` for actual future times (BUG-013).
*Verified:* `ct-live-report` turn 2 â€” `using send_channel_message`, no schedule,
"8 messages, 26,550 chars".

**M â€” lightweight chat searched the document store on every turn.** Even
"hello" embedded, searched and injected a document block before the model saw
the question. The up-front search and injection are gone from both chat
endpoints; the `search_knowledge` tool is the access, and it is still scoped to
the chat's enabled documents (BUG-015).
*Verified:* endpoint test asserting the retrieval is never called; live turns
carry no injected block.

## Round 3 â€” the user's follow-up session (2026-10-19)

**H â€” a report could not be summarised: "it is already above", four times.**
After a delegated report was shown, "what is this report about?" â†’ "can you
summarise it" â†’ "yes, i understand that the report is full, but can you
summarise it?" all got stonewall variants ("The full research report is
already available above"). The model only had a 240-char stub by design
(BUG-014), so summarising was impossible. New `read_report` tool returns the
body on demand; the stub now points at it (BUG-018).
*Verified:* live on the recorded thread â€” 1,690-char executive summary, 17.4s.

**H â€” Telegram attachments never reached the agent.** Files sent to the bot
produced nothing: no Documents entry, no reaction in chat. The gateway
ledger had two `agent-forward` errors (`'Attachment' object has no attribute
'get'`) at the exact second the files were sent, and the poller's download
thread died the same way. Both treated pydantic models as dicts; fixed in
`channels_api.py` + `telegram_poller.py` (BUG-016).
*Verified:* unit tests with real `Attachment` models; live needs one file
re-sent on Telegram (cannot be driven from here).

**H â€” Telegram-initiated research delivered a truncated report as one
message.** "research about evs and send me the report" from Telegram sent the
first 3500 chars, nothing else. `channel_chat._run_turn` cut at
`_MAX_REPLY` instead of chunking; the console path always chunked at 3900.
Now chunked, media on the first chunk, partial-failure message says how much
arrived (BUG-017).
*Verified:* `tests/test_channel_chat.py` (four new tests).

**M â€” "hi" costs ~4.5-5.7k input tokens, on every model.** The user suspected
related chunks at the start; the ledger and a measurement of the payload say
otherwise: memory hits for "hi" = 0 chars. The fixed cost is 24 tool schemas
(14,509 chars, ~3.6k tokens) + system prompt (~1.5k) + profile block (~0.4k).
`schedule_task` alone is 3,252 chars. Kilo/Cohere in the ledger rows was just
the router's pick. Recorded as BUG-020 (open â€” a fix is a product trade-off,
not yet made).

**M â€” "send the PDFs to my telegram" answered with words, not the file.**
`send_channel_message` had no `file` argument: a rendered PDF could only be
described, never attached. The tool now resolves a rendered file from the
thread, reads the artifact bytes and posts them as Telegram media; caption
truncates to 1000 chars; unknown names get an explicit refusal (part of the
BUG-016/013 channel round).
*Verified:* `tests/test_chat_report_full.py` file-send section incl.
`file` beats a stale `source`.

## Round 4 â€” the Code section, driven as a user (2026-10-19)

**H â€” "the agent adds/modifies files but I have to refresh the webpage to see
it."** Two separate faults, both found and fixed (BUG-023, BUG-024): the
explorer refreshed only on manual toggles, and accepting a write reloaded the
pane by the agent-relative path (`bye.py`) where the route wants
`code_workspace/bye.py` â€” a 403 that was caught and dropped, so nothing moved
and nothing said so. Accepted writes now flow through: explorer re-reads the
tree the moment a write tool succeeds, the open pane reloads from disk (or
raises the stale banner if the draft is dirty), and the server sends the
client-visible path on the permission frame.
*Verified:* Playwright runs with 0 `/api/code/file` failures; explorer picks up
new files with no manual action; pane sync proven twice; screenshots
`9-marks-and-synced-pane.png`, `12-final-sync-and-marks.png`.

**H â€” "reminder on 9am today" fired as "+1h".** The scheduler could not parse
named clock times, and its generic error suggested a relative delay â€” so the
model retried "9am" as "in 1h". Clock times (`9am`, `14:45`, `9:30`, `2:30pm`,
`today 9am`, `9am today`, `tomorrow 14:30`) now parse; a past named time
refuses with a message that says explicitly it must not become a relative
delay (BUG-022).
*Verified:* live API â€” `6am` refused with that text; `9am` â†’ `next_fire`
exactly 09:00 local; `tomorrow 9am` accepted; `TestClockTimes` passes.

**M â€” Cursor-parity, taken as a request:** after the agent's write lands, the
file now shows green bars + wash on added lines and a red bar at the deletion
point (Monaco decorations, repainted against the new body), a bar above the
editor recounts `+N/âˆ’R` with a dismiss, and saving folds the change in and
clears the marks. Markdown files got a Preview/Edit toggle that renders the
*buffer* (so a draft previews before saving). The marks ordering defect found
while proving this (decorations computed against the pre-write model because
the marks effect ran before the body-push effect, then never recomputed) is
fixed by declaration order.
*Verified:* `+2`/`+1` counts match DOM decoration counts; a replace shows 1
green + 1 red; preview renders heading/bullets/table (screenshot
`10-md-preview.png`).

**L â€” the model picker read a bare "+325".** Now "+325 more" (BUG-025).

**L â€” dead test marker + a false empty-state claim.** The `/code` e2e spec
pinned "read-only source" (exists nowhere) and the panel said the agent
"searches the repo" while the shipped workspace is a blank scratch dir; both
corrected (BUG-026).

*Probe correction (harness, not app):* Monaco renders spaces in `view-lines`
as non-breaking spaces, so a Playwright `textContent.includes("bye from Aria
v3")` never matches. Assert on a single token (`v4`) or normalize `\u00a0`.

## Round 5 - Cursor-style auto-apply + Keep/Revert (2026-10-19)

Asked for by the user: no per-write approval dialog, edits apply during the
run, edited lines stay marked, and the user checks in (Keep) or rolls back
(Revert) after the agent finishes, as in Cursor.

- **H fixed - Revert returned 500 and lost the snapshot (BUG-027).** Found by
  the proof, not by reading: the handler used `_aio` without importing it, and
  popped the snapshot before crashing. Now retryable; proven by API probe and
  by the browser flow.
- **M fixed - `run_checks("some_file.py")` refused a file argument (BUG-028).**
  This is the exact call in the pasted transcript that made the agent retry
  until the loop guard stopped it.
- **L fixed - the change bar counted `-1` for a replacement the editor never
  paints red (BUG-029).** Counts are now `+N added / ~N edited / -N deleted`
  and match the marks on screen.
- **Verified live (Playwright, screenshots 13-17):** asking for an edit shows
  **zero** approval dialogs; the review bar appears with Keep/Revert; a
  one-line replacement paints one yellow line and one amber gutter bar; Keep
  clears the marks and leaves the bytes; a second edit + Revert restores the
  previous bytes; a page reload re-fetches the pending change and re-paints
  the bar and marks; Revert from the reloaded page restores the file.
- **Also verified:** explorer rows carry a yellow dot for unreviewed files;
  the status bar and AgentPanel footer now say edits auto-apply and are
  reviewed after, instead of "need approval".

## Round 6 - Code-section UI polish (2026-10-19)

The user asked why the Code section looks "messy and unpolished" next to the
others. Driven as a careless user at 1500/1280/1024, surveyed against Chat and
Documents, fixed iteratively, re-shot after each pass.

- **M fixed - two permanently-open bottom docks (BUG-030).** Problems showed
  three lines of apology and a Run-checks button at all times, under a second
  Terminal header: 142px of editor at 1500, 307px at 1024. Now one 28px strip
  (`Problems N | Terminal | Run checks`), bodies open as tabs, panel collapsed
  by default; the empty Problems body is two compact lines. The Terminal
  session stays mounted across collapses, and its palette is graphite + editor
  blue now instead of the dashboard's navy/violet.
- **M fixed - laptop layout clipped controls (BUG-030).** Agent pane was wider
  than the editor at 1024; Send fell 11px off-screen; breadcrumb label clipped.
  Fixed agent widths, `min-w-0` through the composer, agent defaults closed
  below 1280 (top-bar toggle, docked not unmounted), minimap off below 1280,
  status bar single-line. Re-probed: docW == winW at all three widths.
- **L fixed - the explorer showed a phantom "state" level.** The workspace
  name (`state`, the base dir) rendered at tree-row size directly above
  `code_workspace`, reading as an extra empty folder. Removed; reset moved
  into the EXPLORER header.
- **L fixed - IDE gestures lived in the AGENT panel header.** Definition /
  References / Save sat in the agent's header, implying the AI owned them.
  Moved to the editor breadcrumb row; the agent panel's duplicated "AGENT"
  title bar was removed entirely (its own header carries the title).
- **L fixed - status bar noise and wrap.** Raw model id + duplicate
  auto-apply sentence + "your draft stays in this browser until you save" made
  it wrap to 42px at 1024; the agent footer repeated the auto-apply line. Now
  one line: friendly model name in the footer, low-priority items hidden below
  xl/2xl, duplicate footer line removed.
- **L fixed - agent empty state looked like a form.** Four full-width bordered
  suggestion rows are now compact chips, and the seeds change when no file is
  open ("this module" referred to nothing).
- **L fixed - model picker trigger.** Truncated mid-token
  (`FREE north mini code Â· cohere Â· 25â€¦`); context window moved to the option
  title, the trigger shows `cost Â· name Â· vendor`, and "+N more" keeps its
  label (BUG-025 stays fixed). The redundant "Agent" pill hides below xl.
- **Verified:** `tsc` clean, `vitest run src` 9/9 files, build clean; proof
  screenshots at all three widths plus panel-collapsed/open states; no
  horizontal overflow with the agent open at 1024.

## Round 6 addendum - visual pass: colour, chrome, spacing (2026-10-19)

The user came back on the same round: "plain chrome, color, spacing etc, dont you
feel anything off?" â€” so this pass hunted palette and chrome inconsistencies
with pixel probes of the rendered screenshots, not just DOM state.

- **M fixed - the editor surface was a different app (BUG-031).** Monaco still
  used the dashboard theme: navy `#0a0c13` editor vs graphite `#181818` chrome,
  violet cursor, pastel tokens; the minimap inherited the navy and read as a
  black column. Rewrote `ARIA_THEME` to VS Code Dark Modern (editor `#1f1f1f`
  above the chrome, cursor `#aeafad`, selection `#264f78`, green italic comments /
  blue keywords / orange strings, `minimap.background #1f1f1f`). Pixel probe of
  the PNG: editor column `#1f1f1f`, minimap `#1f1f1f`, no black rectangle.
- **L fixed - violet utilities still leaked inside `.ide`.** The repoint block
  missed classes actually in use (suggestion-chip hover border, composer
  `focus-within` ring, user-bubble fills). Extended for every used variant;
  live hover probe shows chip border `rgb(55,148,255)` (`#3794ff`), SVG accents
  `#9cdcfe`.
- **L fixed - status bar items were interleaved.** Icons sat between text items.
  Reordered VS Code-like (left: shield notice + errors; right: Ln/Col, selection,
  folded, indent, encoding, chars, language, then icon toggles). Shell check:
  single line, `#181818`, border `#2b2b2b`, no wrap at 1500/1280.
- **L fixed - the explorer footer said just "1 open".** A 28px bordered strip
  under the file tree rendered `{panes.length} open` â€” unlabeled ("1 open" of
  what?) and duplicating the tab strip. Removed; the tree gets the height back.
- **M fixed - the no-file state sat on the app void (BUG-032).** The user came
  back with "this still looks the same?" on the empty Code view: with nothing
  selected the editor column was a `#090909` slab (app background through a
  transparent column), flanked by `#181818` chrome. Every earlier proof
  screenshot had a file open, which is why three passes missed the default
  first impression. The empty-state wrapper now paints `bg-surface-1`; a
  no-file probe shows the whole column `#1f1f1f` and the opened Problems body
  on `#181818`. The no-file state joins the standing screenshot set.
- **L fixed - faint text ghosted through the minimap column (BUG-033).** With a
  long line running under the minimap, its tail showed as gray ghost text below
  the thumbnails. Monaco fades its minimap canvas to 90% and expects the
  container to carry the editor surface; ours was transparent. The `.minimap`
  container now paints `#1f1f1f`. Live pixel check of the repro region:
  uniformly `(31,31,31)`, zero ghost pixels.
- **False alarm, noted:** docstrings looked italic in a downscaled crop; a 2x
  pixel crop shows them upright orange monospace. No change made.
- **Verified:** `tsc` clean, `vitest run src` 9/9 files (57 tests), build clean;
  post-fix screenshots at 1500/1280/1024 with the agent open; DOM probe finds no
  stray text; violetâ†’blue and minimap backgrounds confirmed by computed style and
  PNG pixel sampling.
- **M fixed - the code agent had no way to create a folder (BUG-035).** Asked
  for a directory it said it had no such tool, and `create_file` refused
  missing parents. New gated `make_dir` tool; `create_file` now auto-creates
  parents; revert cleans up a folder it created if still empty. Live E2E
  (create + revert) passed.
- **M fixed - premium pass on the Code view (design review aftermath).** The
  mockup critique distilled to: one focal surface, chrome quiet, accent
  rationed, no redundant labels. Applied to the real UI: the agent panel now
  paints chrome `#181818` (was `#1f1f1f`, competing with the editor) with a
  `#1f1f1f` composer well; the rail's active item is a neutral
  `white/6%` fill with the violet glow removed from its indicator; the TopBar
  no longer repeats "Aria /" on desktop; explorer tree icons went monochrome
  (were blue `accent/80`, sky), file selection is neutral; per-tab `border-r`
  separators are gone (active tab keeps a 2px accent top-border); the
  Problems/Terminal strip dropped fake-small-caps for 11.5px medium; agent
  panel accents (header sparkle, suggestion-chip hover, user bubble, attached
  chips, picker/queue pills, step icons, git toggle) all went neutral â€”
  send button keeps the one accent, and a CSS override now keeps it blue on
  hover (the blanket `.ide button:hover` grey used to win). Verified: new
  bundle `index-C_BS6vAQ.js`, 0 console errors, probes `panel rgb(24,24,24)`,
  `rail fill white/6%`, `editor rgb(31,31,31)`, send `rgb(55,148,255)` and
  `opacity .4â†’1` on draft; before/after screenshots in the standing set
  (`pp-before-*`, `pp-after-*`).
- **M fixed - the IDE now matches the app's total-dark shell (requested).**
  The `.ide` token scope still carried VS Code grays (`#181818` chrome,
  `#1f1f1f` editor, `#2b2b2b` borders) â€” a lighter app living inside the
  near-black shell (`#090909`/`#0b0b0e` base, `#0e0e12` cards). Repointed to
  `#0b0b0e` chrome / `#101014` editor (kept: graphite, editor-blue accent,
  editor `ABOVE` chrome as the one lifted pane) and updated the Monaco theme
  + xterm palette to match. Root cause of two surfaces resisting the tokens:
  a literal `#181818` pane rule (`[role=tablist]`, `.ide-status`, asides) and
  literal input/hover/indent-guide colors â€” all now tokenized. Verified:
  bundle `index-BSUQb2MN.js`, 0 console errors, PNG pixel probes rail/explorer/
  agent/status/tab-strip `(11,11,14)`, editor `(16,16,20)`, composer well
  `(23,23,28)`; 57/57 vitest.

## Still open (not fixed)

- **M â€” Documents**: the 7.9 MB PDF still sits on **"parsing Â· 0 chunks"** with
  no failed/timeout/retry state. This needs the embedder's failure path looked
  at (a backend behaviour change, not a UI fix), so I left it rather than
  paper over it in the view.
- **L â€” Memory**: the REMEMBER form is wedged mid-list, and duplicate
  observations appear three times even with "hide superseded" on.
- **L â€” Console**: the feed is dominated by `/kilo/models?all=true` and
  `_client.py` access-log noise.
- **L â€” layout**: left-pane usage is inconsistent across sections
  (Scheduler/Documents/Research/Ledger push everything to the right and repeat
  the empty state).
- **L â€” Code**: status bar raw model id â€” **fixed in Round 6** (footer shows
  `north mini code`; the picker trigger shows the friendly label).
- **L â€” Skills**: 39 identical "Withhold" buttons, one per pack.
- **L â€” disabled controls** still read as clickable at 40â€“45% opacity (Runs
  Stop run, Research Stop/Log/Download/Copy). They ARE disabled â€” this is a
  styling weakness, not a functional bug.
- **L â€” Scheduler**: a deleted job can linger in the list until restart
  (delete returns ok, the row stays flagged `_cancelled`).

## Round 7 â€” Code-agent harness hardening (2026-10-20)

Phase 1 of the harness audit, chosen by the user. Defects recorded as
BUG-036â€¦BUG-041 in `S9SharedCode/code/BUGS.md`; this is the session log.

### Fixed
- **Stop now stops** the detached MCP run; a stream without `done` no longer
  looks complete; `done.stopped` carries the reason. Live proof: disconnected
  mid-`run_checks` while a 120 s sleeping test ran â€” `pytest.exe` was gone 6 s
  later, `/api/health` stayed ready.
- **No phantom reviews**: failed, refused or no-op writes no longer appear in
  "Changes to review"; settled by comparing disk to the snapshot. Live proof:
  create â†’ one entry with diff â†’ revert deletes and empties the list; a failed
  edit on a missing file leaves the list empty.
- **Secrets closed**: `.env*` (except `.env.example`), private keys and
  key/cert suffixes are refused by read, symbol, preview and write tools, and
  skipped by repo-wide scans.
- **`run_checks` usable**: 900 s timeout, provider secrets scrubbed from the
  child environment, permission-gated like a write in manual mode.
- **Loop contract**: the 16-tool allowlist is enforced at dispatch, `max_hops`
  is exact (default 60, cap 200, explicit 0 = unlimited), old tool output is
  elided against a 600 k-char budget with a usable hard stop, the MCP breaker
  only trips on a failed handshake, and `done.tool_calls` reports the real
  count.
- **Console mark pairing**: parallel gated writes are matched FIFO by tool name;
  Keep only clears marks after the server confirms.

### Verification
- Targeted backend suite: **222 passed** (14 files, 33.7 s).
- `npx tsc --noEmit` clean; `npx vitest run src` 57/57; `npm run build` passed.
- Live agent run after restart: 127 SSE frames, one tool result, `done` with
  `tool_calls: 2` on the write run; gateway healthy throughout.
- Full dark-theme and BUG-033 pixel proofs remain valid (no CSS change here).

### Still open from the audit (deliberately not in Phase 1)
- The console always sends `auto_approve: true`, so the permission dialog is
  effectively dead in normal use even though the server now has an exec gate.
- Active file is cut at ~4 k chars silently; attachments read the saved file,
  not the unsaved buffer; search returns hits without context lines; no repo
  map or glob tool; `run_checks` output still never reaches the agent; history
  carries no tool results; `thinkingDropped` is not surfaced.

## Round 8 â€” Code palette, PowerShell, sandbox, desktop app (2026-10-20)

### Fixed / delivered
- **Code section no longer pitch black.** `.ide` stopped overriding the
  surface tokens, so the editor chrome inherits the app's global palette
  (`#0a0c13` base, `#10131c` editor, `#171c28` panels). Kept the editor-blue
  accent and semantic IDE colors. Monaco bg/gutter/sticky/minimap are
  `#10131c`, widgets `#171c28`, borders `#2b3448`; xterm bg is `#10131c`.
  Proof: live Monaco computed `rgb(16, 19, 28)`; tsc clean, 57/57 vitest,
  production build clean.
- **PowerShell access for the Coder (BUG-039's sibling).** New gated
  `run_command(command, cwd, timeout)` executes `powershell.exe`/`pwsh.exe`
  through `-EncodedCommand`, returns real exit codes and bounded output, and
  is in `CODE_AGENT_TOOLS`, `_CODE_EXEC_TOOLS`, the manual permission summary,
  the system prompt, and the 56-entry MCP catalogue. Live smoke: `ps-ok 5`,
  `sandbox: jobobject`, `shell: powershell.exe`, `tool_calls: 1`.
- **Sandbox strengthened.** New `sandbox.py` provides wall-clock timeout,
  whole-tree kill, Windows Job Object (kill-on-close, 24-process cap, 1 GB
  process / 2 GB job memory), POSIX process groups, env scrub, bounded
  draining output, workspace-confined `cwd`, and a JSONL audit at
  `state/code_exec_audit.jsonl`. `run_checks` runs through the same executor.
- **Command guard added.** `run_command` refuses machine destruction
  (`format`, diskpart, reboot, registry/account changes, process kills,
  `rm -rf`/recursive deletes, drive-root deletes, `git reset --hard`,
  `git clean -f`, `git checkout/restore -- .`, force-push), credential reads
  (`.env`, keys, token/password env vars, profile paths, UNC/registry access),
  `..` traversal, and drive-qualified paths outside the workspace. Refusals
  are audited and returned as a tool error; a launched command is audited with
  exit code and duration. This is a heuristic guard, not isolation.
  Live proof: the agent called `run_command("cd ..")` and got
  `{"ok": false, "refused": true, "error": "refused: traverse outside the
  workspace..."}` with a matching audit row.
- **Desktop app delivered.** Portable Electron build at
  `agent-desktop\dist\Aria\Aria.exe`, with tray menu, window chrome,
  deep links, packaged-root discovery via `aria-root.txt`, icon, and Desktop
  + Start Menu shortcuts. Build is repeatable with `build_desktop.ps1`.
  Verified through CDP: `window.ariaDesktop.shell == "electron"`,
  `/console` loaded with the full nav, Code title, Monaco `rgb(16,19,28)`;
  the final relaunch has no debug port, and the window title follows the
  active thread (observed "Aria Â· hi").

### Verification
- Full backend suite: **1683 passed, 39 skipped** (11m24s).
- Targeted new work: 76 passed (sandbox/MCP/loop/edit-gate/e2e), 31 passed
  (sandbox + skills + comprehensive tools).
- Frontend: `npx tsc --noEmit` clean, 57/57 vitest, production build clean.
- Live agent after restart: gateway healthy; allowed `run_command` returned
  `ps-ok 5` (`sandbox: jobobject`, `shell: powershell.exe`); a `cd ..`
  command was refused and audited; desktop app relaunched normally.

### Bugs found and fixed during this round
- **BUG-042** â€” replacing `sandbox.py` deleted `run_python`; sandbox_executor
  raised ImportError. Restored the old contract on the new executor (temp
  cwd, separate caps, `files_written`, timeout marker) and re-verified.
- **BUG-043** â€” the comprehensive tool-registration test still expected 54
  tools; the real count is 56 (`make_dir` + `run_command`). Updated.

### Boundaries still open
- The command guard is not a filesystem/network jail; a determined command can
  still reach the network or paths the heuristics miss. Hard isolation needs a
  container/AppContainer backend (Docker Desktop and WSL Ubuntu-24.04 are
  installed but stopped).
- The console still sends `auto_approve: true`, so the permission dialog is
  dead in normal use even though the exec gate exists. Closing this is the
  next security step.
- Desktop is portable only: no installer, auto-update, signing, or keychain.
- `run_checks` output still does not reach the agent; history carries no tool
  results; the remaining Round 7 audit items still stand.

## Round 9 - zoom, shell scroll, coding pack, markdown copy, document preview, authoring (2026-10-20)

### Fixed / delivered
- **Console shell no longer grows the document.** `html`, `body` and `#root` are
  `overflow:hidden`; the chrome is a flex column and only the active section
  scrolls. Browser proof at 1440x900: home, Skills, Documents and Authoring all
  reported `documentElement.scrollHeight == body.scrollHeight == innerHeight == 900`.
- **Ctrl/Cmd + `+` / `-` / `0` zoom works.** `main.cjs` handles
  `before-input-event` (levels -3..5) and the hidden menu exposes the same
  actions. Verified in the packaged Electron app through the main-process
  inspector: menu click -> `level:1, factor:1.2`; injected Ctrl+= -> `level:2`;
  Ctrl+0 -> `level:0`. The test used Electron `sendInputEvent` rather than a
  physical key.
- **Skills has a Coding Agent pack.** It lists all 17 coding tools and shows
  `17/17 tools live`; catalogue still reports 56/56 live. It deliberately
  overlaps Workspace Files on the six basic file verbs, and the filter matches
  any pack membership: "Workspace Files" returns exactly those six,
  "Coding Agent" returns all 17.
- **Markdown code fences have a language label and Copy button.** Copy is a
  single delegated listener in `App`, so it works in chat, reports, Inspector,
  Research, Code, AgentPanel and Authoring rather than only chat. Verified by
  clicking an injected fence: clipboard contained the source, label changed to
  `Copied`, `data-state=ok`, then reset after 1.4 s.
- **Stored PDF/DOCX/XLSX previews are rendered server-side.** New gateway route
  `GET /v1/documents/{id}/preview` plus agent proxy
  `GET /api/documents/{id}/preview`; Documents opens `DocumentViewer` for
  pdf/docx/xlsx and keeps the sandboxed blob/iframe path for text-like files.
  Live: `doc-80c02d11c0` returned 10 PNG pages in 1,281 ms; the modal showed
  10 pages, 827x1170 images, no console errors.
- **Authoring no longer downloads every full PDF before showing a preview.**
  Artifact preview metadata is fetched in parallel and the eager `pdfUrls` blob
  state was removed. Live: first `.docview` in 958 ms, four pages.
- **Malformed uploads now fail honestly instead of looping on `pending`.**
  `_reconcile_registry_with_plane()` treated `index_version == 0` as stale
  parser output; a parse failure never reaches chunking, so every documents
  request reset it to `pending` and restarted the doomed parse. Reconciliation
  now skips `FAILED`; `doc-6d6a61a169` is `failed` with
  `could not parse the file: Cannot find Root object in pdf`, stable across
  repeated requests and a manual reindex.

### Verification
- Gateway full suite: **1181 passed, 18 skipped**.
- Agent full suite: **1684 passed, 39 skipped**.
- Frontend: `npx tsc --noEmit` clean; production build clean.
- Desktop rebuilt with `build_desktop.ps1`, relaunched normally, title
  `Aria - hi`; `GET /api/health` returned `agent: ready, gateway_up: true,
  spa_built: true`.

### Still open / new evidence
- **M - coding pack and Workspace Files overlap.** Disabling one changes the
  other's live count because both contain the six file verbs. Intentional for
  discoverability, but the pack controls are coupled.
- **L - code-copy buttons now appear on every `renderMarkdown` surface.** The
  delegated listener makes them functional; there is no per-surface opt-out.
- Authoring still calls `api.artifactPreview` (pypdf extraction) before opening
  the viewer; the full blob download is gone, but the metadata path remains on
  the critical path.

## Round 10 - dynamic audit repairs (2026-10-20)

### Fixed
- **Corrupt FAISS sidecars no longer disable vector search silently.**
  `vector.py` writes index+ids atomically, quarantines unreadable pairs, and
  rebuilds from `memory.json`; `service.py` also rebuilds on an exact id/count
  mismatch. Live stats now match item/embedded/index counts in every populated
  drawer; three corrupt drawers were quarantined and rebuilt.
- **Part volume is measured from the mesh centroid, not the world origin.**
  Off-origin open meshes no longer report coordinate-offset "volume", and a
  single inverted face touching the origin is visible again. Gateway `parts`
  suite: **223 passed**.
- **Authoring idempotency is stable per brief.** A retry joins the in-flight
  run instead of paying for a duplicate; a deliberate re-run after `done`
  still starts fresh.
- **Authoring Stop now settles.** Cancel is acknowledged, the run stays in the
  `/produced` polling set, and files written before the cancel boundary appear.
- **Chat streams real deltas.** Tool-loop and no-tool paths both use streaming
  LLM calls; the old replay-the-finished-answer path is gone.
- **Live reads are uncached.** Graph, events, documents, schedules, tracker,
  tools and sessions use `no-store`; cache invalidation has a generation guard.
- **The authoring routing prefix no longer adds a length floor.** Bare
  `Create a document.` briefs are not inflated, and "one page"/"at most N"
  requests do not also get a minimum.

### Verification
- Gateway full suite: **1186 passed, 18 skipped**.
- Agent full suite: **1692 passed, 39 skipped**.
- Frontend unit tests **59 passed** under `vitest run src`; bare `vitest run`
  also picks up `e2e/` Playwright specs and reports them as failed suites (the
  `e2e/` directory must be excluded from Vitest). `tsc` and production build
  clean.
- Services restarted; `/api/health` = `agent: ready, gateway_up: true,
  spa_built: true`.

### Still open
- C5 and any remaining C7/C8 sub-cases still need the audit's exact wording;
  the source-level idempotency and stop-settling defects above were confirmed
  and fixed, but the original finding text is not in the working context.
- Authoring first-page latency is still ~11 s (measured twice after the pypdf
  gate was removed); root cause not found.
- H4/H5 ledger/pricing, H7/H8 scheduler/app edge cases, H11-H14 frontend,
  H21/H22 state, and H26 KeepAlive remain unscheduled without the audit text.

## Round 11 - the rest of the dynamic audit (2026-10-21)

The audit's Critical and High findings were worked in order. This round
records what was fixed, what was reviewed and deliberately left alone, and
what is still open.

### Fixed

- **C5 - an authoring run could die with the tab.** The worker now starts
  BEFORE the `started` frame and owns the idempotency claim, the cost
  record and the cancel-registry cleanup in its own `finally`. The stream
  generator releases the claim only when no worker ever started; the
  disconnect branch must not release it, because the worker is still running
  and a second identical run would start and bill.
- **C6 - a part's volume was measured from the world origin.** The mesh
  centroid is now computed area-weighted, and an inverted or empty solid is
  penalised by 1e12 so it can never out-score a valid mesh, whatever the
  triangle budget.
- **C7 - a second identical submit could start a duplicate run.** The
  in-flight claim is atomic (claim, or join the run that already holds it)
  and the dedup stream no longer releases a claim it did not take. In
  `flow.py` a cancelled node is re-raised instead of being recorded as a
  failed node, so a cancelled run stops dispatching.
- **C8 - Stop did nothing before the worker was registered.** A Stop that
  arrives in that window is remembered (`_PENDING_CANCELS`) and honoured
  when the worker registers. The cancel endpoint now returns `status`
  (`cancelled` | `pending` | `missing`), `found`, `cancelled` and the
  resolved `session_id`; Research mints the conversation id before the
  request and sends both ids.
- **H4/H5 - turns on one session could interleave, and a cancelled turn
  lost its cost.** A per-session turn lock serialises turns and is released
  in a `finally` on every path; cost is recorded on success, failure and
  cancellation, with a synchronous fallback when a disconnect tears down
  the async path.
- **H6 - "two paragraphs" was not a limit.** Explicit unit counts
  (one-ten, or a number) with `sentence`/`paragraph`/`bullet` are ceilings
  now, and they scale: two paragraphs = 240 words, three sentences = 90,
  five bullets = 100.
- **H7 - go-to-definition returned a path the console could not open.** The
  definition call measures against the requested root, and the endpoint maps
  the result to a client-visible path.
- **H8 - undo in the code editor left the old buffer in place.** The
  restore callback updates the body snapshot as well as the editor, so the
  next save no longer writes the pre-undo text.
- **H11 - a skills-guard error left the page with no way back.** The
  guard-error state offers "Enable" as a recovery and reads the pack counts
  as "counts unknown" instead of inventing a number.
- **H12 - the terminal could not be restarted and the launch token was
  captured once.** The bare dock has a "Restart shell" button, and
  `launchToken()` re-reads the meta tag for every request.
- **H13 - the command palette swallowed Tab.** The handler always prevents
  the default and moves focus explicitly.
- **H14 - every visited view owned `id="main"`.** `useMainId()` gives the id
  to the visible keep-alive section only, and the skip link focuses it.
- **H21/H22 - memory observations were clipped and only 50 were fetched.**
  The list wraps and clips; the client asks for 200 and the server default
  is 200.
- **H26 - hidden views kept polling the server.** `KeepAlive` exposes
  `useSectionActive`/`useActivePoll`, and every converted view pauses while
  hidden and refreshes on return.
- **M60 - "a deck" and "slides on X" did not pin the format.** The
  request-shape families in `_format_requested_in()` now capture the format
  token in every case, so "a short deck" resolves to `pptx` while a passing
  mention still pins nothing.

### Reviewed and deliberately not changed

- The prefab HMAC scope bypass, the ReportCard XSS, the SSE `\r\n`, the
  Research stale-state writes, the cancel wiring itself, `AgentResult.cost`,
  the memory classifier and the percent-encoded path traversal were
  re-checked against the code: each is either already handled or is inert
  as written. The Documents sandbox XSS is inert only because
  `allow-scripts` is absent - that is the thing to keep absent.
- The tool catalogue is **56**, not the number the audit quoted.
- The ModelPicker defect is the inverse of M73: "Auto" cannot be selected
  once a model is stored.

### Verification

- Agent full suite: **1698 passed, 40 skipped** (one outdated assertion in
  `test_api_validation.py` was updated: the claim is now released by the
  worker, so the disconnect branch must NOT release it).
- Gateway full suite: **1189 passed, 18 skipped**.
- Frontend unit tests: **59 passed** (`vitest run src`); `tsc --noEmit` and
  the production build are clean.
- New/updated regression tests: `test_cancel.py` (stop-before-worker,
  status contract), `test_idempotency_release.py` (worker-before-frame,
  turn-lock release), `test_api_validation.py` (claim ownership),
  `test_brief_format_is_binding.py` (deck/slides capture groups),
  `test_length_contract.py` (unit ceilings), `parts/tests/test_optimise.py`
  (invalid-solid dominance), `test_code_workspace_api.py` (definition path).

### Still open

- Authoring first-page latency is still ~11 s; the pypdf gate is gone and
  the root cause is still not found.
- A page left open across an agent restart still needs a reload to learn a
  new per-launch token.
- True filesystem/network isolation, the `auto_approve: true` bypass of the
  permission gate, `run_checks` output not reaching the agent, the missing
  tool results in history, the dead external credentials and the
  Coding/Workspace pack overlap are unchanged.
- The remaining Medium and Low audit findings (scheduler, ledger, memory,
  research, runs, mission, shell/a11y, code, authoring, parts, settings,
  model picker, skills, misc, L1-L12) have not been worked yet.

## Round 12 - the ledger, the router, the search, and computer-use (2026-10-21)

This round started from a question about the gateway ledger ("why do the
calls mix cohere north code and gemini, and why did a `hi` send 4k tokens?")
and ended with computer-use wired to the agent and to the console. Both
halves are recorded here.

### What the ledger actually showed

- **The mix is two different callers, not one agent.** `cohere/north-mini-code:free`
  (via Kilo, the free code model) is the CHAT worker; it carries the
  system prompt, the 25 chat tool schemas and the memory, so it is billed
  ~3,100 input tokens per turn. The small Gemini calls (~420 tokens) are
  the MEMORY subsystem's own helper calls - the classifier and the
  quote-support check in `memory.py` - which run with `auto_route="memory"`.
- **A third of the ledger was waste.** 217 `router_memory` calls in 24h,
  100% errors, 1.5-4s each: the free router model does not follow the tier
  instruction, so its reply was discarded and the deterministic rule
  decided anyway - after the round trip. That is BUG-076.
- **"hi" costs 4k tokens because the tools and the system prompt are
  static.** Measured: 25 tool schemas = 15,370 chars (~3.8k tokens) and
  the system prompt = 6,192 chars (~1.5k tokens), sent on EVERY turn.
  The user's own words are two tokens of that. It is inherent to a
  tool-using chat; the honest fix is prompt caching, not trimming the
  behaviour rules, and the model in use is free.
- **The web search was slow because nothing bounded it** (BUG-077): four
  sources, each allowed its own timeout, the worst call 25.5s.

### Fixed

- **The tier router** no longer spends a call it will throw away (small
  prompts skip it; a router that mangles its replies is benched; an
  echoed option list is no longer read as HUGE).
- **Web search** has a per-source budget (6s), a chain budget (15s) and a
  working cooldown for a source that just errored - with the cooldown as a
  last resort, so it can never turn a search into an empty result. Live:
  8 results in 6.5s (was 9.2s average, 25.5s worst).
- **Memory writes** no longer orphan a record when the supersede target is
  gone (BUG-078), and a repeat sighting reinforces the LIVE row
  (BUG-079). The live profile drawer held 111 rows for 6 observations; it
  now holds 7, and an exact-match search comes back first.
- **A document that never parses** now fails with a reason instead of
  parking on "parsing - 0 chunks" and blocking every other upload.
- **The deployment probe authenticates** (BUG-085): it had been skipping
  all 18 tests while the gateway was up, and the moment it ran for real it
  found the memory round-trip failure - which was the duplicates.

### Computer-use, wired up and tested

- **The L3 layer of the desktop agent was dead**: the vision call sent no
  gateway token (401) and the vision judge never asked for JSON, so every
  escalation died with "vision returned unexpected type: NoneType"
  (BUG-081, BUG-082). Both fixed and verified live.
- **The approval queue was unreachable** (BUG-083). The gates name a "web
  UI" that did not exist, and the agent could not even mint a pending
  approval, because the cascade drives a GUI and a GUI click is not
  approval-worthy. Now:
  - a `computer` node with `metadata.action` (run_command / read_file /
    write_file / open_app) takes the gated shell route, which is the only
    path that can mint an approval;
  - the console has a **Computer** page - mode, pending approvals with
    their exact arguments, approve/reject, the redacted audit trail, the
    recorded trajectories;
  - `start_aria.ps1` enables the feature in DRY-RUN mode (nothing
    executes; a sensitive action still waits for a human). Live mode is
    `-ComputerUseMode live`, deliberately not the default.
- **Live end-to-end**: the agent asked for `python --version`, the gate
  minted `cu-1792538640-1`, the page showed the action and its arguments,
  one click resolved it, the audit recorded `pending-approval` then
  `approved`, and in live mode the approved command executed
  (`returncode 0`, `Python 3.8.2`).

### Verification

- Agent full suite: **1708 passed, 39 skipped**.
- Gateway full suite: **1216 passed, 2 skipped** - the two skips are Gmail
  and GitHub, whose credentials are dead (skipped on purpose, not passed).
- Frontend unit tests: **64 passed**; `tsc` and the production build are
  clean; `e2e/computer.spec.ts` passes in a real browser.

### Still open

- The chat's static payload (5.4k tokens per turn) is unchanged; only
  prompt caching would cut it, and the model in use is free.
- The remaining Medium/Low audit findings are untouched.
- Authoring first-page latency is still ~11 s.

## Round 13 - checking the Computer page in a real browser (2026-10-21)

The page was verified the way a user meets it: a real browser, a real pending
approval minted by the agent itself, and every tab opened.

**What already worked.** Rail entry and route, the Control sidebar with the
pending count, the four stats, the dry-run banner, a pending `run_command`
with its exact arguments, approve/reject resolving through the gate, the
empty state, the audit log (redacted), the recordings list, and the replay
guard on the server (403 when off, 400 when not live). No console errors.

**Four defects the browser check found, all fixed:**

1. **DRIVER said "AX no tree - shots none" on a healthy host** (BUG-086). The
   probe read `windows` off `get_desktop_state`; the Windows driver answers
   that with display metadata and no window list. The AX tree is a separate
   call and returned 16 windows; the desktop screenshot was right there in
   the same response. The page now reads `AX ok - shots ok`, which is the
   truth, and two tests pin both directions (tree, and no tree).
2. **The audit tab rendered every line ever written** (BUG-087) - 6,897 of
   them, mostly clicks, with the approvals invisible at the bottom.
   `/api/audit` now takes `limit` and the page shows "newest 400 of 6,904".
3. **The approval id was not on the page** (BUG-088), although the agent's
   answer quotes it.
4. **Replay was offered in dry-run** (BUG-089) where it can only 400, and
   **a recording could be replayed but not read** (BUG-090) - the id now
   expands to its turns and calls.

**Verification:** agent suite 1712 passed / 39 skipped; frontend 64 passed;
`tsc` and the production build clean; `e2e/computer.spec.ts` 2 passed
(including the new "replay is disabled in dry-run" assertion); the scratch
tab-by-tab browser pass passed with zero console errors.

