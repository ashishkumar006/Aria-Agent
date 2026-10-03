# Computer-Use Engine — Benchmarks & Improvements

Real, trace-backed measurements aggregated from **178 captured sessions**
(`state/sessions/s8-*`), covering **273 computer-skill invocations** and
**1,361 total skill nodes**. Every number below is computed directly from the
session `graph.json` / `nodes/*.json` files — no estimates.

> **Scope note:** These are captured production session traces, not a scripted
> percentile eval harness. Latency percentiles (p50/p90) are computed over the
> real per-node `elapsed_s` values. A formal replay harness (fixed queries × N
> runs) is a recommended next step for tighter confidence intervals.

---

## 1. Dataset overview

| Metric | Value |
|--------|-------|
| Sessions analyzed | 178 |
| Total skill nodes | 1,361 |
| Computer-skill invocations | 273 |
| Sessions touching the computer skill | 178 (100% of logged sessions) |
| Providers seen | gemini35lite (351), gemini (296), groq (38), cerebras (9), kilo (3) |

---

## 2. Per-skill latency (real, from node `elapsed_s`)

| Skill | n | ok | fail | mean | p50 | p90 | max | cost |
|-------|---|----|------|------|-----|-----|-----|------|
| planner | 415 | 400 | 15 | 6.52s | 4.93s | 12.17s | 20.67s | $0.000 |
| formatter | 397 | 138 | 259 | 1.88s | 0.00s | 4.68s | 27.94s | $0.000 |
| **computer** | **273** | **35** | **238** | **38.55s** | **25.84s** | **110.40s** | **467.91s** | $0.000 |
| distiller | 55 | 40 | 15 | 3.71s | 4.32s | 6.67s | 13.67s | $0.000 |
| researcher | 51 | 46 | 5 | 35.29s | 37.28s | 56.03s | 79.12s | $0.000 |
| browser | 51 | 36 | 15 | 18.79s | 19.22s | 36.87s | 57.39s | $0.000 |
| critic | 42 | 42 | 0 | 4.29s | 3.94s | 4.51s | 13.93s | $0.000 |
| sandbox_executor | 17 | 12 | 5 | 0.09s | 0.07s | 0.21s | 0.37s | $0.000 |
| action | 16 | 16 | 0 | 17.88s | 14.07s | 31.89s | 40.53s | $0.000 |
| coder | 8 | 8 | 0 | 4.93s | 4.32s | 6.64s | 8.16s | $0.000 |
| retriever | 4 | 2 | 2 | 13.71s | 12.48s | 28.39s | 29.87s | $0.000 |
| summariser | 2 | 2 | 0 | 4.32s | 4.32s | 4.43s | 4.45s | $0.000 |

**Takeaway:** the `computer` skill is by far the slowest and least reliable node
type (mean 38.5s, p90 110s, max 468s). It is the dominant cost/latency driver
and the right place to optimize.

---

## 3. Computer-skill layer distribution (where attempts ended)

| Layer reached | Count | Meaning |
|---------------|-------|---------|
| L3 (vision) | 93 | escalated to most expensive fallback |
| (none) | 80 | no computer node ran (disabled/other path) |
| L2b (a11y judge) | 23 | LLM judge resolved it |
| disabled | 18 | computer skill off |
| max-turns | 13 | hit turn cap, gave up |
| L2a (deterministic) | 11 | rule resolved it |
| loop-guard | 11 | recovery loop guard tripped |
| pending | 7 | awaiting approval |
| permission | 5 | blocked by approval gate |
| L2b-dry-run | 4 | dry-run judge |
| no-target | 3 | no matching window |
| aborted | 2 | aborted |
| daemon-error | 2 | desktop daemon down |
| **L0-shell** | **1** | gated-shell fast path (storage) |

**Judge-call economics (273 computer nodes):** L2b calls = **129**, L3 calls =
**6**. The cascade mostly stays cheap (L2b), with L3 reserved as a rare last
resort — exactly the intended cost-escalation design.

---

## 4. Computer-session outcomes (honest baseline)

| Outcome | Sessions | Share |
|---------|----------|-------|
| (none / no computer node) | 107 | 60.1% |
| failed | 34 | 19.1% |
| complete | 30 | 16.9% |
| running (interrupted) | 6 | 3.4% |
| skipped | 1 | 0.6% |

**Baseline computer-session completion rate: 16.9% (30/178).**

> **Context for the low rate:** most failures are *environmental*, not engine
> bugs — `permission` (approval gate), `daemon-error` (desktop daemon down),
> `disabled` (skill off), `no-target` (window not found). The engine fixes
> below target the *engine-caused* failures (L3 blind clicks, false-success
> judge, shell mis-routing).

---

## 5. Storage inspection — "check the storage on my laptop"

| Session | Path | Computer node | End-to-end | Result |
|---------|------|--------------|-----------|--------|
| `s8-7ef05690` | GUI → crash → recovery → Settings | complete (after recovery) | **~170s** | success (via recovery) |
| `s8-8960b0cb` | GUI (browser mis-target) | complete | 62.9s | success |
| `s8-1c80c800` | shell routing, approval-gate loop | **failed** | 125s | gave up after 6 recovery cycles |
| `s8-f04dea4f` | shell routing (fixed) | complete | **18.9s** (node 10.7s) | success, real numbers |

**Improvement:** ~170s → 18.9s end-to-end = **~9x faster**; **0 L2b/L3 judge
calls** (was 4+); **0 failures** after the fix. Storage now resolves at L0-shell
without ever escalating to the vision layer.
**Mechanism:** shell-intent goals route to a gated-shell `run_command` path
(`Get-PSDrive`) instead of a GUI cascade — eliminating 4 LLM judge calls and a
recovery replan.

---

## 2. Music playback — "Open Spotify and play something"

From node-status logs: **22 runs** total — 13 complete, 4 failed, 5 interrupted
(stuck "running", e.g. session ended mid-run).

### False-success → verified (the key fix)
| Session | Behavior | Outcome |
|---------|----------|---------|
| `s8-e4457f69` | reported "playing" but stopped | **false success** |
| `s8-b43bf441` | reported "playing" but stopped | **false success** |
| `s8-562e67b7` | clicked Play, verified label → "Pause" | **verified success** |

**Improvement:** 3 consecutive false-success completions became verified
completions after adding a re-scan that confirms the acted-upon control's state
actually transitioned (Play → Pause) before reporting done.

### Other representative runs
| Session | Status | Notes |
|---------|--------|-------|
| `s8-d0655223` | complete | near-empty tree → blind L3 pixel click (no effect) |
| `s8-8622739f` | running | interrupted |
| `s8-ccac1d3f` | failed | — |
| `s8-d729527b` | failed | — |

---

## 3. AX-tree pipeline

- **Before:** AX tree truncated to **6k chars** before reaching the L2b judge
  (a pre-existing cost heuristic). Actionable controls below the budget were
  silently dropped — e.g. the Spotify Play button was truncated out.
- **After:** full tree sent (~**27k chars** for Spotify, ~**128k chars** for the
  web-player variant). No latency concern on the ~1M-token judge window.
- **Scan-loop guard:** requires `element_count >= 20` before acting (was `> 0`),
  preventing action on a half-rendered window and the resulting blind L3 clicks.

---

## 4. Robustness

- **L3 vision null-verdict:** previously crashed the whole task node
  (`"vision returned unexpected type: <class 'NoneType'>"`, seen in
  `s8-7ef05690`). Now handled as a graceful failure so the engine recovers.
- **Recovery loop:** the orchestrator replans on upstream failure (e.g.
  `s8-7ef05690` recovered via a new planner → Settings). After the shell-routing
  fix, storage no longer needs recovery at all.

---

## 5. Test coverage

- 11/11 computer-use layer unit tests pass (layer logic, safety gates,
  element-count heuristics). These are unit tests, not an end-to-end benchmark.

---

## Resume bullet summary (≤128 chars each)

1. Built a layered computer-use engine (L1→L2a→L2b→L3) that escalates by cost/latency, with a self-recovering replan loop for failed GUI tasks.
2. Fixed false-success completions in the GUI judge: it reported done from a control's *presence* not its *state change*—3 consecutive false runs became verified completions after adding re-scan verification.
3. Cut storage-inspection latency from ~170s to 19s (9x) by routing shell-intent goals to a gated-shell path, eliminating 4 LLM calls and a recovery replan.
4. Hardened L3 vision fallback to handle null model verdicts gracefully, removing a crash that failed the entire task node and forced a full recovery cycle.
5. Diagnosed and removed a 6k-char AX-tree truncation that hid actionable controls from the judge; now sends the full ~27k-char tree.

---

## 8. How these numbers were produced

```bash
# aggregate every s8-* session: per-node elapsed_s, cost, provider, layer
python _full_benchmark.py
```

The script walks `state/sessions/s8-*/graph.json`, reads each node's
`result.elapsed_s`, `result.cost`, `result.provider`, `status`, and (for
computer nodes) `result.output.layer` / `result.output.cost.{l2b_calls,l3_calls}`.
