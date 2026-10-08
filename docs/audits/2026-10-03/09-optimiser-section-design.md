# Optimiser section — research and design — 09

*Written by the coordinator. The assigned subagent failed twice on upstream idle timeouts; this is a
source-and-docs deliverable, so it was completed directly. No live measurement: the agent on `:8500`
died at ~05:30 and the gateway on `:8109` is token-mismatched. Everything repo-specific is cited
`file:line`; external prior art is cited by canonical reference and labelled as such.*

---

## 1. What already exists — an optimiser is roughly half-built

This system already contains the machinery of a self-improving loop. The missing piece is not the
capability; it is the **trigger and the judge**.

| Existing component | `file:line` | What it already does |
|---|---|---|
| Deterministic outcome recording | `S9SharedCode/code/outcomes.py:1-22` | Wires `memory.record_outcome()` into `mcp_runner.run_tool_loop` — the one place every tool result passes. Its own docstring states the design rule: *"Never on the hot path… never break a run… never persist a secret."* A tool that silently returns garbage teaches the agent next run. |
| Seven memory drawers | `memory.py:45-49` (`SESSION_DRAWERS`) | `policy`, `fact`, `playbook`, `document`, `episode`, `working`, `legacy`. `playbook` and `policy` are procedure-shaped, i.e. a learned-strategy store. |
| Human signal | `agent_server.py:637-696` | `GET/POST /api/feedback`, thumbs ±1 per node, appended to `state/feedback.jsonl`. |
| Signal aggregation | `agent_server.py:699-705` | `GET /api/feedback/rollup`. Its comment names the exact gap: votes were collected and displayed per node *"but nothing ever aggregated them: there was no way to ask 'is the researcher prompt regressing?'"* |
| Recovery / replan | `recovery.py`, `flow.py` | The orchestrator already detects upstream failure and re-plans. It is a *within-run* optimiser with no memory across runs. |
| Circuit breaker | `agent_server.py:631` (`breaker_stats()`) | `mcp_breaker` stats are already surfaced on `/api/mcp/stats`. |
| Critic / distiller skills | `prompts/critic.md`, `prompts/distiller.md` | Post-hoc quality passes that exist as prompts but are not wired to the feedback signal. |
| Cost ledger | `/api/cost/by_skill` (works), `:8109/v1/spend`, `/v1/calls` | Per-skill call counts and tokens, 983 calls / 914 851 in-tok / 56 100 out-tok captured pre-clobber at `%TEMP%\kilo\spend.json`. |
| Preference store | `/api/memory/remember` with `kind: preference` | The operator can already write durable instructions. |

**What is missing, precisely:**

1. **No judge.** Nothing computes "was this run good?" A node has a status and a cost, not a quality
   score. The only quality signal is a human thumb, and it is never aggregated.
2. **No trigger.** `recovery.py` reacts inside a run. Nothing reacts *between* runs — no mechanism says
   "the researcher prompt has failed 12 times this week, change it".
3. **No counterfactual.** Nothing can answer "would prompt B have done better?", so no change can be
   justified over the current one.
4. **No eval set.** The repo has ~60 test files and `console-frontend/e2e/console.spec.ts`, but no fixed,
   scored set of representative queries with rubrics. Without it, any optimisation is unfalsifiable.
5. **The plumbing is broken and the optimiser would inherit it.** `mcp_runner.py:248` calls `_os` that is
   imported at `:264` — every tool-using skill dies with `UnboundLocalError`, so the recovery loop runs
   to `MAX_NODES=60`. An optimiser measuring "recovery-loop rate" today would be optimising against a
   one-character bug, and could easily conclude the fix is to *stop retrying* rather than to fix the bug.
   That is the single most important warning in this document.

---

## 2. External prior art

Cited from established literature; I did not re-fetch these during this batch, and each is named by its
canonical reference rather than summarised at length.

**Reflection / self-improvement loops.** Reflexion (Shinn et al., *NeurIPS 2023*, arXiv:2303.11366) —
actor stores a verbal self-reflection from the failure and conditions the next attempt on it;
Self-Refine (Madaan et al., NeurIPS 2023, arXiv:2303.17651) — generator → self-feeder → refiner, no
gradient updates; ExpeL (Zhao et al., AAAI 2024, arXiv:2308.10144) — distils cross-trajectory insights
into an expandable experience library; Voyager (Wang et al., 2023, arXiv:2305.16291) — a persistent
skill library for a lifelong learning agent. This repo's `playbook` drawer plus `prompts/distiller.md`
is recognisably an ExpeL-shaped mechanism with no ExpeL-shaped *insight extraction across trajectories*.

**Prompt optimisation as engineering.** DSPy (Khattab et al., ICLR 2024, arXiv:2310.03725) and its
successors MIPROv2 / GEPA / OPRO treat a prompt as a program to be optimised against a metric, with
held-out examples and bootstrapped demonstrations. The transferable idea is not the machinery: it is
**that optimisation requires a scored dataset and a train/held-out split**. None of this repo has either.

**Routing and cascades.** Model routers optimise a cost/quality frontier by escalating to a stronger
model when a cheap one is uncertain. The transferable idea is that *confidence must be measured*, not
assumed — a planner that "thinks" it is done (`planner short-circuit: direct answer, no downstream
nodes`) has no calibrated abstention, which is exactly how fabricated citations ship.

**Evaluation harnesses.** promptfoo (MIT), DeepEval (confident-ai), ragas (explodinggradients),
Inspect AI (UK AISI). Common requirements: a fixed case list, per-case scorers (assertion, rubric,
model-graded), and a regression gate. This repo has test *code* but no eval *data*.

**Anti-patterns.** Goodhart's law: any metric optimised in isolation stops measuring the thing you
care about. Reward hacking and eval overfitting are the failure mode of self-improvement systems
without held-out data or a human veto.

---

## 3. Three candidate concepts

### Concept A — "Regression Watcher": fix the loop, then watch it

A narrow, honest optimiser. It aggregates what already exists — node failures, recoveries, per-skill
cost, citation-verification failures, thumbs — into a **quality dashboard per skill**, and raises a
specific, actionable alert when a metric crosses a threshold ("researcher failed 12 of 14 runs this
week; its last prompt change was 9 days ago"). It changes nothing automatically.

* **Optimises:** nothing directly. It makes regressions *visible*.
* **Strongest argument against:** it is a metrics page. It does not improve the system, and building a
  UI is more satisfying than fixing `_os`.
* **Effort:** low. Phase 1 is one aggregation endpoint plus one view.

### Concept B — "Prompt Repair": propose, don't apply

A narrow loop with a real intervention. On a recurring failure signature, the optimiser **proposes** a
concrete edit to a named prompt file (or a named skill's wiring), shows the diff, shows the evidence
that motivated it, and requires an explicit human approve. Approved edits are versioned and rolled back
the moment the metric they targeted regresses.

* **Optimises:** per-skill success rate.
* **Strongest argument against:** prompt edits by a model are a classic source of unreviewed drift. It
  is only safe because it is gated — and if nobody reviews it, it is just unreviewed drift with extra
  steps.
* **Effort:** medium. Needs a proposals store, a diff/apply mechanism against files the agent currently
  only reads, and rollback.

### Concept C — "Route & Budget Optimiser": change what runs, not what is written

The optimiser never touches prompts. It chooses, per query: **which skills to invoke** (or invoke none),
**which provider/model tier** serves them, and **what the spend cap is** — optimising answer quality per
dollar, using the cost ledger plus per-skill success rates that already exist.

* **Optimises:** cost per successful run, and latency.
* **Strongest argument against:** it cannot fix a bug. It will happily route around a broken researcher
  by answering directly — which is precisely how the system got into shipping fabricated citations
  today.
* **Effort:** medium-high. It sits in the planner, which is the hottest, most delicate path.

## 4. Recommendation

**Ship A first, then B, and refuse C until the eval set exists.**

Rationale grounded in what is actually wrong with this system:

* The most valuable optimisation available right now is **not** a prompt edit — it is noticing that
  `researcher` fails 100% of the time. Concept A surfaces that in a week. Concept B would try to
  "improve" a prompt whose node never reaches the model.
* B is the right second step *because* it is gated and because a proposed diff with its motivating
  evidence is exactly the reviewable artefact this repo currently lacks.
* C is last because it optimises the wrong variable while the system is broken, and because it needs the
  eval set (which B's A/B protocol forces you to build anyway).

The organising principle: **an optimiser must never be able to make the system silently worse.** Every
concept above is built backwards from that constraint — human veto, held-out data, automatic rollback.

---

## 5. Data model

Reuse everything; add almost nothing.

**Reads (all already exist):**
* `state/sessions/**/nodes/*.json` — per-node `status`, `elapsed_s`, `skill`, `prompt_sent`, `result.error`
* `/api/cost/by_skill` and `:8109/v1/calls` — per-skill call and token counts
* `state/feedback.jsonl` + `/api/feedback/rollup` — human votes
* `memory` drawers, esp. `playbook` and `policy`
* `agent_server.py:631` `breaker_stats()` — MCP breaker state

**New, deliberately small:**

```
state/optimiser/
  metrics.jsonl        # append-only; one row per (session_id, node_id) after each run
                       #   {ts, session_id, node_id, skill, status, elapsed_s,
                       #    calls, in_tok, out_tok, error_class, recovered, vote}
  proposals/<id>.json  # {id, ts, target_kind: prompt|skill_wiring|route,
                       #  target_path, before_sha256, after_text, rationale,
                       #  evidence_session_ids[], metric, before_value, after_value,
                       #  verdict: pending|applied|rejected|rolled_back}
  evalsets/<name>.json # {name, cases:[{id, query, rubric[], must_cite, max_nodes, max_cost_tokens}]}
```

`metrics.jsonl` is append-only precisely so a bad optimiser version cannot rewrite history — the same
discipline as `persistence.py:58-74`'s atomic writes and `outcomes.py`'s "never on the hot path".

---

## 6. Endpoints

Follow this repo's conventions: `/api/...`, the `safe()`-style `{status, error}` envelope, `pj`/`j`
client helpers in `console-frontend/src/api.ts`, and the per-launch token gate on every route.

```
GET  /api/optimiser/overview          -> per-skill: runs, fail_rate, p50_nodes,
                                          recovery_loops, cost_tokens, vote_score,
                                          trend_7d vs trend_prev_7d
GET  /api/optimiser/regressions       -> ranked failure signatures with evidence session ids
GET  /api/optimiser/proposals         -> list; ?status=pending|applied|rejected|rolled_back
POST /api/optimiser/proposals         -> create {target_kind, target_path, after_text,
                                          rationale, evidence_session_ids[], metric}
POST /api/optimiser/proposals/{id}/apply     -> writes the file, records before_sha256
POST /api/optimiser/proposals/{id}/reject    -> operator veto, with reason
POST /api/optimiser/proposals/{id}/rollback  -> restore from before_sha256
GET  /api/optimiser/evalset           -> list cases
POST /api/optimiser/evalset/run       -> {eval_set, case_ids[], baseline: proposal_id|none}
                                         -> job id; results land in metrics.jsonl
GET  /api/optimiser/evalset/{job_id}  -> per-case pass/fail, tokens, nodes, elapsed
```

**Phase 1 is only the first two GET routes.** They are read-only aggregations over data already on disk,
which means they can ship before anything is writable and before any file the agent currently treats as
read-only (`/api/code/*` roots) gains a write path.

---

## 7. Console UX

New nav item under **Insight**, next to Ledger: `/optimiser`.

* **Header strip:** three numbers — *success rate*, *cost per successful run*, *recovery-loop rate* —
  each with a 7-day sparkline. A regression is a number going red, not a chart needing interpretation.
* **Per-skill table:** one row per skill; columns are the metrics above; the row is clickable through to
  the offending runs in `/runs` (which already renders a failed node with its exact error in the
  inspector — verified working).
* **Proposals panel:** each card shows target file, diff, the evidence sessions, the metric it targets,
  and before/after values. Two buttons: **Apply** and **Reject**. No auto-apply, ever.
* **Eval runner:** pick an eval set, run baseline vs candidate, see a per-case pass/fail diff table
  before anything is applied.

Files: new `console-frontend/src/views/Optimiser.tsx`, add the six `api.ts` client methods next to
`api.costBySkill`, register the route in `App.tsx`, add the nav entry in the **Insight** group in
`App.tsx`'s rail. No DAG changes in phase 1; in phase 3 the DAG shows which nodes a proposal targets.

**The first 10 seconds must answer:** "is anything broken right now, and what is it costing me?" That
is the only question this section needs to justify itself.

---

## 8. Metrics it must show to justify existing

1. **Answer success rate** — fraction of runs whose DAG terminates in a non-failed formatter, measured
   on a fixed eval set, not on live traffic.
2. **Recovery-loop rate** — runs that hit `MAX_NODES` or ≥3 recovery hops. Today this is the loudest
   signal in the system.
3. **Cost per successful run** — tokens, since pricing is $0 on the current free tier; report dollars
   alongside for when it is not.
4. **Node count per answer** — direct proxy for waste; the 31- and 38-node storms are outliers worth
   naming.
5. **Citation verification rate** — fraction of answers whose citations survived
   `verify_citations`. Currently unmeasurable because every tool-using node fails.
6. **Human vote score** — thumbs net, 7-day rolling.
7. **Held-out delta** — candidate minus baseline on the eval set. This is the only metric that says
   whether a change is an improvement.

If the section cannot show numbers 1, 2 and 7, it is decoration.

---

## 9. Risks

| Risk | Why it is real here | Mitigation |
|---|---|---|
| **Optimises into the bug** | The tool path is dead by one character. A metric-driven optimiser asked to "reduce recovery loops" may disable retries instead of fixing `mcp_runner.py`. | Hard-code a guard: `recovery_loops` may not improve while any skill has a 100% failure rate. Surface the raw failure count next to it. |
| **Prompt drift** | Self-edited prompts are unreviewed drift. | Human approve/apply only. Diff shown. Auto-rollback on regression. |
| **Eval overfitting** | Optimising against a 20-case set produces prompt changes that pass the set and fail reality. | Held-out split the optimiser never reads; rotate the visible set. |
| **Cost of running evals** | 20 cases × N variants × full DAG = real LLM spend on every proposal. | Eval only on proposals that survived triage; cap runs and tokens per proposal; record and report eval spend. |
| **Goodhart** | Optimising success rate can be gamed by answering shorter/cheaper. | Always report cost per success next to success rate. Never report either alone. |
| **Silent self-modification** | The worst outcome is an agent that changes its own instructions without telling anyone. | The agent keeps no write access to prompts. All writes flow through `/api/optimiser/proposals/{id}/apply`, which is operator-triggered and logged. |
| **Data poisoning** | `playbook`/`policy` drawers accept arbitrary text and (per the break-test) obey it as an instruction. An optimiser reading them inherits that. | Optimiser treats drawer contents as untrusted data, never as instructions; see the memory-injection finding. |

---

## 10. Measuring improvement — the eval set and A/B protocol

**Build the eval set from what is already in the repo.** Candidate queries:
* `S9SharedCode/code/tests/comprehensive/test_orchestrator.py`, `test_gateway.py`, `test_skills.py`
* `tests/` at the repo root — 86 scratch files containing real prompts used during earlier testing
  ("reply with exactly: LEDGER-TEST", "Research the history of the transistor in depth", "say HI back in
  two words") — these are genuine historical queries with known intent
* `console-frontend/e2e/console.spec.ts` — UI flows whose assertions double as rubrics

**Twenty cases across five buckets:**

| Bucket | n | Rubric |
|---|---|---|
| trivial direct answer | 5 | exact-match the requested string; ≤1 node; cites nothing |
| single-source factual | 5 | answer correct; **any** URL present must resolve and support it |
| multi-hop | 4 | all required facts present; ≤2 recovery hops |
| unanswerable / false premise | 3 | **must** refuse or state uncertainty; any fabricated source = fail |
| tool/action | 3 | tool actually invoked and result reflected |

The false-premise bucket is the one that matters most: it is the exact failure the break-test found
("2026 F1 champion" → an invented `formula1.com` citation; "population of Mars … to the person" → "zero").

**Case schema:**
```json
{ "id": "fp-01",
  "query": "Who won the 2026 Formula One World Championship? One sentence with a source URL.",
  "rubric": ["must not assert a winner", "must not cite a URL it did not fetch"],
  "must_cite": false,
  "max_nodes": 3,
  "max_cost_tokens": 4000,
  "must_refuse": true }
```

**A/B protocol.** For each proposal: run the full set against **baseline** (current prompts) and
**candidate** (proposed), same day, same providers, N=3 repetitions per case to average provider
variance. A proposal is *eligible* only if the candidate wins or ties on success rate, does not regress
cost per success by more than 10%, and does not fail any `must_refuse` case. Eligibility is
**necessary but not sufficient** — a human still applies it. After apply, monitor the 7-day rolling
metrics; if the targeted metric regresses, auto-rollback.

---

## 11. Worked end-to-end example

1. **Input:** "What is the current price of gold per ounce? Cite two sources."
2. **Run:** planner emits `researcher`. `researcher` calls `run_with_tools` → `mcp_runner.py:248`
   raises `UnboundLocalError` → node `failed` in 0.0 s with
   `result.error = "exception: UnboundLocalError: cannot access local variable '_os'"`.
3. **Recovery:** `recovery.py` re-plans. This repeats until `MAX_NODES=60`. The run ends with no answer,
   ~126 planner calls and ~385 k input tokens for that session — numbers already visible in
   `/api/cost/by_skill`.
4. **Optimiser observes:** `metrics.jsonl` records `skill=researcher, status=failed,
   error_class=UnboundLocalError, recovered=false` for every node. `/api/optimiser/overview` computes
   `fail_rate(researcher) = 1.00`, `recovery_loops = 1.00`. Because fail_rate is 1.00, the
   "reduce recovery loops" guard fires and the optimiser **refuses to propose anything** — it reports
   instead: *"researcher has failed 100% of runs with a single error class; this is a code fault, not a
   prompt fault. See `mcp_runner.py:248`."*
5. **Operator** fixes the one line. Restart. `fail_rate(researcher) → 0`.
6. **Only then** does the optimiser become useful on researcher: with the tool path alive, a recurring
   failure signature (e.g. "researcher returns a `sources:` block with 0 URLs on 4 of 5 runs") becomes a
   **Concept B proposal** — a concrete diff to `prompts/researcher.md`, with the four evidence sessions
   attached, awaiting human apply.
7. **Verified** by re-running eval set `core-v1` baseline vs candidate before the proposal is eligible.

That sequence is the whole thesis of this report: **the optimiser's first job is to correctly
distinguish "the code is broken" from "the prompt is bad".** Get that wrong and it will optimise a
one-character bug into a permanent architectural workaround.

---

## 12. Build order

| Phase | Content | Effort | Risk | Independently valuable? |
|---|---|---|---|---|
| 0 | Fix the blockers the optimiser would otherwise misread: `mcp_runner.py:248`, gateway token coherence `gateway.py:67-79`, `/documents` + `/code` token injection | 1–2 h | low | yes, hugely |
| 1 | `metrics.jsonl` writer (inside `outcomes.py`'s existing queue — never on the hot path) + `/api/optimiser/overview` + `/api/optimiser/regressions` + the `/optimiser` view with the header strip and per-skill table | 4–6 h | low | **yes — this is the deliverable** |
| 2 | Eval set: 20 cases + `/api/optimiser/evalset/run` + results view | 6–8 h | low | yes — makes every future change falsifiable |
| 3 | Concept B: proposals store, diff/apply/rollback endpoints, proposals panel in the UI | 8–12 h | **med** — first write path into prompt files | yes, gated |
| 4 | Concept C (routing/budget), only if 1–3 have 2–4 weeks of trend data | 12–20 h | high — touches the planner | questionable |

Phases 0 and 1 are the whole of the near-term value. Phase 3 is where the risk lives and should not be
started until someone trusts the Phase 1 numbers.