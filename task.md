# Task log

Running list of what is done, what is verified, and what is still open.
Updated as work lands. Nothing here is a substitute for the test suite — every
"verified" claim below was checked against a running service or a test.

Services: agent `http://127.0.0.1:8500`, gateway `http://127.0.0.1:8109`.
Restart both after backend edits; verify with `/api/health` and
`/v1/providers`.

---

## DONE and VERIFIED

### Chat over Telegram (inbound)
- `channel_chat.py`: inbound channel message → trust-gated chat turn on
  `/api/chat/simple/stream` → reply on the same channel. One stable
  conversation per `(channel, sender)`, sha1-derived so a restart does not
  hand a returning user an empty history.
- Gateway `telegram_poller.py`: `getUpdates` long-poll, the local-install
  inbound path. Telegram cannot reach `127.0.0.1`, and nothing had ever
  called `setWebhook`, so no message could arrive and nobody could be paired.
- Trust gate: only `owner`/`paired` senders may drive a turn. An unreadable
  pairing store yields `unknown`, which refuses.
- Routing order: an open ack thread owns its own replies; anything else is a
  chat turn.
- Verified live end-to-end: inbound → agent → `delivered=True status=200` on
  Telegram.

### Bugs found and fixed on that path
- `channels_api._agent_token_path()` climbed one directory too far, so the
  agent token was never found and **every** forward was skipped as
  `"no agent token"`. This predated the chat work: a Telegram reply had never
  actually reached the agent.
- The agent writes `agent.token` as plain text; it was being read as JSON, so
  the token parsed as empty.
- `_forward_to_agent` blocked the gateway's event loop while the agent called
  *back* into the same gateway for its trust check. Self-deadlock → trust
  timed out to `unknown` → every message refused as untrusted behind a `200`.
- A transient Telegram `409` stood the poller down permanently. It now only
  stands down when a webhook genuinely owns the queue.
- `ack_bridge`: a live reminder thread swallowed ordinary questions — the
  classifier answered `silent`, so questions vanished with no reply. Added an
  `unrelated` verdict plus a deterministic `looks_like_a_request()` guard.
  Verified live: the same question now answers normally.
- `nudge_worker` charged an **undelivered** nudge against `max_follow_ups`,
  which would end the conversation after one network blip.
- `test_adaptors.py` pairing tests asserted an empty live store; they only
  passed on a machine nobody had paired. Now isolated to `tmp_path`.

### Scheduling + notification
- `schedule_task` carries the full notification contract (11 args) in the
  catalog the chat model actually reads, plus `list_channels` and
  `check_channel`. The catalog previously advertised 3 args, so the model
  genuinely could not be told `channel` existed.
- `check_channel` verifies and, where the platform allows it without a human,
  refreshes a credential. It never pretends to mint a token it cannot.
- Verified live: the model called `list_channels`, then `schedule_task`; the
  pre-warning was `delivered=True status=200` on Telegram.
- The lead notification is armed as a derived job with **no query**, so firing
  it costs no LLM spend.

---

## OPEN

### 1. A2UI: drift and reachability (from the AG-UI/A2UI audit)
Security is sound — no XSS, closed component set, real DoS bounds, validation
on the way out. The problems are drift and reachability.

- [x] `openApp` did not reset `showA2ui`; one board's rows rendered under the
      next board's name (`Apps.tsx:143`).
- [x] A2UI table kept a stale page index after the data changed, so it printed
      "No items" while rows existed (`A2ui.tsx`).
- [x] `{"_new" not in cols[0]}` tested dict KEYS, always True; an app with a
      field named `_new` 502'd the whole board (`apps_a2ui.py:124`).
- [x] `StatusRow`, `Empty` and `Progress` were in the catalog with no renderer,
      so a validated surface containing them rendered nothing.
- [x] `enum:` type strings were never enforced — `Link.target` accepted any
      URL. Harmless only while the client renders a `<span>`.
- [x] Dead conditional `searchable || true` made the sort button's label
      unreachable; `aria-sort` was never set.
- [ ] **Blocker for `/api/a2ui/generate`**: the client implements no data
      binding, and the generation prompt *instructs the model to use bindings*,
      so a compliant generated surface renders `[object Object]`. Needs
      `resolveProp(props, data)`.
- [ ] Nine props are accepted and then dropped (`Row.align`, `Divider.axis`,
      `Text.variant`, `KeyValue.tone`, `Badge.tone`, `Sparkline.max`,
      `Action.variant`, `Link.target`, `Spacer.size`).
- [ ] `POST /api/a2ui/generate` is fully built and tested but **unreachable**,
      while `flags.py` describes it to the user in the Apps flags list. Wire it
      or delete it and the flag.
- [ ] No frontend test asserts every catalog component has a renderer. The
      Python suite has exactly that loop; the client has none, which is how the
      drift survived.
- [ ] `DataList` / `FilterChips` / `StatusRow` have no shape validation.
- [ ] `apps.views.a2ui` flag is ungated, so the most fragile renderer is the
      one with no off switch.

### 2. Rendering quality (research reports, authoring output)
Not yet re-reviewed. Earlier audits flagged:
- [ ] PPTX preview silently drops slides — `llm_gatewayV9/docpreviewimg.py`
      around lines 108-109 and 159.
- [ ] Authoring shows a fabricated receipt when a run is re-opened with zero
      files — `Authoring.tsx:302-312`.

### 3. Chat section: visuals (from the frontend audit)
Root cause was that the chat SSE contract is text-only. Now fixed:
- [x] `done` frame carries `files` (both streaming and one-shot), resolved
      against the artifact store server-side.
- [x] `render_document`'s description listed only plain block types while the
      schema accepted `chart` / `image` / `cover` — the model was told charts
      did not exist.
- [x] `markdown.tsx` had no `![]()` image support; `![a](u)` degraded to a
      stray `!` plus a link.
- [x] `Chat.tsx` discarded a fully streamed answer if the server then emitted
      `error` ("answer delivered but could not be saved").
- [ ] A2UI in Chat: recommend **against**. `MAX_TOTAL_TEXT = 60_000` and
      `MAX_TEXT_LEN = 4000` reject a standard research report outright, and
      markdown already renders prose better than any A2UI component.

### 4. Chat tests: LLM behaviour vs agentic behaviour
129 of 144 were genuine agent behaviour; the LLM is properly stubbed almost
everywhere. The real problem was different and worse.
- [x] `test_chat_simple_regression.py` patched `agent_server._gateway_chat`,
      which **does not exist**. With `raising=False` the real gateway was
      called: 10.5s of live LLM per test, and they would fail on a machine with
      no gateway. Now stubs `gateway.LLM` / `mcp_runner.run_with_tools`.
- [x] `test_chat_simple_tools_disabled_flag_yields_no_tools` captured the tool
      payload and **never asserted on it** — it passed with the flag on or off.
- [x] The whole channel→agent seam had **zero** tests: the SSE parser, the
      trust producer, the delivery-failure paths, and both HTTP routes. Added
      `tests/test_channel_seam.py` (17 tests). Verified by mutation: renaming
      the `delta` frame and flipping the trust fallback both fail the suite.
- [ ] Still untested: `ack_bridge._ask_model` verdict parsing, two real
      threads racing one sender, chat thread-store persistence across a
      restart, and idempotency on `/api/chat/simple/stream`.

---

## GROUND RULES (learned the hard way)

- **Never change a prompt to make behaviour correct.** You asked about this and
  you are right: prompt edits move every conversation, are invisible to tests
  and cannot be verified. They are the last resort. The `_CHAT_SYSTEM` edits in
  this work were documentation of capability that already existed (argument
  names that are in the schema) — and the correct fix for the schema drift was
  in `skills.py`, not the prompt. Prefer code, schema or routing.
- Prefer stubbing the module a function-local import actually resolves to
  (`gateway.LLM`), never a phantom attribute on the caller.
- A test that spends money or needs the network is not a unit test. Assert the
  stub was used, so the phantom-patch failure cannot recur.
- A refusal the user cannot see is indistinguishable from being ignored.
- Never block an event loop on an HTTP call to a service that may call back
  into the same process.
- Acknowledge work only after it is delivered, so a crash retries rather than
  drops.
- Tests must not depend on live state. Anything reading `state/*.json` belongs
  behind `tmp_path`.
- Do not read `.env`. Do not commit secrets, runtime state, models, or
  generated output. `gateway_v8.db` is runtime state and must never be
  committed.

## SUITES

- Agent: `uv run python -m pytest tests/ -q` from `S9SharedCode/code`
  (not bare `uv run pytest`). ~5 min.
- Gateway: `uv run python -m pytest tests/ parts/tests -q` from
  `llm_gatewayV9`. ~1.5 min.
- Frontend: `npx tsc --noEmit` then `npm run build` from
  `console-frontend/`.

Last run: agent 1249 passed / 39 skipped; gateway 956 passed / 18 skipped;
`tsc --noEmit` clean; `npm run build` clean.

## DATE AWARENESS

The model wrote `2025-10-12T09:00:00` for "tomorrow 9am" and nothing caught
it. The cause was not a stale training cutoff: `get_time` was removed on the
assumption that "the model's own clock" covered it (`mcp_server.py`, the
comment is still there). A model has no clock. Its sense of now is its
training data, so it guesses - and a reminder is the worst place for a stale
guess, because the row looks scheduled and never fires.

Checked all 14 prompt-building paths: no date reached the model anywhere.
`flow.py` computes a run start time and uses it only for scratch-file TTL.

Fixed at the two chokepoints, not per prompt file:
- `agent_server._chat_system_now()` - `_CHAT_SYSTEM` plus today's date,
  resolved per call (a constant would freeze at import). Wired at all five
  sites that used the bare constant.
- `skills.render_prompt()` - covers all 15 skills at once, so
  `researcher.md`'s `as_of` and `planner.md`'s recency routing stop being
  guesses.

Also removed `get_time` / `currency_convert` from `prompts/action.md`: they
were still listed there as callable after being deleted, so the action skill
was told to prefer tools that did not exist.

## SCHEDULING

- `scheduler.schedule()` rejects past dates with a message naming the
  accepted forms; the bare error left the model no way to know it was a year
  out.
- `scheduler.set_enabled()` + the `enable_scheduled` tool + `POST
  /api/schedule/{id}/enabled`. "Turn that back on" had no action behind it
  anywhere, so it was answered by scheduling a second copy and leaving the
  original greyed out. A past-dated one-shot refuses rather than re-arming.
- `Scheduler.tsx` renders armed rows first (by due time) and finished ones
  last, behind a FINISHED divider. The backend returns rows in JSON-key
  order and both render sites used that unsorted; the existing `.sort()` fed
  only the stat tiles.
- The disabled row's only action was Delete. It now has Enable.

## TELEGRAM DOCUMENTS

`document`, `photo`, `voice` and friends were dropped by `extra="ignore"` in
the Telegram schema, so a file arrived as an empty message and the agent
told the user it could only read text. `send()` took a `media` argument it
never read, though `Capability(send_media=True)` had claimed otherwise.

- Inbound: schema models the media, `normalize` populates
  `ChannelMessage.attachments`, the poller downloads and registers each file
  in the Documents store on a worker thread (never on the poll loop, which
  would stall the offset for every other message).
- `Attachment` needed a `file_id` field. Without it pydantic dropped the
  identifier at construction and every downstream lookup came back empty -
  the file existed and could never be fetched.
- Outbound: `sendDocument` via a new multipart helper; falls back to
  `sendMessage` when the attachment is unusable, so a bad file still
  delivers the words.
- `memory_api.ingest_document()` is now the single place a document is
  created, shared by the upload route, Telegram and the new tool.
- `save_as_document` MCP tool: the Documents list is a different store from
  the artifact store, so `render_document` output and sandbox files never
  appeared in it and "save that to my docs" had nothing to call.

## USER OBSERVATIONS (behavioural profile)

An evidence-backed record of how the user works. Deliberately NOT a
psychological assessment.

Why not a personality profile: "you have an avoidant attachment style" is
unfalsifiable, there is no reading of the data that makes it true in a way the
user would recognise, and once stored it silently steers every later answer. It
feels insightful and it quietly distorts the relationship. So the vocabulary is
behaviour only — requests, corrections, answer shape, pushback.

The safety property is one function, `_evidence_verifies`: every record
carries a VERBATIM quote from the user, and the quote is checked against the
source text before anything is written. A paraphrase fails and is discarded.
That is the difference between "the user said this" and "a model inferred this
about the user".

- Off by default (`memory.observations`). It is the only path that retains
  conversation text nobody asked to be retained, so it must be turned on
  deliberately.
- Extractor runs at temperature 0.1, not the 1.0 the fact classifier uses.
  A misfiled fact is cheap; a wrong extraction is a durable claim about a
  person, so creativity must not be in the loop.
- `_TRAIT_BANNED` rejects trait, motive and mental-health language at write
  time. Every alternative is a STEM with `\w*` — a trailing `\b` on "anxie"
  matches neither "anxious" nor "anxiety", which is how the first version
  passed every test written against the exact word it was written for. Test a
  guard against variants it was NOT written against.
- Own drawer (`profile`), not the `fact` drawer, so it is listable, wipeable
  and deletable without touching real memories. `runtime` cannot write it:
  tool-outcome capture fires on every skill node.
- `record_observation` / `recall_observations` MCP tools; chat injects a
  separate block that frames entries as hypotheses with the quote attached.
  Never quote the profile back at the user.
- Also fixed while here: `confidence` was declared on MemoryItem and written
  by nothing, so every record in the store claimed certainty. It is now
  threaded API → plane → service, clamped, and surfaced in the UI — but only
  for kinds that actually write it, so records that were never assessed do
  not wear a confident 1.00 badge.

## WHAT LIVE TESTING FOUND (unit tests all passed first)

Every one of these passed the suite and was found by driving the real chat
endpoint. The pattern: **a test that stubs the model cannot see whether the
model will accept the schema, whether two writers collide, or whether a quote
supports the claim it is attached to.**

1. **The extractor never ran at all.** The JSON schema came from a nested
   pydantic model, so it emitted `$defs` + `$ref`; Gemini's strict mode
   accepts neither and requires `required` + `additionalProperties: false`.
   Every extraction died `503 structured output failed validation`, which
   reads like the gateway being down. Plain `json_object` on the identical
   prompt returned perfect JSON. Now a hand-written schema, plus a test that
   pins the PROVIDER's constraints rather than pydantic's opinion.
2. **Two writers.** The background extractor wrote, AND the agent was told to
   call `record_observation` in the same turn. Every correction was stored
   twice, and the tool's 0.8 default confidence made the duplicates
   identifiable. Both now go through `submit_observation`; the tool was
   removed from chat entirely, because the agent sees the same message the
   extractor does and cannot verify a quote is verbatim — it has strictly
   less to contribute.
3. **One message became 13 rows.** No dedup anywhere in the store. Worse,
   lexical dedup does not work here: "prefers very short, preamble-free" and
   "prefers concise responses without introductory text" overlap 0.22.
   Dedup runs on the extractor's topical keywords, with singular/plural
   folded and generic words ("format", "style") excluded so unrelated claims
   sharing only those are not merged.
4. **A real quote about the wrong thing.** "wants detailed decision
   reasoning" was evidenced with "never give me a table when there are only
   two options" — the keywords matched on "options", so every word-overlap
   rule passed it, and dedup then discarded the *correct* observation about
   reasoning in favour of the wrong one. `_evidence_verifies` proves a quote
   is REAL, never that it supports the claim. Added a cheap semantic check.
   The two free guards are layered because neither is sufficient alone: the
   support check waves through "yeah ok" → "the user acknowledges", which
   keyword grounding rejects.
5. **A single message read as a settled preference.** One "I really don't
   need the preamble" was stored at confidence 0.9. The extractor grades how
   well its sentence matched the instructions, not how often the behaviour
   happened. Confidence is now banded by SIGHTINGS (1 → ≤0.5), and the UI and
   the chat block both say "seen once — a single incident, not a standing
   preference" in words, because 0.5 still looks like a number.
6. **Not wired to Research/Authoring.** `/api/chat` is the endpoint those
   pages use and it never called the capture. The DAG also rendered
   observations identically to facts, with no quote and no caveat.
7. **Observations were not embedded.** Missing from `EMBEDDABLE_KINDS`, so the
   profile drawer was keyword-only — the most semantically retrievable thing
   in the store was the one kind that could not be found by meaning.

## GROUND RULES ADDED

- Never use PowerShell `Set-Content` / `Out-File -Encoding utf8` on a source
  file. It round-trips UTF-8 through cp1252: every em-dash and icon becomes
  mojibake and a BOM is prepended. It silently damaged six files at once,
  including UI icons rendering as `â—‰`. Use the edit tool, or write via
  Python with `encoding="utf-8"`.
- After any bulk PowerShell text edit, re-parse and re-scan for mojibake
  before trusting the file.
- An `Attachment`/`ChannelMessage` field that the adaptor populates must
  exist on the model; pydantic drops unknown keys silently.
## SESSION: AUDIT P1/P2 + AUTHORING VERIFIED

### P2 - Research stream crash (CONFIRMED, FIXED)
`UnboundLocalError: cannot access local variable 'disconnected'` aborted the
SSE generator on the first yield, so every Research run returned an empty
stream. Initialised before the loop. Live check: 8 frames
(started/log/meta/done), first frame at 25ms, held open 7.7s.

### P1 - Event loop blocked during a chat turn (CONFIRMED, FIXED)
Measured `/api/health` from a second client while a Research turn ran:
- before: median 19ms but **max 3148ms** (every other request stalled)
- after:  median 14ms, **max 26ms** - a 120x cut in the worst stall.

Sync gateway calls were being made from `async def` handlers:
`_session_cost_breakdown` / `_session_cost_delta` (httpx.Client, 5s
timeout), `_with_chat_memory` (three sequential round-trips, 30-60s
timeouts), `ensure_gateway`, warmup thread joins, and the run-completion
notification. All now go through `asyncio.to_thread` or a daemon thread.

### The trap in fixing it
`import asyncio as _aio` was placed *where the alias was first needed*, below
earlier awaits. `await _aio...` with no binding in scope is a NameError at
RUNTIME, so the module imported fine, the tests that did not run a turn
stayed green, and the chat route went down anyway. It bit three times in one
session. `tests/test_async_blocking.py` now sweeps every module-level
`async def` in `agent_server.py` for the ordering mistake, plus pins the
off-thread calls, so the next handler added cannot reintroduce it.

### Authoring (WORKING, verified end to end)
`POST /api/chat` with a `doc_setup` produced a real artefact:
`Probe.pdf`, 3577 bytes, valid `%PDF-` header and `%%EOF`, parses as 2 pages
under pypdf, served as `application/pdf` with a correct
`filename*=UTF-8''Probe.pdf` disposition. The `done` frame carried
`{"artifact": "art:...", "format": "pdf", "skill": "author"}` and the answer
named the file. Cost: **220s wall clock for a 128-word memo** - correct, but
slow; that is the next thing to profile.

### Verify loop was under-testing the gateway by 344 tests
`AGENTS.md` documented `pytest tests/ -q` for the gateway. But 14 of the 16
adaptor suites live in `adaptors/*/test_adapter.py`, and `policy/` and
`integrations/` sit outside `tests/` too. So the documented command reported
green while never running them:
- `pytest tests/ -q`  -> 755 passed
- `pytest -q` (root)  -> 1099 passed  <- the real number

No files had been deleted; the command was simply scoped wrong. Fixed
`AGENTS.md`. The agent side has no equivalent gap (all 97 test files are
under `tests/`).

### Final state
- Agent: `1264 passed / 39 skipped`
- Gateway: `1099 passed / 18 skipped` (full suite, root invocation)
- Frontend: `tsc --noEmit` clean, `npm run build` clean
- Both services up, agent reports `gateway_up: true`, no new tracebacks.

## SESSION: AUTHORING OUTPUT CAPACITY FIXED

### What the user saw
Two documents from one Authoring run: the first "had no content in it except
the sources at the end", the second had some content. Reading both PDFs
page-by-page confirmed it exactly - `art:67be3c8c02e65018` was 4 pages /
8 words: cover, a literal "0 Placeholder for table of contents", a BLANK page
3, and references.

### Root cause 1 - a guard that did not guard
`render_document` refused only when blocks AND sheets AND title were all
absent. A **title alone** passed, so the renderer wrote cover + TOC + blank +
references and reported `ok: true`. The failing run passed `blocks: 1` - a
single `cover` - so counting blocks would NOT have caught it. The guard now
ignores chrome types (cover/pagebreak/heading) and requires real content
(paragraph/bullets/numbers/table/quote/chart/image/note), with an error that
names what was passed and tells the model to call again. One real block is
still enough - this must not become a new cap.

### Root cause 2 - the author was told to stop
`length_contract` classified "write a report on X" as matching NO tier, so
floor=0, so the prompt said "No length has been imposed... and no more". The
same intent worded "a detailed analysis" hit tier 1 (floor 3000) and
delivered 2,367 words; the report wording delivered 310. The phrase "and no
more" is the defect - it reads as an instruction to stop early. Replaced with
"develop every point you have evidence for". Added a genre tier
(report/analysis/study/paper/essay/survey...) at 1200 and a document-intent
tier at 1000, both below the depth tiers so "detailed analysis" keeps 3000.
An explicit ceiling outranks a genre word: "a TL;DR of this report" must not
get a 1,200-word floor.

### Root cause 3 - the done frame lied about size
`take_produced_files` read bytes/stats from the sidecar and dropped them, so
every file in the SSE `done` frame arrived with no size and no word count -
the exact signal used to spot a stub. Now carried through, with the
unresolvable-handle guard intact.

### Output ceilings raised to the provider maximum
author 16000, deck 16000, formatter 12000, summariser 4000, planner 3000,
retriever 2000 -> all 65536 (researcher/distiller were already there).
A 12-page document is ~5,400 words and each block carries JSON escaping, so
16000 ran out MID-`blocks` - a document that stops halfway rather than one
that fails loudly. Verified live: `POST /v1/chat` accepts max_tokens=65536
(200, gemini-3.5-flash-lite); `router.py` confirms gemini max_ctx 1000000.
Left low on purpose: critic, sandbox_executor, coder, browser, action,
vision_file, computer - tool-call/structured-output skills where a high
ceiling only buys malformed JSON.

### Measured, before -> after
| request | before | after |
|---|---|---|
| "find out about rag and create a neat pdf explaining it" | 2 files, first = 9 words / 0 sections, blank page | **1 file, 376 words, 15 sections, 4,194 chars** |
| "Write a report on Roman aqueducts" | 620 words, 2 sections, 4 pages | **1,488 words, 7 sections, 7 pages, 11,592 chars** |

No stub, no blank page, no "0 Placeholder", one file per request. Latency
163s / 210s (was 118s / 321s).

### Tests
New: `tests/test_render_shell_guard.py` (17), `tests/test_produced_file_stats.py` (4),
`tests/test_length_genre_floor.py`, plus `tests/test_async_blocking.py` from earlier.
Agent: **1300 passed / 39 skipped**. Gateway: **1099 passed / 18 skipped** (root invocation).

### Still open
- `fetch_url` truncates at 20,000 chars MID-SENTENCE with no flag to the
  researcher. Full-text fetch + artifact, and the 4KB structured inline/spill
  for researcher output, are not built yet.
- `MAX_FANOUT = 4` in flow.py caps researcher nodes; the planner's `depth`
  field is emitted and then only logged - nothing scales on it.
- gemini `tpm: 250000` - uncapped fetching with 4 researchers in parallel is
  the thing most likely to hit this. Keep the fan-out bounded when built.

## SESSION: RESEARCH BREADTH, DUPLICATE DOCS, TOKEN DISPLAY

### Why each researcher transferred so little - four separate caps
The user pasted a real researcher output (NW Territory / Indiana statehood)
and asked why it carried so little. Four distinct limits, all real:

1. **`_extract_page` truncated at 20,000 chars MID-SENTENCE.** The marker
   (`…[truncated at 20000 chars]`) is appended to the text but nothing else
   told the researcher, so a long article silently arrived as its opening
   paragraphs. THIS is the biggest single cause. Raised to 300,000 (above any
   real article; runaway guard only), and when it does fire the text now says
   `TRUNCATED` and tells the model to record it in `caveats` rather than
   guess. The stale `max_chars=20000` at the wayback call site is gone too.
2. **The researcher prompt told it to fetch 3-8 URLs.** Now 8-20, with an
   explicit statement that it is one worker among many and the author can
   only write what the fleet gathered. It now also says fetch_url returns the
   COMPLETE page, so read past the fold.
3. **The planner prompt said `researcher x2-4`.** Now x6-12, with the facet
   count named as the biggest lever on document size and the measured numbers
   attached (3 facets -> 13k chars -> 310-word stub; 4 -> 18k -> 2,367 words;
   6 -> 23k).
4. **`_ARTIFACT_SPILL_BYTES` was 24,000**, so almost nothing spilled and every
   node carried all eight researchers' findings inline. Now **4,096**, with
   the preview shortened 1500 -> 800 to match. The old comment argued 4KB was
   "far too low" because findings are ~8KB - that reasoning has it backwards:
   the text is preserved intact in the artifact, `read_artifact` is already in
   the author/deck tool surface, and what changes is tokens per turn.

Also: `_MAX_WORDS_PER_RENDER` 60,000 -> 200,000. 60k was inherited from the
old 16k-output-cap era, so it described a limit that no longer existed.

### MAX_FANOUT was two caps wearing one name
- `MAX_RESEARCH_FACETS` 4 -> **12** (how much evidence a run gathers; the
  planner's decision)
- `MAX_CONCURRENCY` 4 -> **8** (how many calls run per batch; gemini tpm is
  250000, so unbounded batches 429 together)
- `MAX_NODES = 60` unchanged as the runaway backstop.

### Two documents per run - fixed
A second `render_document` call is normally the model re-rendering after
reading `stats.words`. Both files reached `produced_files.json` and the
console, so one request produced two near-identically-named PDFs. Now the
author collapses them to the LAST render (the finished one) and logs what it
dropped.

### Tokens per document in the Authoring UI
Added `flow.session_cost_totals()` and attached the run's spend to every
produced file in the `done` frame, labelled `scope: "run"` (splitting per
document would be a fiction - one shared budget). Authoring.tsx shows
`words · sections · size · tokens · $`.

**Two real bugs found while testing it:**
- `take_produced_files` was reading `input_tokens`/`cost` off the sidecar,
  but the schema is `{"ts","query","per_agent":{skill:{usd,in_tok,out_tok,
  calls}},"totals":{...}}`. It reported **0 tokens for every run**. Now reads
  the real shape, and breaks the figure down by skill.
- `withProduced` in Authoring.tsx dropped `bytes`/`blocks`/`stats` when folding
  the snapshot, and `Produced.files` was typed without them, so the UI could
  never show them even once the server sent them.

### Verified live, after all of it
- **1 file** (not 2), 8 pages, 1,603 words, 7 sections, 14,561 text chars
- 313,020 tokens reported for the run
- Agent **1319 passed / 39 skipped**; gateway **1099 / 18**; tsc + build clean

### Still open
- The intermittent `NameError: name 'title' is not defined` in the author
  node could NOT be reproduced: the loaded function has no bare `title`
  reference and a direct call works. Suspected stale process; unconfirmed.
- `_MAX_WORDS_PER_RENDER` is now 200k, but nothing yet instructs the author
  to aim at 50-100 pages for a long-form document - only the floor contract.
- Documents still land at 5-8 pages; the evidence base (23k chars) is the
  limit, not the renderer.

## SESSION: CODE EDITOR MADE AN AI EDITOR

The Code view was a text editor with syntax colouring. The audit: the Coder
skill could read, search and write files but **could not check anything it
wrote** - `create_file` returned ok:true for a file with a syntax error and
the node reported success.

### Three tools that close the loop (mcp_server, now 49 registered)
- `run_checks` - runs the project's OWN lint/typecheck/tests, detected from
  what the project contains (pyproject/pytest.ini -> pytest, tsconfig.json ->
  tsc, package.json -> npm test, ruff.toml -> ruff, Cargo.toml -> cargo check,
  go.mod -> go vet). Returns the real exit code.
- `git_inspect` - status / diff / log, so the agent can review its own edits.
- `find_symbol` - definitions reported SEPARATELY from usages, with line
  numbers. Regex cannot tell a definition from a call site, a comment or a
  string.

Bug found while testing: the pytest profile was `("-m","pytest",...)` against
`shutil.which("pytest")`, which is the console script - so it ran
`pytest -m pytest`, a MARKER filter. It matched nothing and exited 5, which
reads as "tests failed" rather than "you invoked me wrongly". Fixed.

### Config
`coder.max_tokens` 1500 -> **65536** (a new function with types and error
handling is several thousand tokens; the node was truncated mid-function and
the tail lost silently). `sandbox_executor` 400 -> 4000 (400 truncated stdout
capture and any failure explanation). All three new tools added to the coder.

### coder.md rewritten
States the loop in order - READ, EDIT, `run_checks`, fix, done - and says
plainly that skipping step 3 means shipping code that does not compile with
nothing downstream to catch it. Also: prefer `edit_file` (fails loudly if the
text is absent, so you cannot silently edit nothing), never weaken a test to
make a check pass, match the surrounding dialect.

### HTTP: three read-only routes
- `POST /api/code/ask` - the AI half. Sends the DRAFT (unsaved edits
  included) plus attached files and a bounded project listing, streams SSE
  `status`/`delta`/`done`. Answers; does not edit.
- `POST /api/code/checks` - runs the real checks for the editor.
- `GET  /api/code/git` - status/diff/log for review.

All three are read-only by construction: **no PUT/PATCH/DELETE verb**. A
question typed into a panel must not rewrite files behind the user's back,
and the review route must not be able to commit.

Two live bugs found and fixed while verifying:
1. `LLM.chat` is BLOCKING, not a coroutine. Calling it inline blocked the
   event loop; awaiting it raised "object dict can't be used in 'await'
   expression". It now goes through `to_thread`.
2. `/api/code/git` first delegated to `mcp_server.git_inspect`, which is
   sandbox-relative by design - "." resolved to the sandbox and answered
   "nothing to review" for a workspace that IS a repo. Now runs git against
   `_CODE_ROOT` directly.

### Code.tsx
- "Run checks" button in the Problems header with real per-checker output
  (exit code, stdout/stderr, expandable). Deliberately NOT run on keystroke.
- "Ask" panel: stream, and "Insert at cursor" writes through the same buffer
  setter as typing, so it is visible and undoable.
- status/diff/log review above the answer.

The stale "Run and agent actions stay disabled" line is gone - both exist now.

### Verified
1368 agent tests pass / 39 skipped; gateway 1099 / 18; tsc + build clean.
Live: ask returned a grounded 1,117-char answer about `length_contract`;
checks ran pytest for real; git status/diff/log all returned.

### STILL OPEN
- **`lang.ts` symbols/foldRanges are regex, not AST.** No tree-sitter, so the
  outline and folding are wrong exactly on ambiguous code. Not started.
- **The 3D section does not exist** - no route, no view, no backend, no tools.
  Not started.
- `sandbox_executor` still has no tool surface; unclear what it runs.
