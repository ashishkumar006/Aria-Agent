# Known Bugs & Issues

Tracked from E2E testing (see `E2E_TEST_PLAN.md` §12).

## BUG-001: Parallel sibling nodes crash with `ExceptionGroup` (§8.2)
- **Severity:** High (breaks fan-out, a core routing feature)
- **Symptom:** When the planner fans out N parallel nodes (e.g. 3 `researcher`
  for a multi-subject compare), 1–2 siblings die with
  `ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)` during
  the parallel `asyncio.gather` in `flow.py`.
- **Evidence:** `e2e-82b` / `s8-096edb88` — planner emitted n:2/n:3/n:4
  (London/Paris/Berlin); n:3 & n:4 failed; recovery planner re-emitted as
  n:7/n:8. `graph.json` shows `failure_report: ... ExceptionGroup: unhandled
  errors in a TaskGroup`.
- **Root cause:** `flow.py` ~line 291 `asyncio.gather(*[_run_one(...) for nid
  in ready])` — a `TaskGroup` exception inside one `_run_one` (or the skill it
  calls) propagates out of `gather` and aborts the whole batch, instead of
  being isolated per-node.
- **Fix direction:** isolate per-sibling failure — either
  `asyncio.gather(*tasks, return_exceptions=True)` and mark each failed node
  individually, or wrap each `_run_one` call in try/except so one sibling's
  `ExceptionGroup` becomes that node's `AgentResult(success=False, error=...)`
  rather than killing the batch. Recovery path already handles a single failed
  node correctly; the bug is only the *parallel* propagation.
- **Fix applied (2026-08-21):** `flow.py` executor loop now uses
  `asyncio.gather(..., return_exceptions=True)`; any `BaseException` returned
  is converted to a failed `AgentResult` for that node (marked failed,
  individually recoverable) instead of aborting the batch.
- **Verified:** `e2e-131` / `s8-43e6447e` — 3 parallel researchers (London/
  Paris/Berlin) all `status=complete`, **0 recovery nodes** (previously 2 of 3
  died with `ExceptionGroup`). §1.3 now fully green.
- **Status:** **FIXED.**

## BUG-002: Local Qwen2.5-1.5B a11y unreliable on multi-turn (§10)
- **Severity:** Medium (only affects the optional local-a11y path; not wired in)
- **Symptom:** On a 5-turn browser task the local model drifted to prose
  (`thinking: ...`) with no JSON, then got stuck clicking the same mark.
- **Fix direction:** hardened a11y prompt + JSON normalizer (strip fences,
  coerce `mark`→int, drop unknown `type`) + no-op-retry (re-enumerate, confirm
  page changed). Needs GPU/Q4 quant to be fast enough on this laptop.
- **Status:** **WON'T FIX — user decided not to use the local Qwen model
  (2026-08-21).** Benchmark data in `E2E_TEST_PLAN.md` §10 kept for reference
  only. Browser skill stays on the gateway a11y path.

## BUG-003: Offline gated-shell inert without vision judge (§8.1)
- **Severity:** Low (graceful `error`, no 500)
- **Symptom:** `_shell_intent_offline` returns `None` when no vision model is
  configured, so the offline answer degrades to a graceful error rather than a
  shell answer.
- **Fix direction:** plug a local `Qwen2.5-VL-3B` in as the vision judge (note:
  separate from the text-only a11y use case in BUG-002).
- **Status:** Open.

## BUG-004: Research DAG always fails - the brief was the run's USER_QUERY
- **Severity:** Critical (every Research-page run degraded)
- **Symptom:** every run logged `researcher returned no usable JSON: reply was
  not JSON (starts '# Executive Summary...')`, after up to ~300s per researcher,
  then a recovery planner re-emitted the same instruction; the user got a
  partial or apologetic answer.
- **Evidence:** `state/sessions/s8-079ce1b3`, `s8-f0df231d`, `s8-78d63582`,
  `s8-ca939ff3` - `query.txt` held the console's multi-role brief ("You are a
  research agent... deliver a final report as Markdown"), and `run_status.json`
  held the matching "no usable JSON" failures.
- **Root cause:** every DAG node receives `USER_QUERY`. The console sent a
  brief addressed to a single research agent; each researcher obeyed it and
  wrote the final report instead of its JSON contract.
- **Fix (2026-10-18):** the TOPIC is the run query. Depth is a planner-only
  `run_note` (`agent_server._research_depth_note` -> `Executor.run(run_note=)`),
  appended by `flow._node_note` only for the planner. The console no longer
  builds a brief (`api.briefFor` removed). `formatter.md` gained the
  no-banner/no-internals rule the brief used to carry.
- **Verified:** `s8-95a538b1`, `s8-1fb46950` - planner, researcher and
  formatter all `complete`; 4,594-5,142 char reports; zero "no usable JSON".
- **Status:** **FIXED.**

## BUG-005: Planner sibling positions wired the formatter to the planner
- **Severity:** High (a successful research node was thrown away)
- **Symptom:** researcher completes with real findings, formatter answers
  "upstream research findings were not provided", user gets a 272-char apology.
- **Evidence:** `s8-7f7bbe50` - `graph.json` had only `n:1 (planner) -> n:3`
  `(formatter)`; the planner's output listed the formatter with
  `"inputs": ["0"]`. `n_003.json`'s prompt showed the planner's node list as
  INPUTS.
- **Root cause:** `Graph.extend_from` resolved `n:<label>`/`n:<int>`/labels but
  not a bare zero-based SIBLING POSITION, so `"0"` hit the
  "unresolvable input" fallback and was rewired to the planner (the parent).
- **Fix (2026-10-18):** `extend_from` maps a bare integer in range to that
  sibling's id (after all id forms), and logs a warning when it still has to
  fall back. `planner.md` now spells out label/position/`USER_QUERY` and warns
  that `n:1` is an existing graph id, not "the first node you listed".
- **Verified:** `tests/test_plan_input_indices.py`; live `s8-95a538b1` /
  `s8-1fb46950` produce full reports.
- **Status:** **FIXED.**

## BUG-006: MCP teardown fault discarded a completed tool loop
- **Severity:** High (fails nodes that already succeeded)
- **Symptom:** `ExceptionGroup: unhandled errors in a TaskGroup (1
  sub-exception)` from `mcp_runner.run_with_tools`, with the node's
  `elapsed_s` at 0.0.
- **Root cause:** `run_tool_loop` returned its result, then the anyio task
  group closing the stdio session raised; `return` was inside both `async
  with` blocks, so the fault replaced the answer.
- **Fix (2026-10-18):** the loop result is assigned outside the client blocks;
  a teardown fault with a result in hand returns the result (and does not trip
  the breaker). With no result, the real cause is unwrapped from the group's
  `__context__`, tripped and re-raised. `flow._readable_exc` unwraps groups in
  node errors.
- **Verified:** `tests/test_mcp_teardown_guard.py`; live runs above show no
  ExceptionGroup.
- **Status:** **FIXED.**

## BUG-007: Research nodes die 502 on Gemini `thought_signature` replay
- **Severity:** High (kills researcher/author nodes at 0.0 s)
- **Symptom:** researcher/author node failed with
  `HTTPStatusError: Server error '502 Bad Gateway'`; `gateway_v8.db` recorded
  Gemini HTTP 400 `Function call is missing a thought_signature in functionCall
  parts ... position N` against `web_search` / `fetch_url` / `read_artifact`.
  Three researchers failed on the same plan twice in a row.
- **Root cause:** Gemini 3 returns a signature on only the first part of a
  parallel batch, sometimes on none at all, and rejects the REPLAY of any
  `functionCall` part without one. The adapter replayed tool calls exactly as
  received.
- **Fix (2026-10-18, `llm_gatewayV9/providers.py` `GeminiProvider`):** carry
  `provider_meta.thoughtSignature` back; if a part has none, use the newest
  signature seen in the conversation (verified live: the exact request that
  400'd returned 200 with a borrowed signature); if the conversation has none
  at all (the author's first tool batch), render that call and its result as
  prose so Gemini never sees an unsigned `functionCall`.
- **Verified:** `tests/test_gemini_thought_signature.py` (4); live standard
  runs `s8-c7162a1f` (3 researchers complete, 19,661-char report) and
  `s8-9150ca35` (files=0) with no gateway error rows.
- **Status:** **FIXED.**

## BUG-008: Kilo fallback rejects an empty tool result
- **Severity:** High (fails the node whenever all Gemini aliases are cooling)
- **Symptom:** `kilo HTTP 400 ... invalid request: all elements in tool_results
  must have the 'outputs' property specified`, surfaced as a 502.
- **Root cause:** `OpenAICompatProvider` sent `role:"tool"` with `content: ""`
  when a tool returned nothing; Kilo's Cohere-backed ids reject the whole turn.
- **Fix (2026-10-18):** an empty tool result travels as `(no output)`.
  Measured against the live gateway: `content: ""` was a 502, `"{}"` a 200.
- **Verified:** `tests/test_openai_empty_tool_result.py` (2); live probe green.
- **Status:** **FIXED.**

## BUG-009: A Research run rendered a PDF instead of answering in the transcript
- **Severity:** High (wrong section behaviour; user asked for a report, got a file)
- **Symptom:** a research query ending "...give me a proper report" planned
  `planner -> researchers -> author -> formatter`; the author rendered
  `current-state-of-solid-state-EV-batteries.pdf` and the chat answer pointed
  at a download. Research is required to answer inline (session s8-c7162a1f).
- **Root cause:** Research and Authoring share one planner and skill registry,
  so the planner can always reach `author`; the depth note only suggested
  "researchers plus one formatter".
- **Fix (2026-10-18, `flow.py`):** `ensure_research_is_text_only` runs after
  the planner expands the graph when `run_kind == "research"`: author/deck nodes
  are removed, their successors are rewired to the evidence they consumed, and
  a terminal author becomes the formatter. Idempotent per session. The depth
  note now also states that Research never produces a file.
- **Verified:** `tests/test_research_is_text_only.py` (5); live
  `s8-9150ca35` (planner + 3 researchers + formatter, 20,705 chars, files=0)
  and delegated `s8-60be532e` (run_kind research, no `produced_files.json`).
- **Status:** **FIXED.**

## BUG-010: Chat invented a `sandbox:` download link for a delegated file
- **Severity:** Medium (dead link; file invisible in the transcript)
- **Symptom:** after a delegated run the answer said
  `[report.pdf](sandbox:/mnt/data/report.pdf)`; the `done` frame carried
  `files: 0`, so the console had no button either.
- **Root cause:** the tool result named no filenames, chat's `done` frame only
  scraped `art:` handles out of the answer, and the one-shot `/api/chat/simple`
  never even received the handoff dispatcher.
- **Fix (2026-10-18, `agent_server.py`):** `_run_handoff` returns the real
  filenames plus a `say` instruction ("do NOT write any URL or path");
  `_chat_handoff_dispatch(..., files_sink)` collects the rendered files; both
  chat endpoints report them on `done` (streaming and one-shot).
- **Verified:** `tests/test_chat_tool_surface.py` (17, +2 new); live chat
  delegation reported `files=2` with real artifact handles.
- **Status:** **FIXED.**

## BUG-011: A delegated document was titled and filed after the agent's own instruction
- **Severity:** Medium (unreadable filename, wrong cover title)
- **Symptom:** `You-are-the-authoring-agent.-Produce-a-finished-document-and-
  render-it-as-a-real.pdf`.
- **Root cause:** the delegated brief was the agent instruction with the user's
  words inside it, and `_document_title`'s fallback used the whole query.
- **Fix (2026-10-18):** `_authoring_brief` puts the user's words in the first
  paragraph and the instruction in a second one; `_document_title` ignores
  everything after the first blank line.
- **Verified:** `tests/test_document_title_and_structure.py` (+1).
- **Status:** **FIXED.**

## BUG-012: `test_the_detected_format_reaches_a_real_tool_call` passed on live luck
- **Severity:** Medium (false green; failed in the full suite, passed alone)
- **Root cause:** the test replaced `mcp_runner.run_with_tools` with a fake
  returning a `render_document` call but nothing dispatched it - the real loop
  is what dispatches. It only passed when the harness fallback made a live model
  call that happened to return blocks.
- **Fix (2026-10-18):** the fake now performs one faithful hop: `prepare_fn`,
  renderer, `on_outcome`, then final text. Offline and deterministic (1.3 s vs
  25 s).
- **Status:** **FIXED.**

## BUG-013: "Send it to my Telegram" was scheduled a minute out
- **Severity:** Medium (the message was delayed; "send" means now)
- **Symptom:** asked to send something to Telegram, chat called `schedule_task`
  with `when="in 1 minute"` and the user saw a scheduled job - never an
  immediate send.
- **Root cause:** chat had no immediate-send tool (`send_telegram` is refused
  for chat), only `schedule_task`; the model bridged the gap with the smallest
  delay it knew. The scheduler itself supports "in 1s".
- **Fix (2026-10-18, `agent_server.py`):** `_chat_send_channel_tool`
  (`send_channel_message`): resolves the paired destination via
  `resolve_notify_chat`, posts to the gateway `/v1/channels/{c}/send` right
  away, chunks long text at 3,900 chars on paragraph/sentence boundaries, and
  surfaces pending approvals. The prompt now says: this tool is for NOW,
  `schedule_task` is only for a future time.
- **Verified:** `tests/test_chat_report_full.py` (chunking, no destination,
  unknown channel, pending approval, missing source); live chat
  `ct-live-report` turn 2 - `using send_channel_message`, no `schedule_task`,
  answer mirrored the tool's own "8 messages, 26,550 chars".
- **Status:** **FIXED.**

## BUG-014: Chat summarised the delegated research report, and re-sent it every later turn
- **Severity:** High (the user asked for the report and got a paraphrase)
- **Symptom:** a research delegation from chat answered "Based on my
  research..."; the Research page showed the full report for the same run. The
  stored transcript then re-injected the whole 20k+ chars into the model on
  every later turn (the opposite of a lightweight surface).
- **Root cause:** `_run_handoff` handed the full report to the model as tool
  output and the endpoint answered with the model's text; the thread stored
  that same blob and replayed it.
- **Fix (2026-10-18, `agent_server.py` + `Chat.tsx`):** `answer_full` is popped
  before the tool output is serialised and goes to the caller only; the
  endpoint's answer IS the report verbatim (`kind="report"`, `title`); later
  turns get a name + 240-char opening + `source="<title>"` stub via
  `_chat_history_for_model`, files as filename+handle; the console renders a
  preview card whose dialog opens over the Ctrl+K blurred backdrop, with no
  word-by-word repainting of a finished report.
- **Verified:** `tests/test_chat_report_full.py` (21); live chat
  `ct-live-report`: "research about ev cars and tell me" - `done kind='report'`,
  26,550 chars, zero `delta` frames, ~136s; follow-up "send it to my telegram"
  resolved the report by name, not by body.
- **Status:** **FIXED.**

## BUG-015: Every lightweight chat turn searched the document store up front
- **Severity:** Medium (cost/latency on the lightest surface; unasked-for
  context that made answers drift to the user's uploads)
- **Symptom:** even a plain "hello" chat turn embedded the query, searched the
  documents index and injected a REFERENCE MATERIAL block; the system prompt
  explained a block that often contained nothing useful.
- **Root cause:** `_with_doc_context` was called unconditionally by both chat
  endpoints, from before `search_knowledge` existed as a tool.
- **Fix (2026-10-18, `agent_server.py`):** removed from both chat endpoints
  (the AG-UI path keeps it); `_conversation_doc_ids` still scopes the tool, and
  the prompt now points at `search_knowledge` instead of a pre-injected block.
- **Verified:** `tests/test_chat_report_full.py`
  (`test_one_shot_chat_answers_with_the_report_verbatim` asserts
  `_with_doc_context` is never called); `test_chat_document_context.py` updated
  to assert the prompt no longer claims a REFERENCE MATERIAL block exists.
- **Status:** **FIXED.**

## BUG-016: Telegram files never reached the agent - two `.get` calls on pydantic models
- **Severity:** High (a core channel feature was silently dead)
- **Symptom:** files sent to the bot on Telegram produced no Documents entry
  and the agent answered as if nothing had been sent; asking for a summary of
  a file it "received" earlier went nowhere. Two agent-forward errors sat in
  the ledger at the exact second the files were sent.
- **Root cause:** `ChannelMessage.attachments` holds pydantic `Attachment`
  models, and both `channels_api._forward_to_agent` (`a.get(k)`) and
  `telegram_poller._absorb_files` (`(att or {}).get(...)`) treated them as
  dicts. The AttributeError aborted the forward before HTTP (`'Attachment'
  object has no attribute 'get'` ×2 in the gateway ledger) and killed the
  download thread, so nothing was ever stored either.
- **Fix (2026-10-19, `channels_api.py` + `telegram_poller.py`):** one
  `_attachment_refs()` helper converts via `model_dump()`; the poller does the
  same. The agent now receives `{kind, name, mime, size, file_id}` metadata
  and the file lands in the Documents store.
- **Verified:** `tests/test_telegram_poller.py::test_a_real_attachment_model_is_stored_not_crashed`,
  `::test_forward_sends_attachment_metadata_not_a_crash`; full gateway suite
  1174 passed. Live: needs one file re-sent on Telegram.
- **Status:** **FIXED.**

## BUG-017: A Telegram-initiated report arrived as one truncated message
- **Severity:** High (the user asked for the report and got its first page)
- **Symptom:** asking on Telegram "research about evs and send me the report"
  delivered one message containing the first 3500 characters; the rest never
  arrived. The same report sent from the console chat arrived chunked.
- **Root cause:** `channel_chat._run_turn` sent `answer[:_MAX_REPLY]`
  (3500) as a single message; the console path chunks at 3900
  (`agent_server._channel_chunks`) because Telegram's hard cap is 4096.
- **Fix (2026-10-19, `channel_chat.py`):** `_chunks()` splits on
  paragraph/sentence boundaries at `_MAX_REPLY` and every chunk is sent in
  order; media rides the first chunk only; a failed chunk reports
  "after N of M messages".
- **Verified:** `tests/test_channel_chat.py::{test_a_long_channel_answer_arrives_as_several_messages,
  test_media_rides_the_first_chunk_only, test_a_failed_chunk_says_how_much_arrived,
  test_chunks_keep_every_character_and_respect_the_limit}`.
- **Status:** **FIXED.**

## BUG-018: "Can you summarise the report?" answered "it is already above" - four times
- **Severity:** High (refusal loop on a plain follow-up)
- **Symptom (live, thread `telegram-069246b4b2a2`):** after a delegated
  research report was displayed, "what is this report about?" / "can you
  summarise it" / "yes, i understand that the report is full, but can you
  summarise it?" all got variants of "The report is already available in
  full above". The model had no way to read the body.
- **Root cause:** `_chat_history_for_model` replaces the report body with a
  240-char stub (correct, BUG-014) and named only send/save tools - nothing
  could return the text, so summarising was impossible by construction.
- **Fix (2026-10-19, `agent_server.py`):** new chat-local tool `read_report`
  returns the full text on demand; the history stub now says to call it
  before summarising or answering questions about the report.
- **Verified:** `tests/test_chat_report_full.py`
  (`test_read_report_returns_the_whole_body`, `..._defaults_to_the_newest`,
  `..._names_one_it_cannot_find`,
  `test_the_stub_tells_the_model_to_read_before_summarising`,
  `test_read_report_is_offered_to_chat`); live on the recorded thread:
  "can you summarise the report?" returned a 1,690-char executive summary
  whose figures ("23 million units", Europe +27%) appear only in the report
  body, in 17.4s.
- **Status:** **FIXED.**

## BUG-019: A no-tools skill with a non-JSON reply crashed with `UnboundLocalError: render_errors`
- **Severity:** Medium (latent crash masking a real, recorded failure)
- **Symptom:** found while writing the recovery-splice tests: any planner or
  formatter reply that failed to parse raised `UnboundLocalError: cannot
  access local variable 'render_errors'` instead of the intended
  "returned no usable JSON" failure.
- **Root cause:** `produced`/`render_errors` were initialised only inside
  `if tools:` in `skills.run_skill`, but the unparseable-reply path at
  `skills.py:3518` reads them for every skill.
- **Fix (2026-10-19, `skills.py`):** both are initialised before the branch.
- **Verified:** `tests/test_authoring_pipeline.py::test_a_no_tools_skill_with_prose_fails_cleanly_not_with_unbound_local`;
  full agent suite 1623 passed.
- **Status:** **FIXED.**

## BUG-020: Lightweight chat sends ~4.5-5.7k tokens on every turn, "hi" included
- **Severity:** Medium (cost/UX; the "lightest" surface carries the heaviest fixed prompt)
- **Symptom:** the user asked why "hi" billed ~4500 input tokens, suspecting
  up-front memory/document chunks. Ledger (gateway_v8.db): the first turns of
  `ct-7c6f06ea` and `telegram-069246b4b2a2` are 4494-5742 input tokens on
  every model (Kilo/Cohere was just the router's pick, not the cause).
- **Root cause (measured, not chunks):** the fixed payload: 24 tool schemas
  = 14,509 chars (~3.6k tokens; `schedule_task` alone is 3,252), system
  prompt 6,189 chars (~1.5k), profile block 1,525 chars (~0.4k). Memory hits
  for "hi" measured 0 chars - documents are no longer injected (BUG-015).
- **Status:** **OPEN** - reported with numbers; no code change yet (trimming
  tool descriptions is a product trade-off). Deliverable in this round was
  the measurement, not a fix.

## BUG-021: "Send the PDFs to my telegram" produced words, never the file
- **Severity:** High (the one thing the user asked for was the file)
- **Symptom:** "send the pdf to my telegram" from the console chat answered as
  if it had sent something; no attachment ever arrived. The tool had no way
  to attach a rendered document - only report text and plain messages.
- **Root cause:** `send_channel_message` accepted only `channel` / `message`
  / `source`; the gateway and Telegram adaptor had carried `media`
  (`sendDocument`) all along, and `notify.send_channel_message` already
  forwarded it - the chat tool never sent the key.
- **Fix (2026-10-19, `agent_server.py`):** new `file` argument resolves a
  rendered file from the thread (`_chat_resolve_file`), reads the artifact
  bytes, and posts `media:[{data_b64, filename, caption}]`; caption caps at
  1000 chars; unknown name gets an explicit refusal; a stale `source` never
  turns the attachment back into chunked text.
- **Verified:** `tests/test_chat_report_full.py` file-send section
  (attachment payload bytes, caption, missing-file refusal, no-destination
  refusal, `file` beats `source`).
- **Status:** **FIXED.**

## BUG-022: "remind me at 9am" was silently rescheduled to "+1h"
- **Severity:** High (a reminder that fires at the wrong time is worse than one
  that refuses; the user asked for 9:00 and got 10:09)
- **Symptom:** "send a calendar reminder on 9am today for a meeting" answered
  success; the armed task read `when="in 1h"` in `state/schedules.json`
  (`sch-f43fca30`), firing roughly an hour after the request instead of 9:00.
- **Root cause:** `scheduler.schedule` accepted only `in N…`, `tomorrow HH:MM`,
  `daily@HH:MM`, `every N…`, ISO and epoch strings. "9am today"/"9am" were
  unparseable; the generic error's first suggestion was a relative delay, and
  the model, retrying, silently rewrote the named time as "in 1h".
- **Fix (2026-10-19, `scheduler.py` + `mcp_server.py`):** clock forms now
  parse: `9am`, `9:30`, `14:45`, `2:30pm`, `at 10:15`, and `today`/`tomorrow`
  prefixes or suffixes (`today 9am`, `9am today`, `tomorrow 9am`, `tomorrow
  14:30`). A named time that already passed today refuses via `_BadWhen` with
  a message that explicitly says it must NOT be turned into a relative delay
  and names `tomorrow <time>` as the alternative; the generic error message no
  longer leads with a relative delay. `schedule_task`'s docstring now tells
  the model the same rule.
- **Verified:** `tests/test_scheduler.py::TestClockTimes` (5 tests, 20 passed
  in the file) and live API: `when:"6am"` → 400 with the refusal text;
  `when:"9am"` → `next_fire` exactly 2026-10-19 09:00 local; `"tomorrow 9am"`
  → accepted (all proof tasks deleted after the run).
- **Status:** **FIXED.** (The stale `sch-f43fca30` stays until the user
  deletes it in Scheduler - it carries the user's own words.)

## BUG-023: Accepting an agent edit never refreshed the file (403, swallowed)
- **Severity:** High (the headline "I have to refresh the page to see what the
  agent did")
- **Symptom:** after accepting a write in Code, the open pane kept the old
  text; the network log showed `GET /api/code/file?path=bye.py` → 403 (agent
  -relative path; the route expects `code_workspace/bye.py`), and the failure
  was caught and dropped, so nothing told the user.
- **Root cause:** `decideEdit` used `pendingEditPath.current`, set from the
  permission frame's `args.path` - the tool-side relative path, not the
  client-visible one.
- **Fix (2026-10-19, `agent_server.py` + `Code.tsx`/`api.ts`):** the
  permission frame now carries `path` computed by `_code_client_path()`
  (tool path → `<root>/…`, inverse of `_code_agent_rel`), forwarded on SSE;
  the client pairs permission → tool_result by tool name, and when a write
  LANDS, reloads the open pane (or shows the stale banner if the draft is
  dirty, never clobbering), refreshes the explorer, and paints the change
  marks.
- **Verified:** Playwright proof runs with 0 `/api/code/file` failures and the
  pane updating itself (`micro_proof2`: auto-synced true); screenshots
  `9-marks-and-synced-pane.png`, `12-final-sync-and-marks.png`.
- **Status:** **FIXED.**

## BUG-024: Agent-created files stayed invisible in the explorer
- **Severity:** Medium (work looked lost; the user had to collapse and
  re-expand the folder)
- **Symptom:** after the agent created or edited a file, the explorer tree
  still showed the old listing - including folders stuck on "empty" - until a
  manual toggle or a page reload.
- **Root cause:** the tree only refreshed on roots/known changes; nothing
  watched the tool stream, and the write ran in the same turn as the answer.
- **Fix (2026-10-19, `Code.tsx`):** `WRITE_TOOLS` detection on successful
  `tool_result` frames triggers `refreshFromDisk()` (clears `known`, bumps a
  workspace epoch), and the Explorer watches `reloadAt` to refresh roots and
  re-read open folders in place.
- **Verified:** `proof_chat2.cjs` STEP 4 (bye.py appeared with no manual
  action) and `proof3b/proof3c` (NOTES2.md/NOTES3.md appear and open).
- **Status:** **FIXED.**

## BUG-025: The model picker's overflow button read a bare "+325"
- **Severity:** Low (a number with no noun; it looks like a stray token)
- **Symptom:** the Code agent's model picker showed `+325` as if it were a
  model count with no context.
- **Root cause:** `ModelPicker.tsx` rendered `+{paid.length - shownPaid.length}`
  with no label.
- **Fix (2026-10-19, `ModelPicker.tsx`):** renders `+{n} more`.
- **Status:** **FIXED.**

## BUG-026: The e2e spec pinned a string no page ever showed; empty state lied
- **Severity:** Low (test theatre + a false claim in copy)
- **Symptom:** `e2e/console.spec.ts` pinned `/code` on "read-only source",
  which existed nowhere in the app; the Agent panel's empty state said
  "searches the repo" although the shipped workspace is a blank scratch dir.
- **Root cause:** the marker survived a Code rewrite; the copy was written
  when the repo itself was the default workspace.
- **Fix (2026-10-19, `e2e/console.spec.ts` + `AgentPanel.tsx`):** markers are
  now `['Explorer', 'Pick a file to start editing', 'Problems']` (verified
  against the live page); the empty state reads "Ask about this file or your
  workspace. The agent reads your buffer and explores the workspace with its
  own tools - every step it takes is shown."
- **Status:** **FIXED.** (Running the e2e suite locally still needs
  `npx playwright install` - the headless shell it pins is absent.)

## BUG-027: Revert answered 500 and lost the only snapshot (NameError)
- **Severity:** High (the Cursor-style safety net failed exactly when used)
- **Symptom:** after an auto-applied write, POST `/api/code/agent/revert`
  returned 500 and the file stayed edited; the review bar stayed up. Worse,
  the endpoint had already popped the snapshot before the crash, so the
  original bytes were gone from the store and a retry returned 404
  "no unreviewed change for this path" - the review could never be completed.
- **Root cause:** `code_agent_revert` used `_aio.to_thread(...)` without the
  `import asyncio as _aio` its sibling handlers each declare locally, so the
  call raised `NameError` after `_CODE_REVIEW.pop`. The pop-then-crash
  ordering turned a handler bug into data loss.
- **Fix (2026-10-19, `agent_server.py`):** the import is present, and the
  restore is wrapped so ANY exception re-inserts the snapshot before the 500
  - a failed revert is now retryable instead of final. Covered by
  `tests/test_code_agent_review.py`.
- **Status:** **FIXED.** Live proof: POST revert -> `{ok: true, restored: true}`,
  disk back to the pre-agent bytes; Playwright keep/revert/reload flow green.

## BUG-028: `run_checks` refused the file it had just been asked to verify
- **Severity:** Medium (burned a whole live turn; the model looped)
- **Symptom:** after editing `sort_example.py`, the model called
  `run_checks("sort_example.py")`, got `'sort_example.py' is not a directory`,
  retried the identical call, and the repeat guard killed the run.
- **Root cause:** the tool treated every `path` argument as a project root.
  A file is a perfectly natural thing to name after an edit.
- **Fix (2026-10-19, `mcp_server.py`):** a file path now runs the checks in
  its parent directory; a missing path still returns the honest error.
  Covered by `test_run_checks_accepts_the_file_the_agent_just_edited`.
- **Status:** **FIXED.**

## BUG-029: The change bar counted a red deletion the editor never painted
- **Severity:** Low (a count that contradicted the screen)
- **Symptom:** a one-line replacement showed `−1` and `~1` together; the
  editor only painted the yellow line.
- **Root cause:** the bar summed `removed` (every `-` line, including
  replacement old lines) while the red point is only drawn for deletions with
  nothing put back.
- **Fix (2026-10-19, `diffMarks.ts` + `Code.tsx`):** the parser reports
  `deleted` (the subset behind red points) and the bar uses it; the bar's
  legend was replaced by self-describing counts (`+N added`, `~N edited`,
  `−N deleted`) that no longer wrap. `diffMarks.test.ts` pins the split.
- **Status:** **FIXED.**

## BUG-030: The Code layout clipped controls below 1280px and parked two always-open docks under the editor
- **Severity:** Medium (a disabled-looking Send button partly off-screen; the
  editor itself narrower than the agent panel on a 1024 laptop)
- **Symptom:** at a 1024×768 viewport the document scrolled 11px sideways; the
  agent composer's Send button sat at right=1035 against a 1024 window, the
  breadcrumb's language label was clipped behind the Definition/References/Save
  buttons, the Problems panel alone took 307px of height (its three-line
  apology showing permanently, above a second Terminal dock header), and the
  agent pane was 386px against a 370px editor.
- **Root cause:** the agent pane was `flex-1 lg:max-w-[520px]` — it absorbed
  spare width instead of yielding it — and neither it nor the composer toolbar
  had `min-w-0`, so their min-content width pushed the layout past the
  viewport. Both bottom docks rendered expanded unconditionally. The status bar
  was `flex-wrap`.
- **Fix (2026-10-19, `Code.tsx` + `AgentPanel.tsx` + `TerminalDock.tsx`):**
  one collapsible Problems/Terminal panel (28px strip, bodies as tabs);
  fixed agent widths (`lg:340 / xl:400 / 2xl:440`), `min-w-0` on the pane and
  composer, the agent starts collapsed below 1280 (top-bar toggle, kept
  mounted so a draft survives), minimap off below 1280, status bar single-line
  with low-priority items hidden at narrow widths. Re-probed: docW == winW at
  1024/1280/1500 with the agent open, no element past the right edge.
- **Status:** **FIXED.**

## BUG-031: The Monaco editor still wore the dashboard palette inside the graphite IDE chrome
- **Severity:** Medium (the text surface — the one thing this section exists for —
  read as a different application than its own chrome; the user flagged "something
  feels off" about colour/chrome)
- **Symptom:** the editor surface was navy `#0a0c13` against `#181818`/`#1f1f1f`
  graphite chrome, cursor violet `#8b7cf6`, selection violet-tinted, tokens pastel;
  the minimap inherited the navy editor background and rendered as a black column
  down the editor's right edge. Violet utilities also leaked back in via classes the
  `.ide` repoint block had missed (suggestion-chip hover borders, composer focus
  ring, user-bubble fills).
- **Root cause:** `ARIA_THEME` in `MonacoEditor.tsx` predated the graphite repaint
  of `.ide` and still carried the dashboard values; the `.ide` repoint block was
  also incomplete, so a handful of violet utilities were never overridden.
- **Fix (2026-10-19, `MonacoEditor.tsx` + `index.css`):** rewrote the theme to
  VS Code Dark Modern — editor `#1f1f1f` sitting ABOVE the `#181818` chrome, cursor
  `#aeafad`, selection `#264f78`, real token colours (comment `6a9955` italic,
  keyword `569cd6`, string `ce9178`, number `b5cea8`, type `4ec9b0`),
  `minimap.background: #1f1f1f`, scrollbar sliders, indent guides and a
  `#04395e` suggest-selection; extended the `.ide` violet-repoint block to every
  violet utility still in use. Pixel-probed the rendered PNG: editor and minimap
  both `#1f1f1f`, no black column; live hover probe: chip hover border
  `rgb(55,148,255)`. Status bar items were also re-ordered VS Code-style and the
  explorer's unlabeled "{n} open" footer removed — see CONSOLE_TEST_FINDINGS.md
  Round 6 addendum.
- **Status:** **FIXED.**

## BUG-032: The no-file Code state sat on the app's near-black void
- **Severity:** Medium (the section's default first impression — open Code with
  nothing selected — was a `#090909` slab flanked by `#181818` chrome, reading
  as an unpainted placeholder)
- **Symptom:** with no file open the editor column showed a large near-black
  rectangle between the tab strip and the panel strip, the empty state's text
  centered on it. Opening any file made it disappear (Monaco painted `#1f1f1f`
  over the column).
- **Root cause:** the empty-state wrapper and every ancestor up to `.ide` were
  transparent, so the app background (`--color-void: #090909`) showed through
  in the one place the editor surface would be.
- **Fix (2026-10-19, `Code.tsx`):** the no-pane wrapper carries `bg-surface-1`
  (`#1f1f1f` inside `.ide`) — the exact surface the editor paints once a file
  opens. Verified with a no-file probe: computed `rgb(31,31,31)`, PNG pixels
  `#1f1f1f` across the whole column, and the opened Problems body stays on the
  panel's `#181818`. Process note: earlier proof screenshots always had a file
  open, which is why this survived three polish passes — the empty state is
  part of the standing proof set now.
- **Status:** **FIXED.**

## BUG-033: Editor text bled faintly through the minimap's empty area
- **Severity:** Low (subtle rendering artifact in the editor surface; visible
  when a long line extends under the minimap and the thumbnails don't reach it)
- **Symptom:** with `bubble_sort.py` open, the tail of line 39 ("…already
  sorted.") showed as faint gray ghost text inside the minimap column, below
  the thumbnails. Persistent across frames and after scroll; vanished when the
  minimap was hidden in the DOM.
- **Root cause:** Monaco's stock CSS fades its canvas
  (`.monaco-editor .minimap canvas { opacity: .9 }`) and expects the `.minimap`
  container to paint the editor surface, as VS Code's background layer does.
  Ours was `rgba(0,0,0,0)`, so 10% of the text extending under the minimap
  blended through: `#ce9178` at 10% over `#1f1f1f` ≈ `#302a28`.
- **Fix (2026-10-19, `index.css`):** `.ide .monaco-editor .minimap` gets
  `background-color: var(--color-surface-1)`, so the 10% translucency blends
  into the same surface the theme assumes.
- **Verified:** live repro screenshot of the region is uniformly `(31,31,31)` —
  0 warm pixels and 0 non-`#1f1f1f` pixels in a 74×26 sample; full page
  re-check clean (bundle `index-DSIyCnOc.js`, 0 console errors).
- **Status:** **FIXED.**

## BUG-034: check_keys.py crashed on a cp1252 console before finishing its report
- **Severity:** Low (developer tool; the documented command failed partway)
- **Symptom:** `uv run python check_keys.py` printed the main key list, then
  died with `UnicodeEncodeError: 'charmap' codec …` on the `── key pools ──`
  banner — the pools, channels, integrations and voice sections never printed.
- **Root cause:** Windows piped stdout is encoded with the locale codepage
  (cp1252); the box-drawing characters are unencodable there.
- **Fix (2026-10-19, `llm_gatewayV9/check_keys.py`):**
  `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` at the top, so
  the report always completes.
- **Status:** **FIXED.**

## BUG-035: The code agent could not create folders ("does not have tools to create new directories")
- **Severity:** Medium (a first-run, core ask — "set up a project structure" —
  failed; the agent improvised with a placeholder `.gitkeep` instead)
- **Symptom:** asked to create a folder, the agent said it had no tool for
  directories. `create_file` then died with
  `ValueError: Parent directory of '<path>' does not exist`, so even the
  workaround (write a file inside the folder) was impossible: nothing could be
  created outside the workspace root, and no `make_dir` existed in any catalog.
- **Root cause:** the MCP server shipped 38 tools and none created a
  directory; `create_file` deliberately refused missing parents
  (`mcp_server.py` ~1833) and the code-agent allowlist, `_TOOL_CATALOG` and
  prompts all listed write tools without any folder concept.
- **Fix (2026-10-20):**
  - `mcp_server.py`: new `make_dir(path)` tool (`mkdir(parents=True,
    exist_ok=True)`, returns `{ok, path, created}`; idempotent) — docstring now
    39 tools; `create_file` auto-creates missing parents.
  - `skills.py`: `make_dir` schema in `_TOOL_CATALOG` (keeps
    `test_every_write_tool_is_gated` green); `create_file` description updated.
  - `agent_server.py`: `make_dir` added to `CODE_AGENT_TOOLS` and
    `_CODE_WRITE_TOOLS` (so it is permission-gated and snapshotted), write-diff
    summary `create folder <path>`, and revert now removes a folder the agent
    created when empty (non-empty: refused with "the folder is not empty —
    revert its files first"); prompts mention `make_dir`.
  - Tests: `test_make_dir_nested_and_idempotent`,
    `test_create_file_makes_missing_parents`,
    `test_revert_removes_an_empty_folder_the_agent_created`,
    `test_revert_refuses_a_non_empty_created_folder`.
- **Verified:** targeted pytest 77/77; live E2E — agent called `make_dir
  demo_dir_alpha`, the write frame read "create folder demo_dir_alpha", the
  folder appeared on disk, and a live revert removed it
  (`{"ok":true,"deleted":true}`); folder gone.
- **Status:** **FIXED.**

## BUG-036: Stop did not stop the run, and a truncated stream looked finished
- **Severity:** High (the only way to abort an expensive or wrong run didn't work)
- **Symptom:** pressing Stop (or closing the tab) mid-run left the MCP loop
  working; tokens were still spent. A stream that ended without `done` (proxy
  cut, server restart, Stop) rendered exactly like a completed answer.
- **Root cause:** `gen()` started the MCP loop in a detached task tied to a
  queue, and the streaming generator returned on disconnect without cancelling
  either; nothing marked the run as stopped, and `Code.tsx` never required a
  `done` frame.
- **Fix (2026-10-20):** the stream loop is wrapped in `try/finally` that cancels
  the queue reader and the run task, and settles any review token still parked;
  `done` carries `stopped` (and the answer becomes `[stopped: …]` when there was
  none); the console throws "the agent stream ended before the run finished"
  when `done` is missing and shows a red `stopped` step.
- **Verified:** live disconnect while `run_checks` was executing a 120 s
  sleeping test — `pytest.exe` (grandchild of the MCP child) was gone 6 s later,
  `/api/health` stayed ready; 222 targeted tests green.
- **Status:** **FIXED.**

## BUG-037: Failed and no-op agent writes still appeared as reviewable changes
- **Severity:** High (Keep/Revert could act on a file the write never touched)
- **Symptom:** after a refused permission, an `edit_file` that found no match,
  or a write that raised, "Changes to review" still listed the path. Keep then
  looked like it applied a change that was never made; Revert wrote a stale
  snapshot over whatever the user had edited since.
- **Root cause:** the review token was registered before the tool result was
  known and dropped only by an explicit keep/revert; error paths never settled
  it. `pending_reviews` did not exist, so outcomes were not paired with tokens.
- **Fix (2026-10-20):** new `_code_review_settle()` compares the file on disk
  with the token's snapshot and drops the entry when unchanged (which also
  preserves an earlier unreviewed change); `_on_outcome` pops tokens FIFO per
  `(tool, client)`; refusal, cancellation and stream teardown all settle.
- **Verified:** live create → one entry with diff; revert → file deleted and
  list empty; live failed `edit_file` on a missing file → list empty; unit
  tests for failed write, no-op write, settle-keeps-earlier-change,
  settle-keeps-created file.
- **Status:** **FIXED.**

## BUG-038: Code tools could read, search and overwrite secret files
- **Severity:** High (credential exfiltration through the agent's own tools)
- **Symptom:** `.env` was hidden on some paths only; `id_rsa`-style keys and
  `.pem/.key/.p12/.pfx/.keystore/.jks` files were readable, listed, searchable
  and writable. Repo-wide searches enumerated `.env` contents, and symbol tools
  accepted those paths.
- **Root cause:** the deny-list lived in one server helper and only matched
  `.env` names; `code_intel.iter_files` (search, repo scans) had no filter, and
  the write/symbol tools never checked.
- **Fix (2026-10-20):** `code_intel.is_secret_name()` is the single definition
  (`.env` and variants except `.env.example`, private key names, key/cert
  suffixes); `iter_files` skips them; `_refuse_secret()` is applied to the read,
  symbol, preview, create, edit, update, delete and mkdir tools; the API's
  `_is_secret_file` delegates to the same predicate.
- **Verified:** new `TestSecretHygiene` suite — dotenv absent from read/list/
  search, symbol tools refuse, repo scans skip, write tools refuse (24 tests in
  `test_mcp_tools.py`).
- **Status:** **FIXED.**

## BUG-039: `run_checks` could not finish on this repo and leaked provider keys
- **Severity:** Medium (the agent's only verification tool timed out on the
  suites it was written for; checker subprocesses inherited every API key)
- **Symptom:** checks were killed at 120 s while the real suites take 5–6 min;
  `run_checks` also ran without the permission gate applied to writes. The
  subprocess environment carried `ARIA_*`, provider and integration secrets.
- **Root cause:** one shared 120 s `TOOL_CALL_TIMEOUT_S`; `subprocess.run` used
  the inherited environment; run_checks was not in any exec gate.
- **Fix (2026-10-20):** `RUN_CHECKS_TIMEOUT_S = 900`; `_check_env()` drops
  TOKEN/SECRET/PASSWORD/CREDENTIAL/API_KEY/PRIVATE_KEY/AUTH/COOKIE variables
  (and `ARIA_*`) and forces `PYTHONIOENCODING=utf-8`; `_CODE_EXEC_TOOLS`
  gates `run_checks` — auto mode allows it silently, manual mode parks a
  permission frame; the stale `target` claim was removed from its docstring.
- **Verified:** env-scrub unit test; timeout path exercised by the cancel test
  (pytest started and was terminated with the run); 222 targeted tests green.
- **Status:** **FIXED.**

## BUG-040: The loop's own contract was fiction in five places
- **Severity:** High (safety promises and budget controls did not hold)
- **Symptom / root cause:**
  1. `allowed_tools` was only described in the prompt — the model could call
     any of the 55 MCP tools, and a refusal would still have prompted.
  2. `max_hops=1` produced two model calls (check-then-increment); an explicit
     `0` was replaced by the default instead of meaning unlimited.
  3. Nothing bounded message growth; a long run died mid-stream with the
     provider's raw 400.
  4. Any model/gateway error tripped the MCP breaker, refusing later runs even
     though the child was healthy.
  5. `done.tool_calls` read `tool_calls_made`, which no code set — always 0.
- **Fix (2026-10-20):** allowlist enforced in `run_tool_loop._run_one` before
  the gate (tool error, nothing dispatched, no prompt); exact hop loop with
  default 60, clamp 200, explicit 0 unlimited; `_context_chars` +
  `_elide_oldest_tool_output` trim oldest tool output, with a hard stop and a
  usable message only when nothing is left to elide (600 k chars for the code
  agent); breaker trips only when the handshake itself failed; the stream counts
  forwarded `tool_result` frames.
- **Verified:** new tests for allowlist refusal, exact hop cap (3), budget
  hard-stop and elision; a live run reported `tool_calls: 2` with two results.
- **Status:** **FIXED.**

## BUG-041: Keep marks could land on the wrong file after parallel writes
- **Severity:** Medium (visual state lied; Keep could be shown on a file that
  was not written)
- **Symptom:** two gated writes in one run overwrote the single `lastWrite` ref;
  the first `tool_result` painted marks and refreshed the second file's editor,
  and a failed keep still cleared the marks locally.
- **Root cause:** FIFO pairing by a single slot; local bookkeeping committed
  before the server answered.
- **Fix (2026-10-20, `Code.tsx`):** `pendingWrites` queue matched by tool name,
  consumed on success and failure; `keepChange` awaits `codeKeep` and clears
  marks/edits only when it succeeds, flashing the server error otherwise.
- **Verified:** `npx tsc --noEmit` clean, 57/57 vitest, production build, live
  create/revert flow.
- **Status:** **FIXED.**

## BUG-042: The new sandbox executor removed `run_python` and broke sandbox_executor
- **Severity:** High (the Coder -> SandboxExecutor path raised ImportError)
- **Symptom:** `from sandbox import run_python` failed; two comprehensive
  sandbox tests failed and every sandbox_executor run would have returned no
  output.
- **Root cause:** `sandbox.py` was replaced wholesale by the `run_command`
  executor. The old public function, its temp-directory contract and its
  timeout marker were assumed unused; `skills.run_skill` still called them.
- **Fix (2026-10-20):** `run_python` is restored on top of `run()`, keeping the
  old return shape (`exit_code`, capped stdout/stderr, `files_written`,
  `timed_out`, `timeout_s`, deleted `cwd`) and adding the Job Object /
  process-group kill. `[sandbox] killed after Ns wall-clock` is still emitted.
- **Verified:** 14 sandbox tests, 31 targeted sandbox/skills/MCP tests, and the
  full suite (1683 passed, 39 skipped).
- **Status:** **FIXED.**

## BUG-043: The tool-registration test still pinned the pre-`run_command` count
- **Severity:** Low (verification false negative: a correct registry looked
  like a regression)
- **Symptom:** `tests/comprehensive/test_mcp_tools.py` expected 54 tools after
  `make_dir` and `run_command` took the real count to 56.
- **Root cause:** the count is asserted literally in two suites; only
  `test_e2e_phase1_l0.py` was updated when the tools were added.
- **Fix (2026-10-20):** expected count is 56, with the delta named in the
  comment.
- **Verified:** full suite green.
- **Status:** **FIXED.**

## BUG-044: Markdown and HTML code lost their indentation
- **Severity:** Medium (copied code was silently corrupted; Python code raised
  IndentationError)
- **Symptom:** fenced Markdown and HTML `<pre>` blocks came back with leading
  spaces and repeated inner spaces collapsed to one.
- **Root cause:** `parsers._clean()` collapses whitespace and ran over every
  line, including code.
- **Fix (2026-10-20):** `_verbatim()` normalises CRLF and invisible characters
  but preserves spaces and newlines; Markdown fences and HTML `<pre>` use it.
- **Verified:** `test_markdown_code_fence_preserves_indentation_and_inner_spaces`,
  `test_html_pre_preserves_indentation`; parser suite green.
- **Status:** **FIXED.**

## BUG-045: A wide table split by characters lost its header
- **Severity:** Medium (continuation chunks became unlabelled value rows)
- **Symptom:** when one table row was huge, the chunker character-split the
  table and only the first piece carried the header, despite the comment
  claiming every piece repeated it.
- **Fix (2026-10-20):** `chunker` prepends the header to every post-split piece
  that does not already start with it.
- **Verified:** `test_header_is_repeated_when_a_wide_table_splits_by_chars`.
- **Status:** **FIXED.**

## BUG-046: Stored PDFs could not render in the desktop Documents modal
- **Severity:** High (PDF preview was blank; downloaded file was fine)
- **Symptom:** the modal used a blob/iframe browser viewer; Electron's PDF
  plugin did not render the stored file, so the user saw an empty frame.
- **Fix (2026-10-20):** added gateway `/v1/documents/{id}/preview` and agent
  `/api/documents/{id}/preview`; Documents renders pdf/docx/xlsx with
  `DocumentViewer`, and keeps the sandboxed blob iframe for text-like files.
- **Verified:** route tests; live `doc-80c02d11c0` returned 10 PNG pages in
  1,281 ms; modal showed 10 pages (827x1170) with no console errors.
- **Status:** **FIXED.**

## BUG-047: Authoring downloaded full PDFs and loaded previews serially
- **Severity:** Medium (opening a document was slow and wasted bandwidth)
- **Symptom:** the preview effect fetched every artifact's full PDF blob before
  rendering and ran `artifactPreview` calls one after another.
- **Fix (2026-10-20):** parallel `fetch`/`artifactPreview`, no eager full-PDF
  blob state, and no `pdfUrls` object URLs.
- **Verified:** `tsc` and production build clean; first `.docview` live in
  958 ms for a four-page PDF.
- **Status:** **FIXED** (the pypdf metadata call remains on the critical path).

## BUG-048: Skills had no Coding Agent capability pack
- **Severity:** Medium (the new coding tools were live but invisible as a
  bundled capability)
- **Fix (2026-10-20):** added a Coding Agent pack listing all 17 coding tools.
- **Verified:** Skills shows `17/17 tools live`, catalogue 56/56, no console
  errors.
- **Status:** **FIXED.**

## BUG-049: The whole console document scrolled instead of the active view
- **Severity:** Medium (rail/title bar moved; inner panes could not fill the
  window)
- **Fix (2026-10-20):** `html/body/#root` are `overflow:hidden`; the shell is a
  flex column and the active section is the only scrolling box.
- **Verified:** home, Skills, Documents and Authoring at 1440x900 all had
  `scrollHeight == innerHeight`; no page-level scroll.
- **Status:** **FIXED.**

## BUG-050: The desktop window had no way to zoom
- **Severity:** Medium (Ctrl+plus/minus/0 did nothing with the hidden menu)
- **Fix (2026-10-20):** `main.cjs` handles `before-input-event` for
  Ctrl/Cmd + `+`/`=`/`-`/`_`/`0`, clamps -3..5, and exposes the same actions
  in the app menu.
- **Verified:** packaged Electron main inspector: menu click -> level 1,
  factor 1.2; injected Ctrl+= -> level 2; Ctrl+0 -> level 0.
- **Status:** **FIXED.**

## BUG-051: A malformed PDF sat on `pending` forever with no error
- **Severity:** Medium (the document could never be enabled, retried, or
  explained; every Documents refresh silently restarted the doomed parse)
- **Symptom:** `test-document.pdf` (305 B, invalid PDF) stayed `pending` with
  `chunk_count: 0`, `error: ""` and `can_enable: false` for a day, although
  the indexer had already failed its parse.
- **Root cause:** `_reconcile_registry_with_plane()` treated every
  `index_version != INDEX_FORMAT_VERSION` document as stale parser output.
  A document that never reached chunking keeps `index_version == 0`, so each
  `/v1/documents` request reset the terminal `failed` state to `pending` and
  started another parse. The next parse failed again, and the cycle repeated.
- **Fix (2026-10-20):** reconciliation now skips `FAILED` documents; a parse
  failure remains terminal until the user explicitly reindexes.
- **Verified:** live `doc-6d6a61a169` is now `failed` with
  `could not parse the file: Cannot find Root object in pdf`, stable across
  repeated list requests and a manual reindex. New
  `test_a_parse_failure_is_not_reindexed_on_every_request`.
- **Status:** **FIXED.**

## BUG-052: Corrupt vector sidecars silently disabled all vector search
- **Severity:** Critical (semantic recall silently degraded to keyword matching)
- **Symptom:** `gw.out` logged `Index type 0x00000000 ... not recognized`;
  every drawer reported `vector_enabled: true` but loaded an empty index, and
  `memory/stats` exposed items with `embedded > 0` and `index_size: 0`.
- **Root cause:** `index.faiss` and `index_ids.json` were written independently
  and non-atomically. A crash between them, or a zero-filled file, left a valid
  JSON sidecar paired with an unreadable FAISS index; the loader swallowed the
  exception and kept the empty index.
- **Fix (2026-10-20):** both sidecars are written to temp paths and promoted
  with `os.replace`; an unreadable pair is renamed to
  `*.corrupt-<epoch>-<uuid>` and the index is rebuilt from the embeddings
  already stored in `memory.json`. `_reconcile_index()` also rebuilds on any
  id/count mismatch, not only on a missing file.
- **Verified:** `test_memory.py` **32 passed**; live stats after restart show
  every populated drawer with matching item/embedded/index counts (episode
  1278, fact 176/177, audit 3637/3641, document 79, profile 101). Three corrupt
  drawers were quarantined and rebuilt without losing memory text.
- **Status:** **FIXED.**

## BUG-053: The first chat send wiped its own live answer
- **Severity:** Critical (the answer appeared, then the transcript cleared)
- **Symptom:** sending the first message in Chat showed a streaming reply that
  vanished a moment later; the sidebar thread existed but the pane was empty.
- **Root cause:** `send()` minted a conversation id and called `rememberCid()`
  while the first answer was still streaming. That re-ran the restore effect,
  which fetched `/api/chat/threads/<cid>` before the turn was persisted, got an
  empty message list, and called `setMsgs([])`.
- **Fix (2026-10-20):** the restore effect returns early while
  `sendingRef.current` is true (`Chat.tsx`).
- **Verified:** `src/views/Chat.restore.test.ts` pins the guard; `tsc` and
  production build clean; frontend unit suite green. A browser regression is
  still required.
- **Status:** **FIXED.**

## BUG-054: Part volume depended on distance from the world origin
- **Severity:** Critical (an off-origin open mesh reported a fabricated volume)
- **Symptom:** `signed_volume` for a non-watertight part grew with its
  coordinates; a triangle a million millimetres from `(0,0,0)` reported a
  million-scale "volume". A single inverted face touching the origin also had
  zero contribution, so winding damage was invisible there.
- **Root cause:** the divergence-theorem sum used `corner_a` directly, i.e.
  tetrahedra from the world origin, which is only translation-invariant for a
  closed mesh.
- **Fix (2026-10-20):** measure from `Geometry.reference`, the area-weighted
  centroid. Closed-mesh values are unchanged; open meshes are now
  translation-invariant, and every face of a closed mesh contributes.
- **Verified:** new translation and far-away-triangle tests; updated the three
  origin-convention tests. Gateway `parts` suite **223 passed**.
- **Status:** **FIXED.**

## BUG-055: Authoring idempotency keys changed on every retry
- **Severity:** High (a dropped response followed by Retry paid for a second run)
- **Root cause:** `Authoring.start()` built `auth-${Date.now()}-${random}` per
  call. The server only dedupes an identical key while a run is in flight, so
  the key could never match a retry.
- **Fix (2026-10-20):** `briefKey(text)` hashes the brief (FNV-1a) and returns
  `auth:<hash>:<length>`. A double-click or retry joins the in-flight run;
  after `done` releases the claim, a deliberate re-run starts fresh.
- **Verified:** `tsc` and production build clean.
- **Status:** **FIXED.**

## BUG-056: Stopping an authoring run left it live forever in the UI
- **Severity:** High (the row reattached on every visit and never showed the
  files the cancelled run still wrote)
- **Symptom:** Stop said "stopped", but the run remained `live:true` in
  localStorage; reopening Authoring reattached to a dead run. The cancel
  request was fire-and-forget and no `/produced` poll was started.
- **Fix (2026-10-20):** `stop()` keeps the session in the polling set, calls
  `cancelRun`, reports whether the server found the run, and immediately
  refreshes `/produced`. The 4 s poll then settles the row and shows any file
  produced before the cancel boundary.
- **Verified:** `tsc` and production build clean.
- **Status:** **FIXED.**

## BUG-057: Chat faked live streaming by replaying the finished answer
- **Severity:** Critical (users waited with no text, then the whole answer
  appeared at once; tool-loop text was hidden until completion)
- **Root cause:** `chat_simple_stream` called blocking `LLM().chat()`, then
  emitted `_chat_chunks(answer)` in the tail. The tool loop also ran with
  `stream=False`, so only status/log frames were sent while it worked.
- **Fix (2026-10-20):** `_emit()` now turns real `text` callbacks into SSE
  frames, the tool loop runs with `stream=True`, and the no-tool path uses
  `LLM().stream_chat`. The tail emits chunks only when nothing streamed.
- **Verified:** 31 agent API tests, including live-delta ordering and
  tool-loop forwarding; full agent suite **1692 passed**.
- **Status:** **FIXED.**

## BUG-058: Cached graph/live reads hid finished work
- **Severity:** High (a completed run could look empty until a hard reload)
- **Fix (2026-10-20):** `graph`, `runsSummary`, `events`, `documents`,
  `scheduleList`, `trackerApp`, `trackerApps`, `tools` and `sessions` opt out
  of HTTP caching with `{cache:'no-store'}`. `getCache` gained a generation
  counter so an invalidation cannot store an older in-flight response.
- **Verified:** `getCache.test.ts`, `api.cache.test.ts` and `Code.diff.test.ts`
  **16 passed**; `tsc` and production build clean.
- **Status:** **FIXED.** `config` and `templates` remain cacheable by design.

## BUG-059: The authoring prefix imposed a 1,000-word floor
- **Severity:** High (a one-page note was inflated into a long report)
- **Root cause:** `length_target()` judged `Create a document.\n\n...`, so the
  console's routing prefix matched the "produce a document" tier. Explicit
  "one page" / "at most N words" requests also took the floor branch.
- **Fix (2026-10-20):** strip the prefix before judging the user's request;
  explicit counts defer to the ceiling and do not also declare a floor; allow
  format adjectives such as "neat" and "polished".
- **Verified:** 50 length/researcher/section tests, including new
  prefix-does-not-add-a-floor and no-floor-plus-ceiling assertions.
- **Status:** **FIXED.**

## BUG-060: An authoring run could die with the browser tab
- **Severity:** High (a paid run could be lost and its spend unrecorded)
- **Symptom:** closing the tab or pressing Stop right after Submit left the
  run unstarted or uncosted, and the idempotency key stayed claimed for the
  full 600s TTL - so a resubmit of the same brief was swallowed as a
  duplicate of a run that had never really run.
- **Root cause:** `_stream_run` started the worker only AFTER the first frame
  was yielded, and the claim was released by the generator after the `done`
  frame - a path a disconnect never reaches. The cost snapshot and delta were
  also taken in the generator, so a client that left at `started` was never
  billed.
- **Fix (2026-10-21):** the worker thread starts BEFORE the `started` frame.
  It owns the claim, the cost record and the cancel-registry cleanup in its
  own `finally`; the generator releases the claim only when no worker ever
  started. The disconnect branch deliberately does NOT release it: the worker
  is still running, and freeing the key there would let a second identical
  run start and bill.
- **Verified:** `tests/test_idempotency_release.py` (worker-before-frame,
  release on completion) and
  `tests/test_api_validation.py::test_inflight_key_is_released_on_disconnect`;
  agent suite **1698 passed, 40 skipped**.
- **Status:** **FIXED.**

## BUG-061: A second identical submit could start a duplicate run
- **Severity:** High (double billing)
- **Root cause:** the in-flight claim was a check-then-set (not atomic), and
  the dedup stream released the ORIGINAL run's claim when it finished.
- **Fix (2026-10-21):** `_inflight_run_claim()` either claims the key or
  joins the run that already holds it; `_dedup_stream` no longer releases a
  claim it did not take. Authoring derives its key from the brief through
  `briefKey()` (stable FNV-1a), so a retry joins the in-flight run while a
  deliberate re-run after `done` still starts fresh.
- **Verified:** `tests/test_hardening_round.py`,
  `tests/test_research_run_contract.py`.
- **Status:** **FIXED.**

## BUG-062: Stop did nothing when it arrived before the run was registered
- **Severity:** High (the button reported a stop that stopped nothing)
- **Root cause:** the cancel endpoint only knew about runs already in the
  run registry. Authoring and Research register their worker a moment later,
  so a Stop inside that window found nothing and was discarded.
- **Fix (2026-10-21):** `_PENDING_CANCELS` remembers a Stop for a session
  that has no worker yet and `_register_run()` honours it. The endpoint
  returns `status` (`cancelled` | `pending` | `missing`), `found`,
  `cancelled` and the resolved `session_id`; Research mints the conversation
  id before the request and sends both ids.
- **Verified:** `tests/test_cancel.py` (6 passed), including
  `test_stop_landing_before_the_worker_is_remembered`.
- **Status:** **FIXED.**

## BUG-063: Two turns on one session interleaved, and a cancelled turn lost its cost
- **Severity:** High (mixed-up output, unaccounted spend)
- **Root cause:** `chat_simple`, `chat_simple_stream` and the DAG worker each
  ran turns on the same session concurrently, and the cancel path returned
  before the turn's cost was recorded.
- **Fix (2026-10-21):** a per-session turn lock (`_turn_lock`) serialises
  turns; every exit path releases it in a `finally`. Cost is recorded on
  success, failure and cancellation, with a synchronous fallback when a
  disconnect tears down the async path before the record is written.
- **Verified:** `tests/test_idempotency_release.py::test_a_completed_run_releases_its_session_turn_lock`
  and the cancel/chat suites.
- **Status:** **FIXED.**

## BUG-064: "Two paragraphs" was not a limit
- **Severity:** High (a two-paragraph request produced ten pages)
- **Root cause:** the ceiling parser only knew `one paragraph` and numbers
  with `at most`, so "two paragraphs on X" fell through to no ceiling at all.
- **Fix (2026-10-21):** `_explicit_unit_cap()` recognises a number or word
  count (one-ten) with `sentence`/`paragraph`/`bullet`, and `ceiling_for()`
  uses it. Unit counts scale: two paragraphs = 240 words, three sentences =
  90, five bullets = 100.
- **Verified:** `tests/test_length_contract.py`, `tests/test_length_genre_floor.py`
  (**40 passed**).
- **Status:** **FIXED.**

## BUG-065: "A deck" did not pin the deck format
- **Severity:** Medium (the user asked for a deck and got a PDF)
- **Root cause:** the request-shape patterns in `_format_requested_in()` had
  no capture group, so the canonical lookup key was the whole matched phrase
  ("ashortdeck", "slideson") and never matched the format table. Only the
  families that happened to capture a token worked.
- **Fix (2026-10-21):** every family captures the format token; "a short
  deck" and "slides on pricing" now resolve to `pptx`, while a passing
  mention ("compare PDF and DOCX export", "compare deck and PDF export")
  still pins nothing.
- **Verified:** `tests/test_brief_format_is_binding.py` (4 passed).
- **Status:** **FIXED.**

## BUG-066: Go-to-definition returned a path the console could not open
- **Severity:** Medium (the feature was dead)
- **Root cause:** the intel endpoint returned the SERVER's path, while the
  console only opens files under its own workspace root.
- **Fix (2026-10-21):** `code_intel.definition(..., root=...)` measures
  against the requested root; the endpoint maps a local result to the
  client-visible path and a remote result through `_code_client_path`.
- **Verified:** `tests/test_code_workspace_api.py::test_go_to_definition_returns_a_path_the_console_can_open`.
- **Status:** **FIXED.**

## BUG-067: Undo in the code editor left the old buffer in place
- **Severity:** Medium (the next save rewrote the pre-undo text)
- **Root cause:** the history restore callback updated the editor but not the
  body snapshot, so the next save sent the text as it was before the undo.
- **Fix (2026-10-21):** the restore callback sets the body snapshot and
  applies the restored text through the same edit path as any other change.
- **Verified:** `src/views/Code.diff.test.ts` (4 passed) plus the editor flow.
- **Status:** **FIXED.**

## BUG-068: A skills-guard error left the page with no way back
- **Severity:** Medium (the view was unusable)
- **Root cause:** in the guard-error state the pack and tool controls stayed
  disabled, and the counts were rendered as if they were known.
- **Fix (2026-10-21):** the guard-error state turns those controls into an
  explicit "Enable" recovery, and the pack counts read "counts unknown"
  instead of inventing a number.
- **Status:** **FIXED.** (no unit test; the state is exercised in the view)

## BUG-069: The terminal could not be restarted, and the launch token was captured once
- **Severity:** Medium (a dead shell, and a dead token after a restart)
- **Root cause:** the bare terminal dock had no restart affordance, and the
  request headers were built from the meta tag once at module load - so a
  page left open across an agent restart kept sending the old token.
- **Fix (2026-10-21):** the bare dock has a "Restart shell" button, and
  `launchToken()` re-reads the meta tag on every call with the headers using
  it. A page that was open across a restart still needs a reload to learn a
  NEW token - the old one is gone with the old process.
- **Status:** **FIXED** (the reload limitation is inherent to per-launch tokens).

## BUG-070: The command palette swallowed Tab
- **Severity:** Medium (a keyboard trap)
- **Root cause:** the palette's key handler only moved focus when the active
  element was inside the palette, so Tab from the input was swallowed and
  the user could not leave.
- **Fix (2026-10-21):** the handler always prevents the default and moves
  focus explicitly through the palette's focusable nodes.
- **Verified:** `src/components/ui.rail.test.ts` (4 passed).
- **Status:** **FIXED.**

## BUG-071: Every visited view owned `id="main"`
- **Severity:** Medium (invalid HTML; the skip link focused a hidden view)
- **Root cause:** `KeepAlive` leaves every visited view mounted, so each one
  rendered `<main id="main">` - several elements with the same id, and
  `getElementById` returned whichever came first (usually a hidden one).
- **Fix (2026-10-21):** `useMainId()` gives the id to the visible section
  only, and `SkipLink` focuses it directly.
- **Verified:** `tsc` clean, frontend unit tests **59 passed**.
- **Status:** **FIXED.**

## BUG-072: Memory observations were clipped, and half the list was unreachable
- **Severity:** Medium (long text broke the layout; only 50 were fetched)
- **Root cause:** the observation list had no width constraint, and
  `/api/profile` defaulted to 50 observations while the client asked for
  neither a limit nor a larger one.
- **Fix (2026-10-21):** the list wraps and clips (`overflow-x-hidden`,
  `break-words`, `min-w-0 flex-1`); the client requests 200 observations and
  the server default is 200.
- **Status:** **FIXED.**

## BUG-073: Hidden views kept polling the server
- **Severity:** Medium (wasted requests, stale data on return)
- **Root cause:** kept-alive views stayed mounted, so their intervals kept
  firing while another section was on screen.
- **Fix (2026-10-21):** `KeepAlive` exposes `useSectionActive` and
  `useActivePoll`; Apps, Settings, Skills, Scheduler, Ledger, Runs, Research,
  Documents, Mission, Chat, Code and Authoring pause while hidden and
  refresh when they become visible again.
- **Verified:** `tsc` clean, frontend unit tests **59 passed**.
- **Status:** **FIXED.**

## BUG-074: A cancelled node was recorded as a failed node
- **Severity:** Medium (a cancelled run looked like a broken one)
- **Root cause:** `flow.py` coerced every `BaseException` from the fan-out
  batch into a failed `AgentResult`, including `asyncio.CancelledError`, so
  the loop kept dispatching nodes after the run was cancelled.
- **Fix (2026-10-21):** cancellation is re-raised, not coerced; other
  exceptions still fail their own node without taking the batch down.
- **Status:** **FIXED.**

## BUG-075: An invalid solid could out-score a valid one
- **Severity:** Medium (the optimiser could "improve" an inside-out part)
- **Root cause:** the inverted/empty-solid penalty was a flat 1000, which a
  large `target_faces` miss could swamp - the invalid mesh then looked like
  the better candidate.
- **Fix (2026-10-21):** the hard-failure penalty is 1e12, so an inverted or
  empty mesh always loses to a valid one, whatever the triangle budget.
- **Verified:** `llm_gatewayV9/parts/tests/test_optimise.py` (13 passed),
  including `test_an_invalid_solid_loses_even_to_a_wildly_missed_target`.
- **Status:** **FIXED.**

## BUG-076: The tier router spent 1.5-4s per call on a reply it always discarded
- **Severity:** High (every memory operation paid for it; 217 wasted calls in 24h)
- **Symptom:** the gateway's ledger showed 217 `router_memory` calls in a day,
  every one of them an error, averaging 6.4s: `unparseable tier reply: We need
  answer routing classifier`. Each was a small prompt (830 chars) that the
  deterministic token-count rule could have answered instantly.
- **Root cause:** the router pool has exactly one member - the free
  `kilo-auto/free` model - and it does not follow the tier instruction. The
  reply was discarded and the deterministic rule decided anyway, but only
  AFTER the round trip. The parse also took the FIRST tier word in a fixed
  order, so an echoed option list ("TINY, LARGE, or HUGE") read as HUGE.
- **Fix (2026-10-21):** prompts under 512 estimated tokens skip the router
  (the deterministic rule already answers that band); a router that mangles
  three replies is benched for five minutes; the reply must contain exactly
  ONE tier word; the prompt now asks for a single word and gives an example.
- **Verified:** `tests/test_router_backoff.py` - 7 passed, including
  "a small prompt never asks the router" and "a router that cannot follow
  the instruction is benched".
- **Status:** **FIXED.**

## BUG-077: A web search could take 25 seconds
- **Severity:** High (the user's request waited on it)
- **Symptom:** the live ledger showed 93 `integration:websearch` calls
  averaging 9.2s, the worst at 25.5s. The chain walked four sources in
  order, each allowed to burn its own timeout before the next was tried:
  Tavily's own default is 60s, DDG's backend fan-out is unbounded, and
  Marginalia was set to 20s. A source that had just failed was paid for
  again on the very next search.
- **Root cause:** no per-source budget, no whole-chain budget, and the
  `HEALTH_COOLDOWN_S` constant was defined but never used.
- **Fix (2026-10-21):** each source gets 6s (Tavily 60s -> 6s, Brave and
  Marginalia 20s -> 6s, DDG 3 backends -> 2), the whole chain gets 15s, and
  a source that just ERRORED is skipped for 5 minutes - but only until
  every other source has also failed, because a cooldown that can return
  nothing is worse than a slow search. "No hits" is no longer counted as an
  error, so a working source is not cooled down for answering.
- **Verified:** `tests/test_search_chain.py` (14 passed) and
  `integrations/test_integrations.py` (22 passed, now hermetic - the class
  used to read and WRITE the live health and usage files, so a source that
  had errored minutes earlier made the tests fail for no reason). Live
  search: 8 results in 6.5s.
- **Status:** **FIXED.**

## BUG-078: A memory write whose supersede target was missing left an orphan
- **Severity:** High (the store filled with duplicates, and the duplicates
  broke search)
- **Symptom:** the profile drawer held 111 rows for SIX distinct
  observations - 80 copies of "prefers publication dates on every claim",
  every one of them superseding the same vanished record `mem:1`. Worse,
  those duplicates crowded every search out of the results: a nonsense
  query returned five copies of one observation and an EXACT keyword match
  (the deployment probe's written fact) did not come back at all.
- **Root cause:** the gateway linked a superseding record to its target
  AFTER the new record was written. A missing target therefore persisted
  the record and then raised: the client saw a 404 and believed nothing was
  stored, while the drawer kept the orphan. Every repeat sighting wrote
  another one.
- **Fix (2026-10-21):** the target is checked BEFORE the write, so a
  missing one leaves the drawer exactly as it was. The agent's reinforce
  path falls back to storing the claim as a NEW row when the target has
  vanished, instead of dropping it.
- **Verified:** `tests/test_memory.py` (34 passed) including "a missing
  supersede target leaves no orphan" and "a superseding write hides the old
  record". Live repair: the profile drawer is back to 7 distinct
  observations and an exact match comes back first.
- **Status:** **FIXED.**

## BUG-079: A repeat sighting reinforced the wrong observation row
- **Severity:** High (it is what made the duplicates in BUG-078)
- **Symptom:** every repeat sighting of a claim reinforced the same
  unlinkable legacy row instead of the live one, so the claim never
  converged on a single row.
- **Root cause:** the reinforce path took the FIRST match in the list. The
  list is sorted by age, but a legacy record sorts to the front, and a
  legacy record can never be linked - so the gate could never succeed and
  the duplicates multiplied.
- **Fix (2026-10-21):** the newest matching row (by creation time) is the
  one that is reinforced.
- **Verified:** `tests/test_user_observations.py` (58 passed) including
  "a repeat sighting reinforces the newest entry" and "a repeat sighting
  survives a vanished target".
- **Status:** **FIXED.**

## BUG-080: A document that never parses parked on "parsing - 0 chunks"
- **Severity:** Medium (the document was unreachable, and it blocked every
  other upload behind it)
- **Symptom:** a 7.9 MB PDF sat on `parsing - 0 chunks` with no error, no
  timeout and no retry. The indexer serialises documents, so it also
  blocked every other upload.
- **Root cause:** the parse ran with no deadline. A pathological file (a
  broken xref, a zip bomb) can make a parser spin forever, and the worker
  thread stays alive, so the reconcile that resumes an interrupted document
  sees a "live" worker and does nothing.
- **Fix (2026-10-21):** the parse runs on its own daemon thread under a
  5-minute deadline (configurable). On timeout the document is marked
  failed with a reason instead of parking forever.
- **Verified:** `tests/test_documents_registry.py` (32 passed) including
  "a parser that never returns fails the document".
- **Status:** **FIXED.**

## BUG-081: The vision call sent no gateway token, so the whole L3 layer was dead
- **Severity:** High (a third of the computer-use engine never ran)
- **Symptom:** the computer-use skill failed with "vision returned
  unexpected type: NoneType" on every turn that escalated to vision. The
  chat path worked, so it looked like a model problem.
- **Root cause:** `LLM().vision()` posted to the gateway's `/v1/vision` with
  a bare `httpx.post` and NO headers. Every `/v1/*` route has been behind
  `X-Gateway-Token` since the auth work, so the call answered 401 - and
  the engine, which swallows the error and escalates, reported a NoneType
  instead. The same class of bug made `report.py`'s cost summary 401.
- **Fix (2026-10-21):** both calls use the pooled, token-carrying client.
- **Verified:** a live vision call now answers ("Red" for a red pixel).
- **Status:** **FIXED.**

## BUG-082: The vision judge never asked for JSON
- **Severity:** High (the L3 layer failed even with a working vision call)
- **Symptom:** with the token fixed, the vision call still ended in
  "vision returned unexpected type: NoneType" - the model had answered
  perfectly well, in prose.
- **Root cause:** the L2b a11y judge passes the action schema as a strict
  JSON schema; the L3 vision judge passed nothing, so the model answered
  "the click target is the blue button" and `_extract_json` found no JSON.
- **Fix (2026-10-21):** the vision judge sends the same action schema
  (`ACTION_SCHEMA_JSON`), and the gateway's `/v1/vision` returns the parsed
  object.
- **Verified:** a live schema-constrained vision call returns
  `{"verdict": "act", "action": {"type": "click", "x": 100, "y": 100}}`.
- **Status:** **FIXED.**

## BUG-083: Computer-use could not reach its own approval queue
- **Severity:** High ("gated" meant "stuck")
- **Symptom:** the safety gates say a sensitive action "returns a pending
  token; the web UI resolves it (approve/reject) before execution"
  (safety/gates.py). The agent server had the endpoints; the console had
  nothing that could call them, and the agent could not even MINT a pending
  approval: the computer skill runs the desktop cascade, and a GUI click is
  not approval-worthy. A request for a command drove a terminal window
  instead of asking the user.
- **Root cause:** the gated shell - the only path that can mint an
  approval - was reachable only through the `computer_action` MCP tool, and
  no skill route reached it. The "web UI" the gates name did not exist.
- **Fix (2026-10-21):**
  - The `computer` skill takes a GATED ACTION when the node carries
    `metadata.action` (run_command | read_file | write_file | open_app) and
    `metadata.args`; the planner prompt and the skill prompt both describe
    it, and a `pending` status is reported as "waiting for approval", not
    as a failure (a failure would be retried, and every retry mints another
    approval).
  - The console has a **Computer** page: the mode, the pending approvals
    with their exact arguments, approve/reject buttons, the redacted audit
    trail and the recorded trajectories.
- **Verified:** `tests/test_computer_gated_route.py` (5 passed);
  `e2e/computer.spec.ts` (2 passed) in a real browser; live end-to-end:
  the agent asked, the gate minted `cu-...`, the page showed the command
  and its arguments, one click resolved it, and the audit recorded
  `pending-approval` then `approved`. In live mode the approved command
  executed (`returncode 0`, `Python 3.8.2`).
- **Status:** **FIXED.**

## BUG-084: Computer-use was off on every normal launch
- **Severity:** Medium (the feature was unreachable)
- **Symptom:** every computer-use request answered "computer-use is
  disabled. Set COMPUTER_USE_ENABLED=true to opt in" - on a machine where
  the cua-driver daemon was running and the feature was fully built.
- **Root cause:** the gates are OFF unless the host opts in, and the
  launcher never set the flag.
- **Fix (2026-10-21):** `start_aria.ps1` sets `COMPUTER_USE_ENABLED=true`
  and `COMPUTER_USE_MODE=dry-run` (nothing executes; a sensitive action
  still waits for a human). Live mode is one flag away and is deliberately
  not the default: `start_aria.ps1 -ComputerUseMode live`.
- **Status:** **FIXED.**

## BUG-085: The deployment probe never authenticated, so it tested nothing
- **Severity:** Medium (18 tests that looked like a passing check)
- **Symptom:** every probe in `tests/test_deployment.py` skipped with
  "gateway down at http://localhost:8109" - while the gateway was up.
  The presence check itself got a 401 and read as "down".
- **Root cause:** `/v1/*` has required `X-Gateway-Token` since the auth
  work, and the probe sent no header.
- **Fix (2026-10-21):** the probe reads the live token file and sends it
  on every request. The suite now runs 16 of the 18 probes for real; the
  two that skip are Gmail and GitHub, whose credentials are dead (correctly
  skipped, not passed).
- **Bonus:** with the probe live, it found a REAL failure - the memory
  round-trip, where a written fact was not recalled - which turned out to
  be the duplicate observations (BUG-078). It passes now.
- **Status:** **FIXED.**

## BUG-086: The console told a healthy host "AX no tree - shots none"
- **Severity:** High (the page that exists to explain computer-use said it was blind)
- **Symptom:** on a machine where the cua-driver was running, 309 apps were
  listed and the engine could see 16 windows, the Computer page's DRIVER card
  read `AX no tree - shots none` - the exact message for "this host cannot
  see the screen", so the feature looked broken.
- **Root cause:** `daemon.capabilities()` read `windows` off
  `get_desktop_state`, which the current Windows driver answers with display
  metadata plus a screenshot and no window list. `ax_ok` was therefore
  always False, and the screenshot sample was only attempted per-window, so
  it was skipped too. `safety/permissions.py` had always asked the right
  question (`get_accessibility_tree`); the capability probe did not.
- **Fix (2026-10-21):** the probe asks `get_accessibility_tree` for the
  window list, and accepts a desktop screenshot as proof of capture before
  falling back to sampling a window.
- **Verified:** live probe now reports `ax_ok True, screenshot_ok True, 309
  apps, 16 windows`; page shows `AX ok - shots ok`. Tests
  `test_capability_probe_reads_the_ax_tree` and
  `test_capability_probe_still_says_no_tree_when_there_is_none`.
- **Status:** **FIXED.**

## BUG-087: The audit tab rendered the entire, uncapped log
- **Severity:** Medium (6,900 DOM nodes, and the one line worth reading was
  buried)
- **Symptom:** opening "Audit trail" rendered every entry ever written -
  6,897 lines and growing - dominated by desktop clicks, with the approvals
  the user actually came to see scrolled far out of view.
- **Root cause:** `/api/audit` returned `export_audit()` whole (the log is
  append-only by design), and the view mapped all of it.
- **Fix (2026-10-21):** `/api/audit` takes `limit` and returns the newest N
  plus `total`; the page asks for 400 and labels it "newest 400 of N".
  Compliance exports still get the whole log with no limit.
- **Verified:** `tests/test_audit_log.py::TestAuditEndpointLimit` (2 tests);
  browser shows "newest 400 of 6904".
- **Status:** **FIXED.**

## BUG-088: The approval the agent quoted could not be found on the page
- **Severity:** Low (but it is the id the user needs to act on)
- **Symptom:** the agent answered "requires your approval (Approval ID:
  cu-1792545714-1)"; the page listed the action and its arguments but never
  the id, so there was nothing to match the message against.
- **Fix (2026-10-21):** the row shows the id in mono next to the action.
- **Status:** **FIXED.**

## BUG-089: "replay" was offered in dry-run, where it can only fail
- **Severity:** Low (a button whose only outcome is an error)
- **Symptom:** every replay click in dry-run answered 400 "replay executes
  real desktop actions; it requires COMPUTER_USE_MODE=live" - correct on the
  server, but the page kept offering the button.
- **Fix (2026-10-21):** disabled unless the feature is enabled AND the mode
  is live, with the reason in the tooltip.
- **Status:** **FIXED.**

## BUG-090: A recording could be replayed but not read
- **Severity:** Low (the tab was a list of ids)
- **Symptom:** "Recordings" showed `run-... N turns` and a replay button;
  the recorded tool calls - the reason to keep a recording - were
  unreachable, though `GET /api/computer/runs/{id}` existed and the client
  already had `computerRun`.
- **Fix (2026-10-21):** the run id expands to its turns and their calls;
  a recording with none says "This recording holds no tool calls."
- **Verified:** browser - expanding the live run lists its turns.
- **Status:** **FIXED.**

## BUG-091: GUI typing reported success but wrote nothing
- **Severity:** High (the feature's core action silently did nothing)
- **Symptom:** live `drive_app` on Notepad traced `replace_text -> ok` three
  times, then `aborted: loop-guard: repeated action replace_text 3x`; the
  document stayed empty.
- **Cause:** the driver had answered
  `{"status":"refused","refusal":{"code":"snapshot_id_required"}}` and, on
  the plain path, `{"effect":"unverifiable","escalation":{"target":
  "foreground"}}`. The engine treated both as success because
  `daemon.call` returns refusals as ordinary bodies. `replace_text` also
  passed a bare `element_index` with no snapshot, which this driver build
  refuses, and modern Notepad (a XAML host) drops posted WM_CHAR, so only an
  element-targeted UIA write lands.
- **Fix (2026-10-21):** refusals raise (`_call_checked`); `replace_text`
  resolves the editable element (`Document`/`Edit`) and overwrites via UIA
  `set_value` with a fresh `element_token`; `type` retries once with
  `delivery_mode:"foreground"`, then against the editable token;
  `press_key`/`hotkey` honour the same escalation.
- **Verified:** live Notepad run - `success=True`, layer L2b, document value
  `aria computer use ok`; `replace_text -> ok` on turn 0, `goal met` on turn 1.
- **Residual:** Ctrl+S to modern Notepad is still undeliverable by the driver
  (posted keys ignored, foreground unverified). Typing is confirmed; saving
  needs a UIA click on the app's Save control.
- **Status:** **FIXED.**

## BUG-092: A recurring reminder's clock time moves after a restart
- **Severity:** Medium (a 2h reminder silently changes its phase)
- **Symptom:** the water reminder (`sch-14effc0f`, `every 2h`, created
  2026-10-18 00:43:57) fired at 03:02, 05:02, 07:02 after an agent restart
  instead of staying on the original 02:44 / 04:44 grid.
- **Cause:** `_worker_tick` re-armed an interval with
  `_next_fire_from_cron(..., time.time())`. When the agent was down over a
  slot, the overdue fire was re-anchored to the restart time, moving every
  later reminder. Ordinary tick latency accumulated too.
- **Fix (2026-10-23):** `_advance_recurring` advances from the scheduled
  epoch by whole intervals, so the phase is preserved. An interval slot
  that fell due before this process started is skipped rather than delivered
  late. `daily@HH:MM` jobs still fire late because their phase is the clock.
- **Verified:** targeted scheduler suites 84 passed; the live water row was
  re-anchored from 09:02:51 to 08:43:57 (the creation grid).
- **Status:** **FIXED.**

## BUG-093: The researcher was told 8k and silently cut at 32k
- **Severity:** High (a long article arrived mid-sentence; findings were
  written from a partial page without knowing it)
- **Symptom:** `fetch_url` itself returned up to 300k chars, but
  `mcp_runner` clipped every tool message at 32,000 chars. The researcher
  prompt claimed `fetch_url` returned "the first ~8,000 characters", which
  contradicted both layers.
- **Cause:** the 32k clip was a global harness default with no per-skill
  override, and the prompt was pinned to an older 8k fetch cap.
- **Fix (2026-10-23):** `run_tool_loop`/`run_with_tools` accept
  `max_tool_result_chars` (None = 32k default, 0 = no harness clip);
  `Skill` reads it from `agent_config.yaml`; the researcher sets it to 0.
  `fetch_url`'s own 300k runaway guard still applies and marks a cut page
  with `TRUNCATED` in the body. The prompt now states the whole-page
  contract and that guard.
- **Verified:** new `tests/test_tool_result_cap.py` (100k page arrives
  whole with 0, still clipped at 32k with None); prompt-consistency tests
  updated; full agent suite 1723 passed, 39 skipped.
- **Status:** **FIXED.**

## BUG-094: A chat run died whenever its viewer left
- **Severity:** Critical (refresh, navigation, or switching threads lost the
  answer; the user could not tell a stopped run from a detached one)
- **Symptom:** a long lightweight answer vanished if the Chat view unmounted.
  A DAG answer streamed to completion but a refresh before the final frame
  left no assistant row, because the browser wrote the turn on `done`.
  Switching Lightweight/DAG aborted the open stream without telling the
  server, and Stop only aborted the client request.
- **Cause:** `chat_simple_stream` ran the model call inside the SSE generator,
  so Starlette cancellation on disconnect cancelled the turn. DAG persistence
  was client-side only. `stop()` had no server-side effect for lightweight
  runs. `switchMode()` cleared the transcript while the old stream was still
  writing into it.
- **Fix (2026-10-23):** the lightweight turn now runs in a server-owned
  `asyncio.Task` (`_CHAT_RUNS`), with the SSE response as a bounded queue
  viewer. The user row + pending assistant row are written before the
  response starts; the worker settles them itself. `/api/chat/cancel` cancels
  the task. DAG workers settle their own pending row. The console remembers
  one thread per engine, polls a reattached pending run, and treats unmount /
  thread switch / mode switch / New chat as detach, not Stop; Stop calls
  `/api/chat/cancel` and then reloads the settled row.
- **Verified:** disconnect/closed-stream persistence and lightweight cancel
  tests added; DAG closed-stream persistence test added; full agent suite
  **1733 passed, 39 skipped**; frontend **68 passed**; `tsc --noEmit` clean.
  Live probes confirmed: closing the SSE socket still stored the answer;
  `/api/chat/cancel` cancelled and stored `stopped`; a DAG stream closed at
  frame one still settled; browser probes passed refresh, mode-switch and
  Stop without console errors.
- **Status:** **FIXED.**

## Skipped / unexercised
- §3 LIVE actions (Telegram/email/calendar/weather/GitHub) — no credentials.
- §8.3 (malformed skill JSON), §8.4 (MAX_NODES cap) — not exercised.
