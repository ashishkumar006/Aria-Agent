# Failure modes when a dependency is slow, down or lying — 03

*Written by the coordinator. The assigned subagent timed out upstream. Findings combine the gateway ledger
capture (5 000 real call records, taken before the token clobber), first-hand break-test evidence, and
`file:line`-cited static reading. The agent on `:8500` went down mid-batch, so the live probing this report
originally called for was not possible — that is stated per finding.*

The question this report answers is not "what breaks" but **"when something breaks, does the system fail
loudly or lie?"** The answer, repeatedly, is: *it lies.*

---

## Failure-mode matrix

| Dependency | Slow | Absent | Garbage / lying | Loud or silent |
|---|---|---|---|---|
| **LLM gateway** | Chat streams emit a heartbeat every `_CHAT_STREAM_HEARTBEAT_S` (`:4886-4898`) — good | `ensure_gateway()` tries to launch it, blocking the loop up to 45 s (`:3680-3687`, report 02 F1) | **Silent.** `/api/health` reports `gateway_up: true` while every LLM call 401s | **Silent** |
| **Gateway auth token** | n/a | n/a | **Silent.** Pooled client pins a stale token forever; five ad-hoc clients disagree with it, so half the API 401s and half works | **Silent** |
| **SQLite ledger** | Not probed | `/v1/spend` unreachable during the clobber | `/api/cost` returns empty rows while `/api/cost/by_skill` returns 429 calls — **two cost endpoints disagree** | **Silent** |
| **On-disk state** | Graph rewritten per save (report 05 F3) | Session/graph reads wrapped in bare `except: continue` | Unparseable `graph.json` → empty status counts, no error | **Silent** |
| **Embedding model** | Cold Ollama load 30–40 s, joined synchronously (`:4790`) | `all embedders unavailable` after retries | **357 calls embedded at 8 dimensions** against a 768-dim store — wrong-shaped vectors accepted silently | **Silent** |
| **MCP subprocesses** | — | **Every tool-using skill fails in 0.0 s** | `UnboundLocalError: '_os'` — and **the agent answers anyway** | **Silent, and dangerous** |
| **Browser driver** | 20 s per node (measured) | — | — | Loud (node `failed` with the error) |
| **Policy engine** | — | — | Returns `allowed: true` alongside `action: "deny"` | **Silent, and dangerous** |
| **The model itself** | p99 12.3 s, max 60.5 s per call | — | **Fabricates citations and facts with total confidence** | **Silent, and dangerous** |

---

## Findings

### R1 — The monitoring endpoint reports "up" while the dependency is failing every call — **P0**

`gateway.py:82-98` `_is_up()` probes `/health`, which is deliberately unauthenticated (`main.py:1125-1135`).
So `ensure_gateway()` returns early and `GET /api/health` reports:

```
GET /api/health  ->  {"agent":"ready","gateway_up":true,"spa_built":true}
POST /api/chat/simple -> 502 "chat failed: Client error '401 Unauthorized' for url
                          'http://127.0.0.1:8109/v1/chat'"
```

**This was measured during this batch, by me, on a live instance.** The console's health dot is green
throughout. Any operator or agent relying on `/api/health` to decide whether the system works is being
told the opposite of the truth.

**Impact:** this is the single reason the outage I hit went unnoticed for ~15 minutes. It also defeats
the batch's own methodology: agents were told to check `/api/health` before concluding a service was down.
**Fix:** make the agent's health probe exercise an **authenticated** gateway call, and distinguish
`gateway_up` (listening) from `gateway_usable` (authed round trip succeeds). The break-test's
`GET /v1/config/keys` with a correct token is a suitable probe; `/health` is not.

### R2 — The system re-attempts permanently-failing operations instead of tripping a breaker — **P1**

From the ledger, 5 000 calls produced **222 errors and 296 non-`ok` statuses**, and the error strings
cluster into six repeated classes:

| count | error |
|---|---|
| 25 | `HTTPStatusError: Client error '401 Unauthorized'` |
| 25 | `GMAIL_TOKEN expired; refresh failed: HTTPStatusError` |
| 25 | `RuntimeError: telegram error 400: Bad Request: chat not found` |
| 21 | `all embedders unavailable. attempts=[{'provider': 'ollama', …}]` |
| 21 | `unknown embedder 'gemini'` |
| 21 | `role 'agent' may not write drawer 'playbook' (allowed: ['gat…'])` |

Every one is a **configuration or authorisation fault, not a transient one**. None is a timeout or a
network blip. And `retries > 0` appears on only **7** rows — so these are not retry loops inside one
call; they are **21–25 separate callers independently making the same doomed request**. The system is
paying, in full, for a request it already knows cannot succeed.

Meanwhile `breaker_stats()` is implemented and already surfaced at `agent_server.py:631` (`/api/mcp/stats`
reports `mcp_breaker`). **The breaker exists and is not being applied to these classes of failure.**

**Fix:** route the six classes above to the existing breaker rather than to blind calls. `unknown embedder
'gemini'` and `role 'agent' may not write drawer 'playbook'` should be *startup* errors, not per-call
ones — they are deterministic and detectable before any traffic.

### R3 — A dependency returns wrong-shaped data and nothing checks the shape — **P1**

**357 of 523 embedding calls used an 8-dimensional embedder** (report 07 F5); the store is built for
`nomic-embed-text`'s 768. In 8 dimensions, semantic retrieval is close to meaningless — and the Memory
and document drawers returned confident, unrelated rows for arbitrary queries
(`GET /api/memory?q=<a string that does not exist>` returned three unrelated facts).

The system recorded `embed_dim` on every single ledger row and nobody looks at it.

**Fix:** validate the dimension at embed time against the configured dimension and refuse loudly; alert on
`embed_dim != configured`. This is the clearest example in this report of a dependency *lying* — returning
success with data of the wrong shape.

### R4 — Total tool failure degrades to a confident answer instead of an error — **P0**

The headline P0 from the break-test, restated as a reliability property rather than a bug:
`mcp_runner.py:248` raises `UnboundLocalError` before every MCP session is created, so **every**
tool-using skill fails in 0.0 s. The orchestrator replans in a loop until `MAX_NODES=60` and then returns
whatever it has — in several cases a *planner-generated answer with an invented citation*.

**Reliability framing:** the system's designed response to "my tools are all dead" is to keep asking the
model until it produces something. That is the worst possible failure mode for an agent — it converts a
hard outage into a silent wrong answer. Observed directly:
- "Who won the 2026 F1 championship? One sentence with a source URL." → an answer with
  `(Source: https://www.formula1.com)` — a URL never fetched.
- "Exact population of Mars on 3 March 2027 at 14:05 GMT, to the person?" → "The population of Mars is
  **zero**."
- "What colour is the sky on Tuesdays?" → returned a different concurrent run's answer token.

**Fix:** if every tool-using node in a run failed, the run must terminate with an explicit error, not a
planner fallback. This is the same guard report 09 makes load-bearing in the Optimiser design.

### R5 — A safety control reports success while denying the action — **P0**

`policy.yaml` ships `dry_run: true`, and the engine returns the action it *would* take with
`allowed: True`:

```
POST :8109/v1/policy/evaluate {"action":"delete_file","channel":"webhook","trust":"untrusted"}
 -> {"allowed":true,"action":"deny","rule_id":"deny-untrusted-default", … ,"dry_run":true}
```

**Impact:** any caller branching on `allowed` — rather than reading `action` — proceeds with a denied
action, in the default configuration. The policy engine is in the path and evaluating (153 calls in the
capture carried verdicts: 100 allow, 53 deny), so this is not bypassed; it is mis-reported.
**Fix:** `allowed` must reflect the verdict. Expose dry-run as a separate field.

### R6 — Reads wrapped in bare `except` turn corruption into emptiness — **P2**

`/api/events` wraps each session read in `try/except: continue` (`agent_server.py:893-919`) — a corrupt
or unreadable `graph.json` yields a session that simply does not appear in the feed. `/api/sessions`
likewise. The operator sees a missing run, not an error.
**Fix:** count and surface skipped records; return a partial result with an explicit `skipped` count.

### R7 — Gateway accepts unbounded input and returns empty-bodied errors — **P3**

* `:8109/v1/chat` accepted a **200 000-character** message and sent it to the provider. Only embeddings
  have a cap (`MAX_INPUT_CHARS`); chat does not.
* `:8109/v1/tts` with `{}` → **HTTP 400 with an empty body**, so a client has nothing to display.

---

## Verified correct

* **Genuine input validation at the gateway.** 15 malformed bodies produced precise 422s with field
  paths (`{"type":"list_type","loc":["body","messages"]}`) — materially better than the agent's own
  layer, which returns a bare 500 on a type mismatch (report 08 A6).
* **The breaker machinery exists and is exposed** (`agent_server.py:631`) — R2 is about applying it, not
  building it.
* **Offline degradation is explicit where it was designed.** When the gateway is genuinely absent, the
  chat path yields a `[offline] gateway down — answered via gated-shell fallback (no LLM)` log frame and a
  `session_id` of `s8-offline` (`:4801-4805`), and reports `gateway_up: false`. That is the right shape —
  it is R1's *unauthenticated probe* that fails to detect the partial failure case.
* **Tool-outcome writes are designed to never break a run** — `outcomes.py` states "every failure here is
  swallowed and counted". Swallowing *and counting* is correct; the count is what is missing from every
  other swallow in this report.
* **Credential hygiene holds.** `.env`/`.db`/`.token` are refused from the code workspace by extension as
  well as by path, and `outcomes.py` redacts arguments and truncates results before persisting, because
  *"tool arguments can contain OAuth tokens, API keys and message bodies"*.
* **Gateway streaming is well-formed** — `stream: true` emits clean `delta` + `done` frames.

---

## Recommended work, ordered

| # | Fix | Finding | Effort |
|---|-----|---------|--------|
| 1 | Authenticated gateway probe; split `gateway_up` from `gateway_usable` | R1 | 1 h |
| 2 | Terminate a run with an explicit error when every tool-using node failed | R4 | 2 h |
| 3 | Make policy `allowed` reflect the verdict | R5 | 30 min |
| 4 | Route the six repeat-failure classes to the existing breaker; fail fast at startup | R2 | 2 h |
| 5 | Validate embedding dimensions at write and read | R3 | 30 min |
| 6 | Surface `skipped`/`swallowed` counts instead of silently dropping records | R6 | 1 h |
| 7 | Cap chat input; return a JSON body for `/v1/tts` errors | R7 | 30 min |

**The through-line:** four of the seven are *reporting* failures — the system knows, and does not say.
`agent.err` had 894 KB of ResourceWarnings and a counter existed in `outcomes.py`; `/api/mcp/stats`
exposed a breaker that was never tripped; the ledger recorded `embed_dim` on every row; the policy engine
recorded `action: "deny"` next to `allowed: true`. In every case the information needed to fail loudly was
already being produced and was being discarded.