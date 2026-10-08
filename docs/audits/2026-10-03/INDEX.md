# Audit batch — 2026-10-03

Twelve audits of the Aria agent + console, run black-box against the live services
(`127.0.0.1:8500`, `127.0.0.1:8109`). Read [`_BRIEFING.md`](./_BRIEFING.md) first — auth helper, hard
rules, and the already-known issues no report should re-raise.

**Status: 12 of 12 written.** Three by subagents, nine by the coordinator after the fleet began failing on
upstream idle timeouts. The agent on `:8500` went down mid-batch at ~05:30 and no restart was authorised, so
**§01–§04 and §06 are source analysis plus the measurements taken before the outage** — each says exactly
what it could and could not measure.

## Written

| # | Report | Author | Headline |
|---|--------|--------|----------|
| 01 | [Latency & throughput of `/api/*`](./01-performance-api-latency.md) | coordinator | A **`join(timeout=60)` blocks the event loop inside the `/api/chat` handler** (`:4790`), and synchronous `ensure_gateway()` runs in five `async def` routes — the mechanism behind both total outages |
| 02 | [Concurrency, load & resource leaks](./02-concurrency-load.md) | agent + coordinator | **At zero self-load `/api/health` was p50 2.1 s / p95 10.5 s** — latency is a function of total load; three of four chat paths cannot be cancelled |
| 03 | [Failure modes: slow, down or lying](./03-reliability-failure-modes.md) | coordinator | **`/api/health` reports `gateway_up: true` while every LLM call 401s**; 4.4 % hard failure rate in six repeat classes that the existing breaker never trips |
| 04 | [Console frontend performance](./04-frontend-performance.md) | coordinator | 24 static requests, 266 KB entry; three independent pollers on 3 s/15 s/15 s intervals; `/code` hangs silently instead of erroring |
| 05 | [Persistence, state and clock](./05-persistence-io.md) | coordinator | `state/templates.json` is **23.4 MB**; `/api/events` stamps jobs with a **future** `next_fire`; 894 KB of MCP pipe-leak warnings |
| 06 | [Response size, pagination, streaming contracts](./06-api-contract.md) | coordinator | `/api/calls` **~20 MB**, `/api/templates` **~16 MB**, graph **202 KB vs 5.5 KB** with `?light=true`; three SSE vocabularies for one orchestrator |
| 07 | [LLM cost & token efficiency](./07-llm-cost-efficiency.md) | coordinator | Prompt caching **off** (1 cache read in 5 000 calls); planner is **28 % of tokens on 7 % of calls**; **357 embeddings at 8 dimensions** |
| 08 | [New functional bugs](./08-new-functional-bugs.md) | agent + coordinator | **`use_documents: false` is ignored and fails OPEN** (`flow.py:356-363`); A2UI depth/text limits bypassable; 30 findings |
| 09 | [Optimiser section — research & design](./09-optimiser-section-design.md) | coordinator | An optimiser built today would **optimise a one-character bug into an architectural workaround** |
| 10 | [AG-UI / A2UI integration map](./10-agui-a2ui-integration-map.md) | coordinator | Both protocols **fully built** (12 events, 17 components, 4 routes); client references: **zero** |
| 11 | [Repository structure plan](./11-repo-structure-plan.md) | coordinator | Only **one** hard-coded path in the agent code; **`gateway_auth.py` is untracked** — a fresh clone has no gateway auth |
| 12 | [Documentation truth audit](./12-docs-truth.md) | agent | `README.md:103-105` says the gateway is unauthenticated and it isn't; **`BENCHMARKS.md` is entirely unreproducible** |

## Cross-cutting: the four things worth fixing first

1. **`mcp_runner.py:248`** — one line (`_os` used at :248, imported at :264). Restores every tool-using
   skill, and stops the recovery loop that burns 28 % of all tokens.
2. **The SPA catch-all does not inject `aria-token`** — two of twelve nav routes are dead, and they
   break authentication for the whole console session.
3. **A blocking `join(timeout=60)` and five synchronous `ensure_gateway()` calls in `async def` handlers** —
   the only two places found that freeze the entire service with no load at all.
4. **Commit the untracked product files, `gateway_auth.py` first** — a fresh clone currently has no
   gateway authentication and no React console.

## Conventions

New batches go in `docs/audits/YYYY-MM-DD/` with the same `_BRIEFING.md` and one file per agent, prefixed
with a two-digit ordinal so the folder reads in execution order. Filenames are kebab-case and end in the
topic, never in the tool used. **Write the report file first and fill it in** — three agents in this batch
lost everything by investigating first and timing out before writing (§05, §09, §10 all had to be
recovered or rewritten by the coordinator).

## Related

* [`../BREAK_TEST_2026-10-03.md`](../BREAK_TEST_2026-10-03.md) — the black-box break-test that surfaced
  the three P0s: the dead tool path (`mcp_runner.py:248`), the two unauthenticated console routes, and the
  policy engine reporting `allowed: true` for denials. (Currently still at the repo root as
  `AGENT_BREAK_TEST_REPORT.md`; §11 of this batch stages its move.)
* The mojibake repair of `S9SharedCode/code/agent_server.py` (4 411 C1 chars → 0, AST verified identical)
  happened before this batch and is not audited here.