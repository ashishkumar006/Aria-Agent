# Response size, pagination and streaming contracts — 06

*Written by the coordinator. The assigned subagent timed out upstream after establishing the gateway-token
mechanism, which is carried forward here with a corrected framing (see §6). Route-level payload figures are
measured from the break-test; the inventory and contract analysis are `file:line`-cited. Live enumeration
of every route was not possible — the agent went down mid-batch.*

## 1. Payload sizes — measured

| Endpoint | Size | Trigger |
|---|---|---|
| `GET :8109/v1/calls?limit=999999999` | **~20 MB** | limit accepted as given; `limit=-1` returns the same |
| `GET :8109/v1/calls?limit=5000` | 2.9 MB (the capture file) | a *reasonable* limit is already 2.9 MB |
| `GET /api/templates` | **~16 MB** | `state/templates.json` is **23.4 MB** |
| `GET /api/sessions/{id}/graph` | **202 670 chars** | one 31-node recovery-storm session |
| `GET /api/sessions/{id}/graph?light=true` | 5 593 chars | same session — **36× smaller** |
| `GET /api/code/files` | 80 KB | every call, no path needed |
| `GET /api/runs/summary` | 55 KB | polled by the Runs page |
| `GET /api/cost/by_skill` | ~1 KB | fine |
| `GET /v1/spend` | 9 KB | fine |

**Two of these are one-line fixes.** `?light=true` already exists and already works — 202 KB → 5.5 KB.
And `GET /api/cost` returning `{"rows":[],"totals":{…0}}` while `/api/cost/by_skill` returns 429 calls
means the agent has two cost endpoints with different data and the legacy console uses the empty one.

### Proposed caps

| Endpoint | Current | Proposed | Error when exceeded |
|---|---|---|---|
| `/api/calls`, `/v1/calls` | unbounded | default 200, max 1000 | 400 with `limit` in the message |
| `/api/sessions` | unclamped | default 50, max 500 | 400 — the docstring at `agent_server.py:1346-1349` **already claims** ≤500 |
| `/api/events` | unclamped | default 200, max 1000 | 400 |
| `/api/memory` | unclamped | default 50, max 500 | 400 |
| `/api/templates` | full objects | summary list (`name`, `vars`, `updated_at`); body on `GET /api/templates/{name}` | — |
| `/api/sessions/{id}/graph` | full | **default `light=true`**; `full=1` opt-in | — |
| `/api/code/files` | 80 KB | unchanged, but cacheable on roots mtime | — |

The `/api/sessions` case is worth calling out: the code *documents* the clamp and does not implement it.
That mismatch is why `?limit=99999999` returned 200.

## 2. Errors returned as HTTP 200

This is the most common contract defect, and it is systemic rather than per-route. Measured:

```
GET    /api/documents/does-not-exist     -> 200 {"error":"no such document …","status_code":404}
GET    /api/documents/%2e%2e%2f%2e%2e     -> 404 {"error":…}      <- same route, different handler
GET    /api/sessions/nope/graph          -> 200 {"nodes":[],"edges":[]}
GET    /api/sessions/nope/browser-shots  -> 200 {"shots":[]}
POST   /api/computer/approvals/nope      -> 200 {"status":"not_found"}
DELETE /api/documents|nope               -> 200 {"status":"ok"}
DELETE /api/schedule|nope                -> 200
DELETE /api/templates|nope               -> 200
GET    :8109/v1/templates/{missing}      -> 200 {"status":"not_found"}
```

The second line proves this is a defect and not a convention: one route, two handlers, opposite
behaviour. **Every client that checks `response.ok` renders "deleted" for something that never existed.**
The console's `api.ts` uses a `safe()` wrapper that inspects `d.status === "error"` in some places and
`r.ok` in others — so which failures surface depends on which helper the view happens to call.

**Fix:** return real status codes; keep the `{status, error}` envelope for *recoverable* conditions only.

## 3. SSE contracts — two vocabularies for one orchestrator

Observed on the wire:

| Endpoint | Frame types |
|---|---|
| `POST /api/chat` | `started`, `log`, `meta`, `done`, `status` |
| `POST /api/chat/simple/stream` | `started`, `status`, `delta`, `done`, `error` (per the docstring at `agent_server.py:24`) |
| `POST /api/agui` | AG-UI: `run_started`, `run_finished`, `run_error`, `step_started`, `text_start`, `text_content`, `text_end`, `tool_call_*`, `activity_snapshot` (`agui.py:75-132`) |

**Three different frame vocabularies for the same underlying orchestration.** `/api/chat` has no `delta`
frame — text arrives inside `log` — so a client wanting incremental rendering must scrape `log`. The
lightweight path has `delta` and `error`; the DAG path has neither.

**Measured contradiction within one response.** On a cancelled run the same response contained:
```
data: {"type":"log","text":"…\nFINAL: \n…"}                                  <- empty answer
data: {"type":"done","answer":"Stopped by operator before the run finished.","cancelled":true}
```
The legacy console and the `/api/events` rollup render the log; a client reading `done.answer` gets the
explanation. **The two disagree about the same run.**

**Recommended canonical schema** (one vocabulary, additive changes only):
`{type:"started", session_id, conversation_id, query, t}` → `{type:"delta", text}` →
`{type:"node", id, skill, status, elapsed_s}` → `{type:"status", elapsed_s}` (heartbeat) →
`{type:"usage", calls, in_tok, out_tok, cost}` → `{type:"done", answer, cancelled, session_id, error?}`.
Keep `log` as a deprecated passthrough for one release. `/api/agui` keeps its own vocabulary — that one is
a standard, not a local contract.

## 4. Idempotency

| Endpoint | Behaviour | Verdict |
|---|---|---|
| `POST /api/chat` with `idempotency_key` | Two POSTs, same key → **two sessions** (`s8-ee40156f`, `s8-d622d160`), two billed runs | **ignored** |
| `POST /api/chat/simple/stream` | Claims a key, never releases it (report 08 finding 6) → a repeat question is swallowed with "an identical run is already in progress" for 600 s | **leaked** |
| `POST /api/schedule` | Two identical `(query, when)` → same `sch-4b22817d` | **correct** — use this as the pattern |
| `POST /api/memory/remember` | Repeats append a new row | additive, acceptable |
| `POST /api/documents/{id}/reindex` | Idempotent (resets and re-embeds) | correct |

**Proposed contract:** `Idempotency-Key` header (not a body field), honoured for `POST /api/chat`,
`/api/chat/simple`, `/api/chat/simple/stream` and `/api/agui`; a completed key returns the original
`session_id` with `Idempotent-Replay: true`; keys TTL out (the `_IDEM_TTL_S` sweep at
`agent_server.py:4345-4369` already exists and is correct); keys are released on **completion**, not just
on failure.

## 5. Server/client drift

* **Memory field naming.** The wire protocol is `{kind, descriptor, keywords, value, source, run_id}` and
  `console-frontend/src/api.ts` matches it, but `GET /api/memory` returns items with keys exactly
  `['descriptor','drawer','id','keywords','kind','source']` — **no `text`/`value`**, so the operator cannot
  see what was stored. The console's own create path (`Memory.tsx:160`) correctly sends both `descriptor`
  and `value`, which is why writing works and reading back does not.
* **Document search.** Client sends `{query, top_k}` (`api.ts:598-600`); server also accepts `doc_ids` and
  `k`. `top_k` vs `k` is an unversioned alias.
* **Conversation vs session id.** The DAG path keys the per-turn ledger by the mapped `s8-…` session id
  while the lightweight paths key it by the conversation id (report 08). One client-visible concept, two
  identifiers, no field saying which is which.
* **Tool listing.** `GET /api/tools` returns the skill catalogue and none of the MCP tools that actually
  execute (`read_file`, `write_file`, `final_answer`, …). The console's tool-disable control therefore
  operates on a list that is not the executed set.

## 6. The gateway authentication contract — corrected

Three subagents initially reported the gateway control plane as unauthenticated, and I repeated that in an
earlier summary. **That was wrong for the current code.** Report 12 verified live that every `/v1/*` route
returns 401 without a header; auth is implemented in `llm_gatewayV9/gateway_auth.py` and enforced at
`main.py:283-304`. What is actually broken is token **coherence on rotation**:

* `gateway.py:67-79` — the pooled client's refresh guard is
  `elif token and "X-Gateway-Token" not in _CLIENT.headers`, but the header was added at construction, so
  **the branch can never fire again**; `_gateway_token()` re-reads the file per call and the result is
  discarded.
* `agent_server.py:3604-3631` — five ad-hoc `httpx.AsyncClient`s read the token file per call instead.

Result: the pool sends the token the gateway had at start-up; the ad-hoc clients send the current file
token. Observed split on one live instance: `/api/documents` → 401, `/api/memory` → 200. The gateway
restarted at 04:15:48; the token file was regenerated at 05:15:32 by a second process calling
`publish_token_file()`, and the two never reconciled.

**Also load-bearing:** `gateway_auth.py` is **untracked in git**. A fresh clone has no gateway
authentication at all. That is the real version of this finding.

**Fix:** one shared client, re-keyed when the token changes, plus a TTL; make `publish_token_file()`
refuse to overwrite when a live process already owns the token; commit the file.

## 7. Minor

* `:8109/v1/tts` with `{}` → **400 with an empty body**.
* `:8109/v1/chat` accepted a **200 000-character** message; only embeddings are capped.
* `:8109/v1/memory/search` with `{}` → `200 {"items":[]}` — an empty query is silently a no-op.
* `GET /api/health?x=…` accepts a 4 000-char query string (harmless, but no request validation exists on
  the agent's routes at all).

---

## Recommended work, ordered

| # | Fix | Effort |
|---|-----|--------|
| 1 | Default `/api/sessions/{id}/graph` to `light=true` | 10 min |
| 2 | Clamp every `limit`; return 400 (the `/api/sessions` clamp is already documented) | 30 min |
| 3 | Return real status codes instead of 200-with-error | 1–2 h |
| 4 | Summary list for `/api/templates`; cap template `name` at 64 chars | 30 min |
| 5 | One shared, re-keyed HTTP client; commit `gateway_auth.py` | 2 h |
| 6 | Adopt the canonical SSE schema in §3, deprecate `log` | 1 d |
| 7 | `Idempotency-Key` header honoured on completion across the four chat paths | 1 d |
| 8 | Union the two tool registries in `/api/tools`; expose `text` in the memory listing | 1 h |

**1 and 2 together are under an hour and remove the two largest payloads in the system.** A client that
forgets `?light=true` currently transfers 202 KB to render one DAG; a client that passes
`?limit=99999999` can make the server do unbounded synchronous work in one request. Both are one-line
defences that the code already documents and does not enforce.