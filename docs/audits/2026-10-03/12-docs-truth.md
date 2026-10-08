# Documentation truth audit

Audit of `README.md`, `ARCHITECTURE.md`, `ARCHITECTURE_V2.md`, `AGENTS.md`, `BENCHMARKS.md`,
`COMPUTER_USE_ARCHITECTURE.md`, `ADAPTOR_KEYS_GUIDE.md`, `llm_gatewayV9/{README,ARCHITECTURE,
ADAPTORS}.md` and the `agent_server.py` module docstring against on-disk source and the two live
services (`:8500`, `:8109`). Read-only: nothing in the repo was modified, no service was restarted,
no test suite or build was executed. **Headline:** the prose is mostly honest — the port table, the
rate-limit table, the 13-skill count, the frontend stack versions and the 16-adaptor count all check
out exactly — but the *numbers* and the *security section* have drifted badly. Six concrete
falsehoods: `README.md:103` claims the gateway `/v1/*` plane has no authentication when every
`/v1/*` route returns `401` without `x-gateway-token`; `llm_gatewayV9/ARCHITECTURE.md:42` says the
gateway binds `0.0.0.0` when it binds `127.0.0.1`; `agent_server.py:18` claims 83 routes when the
live agent serves 106 (80 under `/api/*`); "37 tools" is wrong in both architecture docs (real: 38
in `_TOOL_CATALOG`, 35 in `mcp_server.py`); `BENCHMARKS.md` claims 178 captured sessions when
`state/sessions/s8-*` holds 135 and the script it names for reproduction (`_full_benchmark.py`) does
not exist; and `COMPUTER_USE_ARCHITECTURE.md` is corrupted from line 65 onward so §4–§12 do not
render.

## Scope & method

Read-only verification performed:

```powershell
# route inventory, static source + live OpenAPI
Select-String -LiteralPath "S9SharedCode\code\agent_server.py" -Pattern '@app\.(get|post|put|delete|patch|websocket)\('
Select-String -LiteralPath "S9SharedCode\code\agent_server.py" -Pattern '@app\.(get|post|put|delete|patch|websocket)\("/api'
# live route tables (no token sent)
python -c urllib -> http://127.0.0.1:8500/openapi.json , http://127.0.0.1:8109/openapi.json
# live gateway auth posture (no headers at all)
GET http://127.0.0.1:8109/v1/providers | /v1/channels | /v1/embedders | /v1/status | /v1/routers | /v1/capabilities | /v1/control/pair
# live agent auth posture
GET http://127.0.0.1:8500/api/health   (200, no token)
GET http://127.0.0.1:8500/openapi.json (200, no token)
# tool inventory
GET http://127.0.0.1:8500/api/tools   (via aria_probe, token never printed)
# files
Get-ChildItem -Recurse -Filter "test_*.py" S9SharedCode\code\tests ; llm_gatewayV9\tests
Get-ChildItem llm_gatewayV9\adaptors ; console-frontend\package.json ; *.pyproject.toml
```

Scratch scripts lived in `%TEMP%\kilo\` and were deleted afterwards. `aria_probe` supplied the
agent's per-launch token for `GET /api/tools`; the **gateway token was never obtained**, because the
briefing forbids reading `*token*` files (`llm_gatewayV9/state/gateway.token`), so every
authenticated-gateway claim below is marked **unverifiable** rather than guessed.

**Mid-audit service state change (observation, not a finding).** All measurements in this report
were taken while both services were healthy: `GET :8500/api/health` and `GET :8500/openapi.json`
returned 200 with no token, `GET :8500/api/tools` returned 38 tools, and `GET :8109/openapi.json`
returned a 44,923-byte schema. In the last ~4 minutes of the audit, `GET :8500/api/health` and
`GET :8500/research` began timing out at 8 s, then `GET :8109/openapi.json` began returning
`ConnectionRefusedError (10061)`. Both are consistent with the batch-level saturation the coordinator
measured (`/api/health` p50 2138 ms / p95 10481 ms with zero load from that agent) plus the known
self-recovering unresponsiveness in `_BRIEFING.md:66-69`. Per briefing rules 12 and 6 I did **not**
restart, kill, or otherwise touch either service, and no measurement below depends on the post-hoc
state. Re-verification of any number in this report requires a healthy instance.

**Baseline caveat.** Per the coordinator's mandatory methodology, the absolute timings above were
measured at the start of the audit window; I did not take a same-minute zero-load baseline series, so
I report them only as liveness evidence (200 vs 401 vs timeout), never as latency figures.

## Findings

### F1 — `README.md` "Known gaps" is factually inverted: the gateway control plane *is* authenticated

**Evidence.** `README.md:103-105`:

> "**Gateway `/v1/*` has no authentication.** The agent token guards `:8500`, but every gateway
> route is reachable unauthenticated on its port."

Live, with **no headers sent at all** (raw `urllib`, not `aria_probe`, so no token was attached):

| Request | Status | Body |
|---|---|---|
| `GET :8109/v1/providers` | **401** | `{"error":"missing or invalid x-gateway-token header"}` |
| `GET :8109/v1/channels` | **401** | same |
| `GET :8109/v1/embedders` | **401** | same |
| `GET :8109/v1/status` | **401** | same |
| `GET :8109/v1/routers` | **401** | same |
| `GET :8109/v1/capabilities` | **401** | same |
| `GET :8109/v1/control/pair` | **401** | same |
| `GET :8109/openapi.json` | 200 | full 55-path schema (documented, deliberate) |

The on-disk control is `llm_gatewayV9/main.py:283-304`: `_OPEN_PATHS = {"/", "/health", "/healthz",
"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc", "/favicon.ico"}` and the
`_gateway_token_guard` middleware 401s everything else. `gateway_auth.py:88-99` (`is_enabled()`
returns `True` unconditionally; `hmac.compare_digest`) and `gateway_auth.py:111-128`
(`assert_safe_bind`) mean enforcement is never inert and a non-loopback bind without a configured
`GATEWAY_V9_TOKEN` refuses to start. The module docstring at `gateway_auth.py:5-23` documents the
exact incident that motivated the fix.

**Why this measurement is not affected by the mid-batch token clobber.** Three sibling agents reported
(board, 2026-10-04 ~05:15Z) that `llm_gatewayV9/state/gateway.token` was regenerated while the live
gateway (pid 29124, started 04:15:48) kept its original in-memory token, so a caller reading the
token *file* also gets 401. **My probe deliberately sent no token at all**, so it is immune to that
clobber: an unauthenticated caller either passes a gate that does not exist (→ 200/404) or does not
(→ 401). Every `/v1/*` route returned 401 with a body byte-identical to the string literal at
`main.py:302`. That is direct behavioural proof that the running gateway process enforces the token,
independent of which token value is correct. Two further notes for the record: `gateway_auth.py` is
**untracked in git** (`git status` → `?? llm_gatewayV9/gateway_auth.py`), i.e. the fix that makes
`README.md` wrong is itself uncommitted, which is the mechanical reason the doc was never updated;
and sibling agents report the live process predates the on-disk `/static` exemption at
`main.py:291-293`, so disk and live differ on *which* paths are open — not on *whether* the gate
exists.

**Root cause.** The `README.md` "Known gaps" list was written before `gateway_auth.py` landed and was
never updated. This is the single worst falsehood in the docs: it makes an operator *add* fronting
work for a hole that is closed, while giving a false sense that `/v1/memory/sweep` and
`/v1/control/deploy-test` are one `curl` away — the failure mode `gateway_auth.py:9-13` records.

**Impact.** Overstated risk; misdirected hardening effort; and the doc actively names routes that the
audience will now believe are open. **Severity: P0** (documented security posture is wrong).

**Cheapest correct fix.** Delete the first bullet of `README.md` "Known gaps" and replace the
security section with the current truth:

```
- **Gateway `/v1/*` requires `x-gateway-token`.** The gateway generates a
  per-launch token (`GATEWAY_V9_TOKEN` if set) and 401s every `/v1/*` route
  without it; `/`, `/health`, `/docs`, `/redoc` and `/openapi.json` stay open
  deliberately. A non-loopback `GATEWAY_HOST` bind without a configured
  `GATEWAY_V9_TOKEN` refuses to start. See `llm_gatewayV9/gateway_auth.py`.
```

### F2 — `agent_server.py:18` "83 routes" is wrong; the live agent serves 106

**Evidence.** `agent_server.py:18`: `Endpoints (83 routes; the full list is the code — this is the shape):`

| Measure | On disk | Live `GET /openapi.json` |
|---|---|---|
| `@app.<verb>` decorators, total | 106 | 106 operations |
| decorators whose path starts `/api` | **80** | **80** |
| non-`/api` (static + SPA shell) | 26 | 26 |
| unique paths | — | 92 |
| verb split | — | 68 GET / 30 POST / 8 DELETE |

**Root cause.** Hand-maintained count in a docstring, drifting as routes were added (the largest
recent additions are the 7 `/api/apps*` routes and 7 `/api/templates*` routes).

**Impact.** Low direct impact, but this is the number a new operator quotes when reasoning about the
API surface, and the briefing itself repeats "~83 `/api/*` routes" as if authoritative.
**Severity: P2.**

**Cheapest correct fix.** `agent_server.py:18` →
`Endpoints (106 routes — 80 under /api/*, 26 SPA shell/static; the full list is the code — this is the shape):`

Better still, replace the hand count with a pointer: `Endpoints (the full list is the code; GET
/openapi.json for the machine-readable table — this is the shape):`

### F3 — "37 tools" is wrong in two architecture docs; the real numbers are 38 and 35

**Evidence.**

| Claim | Location | Truth |
|---|---|---|
| "mcp_server.py — MCP tool surface (stdio) — 37 tools, incl. Tier-1" | `ARCHITECTURE.md:27` | **35** functions carry `@mcp.tool` in `mcp_server.py` |
| "`_TOOL_CATALOG` schemas (37 tools)" | `ARCHITECTURE_V2.md:91` | **38** entries in `skills.py:544` `_TOOL_CATALOG` |
| — | — | `GET /api/tools` (live) returns **38** tools |

The 3 catalog entries with no `@mcp.tool` implementation are `fetch_url`, `fetch_pdf`,
`wayback_fetch`: they are in `_TOOL_CATALOG` (so `agent_config.yaml` accepts them for `researcher`)
but are served by the gateway plane instead of the agent's stdio MCP server. So `37` is wrong in
**both** directions depending on which file you read.

**Root cause.** No single source of truth for the tool count; `_TOOL_CATALOG` and the `@mcp.tool`
set have legitimately diverged and the docs quote a number that matched neither.

**Impact.** A new operator grepping `mcp_server.py` for a documented tool finds nothing; the
researcher skill is configured with 3 tools (`agent_config.yaml:47-56`) that the local MCP server
cannot dispatch. **Severity: P2.**

**Cheapest correct fix.**

- `ARCHITECTURE.md:27` → `mcp_server.py         # MCP tool surface (stdio) — 35 tools, incl. Tier-1`
- `ARCHITECTURE_V2.md:91` → `` schemas (38 tools in `_TOOL_CATALOG`; 35 registered in `mcp_server.py`). ``

### F4 — `llm_gatewayV9/ARCHITECTURE.md:42` says the gateway binds `0.0.0.0`

**Evidence.** `llm_gatewayV9/ARCHITECTURE.md:42`:

> "Entry: `uv run main.py` → uvicorn on `0.0.0.0:$GATEWAY_V9_PORT` (default **8109**)."

Actual, `llm_gatewayV9/main.py:1152`:

```python
_host = _os.environ.get("GATEWAY_HOST", "127.0.0.1").strip() or "127.0.0.1"
```

and `main.py:1161` `uvicorn.run("main:app", host=_host, port=PORT, reload=False)`. Default bind is
loopback; `GATEWAY_HOST` is the override. This also contradicts the same file's own security section
(`ARCHITECTURE.md:201` "Control (loopback-only)") and `gateway_auth.py:19-23`.

**Impact.** Tells an operator the gateway is already LAN-exposed (it is not) and that loopback is a
policy rather than the default. **Severity: P1.**

**Cheapest correct fix.** `llm_gatewayV9/ARCHITECTURE.md:42` →
`Entry: \`uv run main.py\` → uvicorn on \`$GATEWAY_HOST\`, default **127.0.0.1**, port \`$GATEWAY_V9_PORT\` (default **8109**). A non-loopback bind without a configured \`GATEWAY_V9_TOKEN\` refuses to start (\`gateway_auth.assert_safe_bind\`).`

### F5 — `static/help.html` advertises three default models the code no longer uses

**Evidence.** The gateway help page is served at `/help` and is the page an operator reads to pick a
provider. Its "★ default" markers disagree with `providers.py`:

| Provider | `help.html` says | `providers.py` default | `llm_gatewayV9/ARCHITECTURE.md:127-134` says |
|---|---|---|---|
| gemini | `gemini-3.1-flash-lite-preview` (`static/help.html:105`) | `gemini-2.5-flash` (`providers.py:1238`) | `gemini-2.5-flash` ✓ |
| groq | `llama-3.3-70b-versatile` (`static/help.html:135`) | `openai/gpt-oss-120b` (`providers.py:1249`) | `openai/gpt-oss-120b` ✓ |
| cerebras | `zai-glm-4.7` (`static/help.html:150`) | `gpt-oss-120b` (`providers.py:1258`) | `gpt-oss-120b` ✓, with a note that `zai-glm-4.7` was archived 2026-09 |

`static/help.html` also lists `llama3.1-8b` era router lore via the cerebras section and does not
mention `kilo`/`gemini35lite` defaults at all.

**Root cause.** `help.html` is hand-maintained static HTML; `providers.py` env-overridable defaults
moved on. The two `*_MODEL` env vars still win at runtime, so the code is right and the page is
stale — but the page is the one a browser user reads.

**Impact.** An operator pinning a model from `/help` gets a model the gateway will not send by
default; cerebras `zai-glm-4.7` is documented in `providers.py:1301`-adjacent comments as archived.
**Severity: P1.**

**Cheapest correct fix.** Regenerate the three `★ default` markers in `static/help.html` from
`providers.py` at build time, or at minimum replace them with
`gemini-2.5-flash`, `openai/gpt-oss-120b`, `gpt-oss-120b`, and add
`cerebras: zai-glm-4.7 archived 2026-09; this account currently 402s on all Cerebras models`.

### F6 — `COMPUTER_USE_ARCHITECTURE.md` is corrupted mid-file and internally contradicts `ARCHITECTURE.md`

**Evidence.** Literal text damage, not just staleness:

* `:65` — the fenced block in §4 opens with a single backtick `` ` `` and never closes.
* `:85` — the §5 trap-table Python block opens with `` `python `` instead of ` ```python `.
* `:129` — "L2b LLM emits `ct` (element_index)" — the leading `a` is gone; should be `act`.
* `:141` — "`gent_config.yaml: computer entry`" — should be `agent_config.yaml`.
* `:161` — "Windows uses `ring_to_front`" — should be `bring_to_front`.
* `:173` — §12 item 5 starts with a stray backtick and `:174` with a stray backtick
  ("`Permission pre-flight` — capabilities()…"). Several §12 bullet lines are shifted by one.

Layer-count contradiction: `COMPUTER_USE_ARCHITECTURE.md:50` heads the section "The **four**
perception layers" and lists L1/L2a/L2b/L3. `ARCHITECTURE.md:178-187` describes a **five**-level
cascade `L0 (gated-shell) → L1 → L2a → L2b → L3`, and the live `computer` skill's L0-shell result is
what `BENCHMARKS.md:67` counts as a real layer. `ARCHITECTURE.md:189-191` gives `max_turns`
default 12.

**Root cause.** A past mojibake/encoding repair dropped the first character of a run of lines and ate
fence markers. (The same repair is described in `_BRIEFING.md:14-16` for `agent_server.py`; this file
evidently took the same hit and was not reviewed.)

**Impact.** Rendered Markdown is broken from line 65 onward: every code block after §4 swallows the
rest of the document. A reader cannot reach §8–§12 at all. **Severity: P1** for the corruption,
**P2** for the layer-count mismatch.

**Cheapest correct fix.** Re-apply the fence markers and restore the three truncated words
(`act`, `agent_config.yaml`, `bring_to_front`); retitle `:50` to "The four LLM layers" or add the L0
row so both docs agree; state `max_turns` default once, in `ARCHITECTURE.md`, and cross-reference.

### F7 — `README.md:45` tells you to copy `.env.example`, but `.env.example` is not where it says for both services

**Evidence.** `README.md:45`: "Copy `.env.example` to `.env`". Both services do have one:
`S9SharedCode/code/.env.example` and `llm_gatewayV9/.env.example` (neither is read here). So the
instruction is correct but **underspecified** — it does not say you need *two*, one per service
directory, and `llm_gatewayV9/README.md:41-42` documents a *third* load path (gateway `.env` first,
then the **parent project** `.env` as non-overriding fallback). `ARCHITECTURE.md:50` and
`llm_gatewayV9/ARCHITECTURE.md:46` repeat the two-file story.

There is no `.env.example` at the workspace root, so a reader who creates the root one only (the
reading `README.md:45` most naturally invites) gets a parent-fallback file and no provider keys.

**Impact.** A new operator following `README.md` verbatim ends up with an empty pool.
**Severity: P2.**

**Cheapest correct fix.** `README.md:45` →

```
Copy `llm_gatewayV9/.env.example` to `llm_gatewayV9/.env` and
`S9SharedCode/code/.env.example` to `S9SharedCode/code/.env`, then fill in
whichever provider keys you have — the gateway fails over across whatever is
configured, and runs fully on Ollama alone.
```

### F8 — `AGENTS.md` runbook: the prescription is coherent, with two caveats

Checked read-only; **no test suite or build was run**.

| `AGENTS.md` claim | Verdict | Evidence |
|---|---|---|
| Agent dir `S9SharedCode/code/` holds `agent_server.py`, `flow.py`, `skills.py`, `console-frontend/` | **true** | all four exist |
| Gateway dir `llm_gatewayV9/`, own venv | **true** | `.venv/` present in both |
| Agent on `:8500` via `uv run agent_server.py` | **true** | `agent_server.py:5188-5241` is a `__main__` script calling `uvicorn.run` |
| Gateway on `:8109` via `uv run main.py` | **true** | `main.py:1161`; port from `GATEWAY_V9_PORT`, default 8109 (`main.py:63`) |
| Tests: `uv run python -m pytest tests/ -q` from each dir | **true** | `tests/` exists in both (58 and 33 `test_*.py` files); `[tool.pytest.ini_options]` present in both `pyproject.toml` |
| "NOT bare `uv run pytest` — resolves the wrong interpreter, fails on `numpy`" | **unverifiable (plausible)** | requires executing `uv run pytest`; not run. Both `pyproject.toml` declare `numpy>=1.26`, so the failure mode is consistent with the claim, but nothing on disk proves the shim behaviour. |
| Frontend: `npx tsc --noEmit` then `npm run build` from `console-frontend/` | **true** | `console-frontend/package.json` has `build: "tsc && vite build"` and `typescript ~6.0.2` in devDependencies; no `typecheck` script exists, so the bare `npx tsc` form is the only correct one |
| Frontend tests: `npx playwright test` (`README.md:93`) | **true** | `@playwright/test ^1.63.0` + `test: "playwright test"` script |
| Restart = kill listener, start fresh; verify `/api/health` + `/v1/providers` | **true, with a gap** | live `GET :8500/api/health` → `{"agent":"ready","gateway_up":true,"spa_built":true}` ✓. But `/v1/providers` now needs `x-gateway-token` (`F1`) — the verification command in `AGENTS.md:12` will return **401** for an operator following it literally. |
| PowerShell 5.1 rules (`;` not `&&`, `>` writes UTF-16, `python -c` quoting) | **true** | consistent with the shell in use; no doc contradicts them |
| Never read `.env` | **true** | `.opencode/plugins/env-protection.js` exists per `AGENTS.md:23` (plugin file present) |

**Caveat 1 (P2).** `AGENTS.md:12` and `ARCHITECTURE_V2.md:40` both name `/v1/providers` as the
gateway verification endpoint. It is now token-gated. Correct wording:
`verify with \`/api/health\` (agent) and \`/v1/providers\` **with \`x-gateway-token\`** (gateway —
it is 401 without it, see llm_gatewayV9/gateway_auth.py).`

**Caveat 2 (P3).** `AGENTS.md:17` and `ARCHITECTURE_V2.md:298` give suite outcome counts
("372 passed, 36 skipped", "174 passed + 2 known cred fails"). Test *files* are 58 (agent) and 33
(gateway); the pass/skip counts cannot be checked without running the suites, which this audit is
forbidden to do. **Unverifiable.** A count this precise will silently rot; prefer "58 test files".

### F9 — `BENCHMARKS.md` cites 178 sessions; **135** `s8-*` session directories exist on disk

**Evidence.**

| Source | Sessions |
|---|---|
| `BENCHMARKS.md:4` — "**178 captured sessions** (`state/sessions/s8-*`)" | 178 |
| `BENCHMARKS.md:19` — "Sessions analyzed \| 178" | 178 |
| `BENCHMARKS.md:22` — "Sessions touching the computer skill \| 178 (100% of logged sessions)" | 178 |
| `BENCHMARKS.md:85` — "Baseline computer-session completion rate: 16.9% (30/178)" | 178 |
| `Get-ChildItem S9SharedCode\code\state\sessions -Directory -Filter "s8-*"` | **135** |
| same, all directories in `state/sessions/` | 194 (`s8-*` 135, `ct-*` 0, `zz*` 0) |

So the dataset the entire file is built on is **24% smaller than advertised**, and the four places
that quote 178 all inherit the same error. Every derived statistic (completion rate 16.9%, the
1,361 node count, the 273 computer-node count, every p50/p90 in §2) is stated as computed from a
178-session population that cannot be reproduced from the tree.

Two readings are possible and I cannot distinguish them read-only: either sessions were pruned
after the benchmark ran (in which case the file is documenting a corpus that no longer exists and
should say so), or the 178 figure was never derived from `state/sessions/` at all (in which case it
was wrong when written). **Either way it is not re-derivable today.**

**Root cause.** A hand-copied headline number with no command that regenerates it — compounded by
F9.1 below, the regeneration script not existing.

**Impact.** `BENCHMARKS.md` is the only doc in the repo that offers quantitative assurance. A 24%
corpus discrepancy, plus a "100% of logged sessions" claim that the same file contradicts at `:79`,
means the file's authority should not be relied on. **Severity: P1.**

**Cheapest correct fix.** Re-run the aggregation over the 135 sessions that exist and replace every
`178` with the new figure and every derived percentage with the recomputed one; or, if the original
178-session corpus was deleted, add a dated banner to the top:

```
> **Dataset note (2026-10-03):** the original 178-session corpus is no longer
> present — `state/sessions/s8-*` currently holds 135 directories. The figures
> below are the original run's numbers and are no longer re-derivable from the
> tree. Do not quote them as current.
```

### F9b — `ADAPTORS.md` numbers 16 channels but section-numbers to 19, and its "LIVE" claims contradict `ADAPTOR_KEYS_GUIDE.md`

**Evidence.**

* `llm_gatewayV9/ADAPTORS.md:3` — "All **16** messaging surfaces live behind one contract".
  `adaptors/registry.py:13-33` has exactly 16 `_BUILDERS`. So 16 is right and
  `llm_gatewayV9/ARCHITECTURE.md:174` agrees.
* But the document's own headings run 1, 2, 3, 4, "5–6", 8, 9, 10, "11–12", 13, 14, 15, "16–17",
  "18–19" (`ADAPTORS.md:40,56,76,83,89,104,109,116,121,131,137,144,150,157`). There is **no §7**, and
  the sequence ends at 19 for a 16-item population.
* Cause: §3 GitHub (`:76`) and §4 "Web search (Tavily + DDG)" (`:83`) are **gateway integrations**,
  not channel adaptors — they live in `llm_gatewayV9/integrations/{github,websearch}.py` and appear
  in **no** `_BUILDERS` entry. `ADAPTORS.md` merges 16 adaptors and 6 integrations under one heading
  and then numbers the union.
* Status contradiction between the two channel guides:

| Channel | `ADAPTOR_KEYS_GUIDE.md` | `ADAPTORS.md` |
|---|---|---|
| discord | "unkeyed" (`:24`) | "SEND LIVE" (`:121`) |
| matrix | "unkeyed" (`:29`) | "SEND LIVE" (`:121`) |
| line | "unkeyed" (`:34`) | "SEND LIVE" (`:131`) |
| signal | "unkeyed" (`:54`) | "SEND + POLL LIVE" (`:137`) |
| imap | "unkeyed" (`:59`) | "SEND + POLL LIVE" (`:144`) |
| twilio_sms | "unkeyed" (`:64`) | "SEND LIVE" (`:150`) |
| teams | "unkeyed" (`:79`) | "SEND LIVE" (`:157`) |
| whatsapp_meta | "unkeyed (heaviest setup, days)" (`:84`) | "SEND LIVE (heaviest setup)" (`:157`) |

The two documents are describing two different meanings of "live" (transport exercised vs credential
present) without saying so, and neither defines it. `ADAPTORS.md:35-36` does define status words
(`live` = keys present, `scaffold` = code ready) — under *its own* definition, "SEND LIVE" for an
unkeyed channel is self-contradictory.

* Internal inconsistency in `ADAPTOR_KEYS_GUIDE.md` itself: `:8-9` counts **gmail** toward
  "5/16 live", but `:19` heads the same section "⚠️ key set, token **EXPIRED**". An expired token is
  not live.

**Root cause.** Two guides written at different times against the same 16-adaptor registry, never
reconciled; plus an integration/adaptor category error in one of them.

**Impact.** `ADAPTORS.md` is the file an operator opens to answer "which channels work?". It gives
19 numbered answers for 16 things, two of which are not channels, and it labels eight unkeyed
channels as LIVE under a definition it states two sections earlier. **Severity: P1.**

**Cheapest correct fix.**

1. Split `ADAPTORS.md` §3/§4 out into a clearly-labelled "Gateway integrations (not channel
   adaptors)" section after the 16 adaptors, and renumber the adaptors 1–16 contiguously with no
   gap at 7.
2. Replace every `SEND LIVE` with the registry's own vocabulary: `code live, key not set` —
   matching `ADAPTOR_KEYS_GUIDE.md`.
3. `ADAPTOR_KEYS_GUIDE.md:8` → `Your current status (from \`check_keys.py\`): 4/16 live
   (telegram, local_mic, webhook, webui); gmail has a key but an expired token. Rest are
   code-ready, unkeyed.`
   **Unverifiable:** the live count needs `check_keys.py` (reads `.env`) or the token-gated
   `/v1/channels`, so I am correcting the internal inconsistency (gmail counted as live) rather than
   asserting a new number.

### F9c — `BENCHMARKS.md` numbers are trace-derived and mostly self-consistent, but the reproduction command does not exist

**Evidence — internally consistent:** the per-skill row `n` values sum to 1,361 exactly as the header
claims (415+397+273+55+51+51+42+17+16+8+4+2 = 1,361); the §3 layer-distribution counts sum to 273
(93+80+23+18+13+11+11+7+5+4+3+2+2+1 = 273); the §4 outcome shares sum to
100% and 30/178 = 16.9% ✓; the §1 provider counts (351+296+38+9+3) sum to 697 across 5 providers.

**Evidence — unverifiable or stale:**

1. `BENCHMARKS.md:183` tells the reader to reproduce the whole table with
   `python _full_benchmark.py`. **That file does not exist** anywhere in the repo
   (`Get-ChildItem -Recurse -Filter _full_benchmark.py` → 0 hits). The documented reproduction
   command is impossible to run, which is why the 178→135 drift in F9 went unnoticed.
   **Severity: P1.** Fix: either commit the script, or replace `:181-184` with
   `These numbers were produced by an ad-hoc aggregation script that was not kept; only the
   session traces under \`state/sessions/s8-*/\` survive, so they can be re-derived by walking
   \`graph.json\` + \`nodes/*.json\` but not by rerunning one command.`
2. `:22` "Sessions touching the computer skill: **178 (100% of logged sessions)**" contradicts the
   dataset it summarises: `:79` says "107 sessions (60.1%) had **no computer node**" and `:55`
   says 80 of 273 computer nodes were "(none) — no computer node ran". A session cannot both touch
   the computer skill 100% of the time and have no computer node in 60.1% of sessions.
   **Severity: P2.** Fix: change `:22` to
   `Sessions with ≥1 computer node | 71 (39.9% of logged sessions)` (178 − 107).
3. `:164` "11/11 computer-use layer unit tests pass" and `llm_gatewayV9/ADAPTORS.md:19` "125 tests,
   <1s" — **unverifiable** without running the suites, which this audit is forbidden to do. Neither
   contradicts any countable fact I could check, so both stand as unverifiable rather than false.

**Cancellation semantics** (checked, not benchmarked): `ARCHITECTURE_V2.md:69-71` says cancel "is
polled **between batches** via `Event.is_set` (never `.set`)". That is a correct statement about
`asyncio.Event` semantics, and it is the honest limit: a cancel that lands while a batch of
`asyncio.gather` node executions is in flight is not observed until that batch returns
(`agent_server.py:4728-4750` `cancel_chat` only sets the event and reports `flagged`).
`agent_server.py:4940-4944` shows the same event consulted before/after the answer is assembled, so
a cancel can still discard an already-computed answer. No doc claims otherwise, so no correction is
needed — but any future doc saying "cancels immediately" would be false, and `README.md`'s
"Known gaps" list omits this latency entirely.

### F10 — Both channel guides hand out `curl` recipes that now return `401`

**Evidence.** `llm_gatewayV9/ADAPTORS.md:26,46,49,66,95,166,177,180,181,182` and
`ADAPTOR_KEYS_GUIDE.md:17,22,27,42,47` all instruct:

```
curl -s http://localhost:8109/v1/channels | python3 -m json.tool
```

Live, unauthenticated, that returns **401** `{"error":"missing or invalid x-gateway-token
header"}` (measured; see F1's table). `ADAPTORS.md:30` additionally omits any `Content-Type` on
`/v1/channels/{name}/send`, and neither guide mentions `x-gateway-token` anywhere — I grepped both
files for it and there is no hit in either.

**Root cause.** Same cause as F1: `gateway_auth.py` landed after these guides were written, and the
one file whose whole job is to tell operators how to authenticate was not updated alongside it.

**Impact.** Every verification command in both channel guides fails on the first try for a new
operator, with an error message that names the required header but not where to get it. Worse, the
failure looks like "the gateway is down", which sends the operator to restart a service that is
already up. As of this writing there is a second, compounding problem: per sibling agents,
`state/gateway.token` no longer matches the live process's in-memory token, so even the corrected
recipe below fails until the gateway is restarted — which this batch is forbidden to do. That is
itself a documentation-shaped problem: neither guide mentions that the token is per-launch and
launch-bound, so an operator has no way to know a stale file is the cause.
**Severity: P1.**

**Cheapest correct fix.** Add one block to `ADAPTORS.md` immediately under the title and one line to
`ADAPTOR_KEYS_GUIDE.md`, and prefix every `curl` in both files with it:

```
The gateway requires its per-launch token on every /v1/* route. Export it once
per gateway launch (it is printed at gateway start-up, and also written to
llm_gatewayV9/state/gateway.token):

  export XGATEWAY_TOKEN=$(cat llm_gatewayV9/state/gateway.token)
  curl -s -H "x-gateway-token: $XGATEWAY_TOKEN" http://localhost:8109/v1/channels
```

(On Windows/PowerShell use `$env:XGATEWAY_TOKEN` and `-H "x-gateway-token: $env:XGATEWAY_TOKEN"`.)
`/`, `/help`, `/docs` and `/openapi.json` stay open, so the dashboard and help page still load
without it.

### F11 — Env-var documentation drift (names and `file:line` only; no values read)

Vars read in code with **no** mention in any of the audited docs. All of these are in
`llm_gatewayV9` unless stated:

| Var | Read at | Documented anywhere? |
|---|---|---|
| `GATEWAY_V9_TOKEN` | `llm_gatewayV9/gateway_auth.py:34` | **no** — it is the control that F1/F10's corrected text must name |
| `GATEWAY_HOST` | `llm_gatewayV9/main.py:1152` | **no** |
| `ARIA_AUTH` | `S9SharedCode/code/agent_server.py:5211` | **no** — `ARIA_AUTH=off` **disables the per-launch token entirely** ("every /api route is UNAUTHENTICATED", printed at `:5213`) |
| `ARIA_HOST` | `S9SharedCode/code/agent_server.py:5203` | **no** |
| `ARIA_API_TOKEN` | `S9SharedCode/code/agent_server.py:202` | **no** — a *second, independent* bearer mechanism (`Authorization: Bearer`, 401) that coexists with the per-launch `X-Aria-Token` (403). No doc explains that there are two. |
| `COMPUTER_USE_MODE` | `S9SharedCode/code/computer_use/safety/gates.py` (mode knob), exercised by `tests/test_audit_safety_regression.py:22` | **no** — `ARCHITECTURE.md:172` describes a "mode (dry-run\|live)" but never names the var |
| `ARIA_ENABLE_KILL` | named at `llm_gatewayV9/ADAPTORS.md:190` | **yes**, gateway guide only |
| `GATEWAY_MEMORY_STATE` | `llm_gatewayV9/memory_api.py:63` | **yes** (`llm_gatewayV9/ARCHITECTURE.md:244`) |
| `ENABLE_OLLAMA_LLM` | `llm_gatewayV9/providers.py:1271` | **yes** (`llm_gatewayV9/ARCHITECTURE.md:134`) |
| `GATEWAY_GEMINI_ONLY` | `llm_gatewayV9/main.py:255` | **yes** (`ARCHITECTURE_V2.md:179`) |
| `COMPUTER_USE_ENABLED` | `computer_use/safety/gates.py:53` | **yes** (`ARCHITECTURE.md:139`) |
| `S9_STATE_DIR` | `S9SharedCode/code/agent_server.py:134` | **yes** (`ARCHITECTURE_V2.md:125`) |

The pattern is consistent and worth naming: **every variable that turns a security control *off*
(`GATEWAY_V9_TOKEN`, `ARIA_AUTH`, `ARIA_HOST`, `ARIA_API_TOKEN`) is undocumented, and every
variable that turns one *on* is documented.** An operator reading the docs learns where the switches
are but not where the off-switches are.

Documented-but-unverified / likely-stale:

* `ARCHITECTURE_V2.md:179` — "`GATEWAY_GEMINI_ONLY=true` … (current production setting: all-$0
  spend)". Whether it is set on the running gateway is **unverifiable** here: it lives in
  `llm_gatewayV9/.env`, which this audit may not read, and `/v1/providers` is token-gated.
* `llm_gatewayV9/README.md:471` still documents `GATEWAY_V3_PORT=8101` in the "Configuration"
  block. `main.py:59-63` honours only `GATEWAY_V9_PORT` and deliberately ignores `GATEWAY_V3_PORT`.
  The file's own `:5` and `:130-137` say so, so the block contradicts its own header — it is inside
  the section explicitly marked "Historical … do NOT copy", which mitigates but does not remove the
  trap. **Severity: P3.**
* `llm_gatewayV9/README.md:599` tells you to run `./.venv/bin/python tests/test_all_providers.py` —
  a POSIX path, unusable on this Windows host, and the file is flagged known-stale by
  `llm_gatewayV9/ARCHITECTURE.md:272-274`. **Severity: P3.**

**Severity of F11 as a whole: P2**, escalating to P1 for the two unauthenticated-`curl` guides (F10).

## Verified correct

Checked and *not* broken; do not re-test these.

* **Ports and service names.** `:8500` agent, `:8109` gateway, `GATEWAY_V9_PORT` default 8109
  (`main.py:63`), `GATEWAY_V3_PORT` ignored (`main.py:59`). Live `/api/health` →
  `{"agent":"ready","gateway_up":true,"spa_built":true}`.
* **Python versions.** `ARCHITECTURE.md:218-220` — both venvs are `Python 3.11.13` and the system
  interpreter is `Python 3.8.2`, exactly as documented. This also means the "system python is 3.8
  and breaks on `list[dict]` annotations" warning is live, not historical.
* **`ARCHITECTURE.md:41-42` archive location.** "`archive/` … sibling of this workspace root" — both
  `project3\archive` and `..\archive` exist. **True.**
* **`ARCHITECTURE.md:189-191` `max_turns` default 12.** Confirmed three ways:
  `computer_use/engine.py:54` `MAX_TURNS_HARD = 12`, `engine.py:101` `max_turns: int = 12`,
  `computer_use/__init__.py:62` `payload.get("max_turns", 12)`.
* **Skill count 13.** `ARCHITECTURE_V2.md:74` — exactly 13 top-level keys in
  `S9SharedCode/code/agent_config.yaml` (lines 28, 35, 47, 59, 66, 72, 78, 84, 90, 102, 123, 141, 155),
  and the 13 names the doc lists match those keys.
* **Frontend stack versions.** `ARCHITECTURE_V2.md:200` "React 19 + Vite 8 + Tailwind v4 + xyflow 12"
  matches `console-frontend/package.json` exactly (react ^19.3.0, vite ^8.3.0, tailwindcss ^4.3.3,
  @xyflow/react ^12.11.6).
* **Provider rate-limit table.** `ARCHITECTURE_V2.md:165-174` matches `router.py:8-18` `LIMITS` on
  all 8 rows × 5 columns (ollama 9999/9999999/99999999/0/32000, cerebras 30/9999/60000/2/8000 +
  `tokens_per_day` 1_000_000, groq 30/1000/6000/2/100000, nvidia 40/9999/100000/2/100000,
  gemini 15/1000/250000/4/1000000, openrouter 20/50/99999999/3/100000, github 10/50/99999999/6/8000,
  kilo 60/9999/99999999/1/262144).
* **Provider count 9.** `ARCHITECTURE_V2.md:28` "providers.py (9 providers)" = 9 `LIMITS` entries
  including `gemini35lite`; `static/help.html:58` "eight providers … plus local ollama" = the same 9.
* **16 adaptors / 16 channels.** `llm_gatewayV9/ARCHITECTURE.md:174` and `ADAPTOR_KEYS_GUIDE.md`
  ("5/16 live") — `adaptors/registry.py:13-33` has exactly 16 `_BUILDERS` entries and
  `llm_gatewayV9/adaptors/` contains exactly 16 adaptor subpackages, so the file map at
  `llm_gatewayV9/ARCHITECTURE.md:261` is real.
* **Embedder pinning.** `llm_gatewayV9/README.md:10` 768-dim Ollama-only and
  `llm_gatewayV9/ARCHITECTURE.md:151-153` agree with each other; `README.md:35` "(768-dim)" agrees.
  *(The 8000-char input cap at `llm_gatewayV9/README.md:109` and the `413` at
  `llm_gatewayV9/ARCHITECTURE.md:118` were not exercised — no `/v1/embed` call was made, so the
  cap itself is **unverifiable** here.)*
* **`llm_gatewayV9/README.md` honest self-labelling.** Lines 8-21 and 130-137 clearly fence off the
  V3/V7 historical material, which is why the stale `GATEWAY_V3_PORT` block is survivable.
* **`README.md:49` "the agent issues a per-launch token that the console sends as `X-Aria-Token`"** —
  **true**. `agent_server.py:243-249` checks `_auth.TOKEN_HEADER` and 403s; the SPA injects
  `<meta name="aria-token">` and `aria_probe` scrapes it from `/research` successfully.
* **`README.md:80` "Binding to a non-loopback address without a token refuses to start"** — **true**.
  `agent_server.py:5203-5224`: `ARIA_HOST` default `127.0.0.1`, `_auth.assert_safe_binding(_host)`,
  and `raise SystemExit` when it fails.
* **`README.md:22-23` read-only chat tools.** The named set (`web_search`, `search_knowledge`,
  calendar, mail, GitHub, Slack) all exist in the live 38-tool `/api/tools` inventory.
* **`ARCHITECTURE.md` §5 env-var table.** Every var it lists (`TELEGRAM_BOT_TOKEN`, `GMAIL_TOKEN`,
  `GMAIL_REFRESH_TOKEN`, `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GITHUB_TOKEN`,
  `SLACK_BOT_TOKEN`, `NOTION_TOKEN`, `GOOGLE_CALENDAR_TOKEN`, `TAVILY_API_KEY`) is genuinely read
  by gateway code — `integrations_api.py:136-149` and `check_keys.py:100-102` list the same names,
  `integrations/calendar.py:42`, `integrations/websearch.py:138`, `llm_gatewayV9/tests/
  test_adaptors.py:31-32`. `COMPUTER_USE_ENABLED` is read at `computer_use/safety/gates.py:53`.
  So the table is not aspirational.
* **`ADAPTOR_KEYS_GUIDE.md` env-var names.** All names it asks for (`TELEGRAM_CHAT_ID`,
  `MATRIX_HOMESERVER/USER/PASSWORD`, `LINE_CHANNEL_SECRET/ACCESS_TOKEN`, `SLACK_SIGNING_SECRET`,
  `SIGNAL_NUMBER/DATA_DIR`, `IMAP_*`, `SMTP_*`, `TWILIO_SID/AUTH/FROM/WEBHOOK_URL`,
  `WHATSAPP_FROM`, `TEAMS_APP_ID/PASSWORD/TENANT`, `WA_TOKEN/PHONE_ID/VERIFY_TOKEN`,
  `TWILIO_VOICE_FROM`, `VOICE_WS_URL`, `WEBHOOK_SECRET`, `WEBHOOK_OUT_URL`) appear in
  `llm_gatewayV9/tests/test_adaptors.py:31-32`'s key inventory or the registry.
  **Unverifiable:** whether each is currently *set* — that requires reading `.env`.
* **`ARCHITECTURE_V2.md:69-71` cancellation semantics** — accurate as written (see F9).
* **`BENCHMARKS.md` internal arithmetic** — the 1,361 node total and 273 computer-node total each
  reconcile exactly from the tables above them.

## Recommended work, ordered

| Fix | File(s) | Effort | Risk | Blocks what |
|---|---|---|---|---|
| Correct the "gateway has no auth" known gap (F1) | `README.md:103-105`, `README.md:78-86` | 5 min | none | Any security review; operator trust |
| Add the `x-gateway-token` header to every `curl` recipe (F10) | `llm_gatewayV9/ADAPTORS.md:26-33,46-52,66,95,166,177-182`, `ADAPTOR_KEYS_GUIDE.md:17-47` | 30 min | none | Every channel-setup walkthrough |
| Correct the gateway bind address (F4) | `llm_gatewayV9/ARCHITECTURE.md:42` | 2 min | none | External-binding decisions |
| Repair `COMPUTER_USE_ARCHITECTURE.md` fences + 3 truncated words (F6) | `COMPUTER_USE_ARCHITECTURE.md:65,85,129,141,161,173-174` | 20 min | low (rendering only) | Anyone reading §4–§12 |
| Re-derive `static/help.html` default-model markers (F5) | `llm_gatewayV9/static/help.html:105,135,150` | 30 min, or automate | low | Model-pinning decisions |
| Reconcile `BENCHMARKS.md` with the 135 sessions on disk, or date-stamp it (F9) | `BENCHMARKS.md:4,19,22,85` + every derived % | 1 h (re-aggregate) | low | Any quantitative claim from this repo |
| Commit `_full_benchmark.py` or remove the command that runs it (F9c.1) | `BENCHMARKS.md:181-184` | 5 min (delete) / 2 h (write it) | none | Reproducibility of every number in the file |
| Renumber `ADAPTORS.md` and reconcile its LIVE claims with `ADAPTOR_KEYS_GUIDE.md` (F9b) | `llm_gatewayV9/ADAPTORS.md:40-172`, `ADAPTOR_KEYS_GUIDE.md:8-9,24-92` | 45 min | none | "Which channels actually work?" |
| Fix route counts (F2) | `agent_server.py:18` | 2 min | none | API-surface reasoning |
| Fix tool counts (F3) | `ARCHITECTURE.md:27`, `ARCHITECTURE_V2.md:91` | 5 min | none | Tool-catalog audits |
| Fix the two-file `.env` instruction (F7) | `README.md:45` | 5 min | none | First-run setup |
| Fix the 100%-vs-60.1% contradiction (F9c.2) | `BENCHMARKS.md:22` | 5 min | none | Baseline credibility |
| Add `x-gateway-token` to the gateway verify step (F8 caveat 1) | `AGENTS.md:12`, `ARCHITECTURE_V2.md:40` | 5 min | none | The documented restart loop |
| Document the off-switches `GATEWAY_V9_TOKEN`, `ARIA_AUTH`, `ARIA_HOST`, `ARIA_API_TOKEN`, `COMPUTER_USE_MODE` (F11) | `README.md` security section, `AGENTS.md` | 20 min | none | Safe exposure of either port |
| Replace exact suite counts with file counts (F8 caveat 2) | `AGENTS.md:17`, `ARCHITECTURE_V2.md:298-299` | 5 min | none | Nothing — but stops future rot |

## Numbers that must be regenerated when code changes

The recurring failure in this batch is a hand-copied count. Each row below is a fact a doc asserts
somewhere plus the exact one-line command that re-derives it — cheap enough that "check before you
trust the doc" becomes a command, not an audit.

| Fact | Documented as | Truth (2026-10-03) | Re-derive with |
|---|---|---|---|
| Agent routes | 83 (`agent_server.py:18`) | 106 ops / 80 `/api` / 92 paths | `Select-String agent_server.py -Pattern '@app\.(get\|post\|put\|delete\|patch\|websocket)\(' \| Measure-Object` |
| MCP tools | 37 (`ARCHITECTURE.md:27`, `ARCHITECTURE_V2.md:91`) | 38 catalog / 35 `@mcp.tool` | live `GET /api/tools`; and `Select-String mcp_server.py -Pattern '@mcp\.tool'` |
| Agent test files | 372 passed, 36 skipped (`ARCHITECTURE_V2.md:298`) | 58 `test_*.py` files | `Get-ChildItem S9SharedCode\code\tests -Recurse -Filter test_*.py` |
| Gateway test files | 174 passed (`ARCHITECTURE_V2.md:299`) | 33 `test_*.py` files | `Get-ChildItem llm_gatewayV9\tests -Recurse -Filter test_*.py` |
| Channel adaptors | 16 (`llm_gatewayV9/ARCHITECTURE.md:174`, `ADAPTORS.md:3`) | 16 ✓ | `Select-String adaptors\registry.py -Pattern '"[a-z_]+": "adaptors\.' \| Measure-Object` |
| Provider LIMITS rows | 8 rows + 9 providers | 9 entries incl. `gemini35lite` ✓ | `Select-String router.py -Pattern '^\s+"[a-z0-9]+":\s+\{"rpm"'` |
| Skills | 13 (`ARCHITECTURE_V2.md:74`) | 13 ✓ | `Select-String agent_config.yaml -Pattern '^[a-z_]+:'` |
| Benchmark corpus | 178 sessions (`BENCHMARKS.md:4`) | **135** | `Get-ChildItem state\sessions -Directory -Filter "s8-*"` |
| Gateway default models | 3 defaults in `static/help.html` | 3 disagree with `providers.py` | diff `help.html` `★ default` against `providers.py` `os.getenv("*_MODEL", …)` |

## Proposal: convention for future batches in `docs/audits/`

Building on `_BRIEFING.md` + `INDEX.md` rather than repeating them.

1. **Folder naming.** `docs/audits/<YYYY-MM-DD>/` (already correct). Within it, exactly two
   reserved names — `_BRIEFING.md` (shared constraints) and `INDEX.md` (the batch's table of
   contents) — plus numbered reports `NN-<slug>.md` where `NN` is the batch-wide slot number that
   the coordinator assigns, not the discovery order. Stable numbers are what let the next batch
   write "see `2026-10-03/07-*.md`" instead of searching.
2. **Report filename pattern.** `NN-<subject-slug>.md`, subject slug describing the *system area*
   (`12-docs-truth`), not the finding (`12-route-counts`). Reason: one agent owns one report and
   that agent's findings span several areas; an area-named file can be found by the next auditor
   working the same area, a finding-named file cannot.
3. **Front matter.** Add a YAML block to every report, and to `_BRIEFING.md` a schema the coordinator
   fills in:

   ```yaml
   ---
   batch: 2026-10-03
   slot: 12
   area: docs-truth
   agent_role: docs-auditor
   services_probed: [8500, 8109]
   destructive_actions: none
   commands_run: [pytest, npm-build, uv-sync, service-restart]   # all `none` for doc audits
   verdict: complete | partial | blocked
   reports_known_gaps_at_start: true
   ---
   ```

   `commands_run` is the field that matters most: it makes "I did not run the suite, so the
   pass/skip counts are unverifiable" a machine-readable fact instead of a promise in prose, and it
   is exactly what stops the next agent re-running a 6-minute suite to re-learn the same thing.
4. **Root README line.** Yes, but one line per **batch**, not per report, and only for reports that
   produced a `P0`/`P1`. Suggested shape appended to the root `README.md` under a
   `## Audits` heading:

   ```markdown
   ## Audits
   - `docs/audits/2026-10-03/` — 12 parallel read-only audits. P0/P1:
     [gateway-auth claim in README is false](docs/audits/2026-10-03/12-docs-truth.md),
     [...](docs/audits/2026-10-03/NN-slug.md).
   ```

   12 rows of P3 nitpick in the README is noise; 2–4 P0/P1 lines is a signal. Keep `INDEX.md` as the
   exhaustive index and the README as the triage view.
5. **One extra convention this batch argues for.** Because the single worst finding here was a
   *number* that drifted (83 routes, 37 tools, 5/6 of suite outcomes), every report should carry a
   short `## Numbers that must be regenerated when code changes` list — the countable facts
   (route count, tool count, test-file count, `LIMITS` rows, adaptor count) with the exact
   one-line command that re-derives each. That converts a class of recurring false claims into a
   checklist instead of a re-audit.