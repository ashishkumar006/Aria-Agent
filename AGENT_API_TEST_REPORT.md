# Agent Server API Test Report

**Date:** 2026-10-27  
**Agent API:** `http://127.0.0.1:8500`  
**Gateway API:** `http://127.0.0.1:8109`  
**Test Script:** `C:/Users/AISHWARYA/AppData/Local/Temp/kilo/api_test.py`  
**Full JSON Report:** `C:/Users/AISHWARYA/AppData/Local/Temp/kilo/api_test_report.json`

---

## Executive Summary

Black-box testing of the Aria agent server API identified **13 issues** across H/M/L severities. Core functionality (health, chat, sessions, streaming, auth, uploads) works, but several security and data integrity issues exist. The agent server took 9-14s per LLM call and gateway health reports `gateway_up: true`.

| Severity | Count |
|----------|-------|
| H (High) | 2 |
| M (Medium) | 6 |
| L (Low) | 3 |
| **Total** | **11** |

---

## High Severity

### 1. Path Traversal in Session ID Returns HTML (SPA Fallback)

**Category:** sessions, errors  
**Finding:** Requesting `/api/sessions/../../etc/graph` or `/api/sessions/../../etc/passwd/nodes/n:1` returns HTTP 200 with HTML content (the SPA index.html). No path sanitization is performed on the session_id path segment.

**Evidence:** `status=200`, body starts with `<!doctype html><html lang="en" class="dark">`  
**Files:** `agent_server.py` - session graph route handler

**Impact:** Potential path traversal exposure. Should return 403/404 or sanitize the session_id.

### 2. Path Traversal in Session/Node Paths Returns HTML

**Category:** errors  
**Finding:** Same pattern for `/api/sessions/../../etc/passwd/nodes/n:1` — returns 200 with HTML instead of rejecting the traversal attempt.

---

## Medium Severity

### 3. 14 Zombie Sessions with Empty Query

**Category:** runs  
**Finding:** 14 sessions (IDs like `L0-er04`, `L0-er02`, `L0-er01`, etc.) have empty queries and empty status_counts. These are incomplete/corrupted sessions that should be cleaned up or marked as errored.

**Evidence:** `[('L0-er04', {}), ('L0-er02', {}), ('L0-er01', {})]`  
**Files:** `agent_server.py` - runs/summary endpoint, `_CHAT_THREADS_PATH`

### 4. Non-Existent Session Returns 200 Instead of 404

**Category:** errors  
**Finding:** Requesting `/api/sessions/nonexistent_session/graph` returns HTTP 200 with `{'session_id': 'nonexistent_session', 'nodes': [], 'edges': []}` instead of 404. API consumers cannot distinguish real sessions from non-existent ones.

**Files:** `agent_server.py` - session graph handler

### 5. Gateway OpenAPI Spec Exposed Without Authentication

**Category:** security  
**Finding:** `/openapi.json` on the gateway (port 8109) returns HTTP 200 without authentication, revealing 71 API paths including sensitive endpoints (`/v1/config/keys`, `/v1/control/kill`, `/v1/control/pair`, `/v1/policy/reload`, `/v1/memory/remember`).

**Evidence:** `openapi.json` returns 71 paths including `/v1/control/kill`, `/v1/control/pair`, `/v1/config/keys`  
**Impact:** Information disclosure — reveals internal API surface and endpoints that require tokens.

### 6. Templates Endpoint Stores Path Traversal and NUL Characters

**Category:** uploads  
**Finding:** The templates endpoint stores templates with names containing `../..` (path traversal) and `a\x00b` (NUL byte). No input sanitization on template names.

**Evidence:** `[{'name': '../..', ...}, {'name': 'a\x00b', ...}]`  
**Files:** `agent_server.py` - templates endpoints

### 7. Invalid Conversation ID Types Accepted by /api/chat

**Category:** chat  
**Finding:** POSTing to `/api/chat` with `conversation_id: 12345` (number) or `conversation_id: true` (boolean) returns HTTP 200 and starts a DAG run instead of returning 400. The `resolve_session()` / `_conv_load()` functions do not validate the type.

**Files:** `agent_server.py` - `resolve_session()`, `_conv_load()`

### 8. /api/capabilities Returns "tools" as Dict Not List

**Category:** capabilities  
**Finding:** The `/api/capabilities` endpoint returns `tools` as a dict with keys `['toolCalling', 'tools']` instead of a list. This is inconsistent with expected schema and may break clients expecting a list.

**Evidence:** `tools type: dict, tools keys: ['toolCalling', 'tools']`  
**Files:** `agent_server.py` - capabilities handler

### 9. 18 Sessions With No Graph

**Category:** runs  
**Finding:** 18 sessions have `has_graph: false`, including the 14 zombie sessions. These are sessions with nodes but no graph data, indicating incomplete writes or corrupted state.

---

## Low Severity

### 10. /api/cost Returns Empty Rows Without Explanation

**Category:** cost  
**Finding:** Calling `/api/cost` without a session returns `{'in_tokens': 0, 'out_tokens': 0, 'dollars': 0.0, 'calls': 0}` with empty rows. No explanation is provided (e.g., "specify ?session=X or ?conversation_id=Y").

### 11. Retriever Node 35.7% Failure Rate

**Category:** nodes/health  
**Finding:** The retriever skill has 5 failed out of 15 nodes (35.7% failure rate). Researcher has 23/208 (14.7%), author has 3/50 (13.6%).

**Files:** `agent_server.py` - `/api/nodes/health`

---

## What Works Correctly

### API Functionality
- **Health:** `/api/health` returns 200 with `{"agent":"ready","gateway_up":true,"spa_built":true}`; publicly accessible without auth
- **Auth:** Token-based auth via `X-Aria-Token` properly enforces 403 on unauthenticated requests; 403 on wrong token; health remains public with any/wrong token
- **Chat (simple):** `/api/chat/simple` returns answers (e.g., "2 + 2 is 4"); supports `conversation_id` for follow-ups; enforces 4000-char query limit; rejects empty/missing query with 400
- **Chat (DAG):** `/api/chat` sends SSE with frames: `started` → `log` → `status` → `meta` → `done` (terminal)
- **Chat (streaming):** `/api/chat/simple/stream` sends SSE with frames: `started` → `status` → `delta` → `done`
- **SSE Headers:** Correct `Cache-Control: no-cache, no-transform`, `Connection: keep-alive`, `X-Accel-Buffering: no`, `content-type: text/event-stream`
- **Sessions:** `/api/sessions` returns paginated list with limit support; `limit=-1` returns 1 session (suspicious but functional)
- **Documents:** Upload rejects `.exe` (400), no-file (400); search returns empty hits for nonexistent doc_ids; test documents properly cleaned up
- **Cost:** `/api/cost?session=X` returns token/cost breakdown; `/api/cost/by_skill` returns per-skill costs; `/api/cost?conversation_id=X` handles unknown conversations
- **Scheduler:** Rejects empty and invalid `when` with clear error messages
- **Memory:** Accepts/returns data for normal and NUL-byte queries
- **Gateway:** `/health` returns 200 publicly; `/v1/config/keys`, `/v1/policy`, `/v1/spend` return 401 without token (properly protected)

### Performance & Reliability
- 20 concurrent health checks: 0 errors, max latency 0.032s, 0.06s total
- 10 concurrent session lists: 0 non-200, max latency 0.469s
- 2 concurrent chat/simple: both 200 (one at a time, ~9-12s each)
- 50 rapid health checks: 0 errors
- 20 rapid session-list checks: 0 errors
- Health endpoint: ~0.015s latency
- Session list: ~0.5s latency

### Security (Working)
- Cross-origin POST with `Origin: http://evil.example:8500` returns 403
- Same-origin POST with `Origin: http://127.0.0.1:8500` returns 200
- Invalid JSON body → 400
- Array body → 400
- Null body → 400
- Unsupported file upload (.exe) → 400
- Invalid memory kind → 400
- Invalid schedule time → 400 with helpful error message
- Malformed artifact id → 400

---

## Recommendations

1. **Fix path traversal:** Sanitize `session_id` and `node_id` path parameters; reject `..`, `/`, NUL bytes. Return 404 not 200+HTML for invalid sessions.
2. **Validate conversation_id:** Type-check `conversation_id` in `/api/chat` — reject non-string values with 400.
3. **Protect gateway openapi:** Require auth on `/openapi.json` or strip sensitive paths from the public spec.
4. **Sanitize template names:** Reject template names containing `..`, NUL bytes, or path separators.
5. **Fix capabilities schema:** Return `tools` as a list (or document the dict format).
6. **Add cost guidance:** When no session specified, suggest valid query parameters.
7. **Investigate zombie sessions:** 14 sessions with empty query/status — these should be cleaned up or auto-deleted.
8. **Investigate node failures:** retriever 35.7% failure rate needs investigation.
