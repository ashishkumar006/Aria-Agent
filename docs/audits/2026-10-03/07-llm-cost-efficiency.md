# LLM cost and token efficiency — 07

*Written by the coordinator. The assigned subagent timed out upstream, but before it died it captured
the gateway ledger to `%TEMP%\kilo\spend.json` and `%TEMP%\kilo\calls.json` (5 000 call records) while
`/v1/spend` and `/v1/calls` were still reachable. Every number below is computed from that capture, so
it survives the token clobber that later made `/v1/*` return 401. Report recomputed and extended by the
coordinator; nothing here is estimated.*

## Method and an important caveat

`calls.json` holds 5 000 rows covering 983 calls' worth of tokens (the gateway retains a capped window).
**The table is bimodal**: it records LLM calls *and* bookkeeping operations (embed, memory
classify/remember/search/sweep, stt, tts) in the same ledger with the same columns. Evidence: the `model`
column contains `clear`, `remember`, `search`, `sweep`, `tts:af_heart`, `stt`, `nomic-embed-text`
alongside two real models, all with 0 input tokens. Consequently the global percentiles below are
misleading (`input_tokens` p50 = 0, `prompt_chars` p50 = 17) and I have split the figures by `agent`
rather than reporting a single blended distribution. **That the ledger cannot distinguish a bookkeeping
row from an LLM call is itself finding F7.**

Window captured: `spend.json` totals **983 calls, 914 851 in-tok, 56 100 out-tok, $0.00**.
The 5 000 captured rows total **1 061 769 in-tok / 83 530 out-tok — a 12.7 : 1 input:output ratio.**

---

## Findings

### F1 — Prompt caching is effectively disabled, on a workload built out of large repeated prefixes — **P1**

Across all 5 000 captured calls:

```
cache_create_tokens total : 0
cache_read_tokens   total : 4 075
calls with any cache_read>0 : 1        (0.02%)
```

**One call in five thousand received a cache read, and nothing ever created a cache entry.** The gateway
records both fields per call, so this is measured, not inferred.

That matters because the dominant cost in this system is a *repeated large prefix*. The planner alone
sent **261 506 input tokens across only 70 calls — a mean of 3 735 tokens each** (see F2). Each of
those 70 calls re-sent the same multi-kilobyte system prompt (`prompts/planner.md` plus the skill
catalog and tool contract) with no cache write. With working prompt caching, the marginal cost of 69 of
those 70 calls would be output-only.

**Impact:** on a free tier this is invisible as dollars and very visible as latency — cache hits are the
single biggest lever on time-to-first-token for a long stable prefix. It becomes real money the moment a
paid provider is configured, and `llm_gatewayV9/pricing.py` will price it accordingly.
**Cheapest fix.** Confirm the provider supports context caching for the configured models, then send the
stable prefix first and mark the cache boundary. If the Gemini endpoint does not cache, the fallback is
to shrink the prefix (F2) rather than to rely on caching.

### F2 — The planner is 28 % of all tokens for 7 % of the LLM calls, at 4.6× the chat path's prompt size — **P1**

| agent | calls | input tokens | share of tokens | mean input/call |
|---|---|---|---|---|
| `chat` | 703 | 567 498 | **60.8 %** | 807 |
| `planner` | **70** | 261 506 | **28.0 %** | **3 735** |
| `computer` | — | 43 976 | 4.7 % | — |
| `v9_vision_endpoint_smoke` | — | 23 205 | 2.5 % | — |
| `deploy-probe` | — | 22 652 | 2.4 % | — |
| `formatter` | — | 12 070 | 1.3 % | — |
| `browser:a11y` | — | 3 207 | 0.3 % | — |

A single planner call costs **4.6× a whole chat turn's prompt**. This is the arithmetic signature of the
recovery storm documented in `AGENT_BREAK_TEST_REPORT.md`: `mcp_runner.py:248` breaks every tool-using
skill, `recovery.py` re-plans, and each re-plan is billed at ~3 700 tokens. The "gold price" session
alone produced a 31-node DAG and ~385 k input tokens; the ledger shows planner prompts landing
repeatedly at **6 232 tokens** in consecutive rows (two planner calls at exactly 6 232 in-tok, 57 and 32
out-tok, 1.6 s and 3.6 s apart) — the signature of a loop, not of distinct decisions.

**Impact.** Roughly a quarter of all tokens are spent deciding what to do about the fact that the tool
layer is broken.
**Cheapest fix, in order:** (1) cap recovery replans per run and make the *n*-th consecutive failure of
the same skill terminal with a clear error — this removes the amplification immediately; (2) do not send
the planner the full node-state dump on replans; (3) shrink the stable planner prefix so F1's fallback
is cheap even without caching.

### F3 — Tool calling is effectively unused: 11 of 5 000 calls — **P1**

`tool_calls > 0` on **11 records (0.22 %)**. This is the intended shape of an agent whose primary
capability is tools, and it corroborates the break-test's P0: the tool path dies before any call is
constructed, so the model is never given a live tool to invoke.

### F4 — A 4.4–5.9 % hard failure rate, and every failure class is systemic — **P1**

Of 5 000 captured rows: **222 carry an `error`, 296 have `status != ok`**. The top error strings, each
appearing 21–25 times, are not transient:

| count | error |
|---|---|
| 25 | `HTTPStatusError: Client error '401 Unauthorized'` |
| 25 | `GMAIL_TOKEN expired; refresh failed: HTTPStatusError` |
| 25 | `RuntimeError: telegram error 400: Bad Request: chat not found` |
| 21 | `all embedders unavailable. attempts=[{'provider': 'ollama', …}]` |
| 21 | `unknown embedder 'gemini'` |
| 21 | `role 'agent' may not write drawer 'playbook' (allowed: ['gat…'])` |

Every one of these is a *configuration or authorisation* fault, not a network blip: an auth token that
rotated (the same class as `gateway.py:67-79`), an expired credential, a wrong chat id, an embedder
chain with no live provider, a misspelled embedder name, and a drawer permission that blocks the agent
from writing its own playbook. **They are also being paid for repeatedly** — the same error 21–25 times
means the system is retrying a permanently-failing operation rather than tripping a breaker, even though
`breaker_stats()` is already exposed at `agent_server.py:631`.

Retries are otherwise rare (`retries > 0` on only 7 rows), which makes 21–25 identical failures more
alarming: these are separate callers making the same doomed call, not a retry loop.

### F5 — 357 embedding calls were made with an 8-dimensional embedder — **P1**

Embed dimensions recorded: **`8` on 357 calls, `768` on 166 calls.**

A 768-dimension vector is what `nomic-embed-text` produces and what the store is built for. **357 calls
against an 8-dimension embedder** means either a test stub is wired into the live path, or a
misconfigured `OLLAMA`-class embedder is silently returning 8 numbers and the system is storing and
searching them as if they were real embeddings. In an 8-dimensional space, semantic retrieval is close to
meaningless, and the Memory and document-search drawers would return confident nonsense.

**Impact.** This is a correctness defect disguised as a config, and it is invisible because nothing
validates dimensionality on write or read.
**Cheapest fix.** Reject or warn on an embed dimension that is not in the configured set at embed time;
log the dimension on every call (the ledger has the field — `embed_dim` — so this is a one-line alert).

### F6 — Latency tail is long even before any concurrency — **P2**

Per-call `latency_ms` across the capture: **p50 1 454 ms, p90 4 808 ms, p99 12 263 ms, max 60 493 ms.**
The max is a full minute on a single call. This is with no load attributable to this audit — the
concurrent-load degradation is a separate finding (report 02).

### F7 — The ledger cannot distinguish a bookkeeping row from an LLM call — **P2**

`model` values of `clear`, `remember`, `search`, `sweep`, `stt`, `tts:af_heart`, `nomic-embed-text` sit
in the same table as `gemini-3.5-flash-lite`, with 0 input tokens and a `call_role` of `memory`/`voice`/
`embed`. `call_role` exists and would distinguish them cleanly (`worker` for real calls, 100 % of the
token volume) but no aggregate view uses it.

**Impact.** Every cost query that groups by `model` or `provider` silently mixes operational rows into
LLM spend. `/api/cost/by_skill` and the console Ledger page render it as-is.
**Fix.** Group by `call_role` and exclude non-`worker` roles from cost figures; surface the role in the
console.

### F8 — `reasoning_applied` is false on all 5 000 calls — **P3**

The field exists on every record and is never true. A configured capability is inert. Either wire it or
remove it, because a permanently-false capability flag is the same class of lie as the console's
`/api/capabilities` reporting `multimodal.input: false` while a vision path exists.

### F9 — Provider spread is even, model choice is not — **P3**

Input tokens by provider: `gemini35lite-3` 15.2 %, `gemini35lite` 13.2 %, `gemini35lite-2` 13.1 %,
`gemini-2` 12.6 %, `gemini35lite-4` 12.3 %, `gemini-3` 11.8 %, `gemini` 11.5 %, `gemini-4` 10.2 % —
a tight spread across nine keys. By model it is bimodal: `gemini-3.5-flash-lite` 53.8 % and
`gemini-3.1-flash-lite` 46.2 %, and nothing else.

The even key spread means load balancing is working as intended. The model bimodality means there is
**no capability tiering at all** — every prompt, from a 3-token ping to a 25 001-token deep task, is sent
to one of two closely-related flash-lite models. There is no strong model to escalate to, which is why
the "cascades / escalate on low confidence" pattern from the prior-art section is not available here
without adding a model first.

---

## Verified correct

* **Load balancing across the nine provider keys is genuinely even** (10.2 %–15.2 %), so no single key
  is a hot spot and failover has somewhere to go.
* **Retry storms are not the default.** Only 7 of 5 000 rows carry `retries > 0`.
* **Embeddings are dimensionally consistent within each embedder** (all `8`s together, all `768`s
  together) — the problem in F5 is which embedder is live, not inconsistency.
* **Policy is being evaluated on real traffic**: 153 calls carried `trust_level: paired` and 153 policy
  verdicts were recorded (100 allow, 53 deny). The engine is in the path, not bypassed. (Its reporting
  defect — `allowed: true` alongside `action: "deny"` because the shipped config is `dry_run` — is in the
  break-test report, not repeated here.)

---

## Recommended work, ordered

| # | Action | Saving | Effort | Notes |
|---|--------|--------|--------|-------|
| 1 | Cap consecutive replans per run; make repeated failure of one skill terminal | Removes most of the **28 %** planner share (F2) | 1–2 h | Biggest single win. Also stops the 60-node DAGs. |
| 2 | Trip the existing MCP breaker instead of re-firing a permanently-broken skill | Removes the repeated 21–25× failures (F4) | 30 min | `breaker_stats()` already exists |
| 3 | Fix `mcp_runner.py:248` | Restores tool calling (F3) and the research path entirely | 1 line | Prerequisite for any real cost number |
| 4 | Validate embed dimensions; alert on `embed_dim != configured` | Removes 357 searches against meaningless vectors (F5) | 30 min | Correctness, not cost |
| 5 | Group all cost views by `call_role`, exclude non-`worker` (F7) | Makes every other number on this page trustworthy | 30 min | Prerequisite for measurement |
| 6 | Enable prompt caching, or shrink the planner prefix (F1) | Potentially ~2 000 tokens × 69 of 70 planner calls | 2–4 h | Needs a provider-capability check first |
| 7 | Shrink the planner prompt: drop node-state dumps from replans | Direct, on top of #1 | 2 h | |
| 8 | Add a strong model tier so confidence-based escalation becomes possible (F9) | Enables the routing pattern; no saving yet | config + eval | Do after 1–5 |
| 9 | Remove or wire `reasoning_applied` (F8) | — | 15 min | |

**Framing for the operator:** at $0.00 recorded spend, none of this is costing money today. It is costing
**latency, tokens and correctness** — the 4.4 % hard failure rate, the 357 searches against an
8-dimensional vector, and the ~28 % of tokens spent re-deciding what to do about a one-character bug.
Actions 1–4 are the ones that make the rest of the numbers mean something.