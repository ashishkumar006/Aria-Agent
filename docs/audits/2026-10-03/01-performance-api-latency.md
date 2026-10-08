# Latency and throughput of the agent API — 01

*Written by the coordinator. **This report is source analysis, not a live profile.** The agent on
`:8500` went down at ~05:30 during the audit batch and no restart was authorised, so per-route timings
could not be taken. What *is* measured here comes from the gateway ledger (real per-call latencies) and
from first-hand break-test measurements taken before the outage. Everything else is `file:line`-cited
static analysis, and labelled as such.*

## What is measured, and what is not

| Layer | Status |
|---|---|
| **Per-LLM-call latency** | **Measured** — 5 000 ledger records, see below |
| **Per-`/api/*`-route latency** | **Not measured** — service unavailable. No route table is presented rather than presenting guesses |
| **Blocking work on the request path** | **Source-verified** — this is where the report concentrates, and it is where the answer is |

### Measured: LLM call latency (from the ledger capture)

| percentile | `latency_ms` |
|---|---|
| p50 | 1 454 |
| p90 | 4 808 |
| p99 | 12 263 |
| max | **60 493** |

Measured with no load attributable to this audit. Individual slow calls are visible in the capture — e.g.
a planner call at 4 115 in-tok took **13 251 ms**, against 1 616 ms for a 4 265-token planner call, so
provider variance dominates at the tail rather than prompt size.

### Measured: request-path stalls (break-test, before the outage)

| observation | value |
|---|---|
| `/api/health` at rest | ~20 ms warm |
| `/api/health` with 4 concurrent chats | p50 45 ms, worst **5 582 ms / 6 035 ms**, 1 timeout at 6 s |
| `/api/health` with zero load, 12 audit agents + an external harness sharing the instance | p50 **2 138 ms**, p95 **10 481 ms**, max **11 113 ms** |
| Full outage, twice | static pages unreachable too; once ~60 s, once **~3 minutes**; self-recovered both times |
| Partial response observed | `HTTP/1.1 200 OK` + `content-length: 52` with **no body** — headers flushed, then blocked |

---

## Findings

### F1 — A blocking `join(timeout=60)` sits inside the `/api/chat` handler — **P1**

`agent_server.py:4790-4791`:

```python
if _EMBEDDER_WARMUP_THREAD is not None and _EMBEDDER_WARMUP_THREAD.is_alive():
    _EMBEDDER_WARMUP_THREAD.join(timeout=60)
```

This is inside the main chat path, and `Thread.join()` **blocks the calling thread**. The handler is
`async def`, so for up to 60 seconds this stalls the entire event loop: no `/api/health`, no
`/api/events`, no other chat, nothing. The code is defensively correct — it only joins while warmup is
still running — and the surrounding comment explains why it exists (`:4788-4789`: *"cold load = 30-40s"*).
But an Ollama model loading on a cold start is exactly the moment a user sends their first request, and
the symptom is a console that looks dead rather than busy.

**Why this matters for the outages:** it is one of only two places I can find where a *single* request
can block the loop for tens of seconds without any load at all. The "headers but no body" observation is
consistent with a handler that produces its response object and then blocks before the body is written.
**Cheapest fix:** `await _aio.to_thread(thread.join, 60)` — or better, do not block at all: if warmup is
still running, proceed without it (the memory read already degrades) and let the daemon thread finish.

### F2 — Synchronous `ensure_gateway()` is called from five `async def` handlers — **P1**

`gateway.ensure_gateway()` is a **blocking** function (it probes the port and, if needed, launches the
process). It is called directly — not awaited, not offloaded — at:

| route | line |
|---|---|
| `POST /api/chat` (main DAG path) | `agent_server.py:4794` |
| `POST /api/chat/simple` | `:4147` |
| `POST /api/chat/simple/stream` | `:4441` |
| `POST /api/agui` | `:2323` |
| `POST /api/a2ui/generate` | `:2570` |

The module docstring at `:3680-3687` is candid about the intent: *"Eagerly launch the V9 gateway in a
background thread at import time so the first /api/chat request doesn't block up to 45s on a cold
gateway start. `ensure_gateway()` is idempotent — if the gateway is already up it returns instantly."*

So the design is: warm at import, cheap in the happy path, **up to 45 seconds of event-loop stall
whenever the gateway is unhealthy** — precisely when the console most needs to stay responsive, and
precisely when a user is most likely to retry, multiplying the stall. This is the second mechanism
behind the observed outages, and unlike F1 it recurs.

**Cheapest fix:** wrap all five in `await _aio.to_thread(ensure_gateway)`, or — better — have
`_warm_gateway()` publish a flag and have handlers check the flag (a non-blocking `is_up()` with a short
timeout) instead of calling `ensure_gateway()` per request. The warm thread already owns the cold start.

### F3 — Five routes construct a fresh `httpx.AsyncClient` per request instead of using the pooled client — **P2**

`async with httpx.AsyncClient(headers=_gw_auth_headers(), …)` at `:2022` (document upload), `:2111`
(document context search), `:3541` (voice bytes), `:3631` (voice proxy), `:3927` (STT).

The code is self-aware — `_gw_auth_headers()`'s docstring at `:3605-3608` says *"the five places here
build an `httpx.AsyncClient` directly and would otherwise send no"* auth header. Correct as far as auth
goes, but each construction means a fresh TCP connect plus TLS/handshake setup per call and no keep-alive
reuse, against a gateway on loopback where connection setup is pure overhead.

**This is also the mechanism behind the live outage I hit mid-batch**, which makes it worth fixing
regardless of latency: `gateway.py:67-79` pins the auth header into the pooled client at construction and
its refresh guard (`not in _CLIENT.headers`) can never fire, so the pool keeps a stale token forever —
while these five per-request clients re-read the token file every call and therefore disagree with it.
That is why `/api/documents` returned 401 while `/api/memory` returned 200 on the same instance.

**Fix:** one shared async client that is re-keyed when the token changes, plus a TTL, used by all five.

### F4 — `/api/events` re-reads and re-parses both whole log files on every poll — **P2**

`agent_server.py:946-947` tails `logs/agent.out` and `logs/agent.err` per request, stripping ANSI and
filtering uvicorn noise each time. The console polls this route every **3 s**. `logs/` is now 5.97 MB
and growing — largely from the MCP pipe leak (report 05 F5). So poll cost scales with log size and log
size scales with a defect. **Fix:** byte-offset tailing (the route already accepts `since`), and a size-
capped ring rather than an ever-growing file.

### F5 — Memory recall is a cross-process HTTP round trip on the critical path of every run — **P2**

`state/memory` does not exist; the drawers live in the gateway. Every run performs a `memory.read`
(the SSE log line reads `[memory.read] 8 hit(s) visible to every skill this run`), so recall sits in the
critical path of a chat turn and behind the gateway's token. Compounding it, `outcomes.py`'s design
comment states a tool-outcome write *"costs a gateway round-trip (classifier + embed, 3-7s)"* — queued to
a daemon thread so it is off the request path, which is the right call and worth preserving.

**Fix:** cache the drawer read per run (it already is computed once — 8 hits shared across all skills),
and make the warmup join non-blocking so a cold embedder does not serialise behind recall.

### F6 — `/api/sessions` parses a whole `graph.json` per row — **P2**

The code documents this itself at `agent_server.py:1346-1349`: *"each row parses a whole graph.json, so
`?limit=100000` was a one-request way to make the server parse every session ever recorded, synchronously
on the event loop."* The amplification was fixed; the per-row parse was not. The store holds **862 session
files / 135 session directories / 9.12 MB**, so the default list already parses every graph. Measured in
the break-test: `GET /api/sessions?limit=5` completed, and the Runs page header showed "186 runs ·
406 nodes".

**Fix:** write a small per-session summary (`session_id`, `query`, `status_counts`, `updated_at`,
`node_count`) when a run ends, and have the list route read summaries. That is the same fix the
`?light=true` flag already proves works for the graph route.

### F7 — Code-workspace routes re-walk the source roots per request — **P3**

`/api/code/tree` and `/api/code/files` build their file list from `_CODE_ROOTS`
(`:2749`, walked at `:2887` and `:3180`). Measured: `/api/code/files` transfers **80 KB** per call and the
search route scans ~400 files per query (`q=a` took 383 ms in the break-test). The tree only changes when
a developer edits a file, so it is a textbook cache candidate — keyed on the roots' max mtime.

### F8 — Unbounded response sizes are a latency problem, not just a contract problem — **P1**

Measured payload sizes from the break-test: `/api/templates` **~16 MB**; `:8109/v1/calls?limit=999999999`
**~20 MB**; `/api/sessions/{id}/graph` **202 KB** for one 31-node session; `/api/code/files` **80 KB**;
`/api/runs/summary` 55 KB. On loopback these are fast, but they are parsed and rendered by the browser on
every poll, and `/ledger` polls every 15 s. The template store is a single 23.4 MB file re-read and
re-parsed per request (report 05 F4). **Fix:** caps first (report 08 A10), then caching.

---

## Verified correct

* **`_session_cost_breakdown` is correctly off the event loop.** `agent_server.py:4872` wraps it in
  `await _aio.to_thread(...)`, and the comment at `:4867-4871` explains precisely why it must be:
  *"blocks the event loop for up to 5s on EVERY /api/chat request, which stalls /api/health, /api/events,
  TTS and every concurrent chat — the exact hazard the comments 20 lines above warn about."* This is a
  fix someone already made and documented; it is a model for F1 and F2.
* **The SSE heartbeat exists and is correct.** `:4886-4898` emits a `status` frame every
  `_CHAT_STREAM_HEARTBEAT_S`, with the rationale at `:4883-4885` that a silent node *"looks dead rather
  than busy"*. Good.
* **Tool-outcome recording is kept off the hot path** by design (`outcomes.py`), queued to a single
  daemon thread with failures swallowed and counted. The comment states the three constraints
  explicitly — never on the hot path, never break a run, never persist a secret.
* **Embedder warmup is asynchronous in intent** — `_warm_gateway()` and `_EMBEDDER_WARMUP_THREAD` are
  started on daemon threads (`:3695-3696`). The design is right; only the join in F1 is wrong.
* **Provider load balancing is even** (report 07 F9: 10.2 %–15.2 % across nine keys), so no single
  provider is a hot spot.

---

## Recommended work, ordered

| # | Fix | Finding | Effort | Risk |
|---|-----|---------|--------|------|
| 1 | Make the embedder-warmup join non-blocking (`to_thread`, or skip it) | F1 | 15 min | low |
| 2 | Replace the five per-request `ensure_gateway()` calls with a non-blocking `is_up()` check | F2 | 1 h | med — touches the chat pre-flight |
| 3 | Unify on one re-keyed async client; remove the five ad-hoc constructions | F3 | 2 h | med — also fixes the token-split outage |
| 4 | Cap every collection limit and the template name; return template summaries | F8 | 1 h | low |
| 5 | Byte-offset log tailing in `/api/events` | F4 | 1 h | low |
| 6 | Per-session summary files so `/api/sessions` stops parsing graphs | F6 | 2 h | low |
| 7 | Cache the code-workspace tree on roots mtime | F7 | 1 h | low |
| 8 | Add route-level timing middleware and expose p50/p95 per route | — | 2 h | low |

**Item 8 is what would make the next version of this report unnecessary.** There is currently no
per-route latency instrumentation anywhere in the agent, which is why this report had to be assembled
from a ledger capture and static reading. A few lines of middleware emitting a histogram per route — and
surfacing it on the existing `/api/health` — would turn every future performance question into a
one-line lookup instead of an audit.

## Not measured

Per-route latency percentiles, concurrent-load curves, frontend request/byte rates, and heap/DOM
behaviour all require the live service. Do not treat the absence of a route table as "no problems found"
— F1, F2 and F6 are individually sufficient to explain the observed multi-minute outage without any of
them being measured.