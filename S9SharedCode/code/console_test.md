# Aria Console — Cross-Section Test Manual

> **Audience:** a testing agent that is excellent at *executing* checks but should
> not have to *decide* what to test. Every check below is written as exact steps
> with an exact expected result. Follow them in order. Do not skip the Setup or
> Safety sections. Do not invent extra destructive actions.
>
> **Scope:** every console section EXCEPT Studio (Studio has its own checklist in
> `TEST.md`). This manual covers Chat, Runs, Memory, Documents, Research,
> Scheduler, Skills, Code, Authoring, Apps, Ledger, Mission/Console, Parts,
> Computer, Settings.
>
> **How to judge a check:** a check PASSES only if the *Expected* outcome is
> observed exactly. If the UI shows something different, or an API returns an
> unexpected status/body, it FAILS — record what you actually saw. Never mark a
> check passed because "nothing errored"; verify the positive signal.

---

## 0. Setup (do this first, once)

0.1 **Start the services.** From the repo root
`C:\Users\AISHWARYA\Downloads\project3` run:
`powershell -ExecutionPolicy Bypass -File start_aria.ps1`
This starts the agent on `http://127.0.0.1:8500` and the gateway on
`http://127.0.0.1:8109`. Wait until both respond.

0.2 **Verify health.** `curl -s http://127.0.0.1:8500/api/health` must return JSON
with `"agent":"ready"` and `"gateway_up":true`. If `gateway_up` is false, STOP and
report "gateway down" — most model-backed checks will fail for that reason, not
because of a product bug.

0.3 **Read the auth token.** The agent requires a bearer token on `/api/*`.
Read it once: `TOK=$(cat S9SharedCode/code/state/agent.token)`.
Every API call below must send the header `X-Aria-Token: $TOK`.
Example: `curl -s -H "X-Aria-Token: $TOK" http://127.0.0.1:8500/api/tools`.
If the file does not exist, the agent is running with auth off; you may omit the
header.

0.4 **Open the console.** In a browser go to `http://127.0.0.1:8500/console`.
The left rail lists the sections. The rail item labelled **"Console"** is the
**Mission** page (`/mission`) — do not confuse it with the Chat home (`/console`).

0.5 **Two ways to verify.** For each check you may verify via the UI (click and
look) AND via the API (curl). Prefer doing both when cheap. A UI action is only
"real" if a subsequent API read or on-disk state confirms it.

### Safety rules (mandatory)
- Use **test fixtures** (files you create, threads you create). Never delete or
 wipe real user data.
- Memory "Wipe session" and Documents/Scheduler/Apps/Parts delete actions must
 only target items **you created in this run**.
- For any confirm dialog, first test **Cancel** (nothing changes), then Confirm.
- Do not toggle feature flags off and leave them off; restore the original value.
- Record every created item's id so you can clean it up at the end.

### Reporting format
For each check id write one line:
`[PASS|FAIL] <id> — <what you observed>` ; on FAIL include the actual status/body
or screenshot path. At the end give counts and list all FAILs.

---

## 1. Chat — `/console`

1.1 **Load.** Navigate `/console`. Expected: thread sidebar + composer with
"Send message" button and an engine toggle (Light/DAG). No error banner.

1.2 **New thread.** Click "Start new chat" (+). Expected: empty transcript, composer
focused. API: `GET /api/chat/threads` returns a list (your new thread appears
after you send).

1.3 **Light send.** Set engine Light. Type `Say hello in one word.` and click
"Send message". Expected: a reply streams into the transcript; the thread appears
in the sidebar with a model badge. API: a new id in `GET /api/chat/threads`.

1.4 **Queue while busy.** Send a longer prompt; while it is running the button
reads "Queue message". Click it with a second message. Expected: second message
is queued and answered after the first; no crash.

1.5 **Stop.** Start a long prompt, click "stop". Expected: the run cancels
(`POST /api/chat/cancel` returns ok) and the transcript shows a stopped note, not
a hang.

1.6 **Attach.** Use "Attach files" to upload a small `.md` you create. Expected:
upload succeeds (`POST /api/documents` 200) and the file is referenced/available;
ask "what does the attached file say?" and expect an answer grounded in it.

1.7 **Delete thread.** On a thread YOU created, click "Delete thread …", confirm.
Expected: it disappears; `GET /api/chat/threads/{cid}` then 404s.

1.8 **Edge — gateway down.** (Only if you can safely stop the gateway; otherwise
skip and note skipped.) With gateway down, sending shows an error bubble, not a
silent success.

## 2. Runs — `/runs`

2.1 **Load.** Navigate `/runs`. Expected: a list of run rows with status badges
(ok/err) and a "Filter runs" input. API: `GET /api/runs/summary?limit=20` 200.

2.2 **Open a run.** Click a row that has a graph. Expected: graph view renders
nodes/edges (`GET /api/sessions/{sid}/graph?light=true` 200 with nodes).

2.3 **Filter.** Type a status in "Status filter" (err). Expected: only err rows.

2.4 **Edge — light chats absent.** A Light-engine chat (1.3) must NOT appear in
Runs. Verify its id is absent from `GET /api/runs/summary`.

2.5 **Edge — no graph.** Open a partial/light run if present; expect the empty
state "This run has no graph", not a crash.

## 3. Memory — `/memory`

3.1 **Load.** Navigate `/memory`. Expected: kind tabs + "Search memory…" + a
"Remember" form; `GET /api/memory/stats` 200.

3.2 **Remember.** In "What should Aria remember?" type a unique fact
(`TESTFACT-<timestamp> likes teal`) + keywords, click "Remember". Expected: the
fact appears in the list and stats count increments (`POST /api/memory/remember`
200; `GET /api/memory?…` contains it).

3.3 **Search.** Search the unique fact. Expected: it is returned.

3.4 **Forget.** On that fact click "Forget this memory"/delete, confirm. Expected:
it is gone (`DELETE /api/memory/{id}` 200; search no longer returns it).

3.5 **Edge — wipe guard.** In "Session id to wipe" leave empty and click "Wipe
session". Expected: it must NOT wipe everything (error or no-op). Do not wipe a
real session.

## 4. Documents — `/documents`

4.1 **Load.** Navigate `/documents`. Expected: folder rail + doc list + "Upload
documents". `GET /api/documents` 200.

4.2 **Upload.** Upload a small `.md` you create. Expected: a row appears and
progresses to status `ready` with chunk counts; "in use" pill present.

4.3 **Search.** In "Test-search enabled docs…" type a word from your file, run
search. Expected: a hit "from enabled documents" referencing your file
(`POST /api/documents/search` 200 with hits).

4.4 **Disable excludes.** Click "Disable" on your doc, re-run the search.
Expected: your doc no longer appears in hits. Re-enable afterwards.

4.5 **Folders.** "New main folder" → create `test-folder-<ts>`. Expected: appears
(`POST /api/documents/folders`). Move your doc into it, then try "Delete empty
folder" on a NON-empty folder → expect refusal; delete after moving the doc out.

4.6 **Cleanup.** Delete your test doc (`DELETE /api/documents/{id}`) and folder.

## 5. Research — `/research`

5.1 **Load.** Navigate `/research`. Expected: topic textarea, depth group
(quick/standard/deep), "Research" button.

5.2 **Run quick.** Enter a narrow topic, depth quick, click "Research". Expected:
live graph progresses and a report pane fills; "Copy"/"Download .md" become
enabled. API: a session appears in `GET /api/sessions`; `…/produced` returns the
report.

5.3 **Download.** Click "Download .md". Expected: a markdown file downloads with
non-empty content.

5.4 **Stop.** Start another run, click "Stop". Expected: run cancels; partial
report remains; no hang.

5.5 **Edge — follow-up.** After a finished run, type a follow-up and "Send".
Expected: it reuses the run context (same session id), not a brand-new topic.

## 6. Scheduler — `/scheduler`

6.1 **Load.** Navigate `/scheduler`. Expected: query + schedule inputs, "Schedule"
button, sections ARMED / FINISHED / WATCHES. `GET /api/schedule` 200.

6.2 **Schedule.** Query `TEST stretch reminder`, schedule `in 30m`, click
"Schedule". Expected: job appears under ARMED with a next-fire time
(`POST /api/schedule` 200; listed in `GET /api/schedule`).

6.3 **Cancel.** On your job click "Cancel". Expected: it moves to FINISHED
(`DELETE /api/schedule/{id}` 200).

6.4 **Hard delete.** On your job click "Delete". Expected: row removed
(`DELETE /api/schedule/{id}?hard=true`).

6.5 **Edge — invalid when.** Schedule with `when=not-a-time`. Expected: an error
message and NO job created.

## 7. Skills — `/skills`

7.1 **Load.** Navigate `/skills`. Expected: pack cards + tool table with
Enable/Withhold buttons. `GET /api/tools` 200.

7.2 **Toggle a tool.** Pick a harmless tool, click "Withhold tool {name}".
Expected: state flips to withheld and persists after reload
(`POST /api/config/tools` 200; `GET /api/tools` guard list reflects it). Re-enable.

7.3 **Toggle a pack.** Click "Disable pack {name}" then "Enable pack". Expected:
all tools in the pack flip and back.

7.4 **Edge — failed toggle.** If a toggle returns 500, expect an amber alert
"toggle did not apply: …" and the control reverts (must not look applied).

## 8. Code — `/code`

> If the `ui.code_ide` flag is ON, `/code` redirects to Studio; in that case note
> "redirected to Studio" and test the legacy page only if the flag is OFF. Restore
> the flag afterwards.

8.1 **Load.** Navigate `/code`. Expected: Explorer + editor + agent pane, "Open
folder"/"New playground".

8.2 **Playground + file.** "New playground", then "New file" `t.py`, type
`x = 1`, "Save to disk". Expected: dirty dot clears; file exists on disk
(`GET /api/code/file?path=…` returns `x = 1`).

8.3 **Agent edit.** In the agent pane (Agent mode) ask "change x to 2". Expected:
the tree row is marked changed/unreviewed; a Keep/Revert control appears. Click
Keep; expect file now `x = 2`.

8.4 **Checks.** Click "Run the project's lint, typecheck and tests". Expected:
results panel shows pass/fail per tool (`GET /api/code/checks`).

8.5 **Edge — mtime conflict.** Save a file, then modify it on disk externally,
then save again from the editor. Expected: a conflict prompt, not silent
overwrite.

## 9. Authoring — `/authoring`

9.1 **Load.** Navigate `/authoring`. Expected: composer "Describe the document you
want…", format options.

9.2 **Generate.** Ask `a one-page PDF titled TEST DOC with three bullets`. Send.
Expected: a produced-file card with preview appears (`GET /api/sessions/{sid}/produced`).

9.3 **Get.** Click "Get". Expected: a valid PDF downloads (non-zero bytes, correct
magic `%PDF`).

9.4 **Index.** Use index-to-Documents. Expected: the doc appears in Documents.

9.5 **Edge — auth on download.** `GET /api/artifact/{id}?download=1` WITHOUT the
token must 403; with token 200.

## 10. Apps — `/apps`

10.1 **Load.** Navigate `/apps`. Expected: app cards + "New app". `GET /api/apps` 200.

10.2 **Build.** "New app" → describe `a habit tracker with water and sleep` →
"Build app". Expected: "Building…" banner then an app card with items/surface
(`POST /api/apps/build` 200; `GET /api/apps/{id}`).

10.3 **Refresh.** "Refresh app now". Expected: items update or an `error` is shown
without wiping existing items.

10.4 **Delete cancel/confirm.** "Delete app" → Cancel (nothing changes) → Confirm
(row removed, `DELETE /api/apps/{id}` 200).

## 11. Ledger — `/ledger`

11.1 **Load.** Navigate `/ledger`. Expected: CALLS/SKILLS/TURNS stats + spend rows.
`GET /api/cost/by_skill` 200.

11.2 **Scope.** Switch to "One conversation", pick a recent id. Expected: numbers
scope to that conversation, not the whole ledger.

11.3 **Edge — unknown id.** Paste a bogus conversation id. Expected: empty/error,
NOT whole-ledger numbers. Missing pricing must not render as a fake $0.0000 success.

## 12. Mission / Console — `/mission`

12.1 **Load.** Navigate `/mission` (rail label "Console"). Expected: "● live" pill,
event feed with timestamps; `GET /api/events?limit=200` 200.

12.2 **Filter.** Click "errors" source and type in "Filter events". Expected: feed
filters; ERRORS stat counts err lines.

12.3 **Pause/resume.** Click "pause" then "resume". Expected: feed freezes then
updates.

12.4 **Edge — feed down.** If the agent is stopped, expect red "● feed error" +
"Cannot read the event feed", NOT "live/OK". (Restore agent afterwards.)

## 13. Parts — `/parts`

13.1 **Load.** Navigate `/parts`. Expected: "Import a part", saved-parts list,
`GET /api/parts` 200.

13.2 **Import.** "Import a part" → choose a small STL/OBJ you create → "Import &
measure". Expected: part appears with metrics; a version v1 exists.

13.3 **Repair versioning.** Run repair/analyse. Expected: a NEW version is added
and v1 is preserved (`GET /api/parts/{id}` versions length increases).

13.4 **Restore.** "Restore" an old version. Expected: current geometry reverts.

13.5 **Cleanup.** Delete your part. Edge: duplicate import of the same mesh offers
the existing part rather than silently duplicating.

## 14. Computer — `/computer`

14.1 **Load.** Navigate `/computer`. Expected: approvals/audit/runs tabs. If
`COMPUTER_USE_ENABLED` is off, expect a disabled state (note it, don't fail).

14.2 **Approve/reject.** If a pending action exists (create one only if safe),
click "reject". Expected: flash "Rejected: … The action did not run." and the item
leaves pending; the action must NOT execute. Then test "approve" on a separate
safe item.

14.3 **Audit.** Open audit tab. Expected: redacted lines (`GET /api/audit?redact=true`).

## 15. Settings — `/settings`

15.1 **Load.** Navigate `/settings`. Expected: health, token inputs, feature-flag
toggles, raw config. `GET /api/health` + `GET /api/config` 200.

15.2 **Flag round-trip.** Toggle a harmless flag (e.g. `browser.tools`), reload,
verify it persisted, then restore the original value.

15.3 **Edge — bad gateway token.** Enter an invalid gateway user token. Expected:
amber "token … was rejected", not a saved success.

15.4 **Edge — agent down.** With agent stopped, expect "agent unreachable — is it
running on :8500?" + retry, not a blank page. (Restore agent.)

---

## 16. Cleanup & final report
- Delete every fixture you created (threads, docs, folders, jobs, apps, parts,
 memories, files). Restore any flag you changed.
- Produce the report: total checks, PASS count, FAIL count, and each FAIL with the
 observed actual result. List any checks you skipped and why.