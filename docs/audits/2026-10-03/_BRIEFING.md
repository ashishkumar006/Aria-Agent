# Audit batch 2026-10-03 — shared briefing (read this first)

You are one of **12 parallel audit agents** working on `C:\Users\AISHWARYA\Downloads\project3`
("Aria": a local two-service LLM agent — FastAPI agent/console on `:8500`, FastAPI LLM gateway on `:8109`).
Each agent owns exactly one report file in this folder. **Do not touch any other file in the repo.**

## Live services — both are RUNNING, leave them alone

* Agent console: `http://127.0.0.1:8500` (React SPA + ~83 `/api/*` routes)
* LLM gateway: `http://127.0.0.1:8109` (providers, cost ledger, memory, documents, policy, 16 channel adaptors)

* **NEVER** start, stop, restart or kill either service.
* **NEVER** run the pytest suite (`uv run python -m pytest tests/ -q` takes ~6 min and hits live services).
* The agent process loaded its code at **16:41**; a mojibake text repair to `agent_server.py` landed
  afterwards. On-disk source and the running process therefore differ in string/comment text only.
  Judge behaviour from the **live service**, and cite on-disk source for code claims.

## Authenticating against `/api/*`

Every `/api/*` route is gated by a per-launch token injected into the SPA shell as
`<meta name="aria-token">`. A helper that handles this for you (it never prints the token):

```python
import sys; sys.path.insert(0, r"C:\Users\AISHWA~1\AppData\Local\Temp\kilo")
import aria_probe as a
a.show("GET", "/api/tools")                       # -> status + clipped body
a.show("POST", "/api/code/check", {"path": "x.py", "text": "def f("})
st, txt = a.sse("/api/chat", {"query": "..."}, seconds=90)   # SSE reader
```
It also exposes `a.raw(method, path, body, timeout, base=a.GATEWAY)` and `a.token()`.
If the helper is missing, recreate it from that snippet — never read `state/agent.token` directly.
`/api/health` is the only unauthenticated route. Gateway `:8109` needs no token at all.

Put every scratch script in `%TEMP%\kilo\` — **never inside the repo** — and delete it when done.

## Hard rules

1. **Read-only on the repo.** The only file you may create or modify is the single report file named in
   your own task. No edits to source, no `git add/commit/checkout/stash`, no deletions.
2. **Never read**: `.env`, `.env.*`, `*.env`, `*secret*`, `*credential*`, `*token*`, `*.db`, `*.sqlite`,
   `*.faiss`, `*.onnx`, `*.bin`, `*.key`, `*.pem`, `*.p12`, `*.log`, `*.err`, `*.out`, `*.wav`.
   (You may *measure* the size of a `.db`; you may not read its contents. Prefer the gateway's own
   `/v1/spend` + `/v1/calls` endpoints for ledger data.)
3. **No destructive shared state.** Never wipe memory, delete sessions, purge schedules, or remove
   documents/templates/artifacts you did not create. Scratch resources must be prefixed `zzperf<tag>`
   and only those may be deleted.
4. **No real-world side effects.** Never send email / Telegram / Slack / Discord, never create calendar
   events, GitHub issues or Notion pages. Only exercise the fail-before-dispatch paths.
5. **Mind the wallet.** Short queries, at most ~6 real `/api/chat` runs per agent, and only where the
   measurement genuinely needs one. Most audits need zero.
6. **Expect instability.** Under load the agent has been observed going fully unresponsive for ~3
   minutes and self-recovering. Use short timeouts (5–10 s), retry once, and record failures as
   observations rather than assuming your probe was wrong.
7. **This instance is shared** — another harness has been running `zzqa-*` sessions and scheduler jobs
   concurrently. Don't be surprised by foreign sessions/events; note them, don't delete them.

## Already known — do NOT re-report these

* Every tool-using skill fails instantly with `UnboundLocalError: '_os'`
  (`S9SharedCode/code/mcp_runner.py:248` uses `_os`, imported at `:264`) → `researcher`/`retriever`/
  `action`/`coder` all die in 0.0 s → planner↔researcher recovery loop up to `MAX_NODES=60`.
* The `/documents` and `/code` console routes are served by the SPA catch-all **without** the
  `aria-token` meta, so every API call from those two pages 403s (the Code page hangs silently).
* Gateway `/v1/policy/evaluate` returns `allowed:true` alongside `action:"deny"` because the shipped
  config has `dry_run: true`.
* Memory-borne prompt injection: one `POST /api/memory/remember` permanently steers answers.
* `GET /api/cost` returns `{"rows":[],"totals":{...0}}` while `/api/cost/by_skill` is fully populated.
* 4 concurrent `/api/chat` runs push `/api/health` from ~20 ms to ~6 s; twice the whole agent
  (including static assets) was unresponsive for minutes, then self-recovered.
* Payloads are unbounded: `/api/templates` ≈ 16 MB, `GET :8109/v1/calls?limit=999999999` ≈ 20 MB,
  `/api/sessions/{sid}/graph` = 202 KB for one 31-node session (5.5 KB with `?light=true`).
* Scheduler accepts `when` values of `every 1s`, `in -1h`, `every 0h`, `in 999999999999999999999s`.
* `POST /api/notifications {}` writes junk and there is **no** delete route (405).
* Baseline numbers for comparison: `/api/health` ~20 ms warm, 186 runs recorded, ~$0.0000 spend
  (free-tier providers), gateway `gemini35lite*` pool.

If your task genuinely needs to confirm one of these, confirm it cheaply and move on.

## Report format (required)

Markdown, in this order:

1. `# <Title>` + one-paragraph summary
2. `## Scope & method` — exactly what you ran; include the real commands/requests so others can repeat them
3. `## Findings` — ordered most severe first. Each finding gets a heading, then
   **Evidence** (exact request → exact response / measured numbers), **Repro** (numbered steps),
   **Root cause** with `file:line` citations, **Impact**, **Severity** (`P0` critical / `P1` high /
   `P2` medium / `P3` low), and **Cheapest correct fix**.
4. `## Verified correct` — what you checked that is *not* broken. This matters; it stops the next agent
   re-testing it.
5. `## Recommended work, ordered` — a table: fix | file(s) | effort | risk | blocks what

Rules: cite `file:line` for every code claim; quote only the short fragment you need, never whole files;
give numbers, not adjectives ("p95 412 ms on `/api/code/files`", not "slow"). If you could not verify
something, say so explicitly rather than inferring it.

## Final message back to the coordinator

Keep it short: the single most important finding in one line, then up to 5 bullets for the rest, then
the absolute path of your report. Do not paste the report into the message.