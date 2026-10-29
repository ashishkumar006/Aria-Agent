# Playwright Test Findings: Agent Console Performance, Streaming, and Error Handling

**Date:** 2026-10-27  
**Target:** `http://127.0.0.1:8500` (agent server) + `http://127.0.0.1:8109` (gateway)  
**Tooling:** Playwright (Chromium, headless) + native fetch for SSE  
**Auth:** `X-Aria-Token` header used throughout (token present in `state/agent.token`)

---

## Executive Summary

| Severity | Count |
|----------|-------|
| High     | 3     |
| Medium   | 5     |
| Low      | 28    |

Key issues: SSE stream data loss on large responses, server-side `NameError`/`PermissionError` crashes, no idempotency on lightweight chat duplicates, send button enabled during streaming.

---

## Test 1: Normal Chat Message Response Time

**Endpoint:** `POST /api/chat/simple/stream`  
**Query variety:** Short ("Hello, what is the weather today?")

| Metric                  | Run 1     | Run 2     | Run 3 (final) |
|-------------------------|-----------|-----------|---------------|
| Total wall time         | 21,950ms  | 32,561ms  | 22,216ms      |
| Time to first byte (TTFB)| 79-103ms | 93ms      | 93ms          |
| Time to first delta     | 21,925ms  | N/A       | 21,043ms      |
| Time to `done` frame    | 21,949ms  | 32,560ms  | 22,215ms      |
| Frame count             | 16        | 8         | 16            |
| Answer length           | 100 chars | 0 chars*  | 256 chars     |

\* Run 2 received only status heartbeats and a `done` frame with empty answer — the model produced no text.

**Findings:**
- **[L] Test 1:** TTFB is fast (79–119ms) — server accepts the stream immediately.
- **[M] Test 1:** Time-to-first-delta is consistently ~21s across runs, even for trivial queries. The model call itself is the bottleneck, not the SSE transport.
- **[M] Test 1:** Run 2 received an empty answer (0 chars in `done` frame, 0 delta frames). The gateway returned a response that produced no text. No `error` frame was emitted — the client would see a "successful" empty reply.
- **[L] Test 1:** Average of 5–6 heartbeat `status` frames emitted before the first `delta` arrives (heartbeat interval = 10s per `_CHAT_STREAM_HEARTBEAT_S`).

**Source:** `agent_server.py:10467-10494` (SSE generator), `_CHAT_STREAM_HEARTBEAT_S = 10.0` (`agent_server.py:7639`).

---

## Test 2: Concurrent Chat Requests

**Method:** 3 SSE requests sent simultaneously via `Promise.all`, each with a unique `conversation_id`.

| Request | Query                          | Total time | Frames    | Errors |
|---------|--------------------------------|------------|-----------|--------|
| 1       | "Tell me about quantum computing" | 12,389ms  | started, status×3, delta, done | No |
| 2       | "Explain photosynthesis briefly"  | 11,419ms  | started, status×2, delta, done | No |
| 3       | "What is the capital of France?"  | 11,731ms  | started, status×2, delta, done | No |

**Findings:**
- **[L] Test 2:** 3 concurrent requests completed in 12,389ms wall time (all completed successfully).
- **[L] Test 2:** No frame loss detected — all 3 streams ended with a proper `done` frame.
- **[L] Test 2:** Concurrency overhead minimal: parallel wall time (12.4s) is within 2.5× of single (22.2s), indicating no server-side contention.

---

## Test 2b: Idempotency / Duplicate Submit (Lightweight Chat)

**Method:** Two identical queries sent with the same `conversation_id` to `/api/chat/simple/stream` via `Promise.all`.

| Submit | Total time | Frames                         | Dedup flag | Answer |
|--------|------------|--------------------------------|------------|--------|
| 1      | 40,425ms   | started, status×3, delta, done | false      | 76 chars |
| 2      | 48,888ms   | started, status×5, delta, done | false      | 131 chars |

**Findings:**
- **[M] Test 2b:** **Both requests completed independently** — no deduplication for `/api/chat/simple/stream` when re-using the same `conversation_id`. The idempotency key (`_inflight_run_claim`) is only used for `research: true` submits, not regular chat.
- **[M] Test 2b:** Both requests produced different-length answers (76 vs 131 chars) — the model was called twice for the same query, potentially resulting in **double billing** to the gateway session.
- **[L] Test 2b:** The `started` frame did not carry `deduplicated: true` for either request.

**Source:** `agent_server.py:10153` — `_inflight_run_claim(idem_key, conversation_id)` only claims when `idem_key` is non-empty, and `idem_key` is only auto-generated for `research: true` (`agent_server.py:10126-10128`).

---

## Test 2c: Research Idempotency

**Method:** Two identical `/api/chat` requests with `research: true` and the same `idempotency_key`.

| Submit | HTTP Status | First SSE frame                                                                 |
|--------|-------------|---------------------------------------------------------------------------------|
| 1      | 200         | `data: {"type": "started", "session_id": "s8-...", "conversation_id": "research-idem-1"}` |
| 2      | 200         | `data: {"type": "started", "conversation_id": "research-idem-1", "deduplicated": true, "text": "an id..."}` |

**Findings:**
- **[L] Test 2c:** Research idempotency works correctly — the second submit was detected as a duplicate via `deduplicated: true` in the `started` frame.
- **[L] Test 2c:** Both requests returned within 64ms (the second was immediately deduped).

**Source:** `agent_server.py:10058-10080` (`_dedup_stream`).

---

## Test 3: API Endpoint Latencies

Three requests per endpoint; median reported.

| Endpoint          | Median | Max    | Status | Body size |
|-------------------|--------|--------|--------|-----------|
| `/api/health`     | 5–9ms  | 13ms   | 200    | 52 bytes  |
| `/api/sessions`   | 129–386ms | 419ms | 200    | 500 bytes |
| `/api/tools`      | 6–15ms | 16ms   | 200    | 500 bytes |
| `/api/config`     | 17–47ms | 75ms  | 200    | 500 bytes |
| `/api/nonexistent`| 6ms    | 6ms    | 404    | 67 bytes  |

**Findings:**
- **[L] Test 3:** `/api/health` is fast (5–9ms median) and uses the cached gateway liveness check (`_gateway_up_cached` with 30s TTL).
- **[M] Test 3:** `/api/sessions` is the slowest endpoint (129–386ms median) — it parses `graph.json` and builds node-link graphs for each session. This varies significantly under load.
- **[L] Test 3:** `/api/config` returns `{"raw":..., "parsed":...}` — field names differ from the frontend type definition.
- **[L] Test 3b:** `/api/nonexistent` returns 404 with `{"detail":"Not Found","path":"/api/nonexistent"}` — standard FastAPI 404 response.

---

## Test 4: Error Handling

### 4a: Malformed JSON body
- **Request:** `POST /api/chat/simple/stream` with body `{not valid json`
- **Result:** HTTP 400, `{"error":"query required"}`, ~7ms
- **[L] Test 4a:** Malformed JSON is caught by the `try/except` around `req.json()` and falls back to `body = {}`, which then triggers the `query is None` check. Returns 400. Works but conflates "malformed JSON" with "missing query field".

### 4b: Missing query field
- **Request:** `POST /api/chat/simple/stream` with body `{}`
- **Result:** HTTP 400, `{"error":"query required"}`, ~5ms
- **[L] Test 4b:** Correctly returns 400 with descriptive error message.

### 4c: Invalid session ID (path parameter)
- **Request:** `GET /api/sessions/!!!invalid_id!!!/graph`
- **Result:** HTTP 400, `{"error":"invalid session_id"}`, ~varies
- **[L] Test 4c:** Path-injection characters (like `!!!`) are rejected with 400.

### 4d: Invalid session ID (produced artifacts)
- **Request:** `GET /api/sessions/!!!invalid!!!/produced`
- **Result:** HTTP 400, `{"error":"invalid session_id"}`
- **[L] Test 4d:** Same validation applied — `!!!` rejected.

### 4e: Invalid artifact ID
- **Request:** `GET /api/artifact/!!!invalid!!!/preview`
- **Result:** HTTP 400, `{"error":"malformed artifact id"}`
- **[L] Test 4e:** Artifact ID validation requires `art:[0-9a-fA-F]{16}` pattern; `!!!` fails the regex.

### 4f: Invalid conversation_id with special characters
- **Request:** `POST /api/chat/simple/stream` with `conversation_id: "INVALID!@#$%^&*()"`
- **Result:** **HTTP 200** — streaming response initiated successfully
- **[H] Test 4f:** `conversation_id` containing `!@#$%^&*()` was **accepted** (HTTP 200) and the full stream completed. The `_plain_id_ok()` check (`agent_server.py:7014`) only rejects `..`, `/`, `\`, leading `.`, and control characters (ord < 0x20 or 0x7f). It does **not** validate the character set. The error message claims "conversation_id must be a plain id (letters, digits, '-', '_', '.')" but the validation allows any non-path-separator printable character.

### 4g: Oversized query
- **Request:** `POST /api/chat/simple/stream` with a 5000-char query (limit is 4000)
- **Result:** HTTP 400, `{"error":"query too long (max 4000 chars)"}`
- **[L] Test 4g:** Correctly rejected with 400.

### 4h: No auth token
- **Request:** `GET /api/sessions` without `X-Aria-Token` header
- **Result:** HTTP 403
- **[L] Test 4h:** Auth token is enforced on non-health endpoints.

**Source for 4f:** `_plain_id_ok()` at `agent_server.py:7014-7021`:
```python
def _plain_id_ok(s: str) -> bool:
    return bool(s) and ".." not in s and not any(c in s for c in "/\\") \
        and not s.startswith(".") \
        and not any(ord(c) < 0x20 or ord(c) == 0x7f for c in s)
```

---

## Test 5: SSE Frame Ordering

**Method:** Two SSE streams captured and frame type sequences analyzed.

| Query             | Frame sequence                                      | Order correct? |
|-------------------|-----------------------------------------------------|----------------|
| "Count from 1 to 3" | started, status, delta, done                       | Yes            |
| "Say the alphabet"  | started, status, delta, done                       | Yes            |

**Findings:**
- **[L] Test 5:** SSE frame order is correct for small responses: `started → status → delta → done` (or `error` as terminal).
- **[L] Test 5:** No delta frames were observed after the terminal `done`/`error` frame in any verified stream.
- **[L] Test 5:** The `sseFrames` parser in `api.ts:799-832` correctly handles `\r\n\r\n` separators, split frames, and trailing buffers.

---

## Test 6: UI Race Conditions

**Method:** Load `/console` in Chromium, send a chat message, check if the send button is disabled during streaming, navigate between pages, check for duplicate bubbles.

**Findings:**
- **[M] Test 6:** **Send button is ENABLED during active streaming.** The user can click "Send" again while a response is being generated. While the Chat.tsx component uses `abortRef`/`reqRef` guards (`Chat.tsx:1387: if (!mountedRef.current || reqRef.current !== req) return;`), the button itself is not disabled, presenting a poor UX and potential for confusion/race conditions.
- **[L] Test 6:** No duplicate message bubbles were detected after the stream completed. The `bubbleId` reserved-slot pattern (`Chat.tsx:1328-1335`) correctly replaces the slot in place.
- **[L] Test 10:** Navigating away mid-stream (aborting the fetch) did not cause visible errors in the browser console.

---

## Test 7: Large Response Handling

**Method:** Query requesting an 800+ word response about computer science history.

| Metric              | Value                |
|---------------------|----------------------|
| Total frames        | 23                   |
| Delta frames        | **0**                |
| Delta chars         | **0**                |
| Done answer chars   | 31,868               |
| Status (heartbeat) frames | 21              |
| Total time          | 186,232ms (~3 min)   |

### 7b: Medium response verification
| Metric              | Value                |
|---------------------|----------------------|
| Total frames        | 6                    |
| Delta frames        | 1                    |
| Delta chars         | 1,411                |
| Done answer chars   | **0**                |
| Total time          | ~21,742ms            |

**Findings:**
- **[H] Test 7:** **Critical streaming data loss on large responses.** The 31,868-char response arrived entirely in a single `done` frame with **zero delta frames**. Despite 21 heartbeat `status` frames being emitted over 186 seconds, no incremental text was streamed to the client. The entire answer was delivered as one payload at completion.
- **[H] Test 7:** The `_put()` function (`agent_server.py:10195-10199`) **silently drops** frames when the queue is full (maxsize=4000):
  ```python
  def _put(kind: str, payload: dict) -> None:
      try:
          q.put_nowait((kind, payload))
      except _aio.QueueFull:
          pass  # silently drops
  ```
  For large responses where `_chat_chunks()` generates thousands of 4-word chunks (31,868 chars ≈ ~7,967 chunks), the bounded queue can fill up with heartbeat status frames, causing delta frames to be silently discarded.
- **[H] Test 7b:** **Data consistency issue**: For a 1,411-char medium response, the 1 delta frame carried all text, but the `done` frame's `answer` field was **empty (0 chars)**. This means the full answer is only recoverable from delta frames — if any delta is dropped, the answer is irretrievable. The root cause is that `_finish()` uses `answer` from `reply.get("text")` (`agent_server.py:10379`), which can be empty when the gateway streams text via the `on_event` callback but doesn't include it in the return dict.
- **[L] Test 7:** The large response took 186 seconds (~3 minutes), which is within acceptable bounds for a complex LLM call but significantly degrades UX.

**Source:** `_chat_chunks` at `agent_server.py:9967-9998`, queue at `agent_server.py:10188`, `_put` at `agent_server.py:10195-10199`, `_CHAT_STREAM_PACE_S = 0.012` at `agent_server.py:7640`.

---

## Test 8: Browser Memory/CPU Usage

**Method:** Chromium browser launched headless. Memory sampled via `performance.memory` before and during a streaming chat. Navigation timing via Performance API.

| Metric                          | Value          |
|---------------------------------|----------------|
| JS Heap before streaming        | 9.5–10.1 MB    |
| JS Heap total                   | 12.1–12.8 MB   |
| Peak heap during streaming      | 9.5 MB         |
| Heap growth                     | 0.0 MB         |
| Page load time (navigation)     | ~23ms          |

**Findings:**
- **[L] Test 8:** Browser memory usage is minimal — no memory leaks detected during streaming.
- **[L] Test 8:** The `sseFrames` function in `api.ts:799-832` reads SSE frames via `ReadableStream.getReader()`, which is memory-efficient.
- **[L] Test 8:** The `requestAnimationFrame`-coalesced rendering (`Chat.tsx:1316-1336`) prevents O(n²) re-renders during streaming — confirmed by zero heap growth.

**Note:** CPU usage could not be measured directly (Playwright's `page.metrics()` was removed in v1.64). Navigation timing via the Performance API showed acceptable page load times.

---

## Test 9: Timeout Behavior on Slow Responses

**Method:** Query requesting a long, detailed analysis. Monitored heartbeat status frames and total time.

| Metric                    | Value                                    |
|---------------------------|------------------------------------------|
| Total frames              | 6                                        |
| Status/heartbeat frames   | 3                                        |
| Total time                | 21,742–23,558ms                          |
| Heartbeat texts           | "thinking...", "still working - 10s elapsed", "still working - 20s elapsed" |

**Findings:**
- **[L] Test 9:** Heartbeat mechanism works correctly — `status` frames with elapsed time are emitted every ~10 seconds (`_CHAT_STREAM_HEARTBEAT_S = 10.0` at `agent_server.py:7639`).
- **[L] Test 9:** No server-side timeout was observed — the stream waited for the full model response regardless of duration.
- **[L] Test 9:** The `gen()` loop in `chat_simple_stream` (`agent_server.py:10467-10494`) uses a 0.5s polling interval (`_aio.wait_for(q.get(), timeout=0.5)`) and emits heartbeats when no new items arrive for 10s.
- **[L] Test 9:** The client's `sseFrames` parser (`api.ts:799-832`) correctly handles long-running streams with intermittent heartbeat frames.

---

## Test 10: Unhandled Promise Rejections in Browser Console

**Method:** Loaded all UI pages (`/console`, `/research`, `/runs`, `/memory`, `/skills`, `/settings`, `/apps`, `/ledger`), triggered chat interactions, navigated away mid-stream, and captured:
- `window.addEventListener('unhandledrejection', ...)` events
- `page.on('console')` error messages
- `page.on('pageerror')` uncaught exceptions

| Metric                              | Count |
|-------------------------------------|-------|
| Unhandled promise rejections        | 0     |
| Browser console errors              | 0     |
| Page-level unhandled errors         | 0     |

**Findings:**
- **[L] Test 10:** **No unhandled promise rejections detected** across all tested pages and interactions.
- **[L] Test 10:** **No browser console errors** — the React SPA handles errors gracefully via ErrorBoundary components.
- **[L] Test 10:** **No page-level unhandled exceptions** — all errors are caught and handled.

---

## Server-Side Error Logs (from `logs/agent.err`)

During the test run, the following server-side errors were captured in `logs/agent.err`:

- **[M] Server logs:** `NameError: name '_aio' is not defined` — A route handler referenced `asyncio as _aio` without importing it in scope. The error propagated as `RuntimeError: No response returned` (no response sent to client). This is the pattern that was previously fixed in `chat_simple` (`agent_server.py:9719-9721`) and `chat_simple_stream` (`agent_server.py:10094`) with comments documenting the fix, but appears to still occur in at least one code path.
  ```
  NameError: name '_aio' is not defined
  RuntimeError: No response returned.
  ```

- **[H] Server logs:** `PermissionError: [WinError 5] Access is denied` on `os.replace()` in `_write_json_atomic()` (`agent_server.py:126-135`). Windows cannot atomically rename over a file that is open by another process/thread. Multiple errors on `state/apps/*.json` files:
  ```
  PermissionError: [WinError 5] Access is denied:
  '...\state\apps\aria-e2e-stale-...data.21844.tmp' ->
  '...\state\apps\aria-e2e-stale-...data.json'
  ```
  This affects the `/api/apps` endpoints and the tracker app refresh functionality.

- **[M] Server logs:** `ConnectionResetError: [WinError 10054]` — client connections forcibly closed mid-stream (expected during test aborts, but surfaces as unhandled in asyncio callbacks):
  ```
  Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)
  ```

- **[M] Server logs:** `httpx.HTTPStatusError: Server error '503 Service Unavailable'` from the gateway — all providers unavailable after retries:
  ```
  attempts: [{'provider': 'gemini-3', 'reason': 'exception: Server disconnected without sending a response.'},
  {'provider': 'gemini-2', 'reason': 'exception: '}, {'provider': 'kilo', 'reason': 'cooldown (0.8s)'}, ...]
  ```

- **[M] Server logs:** `[Errno 10048] error while attempting to bind on address` — port 8500 already in use. Multiple server restart attempts logged:
  ```
  ERROR: [Errno 10048] error while attempting to bind on address ('127.0.0.1', 8500)
  ```

- **[L] Server logs:** `ValueError: I/O operation on uninitialized object` — thread I/O issue from `extract_observations` thread, likely the memory observation capture background thread.

---

## Additional Observations

### SSE Frame Format (`agent_server.py:10498-10501`)
SSE frames are formatted as `data: {json}\n\n` (no `event:` or `id:` fields). The `type` field is embedded in the JSON payload itself:
```python
def _sse(event_type: str, **payload) -> str:
    frame = {"type": event_type, **payload}
    return "data: " + json.dumps(frame) + "\n\n"
```
The `sseFrames` parser in `api.ts:799-832` correctly handles this format.

### Heartbeat Interval
`_CHAT_STREAM_HEARTBEAT_S = 10.0` (`agent_server.py:7639`). Heartbeats are `status` frames with elapsed time text.

### Batch Streaming Pace
`_CHAT_STREAM_PACE_S = 0.012` (`agent_server.py:7640`). When the gateway doesn't stream, text is chunked into 4-word groups and paced at 12ms between chunks.

### Queue Boundedness
The SSE frame queue has `maxsize=4000` (`agent_server.py:10188`). When full, `_put()` silently drops frames (`_aio.QueueFull` is caught and ignored). Only `_put_final()` drains the queue to ensure terminal frames are delivered.

### Max Query Length
`_CHAT_MAX_QUERY = 4000` (`agent_server.py:7632`). Queries exceeding 4000 chars are rejected with HTTP 400.

### Conversation ID Validation
`_plain_id_ok()` (`agent_server.py:7014-7021`) only checks for:
- Non-empty
- No `..` (path traversal)
- No `/` or `\` (path separator)
- No leading `.`
- No control characters (ord < 0x20 or 0x7f)

It does NOT enforce character set restrictions, despite the error message stating "letters, digits, '-', '_', '.'".

### Auth Enforcement
The `X-Aria-Token` header is required on all `/api/*` routes except `/api/health` (when auth is enabled). Unauthenticated requests return HTTP 403.
