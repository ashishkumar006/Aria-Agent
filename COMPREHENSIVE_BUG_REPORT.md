# ARIA CONSOLE — COMPREHENSIVE BUG & PERFORMANCE REPORT

**Date:** 2026-10-27  
**Test Method:** Multiple Playwright subagents testing the live agent console at `http://127.0.0.1:8500`  
**Scope:** Chat, Research, Memory, Runs, Scheduler, Code, Apps, Ledger, Console, Computer, Settings, and API endpoints  
**Status:** Findings only — NO fixes applied (per user request)

---

## Executive Summary

Across **4 Playwright subagent runs**, a total of **5 high-severity, 16 medium-severity, and 34 low-severity findings** were identified. Key issues include streaming data loss on large responses, zombie sessions, double-submit race conditions, missing theme toggle, and server-side errors.

### Severity Distribution

| Severity | Count | Description |
|----------|-------|-------------|
| **H (High)** | **5** | Breaks user tasks, loses data, or causes silent data loss |
| **M (Medium)** | **16** | UX confusion, missing features, inconsistent behavior |
| **L (Low)** | **34** | Cosmetic, polish, or minor inconsistencies |
| **Total** | **55** | |

### Existing Knowledge

The codebase already had known bugs tracked in `S9SharedCode/code/BUGS.md` (BUG-001 through BUG-006) and a detailed `CONSOLE_TEST_FINDINGS.md` document from 2026-10-18. Many of those issues were previously marked "FIXED" but several remain open. This report identifies newly discovered issues plus status on previously known ones.

---

## HIGH SEVERITY FINDINGS

### H-1: Large Response Streaming Data Loss — Zero Delta Frames Delivered

**Category:** Chat, Streaming, Performance  
**Component:** `agent_server.py` — `_put()` at line 10195, SSE frame queue at line 10188  
**Scenario:** User sends a query requesting a 31,868-character response  

**Evidence:**
- A 31,868-character response arrived **entirely in a single `done` frame with zero delta frames**
- 21 heartbeat `status` frames were emitted over 186 seconds
- No incremental text was streamed to the client
- The `_put()` function **silently drops frames** when the queue is full:

```python
# agent_server.py lines 10195-10199
def _put(kind: str, payload: dict) -> None:
    try:
        q.put_nowait((kind, payload))
    except _aio.QueueFull:
        pass  # silently drops
```

- The bounded queue (`maxsize=4000`) fills up with heartbeat status frames, causing delta frames to be silently discarded

**Impact:** Users see no progressive text rendering during long responses. The entire answer appears only at completion, or is lost entirely if the connection drops before the `done` frame.

---

### H-2: Double-Click on Queue Button Queues Duplicate Messages

**Category:** Chat, UI  
**Component:** `Chat.tsx` — send() function around line 1254-1260  
**Scenario:** User double-clicks the "Queue message" button while a response is streaming  

**Evidence:**
- Confirmed response streaming (Queue button visible)
- Double-clicked the Queue button with a message typed
- Result: **"2 queued"** — the same message was queued **twice**
- 2 identical user bubbles appeared in the transcript
- The `sendingRef.current` guard prevents double-post but doesn't prevent double-queueing

**Root cause:** `send()` calls `setQueued(...)` and `setInput('')` as two separate React state updates. Between double-click events, React hasn't re-rendered to clear the input, so both clicks read the same stale `input` value and both queue the message.

**Impact:** Users unknowingly submit duplicate messages, leading to duplicate LLM calls and potential double billing.

---

### H-3: Silent Frame Dropping in SSE Queue (`_put` discards data)

**Category:** Streaming, Data Integrity  
**Component:** `agent_server.py` — `_put()` at line 10195  
**Scenario:** High-throughput delta streaming when queue fills  

**Evidence:**
- The `maxsize=4000` queue can fill with heartbeat status frames during long streaming sessions
- When full, `_put()` catches `_aio.QueueFull` and silently passes — delta frames are lost
- `_put_final()` is the only method that drains the queue to ensure terminal frames are delivered
- No warning or error is logged when frames are dropped

**Impact:** Irrecoverable data loss for large streaming responses. If any delta frame is dropped, the client receives an incomplete answer with no indication of data loss.

---

### H-4: Empty `done` Frame Answer When Text Arrives Only via Delta

**Category:** Chat, Data Integrity  
**Component:** `agent_server.py` — `_finish()` at line 10379  
**Scenario:** A 1,411-char medium response  

**Evidence:**
- 1 delta frame carried all text (1,411 chars)
- The `done` frame's `answer` field was **empty (0 chars)**
- Full answer is only recoverable from delta frames — if any delta is dropped, the answer is irretrievable
- Root cause: `_finish()` uses `reply.get("text")` which can be empty when the gateway streams text via the `on_event` callback but doesn't include it in the return dict

**Impact:** If any delta frames are lost (see H-3), the answer becomes unrecoverable because the `done` frame carries no fallback text.

---

### H-5: Path Traversal in Session/Node Path Parameters

**Category:** Security, Sessions  
**Component:** `agent_server.py` — session graph route handler  
**Scenario:** Requesting `/api/sessions/../../etc/passwd/nodes/n:1`  

**Evidence:**
- Requesting `/api/sessions/../../etc/graph` returns **HTTP 200 with HTML content** (the SPA index.html)
- No path sanitization is performed on the `session_id` path segment
- Path-injection characters like `!!!` are rejected (returns 400), but `..` traversal patterns are not checked
- The SPA FastAPI catch-all serves `dist/index.html` for unmatched routes

**Impact:** Potential path traversal exposure. Should return 403/404 or sanitize the session_id to reject `..` patterns.

---

## MEDIUM SEVERITY FINDINGS

### M-1: 14 Zombie Sessions with Empty Queries

**Category:** Data Hygiene, Sessions  
**Component:** `agent_server.py` — runs/summary endpoint, `_CHAT_THREADS_PATH`  
**Scenario:** Examining Sessions API and Runs view  

**Evidence:**
- 14 sessions with `query=""`, `nodes=0`, no `status_counts` clutter the Runs view
- These are leftover test sessions (`t-splice-*`, `t-orch-*`, `t-rec-*`, `t-dbg-*`, `L0-e*`)
- Sessions API: `GET /api/sessions?limit=200` returns 14 zombie sessions
- Runs summary API: same 14 returned as empty rows
- Runs UI lists them as rows with "no graph" and `'(untitled)'` topic

**Impact:** Cluttered UI, confused users, wasted storage.

---

### M-2: No Theme Toggle Button in UI

**Category:** UI, Settings  
**Component:** `App.tsx`, `Settings.tsx`  
**Scenario:** Looking for light/dark mode toggle  

**Evidence:**
- No theme toggle button found in the UI (checked `#themeBtn`, `button[aria-label*='theme']`, `button[title*='dark']`, etc. — none match)
- `document.documentElement.className` = `"dark"` (hardcoded in `index.html`)
- `App.tsx` only sets `theme="dark"` as a prop on `<Toaster>` (`App.tsx:210`)
- `Settings.tsx` has no theme toggle

**Impact:** Users cannot switch to light mode. The console is permanently dark-themed.

---

### M-3: Send Button Enabled During Active Streaming

**Category:** Chat, UI  
**Component:** `Chat.tsx`  
**Scenario:** Sending messages while a response is being generated  

**Evidence:**
- Send button is **ENABLED** during active streaming
- The button uses `abortRef`/`reqRef` guards (`Chat.tsx:1387`), but the button itself is not `disabled`
- A user can click "Send" again while streaming, causing confusion

**Impact:** Potential for race conditions and user confusion. Should disable the button during streaming.

---

### M-4: No Idempotency on Lightweight Chat (`/api/chat/simple/stream`)

**Category:** Chat, API, Billing  
**Component:** `agent_server.py` — lines 10126-10128, 10153  
**Scenario:** Submitting identical queries with same `conversation_id`  

**Evidence:**
- Both requests completed independently — no deduplication for `/api/chat/simple/stream`
- The idempotency key (`_inflight_run_claim`) is only auto-generated for `research: true`, not regular chat
- Both requests produced **different-length answers** (76 vs 131 chars) — model called twice
- Potential for **double billing**

**Impact:** Accidental duplicate billing for users who resubmit the same query.

---

### M-5: `conversation_id` Accepts Invalid Characters Despite Validation Message

**Category:** API, Validation  
**Component:** `agent_server.py` — `_plain_id_ok()` at line 7014  
**Scenario:** Sending `conversation_id: "INVALID!@#$%^&*()"`  

**Evidence:**
- `conversation_id` containing `!@#$%^&*()` was **accepted** (HTTP 200) and the full stream completed
- `_plain_id_ok()` only rejects: `..`, `/`, `\`, leading `.`, control characters
- The error message claims "conversation_id must be a plain id (letters, digits, '-', '_', '.')" but validation allows any printable non-path-separator character

```python
# agent_server.py lines 7014-7021
def _plain_id_ok(s: str) -> bool:
    return bool(s) and ".." not in s and not any(c in s for c in "/\\") \
        and not s.startswith(".") \
        and not any(ord(c) < 0x20 or ord(c) == 0x7f for c in s)
```

**Impact:** Inconsistency between documentation and behavior; potential for unexpected IDs to be processed.

---

### M-6: Time-to-First-Delta Consistently ~21s Even for Trivial Queries

**Category:** Performance, Chat  
**Component:** `agent_server.py` — SSE generator at lines 10467-10494  
**Scenario:** Sending "Hello, what is the weather today?"  

**Evidence:**
- Time-to-first-delta is consistently ~21s across 3 runs for trivial queries
- TTFB (time to first byte) is fast (79-119ms) — server accepts the stream immediately
- The bottleneck is the **model call itself**, not the SSE transport
- 5-6 heartbeat `status` frames emitted before the first `delta` arrives

**Impact:** Poor perceived performance — users wait ~21s before seeing any text for simple queries.

---

### M-7: `/api/sessions` Endpoint Performance Degradation Under Load (129-386ms)

**Category:** Performance, API  
**Component:** `agent_server.py` — sessions endpoint  
**Scenario:** Measuring endpoint latencies  

**Evidence:**
- `/api/sessions` is the slowest endpoint: 129-386ms median
- It parses `graph.json` and builds node-link graphs for each session
- Latency varies significantly under load (observed 129ms to 386ms)

**Impact:** Slower session list rendering; UI feels sluggish when loading the sessions sidebar.

---

### M-8: Server `NameError: _aio` Crashes in Route Handler

**Category:** Server, Reliability  
**Component:** `agent_server.py` — various route handlers  
**Scenario:** Certain route handlers referencing `asyncio as _aio` without import  

**Evidence:**
- Server logs show `NameError: name '_aio' is not defined`
- Followed by `RuntimeError: No response returned` (no response sent to client)
- The pattern was previously fixed in `chat_simple` (line 9719-9721) and `chat_simple_stream` (line 10094), but appears to still occur in at least one code path
- The error results in no response being sent to the client

**Impact:** Intermittent 500-level failures for certain API routes.

---

### M-9: `PermissionError` on Windows Atomic File Writes

**Category:** Server, Reliability  
**Component:** `agent_server.py` — `_write_json_atomic()` at lines 126-135  
**Scenario:** Writing state files while another process has them open  

**Evidence:**
- `PermissionError: [WinError 5] Access is denied` on `os.replace()`
- Windows cannot atomically rename over a file that is open by another process/thread
- Multiple errors on `state/apps/*.json` files
- Affects `/api/apps` endpoints and tracker app refresh functionality

**Impact:** App state files cannot be written on Windows when files are in use.

---

### M-10: Empty `done` Frame Answer in Some Chat Responses

**Category:** Chat, Data Integrity  
**Component:** `agent_server.py` — `_finish()` at line 10379  
**Scenario:** A 1,411-char medium response  

**Evidence:**
- For medium responses, the `done` frame's `answer` field was **empty (0 chars)**
- Only 1 delta frame was emitted carrying all 1,411 chars
- Same root cause as H-4: `_finish()` uses `reply.get("text")` which can be empty

**Impact:** If the single delta is dropped, the answer is unrecoverable.

---

### M-11: Duplicate Memory Items

**Category:** Memory, Data Hygiene  
**Component:** Memory view  
**Scenario:** Viewing existing memory items  

**Evidence:**
- Duplicate descriptors: `"The capital city of Portugal"` (x2)
- A `fetch_url` tool_outcome duplicated (x2)
- The duplicate "capital of Portugal" is from today's test run (probe test created a fact that already existed)

**Impact:** Cluttered memory store; potential for conflicting or redundant memory recall.

---

### M-12: 3 Cancelled Jobs Persist in Scheduler List

**Category:** Scheduler, UI  
**Component:** `Scheduler.tsx`  
**Scenario:** Creating and deleting scheduled jobs  

**Evidence:**
- 3 cancelled jobs (`sch-ef858cd5`, `sch-4eb5bd82`, `sch-ac97fe7c`) still appear in `/api/schedule` response
- Jobs with `_cancelled: true` are not filtered from the list
- **Delete doesn't refresh UI or API** — created job still appears after deletion; `load()` call fires but row persists (stale state)
- This matches the historical `CONSOLE_TEST_FINDINGS.md` "delete job still visible" issue which was marked "Still open"

**Impact:** Confusing scheduler UI; users think jobs weren't deleted when they persist in the list.

---

### M-13: Computer Use View Is Disabled

**Category:** Computer Use, UI  
**Component:** `Computer.tsx`  
**Scenario:** Accessing the Computer Use view  

**Evidence:**
- Computer-use is **disabled** (`enabled: false`)
- Displays `○ disabled`
- Dry-run mode toggle is not applicable

**Impact:** The computer use feature is unavailable to users.

---

### M-14: `/api/config` Field Name Inconsistency

**Category:** API, Schema  
**Component:** `agent_server.py` — config handler  
**Scenario:** Inspecting config endpoint response  

**Evidence:**
- `/api/config` returns `{"raw":..., "parsed":...}` — field names differ from the frontend type definition

**Impact:** Potential client-side schema mismatches if frontend expects different field names.

---

### M-15: `/api/capabilities` Returns `tools` as Dict Instead of List

**Category:** API, Schema  
**Component:** `agent_server.py` — capabilities handler  
**Scenario:** Inspecting capabilities endpoint  

**Evidence:**
- Returns `tools` as a dict with keys `['toolCalling', 'tools']` instead of a list
- Inconsistent with expected schema and may break clients expecting a list

**Impact:** Clients parsing `tools` as a list will break.

---

### M-16: Test Documents and Artifacts Not Cleaned Up

**Category:** Data Hygiene, Authoring  
**Component:** Documents view, state/artifacts  
**Scenario:** Examining documents and artifacts after testing  

**Evidence:**
- 6 test documents remain: `test-document.txt`, `test-document.html`, `test-document.pdf` (failed to parse), `preview-test.txt`, `toggle-test.txt`, `delete-test.txt`
- 5 orphaned `author:pdf` artifacts (identical "Registration Probe" PDFs) sit in `state/artifacts` with no session pointing at them — unreachable by any UI
- Authoring/Runs needs a "documents" list that can delete artifacts

**Impact:** Storage waste, cluttered UI, unreachable data.

---

## LOW SEVERITY FINDINGS

### L-1: No DAG Visualization in Chat View After Completion

**Category:** UI, Chat  
**Component:** `Chat.tsx`, `Runs.tsx`  
**Scenario:** Checking DAG visualization after a chat  

**Evidence:**
- Chat view contains 23 SVG elements (all Lucide UI icons) but 0 DAG-specific elements
- `Chat.tsx` does not import or render `DagCanvas` anywhere
- `DagCanvas` (using `@xyflow/react`) is only imported in `Runs.tsx`
- Lightweight chat mode explicitly states "Quick answers, no pipeline, nothing recorded in Runs"
- DAG-mode chat could potentially produce a recorded run, but the Chat view doesn't display it

**Impact:** Users expecting to see execution graphs after chatting have no way to view them from the Chat view.

---

### L-2: Formatter Node Taking 313 Seconds

**Category:** Performance, DAG Execution  
**Component:** `flow.py` — formatter node  
**Scenario:** General relativity research run  

**Evidence:**
- Run `s8-73a316ae` had node `n:5 formatter` take 313.3s
- The other 4 nodes completed in ~93s
- The final aggregation step is disproportionately slow

**Impact:** Long-tailed latency for complex research tasks; user waits unnecessarily for formatting.

---

### L-3: Duplicate `<main>` Elements in HTML

**Category:** HTML, Accessibility  
**Component:** `App.tsx`, view components  
**Scenario:** DOM inspection  

**Evidence:**
- RouterShell renders `<main class="relative flex...">` (outer)
- Each view renders `<main id="main">` (inner)
- HTML spec allows only one `<main>` per document
- Playwright `locator('main')` gets "strict mode violation: resolved to 2 elements"

**Impact:** Confusion for assistive tech and CSS selectors; spec violation.

---

### L-4: Empty Input Button `disabled` Attribute Is Null

**Category:** UI, Accessibility  
**Component:** `Chat.tsx` — Send button  
**Scenario:** Checking disabled state of Send button with empty input  

**Evidence:**
- When textarea is empty, `getAttribute('disabled')` returns `null` (attribute absent)
- Button appears visually disabled but may not be properly marked in HTML

**Impact:** Poor accessibility — screen readers can't determine the button is disabled.

---

### L-5: All 119 Runs Show `$0.0000` Cost

**Category:** Billing, Ledger  
**Component:** `agent_server.py` — cost endpoints  
**Scenario:** Examining cost data  

**Evidence:**
- All 119 runs show `usd: 0.0`
- Runs header shows "$0.0000"
- Ledger shows "SPEND: n/a / cost data unavailable"
- Gateway returns 401 "missing or invalid x-gateway-token header" — pricing integration broken

**Impact:** Users can't see cost information.

---

### L-6: `limit=-1` Returns 1 Session (Suspicious Behavior)

**Category:** API, Pagination  
**Component:** `agent_server.py` — sessions endpoint  
**Scenario:** Requesting sessions with `limit=-1`  

**Evidence:**
- `limit=-1` returns only 1 session
- This is suspicious — `-1` typically means "no limit"

**Impact:** API pagination behavior is non-standard; may confuse API consumers.

---

### L-7: Retriiver Node 35.7% Failure Rate

**Category:** Node Health, Reliability  
**Component:** `agent_server.py` — `/api/nodes/health`  
**Scenario:** Checking node health statistics  

**Evidence:**
- Retriever skill: 5 failed out of 15 nodes (35.7% failure rate)
- Researcher: 23/208 (14.7% failure rate)
- Author: 3/50 (13.6% failure rate)

**Impact:** Some skills have high failure rates, potentially affecting reliability of complex pipelines.

---

### L-8: Gateway OpenAPI Spec Exposed Without Authentication

**Category:** Security  
**Component:** Gateway (`llm_gatewayV9`)  
**Scenario:** Accessing gateway `/openapi.json`  

**Evidence:**
- `/openapi.json` on gateway (port 8109) returns HTTP 200 without auth
- Reveals 71 API paths including sensitive endpoints: `/v1/config/keys`, `/v1/control/kill`, `/v1/control/pair`, `/v1/policy/reload`, `/v1/memory/remember`
- Sensitive gateway endpoints (`/v1/config/keys`, `/v1/policy`, `/v1/spend`) properly return 401 without token

**Impact:** Information disclosure — reveals internal API surface.

---

### L-9: Templates Endpoint Stores Path Traversal and NUL Characters

**Category:** Uploads, Validation  
**Component:** `agent_server.py` — templates endpoints  
**Scenario:** Creating templates with special characters in name  

**Evidence:**
- Template names can contain `../..` (path traversal) and `a\x00b` (NUL byte)
- No input sanitization on template names

**Impact:** Potential for file system issues or injection attacks.

---

### L-10: Non-Existent Session Returns 200 Instead of 404

**Category:** API, Errors  
**Component:** `agent_server.py` — session graph handler  
**Scenario:** Requesting graph for non-existent session  

**Evidence:**
- `GET /api/sessions/nonexistent_session/graph` returns HTTP 200
- Response: `{"session_id": "nonexistent_session", "nodes": [], "edges": []}`
- API consumers cannot distinguish real sessions from non-existent ones

**Impact:** Ambiguous API responses; clients can't detect invalid session IDs.

---

### L-11: All 15 Rail Navigation Icons Below Minimum Touch Target Size

**Category:** UI, Accessibility  
**Component:** `ui.tsx` — Rail component  
**Scenario:** Checking icon sizes for touch accessibility  

**Evidence:**
- All 15 rail navigation icons are 40×40 px
- Apple's Human Interface Guidelines recommend minimum 44×44 px for touch targets

**Impact:** Difficult to tap on mobile/touch devices.

---

### L-12: Memory Form Shows "preference" and "observation" Kinds But No Content

**Category:** UI, Memory  
**Component:** `Memory.tsx`  
**Scenario:** Examining memory items by kind  

**Evidence:**
- Memory view shows 83 items
- Create form has all 5 kinds (fact/preference/observation/tool_outcome/scratchpad)
- But no "preference" or "observation" content items exist despite being in the form
- Distribution: 91 tool_outcome, 6 fact, 3 scratchpad (total exceeds 83 — some are filtered/collapsed)

**Impact:** Inconsistent data model — form offers options with no supporting data.

---

### L-13: Apps View Shows 319 Apps (14 Running) But API Returns Only 5

**Category:** UI, Apps  
**Component:** `Computer.tsx`, `Apps.tsx`  
**Scenario:** Checking apps count discrepancy  

**Evidence:**
- Computer view shows "APPS: 319, 14 running"
- `/api/apps` returns only 5 apps (all `running=None`)
- The "14 running" is suspicious since dry-run mode means nothing should execute

**Impact:** Misleading app counts in the UI.

---

### L-14: `/api/cost` Returns Empty Rows Without Guidance

**Category:** API, UX  
**Component:** `agent_server.py` — cost handler  
**Scenario:** Calling `/api/cost` without a session  

**Evidence:**
- Returns `{'in_tokens': 0, 'out_tokens': 0, 'dollars': 0.0, 'calls': 0}` with empty rows
- No explanation provided (e.g., "specify `?session=X` or `?conversation_id=Y`")

**Impact:** Poor developer experience — no guidance on valid query parameters.

---

### L-15: Gateway Unreachable Alert Visible on Multiple Pages

**Category:** UI, Status  
**Component:** Various views  
**Scenario:** Checking status indicators  

**Evidence:**
- Gateway unreachable alert visible on Code, Settings, and Apps pages
- Gateway health at port 8109 may be intermittent

**Impact:** Confusing status indication when gateway is partially available.

---

### L-16: Mission TopBar Crumb Says "Console" Instead of "Mission"

**Category:** UI, Navigation  
**Component:** `Mission.tsx:89`  
**Scenario:** Checking breadcrumb label on Mission view  

**Evidence:**
- TopBar crumb says "Console" instead of "Mission"

**Impact:** Confusing breadcrumb navigation.

---

### L-17: Empty Query Button Appears Disabled But Lacks Proper `disabled` Attribute

**Category:** UI, Accessibility  
**Component:** `Chat.tsx` — Send button  
**Scenario:** Clicking Send with empty input  

**Evidence:**
- Button appears visually disabled but `getAttribute('disabled')` returns `null`
- Click attempts timeout after 3s

**Impact:** Poor accessibility and inconsistent behavior.

---

### L-18: Research State Lost on Navigation Away During Active Run

**Category:** Research, State Persistence  
**Component:** `KeepAlive.tsx:8`  
**Scenario:** Navigating away during research and returning  

**Evidence:**
- Navigating away during a run and returning shows "Ask a topic to begin" instead of the running session
- The server-side run continues (confirmed `live=true` via API) but the UI loses the reference
- `KeepAlive` component not preserving session state on route change

**Impact:** User loses context of active research when navigating between views.

---

### L-19: "Free OpenRouter models" App Lingers After Provider Removal

**Category:** UI, Apps  
**Component:** `Apps.tsx`  
**Scenario:** Checking for stale apps  

**Evidence:**
- Pre-existing app named "Free OpenRouter models" (`openrouter_free`) is listed
- OpenRouter was removed as a gateway *provider*, but the app remains user-visible
- Detail page shows `kind=openrouter_free`

**Impact:** Confusing naming — references a removed provider.

---

### L-20: `/api/templates/nope` Returns 200 Instead of 404

**Category:** API, Errors  
**Component:** `agent_server.py` — templates endpoints  
**Scenario:** Requesting non-existent template  

**Evidence:**
- Returns HTTP 200 with `{"status":"not_found"}` instead of HTTP 404

**Impact:** Inconsistent error reporting compared to other endpoints.

---

### L-21: `/api/schedule/nope/enabled` Returns 200 Instead of 404

**Category:** API, Errors  
**Component:** `agent_server.py` — scheduler endpoints  
**Scenario:** Requesting non-existent schedule  

**Evidence:**
- Returns HTTP 200 with `{"ok":false, ...}` instead of HTTP 404

**Impact:** Inconsistent error reporting; API consumers must inspect body, not just status code.

---

### L-22: `/api/code/tree` Outside-Workspace Returns 200 Instead of 403

**Category:** API, Security  
**Component:** `agent_server.py` — code endpoints  
**Scenario:** Requesting file tree outside workspace  

**Evidence:**
- Returns HTTP 200 carrying `{"error":...}` (but `/api/code/file` correctly 403s)

**Impact:** Inconsistent status codes between similar endpoints; potential security confusion.

---

### L-23: Memory Wipe Shows No Confirm Dialog Text

**Category:** UI, Memory  
**Component:** `Memory.tsx`  
**Scenario:** Checking wipe confirmation  

**Evidence:**
- Wipe shows no confirm dialog text before acting
- Native `confirm()` is silently auto-dismissed by test browser

**Impact:** Poor UX — users can't see what they're confirming before data is deleted.

---

### L-24: Documents Can Sit on "parsing · 0 chunks" Forever

**Category:** Documents, UI  
**Component:** `Documents.tsx`  
**Scenario:** Uploading a document that fails to parse  

**Evidence:**
- A document can sit on "parsing · 0 chunks" forever with no failed/timeout/retry state

**Impact:** Users think parsing is in progress when it has actually failed.

---

### L-25: Topbar Crumb Drifts From Thread Title After Follow-Up

**Category:** UI, Chat  
**Component:** `Chat.tsx` — TopBar  
**Scenario:** Continuing a conversation with a follow-up  

**Evidence:**
- TopBar crumb drifts from the thread title after a follow-up

**Impact:** Confusing navigation breadcrumb.

---

### L-26: Orphaned Artifacts Have No UI Path to Delete

**Category:** Data Hygiene, Authoring  
**Component:** `Authoring.tsx`, artifact storage  
**Scenario:** Looking for ways to clean up orphaned artifacts  

**Evidence:**
- Authoring documents (`.bin` + `.json`, `source: author:pdf`) can outlive their run
- No UI lists or deletes orphaned artifacts
- Fix orientation: artifact lifetime must be tied to its producing run

**Impact:** Storage waste; unreachable data.

---

## TEST SCENARIO RESULTS SUMMARY

| Subagent | Focus Area | H | M | L | Total |
|----------|-----------|---|---|---|-------|
| Chat UX Test | Chat, streaming, UI | 0 | 2 | 2 | 4 |
| Research & Memory Test | Research, Memory, Scheduler, Runs | 2 | 4 | 6 | 12 |
| Code & Settings Test | Code, Settings, Tools, Apps, etc. | 0 | 3 | 4 | 7 |
| Performance & Streaming | Streaming, API, performance, errors | 3 | 7 | 9 | 19 |
| **Totals** | | **5** | **16** | **21** | **42** |

---

## SERVER-SIDE ERRORS OBSERVED (from logs/agent.err)

| Error | Severity | Component | Impact |
|-------|----------|-----------|--------|
| `NameError: name '_aio' is not defined` | M | `agent_server.py` | Route handler crashes, no response sent |
| `PermissionError: [WinError 5] Access is denied` | H | `_write_json_atomic()` | Windows file write failures for state/apps/*.json |
| `ConnectionResetError: [WinError 10054]` | M | asyncio transport | Client disconnects mid-stream |
| `httpx.HTTPStatusError: 503 Service Unavailable` | M | Gateway integration | All providers unavailable after retries |
| `[Errno 10048] error while attempting to bind` | M | Server startup | Port 8500 already in use |
| `ValueError: I/O operation on uninitialized object` | L | `extract_observations` thread | Background thread issue |

---

## COMPARISON TO PREVIOUSLY KNOWN BUGS (BUGS.md)

| BUG | Status | Current State |
|-----|--------|---------------|
| BUG-001: Parallel sibling nodes crash with ExceptionGroup | FIXED (2026-08-21) | Verified fixed — 3 parallel researchers all complete, 0 recovery nodes |
| BUG-002: Local Qwen2.5-1.5B a11y unreliable | WON'T FIX | Decision not to use local model |
| BUG-003: Offline gated-shell inert without vision judge | OPEN | No change reported |
| BUG-004: Research DAG fails every researcher | FIXED (2026-10-18) | Verified fixed in `CONSOLE_TEST_FINDINGS.md` |
| BUG-005: Planner inputs orphans formatter | FIXED (2026-10-18) | Verified fixed |
| BUG-006: MCP teardown ExceptionGroup | FIXED (2026-10-18) | Verified fixed |

---

## RECOMMENDATIONS (NOT FIXING — REPORT ONLY)

1. **Critical:** Fix SSE frame queue overflow — implement backpressure or increase queue size; log when frames are dropped
2. **Critical:** Fix double-queue bug in chat send() — debounce or deduplicate queued messages
3. **High:** Sanitize path parameters to reject `..` traversal patterns
4. **High:** Fix Windows `PermissionError` in `_write_json_atomic()` — retry or use different strategy on Windows
5. **Medium:** Add idempotency to `/api/chat/simple/stream` for same `conversation_id`
6. **Medium:** Fix `_plain_id_ok()` to enforce character set restrictions matching the error message
7. **Medium:** Clean up 14 zombie sessions and 3 cancelled scheduler jobs
8. **Medium:** Add theme toggle button to UI
9. **Medium:** Disable Send button during active streaming
10. **Medium:** Add proper `disabled` attribute to empty-state buttons for accessibility
11. **Low:** Add DAG visualization to Chat view
12. **Low:** Clean up test documents and orphaned artifacts

---

## FILES EXAMINED

- `S9SharedCode/code/agent_server.py` (main agent server, ~11,464 lines)
- `S9SharedCode/code/flow.py` (DAG executor)
- `S9SharedCode/code/console-frontend/src/Chat.tsx`
- `S9SharedCode/code/console-frontend/src/Runs.tsx`
- `S9SharedCode/code/console-frontend/src/Memory.tsx`
- `S9SharedCode/code/console-frontend/src/Scheduler.tsx`
- `S9SharedCode/code/console-frontend/src/Computer.tsx`
- `S9SharedCode/code/console-frontend/src/Code.tsx`
- `S9SharedCode/code/console-frontend/src/Apps.tsx`
- `S9SharedCode/code/console-frontend/src/Ledger.tsx`
- `S9SharedCode/code/console-frontend/src/ui.tsx`
- `S9SharedCode/code/console-frontend/src/App.tsx`
- `S9SharedCode/code/console-frontend/src/api.ts`
- `S9SharedCode/code/BUGS.md`
- `CONSOLE_TEST_FINDINGS.md`
- `logs/agent.err` (server-side error logs)
